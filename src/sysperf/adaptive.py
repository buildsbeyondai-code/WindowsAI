"""
Adaptive, self-improving scoring.

A fixed baseline -> target scorecard answers "how far through the plan am I?".
It cannot answer "is today better than my normal?", which is the question that
actually changes behaviour. This module learns each metric's own history and
ratchets the goalposts:

* **personal best** — the best *daily typical* value ever recorded (minimum for
  lower-is-better metrics, maximum for higher-is-better ones);
* **adaptive target** — a small stretch beyond that best, so there is always
  headroom. It only ever moves in the improving direction, so the bar tightens
  as habits improve and never rewards a regression;
* **adaptive baseline** — the worst daily typical recorded, falling back to the
  plan baseline until enough data exists;
* **today's typical** — the mean of today's samples, so the score reflects a day
  of real work rather than one noisy reading.

State persists to ``adaptive_state.json``. Read-only with respect to the OS: it
only reads samples the collector already gathered.
"""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path

# Metrics driven by day-to-day behaviour and therefore worth adapting.
# Structural metrics (disk free, startup entries, hardware faults) keep the
# derived plan scoring — adapting them adds noise, not signal.
ADAPTIVE_KEYS: dict[str, bool] = {
    "committed_mb": False,      # False = lower is better
    "compression_mb": False,
    "available_mb": True,       # True  = higher is better
    "vmmem_mb": False,
    "browser_procs": False,
    "handles": False,
    "net_remote": False,
}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


class AdaptiveScorer:
    def __init__(self, state_file: Path, cfg: dict | None = None) -> None:
        acfg = (cfg or {}).get("adaptive") or {}
        self.state_file = Path(state_file)
        self.enabled = bool(acfg.get("enabled", True))
        self.stretch = float(acfg.get("stretch", 0.05))
        self.alpha = float(acfg.get("ema_alpha", 0.1))
        self.min_day_samples = int(acfg.get("min_day_samples", 3))
        self.learn_samples = int(acfg.get("learn_samples", 20))
        self.lock = threading.Lock()
        self.metrics: dict = {}
        self._load()

    # ------------------------------------------------------------------ io ---
    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.metrics = data.get("metrics", {}) if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            self.metrics = {}

    def _save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "version": 1,
                "updated": datetime.now(timezone.utc).isoformat(),
                "metrics": self.metrics,
            }), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError:
            pass

    # -------------------------------------------------------------- update ---
    @staticmethod
    def _blank(hib: bool) -> dict:
        return {"hib": hib, "personal_best": None, "best_date": None,
                "ema_typical": None, "worst": None, "daily": {}, "n": 0}

    def seed(self, history: list[dict]) -> None:
        """Replay persisted history once so learning starts populated."""
        if not self.enabled or self.metrics:
            return
        for sample in history:
            self.update(sample, persist=False)
        self._save()

    def update(self, sample: dict, persist: bool = True) -> None:
        if not self.enabled:
            return
        day = (sample.get("_iso") or "")[:10] or _today()
        with self.lock:
            for key, hib in ADAPTIVE_KEYS.items():
                raw = sample.get(key)
                if raw is None:
                    continue
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(val):
                    continue
                m = self.metrics.setdefault(key, self._blank(hib))
                m["n"] += 1
                m["ema_typical"] = (val if m["ema_typical"] is None
                                    else self.alpha * val + (1 - self.alpha) * m["ema_typical"])
                d = m["daily"].setdefault(day, {"sum": 0.0, "n": 0, "min": val, "max": val})
                d["sum"] += val
                d["n"] += 1
                d["min"] = min(d["min"], val)
                d["max"] = max(d["max"], val)
                # Worst-ever typical forms the adaptive baseline.
                if m["worst"] is None:
                    m["worst"] = d["max"] if not hib else d["min"]
                else:
                    m["worst"] = (max(m["worst"], d["max"]) if not hib
                                  else min(m["worst"], d["min"]))
                if d["n"] >= self.min_day_samples:
                    self._recompute_best(m)
            for m in self.metrics.values():     # keep 60 days of dailies
                if len(m["daily"]) > 60:
                    for stale in sorted(m["daily"])[:-60]:
                        m["daily"].pop(stale, None)
        if persist:
            self._save()

    def _recompute_best(self, m: dict) -> None:
        hib = m["hib"]
        best_day, best_val = None, None
        for day, d in m["daily"].items():
            if d["n"] < self.min_day_samples:
                continue
            typical = d["sum"] / d["n"]
            if best_val is None or (typical > best_val if hib else typical < best_val):
                best_val, best_day = typical, day
        if best_val is not None and (m["personal_best"] is None or
                                     (best_val > m["personal_best"] if hib else best_val < m["personal_best"])):
            m["personal_best"] = round(best_val, 1)
            m["best_date"] = best_day

    # --------------------------------------------------------------- score ---
    def enrich(self, key: str, current, plan_baseline: float, plan_target: float,
               hib: bool) -> dict | None:
        """Adaptive fields for one metric, or None when it is not adaptive."""
        if not self.enabled or key not in ADAPTIVE_KEYS or current is None:
            return None
        with self.lock:
            m = self.metrics.get(key)
            if not m or m["personal_best"] is None:
                return {"learning": True, "n": (m or {}).get("n", 0)}
            best = m["personal_best"]
            baseline = m["worst"] if m["worst"] is not None else plan_baseline
            target = best * (1 + self.stretch) if hib else best * (1 - self.stretch)
            day = m["daily"].get(_today())
            today_typical = (day["sum"] / day["n"]) if day and day["n"] else m["ema_typical"]
            if abs(target - baseline) < 1e-9 or today_typical is None:
                prog = 100.0
            else:
                prog = _clamp((today_typical - baseline) / (target - baseline) * 100.0)
            improved = bool(day and day["n"] >= self.min_day_samples
                            and m["best_date"] == _today())
            at_best = abs(float(current) - best) <= max(1.0, abs(best) * 0.02)
            learning = m["n"] < self.learn_samples
            return {
                "learning": learning,
                "personal_best": best,
                "best_date": m["best_date"],
                "adaptive_baseline": round(baseline, 1),
                "adaptive_target": round(target, 1),
                "today_typical": round(today_typical, 1) if today_typical is not None else None,
                "adaptive_progress": None if learning else round(prog, 1),
                "improved_today": improved,
                "at_personal_best": at_best,
                "n": m["n"],
            }

    def summary(self, rows: list[dict]) -> dict:
        """Aggregate self-improvement view across the adaptive rows."""
        progs: list[float] = []
        at_best = improved = learning = 0
        for row in rows:
            a = row.get("adaptive")
            if not a:
                continue
            if a.get("learning") and a.get("adaptive_progress") is None:
                learning += 1
                continue
            if a.get("adaptive_progress") is not None:
                progs.append(a["adaptive_progress"])
            at_best += 1 if a.get("at_personal_best") else 0
            improved += 1 if a.get("improved_today") else 0
        return {
            "enabled": self.enabled,
            "self_improvement_score": round(sum(progs) / len(progs), 1) if progs else None,
            "metrics_tracked": len(ADAPTIVE_KEYS),
            "metrics_scored": len(progs),
            "at_personal_best": at_best,
            "improved_today": improved,
            "learning": learning,
        }
