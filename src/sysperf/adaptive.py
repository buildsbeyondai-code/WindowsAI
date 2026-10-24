"""
Adaptive, self-improving scoring for the System Performance Dashboard.

The plan-based scorecard uses fixed baseline->target constants. Those do not
reflect how the machine actually behaves during a normal work day. This module
learns each metric's personal history and ratchets the goalposts:

  * personal best   - the best daily-typical value you have ever achieved
                      (min for lower-is-better metrics, max for higher-is-better).
  * adaptive target - a small STRETCH beyond your personal best, so there is
                      always a little headroom to improve. It only ever moves in
                      the improving direction (never regresses), so the bar
                      self-tightens as your habits improve.
  * adaptive baseline - the worst daily-typical you have recorded (falls back to
                      the plan baseline until enough data exists).
  * today's typical - the mean of today's samples, so the score reflects your
                      actual day of work rather than a single noisy reading.

State persists to adaptive_state.json so learning survives restarts.
Everything here is read-only with respect to the OS; it only reads samples the
collector already gathered and writes its own JSON state file.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

# Metrics whose value is driven by day-to-day work and worth adapting.
# (Structural metrics like c_free_gb / startup_apps / hw_faults keep the plan
# scoring; adapting them adds noise, not signal.)
ADAPTIVE_KEYS = {
    # committed_app_mb (2026-09-13 fix): the scorecard criterion was renamed
    # committed_mb -> committed_app_mb on 2026-09-11, but this adaptive key was
    # not updated to match. The mismatch meant the commit row never received an
    # adaptive/self-improvement block (enrich() is keyed on the criterion key),
    # so only 6 of 7 tracked metrics were ever scored and the commit dimension's
    # self-improvement never moved. Renamed to match the criterion key.
    "committed_app_mb": False,   # value: lower is better
    "compression_mb": False,
    "available_mb": True,    # higher is better
    "vmmem_mb": False,
    "browser_procs": False,
    "handles": False,
    "net_remote": False,
}

STRETCH = 0.05          # adaptive target sits 5% beyond personal best
EMA_ALPHA = 0.1         # smoothing for the running "typical" level
MIN_DAY_SAMPLES = 3     # need this many samples in a day before it counts
_LEARN_SAMPLES = 20     # below this, report "learning" instead of a score


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


class AdaptiveScorer:
    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file
        self.lock = threading.Lock()
        self.metrics: dict = {}
        self._load()

    # ------------------------------------------------------------------ io ---
    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text())
            self.metrics = data.get("metrics", {})
        except (OSError, json.JSONDecodeError):
            self.metrics = {}

    def _save(self) -> None:
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"version": 1, "updated": datetime.now(timezone.utc).isoformat(),
                 "metrics": self.metrics}, indent=0))
            tmp.replace(self.state_file)
        except OSError:
            pass

    # -------------------------------------------------------------- update ---
    def _blank(self, hib: bool) -> dict:
        return {"hib": hib, "personal_best": None, "best_date": None,
                "ema_typical": None, "worst": None, "daily": {}, "n": 0}

    def seed(self, history: list[dict]) -> None:
        """Replay persisted history once so learning starts populated.

        On a cold state (no metrics yet) every adaptive key is seeded. When the
        state already exists but is MISSING some keys — e.g. a criterion was
        renamed (committed_mb -> committed_app_mb) or a new adaptive metric was
        added — only the missing keys are back-filled from history so existing
        keys are not double-counted.
        """
        missing = set(ADAPTIVE_KEYS) - set(self.metrics)
        if self.metrics and not missing:
            return
        only = None if not self.metrics else missing
        for s in history:
            self.update(s, persist=False, only=only)
        self._save()

    def update(self, sample: dict, persist: bool = True,
               only: set | None = None) -> None:
        day = (sample.get("_iso") or "")[:10] or _today()
        with self.lock:
            for key, hib in ADAPTIVE_KEYS.items():
                if only is not None and key not in only:
                    continue
                val = sample.get(key)
                if val is None:
                    continue
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    continue
                m = self.metrics.setdefault(key, self._blank(hib))
                m["n"] += 1
                # running typical (EMA)
                m["ema_typical"] = (val if m["ema_typical"] is None
                                    else EMA_ALPHA * val + (1 - EMA_ALPHA) * m["ema_typical"])
                # per-day aggregate
                d = m["daily"].setdefault(day, {"sum": 0.0, "n": 0,
                                                "min": val, "max": val})
                d["sum"] += val
                d["n"] += 1
                d["min"] = min(d["min"], val)
                d["max"] = max(d["max"], val)
                # worst-ever typical (baseline side)
                worst_side = d["max"] if not hib else d["min"]
                if m["worst"] is None:
                    m["worst"] = worst_side
                else:
                    m["worst"] = (max(m["worst"], d["max"]) if not hib
                                  else min(m["worst"], d["min"]))
                # personal best from settled daily typicals
                if d["n"] >= MIN_DAY_SAMPLES:
                    self._recompute_best(key, m)
            # trim daily history to last 60 days
            for m in self.metrics.values():
                if len(m["daily"]) > 60:
                    for k in sorted(m["daily"])[:-60]:
                        m["daily"].pop(k, None)
        if persist:
            self._save()

    def _recompute_best(self, key: str, m: dict) -> None:
        hib = m["hib"]
        best_day, best_val = None, None
        for day, d in m["daily"].items():
            if d["n"] < MIN_DAY_SAMPLES:
                continue
            typ = d["sum"] / d["n"]
            if best_val is None or (typ > best_val if hib else typ < best_val):
                best_val, best_day = typ, day
        if best_val is not None:
            m["personal_best"] = round(best_val, 1)
            m["best_date"] = best_day

    # --------------------------------------------------------------- score ---
    def enrich(self, key: str, current, plan_baseline: float,
               plan_target: float, hib: bool) -> dict | None:
        """Return adaptive fields for one metric, or None if not adaptive."""
        if key not in ADAPTIVE_KEYS or current is None:
            return None
        with self.lock:
            m = self.metrics.get(key)
            if not m or m["personal_best"] is None:
                return {"learning": True, "n": (m or {}).get("n", 0)}
            pb = m["personal_best"]
            baseline = m["worst"] if m["worst"] is not None else plan_baseline
            # stretch goal a little past personal best (always improving)
            target = pb * (1 - STRETCH) if not hib else pb * (1 + STRETCH)
            day = self.metrics[key]["daily"].get(_today())
            today_typ = (day["sum"] / day["n"]) if day and day["n"] else m["ema_typical"]
            # progress along baseline -> target using today's typical
            if abs(target - baseline) < 1e-9:
                prog = 100.0
            else:
                prog = _clamp((today_typ - baseline) / (target - baseline) * 100.0)
            # is today a new personal best?
            improved = bool(day and day["n"] >= MIN_DAY_SAMPLES
                            and m["best_date"] == _today())
            # within 2% of personal best right now?
            at_best = (abs(current - pb) <= max(1.0, abs(pb) * 0.02))
            learning = m["n"] < _LEARN_SAMPLES
            return {
                "learning": learning,
                "personal_best": pb,
                "best_date": m["best_date"],
                "adaptive_baseline": round(baseline, 1),
                "adaptive_target": round(target, 1),
                "today_typical": round(today_typ, 1) if today_typ is not None else None,
                "adaptive_progress": None if learning else round(prog, 1),
                "improved_today": improved,
                "at_personal_best": at_best,
                "n": m["n"],
            }

    def summary(self, rows: list[dict]) -> dict:
        """Aggregate self-improvement view across adaptive rows."""
        progs, at_best, improved, learning = [], 0, 0, 0
        for r in rows:
            a = r.get("adaptive")
            if not a:
                continue
            if a.get("learning") and a.get("adaptive_progress") is None:
                learning += 1
                continue
            if a.get("adaptive_progress") is not None:
                progs.append(a["adaptive_progress"])
            at_best += 1 if a.get("at_personal_best") else 0
            improved += 1 if a.get("improved_today") else 0
        score = round(sum(progs) / len(progs), 1) if progs else None
        return {
            "self_improvement_score": score,
            "metrics_tracked": len(ADAPTIVE_KEYS),
            "metrics_scored": len(progs),
            "at_personal_best": at_best,
            "improved_today": improved,
            "learning": learning,
        }
