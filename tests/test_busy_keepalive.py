"""Opt-in keep-alive for scale-to-zero hosts (Railway Serverless).

Railway sleeps a container after a few minutes with no outbound packets, even
while a long CPU render is running or jobs are queued. The keep-alive pings
the deployment's own URL only while work is in progress.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from core import busy_keepalive as ka


def test_off_unless_configured(monkeypatch):
    monkeypatch.delenv(ka.KEEPALIVE_URL_ENV, raising=False)
    assert ka.keepalive_url() is None

    async def run():
        return ka.start_keepalive()

    assert asyncio.run(run()) is None


@pytest.mark.parametrize("value", ["ftp://host/health", "not a url", "https://"])
def test_rejects_non_http_urls(monkeypatch, value):
    monkeypatch.setenv(ka.KEEPALIVE_URL_ENV, value)
    assert ka.keepalive_url() is None


def test_interval_default_and_floor(monkeypatch):
    monkeypatch.delenv(ka.KEEPALIVE_INTERVAL_ENV, raising=False)
    assert ka.keepalive_interval_s() == 60.0
    monkeypatch.setenv(ka.KEEPALIVE_INTERVAL_ENV, "1")
    assert ka.keepalive_interval_s() == 10.0
    monkeypatch.setenv(ka.KEEPALIVE_INTERVAL_ENV, "nan")
    assert ka.keepalive_interval_s() == 60.0
    monkeypatch.setenv(ka.KEEPALIVE_INTERVAL_ENV, "junk")
    assert ka.keepalive_interval_s() == 60.0


def test_pings_only_while_busy(monkeypatch):
    monkeypatch.setattr(ka.asyncio, "sleep", _instant_sleep)
    states = iter([["job"], [], ["request"], ["request"]])
    pings: list[str] = []

    async def run():
        task = asyncio.ensure_future(ka.run_keepalive(
            "https://example.test/health", 60, probe=lambda: _next(states), ping=pings.append,
        ))
        with pytest.raises(_Done):
            await task

    asyncio.run(run())
    assert pings == ["https://example.test/health"] * 3


def test_failed_ping_does_not_stop_the_loop(monkeypatch):
    monkeypatch.setattr(ka.asyncio, "sleep", _instant_sleep)
    states = iter([["job"], ["job"]])
    calls = []

    def ping(url):
        calls.append(url)
        raise OSError("unreachable")

    async def run():
        with pytest.raises(_Done):
            await ka.run_keepalive("https://x.test/", 60, probe=lambda: _next(states), ping=ping)

    asyncio.run(run())
    assert len(calls) == 2


def test_middleware_counts_requests_but_not_health(monkeypatch):
    seen = []

    async def app(scope, receive, send):
        seen.append(ka.in_flight_requests())

    mw = ka.InFlightRequestsMiddleware(app)

    async def run():
        await mw({"type": "http", "path": "/generate"}, None, None)
        await mw({"type": "http", "path": "/health"}, None, None)
        await mw({"type": "websocket", "path": "/ws/events"}, None, None)

    asyncio.run(run())
    assert seen == [1, 0, 0]
    assert ka.in_flight_requests() == 0


def test_busy_reasons_reads_loaded_queues(monkeypatch):
    for name in ("services.model_manager", "core.tasks", "core.job_queue"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    assert ka.busy_reasons() == []

    mm = types.SimpleNamespace(
        _gpu_pool_singleton=object(),
        gpu_pool_stats=lambda: {"running": 1, "queued": 2},
    )
    tasks = types.SimpleNamespace(task_manager=types.SimpleNamespace(active_tasks={
        "a": {"status": "running"}, "b": {"status": "done"}, "c": {"status": "pending"},
    }))
    jobs = types.SimpleNamespace(gpu_queue=types.SimpleNamespace(list_jobs=lambda: []))
    monkeypatch.setitem(sys.modules, "services.model_manager", mm)
    monkeypatch.setitem(sys.modules, "core.tasks", tasks)
    monkeypatch.setitem(sys.modules, "core.job_queue", jobs)
    reasons = ka.busy_reasons()
    assert reasons == [
        "3 inference job(s) running or queued",
        "2 background task(s) pending or running",
    ]


class _Done(Exception):
    pass


def _next(states):
    try:
        return next(states)
    except StopIteration:
        raise _Done from None


_real_sleep = asyncio.sleep


async def _instant_sleep(_seconds):
    await _real_sleep(0)
