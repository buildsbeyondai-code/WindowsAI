"""
Root-cause analysis for the System Performance Dashboard.

When the overall score drops below the target threshold, "which number is low"
is not an answer — the same low number can mean four very different things, and
each wants a different response:

  ``transient_spike``     the metric is far above its OWN recent daily typical
                          while the long-term trend is flat/improving -> a burst
                          (a sync, a scan, a build). Cheap reclaim at most; do
                          not chase it.
  ``usage_driven``        the machine is genuinely being worked harder than its
                          long-term norm and the metric sits inside its usual
                          band for that load -> expected cost of the work.
  ``long_term_regression``the daily typical has been drifting the wrong way for
                          days -> a real regression; remediate and escalate.
  ``chronic_unreachable`` the criterion has NEVER been met across CHRONIC_DAYS
                          settled days and is not drifting -> the target (or the
                          machine's floor), not today's behaviour, is the cause.
                          Fixes cannot help; a target correction is proposed
                          instead (the same class of bug already corrected by
                          hand for net_remote 20->30 and handles 150k->95k).

Each finding is weighted by its share of the total scoring gap, so remediation
effort goes where the score is actually being lost. Pure analysis: nothing here
mutates the machine or any state file.
"""
from __future__ import annotations

from .config import CRITERIA

LABELS = {c[0]: c[1] for c in CRITERIA}
UNITS = {c[0]: c[5] for c in CRITERIA}

# criterion -> remediation ids from the fixes.py registry, best first
REMEDIATION = {
    "net_remote": ["flush_dns"],
    # 2.9.1: cpu_pct, cpu_queue and disk_active_pct had no remediation at all, so
    # a shortfall there could only ever be escalated — they drove ~150 of the last
    # 500 cycles' breaches with nothing the loop was allowed to try.
    "cpu_pct": ["background_cpu_hog"],
    "cpu_queue": ["background_cpu_hog"],
    "disk_active_pct": ["background_cpu_hog"],
    "c_free_gb": ["clear_temp", "empty_recycle_bin"],
    "handles": ["restart_explorer"],
    "available_mb": ["clear_standby", "trim_working_sets"],
    "compression_mb": ["clear_standby", "trim_working_sets"],
    "committed_app_mb": ["clear_standby", "trim_working_sets"],
    "vmmem_mb": ["restart_wsl"],
    "startup_apps": ["trim_startup"],
    "wsl_start_s": ["optimize_wsl_boot"],
    "boot_time_s": ["enable_sysmain", "trim_startup"],
    "explorer_launch_s": ["enable_sysmain"],
    "outlook_launch_s": ["trim_office_addins", "enable_sysmain"],
    "word_launch_s": ["trim_office_addins", "enable_sysmain"],
}

# criterion -> (snapshot key, what it tells us) — the non-actionable floor or
# the dominant contributor, so a finding carries evidence, not just a number.
DRIVERS = {
    "handles": [("handles_system", "non-actionable OS/security handle floor"),
                ("handles_total", "total system-wide handles"),
                ("processes", "running processes")],
    "committed_app_mb": [("committed_system_mb", "OS/security/WSL commit floor"),
                         ("committed_mb", "raw commit charge")],
    "browser_procs": [("edge_procs", "Edge processes"),
                      ("wv2_procs", "WebView2 processes"),
                      ("search_procs", "SearchHost processes")],
    "net_remote": [("net_remote_raw", "instantaneous remote conns"),
                   ("net_remote_hosts", "distinct remote hosts"),
                   ("net_established", "established conns")],
    "available_mb": [("compression_mb", "compressed store"),
                     ("committed_mb", "commit charge")],
    "compression_mb": [("available_mb", "available physical")],
    "vmmem_mb": [("wsl", "WSL guest state")],
    "cpu_pct": [("cpu_queue", "run-queue depth"),
                ("processes", "running processes")],
}

SPIKE_RATIO = 1.15          # >15% above own daily typical == a burst
NEAR_MISS_PCT = 5.0         # within 5% of target == effectively there


