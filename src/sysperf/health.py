"""Service self-diagnostics: recent errors, worker liveness, own footprint.

On 2026-10-24 the auto-improve hook raised a TypeError every five minutes for
an hour and nothing on the dashboard said so; the only trace was a log file
nobody reads. This module makes the service's own health visible:

* :class:`ErrorBuffer` — a logging handler that keeps the last WARNING+
  records in memory, collapsing repeats, so the page can show them;
* :class:`HealthMonitor` — registered worker threads plus freshness probes
  ("the collector's last good sample is 14 min old"), rolled into one verdict;
* :func:`footprint` — CPU seconds and resident memory of this process, so a
  performance tool can show that it is not itself the problem;
* :func:`rotate_log` — keeps the runtime log bounded.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class ErrorBuffer(logging.Handler):
    """Keeps the most recent WARNING+ records; identical messages are counted."""

    def __init__(self, capacity: int = 100, clock: Callable[[], float] = time.time):
        super().__init__(level=logging.WARNING)
        self.capacity = capacity
        self.clock = clock
        self._items: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage().splitlines()[0][:400] if record.getMessage() else ""
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                msg = f"{msg} — {type(exc).__name__}: {exc}"[:500]
        except Exception:
            msg = str(record.msg)[:400]
        now = self.clock()
        with self._lock:
            for item in self._items:
                if item["message"] == msg and item["logger"] == record.name:
                    item["count"] += 1
                    item["last"] = now
                    item["level"] = record.levelname
                    return
            self._items.append({"first": now, "last": now, "count": 1,
                                "level": record.levelname, "logger": record.name,
                                "message": msg})

    def items(self, since: float | None = None) -> list:
        with self._lock:
            rows = [dict(i) for i in self._items]
        if since is not None:
            rows = [r for r in rows if r["last"] >= since]
        return sorted(rows, key=lambda r: -r["last"])

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def rss_mb(statm: Path = Path("/proc/self/statm")) -> float | None:
    """Resident memory of this process in MB, or None if it cannot be read."""
    if os.name == "nt":                                      # pragma: no cover - Windows
        return _win_rss_mb()
    try:
        pages = int(statm.read_text().split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return round(pages * os.sysconf("SC_PAGE_SIZE") / 1048576, 1)


def _win_rss_mb() -> float | None:                       # pragma: no cover - Windows
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    proc = ctypes.windll.kernel32.GetCurrentProcess()
    if not ctypes.windll.psapi.GetProcessMemoryInfo(proc, ctypes.byref(pmc), pmc.cb):
        return None
    return round(pmc.WorkingSetSize / 1048576, 1)


def footprint(started_at: float, clock: Callable[[], float] = time.time,
              times: Callable = os.times, rss: Callable = rss_mb) -> dict:
    """CPU and memory this process has used, and its average CPU share."""
    t = times()
    cpu = t.user + t.system
    up = max(1e-6, clock() - started_at)
    return {"cpu_s": round(cpu, 1), "uptime_s": round(up),
            "avg_cpu_pct": round(100.0 * cpu / up / (os.cpu_count() or 1), 2),
            "rss_mb": rss(), "threads": threading.active_count()}


def rotate_log(path: Path, max_bytes: int = 5_000_000, keep_bytes: int = 200_000,
               stream=None) -> bool:
    """Move an oversized log to ``<name>.1`` and keep only its recent tail.

    The launcher holds the log open as stdout, so the file cannot be renamed
    under it on Windows. Instead the old content is copied aside and the live
    file is truncated in place through the open ``stream`` (or rewritten when
    no stream is given), keeping the last ``keep_bytes`` so context survives.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= max_bytes:
        return False
    data = path.read_bytes()
    path.with_name(path.name + ".1").write_bytes(data)
    tail = data[-keep_bytes:]
    cut = tail.find(b"\n")
    tail = tail[cut + 1:] if cut >= 0 else tail
    note = b"--- log rotated; previous content in " + path.name.encode() + b".1 ---\n"
    if stream is not None:
        stream.flush()
        stream.seek(0)
        stream.truncate()
        stream.write(note.decode() + tail.decode("utf-8", "replace"))
        stream.flush()
    else:
        path.write_bytes(note + tail)
    return True


class HealthMonitor:
    """Liveness of the service's workers and freshness of what they produce."""

    def __init__(self, errors: ErrorBuffer, clock: Callable[[], float] = time.time,
                 started_at: float | None = None):
        self.errors = errors
        self.clock = clock
        self.started_at = started_at if started_at is not None else clock()
        self._threads: dict = {}
        self._probes: dict = {}

    def register_thread(self, name: str, thread: threading.Thread) -> threading.Thread:
        self._threads[name] = thread
        return thread

    def add_probe(self, name: str, last_at: Callable[[], float | None],
                  stale_after_s: float, label: str) -> None:
        """``last_at`` returns when this worker last produced something."""
        self._probes[name] = (last_at, stale_after_s, label)

    def check(self) -> dict:
        now = self.clock()
        threads = [{"name": n, "alive": t.is_alive()} for n, t in self._threads.items()]
        probes, problems = [], []
        for name, (last_at, stale, label) in self._probes.items():
            try:
                last = last_at()
            except Exception:
                last = None
            age = None if last is None else round(now - last)
            # Nothing yet is only a problem once the service has had time to start.
            stale_now = (age is not None and age > stale) or \
                (age is None and now - self.started_at > stale)
            probes.append({"name": name, "label": label, "last_at": last,
                           "age_s": age, "stale_after_s": stale, "stale": stale_now})
            if stale_now:
                problems.append(f"{label}: no update for "
                                + (f"{age // 60} min" if age is not None else "since start"))
        for t in threads:
            if not t["alive"]:
                problems.append(f"worker '{t['name']}' has stopped")
        recent = self.errors.items(since=now - 3600)
        errors_1h = sum(r["count"] for r in recent if r["level"] in ("ERROR", "CRITICAL"))
        status = "error" if problems else ("degraded" if errors_1h else "ok")
        return {"status": status, "problems": problems, "threads": threads,
                "probes": probes, "errors_1h": errors_1h,
                "warnings_1h": sum(r["count"] for r in recent if r["level"] == "WARNING")}
