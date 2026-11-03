"""Container CPU quota / memory limit awareness (hosted CPU deployments).

On Railway-style containers the host has many cores and lots of RAM while the
cgroup allows a few vCPUs and a few GB. Reading host figures oversubscribed
torch threads (timeouts) and let model loads run into OOM kills.
"""
from __future__ import annotations

import re
from collections import namedtuple
from pathlib import Path

import pytest

from core import container_limits as cl

_ROOT = Path(__file__).resolve().parents[1]
_GIB = 1024 ** 3
_HostVM = namedtuple("_HostVM", "total available")


@pytest.fixture
def host(monkeypatch):
    """A big host: 64 cores, 256 GiB with 200 GiB available."""
    monkeypatch.setattr(cl.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(cl.os, "sched_getaffinity", lambda _pid: set(range(64)), raising=False)
    import psutil

    monkeypatch.setattr(psutil, "virtual_memory", lambda: _HostVM(256 * _GIB, 200 * _GIB))


def _v2(tmp_path: Path, cpu: str | None, mem: str | None, current: int = 0, inactive: int = 0) -> Path:
    if cpu is not None:
        (tmp_path / "cpu.max").write_text(cpu + "\n")
    if mem is not None:
        (tmp_path / "memory.max").write_text(mem + "\n")
        (tmp_path / "memory.current").write_text(f"{current}\n")
        (tmp_path / "memory.stat").write_text(f"anon 1\ninactive_file {inactive}\nactive_file 2\n")
    return tmp_path


def test_v2_quota_caps_cpu_count(tmp_path, host):
    root = _v2(tmp_path, "200000 100000", None)
    assert cl.cgroup_cpu_limit(root) == 2.0
    assert cl.effective_cpu_count(root) == 2


def test_fractional_quota_rounds_up(tmp_path, host):
    assert cl.effective_cpu_count(_v2(tmp_path, "50000 100000", None)) == 1
    assert cl.effective_cpu_count(_v2(tmp_path, "250000 100000", None)) == 3


def test_unlimited_or_missing_cgroup_keeps_host_figures(tmp_path, host):
    assert cl.effective_cpu_count(_v2(tmp_path, "max 100000", "max")) == 64
    assert cl.effective_cpu_count(tmp_path / "absent") == 64
    vm = cl.virtual_memory(root=tmp_path / "absent")
    assert (vm.total, vm.available) == (256 * _GIB, 200 * _GIB)


def test_probe_fields_pass_through_without_a_limit(tmp_path):
    vm = cl.virtual_memory(lambda: _HostVM(32 * _GIB, 20 * _GIB), root=tmp_path / "absent")
    assert (vm.total, vm.available) == (32 * _GIB, 20 * _GIB)


def test_v1_quota(tmp_path, host):
    (tmp_path / "cpu").mkdir()
    (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text("400000\n")
    (tmp_path / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
    assert cl.effective_cpu_count(tmp_path) == 4


def test_memory_clamped_to_limit_minus_working_set(tmp_path, host):
    # 8 GiB limit, 5 GiB charged of which 1 GiB is reclaimable page cache.
    root = _v2(tmp_path, None, str(8 * _GIB), current=5 * _GIB, inactive=1 * _GIB)
    vm = cl.virtual_memory(root=root)
    assert vm.total == 8 * _GIB
    assert vm.available == 4 * _GIB
    assert vm.used == 4 * _GIB
    assert vm.percent == 50.0


def test_v1_unlimited_memory_sentinel_is_ignored(tmp_path, host):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "memory.limit_in_bytes").write_text("9223372036854771712\n")
    assert cl.cgroup_memory_limit(tmp_path) is None
    assert cl.virtual_memory(root=tmp_path).total == 256 * _GIB


def test_thread_limits_only_with_quota_and_never_override(tmp_path, host, monkeypatch):
    for name in cl.THREAD_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert cl.apply_thread_limits(tmp_path / "absent") is None
    assert all(name not in cl.os.environ for name in cl.THREAD_ENV_VARS)

    monkeypatch.setenv("OMP_NUM_THREADS", "6")
    assert cl.apply_thread_limits(_v2(tmp_path, "300000 100000", None)) == 3
    assert cl.os.environ["OMP_NUM_THREADS"] == "6"  # operator's choice wins
    assert cl.os.environ["MKL_NUM_THREADS"] == "3"


# Recurrence guard: host-wide figures must not drive load/thread decisions.
_HOST_PROBE = re.compile(r"psutil\.virtual_memory\(\)|os\.cpu_count\(\)")
_ALLOWED = {
    "backend/core/container_limits.py",  # the one place that reads host figures
    "omnivoice/models/omnivoice.py",  # opt-in diagnostic log line only
    "omnivoice/utils/audio.py",  # standalone-package fallback when core is absent
}


def test_no_direct_host_cpu_or_memory_probes():
    offenders = []
    for base in ("backend", "omnivoice"):
        for path in (_ROOT / base).rglob("*.py"):
            rel = path.relative_to(_ROOT).as_posix()
            if "/tests/" in rel or rel in _ALLOWED:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if _HOST_PROBE.search(line):
                    offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "Use core.container_limits.effective_cpu_count()/virtual_memory() so "
        "container CPU quotas and memory limits are respected: " + ", ".join(offenders)
    )