def _pct_off(value: float, target: float) -> float:
    """How far `value` sits from `target`, as a percentage of the target."""
    if not target:
        return abs(value) * 100.0
    return abs(value - target) / abs(target) * 100.0


def _worse_than(value: float, ref: float, hib: bool) -> bool:
    return value < ref if hib else value > ref


def _evidence(key: str, latest: dict) -> list:
    """Supporting snapshot values that explain a finding."""
    out = []
    for skey, what in DRIVERS.get(key, []):
        val = latest.get(skey)
        if val is None:
            continue
        if isinstance(val, dict):
            val = ", ".join(f"{k}={v}" for k, v in sorted(val.items())
                            if not isinstance(v, (dict, list)))
        out.append({"key": skey, "what": what, "value": val})
    return out


def classify(row: dict, trend: dict, regime: dict) -> tuple:
    """Return (cause, confidence, explanation) for one under-target row."""
    key = row["key"]
    hib = bool(row.get("higher_is_better"))
    scored = row.get("scored_typical")
    if scored is None:
        scored = row["current"]
    target = row["target"]
    day_typ = trend.get("last")
    direction = trend.get("direction")
    load = regime.get("regime")

    if trend.get("chronic"):
        return ("chronic_unreachable", 0.9,
                f"never met on any of the last {trend['settled_days']} settled "
                f"days (best daily typical {trend.get('min') if not hib else trend.get('max')} "
                f"vs target {target}) — the target sits at or beyond this "
                f"machine's floor, so remediation cannot close it")

    if _pct_off(scored, target) <= NEAR_MISS_PCT:
        return ("near_target", 0.8,
                f"within {NEAR_MISS_PCT:.0f}% of target ({scored} vs {target}) — "
                "no action worth its disruption")

    if direction == "degrading":
        eta = trend.get("days_to_target")
        tail = (f"; at {trend['pct_per_day']:+.2f}%/day it has been drifting "
                f"away for {trend['settled_days']} days")
        return ("long_term_regression", 0.85,
                f"daily typical is trending the wrong way{tail}"
                + (f" (projection: {eta} days to target if reversed)" if eta else ""))

    if day_typ is not None and day_typ and _worse_than(
            scored, day_typ * (SPIKE_RATIO if not hib else 1 / SPIKE_RATIO), hib):
        return ("transient_spike", 0.7,
                f"current sustained value {scored} is >15% worse than this "
                f"metric's own recent daily typical ({day_typ}) while the "
                f"long-term trend is {direction} — a burst, not a regression")

    if load == "heavy":
        return ("usage_driven", 0.75,
                f"machine load index {regime.get('load_index')} is above its "
                f"long-term norm and this metric is inside its usual band "
                f"({trend.get('min')}–{trend.get('max')}) — the cost of the "
                "current workload")

    return ("steady_shortfall", 0.6,
            f"stable at {scored} against target {target} under {load} load — "
            "a persistent gap that needs a structural change, not a reclaim")


# causes that automated remediation should NOT chase
NO_ACTION_CAUSES = ("chronic_unreachable", "near_target")


