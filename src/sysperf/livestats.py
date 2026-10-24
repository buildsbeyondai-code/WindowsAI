"""Real-time headline metrics read in-process from Win32 APIs.

The PowerShell probe runs every SAMPLE_SECONDS (60 s) because spawning it more
often made the dashboard a top CPU consumer. That left the "live" headline
values up to ~80 s old (60 s sample + 20 s page poll). These few values are
read here directly via ctypes — microseconds, no process spawn — so the page
can show them in real time. Semantics match collect_win.ps1:
  cpu_pct       share of all cores busy since the previous call (GetSystemTimes)
  available_mb  free physical memory (Win32_OperatingSystem.FreePhysicalMemory)
  committed_mb  commit charge ('Committed Bytes' counter == CommitTotal)
READ-ONLY. Used for display only; scoring and the trend series still come from
the probe so their sample-count windows are unchanged.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

from .config import PLATFORM

_lock = threading.Lock()
_prev: tuple[int, int, int] | None = None   # (idle, kernel, user) 100-ns ticks
_last_cpu: float | None = None


class _PerfInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD),
                ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t),
                ("PhysicalAvailable", ctypes.c_size_t), ("SystemCache", ctypes.c_size_t),
                ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t),
                ("HandleCount", wintypes.DWORD), ("ProcessCount", wintypes.DWORD),
                ("ThreadCount", wintypes.DWORD)]


def _ft(f: wintypes.FILETIME) -> int:
    return (f.dwHighDateTime << 32) | f.dwLowDateTime


def _cpu_pct() -> float | None:
    global _prev, _last_cpu
    idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                                 ctypes.byref(user)):
        return _last_cpu
    cur = (_ft(idle), _ft(kern), _ft(user))
    if _prev is not None:
        d_idle, d_kern, d_user = (c - p for c, p in zip(cur, _prev))
        total = d_kern + d_user              # kernel time includes idle time
        if total > 0:
            _last_cpu = round(max(0.0, min(100.0, 100.0 * (total - d_idle) / total)), 1)
    _prev = cur
    return _last_cpu


def read() -> dict:
    """Current headline values, or {} off Windows / on API failure."""
    if PLATFORM != "windows":
        return {}
    with _lock:
        try:
            pi = _PerfInfo()
            pi.cb = ctypes.sizeof(pi)
            if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
                return {}
            mb = pi.PageSize / (1024 * 1024)
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return {
                "t": time.time(),
                "cpu_pct": _cpu_pct(),
                "available_mb": round(pi.PhysicalAvailable * mb),
                "total_ram_mb": round(pi.PhysicalTotal * mb),
                "committed_mb": round(pi.CommitTotal * mb),
                "commit_limit_mb": round(pi.CommitLimit * mb),
                "processes": int(pi.ProcessCount),
                "handles_total": int(pi.HandleCount),
                "uptime_h": round(ctypes.windll.kernel32.GetTickCount64() / 3.6e6, 2),
            }
        except (OSError, AttributeError):
            return {}
