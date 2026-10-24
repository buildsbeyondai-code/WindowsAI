"""
Consistent CPU-consumer attribution from the rolling sample history.

Each sample, the Windows collector emits ``cpu_procs`` — the top processes by
*cumulative* processor-seconds (``cpu_s``), plus pid, name and working set — and
``ncpu`` (logical processor count). A single sample cannot say how much CPU a
process is *using*; only the change in cumulative processor-seconds between two
samples does. This module differences consecutive samples (matched by pid) into a
per-interval CPU% for each process, then aggregates by process name across the
window to surface the applications that use CPU *consistently* — present and
non-trivial across many intervals — as opposed to one-off spikes.

Read-only: pure functions over history dicts, no I/O and no host access.

CPU% is expressed as a share of the *whole machine* (all logical processors), so
a process pinning one core of an 8-core box reads ~12.5%, matching how the
``\\Processor(_Total)\\% Processor Time`` counter behind ``cpu_pct`` is scaled.
"""
from __future__ import annotations

# Tunables (kept local so the module is self-contained and dependency-free).
CPU_MIN_PCT = 3.0          # a process must use >= this % of total CPU in an
#                            interval to count as "active" that interval
CPU_FLOOR_PCT = 1.0        # drop consumers whose median CPU% is below this
CPU_TOP = 8                # most consumers to return
CPU_MAX_INTERVALS = 30     # newest N intervals considered (~30 min at 60s)
CPU_CONSISTENT_FRAC = 0.6  # active in >= this fraction of intervals == consistent
CPU_MIN_ACTIVE = 2         # ...but never fewer than this many intervals


def _median(vals: list) -> float:
    vals = sorted(vals)
    n = len(vals)
    mid = n // 2
    return float(vals[mid]) if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _interval_cpu(prev: dict, cur_procs: list, dt: float, ncpu: int) -> dict:
    """CPU% per process *name* for one interval (delta cpu_s / dt / cores)."""
    per_name: dict = {}
    for p in cur_procs:
        if not isinstance(p, dict):
            continue
        pid = p.get("pid")
        was = prev.get(pid)
        if was is None:
            continue                      # process not present last sample
        delta = float(p.get("cpu_s") or 0.0) - float(was.get("cpu_s") or 0.0)
        if delta <= 0:
            continue                      # idle, pid reused, or counter reset
        pct = delta / dt / max(ncpu, 1) * 100.0
        name = p.get("n") or "?"
        acc = per_name.setdefault(name, {"pct": 0.0, "ws_mb": 0})
        acc["pct"] += pct                 # sum instances sharing a name
        acc["ws_mb"] = max(acc["ws_mb"], int(p.get("ws_mb") or 0))
    return per_name


def recent_interval_cpu(history, name, count: int = 3, *,
                        max_intervals: int = CPU_MAX_INTERVALS) -> list:
    """Per-interval CPU% for a single process *name*, newest last.

    Differences consecutive samples exactly as :func:`consistent_cpu` does, but
    returns the raw per-interval CPU% series for one process (summed across
    instances that share the name). Returns the most recent ``count`` values
    (fewer if the window is short). Intervals where the process was absent
    contribute ``0.0`` so a gap reads as "not busy", not "missing".

    Pure function over history dicts — no I/O, no host access. Used by the
    runaway-process watchdog (procwatch.py) to decide, on live data, whether a
    process has pinned CPU across several consecutive intervals rather than for
    a single one-off spike.
    """
    samples = [s for s in history
               if isinstance(s, dict)
               and isinstance(s.get("cpu_procs"), list)
               and s.get("_t") is not None]
    if len(samples) < 2:
        return []
    samples = samples[-(max_intervals + 1):]
    series = []
    for a, b in zip(samples, samples[1:]):
        dt = float(b["_t"]) - float(a["_t"])
        if dt <= 0:
            continue
        ncpu = int(b.get("ncpu") or a.get("ncpu") or 1)
        prev = {p.get("pid"): p for p in a["cpu_procs"]
                if isinstance(p, dict) and p.get("pid") is not None}
        if not prev:
            continue
        per_name = _interval_cpu(prev, b["cpu_procs"], dt, ncpu)
        series.append(round(per_name.get(name, {}).get("pct", 0.0), 2))
    return series[-count:] if count else series


