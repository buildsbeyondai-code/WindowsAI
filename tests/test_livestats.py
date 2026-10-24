"""livestats: the in-process Win32 headline reads, against a fake ``windll`` that
fills the real ctypes structures, so the struct layout and CPU-delta maths run."""
import ctypes
from types import SimpleNamespace

import pytest

import sysperf.livestats as ls


class Kernel32:
    def __init__(self):
        self.times = [(100, 300, 200), (150, 400, 300)]       # idle, kernel, user
        self.ok = True
        self.GetTickCount64 = lambda: 7_200_000              # 2 h

    def GetSystemTimes(self, idle, kern, user):
        if not self.ok:
            return 0
        i, k, u = self.times.pop(0) if len(self.times) > 1 else self.times[0]
        for ref, v in ((idle, i), (kern, k), (user, u)):
            ref._obj.dwLowDateTime, ref._obj.dwHighDateTime = v, 0
        return 1


class Psapi:
    def __init__(self, ok=True):
        self.ok = ok

    def GetPerformanceInfo(self, ref, cb):
        if not self.ok:
            return 0
        pi = ref._obj
        assert cb == ctypes.sizeof(pi)
        pi.PageSize, pi.PhysicalAvailable, pi.PhysicalTotal = 4096, 262144, 1048576
        pi.CommitTotal, pi.CommitLimit = 524288, 2097152
        pi.ProcessCount, pi.HandleCount = 250, 90000
        return 1


@pytest.fixture
def win(monkeypatch):
    k32, ps = Kernel32(), Psapi()
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=k32, psapi=ps),
                        raising=False)
    monkeypatch.setattr(ls, "PLATFORM", "windows")
    monkeypatch.setattr(ls, "_prev", None)
    monkeypatch.setattr(ls, "_last_cpu", None)
    return k32, ps


def test_off_windows_returns_nothing(monkeypatch):
    monkeypatch.setattr(ls, "PLATFORM", "linux")
    assert ls.read() == {}


def test_reads_memory_and_cpu_delta(win):
    first = ls.read()
    assert first["cpu_pct"] is None                         # needs two readings
    assert first["available_mb"] == 1024 and first["total_ram_mb"] == 4096
    assert first["committed_mb"] == 2048 and first["commit_limit_mb"] == 8192
    assert first["processes"] == 250 and first["uptime_h"] == 2.0
    # delta: idle 50, kernel 100 (incl. idle), user 100 -> busy 150 of 200
    assert ls.read()["cpu_pct"] == 75.0


def test_failed_system_times_keeps_last_value_and_zero_delta(win):
    k32, _ = win
    ls.read()
    ls.read()
    k32.ok = False
    assert ls.read()["cpu_pct"] == 75.0
    k32.ok, k32.times = True, [(150, 400, 300)]             # no time passed
    assert ls.read()["cpu_pct"] == 75.0


def test_api_failures_return_nothing(win, monkeypatch):
    _, ps = win
    ps.ok = False
    assert ls.read() == {}
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(), raising=False)
    assert ls.read() == {}                                   # AttributeError path
