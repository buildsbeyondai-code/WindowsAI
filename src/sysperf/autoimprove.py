"""
Autonomous improvement loop for the System Performance Dashboard.

Every AUTO_INTERVAL_S (5 min) this controller:

  1. **scans**   — takes a live snapshot and folds it into the long-term trend
                   store (trends.py), so every scan also grows the multi-week
                   record it reasons from;
  2. **judges**  — compares the overall score to AUTO_THRESHOLD (80%);
  3. **RCAs**    — on a breach, rca.py ranks each shortfall by its share of the
                   scoring gap and attributes a root cause using BOTH the short
                   horizon (rolling median vs target) and the long horizon
                   (daily-typical drift, chronic misses, hour-of-day usage);
  4. **acts**    — executes only the remediations that (a) map to a real root
                   cause, (b) are ``safe`` risk by default, (c) are outside
                   their cooldown, and (d) have not been *learned* to be
                   useless for that metric;
  5. **verifies**— on the next scan it measures what actually changed and
                   updates an effectiveness ledger, so the loop keeps getting
                   better at picking actions instead of repeating dud ones;
  6. **learns long-term** — a criterion drifting the wrong way for days triggers
                   *preventive* action before the score breaches, while one that
                   has never been met across CHRONIC_DAYS gets a target-retune
                   proposal (human review) instead of pointless remediation.

Safety
------
* ``mode="off"`` disables the loop; ``mode="observe"`` does everything except
  execute (full RCA + plan, nothing runs); ``mode="auto"`` executes.
* Only ``safe`` fixes run unattended by default. ``caution`` fixes (visible
  disruption, e.g. restarting Explorer) require AUTO_ALLOW_CAUTION and are also
  deferred out of the user's measured peak-usage hours. ``manual`` and
  ``session_killing`` fixes are never executed — fixes.py enforces that too.
* Nothing here edits config.py or any scoring constant. Structural changes are
  written as proposals for a human to accept.
* Attribution caveat: when a cycle runs more than one action, each shares the
  credit/blame for that cycle's measured deltas — with AUTO_MAX_ACTIONS small
  and cooldowns long, the ledger still separates them over time.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import rca
from .config import (AUTO_ALLOW_RISKS, AUTO_COOLDOWN_S, AUTO_INTERVAL_S,
                    AUTO_LOG_MAX_LINES, AUTO_MAX_ACTIONS, AUTO_MODE,
                    AUTO_SETTLE_S, AUTO_THRESHOLD, FIX_ENABLED,
                    LEDGER_ALPHA, LEDGER_MIN_N, SCORE_TREND_MIN_DAYS)
from .trends import HIB

log = logging.getLogger("perfdash.auto")

MODES = ("off", "observe", "auto")
WIN_EPS = 0.005          # |normalised delta| below this == no measurable change


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _typicals(scorecard: dict, keys) -> dict:
    """{key: scored typical} for the given criteria, from a scorecard."""
    out = {}
    for row in scorecard.get("rows") or []:
        if row.get("key") in keys:
            val = row.get("scored_typical")
            out[row["key"]] = row.get("current") if val is None else val
    return out


def normalised_delta(key: str, before, after) -> float | None:
    """Improvement of one metric, signed so POSITIVE always means better.

    Scaled by the magnitude of the starting value so metrics with wildly
    different units (MB, counts, seconds) are comparable in one ledger.
    """
    if before is None or after is None:
        return None
    try:
        before, after = float(before), float(after)
    except (TypeError, ValueError):
        return None
    scale = max(abs(before), 1.0)
    raw = (after - before) / scale
    return raw if HIB.get(key, False) else -raw


class AutoImprover:
    """The 5-minute scan → RCA → fix → verify → learn controller."""

    # Optional 4.0 hooks, set by app.py: ``action_gate(fix_id) -> reason|None``
    # vetoes unattended execution; ``action_hook(ids, results, snapshot)`` records
    # every execution with its before-metric for later before/after judgement.
    action_gate = None
    action_hook = None

    def __init__(self, collector, store, fix_engine, state_file: Path,
                 log_file: Path, interval: float = AUTO_INTERVAL_S,
                 threshold: float = AUTO_THRESHOLD,
                 allow_risks=AUTO_ALLOW_RISKS,
                 max_actions: int = AUTO_MAX_ACTIONS,
                 cooldown_s: float = AUTO_COOLDOWN_S,
                 settle_s: float = AUTO_SETTLE_S,
                 mode: str = AUTO_MODE, clock=time.time,
                 on_cycle=None, target_store=None) -> None:
        self.collector = collector
        self.store = store
        self.fix_engine = fix_engine
        # Automatic target-realism layer (target_review.AutoTargetStore). When
        # present, the loop decides goal realism from telemetry itself and never
        # raises a target question to the user. None in unit tests -> unchanged.
        self.target_store = target_store
        self.state_file = Path(state_file)
        self.log_file = Path(log_file)
        self.interval = interval
        self.threshold = threshold
        self.allow_risks = tuple(allow_risks)
        self.max_actions = max_actions
        self.cooldown_s = cooldown_s
        self.settle_s = settle_s
        self.now = clock
        # Called once after each completed cycle, with the cycle record. Used by
        # the evolution layer to advance its APE counter on the loop's own
        # schedule instead of guessing from a poll. Failures here are swallowed by
        # the caller: an enhancement must never break the remediation loop.
        self.on_cycle = on_cycle
        # When the scheduled loop last ticked. A user-forced cycle updates
        # last_at (it IS the last cycle) but must not shift the schedule, so the
        # countdown is anchored here instead.
        self.loop_tick: float | None = None
        self.lock = threading.Lock()
        self.state = {"mode": mode, "cycles": 0, "cooldowns": {}, "ledger": {},
                      "pending": None, "proposals": [], "last": None,
                      "last_at": None}
        self._load()

    # ------------------------------------------------------------------ io ---
    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for key in ("mode", "cycles", "cooldowns", "ledger", "pending",
                    "proposals", "last", "last_at"):
            if key in data:
                self.state[key] = data[key]

    def _save(self) -> None:
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "updated": _iso(),
                                       **self.state}, indent=0))
            tmp.replace(self.state_file)
        except OSError:
            pass

    def _append_log(self, record: dict) -> None:
        try:
            with self.log_file.open("a") as fh:
                fh.write(json.dumps(record) + "\n")
            lines = self.log_file.read_text().splitlines()
            if len(lines) > AUTO_LOG_MAX_LINES:
                # Trim through a temp file + atomic replace: a plain write_text
                # truncates in place, so a concurrent reader (status/recent, or
                # the API) can observe a half-written log.
                tmp = self.log_file.with_suffix(".jsonl.tmp")
                tmp.write_text("\n".join(lines[-AUTO_LOG_MAX_LINES:]) + "\n")
                tmp.replace(self.log_file)
        except OSError:
            pass

    def recent(self, limit: int = 20) -> list:
        """The last `limit` cycle records, newest first."""
        try:
            lines = self.log_file.read_text().splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # ----------------------------------------------------------- learning ---
    def _ledger_entry(self, action_id: str, key: str) -> dict:
        return (self.state["ledger"].setdefault(action_id, {})
                .setdefault(key, {"ema": None, "n": 0, "wins": 0,
                                  "losses": 0, "last": None}))

    def _learn(self, action_id: str, key: str, delta: float) -> None:
        e = self._ledger_entry(action_id, key)
        e["n"] += 1
        e["last"] = round(delta, 4)
        e["ema"] = (delta if e["ema"] is None
                    else LEDGER_ALPHA * delta + (1 - LEDGER_ALPHA) * e["ema"])
        if delta > WIN_EPS:
            e["wins"] += 1
        elif delta < -WIN_EPS:
            e["losses"] += 1

    def _suppressed(self, action_id: str, key: str) -> str | None:
        """Learned reason to skip an action for a metric, if any."""
        e = (self.state["ledger"].get(action_id) or {}).get(key)
        if not e or e["n"] < LEDGER_MIN_N or e["ema"] is None:
            return None
        if e["ema"] > 0:
            return None
        return (f"learned ineffective for {key}: no net benefit across "
                f"{e['n']} verified attempts (avg {e['ema'] * 100:+.2f}%)")

    # ------------------------------------------------------- verification ---
    def _verify(self, scorecard: dict) -> dict | None:
        """Measure the effect of the previous cycle's actions and learn from it."""
        pending = self.state.get("pending")
        if not pending:
            return None
        waited = self.now() - pending["at"]
        if waited < self.settle_s:
            return {"status": "waiting", "waited_s": round(waited, 1),
                    "actions": pending["actions"]}
        after = _typicals(scorecard, pending["before"].keys())
        deltas, verdicts = {}, {}
        for key, before in pending["before"].items():
            delta = normalised_delta(key, before, after.get(key))
            if delta is None:
                continue
            deltas[key] = {"before": before, "after": after.get(key),
                           "delta_pct": round(delta * 100.0, 2)}
            verdicts[key] = ("improved" if delta > WIN_EPS
                             else "worsened" if delta < -WIN_EPS else "unchanged")
            for action_id in pending["actions"]:
                self._learn(action_id, key, delta)
        self.state["pending"] = None
        return {"status": "verified", "waited_s": round(waited, 1),
                "actions": pending["actions"], "metrics": deltas,
                "verdicts": verdicts}

    # -------------------------------------------------------------- policy ---
    def _cooling(self, action_id: str) -> float:
        """Seconds of cooldown left for an action (0 == ready)."""
        until = (self.state["cooldowns"] or {}).get(action_id)
        if until is None:
            return 0.0
        return max(0.0, until - self.now())

    def _candidates(self, report: dict) -> list:
        """(action_id, metric_key, why) in priority order for this report."""
        out = []
        if report["breach"]:
            for f in report["findings"]:
                if not f["actionable"]:
                    continue
                for fid in f["remediations"]:
                    out.append((fid, f["key"],
                                f"{f['label']} contributes {f['contribution_pct']}% "
                                f"of the gap ({f['cause']})"))
        else:
            for p in report["preventive"]:
                for fid in p["remediations"]:
                    out.append((fid, p["key"],
                                f"preventive: {p['label']} {p['reason']}"))
        return out

    def plan(self, report: dict) -> list:
        """Decide what to run. Pure policy — never executes anything."""
        budget = self.max_actions
        st = report.get("score_trend") or {}
        if (st.get("direction") == "degrading"
                and (st.get("days") or 0) >= SCORE_TREND_MIN_DAYS):
            budget += 1                      # long-term decline earns one extra
        decisions, chosen, seen = [], set(), set()
        for fid, key, why in self._candidates(report):
            if fid in seen:
                continue          # one decision per action per cycle, whatever
            seen.add(fid)         # the outcome — several metrics can map to it
            fix = self.fix_engine.get(fid)
            decision = {"id": fid, "for": key, "why": why,
                        "title": getattr(fix, "title", fid),
                        "risk": getattr(fix, "risk", "unknown"),
                        "eligible": False, "skip_reason": None}
            cool = self._cooling(fid)
            learned = self._suppressed(fid, key)
            if fix is None:
                decision["skip_reason"] = "unknown remediation id"
            elif not fix.executable:
                decision["skip_reason"] = "manual action required"
            elif self.action_gate is not None and self.action_gate(fid):
                # 4.0 action policy (optactions): autonomy level + per-action
                # opt-in + low-risk/reversible-only for unattended execution.
                decision["skip_reason"] = "policy: " + self.action_gate(fid)
            elif fix.risk not in self.allow_risks:
                decision["skip_reason"] = (
                    f"risk '{fix.risk}' not permitted unattended "
                    f"(allowed: {', '.join(self.allow_risks)})")
            elif fix.risk != "safe" and self.store.is_peak_now():
                decision["skip_reason"] = ("deferred: user-visible fix inside "
                                           "measured peak usage hours")
            elif cool > 0:
                decision["skip_reason"] = f"cooling down ({cool / 60:.0f} min left)"
            elif learned:
                decision["skip_reason"] = learned
            elif len(chosen) >= budget:
                decision["skip_reason"] = f"action budget reached ({budget}/cycle)"
            else:
                decision["eligible"] = True
                chosen.add(fid)
            decisions.append(decision)
        return decisions

    def _escalations(self, report: dict, decisions: list) -> list:
        """Proposals for gaps automation has proven it cannot close itself."""
        blocked = {d["for"]: d for d in decisions
                   if not d["eligible"] and d["skip_reason"]
                   and d["skip_reason"].startswith("learned ineffective")}
        out = []
        for f in report["findings"]:
            if f["cause"] != "long_term_regression" or f["key"] not in blocked:
                continue
            out.append({
                "key": f["key"], "kind": "escalate_manual",
                "detail": (f"{f['label']} is regressing long-term "
                           f"({f['long_term']['pct_per_day']:+.2f}%/day) and the "
                           f"available safe remediation is learned-ineffective "
                           f"({blocked[f['key']]['id']}). Manual investigation "
                           "needed."),
                "evidence": f["long_term"],
            })
        return out

    def _merge_proposals(self, new: list) -> None:
        by_key = {(p["key"], p["kind"]): p for p in self.state["proposals"]}
        for p in new:
            entry = by_key.get((p["key"], p["kind"]))
            if entry:
                entry.update(detail=p["detail"], evidence=p.get("evidence"),
                             seen=entry.get("seen", 1) + 1, last=_iso())
            else:
                by_key[(p["key"], p["kind"])] = {**p, "seen": 1,
                                                 "first": _iso(), "last": _iso()}
        self.state["proposals"] = list(by_key.values())

    # --------------------------------------------------------------- cycle ---
    def cycle(self, force: bool = False) -> dict:
        """One scan → RCA → act → verify → learn pass. Returns the record."""
        with self.lock:
            mode = self.state["mode"]
            snap = self.collector.snapshot()
            scorecard = snap.get("scorecard") or {}
            if not (scorecard.get("rows") or []):
                # No scored sample yet (service just started). Ingesting now
                # would push a meaningless 0% into the long-term score rollup.
                return {"at": _iso(), "t": self.now(), "mode": mode,
                        "score": None, "breach": False, "planned": [],
                        "executed": [], "skipped": [],
                        "note": "no scored sample yet — nothing to analyse"}
            self.store.ingest(snap.get("latest") or {}, scorecard)
            verification = self._verify(scorecard)
            report = rca.analyze(snap, self.store, self.fix_engine,
                                 self.threshold)
            record = {
                "at": _iso(), "t": self.now(), "mode": mode,
                "score": report["score"], "threshold": self.threshold,
                "breach": report["breach"], "regime": report["regime"]["regime"],
                "score_trend": report["score_trend"]["direction"],
                "summary": report["summary"],
                "findings": [{k: f[k] for k in
                              ("key", "label", "cause", "confidence",
                               "contribution_pct", "scored_typical", "target",
                               "explanation", "remediations")}
                             for f in report["findings"]],
                "preventive": report["preventive"],
                "verification": verification,
                "planned": [], "executed": [], "skipped": [],
                "proposals_open": 0,
            }

            if mode == "off" and not force:
                record["note"] = "auto-improve is off — scan and RCA only"
                return self._finish(record, report)

            decisions = self.plan(report)
            record["planned"] = decisions
            record["skipped"] = [d for d in decisions if not d["eligible"]]
            eligible = [d for d in decisions if d["eligible"]]
            new_proposals = (report["proposals"]
                             + self._escalations(report, decisions))
            # Automatic target-realism: decide goal realism from telemetry so the
            # user is never asked. Metrics the system has adjudicated (realistic
            # kept, or unreachable auto-revised) are dropped from the proposal
            # list, so no "decide about the goal" item is ever raised for them.
            if self.target_store is not None:
                try:
                    keys = [r.get("key") for r in (scorecard.get("rows") or [])
                            if r.get("key")]
                    self.target_store.review(self.store, keys)
                    decided = self.target_store.adjudicated_keys()
                    new_proposals = [p for p in new_proposals
                                     if p.get("key") not in decided]
                    self.state["proposals"] = [
                        p for p in self.state["proposals"]
                        if p.get("key") not in decided]
                except Exception:                        # pragma: no cover
                    pass
            self._merge_proposals(new_proposals)

            if not eligible:
                record["note"] = ("no eligible action this cycle"
                                  if report["breach"] else
                                  "score at or above threshold; nothing to do")
                return self._finish(record, report)

            if mode == "observe" and not force:
                record["note"] = ("observe mode: actions planned but not "
                                  "executed")
                return self._finish(record, report)
            if not FIX_ENABLED:
                record["note"] = ("execution disabled "
                                  "(PERFDASH_FIX_ENABLED=0) — plan only")
                return self._finish(record, report)

            ids = [d["id"] for d in eligible]
            results = self.fix_engine.apply(ids, snap)
            if self.action_hook is not None:
                try:
                    self.action_hook(ids, results, snap)
                except Exception:          # pragma: no cover - history must not break the loop
                    pass
            record["executed"] = results
            now = self.now()
            for fid in ids:
                self.state["cooldowns"][fid] = now + self.cooldown_s
            keys = [d["for"] for d in eligible]
            self.state["pending"] = {
                "at": now, "actions": ids, "keys": keys,
                "before": _typicals(scorecard, set(keys)),
            }
            record["note"] = (f"applied {len(ids)} remediation(s); effect will be "
                              f"measured on the next scan")
            return self._finish(record, report)

    def _finish(self, record: dict, report: dict) -> dict:
        record["proposals_open"] = len(self.state["proposals"])
        self.state["cycles"] += 1
        self.state["last"] = record
        self.state["last_at"] = record["t"]
        self._save()
        self._append_log(record)
        if self.on_cycle is not None:
            try:
                record["evolution"] = self.on_cycle(record)
            except Exception:
                log.exception("on_cycle hook failed")
        return record

    # -------------------------------------------------------------- runtime --
    def set_mode(self, mode: str) -> str:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        with self.lock:
            self.state["mode"] = mode
            self._save()
        return mode

    def status(self) -> dict:
        # Snapshot the mutable state under the lock: a cycle running on the
        # daemon thread mutates cooldowns/ledger while an API request reads them,
        # and iterating a dict that another thread resizes raises RuntimeError.
        with self.lock:
            cooldowns = dict(self.state["cooldowns"] or {})
            ledger = json.loads(json.dumps(self.state["ledger"]))
            proposals = list(self.state["proposals"])
            pending = self.state.get("pending")
            pending = dict(pending) if pending else None
            last = self.state.get("last")
            last_at = self.state.get("last_at")
            cycles = self.state["cycles"]
            mode = self.state["mode"]
        anchor = self.loop_tick if self.loop_tick is not None else last_at
        due_in = (None if anchor is None
                  else max(0.0, round(anchor + self.interval - self.now(), 1)))
        return {
            "mode": mode, "modes": list(MODES),
            "interval_s": self.interval, "threshold": self.threshold,
            "execution_enabled": FIX_ENABLED,
            "allowed_risks": list(self.allow_risks),
            "max_actions_per_cycle": self.max_actions,
            "cooldown_s": self.cooldown_s, "settle_s": self.settle_s,
            "cycles": cycles,
            "last_scan": (last or {}).get("at"),
            "next_scan_in_s": due_in,
            "cooldowns": {k: round(max(0.0, v - self.now()), 1)
                          for k, v in cooldowns.items() if v > self.now()},
            "pending_verification": pending,
            "ledger": ledger,
            "proposals": proposals,
            "last": last,
        }

    def loop(self) -> None:                          # pragma: no cover
        """Run cycle() every `interval` seconds, forever (daemon thread)."""
        while True:
            self.loop_tick = self.now()
            try:
                rec = self.cycle()
                log.info("auto-improve cycle: score=%s breach=%s executed=%d",
                         rec.get("score"), rec.get("breach"),
                         len(rec.get("executed") or []))
            except Exception:
                log.exception("auto-improve cycle failed")
            time.sleep(self.interval)