def consistent_cpu(history, *, min_pct: float = CPU_MIN_PCT,
                   top: int = CPU_TOP,
                   max_intervals: int = CPU_MAX_INTERVALS) -> dict:
    """Rank the applications using CPU consistently over the recent window.

    Returns a dict the Auto-Improve tab renders directly::

        {"available": bool, "intervals": int, "window_min": float,
         "ncpu": int | None, "min_pct": float,
         "consumers": [{"name", "median_cpu_pct", "avg_cpu_pct",
                        "peak_cpu_pct", "consistency_pct", "active_intervals",
                        "intervals", "ws_mb", "consistent"}]}

    ``available`` is False (and ``consumers`` empty) when the history predates
    per-process CPU collection or holds fewer than two usable samples.
    """
    samples = [s for s in history
               if isinstance(s, dict)
               and isinstance(s.get("cpu_procs"), list)
               and s.get("_t") is not None]
    if len(samples) < 2:
        return {"available": False, "intervals": 0, "window_min": 0.0,
                "ncpu": None, "min_pct": min_pct, "consumers": []}

    samples = samples[-(max_intervals + 1):]
    stats: dict = {}          # name -> {"pcts": [...], "active": int, "ws_mb": int}
    last_per_name: dict = {}  # per-name CPU% of the most recent counted interval
    intervals = 0
    span_s = 0.0
    ncpu_seen = None
    for a, b in zip(samples, samples[1:]):
        dt = float(b["_t"]) - float(a["_t"])
        if dt <= 0:
            continue
        ncpu = int(b.get("ncpu") or a.get("ncpu") or 1)
        ncpu_seen = ncpu
        prev = {p.get("pid"): p for p in a["cpu_procs"]
                if isinstance(p, dict) and p.get("pid") is not None}
        per_name = _interval_cpu(prev, b["cpu_procs"], dt, ncpu)
        if not prev:
            continue                      # nothing to difference against
        intervals += 1
        span_s += dt
        last_per_name = per_name          # newest interval wins == "current"
        for name, acc in per_name.items():
            e = stats.setdefault(name, {"pcts": [], "active": 0, "ws_mb": 0})
            e["pcts"].append(round(acc["pct"], 2))
            e["ws_mb"] = max(e["ws_mb"], acc["ws_mb"])
            if acc["pct"] >= min_pct:
                e["active"] += 1

    if not intervals:
        return {"available": False, "intervals": 0, "window_min": 0.0,
                "ncpu": ncpu_seen, "min_pct": min_pct, "consumers": []}

    need = max(CPU_MIN_ACTIVE, round(intervals * CPU_CONSISTENT_FRAC))
    consumers = []
    for name, e in stats.items():
        pcts = e["pcts"]
        median = round(_median(pcts), 2)
        if e["active"] < CPU_MIN_ACTIVE and median < CPU_FLOOR_PCT:
            continue                      # neither recurrent nor heavy — noise
        consumers.append({
            "name": name,
            "current_cpu_pct": round(last_per_name.get(name, {}).get("pct", 0.0), 2),
            "median_cpu_pct": median,
            "avg_cpu_pct": round(sum(pcts) / len(pcts), 2),
            "peak_cpu_pct": round(max(pcts), 2),
            "consistency_pct": round(e["active"] / intervals * 100.0, 1),
            "active_intervals": e["active"],
            "intervals": intervals,
            "ws_mb": e["ws_mb"],
            "consistent": e["active"] >= need,
        })

    # Consistent-and-heavy first: rank by how reliably it is busy, then by how
    # much CPU it draws when it is.
    consumers.sort(key=lambda c: (c["consistency_pct"], c["median_cpu_pct"]),
                   reverse=True)
    return {"available": True, "intervals": intervals,
            "window_min": round(span_s / 60.0, 1), "ncpu": ncpu_seen,
            "min_pct": min_pct, "consumers": consumers[:top]}
