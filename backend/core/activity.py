"""What the backend is doing right now, and for whom — the Queue page's data.

Each piece of work is an *activity*: a state-changing HTTP request (from the
app, an API client or MCP), or background work that request started (a dub,
an audiobook, a deferred generate). Activities are tracked in memory only.

* ``ActivityMiddleware`` opens an activity for each non-GET request and binds
  it to the request's context, so everything the request runs (including
  ``asyncio`` tasks it spawns, which copy the context) can find it.
* The inference pool (``services.model_manager.run_on_gpu_pool_guarded``)
  reports each job as queued → running → finished on the current activity,
  which is how an activity is "waiting" (in line for the single worker) or
  "running".
* Routes call :func:`annotate` with what predicts duration (engine, steps,
  voice, characters); finished activities feed :mod:`core.time_estimates`.

Nothing here leaves the process; the Queue page reads it through the
admin-gated ``GET /queue``.
"""
from __future__ import annotations

import contextvars
import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field

_IGNORED_PREFIXES = (
    "/generate/result/", "/queue", "/api/auth/", "/health", "/startup/",
    "/system/shutdown-intent", "/ws/",
)
_LABELS = (
    ("/v1/audio/speech", "Speech (OpenAI API)"),
    ("/v1/audio/transcriptions", "Transcription (OpenAI API)"),
    ("/v1/audio/translations", "Translation (OpenAI API)"),
    ("/generate", "Generate speech"),
    ("/transcribe", "Transcribe"),
    ("/dub/translate", "Dub: translate"),
    ("/dub/generate", "Dub: generate voices"),
    ("/dub/preview-segment", "Dub: preview line"),
    ("/dub/", "Dub"),
    ("/audiobook", "Audiobook"),
    ("/longform", "Long-form render"),
    ("/stories", "Story"),
    ("/batch", "Batch"),
    ("/design", "Voice design"),
    ("/voice-convert", "Voice conversion"),
    ("/convert", "Voice conversion"),
    ("/tools", "Audio tool"),
    ("/profiles", "Voice profile"),
    ("/mcp", "MCP request"),
)
RECENT_LIMIT = 50

_ids = itertools.count(1)
_lock = threading.Lock()
_active: dict[int, Activity] = {}
_recent: deque[Activity] = deque(maxlen=RECENT_LIMIT)
_current: contextvars.ContextVar[Activity | None] = contextvars.ContextVar(
    "omnivoice_activity", default=None
)


@dataclass
class Activity:
    id: int
    label: str
    source: str  # app | api | mcp | background
    method: str = ""
    path: str = ""
    client: str = ""
    agent: str = ""
    parent_id: int | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    queued_jobs: int = 0
    running_jobs: int = 0
    pool_jobs: int = 0
    failed_jobs: int = 0
    status: int | None = None
    error: str = ""
    features: dict = field(default_factory=dict)
    # Monotonic times for durations, independent of wall-clock changes.
    _t_created: float = field(default_factory=time.monotonic)
    _t_started: float | None = None
    _t_finished: float | None = None

    @property
    def state(self) -> str:
        if self.finished_at is not None:
            return "failed" if self.failed else "done"
        if self.running_jobs:
            return "running"
        if self.queued_jobs:
            return "waiting"
        return "running" if self.started_at else "starting"

    @property
    def failed(self) -> bool:
        return bool(self.error) or (self.status or 0) >= 400 or (
            self.failed_jobs > 0 and self.status is None
        )

    def execution_s(self) -> float | None:
        if self._t_started is None:
            return None
        end = self._t_finished if self._t_finished is not None else time.monotonic()
        return max(0.0, end - self._t_started)

    def elapsed_s(self) -> float:
        end = self._t_finished if self._t_finished is not None else time.monotonic()
        return max(0.0, end - self._t_created)


# ── request classification ────────────────────────────────────────────────


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return ""


def classify_source(scope) -> str:
    """``app`` (the VoiceStudio UI), ``api`` (scripts, SDKs) or ``mcp``."""
    path = scope.get("path", "")
    if path.startswith("/mcp"):
        return "mcp"
    if path.startswith("/v1/"):
        return "api"
    if _header(scope, b"x-voicestudio-csrf") or _header(scope, b"sec-fetch-site"):
        return "app"
    agent = _header(scope, b"user-agent")
    if "Electron" in agent or agent.startswith("Mozilla/"):
        return "app"
    return "api"


def _client(scope) -> str:
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else ""


def label_for(path: str, method: str = "POST") -> str:
    for prefix, label in _LABELS:
        if path.startswith(prefix):
            return label
    return f"{method} {path}"


def _tracked(scope) -> bool:
    if scope.get("type") != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
        return False
    return not scope.get("path", "").startswith(_IGNORED_PREFIXES)


# ── lifecycle ─────────────────────────────────────────────────────────────


def _open(**kwargs) -> Activity:
    activity = Activity(id=next(_ids), **kwargs)
    with _lock:
        _active[activity.id] = activity
    return activity


def _close(activity: Activity) -> None:
    if activity._t_finished is not None:
        return
    activity._t_finished = time.monotonic()
    activity.finished_at = time.time()
    with _lock:
        _active.pop(activity.id, None)
        if activity.pool_jobs or activity.elapsed_s() >= 1.0:
            _recent.appendleft(activity)
    execution = activity.execution_s()
    if execution and not activity.failed and activity.pool_jobs:
        try:
            from core import time_estimates

            time_estimates.record(_estimate_features(activity), execution)
        except Exception:  # noqa: BLE001, S110 — estimates are advisory
            pass


