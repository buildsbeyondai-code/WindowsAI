"""Explainable, prioritised recommendations built only from measured data.

Every recommendation answers the same seven questions, and every answer comes
from something SysPerf measured or recorded; where nothing was measured it says
so instead of estimating:

1. what is wrong            the finding's label and the RCA explanation
2. the evidence             typical value vs target, cause, share of the gap,
                            confidence (from ``rca``)
3. the proposed fix         each remediation the finding names (``fixes``)
4. the expected improvement the measured effect of that fix ALONE on that metric
                            on this PC (runs, recency-weighted change, wins and
                            losses, last before/after, age), "seen only with other
                            fixes", or "not measured yet"
5. the risks                the fix's risk class
6. whether it reverses      the fix's ``reversible`` flag
7. how success is measured  the metric re-measured after the settle time,
                            compared with before

Recommendations are ordered by share of the gap x confidence, so the one that
explains most of the problem with the most certainty comes first.
"""
from __future__ import annotations

# The registry's risk levels, in the four classes the UI shows.
RISK_CLASS = {"safe": "Safe", "caution": "Moderate", "manual": "Advanced"}


def risk_class(risk: str | None, evolved: bool = False) -> str:
    """Safe / Moderate / Advanced / Experimental for a fix."""
    if evolved:
        return "Experimental"                 # a recipe self-evolution promoted
    return RISK_CLASS.get(str(risk or ""), "Advanced")


FEW_RUNS = 3          # fewer measured runs than this: shown as not yet reliable
STALE_DAYS = 30       # last measured longer ago than this: shown as possibly out of date


def _num(v) -> str:
    return f"{v:,.0f}" if isinstance(v, (int, float)) and abs(v) >= 100 else (
        f"{v:g}" if isinstance(v, (int, float)) else "?")


def expected(ledger: dict, fix_id: str, key: str, now: float | None = None) -> dict:
    """What this fix did to this metric on this PC, from the ledger only.

    5.5.2 (audit F4): an effect counts only when the fix ran alone in its batch;
    a change seen after several fixes together is a group observation and is
    not attributed. The average is a recency-weighted one (an exponential
    moving average), and the last before/after values are shown so a
    10-point drop is not read as a 20% one. Entries recorded before 5.5.2 were
    credited per batch and are labelled so.
    """
    e = (ledger.get(fix_id) or {}).get(key) or {}
    n, group_n = int(e.get("n") or 0), int(e.get("group_n") or 0)
    if not n or e.get("ema") is None:
        if group_n:
            return {"measured": False, "group_only": True, "group_runs": group_n,
                    "text": f"no effect of its own measured yet: seen only together with "
                            f"other fixes ({group_n} batch(es))"}
        return {"measured": False, "text": "not measured on this PC yet"}
    pct = round(float(e["ema"]) * 100, 1)
    word = "better" if pct > 0 else "worse" if pct < 0 else "no change"
    wins, losses = int(e.get("wins") or 0), int(e.get("losses") or 0)
    text = (f"{abs(pct)}% {word}" if pct else "no measurable change") + \
        f" (recency-weighted, {n} run(s): {wins} better, {losses} worse)"
    if e.get("last_before") is not None and e.get("last_after") is not None:
        text += f"; last run {_num(e['last_before'])} → {_num(e['last_after'])}"
    at, stale = e.get("at"), False
    if at and now:
        days = (now - at) / 86400
        ago = "today" if days < 1 else "1 day ago" if days < 2 else f"{days:.0f} days ago"
        stale = days > STALE_DAYS
        text += f", measured {ago}" + (", may be out of date" if stale else "")
    uncertain = n < FEW_RUNS
    if uncertain:
        text += f"; only {n} run(s), not yet reliable"
    legacy = e.get("v") != 2
    if legacy:
        text += " (recorded before 5.5.2: may include other fixes run at the same time)"
    out = {"measured": True, "runs": n, "recency_weighted_change_pct": pct, "wins": wins,
           "losses": losses, "legacy": legacy, "measured_at": at, "uncertain": uncertain,
           "stale": stale, "text": text}
    if group_n:
        out["group_runs"] = group_n
    return out


def explain(findings: list, fixes: dict, ledger: dict, settle_s: float,
            now: float | None = None) -> list:
    """The findings as ranked recommendations; see the module docstring."""
    out = []
    for f in findings or []:
        key, label = f.get("key"), f.get("label") or f.get("key")
        conf = float(f.get("confidence") or 0)
        share = float(f.get("contribution_pct") or 0)
        steps = []
        for fid in f.get("remediations") or []:
            fx = fixes.get(fid) or {}
            steps.append({
                "id": fid, "title": fx.get("title") or fid,
                "risk": risk_class(fx.get("risk"), bool(fx.get("evolved"))),
                "reversible": bool(fx.get("reversible")),
                "expected": expected(ledger, fid, key, now)})
        out.append({
            "key": key, "what": f"{label}: {f.get('explanation') or 'outside its target'}",
            "evidence": {"typical": f.get("scored_typical"), "target": f.get("target"),
                         "cause": f.get("cause"), "share_of_gap_pct": share,
                         "confidence": conf},
            "fixes": steps,
            "measure": (f"{label} is re-measured about {round(settle_s / 60)} min after a "
                        "fix and compared with the value before it; the result is kept "
                        "and used for the next recommendation"),
            "priority": round(share * conf, 2)})
    return sorted(out, key=lambda r: -r["priority"])
