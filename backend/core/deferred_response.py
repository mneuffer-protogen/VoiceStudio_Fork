"""Submit-then-poll for long requests behind proxies with idle timeouts.

The classic ``POST /generate`` answers only when the take is finished, and a
CPU render can be silent for many minutes. Proxies cut such requests: Railway
closes an HTTP request after 5 minutes with no bytes transferred, nginx after
``proxy_read_timeout``, Cloudflare at 100 s. The render keeps going and saves
the take, but the client sees a 502 and loses the result.

A client that sends ``Prefer: respond-async`` gets ``202 Accepted`` with a
``job_id`` immediately. The request is then served in the background, exactly
as if the client had waited: same route, same validation, same status, headers
and body. The client polls ``GET /generate/result/{job_id}``, which answers
``202`` while the request is still running and then replays the finished
response verbatim. ``DELETE`` on the same URL cancels it, like a disconnect.

Clients that do not send the header are untouched, and an older backend
ignores the header, so either side can be upgraded first.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import secrets
import time
from dataclasses import dataclass, field

logger = logging.getLogger("omnivoice.deferred")

#: Routes that accept ``Prefer: respond-async``. ``/v1/audio/speech`` lets API
#: clients (e.g. bulk narration) avoid Railway's 5-minute idle cut on long CPU
#: renders; they poll ``/generate/result/{id}`` like the app does.
DEFERRABLE_PATHS = frozenset({"/generate", "/v1/audio/speech"})
RESULT_PREFIX = "/generate/result/"
#: Finished results are dropped this long after they finish, fetched or not.
RESULT_TTL_S = 3600.0
#: ...and this long after they are first fetched (a retry after a dropped
#: connection can still read them).
FETCHED_TTL_S = 300.0
POLL_AFTER_S = 2

#: True while a route serves a ``Prefer: respond-async`` request. Nobody holds
#: a connection open for it, so a route may let it wait in the inference queue
#: instead of refusing it as busy (see ``is_deferred``).
_deferred: contextvars.ContextVar[bool] = contextvars.ContextVar("omnivoice_deferred", default=False)


def is_deferred() -> bool:
    """Whether the current request is being served in the background."""
    return _deferred.get()


@dataclass
class _Deferred:
    task: "asyncio.Task | None" = None
    status: int | None = None
    headers: list = field(default_factory=list)
    body: list = field(default_factory=list)
    finished_at: float | None = None
    fetched_at: float | None = None


_jobs: dict[str, _Deferred] = {}


def _prefers_async(scope) -> bool:
    for name, value in scope.get("headers") or ():
        if name == b"prefer" and b"respond-async" in value.lower():
            return True
    return False


def _purge(now: float) -> None:
    for job_id, job in list(_jobs.items()):
        if job.finished_at is None:
            continue
        if now - job.finished_at > RESULT_TTL_S or (
            job.fetched_at is not None and now - job.fetched_at > FETCHED_TTL_S
        ):
            _jobs.pop(job_id, None)


async def _send_json(send, status: int, payload: dict, extra_headers=()) -> None:
    body = json.dumps(payload).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
            *extra_headers,
        ],
    })
    await send({"type": "http.response.body", "body": body})


class DeferredResponseMiddleware:
    """Serve ``Prefer: respond-async`` requests in the background (pure ASGI)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method")
        if path.startswith(RESULT_PREFIX) and method in ("GET", "DELETE"):
            await self._result(path[len(RESULT_PREFIX):], method, send)
            return
        if method == "POST" and path in DEFERRABLE_PATHS and _prefers_async(scope):
            await self._submit(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _submit(self, scope, receive, send) -> None:
        # The client may hang up as soon as it has its job id, so the whole
        # request body is read up front and replayed to the route.
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)

        _purge(time.monotonic())
        job_id = secrets.token_urlsafe(16)
        job = _Deferred()
        _jobs[job_id] = job
        job.task = asyncio.create_task(
            self._run(dict(scope), body, job), name=f"deferred-{scope.get('path')}"
        )
        await _send_json(
            send, 202,
            {"job_id": job_id, "status": "running", "poll_after": POLL_AFTER_S},
            [(b"location", f"{RESULT_PREFIX}{job_id}".encode())],
        )

    async def _run(self, scope, body: bytes, job: _Deferred) -> None:
        # This task has its own context (copied at create_task), so the flag
        # never leaks into other requests.
        _deferred.set(True)
        delivered = False
        never = asyncio.Event()

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            # Nobody is connected; wait until cancelled (DELETE or shutdown).
            await never.wait()
            return {"type": "http.disconnect"}

        async def capture_send(message):
            if message["type"] == "http.response.start":
                job.status = message["status"]
                job.headers = list(message.get("headers") or [])
            elif message["type"] == "http.response.body":
                job.body.append(message.get("body", b""))

        try:
            await self.app(scope, replay_receive, capture_send)
            if job.status is None:
                raise RuntimeError("route finished without a response")
        except asyncio.CancelledError:
            job.status = 499
            job.headers = [(b"content-type", b"application/json")]
            job.body = [json.dumps({"detail": "Request cancelled."}).encode()]
            raise
        except Exception as exc:  # noqa: BLE001 — the client must still get an answer
            # Outside the app's own 500 handler, so log and journal it here.
            logger.exception("Deferred %s failed", scope.get("path"))
            try:
                import traceback

                from core import error_journal

                error_journal.record(exc, route=scope.get("path", ""), trace=traceback.format_exc())
            except Exception:  # noqa: BLE001
                pass
            job.status = 500
            job.headers = [(b"content-type", b"application/json")]
            job.body = [json.dumps({"detail": "Internal Server Error"}).encode()]
        finally:
            job.finished_at = time.monotonic()

    async def _result(self, job_id: str, method: str, send) -> None:
        now = time.monotonic()
        _purge(now)
        job = _jobs.get(job_id)
        if job is None:
            await _send_json(send, 404, {"detail": "Unknown or expired generation job."})
            return
        if method == "DELETE":
            if job.task is not None and not job.task.done():
                job.task.cancel()
            _jobs.pop(job_id, None)
            await _send_json(send, 200, {"job_id": job_id, "status": "cancelled"})
            return
        if job.finished_at is None:
            await _send_json(
                send, 202, {"job_id": job_id, "status": "running", "poll_after": POLL_AFTER_S}
            )
            return
        if job.fetched_at is None:
            job.fetched_at = now
        await send({"type": "http.response.start", "status": job.status, "headers": job.headers})
        await send({"type": "http.response.body", "body": b"".join(job.body)})


def cancel_all() -> None:
    """Cancel every running deferred request (backend shutdown)."""
    for job in list(_jobs.values()):
        if job.task is not None and not job.task.done():
            job.task.cancel()
