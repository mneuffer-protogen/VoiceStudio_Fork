"""`Prefer: respond-async` on /generate: submit, poll, replay (proxy idle timeouts).

Railway closes an HTTP request after 5 minutes with no bytes; a long CPU take
was cut with a 502 while the backend went on to save it.
"""
from __future__ import annotations

import asyncio
import json

from core import deferred_response as dr


async def _route(scope, receive, send):
    """Stand-in /generate: echoes the body after a (cancellable) render."""
    message = await receive()
    body = message["body"]
    if body == b"boom":
        raise RuntimeError("engine exploded")
    await asyncio.sleep(0.05 if body != b"slow" else 3600)
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"audio/wav"), (b"x-audio-id", b"take-1")],
    })
    await send({"type": "http.response.body", "body": b"RIFF" + body, "more_body": True})
    await send({"type": "http.response.body", "body": b"!"})


async def _call(app, method, path, body=b"", headers=()):
    sent = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    await app({"type": "http", "method": method, "path": path, "headers": list(headers)},
              receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), data


ASYNC = [(b"prefer", b"respond-async")]


def test_without_prefer_the_route_answers_directly():
    async def run():
        app = dr.DeferredResponseMiddleware(_route)
        return await _call(app, "POST", "/generate", b"hi")

    status, headers, body = asyncio.run(run())
    assert (status, body, headers[b"x-audio-id"]) == (200, b"RIFFhi!", b"take-1")


def test_submit_poll_and_replay_the_exact_response():
    async def run():
        app = dr.DeferredResponseMiddleware(_route)
        status, headers, body = await _call(app, "POST", "/generate", b"hi", ASYNC)
        assert status == 202
        job_id = json.loads(body)["job_id"]
        assert headers[b"location"] == f"/generate/result/{job_id}".encode()

        status, _h, body = await _call(app, "GET", f"/generate/result/{job_id}")
        assert status == 202 and json.loads(body)["status"] == "running"

        await asyncio.sleep(0.1)
        first = await _call(app, "GET", f"/generate/result/{job_id}")
        again = await _call(app, "GET", f"/generate/result/{job_id}")
        return first, again

    first, again = asyncio.run(run())
    assert first == again  # a retried fetch still gets the take
    status, headers, body = first
    assert (status, body, headers[b"x-audio-id"]) == (200, b"RIFFhi!", b"take-1")


def test_route_errors_become_a_500_result_not_a_lost_job():
    async def run():
        app = dr.DeferredResponseMiddleware(_route)
        _s, _h, body = await _call(app, "POST", "/generate", b"boom", ASYNC)
        job_id = json.loads(body)["job_id"]
        await asyncio.sleep(0.01)
        return await _call(app, "GET", f"/generate/result/{job_id}")

    status, _headers, body = asyncio.run(run())
    assert status == 500
    assert json.loads(body)["detail"] == "Internal Server Error"


def test_delete_cancels_the_render_and_forgets_the_job():
    async def run():
        app = dr.DeferredResponseMiddleware(_route)
        _s, _h, body = await _call(app, "POST", "/generate", b"slow", ASYNC)
        job_id = json.loads(body)["job_id"]
        task = dr._jobs[job_id].task
        status, _h, _b = await _call(app, "DELETE", f"/generate/result/{job_id}")
        await asyncio.sleep(0)
        await asyncio.gather(task, return_exceptions=True)
        missing = await _call(app, "GET", f"/generate/result/{job_id}")
        return status, task.cancelled(), missing[0]

    assert asyncio.run(run()) == (200, True, 404)


def test_openai_speech_is_deferrable_and_polls_the_shared_result_route():
    async def run():
        app = dr.DeferredResponseMiddleware(_route)
        status, _h, body = await _call(app, "POST", "/v1/audio/speech", b"hi", ASYNC)
        assert status == 202
        job_id = json.loads(body)["job_id"]
        await asyncio.sleep(0.1)
        return await _call(app, "GET", f"/generate/result/{job_id}")

    status, headers, body = asyncio.run(run())
    assert (status, body, headers[b"x-audio-id"]) == (200, b"RIFFhi!", b"take-1")


def test_only_generate_is_deferrable():
    async def run():
        app = dr.DeferredResponseMiddleware(_route)
        return await _call(app, "POST", "/dub/generate/x", b"hi", ASYNC)

    assert asyncio.run(run())[0] == 200


def test_finished_results_expire(monkeypatch):
    job = dr._Deferred(status=200, finished_at=0.0)
    dr._jobs["old"] = job
    dr._purge(dr.RESULT_TTL_S + 1)
    assert "old" not in dr._jobs
    fetched = dr._Deferred(status=200, finished_at=100.0, fetched_at=100.0)
    dr._jobs["fetched"] = fetched
    dr._purge(100.0 + dr.FETCHED_TTL_S + 1)
    assert "fetched" not in dr._jobs


def test_routes_can_tell_a_background_request_from_a_held_open_one():
    seen = []

    async def route(scope, receive, send):
        await receive()
        seen.append(dr.is_deferred())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def run():
        app = dr.DeferredResponseMiddleware(route)
        await _call(app, "POST", "/v1/audio/speech", b"x")
        status, _h, body = await _call(app, "POST", "/v1/audio/speech", b"x", ASYNC)
        job_id = json.loads(body)["job_id"]
        await asyncio.sleep(0.05)
        await _call(app, "GET", f"/generate/result/{job_id}")
        return dr.is_deferred()

    outside = asyncio.run(run())
    assert seen == [False, True]
    assert outside is False
