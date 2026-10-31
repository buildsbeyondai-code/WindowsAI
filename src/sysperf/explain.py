"""Explainable, prioritised recommendations built only from measured data.

Every recommendation answers the same seven questions, and every answer comes
from something SysPerf measured or recorded; where nothing was measured it says
so instead of estimating:

1. what is wrong            the finding's label and the RCA explanation
2. the evidence             typical value vs target, cause, share of the gap,
                            confidence (from ``rca``)
3. the proposed fix         each remediation the finding names (``fixes``)
4. the expected improvement the measured effect of that fix on that metric on
                            this PC (the auto-improve ledger: runs, average
                            change, wins/losses) or "not measured yet"
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


def expected(ledger: dict, fix_id: str, key: str) -> dict:
    """What this fix did to this metric on this PC, from the ledger only."""
    e = (ledger.get(fix_id) or {}).get(key) or {}
    n = int(e.get("n") or 0)
    if not n or e.get("ema") is None:
        return {"measured": False, "text": "not measured on this PC yet"}
    pct = round(float(e["ema"]) * 100, 1)
    word = "better" if pct > 0 else "worse" if pct < 0 else "no change"
    return {"measured": True, "runs": n, "avg_change_pct": pct,
            "wins": int(e.get("wins") or 0), "losses": int(e.get("losses") or 0),
            "text": (f"{abs(pct)}% {word} on average over {n} measured run(s)"
                     if pct else f"no measurable change over {n} run(s)")}


def explain(findings: list, fixes: dict, ledger: dict, settle_s: float) -> list:
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
                "expected": expected(ledger, fid, key)})
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
