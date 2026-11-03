"""Queue page data: what is running, what is waiting, and where it came from."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from api.dependencies import require_admin

router = APIRouter(tags=["queue"])


@router.get("/queue", include_in_schema=False, dependencies=[Depends(require_admin)])
async def get_queue():
    """Running and waiting work with learned time estimates, plus recent history.

    Admin-gated: rows show client addresses and user agents."""
    from core import activity

    workers = 1
    try:
        from services.model_manager import gpu_pool_stats

        workers = int(gpu_pool_stats().get("workers") or 1)
    except Exception:  # noqa: BLE001, S110 — the page still renders with one lane
        pass
    return activity.snapshot(workers=workers)
