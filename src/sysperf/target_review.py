"""Automatic target-realism adjudication.

The user should never be asked "is this goal realistic?". The system decides it
from past data:

  * A target the machine has met on at least one settled day is REALISTIC — the
    goal is fair, so a shortfall is real load, not a bad goal. Nothing is asked.
  * A target NEVER met across a settled window (met_days == 0 with at least
    MIN_SETTLED_DAYS of history) is UNREACHABLE — it sits beyond this machine's
    floor. The system auto-revises it to the honest achievable value (the best
    daily-typical actually recorded) and the scorecard uses the revised target.

The revision is a runtime override persisted to its own JSON file. config.py's
CRITERIA is never rewritten, so a revision is fully reversible by deleting the
override. Honesty guard: the revised target is the PROVEN-ACHIEVABLE value (the
window's personal best), never an arbitrarily looser number — the point is to
make the goal honest, not to flatter the score.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

# Metrics whose target is a hard requirement or externally fixed, where an
# auto-revision would be meaningless or wrong (0 hardware faults must stay 0;
# temperature / battery-wear limits are physical, not workload goals).
NON_ADJUSTABLE = {"hw_faults", "cpu_temp_c", "battery_wear_pct", "vmmem_mb"}

# Enough settled days to trust a "never met" reading (matches trends.CHRONIC_DAYS).
MIN_SETTLED_DAYS = 5


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def adjudicate(trend: dict) -> dict | None:
    """Decide target realism for one metric from its long-term trend row.

    `trend` is a trends.TrendStore.analyze() dict (or the equivalent evidence
    carried on a finding). Returns a verdict dict, or None when there is not
    enough data — or no business — to decide.
    """
    key = trend.get("key")
    settled = trend.get("settled_days") or 0
    if not key or key in NON_ADJUSTABLE or settled < MIN_SETTLED_DAYS:
        return None
    met_days = trend.get("met_days")
    target = trend.get("target")
    if met_days is None or target is None:
        return None
    hib = bool(trend.get("higher_is_better"))
    label = trend.get("label") or key
    # best daily-typical actually achieved over the window
    best = trend.get("max") if hib else trend.get("min")

    if met_days > 0:
        return {
            "key": key, "label": label, "verdict": "realistic",
            "target": target, "honest_target": target,
            "met_days": met_days, "settled_days": settled,
            "best_observed": best,
            "rationale": (f"Met the goal on {met_days} of the last {settled} "
                          f"days, so the goal is achievable — the shortfall is "
                          f"real load, not an unfair goal. No change."),
        }

    # never met across a settled window -> genuinely unreachable
    if best is None:
        return None
    honest = int(round(float(best)))
    return {
        "key": key, "label": label, "verdict": "unreachable",
        "target": target, "honest_target": honest,
        "met_days": 0, "settled_days": settled, "best_observed": best,
        "higher_is_better": hib,
        "rationale": (f"Never met the goal on any of the last {settled} days; "
                      f"the best this machine actually reached was {best} "
                      f"against a goal of {target}, so {target} was unreachable. "
                      f"Auto-revised to {honest} (the proven-achievable value)."),
    }


class AutoTargetStore:
    """Persisted auto-decisions and the runtime target overrides they imply."""

    def __init__(self, state_file) -> None:
        self.state_file = Path(state_file)
        self.lock = threading.Lock()
        self._by_key: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self._by_key = {k: v for k, v in data.items()
                            if isinstance(v, dict)}

    def _save(self) -> None:
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._by_key, indent=2))
            tmp.replace(self.state_file)
        except OSError:                                  # pragma: no cover
            pass

    def overrides(self) -> dict:
        """key -> revised target, only for metrics judged unreachable."""
        return {k: v["honest_target"] for k, v in self._by_key.items()
                if v.get("verdict") == "unreachable"
                and v.get("honest_target") is not None}

    def decisions(self) -> list:
        """All current auto-decisions, newest first (for display)."""
        return sorted(self._by_key.values(),
                      key=lambda d: d.get("decided_at", ""), reverse=True)

    def adjudicated_keys(self) -> set:
        """Metric keys the system has decided on this run."""
        return set(self._by_key.keys())

    def review(self, trend_store, keys) -> list:
        """Re-adjudicate the given metric keys from the trend store, persisting
        any changed decision. Returns the verdicts in play after the review."""
        verdicts = []
        with self.lock:
            changed = False
            for key in keys:
                try:
                    trend = trend_store.analyze(key)
                except Exception:                        # pragma: no cover
                    logging.getLogger("perfdash").exception("trend analysis of %s failed", key)
                    continue
                verdict = adjudicate(trend)
                if verdict is None:
                    if key in self._by_key:              # stale -> drop
                        del self._by_key[key]
                        changed = True
                    continue
                prev = self._by_key.get(key)
                if (not prev or prev.get("verdict") != verdict["verdict"]
                        or prev.get("honest_target") != verdict["honest_target"]):
                    verdict["decided_at"] = _iso()
                    self._by_key[key] = verdict
                    changed = True
                else:
                    verdict = prev            # keep original decided_at
                verdicts.append(verdict)
            if changed:
                self._save()
        return verdicts
