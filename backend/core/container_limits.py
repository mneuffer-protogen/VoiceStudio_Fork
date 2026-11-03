"""Container-aware CPU and memory figures.

``os.cpu_count()`` and ``psutil.virtual_memory()`` describe the HOST, not the
container. On a hosted CPU deployment (Railway, Fly, Kubernetes, ``docker run
--cpus/--memory``) the container is throttled by a cgroup CPU quota and killed
by a cgroup memory limit that both figures ignore. The consequences:

* torch/OpenMP/MKL size their thread pools to every host core (often 32-128)
  while the quota allows a handful, so CFS throttling makes CPU inference many
  times slower and requests run into their timeouts.
* RAM preflights see hundreds of GB "available", so background preloads and
  model loads go ahead until the kernel OOM-kills the process — a restart with
  no Python traceback that drops every in-flight request.

Every CPU-count and RAM decision goes through this module instead; on a host
with no cgroup limit it returns exactly the host figures. Stdlib + psutil;
never raises.
"""
from __future__ import annotations

import math
import os
from collections import namedtuple
from pathlib import Path

_CGROUP_ROOT = Path("/sys/fs/cgroup")
# cgroup v1 reports "no limit" as a page-aligned near-2^63 number.
_V1_UNLIMITED = 1 << 60

#: Thread-count variables read by OpenMP, MKL, OpenBLAS, numexpr, onnxruntime
#: users and ggml/llama.cpp wrappers. Set only when the operator left them unset.
THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)

VirtualMemory = namedtuple("VirtualMemory", "total available used percent")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return None


def _read_int(path: Path) -> int | None:
    raw = _read(path)
    if raw is None or raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def cgroup_cpu_limit(root: Path = _CGROUP_ROOT) -> float | None:
    """CPUs allowed by the cgroup quota, or None when unlimited/unknown."""
    raw = _read(root / "cpu.max")  # v2: "<quota> <period>" or "max <period>"
    if raw:
        parts = raw.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = int(parts[0]), int(parts[1])
            except ValueError:
                return None
            return quota / period if quota > 0 and period > 0 else None
        return None
    quota = _read_int(root / "cpu" / "cpu.cfs_quota_us") or _read_int(root / "cpu.cfs_quota_us")
    period = _read_int(root / "cpu" / "cpu.cfs_period_us") or _read_int(root / "cpu.cfs_period_us")
    if quota and period and quota > 0 and period > 0:
        return quota / period
    return None


def effective_cpu_count(root: Path = _CGROUP_ROOT) -> int:
    """Usable CPUs: host count, narrowed by CPU affinity and the cgroup quota."""
    count = os.cpu_count() or 1
    try:
        count = min(count, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass  # macOS/Windows: no affinity API
    quota = cgroup_cpu_limit(root)
    if quota is not None:
        count = min(count, math.ceil(quota))
    return max(1, count)


def cgroup_memory_limit(root: Path = _CGROUP_ROOT) -> int | None:
    """Memory limit in bytes from the cgroup, or None when unlimited/unknown."""
    limit = _read_int(root / "memory.max")
    if limit is None:
        limit = _read_int(root / "memory" / "memory.limit_in_bytes") or _read_int(
            root / "memory.limit_in_bytes"
        )
    if limit is None or limit <= 0 or limit >= _V1_UNLIMITED:
        return None
    return limit


def _cgroup_memory_used(root: Path) -> int | None:
    """Working-set bytes (usage minus reclaimable inactive file cache)."""
    usage = _read_int(root / "memory.current")
    stat_path = root / "memory.stat"
    inactive_key = "inactive_file"
    if usage is None:
        for base in (root / "memory", root):
            usage = _read_int(base / "memory.usage_in_bytes")
            if usage is not None:
                stat_path = base / "memory.stat"
                inactive_key = "total_inactive_file"
                break
    if usage is None:
        return None
    inactive = 0
    for line in (_read(stat_path) or "").splitlines():
        key, _, value = line.partition(" ")
        if key == inactive_key:
            try:
                inactive = int(value)
            except ValueError:
                pass
            break
    return max(0, usage - inactive)


def virtual_memory(probe=None, root: Path = _CGROUP_ROOT) -> VirtualMemory:
    """``psutil.virtual_memory()`` clamped to the container's memory limit.

    Returns ``total``/``available``/``used`` in bytes and ``percent`` used,
    the fields callers in this codebase read. ``probe`` defaults to
    ``psutil.virtual_memory``; callers holding their own psutil reference pass
    it so their seam stays patchable. Without a cgroup limit the host figures
    pass through unchanged (a field the probe lacks stays None)."""
    if probe is None:
        import psutil

        probe = psutil.virtual_memory
    vm = probe()
    total = getattr(vm, "total", None)
    available = getattr(vm, "available", None)
    used = getattr(vm, "used", None)
    percent = getattr(vm, "percent", None)
    limit = cgroup_memory_limit(root)
    if limit is not None and (total is None or limit < total):
        working_set = _cgroup_memory_used(root)
        if working_set is None and total is not None and available is not None:
            working_set = int(total - available)
        cap = limit - working_set if working_set is not None else limit
        total = limit
        available = max(0, min(available, cap) if available is not None else cap)
        used = max(0, total - available)
        percent = round(100.0 * used / total, 1)
    return VirtualMemory(total, available, used, percent)


def apply_thread_limits(root: Path = _CGROUP_ROOT) -> int | None:
    """Cap native thread pools to the cgroup CPU quota.

    Only acts when a quota is present (a hosted container) and the operator
    has not chosen thread counts; desktop hosts are left untouched. Must run
    before torch/numpy are imported so OpenMP reads the variables, and child
    engine processes inherit them. Returns the thread count applied, if any."""
    if cgroup_cpu_limit(root) is None:
        return None
    threads = effective_cpu_count(root)
    applied = False
    for name in THREAD_ENV_VARS:
        if not os.environ.get(name):
            os.environ[name] = str(threads)
            applied = True
    return threads if applied else None
