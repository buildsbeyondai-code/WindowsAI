#!/usr/bin/env python3
"""
Continuous-learning view over the auto-improve loop.

The loop in :mod:`autoimprove` already scans, reasons, fixes, verifies and
learns. What it lacked was a way to *show* that learning: the state file holds a
ledger and a proposal list, and the cycle log holds every decision, but neither
was ever aggregated into something a reader can judge. This module is that
aggregation layer.

Everything here is derived, read-only and bounded — no loop state is mutated and
no unbounded file is read whole:

    kpis()            headline counters (cycles, strategies, applied, ...)
    strategies()      one row per learned remediation, with measured effect
    bands()           success-rate distribution, for the donut
    timeline()        learning events per calendar day, for the area chart
    by_type()         activity-type breakdown
    learnings()       newest-first feed of what each cycle actually taught
    knowledge_sets()  per-metric knowledge the loop has accumulated
    rulebook()        the loop's operating rules + a live compliance audit
    heal_vs_evolve()  evidence for the react-vs-learn explainer
    snapshot()        all of the above in one payload

A "strategy" is a (fix id, metric) pair: the same fix can help one metric and do
nothing for another, and the ledger scores them separately, so the pair — not
the fix — is the unit that gets learned.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

# A strategy needs this many measured attempts before "no effect" is treated as
# a verdict rather than as noise. Mirrors the loop's own suppression rule.
SUPPRESS_AFTER = 3

# Causes the loop attributes a shortfall to, in plain English. Kept here rather
# than in the page so the API is self-describing.
CAUSE_GLOSS = {
    "chronic_unreachable": "target sits at or below this machine's floor",
    "long_term_regression": "daily typical is drifting the wrong way",
    "transient_spike": "a burst above the metric's own recent typical",
    "usage_driven": "the cost of the current workload",
    "steady_shortfall": "a persistent gap needing a structural change",
    "near_target": "effectively on target",
}


def _read_cycles(log_file, limit: int) -> list:
    """Oldest-first cycle records from the JSONL log, capped at ``limit``."""
    try:
        lines = Path(log_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _day(rec: dict) -> str:
    """The calendar day a cycle belongs to (UTC), or "" when unstamped."""
    return str(rec.get("at") or "")[:10]


def _pct(value):
    """A 0-1 ratio as a signed percentage, or None."""
    return None if value is None else round(value * 100, 3)


def _band(rate) -> str:
    if rate is None:
        return "unrated"
    if rate >= 90:
        return "90-100"
    if rate >= 70:
        return "70-89"
    if rate >= 50:
        return "50-69"
    return "<50"


class LearningView:
    """Aggregates the auto-improve loop's learning into display-ready shapes.

    ``improver`` supplies live state, ``trend_store`` the long-term per-metric
    record and ``fix_engine`` the remediation catalogue (for titles and risk).
    ``log_file`` is the loop's JSONL cycle log.
    """

    def __init__(self, improver, trend_store, fix_engine, log_file,
                 log_limit: int = 500, remediation_map=None,
                 evolved_map=None) -> None:
        self.improver = improver
        self.trends = trend_store
        self.fixes = fix_engine
        self.log_file = Path(log_file)
        self.log_limit = int(log_limit)
        # The catalogue's metric -> remediation ids map (rca.REMEDIATION), plus a
        # callable-free view of any mappings the evolution layer has learned.
        self.remediation_map = remediation_map or {}
        self.evolved_map = evolved_map if evolved_map is not None else {}

    # ---------------------------------------------------------------- inputs
    def _state(self) -> dict:
        """The loop's live state, tolerant of a not-yet-written state file."""
        try:
            return self.improver.status()
        except Exception:                                # pragma: no cover
            return {}

    def _cycles(self) -> list:
        return _read_cycles(self.log_file, self.log_limit)

    def _log_lines(self) -> int:
        """Lines actually on disk.

        Deliberately not ``len(self._cycles())``: that read is already truncated
        to ``log_limit``, so comparing it against the limit could never fail and
        the bounded-log rule would be decorative.
        """
        try:
            with self.log_file.open("r", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0

    def _fix_meta(self, fix_id: str) -> dict:
        """Title/risk/category for a fix id, falling back to the bare id."""
        fix = self.fixes.get(fix_id) if self.fixes else None
        if fix is None:
            return {"title": fix_id, "risk": "unknown", "category": "unknown",
                    "reversible": None}
        return {"title": fix.title, "risk": fix.risk, "category": fix.category,
                "reversible": bool(fix.reversible)}

    # ------------------------------------------------------------ strategies
    def strategies(self, state: dict | None = None) -> list:
        """One row per learned (fix, metric) pair, newest measurement first."""
        state = self._state() if state is None else state
        rows = []
        for fix_id, by_key in (state.get("ledger") or {}).items():
            if not isinstance(by_key, dict):
                continue
            for key, entry in by_key.items():
                if not isinstance(entry, dict):
                    continue
                attempts = int(entry.get("n") or 0)
                wins = int(entry.get("wins") or 0)
                losses = int(entry.get("losses") or 0)
                rated = wins + losses
                ema = entry.get("ema")
                effect = _pct(ema)
                if ema is None:
                    status = "learning"
                elif ema > 0:
                    status = "effective"
                elif attempts >= SUPPRESS_AFTER:
                    status = "suppressed"
                else:
                    status = "learning"
                meta = self._fix_meta(fix_id)
                rows.append({
                    "kind": "autofix",
                    "id": fix_id,
                    "metric": key,
                    "title": meta["title"],
                    "risk": meta["risk"],
                    "category": meta["category"],
                    "reversible": meta["reversible"],
                    "attempts": attempts,
                    "wins": wins,
                    "losses": losses,
                    "effect_pct": effect,
                    "last_effect_pct": _pct(entry.get("last")),
                    "success_rate": (round(100.0 * wins / rated, 1)
                                     if rated else None),
                    "status": status,
                })
        rows.sort(key=lambda r: (-r["attempts"], r["id"], r["metric"]))
        return rows


    def bands(self, strategies: list | None = None) -> dict:
        """Success-rate distribution across strategies, for the donut."""
        strategies = self.strategies() if strategies is None else strategies
        counts = Counter(_band(s["success_rate"]) for s in strategies)
        return {b: counts.get(b, 0)
                for b in ("90-100", "70-89", "50-69", "<50", "unrated")}

    # -------------------------------------------------------------- activity
    def _events(self, cycles: list) -> list:
        """Flatten cycles into discrete learning events."""
        events = []
        prev_open = None
        for rec in cycles:
            day = _day(rec)
            for act in rec.get("executed") or []:
                if isinstance(act, dict):
                    events.append({"day": day, "type": "fix_applied",
                                   "rec": rec, "item": act})
            verification = rec.get("verification")
            if isinstance(verification, dict) \
                    and verification.get("status") == "verified":
                events.append({"day": day, "type": "effect_measured",
                               "rec": rec, "item": verification})
            for item in rec.get("preventive") or []:
                if isinstance(item, dict):
                    events.append({"day": day, "type": "preventive_flagged",
                                   "rec": rec, "item": item})
            for item in rec.get("skipped") or []:
                if isinstance(item, dict) and item.get("risk") == "manual":
                    events.append({"day": day, "type": "escalated_manual",
                                   "rec": rec, "item": item})
            if rec.get("findings"):
                events.append({"day": day, "type": "rca_run",
                               "rec": rec, "item": None})
            now_open = rec.get("proposals_open")
            if isinstance(now_open, int) and prev_open is not None \
                    and now_open > prev_open:
                events.append({"day": day, "type": "proposal_opened",
                               "rec": rec, "item": None})
            if isinstance(now_open, int):
                prev_open = now_open
        return events

    def timeline(self, cycles: list | None = None) -> list:
        """Learning events and cycles per calendar day, oldest first."""
        cycles = self._cycles() if cycles is None else cycles
        events = self._events(cycles)
        per_day = defaultdict(lambda: {"count": 0, "cycles": 0, "breaches": 0})
        for rec in cycles:
            day = _day(rec)
            if not day:
                continue
            per_day[day]["cycles"] += 1
            if rec.get("breach"):
                per_day[day]["breaches"] += 1
        for ev in events:
            if ev["day"]:
                per_day[ev["day"]]["count"] += 1
        return [{"date": day, **vals} for day, vals in sorted(per_day.items())]

    def by_type(self, cycles: list | None = None) -> dict:
        """How many of each kind of learning event the window holds."""
        cycles = self._cycles() if cycles is None else cycles
        return dict(Counter(ev["type"] for ev in self._events(cycles)))

    # ------------------------------------------------------------- learnings
    def learnings(self, cycles: list | None = None, limit: int = 40) -> list:
        """Newest-first feed of what each substantive cycle actually taught."""
        cycles = self._cycles() if cycles is None else cycles
        out = []
        for rec in reversed(cycles):
            applied = [a for a in (rec.get("executed") or [])
                       if isinstance(a, dict)]
            verification = rec.get("verification")
            if not (isinstance(verification, dict)
                    and verification.get("status") == "verified"):
                verification = None
            findings = [f for f in (rec.get("findings") or [])
                        if isinstance(f, dict)]
            manual = [s for s in (rec.get("skipped") or [])
                      if isinstance(s, dict) and s.get("risk") == "manual"]
            if not (applied or verification or manual):
                continue                    # a plain in-budget scan taught nothing

            if verification:
                kind, headline = "effect_measured", "Measured a fix's real effect"
            elif applied:
                kind, headline = "fix_applied", "Applied a remediation"
            else:
                kind, headline = "escalated", "Escalated to a human"

            top = max(findings, key=lambda f: f.get("contribution_pct") or 0,
                      default=None)
            cause = str((top or {}).get("cause") or "")
            out.append({
                "at": rec.get("at"),
                "kind": kind,
                "headline": headline,
                "score": rec.get("score"),
                "breach": bool(rec.get("breach")),
                "regime": str(rec.get("regime") or ""),
                "summary": str(rec.get("summary") or rec.get("note") or ""),
                "driver": str((top or {}).get("label") or ""),
                "cause": cause,
                "cause_gloss": CAUSE_GLOSS.get(cause, ""),
                "applied": [{"id": str(a.get("id") or ""),
                             "title": str(a.get("title") or ""),
                             "risk": str(a.get("risk") or "")}
                            for a in applied],
                "escalated": [{"id": str(s.get("id") or ""),
                               "title": str(s.get("title") or ""),
                               "reason": str(s.get("skip_reason") or "")}
                              for s in manual],
                "measured": (self._measured(verification)
                             if verification else None),
            })
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _measured(verification: dict) -> dict:
        """Flatten a verification record into per-metric measured deltas."""
        metrics = verification.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        verdicts = verification.get("verdicts")
        verdicts = verdicts if isinstance(verdicts, dict) else {}
        return {
            "actions": [str(a) for a in (verification.get("actions") or [])],
            "waited_s": verification.get("waited_s"),
            "metrics": [{"metric": key,
                         "delta_pct": (val or {}).get("delta_pct"),
                         "verdict": str(verdicts.get(key) or "")}
                        for key, val in sorted(metrics.items())],
        }

    # -------------------------------------------------------- knowledge sets
    def knowledge_sets(self, state: dict | None = None,
                       strategies: list | None = None) -> list:
        """Per-metric knowledge: long-term shape plus the fixes that move it.

        The ``state`` field distinguishes four genuinely different situations,
        which an earlier version wrongly collapsed into two:

        ``learned``    a mapped remediation is measured effective here.
        ``observed``   remediations have acted and been measured, none proven yet.
        ``mapped``     the catalogue maps a remediation, but it has never run here.
        ``unmapped``   nothing in the catalogue addresses this metric, so a
                       shortfall can only ever be escalated. These are the metrics
                       the self-authoring layer exists to close.
        """
        state = self._state() if state is None else state
        strategies = self.strategies(state) if strategies is None else strategies
        by_metric = defaultdict(list)
        for row in strategies:
            by_metric[row["metric"]].append(row)

        try:
            trends = self.trends.summary(None)
        except Exception:                                # pragma: no cover
            trends = {}
        rows = (trends.get("metrics") or {}).values()

        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            if not key:
                continue
            known = by_metric.get(key, [])
            mapped = list(self.mapped_remediations(key))
            if any(s["status"] == "effective" for s in known):
                knowledge = "learned"
            elif known:
                knowledge = "observed"
            elif mapped:
                knowledge = "mapped"
            else:
                knowledge = "unmapped"
            out.append({
                "key": key,
                "label": str(row.get("label") or key),
                "direction": str(row.get("direction") or ""),
                "pct_per_day": row.get("pct_per_day"),
                "days": row.get("window_days") or row.get("samples"),
                "met_days": row.get("met_days"),
                "chronic": bool(row.get("chronic")),
                "strategy_count": len(known),
                "mapped_count": len(mapped),
                "mapped": mapped,
                "strategies": [{"id": s["id"], "status": s["status"],
                                "effect_pct": s["effect_pct"]} for s in known],
                "state": knowledge,
            })
        out.sort(key=lambda r: (-r["strategy_count"], -r["mapped_count"], r["key"]))
        return out

    def mapped_remediations(self, key: str) -> tuple:
        """Remediation ids the catalogue (plus any evolved mapping) offers for a
        metric. Evolved mappings are included so a newly learned pairing counts
        as coverage immediately."""
        base = tuple(self.remediation_map.get(key, ()))
        extra = tuple(self.evolved_map.get(key, ())) if self.evolved_map else ()
        return tuple(dict.fromkeys(base + extra))

    # ---------------------------------------------------------------- rules
    def rulebook(self, state: dict | None = None,
                 cycles: list | None = None) -> dict:
        """The loop's operating rules, each audited against the cycle log.

        Every rule is checked, not asserted: a rule with no evidence is reported
        as unverifiable rather than silently passing.
        """
        state = self._state() if state is None else state
        cycles = self._cycles() if cycles is None else cycles
        allowed = tuple(state.get("allowed_risks") or ())
        max_actions = int(state.get("max_actions_per_cycle") or 0)
        cooldown_s = float(state.get("cooldown_s") or 0)

        executed = [(rec, act) for rec in cycles
                    for act in (rec.get("executed") or [])
                    if isinstance(act, dict)]

        # 1. unattended risk class
        bad_risk = sorted({str(a.get("risk")) for _, a in executed
                           if a.get("risk") and a.get("risk") not in allowed})
        # 2. per-cycle action budget. The loop grants one extra action when the
        # long-term score trend is degrading, so the ceiling is N+1, not N.
        ceiling = max_actions + 1 if max_actions else 0
        over_budget = [_day(rec) for rec in cycles
                       if ceiling and len(rec.get("executed") or []) > ceiling]
        # 3. cooldown between repeats of the same action
        last_t: dict = {}
        too_soon = []
        for rec, act in executed:
            fix_id, stamp = str(act.get("id") or ""), rec.get("t")
            if not isinstance(stamp, (int, float)):
                continue
            prev = last_t.get(fix_id)
            if prev is not None and cooldown_s and (stamp - prev) < cooldown_s:
                too_soon.append(fix_id)
            last_t[fix_id] = stamp
        # 4. bounded cycle log
        log_lines = self._log_lines()

        rules = [
            {"rule": "Only reversible, non-disruptive fixes run unattended",
             "expect": ", ".join(allowed) or "none",
             "severity": "critical",
             "checked": len(executed),
             "violations": [f"executed a '{r}' risk fix unattended" for r in bad_risk]},
            {"rule": "At most N remediations per cycle",
             "expect": f"{max_actions} (+1 while the score trend is degrading)",
             "severity": "critical",
             "checked": len(cycles),
             "violations": [f"{len(over_budget)} cycle(s) exceeded the budget"]
                           if over_budget else []},
            {"rule": "A repeated action waits out its cooldown",
             "expect": f"{cooldown_s:.0f}s",
             "severity": "high",
             "checked": len(executed),
             "violations": [f"{fix_id} repeated inside its cooldown"
                            for fix_id in sorted(set(too_soon))]},
            {"rule": "A fix's effect is measured on the next scan before it repeats",
             "expect": f"settle {float(state.get('settle_s') or 0):.0f}s",
             "severity": "high",
             "checked": sum(1 for rec in cycles
                            if isinstance(rec.get("verification"), dict)
                            and rec["verification"].get("status") == "verified"),
             "violations": []},
            {"rule": f"A fix measured ineffective {SUPPRESS_AFTER}x is suppressed",
             "expect": f"n >= {SUPPRESS_AFTER}",
             "severity": "medium",
             "checked": len(self.strategies(state)),
             "violations": []},
            {"rule": "Actions needing elevation or user attention are proposed, never run",
             "expect": "manual => propose",
             "severity": "critical",
             "checked": sum(1 for rec in cycles
                            for s in (rec.get("skipped") or [])
                            if isinstance(s, dict) and s.get("risk") == "manual"),
             "violations": [f"executed manual fix {a.get('id')}"
                            for _, a in executed if a.get("risk") == "manual"]},
            {"rule": "The cycle log stays bounded",
             "expect": f"<= {self.log_limit} lines",
             "severity": "low",
             "checked": log_lines,
             "violations": ([f"log holds {log_lines} lines"]
                            if log_lines > self.log_limit else [])},
        ]
        for rule in rules:
            rule["ok"] = not rule["violations"]
            rule["verifiable"] = rule["checked"] > 0
        violations = [v for r in rules for v in r["violations"]]
        return {
            "rules": rules,
            "rule_count": len(rules),
            "compliance": {
                "clean": not violations,
                "violations": violations,
                "checked_rules": len(rules),
                "verifiable_rules": sum(1 for r in rules if r["verifiable"]),
                "window_cycles": len(cycles),
            },
        }

    # --------------------------------------------------- heal vs. evolve
    def heal_vs_evolve(self, state: dict | None = None,
                       cycles: list | None = None,
                       strategies: list | None = None) -> dict:
        """Live evidence for both layers, so the explainer quotes real numbers."""
        state = self._state() if state is None else state
        cycles = self._cycles() if cycles is None else cycles
        strategies = self.strategies(state) if strategies is None else strategies

        applied = sum(len(rec.get("executed") or []) for rec in cycles)
        measured = sum(1 for rec in cycles
                       if isinstance(rec.get("verification"), dict)
                       and rec["verification"].get("status") == "verified")
        transient = sum(1 for rec in cycles for f in (rec.get("findings") or [])
                        if isinstance(f, dict) and f.get("cause") == "transient_spike")
        breaches = sum(1 for rec in cycles if rec.get("breach"))
        # A breach the next cycle no longer reports is one the loop rode out.
        recovered = 0
        flags = [bool(rec.get("breach")) for rec in cycles]
        for i in range(len(flags) - 1):
            if flags[i] and not flags[i + 1]:
                recovered += 1
        effective = [s for s in strategies if s["status"] == "effective"]
        suppressed = [s for s in strategies if s["status"] == "suppressed"]
        preventive = sum(len(rec.get("preventive") or []) for rec in cycles)

        return {
            "window_cycles": len(cycles),
            "healing": {
                "fixes_applied": applied,
                "effects_measured": measured,
                "transient_spikes_absorbed": transient,
                "breaches_seen": breaches,
                "breaches_cleared": recovered,
                "open_breach": bool((state.get("last") or {}).get("breach")),
            },
            "evolution": {
                "strategies_learned": len(strategies),
                "strategies_effective": len(effective),
                "strategies_suppressed": len(suppressed),
                "preventive_flags": preventive,
                "escalations_open": len(state.get("proposals") or []),
                "knowledge_sets": len(self.knowledge_sets(state, strategies)),
            },
        }

    # ------------------------------------------------------------------ kpis
    def kpis(self, state: dict | None = None, cycles: list | None = None,
             strategies: list | None = None) -> dict:
        state = self._state() if state is None else state
        cycles = self._cycles() if cycles is None else cycles
        strategies = self.strategies(state) if strategies is None else strategies
        rated = [s["success_rate"] for s in strategies
                 if s["success_rate"] is not None]
        days = {_day(rec) for rec in cycles if _day(rec)}
        return {
            "cycles": int(state.get("cycles") or 0),
            "strategies": len(strategies),
            "effective": sum(1 for s in strategies if s["status"] == "effective"),
            "fixes_applied": sum(len(rec.get("executed") or []) for rec in cycles),
            "escalations": len(state.get("proposals") or []),
            "avg_success_rate": (round(sum(rated) / len(rated), 1)
                                 if rated else None),
            "learning_days": len(days),
            "mode": str(state.get("mode") or ""),
            "execution_enabled": bool(state.get("execution_enabled")),
        }

    # -------------------------------------------------------------- snapshot
    def snapshot(self, learnings_limit: int = 40) -> dict:
        """Everything the Continuous-Learning view needs, in one read."""
        state = self._state()
        cycles = self._cycles()
        strategies = self.strategies(state)
        knowledge = self.knowledge_sets(state, strategies)
        return {
            "kpis": self.kpis(state, cycles, strategies),
            "strategies": strategies,
            "bands": self.bands(strategies),
            "timeline": self.timeline(cycles),
            "by_type": self.by_type(cycles),
            "learnings": self.learnings(cycles, learnings_limit),
            "knowledge_sets": knowledge,
            "rulebook": self.rulebook(state, cycles),
            "heal_vs_evolve": self.heal_vs_evolve(state, cycles, strategies),
            "cause_gloss": CAUSE_GLOSS,
            "window_cycles": len(cycles),
        }
