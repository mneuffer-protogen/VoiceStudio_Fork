"""Queue page: activity tracking (waiting / running / source) and learned estimates."""
from __future__ import annotations

import asyncio

import pytest
from core import activity, time_estimates


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    activity.reset_for_tests()
    time_estimates.reset_for_tests()
    monkeypatch.setattr(time_estimates, "_path", lambda: str(tmp_path / "est.json"))
    monkeypatch.setattr(activity, "_device", lambda: "cpu")
    yield
    activity.reset_for_tests()


def _scope(path="/generate", method="POST", headers=()):
    return {"type": "http", "method": method, "path": path, "headers": list(headers),
            "client": ("10.0.0.5", 1234)}


@pytest.mark.parametrize("path,headers,expected", [
    ("/v1/audio/speech", [(b"user-agent", b"Mozilla/5.0")], "api"),
    ("/mcp/messages", [], "mcp"),
    ("/generate", [(b"x-voicestudio-csrf", b"1")], "app"),
    ("/generate", [(b"user-agent", b"Mozilla/5.0 Chrome")], "app"),
    ("/generate", [(b"user-agent", b"python-requests/2.32")], "api"),
    ("/generate", [(b"authorization", b"Bearer k"), (b"user-agent", b"curl/8")], "api"),
])
def test_source_classification(path, headers, expected):
    assert activity.classify_source(_scope(path, headers=headers)) == expected


async def _serve(app, scope):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        pass

    await app(scope, receive, send)


def test_request_with_pool_job_moves_waiting_running_done_and_learns():
    gate = asyncio.Event()
    seen = {}

    async def route(scope, receive, send):
        activity.annotate(kind="tts", engine="omnivoice", steps=16, voice="v1", chars=100)
        job = activity.PoolJob("TTS generate")
        seen["waiting"] = activity.snapshot()["waiting"]
        job.started()
        seen["running"] = activity.snapshot()["running"]
        await gate.wait()
        job.finished()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def run():
        app = activity.ActivityMiddleware(route)
        task = asyncio.ensure_future(_serve(app, _scope(headers=[(b"x-forwarded-for", b"1.2.3.4, 10.0.0.1")])))
        await asyncio.sleep(0)
        gate.set()
        await task

    asyncio.run(run())
    waiting, = seen["waiting"]
    assert waiting["state"] == "waiting" and waiting["position"] == 1
    assert waiting["source"] == "api" and waiting["client"] == "1.2.3.4"
    assert waiting["details"] == {"engine": "omnivoice", "steps": 16, "voice": "v1", "chars": 100}
    running, = seen["running"]
    assert running["state"] == "running" and "remaining_s" in running["estimate"]
    snap = activity.snapshot()
    assert snap["running"] == [] and snap["waiting"] == []
    assert snap["recent"][0]["state"] == "done"
    # The finished job became a sample at every level of the hierarchy.
    assert time_estimates.estimate(
        {"kind": "tts", "device": "cpu", "engine": "omnivoice", "steps": 16, "voice": "v1", "chars": 100}
    )["samples"] == 1


def test_get_requests_and_polls_are_not_tracked():
    async def route(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def run():
        app = activity.ActivityMiddleware(route)
        await _serve(app, _scope("/history", "GET"))
        await _serve(app, _scope("/generate/result/abc", "POST"))

    asyncio.run(run())
    assert activity.snapshot()["recent"] == []


def test_pool_job_after_its_request_finished_gets_its_own_row():
    """A dub started by a POST that already answered still shows its origin."""
    holder = {}

    async def route(scope, receive, send):
        holder["ctx"] = __import__("contextvars").copy_context()
        await send({"type": "http.response.start", "status": 202, "headers": []})

    async def run():
        await _serve(activity.ActivityMiddleware(route), _scope("/dub/generate/j1", headers=[(b"x-voicestudio-csrf", b"1")]))
        job = holder["ctx"].run(activity.PoolJob, "Dub segment")
        job.started()
        row, = activity.snapshot()["running"]
        job.finished(RuntimeError("boom"))
        return row

    row = asyncio.run(run())
    assert row["label"] == "Dub segment" and row["source"] == "app" and row["parent_id"]
    assert activity.snapshot()["recent"][0]["state"] == "failed"


def test_waiting_estimates_queue_behind_running_work():
    for chars in (100, 100, 100):
        time_estimates.record({"kind": "tts", "device": "cpu", "chars": chars}, 60.0)
    first = activity._open(label="Generate speech", source="app", features={"kind": "tts", "chars": 100})
    a = activity.PoolJob.__new__(activity.PoolJob)
    a.activity, a._owned, a._state = first, False, "queued"
    first.queued_jobs = first.pool_jobs = 1
    a.started()
    for _ in range(2):
        nxt = activity._open(label="Generate speech", source="api", features={"kind": "tts", "chars": 100})
        nxt.queued_jobs = nxt.pool_jobs = 1
    snap = activity.snapshot(workers=1)
    one, two = snap["waiting"]
    assert one["estimate"]["starts_in_s"] == pytest.approx(snap["running"][0]["estimate"]["remaining_s"], abs=1)
    assert two["estimate"]["starts_in_s"] == pytest.approx(
        one["estimate"]["starts_in_s"] + one["estimate"]["seconds"], abs=0.2)


def test_estimates_learn_and_specific_evidence_wins():
    base = {"kind": "tts", "device": "cpu", "engine": "omnivoice", "steps": 16}
    cold = time_estimates.estimate({**base, "chars": 200})
    assert cold["samples"] == 0
    # Engine-wide: ~0.5 s per character.
    for chars in (50, 100, 200, 400, 800):
        time_estimates.record({**base, "voice": "fast", "chars": chars}, 5 + 0.5 * chars)
    learned = time_estimates.estimate({**base, "voice": "other", "chars": 300})
    assert learned["seconds"] == pytest.approx(155, rel=0.25)
    # One slow voice, seen many times, pulls its own estimate up.
    for chars in (100, 300) * 10:
        time_estimates.record({**base, "voice": "slow", "chars": chars}, 10 + 1.5 * chars)
    slow = time_estimates.estimate({**base, "voice": "slow", "chars": 200})
    fast = time_estimates.estimate({**base, "voice": "fast", "chars": 200})
    assert slow["seconds"] == pytest.approx(310, rel=0.1)
    assert slow["seconds"] > fast["seconds"] * 1.5
    assert slow["samples"] == 20


def test_estimates_persist(tmp_path):
    time_estimates.record({"kind": "transcribe", "device": "cpu"}, 42.0)
    time_estimates._nodes = None  # simulate a restart
    assert time_estimates.estimate({"kind": "transcribe", "device": "cpu"})["samples"] == 1