class ActivityMiddleware:
    """Open an activity for each state-changing request (pure ASGI)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if not _tracked(scope):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        activity = _open(
            label=label_for(path, method),
            source=classify_source(scope),
            method=method,
            path=path,
            client=_client(scope),
            agent=_header(scope, b"user-agent")[:120],
        )
        token = _current.set(activity)

        async def tracking_send(message):
            if message["type"] == "http.response.start":
                activity.status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except BaseException as exc:
            activity.error = type(exc).__name__
            raise
        finally:
            _current.reset(token)
            _close(activity)


def annotate(**features) -> None:
    """Attach duration-relevant features to the current activity."""
    activity = _current.get()
    if activity is None or activity.finished_at is not None:
        return
    activity.features.update({k: v for k, v in features.items() if v not in (None, "")})


def _owner(what: str) -> tuple[Activity, bool]:
    """The activity a pool job belongs to; ``True`` when it was opened here.

    Work that outlives its request (a dub job started by a POST that already
    answered) gets its own activity, keeping the request's source as its
    origin, so the page still says where it came from."""
    parent = _current.get()
    if parent is not None and parent.finished_at is None:
        return parent, False
    activity = _open(
        label=what or (parent.label if parent else "Background work"),
        source=parent.source if parent else "background",
        method=parent.method if parent else "",
        path=parent.path if parent else "",
        client=parent.client if parent else "",
        agent=parent.agent if parent else "",
        parent_id=parent.id if parent else None,
        features=dict(parent.features) if parent else {},
    )
    return activity, True


class PoolJob:
    """One inference-pool job's lifecycle, reported to its activity."""

    def __init__(self, what: str):
        self.activity, self._owned = _owner(what)
        self.activity.queued_jobs += 1
        self.activity.pool_jobs += 1
        self._state = "queued"

    def started(self) -> None:
        if self._state != "queued":
            return
        self._state = "running"
        activity = self.activity
        activity.queued_jobs -= 1
        activity.running_jobs += 1
        if activity._t_started is None:
            activity._t_started = time.monotonic()
            activity.started_at = time.time()

    def finished(self, error: BaseException | None = None) -> None:
        if self._state == "done":
            return
        activity = self.activity
        if self._state == "queued":
            activity.queued_jobs -= 1
        else:
            activity.running_jobs -= 1
        self._state = "done"
        if error is not None:
            activity.failed_jobs += 1
            if self._owned:
                activity.error = type(error).__name__
        if self._owned and not activity.queued_jobs and not activity.running_jobs:
            _close(activity)


# ── snapshot for the Queue page ───────────────────────────────────────────


def _device() -> str:
    try:
        from core.device_caps import detect_host_caps

        return str(detect_host_caps().family)
    except Exception:  # noqa: BLE001
        return ""


def _estimate_features(activity: Activity) -> dict:
    features = dict(activity.features)
    features.setdefault("kind", activity.label)
    features.setdefault("device", _device())
    return features


def _row(activity: Activity) -> dict:
    return {
        "id": activity.id,
        "label": activity.label,
        "state": activity.state,
        "source": activity.source,
        "method": activity.method,
        "path": activity.path,
        "client": activity.client,
        "agent": activity.agent,
        "parent_id": activity.parent_id,
        "created_at": activity.created_at,
        "started_at": activity.started_at,
        "finished_at": activity.finished_at,
        "elapsed_s": round(activity.elapsed_s(), 1),
        "execution_s": (
            round(activity.execution_s(), 1) if activity.execution_s() is not None else None
        ),
        "status": activity.status,
        "error": activity.error,
        "details": {
            k: activity.features[k]
            for k in ("engine", "steps", "voice", "chars", "device")
            if k in activity.features
        },
    }


def snapshot(workers: int = 1) -> dict:
    """Running, waiting (in queue order) and recent activities with estimates."""
    from core import time_estimates

    with _lock:
        active = sorted(_active.values(), key=lambda a: a._t_created)
        recent = list(_recent)
    running, waiting = [], []
    for activity in active:
        if activity.state == "waiting":
            waiting.append(activity)
        elif activity.pool_jobs or activity.elapsed_s() >= 1.0:
            running.append(activity)

    lanes: list[float] = []
    running_rows = []
    for activity in running:
        row = _row(activity)
        if activity.pool_jobs:
            guess = time_estimates.estimate(_estimate_features(activity))
            done = activity.execution_s() or 0.0
            remaining = max(0.0, guess["seconds"] - done)
            row["estimate"] = {**guess, "remaining_s": round(remaining, 1)}
            if activity.running_jobs:
                lanes.append(remaining)
        running_rows.append(row)

    workers = max(1, int(workers))
    lanes = sorted(lanes)[:workers] + [0.0] * max(0, workers - len(lanes))
    waiting_rows = []
    for position, activity in enumerate(waiting, start=1):
        guess = time_estimates.estimate(_estimate_features(activity))
        lanes.sort()
        starts_in = lanes[0]
        lanes[0] = starts_in + guess["seconds"]
        row = _row(activity)
        row["position"] = position
        row["estimate"] = {
            **guess,
            "starts_in_s": round(starts_in, 1),
            "remaining_s": round(starts_in + guess["seconds"], 1),
        }
        waiting_rows.append(row)

    return {
        "workers": workers,
        "running": running_rows,
        "waiting": waiting_rows,
        "recent": [_row(a) for a in recent],
    }


def reset_for_tests() -> None:
    with _lock:
        _active.clear()
        _recent.clear()
