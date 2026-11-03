"""Keep a scale-to-zero host awake while the backend is busy (opt-in).

Hosts with app sleeping (Railway Serverless, and similar idle-stop features)
put a container to sleep after it has sent no *outbound* packets for a few
minutes. They do not look at in-flight requests or queued work. A long CPU
render sends nothing until it finishes, so it can be put to sleep mid-job, and
a queued dub or audiobook can be dropped the same way.

When ``OMNIVOICE_KEEPALIVE_URL`` is set (an operator setting, normally the
deployment's own ``https://<host>/health``), this module sends a small GET to
it every ``OMNIVOICE_KEEPALIVE_INTERVAL_S`` seconds (default 60) **only while
something is in progress**: an HTTP request in flight, a job running or queued
on the inference pool, or a background task pending or running. Once
everything is idle the pings stop and the host may sleep as usual.

Unset (the default, and every desktop install), nothing is started and no
request is ever made.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import urllib.request
from collections.abc import Callable
from urllib.parse import urlparse

logger = logging.getLogger("omnivoice.keepalive")

KEEPALIVE_URL_ENV = "OMNIVOICE_KEEPALIVE_URL"
KEEPALIVE_INTERVAL_ENV = "OMNIVOICE_KEEPALIVE_INTERVAL_S"
_DEFAULT_INTERVAL_S = 60.0
_MIN_INTERVAL_S = 10.0
_PING_TIMEOUT_S = 10.0

#: Probe paths that never count as work: the keep-alive's own ping lands on
#: /health, and orchestrator health probes must not keep an idle host awake.
_IDLE_PATHS = frozenset({"/health", "/startup/progress"})

_in_flight = 0


class InFlightRequestsMiddleware:
    """Count HTTP requests currently being served (pure ASGI, no buffering)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("path", "") in _IDLE_PATHS:
            await self.app(scope, receive, send)
            return
        global _in_flight
        _in_flight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            _in_flight -= 1


def in_flight_requests() -> int:
    return _in_flight


def busy_reasons() -> list[str]:
    """What is keeping the backend busy right now; empty when idle.

    Reads only modules that are already imported, so the probe never triggers
    a heavy import (torch via the model manager) on an idle server."""
    reasons: list[str] = []
    if _in_flight > 0:
        reasons.append(f"{_in_flight} request(s) in flight")

    mm = sys.modules.get("services.model_manager")
    if mm is not None and getattr(mm, "_gpu_pool_singleton", None) is not None:
        try:
            stats = mm.gpu_pool_stats()
            active = int(stats.get("running") or 0) + int(stats.get("queued") or 0)
            if active:
                reasons.append(f"{active} inference job(s) running or queued")
        except Exception:  # noqa: BLE001 — a probe failure must not stop the loop
            logger.debug("inference pool probe failed", exc_info=True)

    tasks = sys.modules.get("core.tasks")
    if tasks is not None:
        try:
            live = sum(
                1
                for task in list(tasks.task_manager.active_tasks.values())
                if task.get("status") in ("pending", "running")
            )
            if live:
                reasons.append(f"{live} background task(s) pending or running")
        except Exception:  # noqa: BLE001
            logger.debug("task manager probe failed", exc_info=True)

    jobs = sys.modules.get("core.job_queue")
    if jobs is not None:
        try:
            live = len(jobs.gpu_queue.list_jobs())
            if live:
                reasons.append(f"{live} queued job(s)")
        except Exception:  # noqa: BLE001
            logger.debug("job queue probe failed", exc_info=True)
    return reasons


def keepalive_url() -> str | None:
    """The configured ping URL, or None when unset or not an http(s) URL."""
    raw = (os.environ.get(KEEPALIVE_URL_ENV) or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        logger.warning("%s is not an http(s) URL; keep-alive disabled", KEEPALIVE_URL_ENV)
        return None
    return raw


def keepalive_interval_s() -> float:
    raw = os.environ.get(KEEPALIVE_INTERVAL_ENV, "")
    try:
        value = float(raw) if raw.strip() else _DEFAULT_INTERVAL_S
    except ValueError:
        value = _DEFAULT_INTERVAL_S
    if value != value:  # NaN
        value = _DEFAULT_INTERVAL_S
    return max(_MIN_INTERVAL_S, value)


def _ping(url: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "VoiceStudio-keepalive"})
    with urllib.request.urlopen(request, timeout=_PING_TIMEOUT_S) as response:  # nosec B310 — operator-set http(s) URL
        response.read(256)


async def run_keepalive(
    url: str,
    interval_s: float,
    *,
    probe: Callable[[], list[str]] = busy_reasons,
    ping: Callable[[str], None] = _ping,
) -> None:
    """Ping ``url`` every ``interval_s`` seconds while ``probe`` reports work."""
    was_busy = False
    while True:
        await asyncio.sleep(interval_s)
        reasons = probe()
        if not reasons:
            if was_busy:
                logger.info("Keep-alive: idle, pings stopped (host may sleep)")
            was_busy = False
            continue
        if not was_busy:
            logger.info("Keep-alive: busy (%s), pinging to stay awake", "; ".join(reasons))
        was_busy = True
        try:
            await asyncio.to_thread(ping, url)
        except Exception as exc:  # noqa: BLE001 — a failed ping still sent packets
            logger.warning("Keep-alive ping failed: %s", type(exc).__name__)


def start_keepalive() -> "asyncio.Task | None":
    """Start the loop when configured; return its task, or None when off."""
    url = keepalive_url()
    if url is None:
        return None
    interval = keepalive_interval_s()
    logger.info("Keep-alive enabled: pinging while busy every %.0fs", interval)
    return asyncio.create_task(run_keepalive(url, interval), name="busy-keepalive")