def analyze(snapshot: dict, store, fix_engine=None,
            threshold: float = 80.0) -> dict:
    """Full RCA report for one snapshot.

    `store` is a trends.TrendStore (long-term horizon); `fix_engine` is optional
    and only used to restrict remediations to those currently applicable.
    """
    scorecard = snapshot.get("scorecard") or {}
    latest = snapshot.get("latest") or {}
    rows = scorecard.get("rows") or []
    score = scorecard.get("overall_improvement")
    regime = store.regime(latest)
    score_trend = store.score_trend()
    detected = ({d["id"] for d in fix_engine.detect(snapshot)}
                if fix_engine is not None else None)

    scored_rows = [r for r in rows if r.get("progress") is not None]
    gaps = {r["key"]: max(0.0, 100.0 - r["progress"]) for r in scored_rows}
    total_gap = sum(gaps.values())

    findings = []
    for row in sorted(scored_rows, key=lambda r: -gaps[r["key"]]):
        if row.get("status") == "met":
            continue
        key = row["key"]
        trend = store.analyze(key)
        cause, conf, why = classify(row, trend, regime)
        ids = [i for i in REMEDIATION.get(key, [])
               if detected is None or i in detected]
        findings.append({
            "key": key, "label": row.get("label", LABELS.get(key, key)),
            "unit": UNITS.get(key, ""),
            "status": row.get("status"),
            "current": row.get("current"),
            "scored_typical": row.get("scored_typical"),
            "target": row.get("target"),
            "progress": row.get("progress"),
            "shortfall": row.get("shortfall"),
            "score_gap": round(gaps[key], 1),
            "contribution_pct": (round(gaps[key] / total_gap * 100.0, 1)
                                 if total_gap else 0.0),
            "cause": cause, "confidence": conf, "explanation": why,
            "long_term": trend,
            "evidence": _evidence(key, latest),
            "remediations": ids,
            "actionable": bool(ids) and cause not in NO_ACTION_CAUSES,
        })

    # Preventive: criteria that are MET today but drifting the wrong way, so the
    # loop can act before the score actually breaches. Remediations here are NOT
    # filtered by fix_engine.detect(): a detector answers "is this broken right
    # now?", which is false by definition while the criterion is still met — the
    # trigger is the multi-day drift, not the current state.
    preventive = []
    for row in scored_rows:
        if row.get("status") != "met":
            continue
        trend = store.analyze(row["key"])
        if trend["direction"] != "degrading":
            continue
        eta = trend.get("days_to_target")
        preventive.append({
            "key": row["key"], "label": row.get("label"),
            "direction": trend["direction"],
            "pct_per_day": trend["pct_per_day"],
            "days_to_target": eta,
            "reason": (f"met today but drifting {trend['pct_per_day']:+.2f}%/day "
                       f"over {trend['settled_days']} days"),
            "remediations": list(REMEDIATION.get(row["key"], [])),
        })

    proposals = [{
        "key": f["key"], "kind": "retune_target",
        "detail": (f"{f['label']}: target {f['target']} never met across "
                   f"{f['long_term']['settled_days']} settled days "
                   f"(best daily typical {f['long_term']['min']}–"
                   f"{f['long_term']['max']}). Review the target against the "
                   "machine's measured floor in config.CRITERIA."),
        "evidence": f["long_term"],
    } for f in findings if f["cause"] == "chronic_unreachable"]

    breach = score is not None and score < threshold
    actionable = [f for f in findings if f["actionable"]]
    return {
        "score": score, "threshold": threshold, "breach": breach,
        "score_trend": score_trend, "regime": regime,
        "counts": scorecard.get("counts"),
        "findings": findings,
        "actionable_count": len(actionable),
        "preventive": preventive,
        "proposals": proposals,
        "summary": _summary(score, threshold, breach, findings, score_trend,
                            regime, preventive),
    }


def _summary(score, threshold, breach, findings, score_trend, regime,
             preventive) -> str:
    """One-paragraph human RCA verdict."""
    if score is None:
        return "No score yet — the collector has not produced a scored sample."
    head = (f"Score {score}% is below the {threshold:.0f}% threshold"
            if breach else f"Score {score}% meets the {threshold:.0f}% threshold")
    trend_txt = ""
    if score_trend.get("direction") in ("improving", "degrading"):
        trend_txt = (f"; long-term score trend is {score_trend['direction']} "
                     f"({score_trend['slope_per_day']:+.2f} pts/day over "
                     f"{score_trend['days']} days)")
    if not findings:
        tail = " — every scored criterion is met."
        if preventive:
            tail = (f" — but {len(preventive)} met criteria are drifting the "
                    "wrong way long-term.")
        return head + trend_txt + tail
    top = findings[:3]
    causes = ", ".join(f"{f['label']} ({f['contribution_pct']}% of the gap, "
                       f"{f['cause']})" for f in top)
    return (f"{head}{trend_txt}. Under {regime.get('regime')} load, the gap is "
            f"dominated by: {causes}.")
