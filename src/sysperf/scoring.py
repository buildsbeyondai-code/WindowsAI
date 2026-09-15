"""
Scoring: progress per criterion, the WSL working-set trend, and the scorecard.

Pure functions. Criteria and tuning come in as arguments, so nothing here is
tied to a particular machine and everything is straightforward to unit-test.

Two rules make the score trustworthy rather than merely reactive:

* **Smoothing.** Counts such as processes and handles breathe constantly.
  Scoring the single latest sample makes the score collapse on every upward
  breath, so the *sustained* state (a rolling median) is scored instead. Live
  gauges and charts still show the raw latest value.
* **Honest abstention.** A criterion that cannot be evaluated yet reports
  ``measuring`` and is excluded from the overall figure rather than being
  assumed good.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .config import CARRY_FORWARD_KEYS

Criterion = tuple[str, str, float, float, bool, str]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def median(values: Iterable[float]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return float(vals[mid]) if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def smoothed_current(history: list[dict], key: str, latest_val: Any,
                     window: int) -> Any:
    """Median of the last ``window`` non-null samples for ``key``."""
    if not window or window <= 1:
        return latest_val
    med = median([s.get(key) for s in history[-window:]])
    return latest_val if med is None else med


def progress_pct(baseline: float, current: float, target: float) -> float:
    """Fraction of the baseline -> target journey completed, clamped to 0..100."""
    if baseline == target:
        return 100.0
    return max(0.0, min(100.0, (current - baseline) / (target - baseline) * 100.0))


# --------------------------------------------------------------------------- #
# WSL working-set trend
# --------------------------------------------------------------------------- #

def vmmem_trend(history: list[dict], trend_cfg: dict) -> tuple[float, float, int]:
    """
    Least-squares slope of the WSL working set, in MB/hour.

    Guards, all of which exist because the naive slope is misleading:

    * only the most recent ``window_seconds`` of samples are used;
    * a minimum elapsed span is required — Windows trims working sets
      opportunistically, and a multi-GB trim over four minutes extrapolates to a
      meaningless five-figure rate;
    * the window is re-anchored at a genuine VM restart or working-set reset (a
      collector gap, or a collapse from a high steady state) so the recovery
      ramp afterwards does not read as a leak.

    Returns ``(slope_mb_per_h, span_seconds, n_points)``.
    """
    min_samples = int(trend_cfg.get("min_samples", 6))
    min_span = float(trend_cfg.get("min_span_seconds", 600))
    window = float(trend_cfg.get("window_seconds", 3600))
    gap_floor = float(trend_cfg.get("gap_seconds", 120))

    pts = [(s["_t"], s["vmmem_mb"]) for s in history
           if s.get("vmmem_mb") is not None and s.get("_t") is not None]
    if not pts:
        return 0.0, 0.0, 0
    newest = pts[-1][0]
    pts = [(t, v) for t, v in pts if newest - t <= window]

    # What counts as "the collector was down" depends on the sample cadence, so
    # it is measured from the data rather than fixed. A configured 60 s cadence
    # against a hardcoded 120 s threshold would re-anchor on every sample and the
    # trend would never be computed at all.
    deltas = [pts[i][0] - pts[i - 1][0] for i in range(1, len(pts))]
    typical = median(deltas) or 0.0
    gap = max(gap_floor, typical * 3)

    anchor = 0
    for i in range(1, len(pts)):
        reset = pts[i][1] < pts[i - 1][1] * 0.5
        if (pts[i][0] - pts[i - 1][0]) > gap or reset:
            anchor = i
    pts = pts[anchor:]

    n = len(pts)
    if n < 2:
        return 0.0, 0.0, n
    span = pts[-1][0] - pts[0][0]
    if n < min_samples or span < min_span:
        return 0.0, span, n
    t0 = pts[0][0]
    xs = [(t - t0) / 3600.0 for t, _ in pts]
    ys = [float(v) for _, v in pts]
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0, span, n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return slope, span, n


def score_ceiling(current: float, cap: float, trend: float, span: float, n: int,
                  trend_cfg: dict) -> tuple[float | None, str, str]:
    """
    Score a *ceiling* criterion: "at or below the cap **and not growing**".

    The generic baseline -> target formula does not apply. A cap normally sits
    *above* the starting value, so improving would score negative. What matters
    is staying under the cap without trending toward it.
    """
    min_samples = int(trend_cfg.get("min_samples", 6))
    min_span = float(trend_cfg.get("min_span_seconds", 600))

    if current > cap:
        return 0.0, "regress", f"above the {cap:,.0f} MB cap"
    if n < min_samples or span < min_span:
        need = max(0, int((min_span - span) / 60) + 1)
        return None, "measuring", (
            f"under cap; slope needs ~{need} more min of samples "
            f"(have {n} over {span / 60:.0f} min)")
    mins = f"over the last {span / 60:.0f} min"
    util = (current / cap) if cap else 0.0
    if trend <= 0:
        word = "falling" if trend < 0 else "flat"
        return 100.0, "met", f"under cap and {word} ({trend:+,.0f} MB/h {mins})"

    # Under the cap but rising: judge by remaining runway, not by the mere
    # existence of a positive slope — working sets breathe, and a slow rise from
    # a low base is normal churn rather than a leak.
    room = cap - current
    hrs = (room / trend) if trend > 0 else 1e9
    if hrs >= 12 and util < 0.5:
        return 100.0, "met", (
            f"under cap; rising only {trend:+,.0f} MB/h {mins} but ~{hrs:.0f} h of "
            f"headroom at {util * 100:.0f}% of cap — not a leak")
    if hrs >= 3:
        return max(50.0, min(95.0, hrs / 12 * 100)), "partial", (
            f"under cap, rising {trend:+,.0f} MB/h {mins}; ~{hrs:.0f} h to cap")
    return max(0.0, hrs / 3 * 40), "regress", (
        f"under cap but rising {trend:+,.0f} MB/h {mins} — ~{hrs:.1f} h to cap")


# --------------------------------------------------------------------------- #
# scorecard
# --------------------------------------------------------------------------- #

def _row_note(key: str, cur: Any, latest: dict, target: float) -> str | None:
    if key == "startup_apps" and latest.get("startup_apps_total") is not None:
        return (f"{cur} enabled of {latest['startup_apps_total']} registered "
                "auto-start entries (disabled entries excluded)")
    if key == "net_remote" and latest.get("net_established") is not None:
        return (f"{cur} remote of {latest['net_established']} established "
                f"({latest.get('net_loopback', 0)} loopback IPC excluded); "
                f"target {target:.0f} accepts the connections the OS, sync and "
                "security agents always hold open")
    if key == "hw_faults":
        pd = latest.get("hw_problem_devices") or 0
        ud = latest.get("hw_disks_unhealthy") or 0
        return ("all devices report OK; no unhealthy disks" if not cur
                else f"{pd} device(s) with error codes, {ud} unhealthy disk(s)")
    if key == "compression_mb":
        return ("memory the OS compressed because RAM was oversubscribed — "
                "lower is better, zero is ideal")
    return None


def build_scorecard(latest: dict, history: list[dict], criteria: list[Criterion],
                    cfg: dict, scorer=None, ceiling_keys=("vmmem_mb",)) -> dict:
    """Assemble the scorecard payload for the API."""
    rows: list[dict] = []
    progs: list[float] = []
    window = int(cfg.get("score_smooth_window", 15))
    overrides = cfg.get("smooth_window_overrides") or {}
    band = float(cfg.get("regress_band_pct", 2.0))
    trend_cfg = cfg.get("trend") or {}

    if latest.get("hw_problem_devices") is not None:
        latest["hw_faults"] = ((latest.get("hw_problem_devices") or 0)
                               + (latest.get("hw_disks_unhealthy") or 0))
    trend, span, npts = vmmem_trend(history, trend_cfg)

    for key, label, base, target, hib, unit in criteria:
        cur = latest.get(key)
        if cur is None:
            continue

        if key in ceiling_keys:
            change = (cur - base) / base * 100.0 if base else 0.0
            prog, status, note = score_ceiling(cur, target, trend, span, npts, trend_cfg)
            met = status == "met"
            scored = cur
        else:
            # Precedence: explicit per-metric window > carry-forward metrics use
            # the raw latest value (a short median would only return a stale
            # earlier step) > everything else uses the default short window.
            ovr = overrides.get(key)
            if ovr is not None:
                scored = smoothed_current(history, key, cur, int(ovr))
            elif key in CARRY_FORWARD_KEYS:
                scored = cur
            else:
                scored = smoothed_current(history, key, cur, window)
            change = (scored - base) / base * 100.0 if base else 0.0
            met = (scored >= target) if hib else (scored <= target)
            # Target achieved means the journey is complete. Without this, a
            # metric that was already better than its achievable floor at
            # baseline time reports "met" alongside 0% progress, because the
            # baseline -> target span points the wrong way.
            prog = 100.0 if met else (0.0 if base == target else progress_pct(base, scored, target))
            worsened = ((scored < base) if hib else (scored > base)) if base == 0 else ((change < -band) if hib else (change > band))
            status = "met" if met else ("regress" if worsened else "partial")
            note = _row_note(key, cur, latest, target)

        rows.append({
            "key": key, "label": label, "baseline": base, "current": cur,
            "scored_typical": round(float(scored), 1),
            "target": target, "unit": unit, "higher_is_better": hib,
            "change_pct": round(change, 1),
            "progress": None if prog is None else round(prog, 1),
            "room": None if prog is None else round(100 - prog, 1),
            "status": status, "note": note,
            "shortfall": 0 if met else round(abs(float(scored) - target), 1),
        })
        if prog is not None:
            progs.append(prog)

    overall = sum(progs) / len(progs) if progs else 0.0

    if scorer is not None:
        for row in rows:
            extra = scorer.enrich(row["key"], row["current"], row["baseline"],
                                  row["target"], row["higher_is_better"])
            if extra is not None:
                row["adaptive"] = extra

    return {
        "rows": rows,
        "overall_improvement": round(overall, 1),
        "overall_room": round(100 - overall, 1),
        "adaptive": scorer.summary(rows) if scorer is not None else None,
        "scored": len(progs),
        "total_criteria": len(rows),
        "vmmem_trend": round(trend, 1),
        "vmmem_trend_span_min": round(span / 60, 1),
        "vmmem_trend_points": npts,
        "counts": {
            "met": sum(1 for r in rows if r["status"] == "met"),
            "partial": sum(1 for r in rows if r["status"] == "partial"),
            "regress": sum(1 for r in rows if r["status"] == "regress"),
            "measuring": sum(1 for r in rows if r["status"] == "measuring"),
        },
    }
