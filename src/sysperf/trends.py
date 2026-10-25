"""
Long-term usage-trend store for the System Performance Dashboard.

adaptive.py learns a *personal best* per metric and the collector keeps only
MAX_SAMPLES (~4 h) of raw samples, so neither can answer "is this machine
getting slower over weeks, and when is it actually worked hard?". This module
owns that horizon:

  * daily rollups   - per criterion: sum/n/min/max plus how often the criterion
                      was met, retained for RETAIN_DAYS days;
  * hourly profile  - per criterion: mean by local hour-of-day, i.e. the real
                      *usage pattern* (which hours are heavy);
  * score rollup    - the overall scorecard percentage per day, so the score
                      itself can be trended;
  * analysis        - least-squares slope over daily typicals, direction
                      (improving / stable / degrading), volatility, chronic-miss
                      detection, and a projection of when a degrading metric
                      will cross its target.

All input comes from snapshots the collector already produced; the only side
effect is this module's own JSON state file (atomic replace).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import CRITERIA

# criterion metadata, mirrored from config so callers need only a metric key
HIB = {c[0]: c[4] for c in CRITERIA}
TARGETS = {c[0]: c[3] for c in CRITERIA}
BASELINES = {c[0]: c[2] for c in CRITERIA}


def refresh_criteria() -> None:
    """Re-read CRITERIA after this machine's baseline is captured (in place)."""
    TARGETS.clear()
    TARGETS.update({c[0]: c[3] for c in CRITERIA})
    BASELINES.clear()
    BASELINES.update({c[0]: c[2] for c in CRITERIA})
LABELS = {c[0]: c[1] for c in CRITERIA}

RETAIN_DAYS = 180        # keep ~6 months of daily rollups
MIN_DAY_SAMPLES = 3      # a day is "settled" once it has this many samples
MIN_TREND_DAYS = 3       # need this many settled days before a slope is trusted
TREND_WINDOW_DAYS = 14   # regression window for the slope
FLAT_PCT_PER_DAY = 0.3   # |slope| below this (% of mean per day) == stable
CHRONIC_DAYS = 5         # never met across this many settled days == chronic
PEAK_FACTOR = 1.15       # an hour is "peak" at this multiple of the daily mean
# usage-regime thresholds, as a ratio of the metric's long-term mean
HEAVY_RATIO = 1.25
LIGHT_RATIO = 0.75
# metrics that describe how hard the machine is being used right now
REGIME_KEYS = ("cpu_pct", "committed_app_mb", "handles", "browser_procs")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day_of(iso: str | None) -> str:
    return (iso or "")[:10] or _now().strftime("%Y-%m-%d")


def _hour_of(iso: str | None) -> str:
    """Local hour-of-day for an ISO timestamp (samples are stamped in UTC).

    Local time is what makes an hourly usage profile meaningful ("busy at 09:00"),
    so the UTC stamp is converted to the host's zone. Falls back to the current
    local hour if the stamp is missing or malformed.
    """
    try:
        return str(datetime.fromisoformat(iso).astimezone().hour)      # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(datetime.now().hour)


def _agg(bucket: dict, val: float) -> None:
    bucket["sum"] += val
    bucket["n"] += 1
    bucket["min"] = min(bucket["min"], val)
    bucket["max"] = max(bucket["max"], val)


def _blank_agg(val: float) -> dict:
    return {"sum": 0.0, "n": 0, "min": val, "max": val}


def _num(val):
    """Coerce to float, or None when the value is not numeric."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _ols(xs: list[float], ys: list[float]) -> float | None:
    """Least-squares slope of ys over xs, or None if it is not defined."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _ordinal(day: str) -> int:
    """Day string -> ordinal day number, so gaps in the record are real gaps."""
    y, m, d = (int(p) for p in day.split("-"))
    return datetime(y, m, d).toordinal()


class TrendStore:
    """Persistent daily/hourly rollups plus long-term trend analysis."""

    def __init__(self, state_file: Path, retain_days: int = RETAIN_DAYS) -> None:
        self.state_file = Path(state_file)
        self.retain_days = retain_days
        self.lock = threading.Lock()
        self.days: dict = {}
        self.hours: dict = {}
        self._load()

    # ------------------------------------------------------------------ io ---
    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text())
            self.days = data.get("days", {})
            self.hours = data.get("hours", {})
        except (OSError, json.JSONDecodeError):
            self.days, self.hours = {}, {}

    def _save(self) -> None:
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1,
                                       "updated": _now().isoformat(),
                                       "days": self.days,
                                       "hours": self.hours}, indent=0))
            tmp.replace(self.state_file)
        except OSError:
            pass

    # -------------------------------------------------------------- ingest ---
    def ingest(self, latest: dict, scorecard: dict, persist: bool = True) -> str:
        """Fold one snapshot into the daily + hourly rollups. Returns the day."""
        iso = latest.get("_iso")
        day, hour = _day_of(iso), _hour_of(iso)
        rows = scorecard.get("rows") or []
        with self.lock:
            d = self.days.setdefault(day, {"metrics": {}, "score": None})
            for row in rows:
                key = row.get("key")
                val = _num(row.get("scored_typical"))
                if val is None:
                    val = _num(row.get("current"))
                if key is None or val is None:
                    continue
                m = d["metrics"].setdefault(key, dict(_blank_agg(val), met=0))
                _agg(m, val)
                if row.get("status") == "met":
                    m["met"] += 1
                hb = self.hours.setdefault(key, {})
                h = hb.setdefault(hour, {"sum": 0.0, "n": 0})
                h["sum"] += val
                h["n"] += 1
            score = _num(scorecard.get("overall_improvement"))
            if score is not None:
                if d["score"] is None:
                    d["score"] = _blank_agg(score)
                _agg(d["score"], score)
            self._trim()
        if persist:
            self._save()
        return day

    def _trim(self) -> None:
        if len(self.days) > self.retain_days:
            for stale in sorted(self.days)[:-self.retain_days]:
                self.days.pop(stale, None)

    # ------------------------------------------------------------ backfill ---
    def backfill_from_adaptive(self, state: dict, persist: bool = True) -> int:
        """Import adaptive.py's per-day aggregates as long-term history.

        The adaptive scorer already keeps 60 days of per-day sum/n/min/max for
        its tracked metrics, so a fresh trend store can start with weeks of
        history instead of waiting a fortnight to say anything. Only (day,
        metric) pairs this store lacks are filled, so it never double-counts and
        live rollups always win. ``met`` is inferred from the daily typical
        against the criterion target (the adaptive state does not record it).
        """
        added = 0
        for key, m in (state.get("metrics") or {}).items():
            if key not in TARGETS:
                continue          # legacy/non-criterion adaptive key (e.g. the
                                  # raw committed_mb counter) — not scored, so it
                                  # has no target to trend against
            target, hib = TARGETS.get(key), HIB.get(key, False)
            for day, d in (m.get("daily") or {}).items():
                if not d.get("n"):
                    continue
                bucket = self.days.setdefault(
                    day, {"metrics": {}, "score": None})["metrics"]
                if key in bucket:
                    continue
                typ = d["sum"] / d["n"]
                met = int(typ >= target if hib else typ <= target)
                bucket[key] = {"sum": d["sum"], "n": d["n"], "min": d["min"],
                               "max": d["max"], "met": met, "backfilled": True}
                added += 1
        if added:
            self._trim()
            if persist:
                self._save()
        return added

    def backfill_from_adaptive_file(self, path) -> int:
        """backfill_from_adaptive() for an adaptive_state.json path."""
        try:
            return self.backfill_from_adaptive(json.loads(Path(path).read_text()))
        except (OSError, json.JSONDecodeError):
            return 0

    # ------------------------------------------------------------ analysis ---
    def day_typicals(self, key: str, settled_only: bool = True) -> list:
        """[(day, typical)] oldest-first for one metric."""
        out = []
        for day in sorted(self.days):
            m = (self.days[day].get("metrics") or {}).get(key)
            if not m or not m.get("n"):
                continue
            if settled_only and m["n"] < MIN_DAY_SAMPLES:
                continue
            out.append((day, m["sum"] / m["n"]))
        return out

    def analyze(self, key: str, target=None,
                window_days: int = TREND_WINDOW_DAYS) -> dict:
        """Long-term trend for one metric over the last `window_days` days."""
        hib = HIB.get(key, False)
        if target is None:
            target = TARGETS.get(key)
        series = self.day_typicals(key)[-window_days:]
        days = [d for d, _ in series]
        vals = [v for _, v in series]
        settled = len(vals)
        met_days = sum(1 for d in days
                       if (self.days[d]["metrics"][key].get("met") or 0) > 0)
        out = {
            "key": key, "label": LABELS.get(key, key), "higher_is_better": hib,
            "window_days": window_days, "settled_days": settled,
            "met_days": met_days, "target": target,
            "samples": sum(self.days[d]["metrics"][key]["n"] for d in days),
            "mean": round(sum(vals) / settled, 1) if settled else None,
            "first": round(vals[0], 1) if settled else None,
            "last": round(vals[-1], 1) if settled else None,
            "min": round(min(vals), 1) if settled else None,
            "max": round(max(vals), 1) if settled else None,
            "slope_per_day": None, "pct_per_day": None,
            "direction": "unknown", "volatility_pct": None,
            "chronic": bool(settled >= CHRONIC_DAYS and met_days == 0),
            "days_to_target": None, "pct_change": None,
        }
        if settled < MIN_TREND_DAYS:
            return out

        mean = sum(vals) / settled
        xs = [float(_ordinal(d)) for d in days]
        slope = _ols(xs, vals)
        spread = max(vals) - min(vals)
        out["volatility_pct"] = round(spread / abs(mean) * 100.0, 1) if mean else None
        out["pct_change"] = (round((vals[-1] - vals[0]) / abs(vals[0]) * 100.0, 1)
                             if vals[0] else None)
        if slope is None:   # pragma: no cover - unreachable: day_typicals()
            # yields one point per distinct day, so the x values always vary
            out["direction"] = "stable"
            return out

        pct_day = (slope / abs(mean) * 100.0) if mean else 0.0
        out["slope_per_day"] = round(slope, 2)
        out["pct_per_day"] = round(pct_day, 2)

        # A "ceiling" criterion (e.g. vmmemWSL) has its target set to a cap that
        # sits ABOVE the baseline: the goal is to stay UNDER it, not to reach it.
        # The generic baseline->target direction logic is wrong for these — it
        # reads a normal rise toward the cap as "degrading, N days to target",
        # which flatly contradicts the live scorecard's cap-aware verdict
        # (score_vmmem: "under cap and falling" == met). Mirror that verdict here:
        # under the cap with headroom is fine; only call it degrading when the
        # drift would actually breach the cap within the analysis window.
        base = BASELINES.get(key)
        is_ceiling = (not hib) and target is not None and base is not None and target >= base
        if is_ceiling:
            cap = target
            if vals[-1] >= cap:                      # already over the ceiling
                out["direction"] = "degrading"
                out["days_to_target"] = 0.0
            elif slope <= 0:                         # under cap and falling/flat
                out["direction"] = ("improving" if abs(pct_day) >= FLAT_PCT_PER_DAY
                                     else "stable")
            else:                                    # under cap but rising
                days_to_cap = (cap - vals[-1]) / slope
                if 0 < days_to_cap <= window_days:   # on track to breach soon
                    out["direction"] = "degrading"
                    out["days_to_target"] = round(days_to_cap, 1)
                else:                                # ample headroom == not a concern
                    out["direction"] = "stable"
            return out

        better = (slope > 0) if hib else (slope < 0)
        if abs(pct_day) < FLAT_PCT_PER_DAY:
            out["direction"] = "stable"
        else:
            out["direction"] = "improving" if better else "degrading"
        if out["direction"] == "degrading" and target is not None:
            gap = target - vals[-1]
            if slope != 0 and (gap / slope) > 0:
                out["days_to_target"] = round(gap / slope, 1)
        return out

    def score_trend(self, window_days: int = TREND_WINDOW_DAYS) -> dict:
        """Long-term trend of the overall scorecard percentage."""
        series = [(d, self.days[d]["score"]["sum"] / self.days[d]["score"]["n"])
                  for d in sorted(self.days)
                  if self.days[d].get("score") and self.days[d]["score"]["n"]]
        series = series[-window_days:]
        vals = [v for _, v in series]
        out = {"days": len(vals), "mean": None, "last": None, "first": None,
               "slope_per_day": None, "direction": "unknown",
               "window_days": window_days}
        if not vals:
            return out
        out["mean"] = round(sum(vals) / len(vals), 1)
        out["first"], out["last"] = round(vals[0], 1), round(vals[-1], 1)
        if len(vals) < MIN_TREND_DAYS:
            return out
        slope = _ols([float(_ordinal(d)) for d, _ in series], vals)
        if slope is None:   # pragma: no cover - unreachable: one point per day
            out["direction"] = "stable"
            return out
        out["slope_per_day"] = round(slope, 2)
        out["direction"] = ("stable" if abs(slope) < 0.2
                            else "improving" if slope > 0 else "degrading")
        return out

    # --------------------------------------------------------- usage shape ---
    def hour_profile(self, key: str) -> dict:
        """{hour(int): mean value} for one metric, from the hourly rollup."""
        return {int(h): b["sum"] / b["n"]
                for h, b in (self.hours.get(key) or {}).items() if b.get("n")}

    def peak_hours(self, key: str = "cpu_pct") -> list:
        """Local hours where this metric runs materially above its own mean.

        Used to keep user-visible remediation out of the busiest part of the day.
        """
        prof = self.hour_profile(key)
        if len(prof) < 4:
            return []
        mean = sum(prof.values()) / len(prof)
        if not mean:
            return []
        hib = HIB.get(key, False)
        return sorted(h for h, v in prof.items()
                      if ((v <= mean / PEAK_FACTOR) if hib
                          else (v >= mean * PEAK_FACTOR)))

    def is_peak_now(self, key: str = "cpu_pct") -> bool:
        return datetime.now().hour in self.peak_hours(key)

    def regime(self, latest: dict) -> dict:
        """Classify the current usage load against this machine's own norm."""
        ratios = {}
        for key in REGIME_KEYS:
            cur = _num(latest.get(key))
            info = self.analyze(key)
            mean = info.get("mean")
            if cur is None or not mean:
                continue
            ratios[key] = round(cur / mean, 2)
        if not ratios:
            return {"regime": "unknown", "ratios": {}, "load_index": None}
        load = sum(ratios.values()) / len(ratios)
        regime = ("heavy" if load >= HEAVY_RATIO
                  else "light" if load <= LIGHT_RATIO else "normal")
        return {"regime": regime, "ratios": ratios, "load_index": round(load, 2)}

    # ------------------------------------------------------------- summary ---
    def summary(self, latest: dict | None = None) -> dict:
        """API payload: score trend + per-metric trends + usage shape."""
        keys = sorted({k for d in self.days.values()
                       for k in (d.get("metrics") or {})})
        metrics = {k: self.analyze(k) for k in keys}
        return {
            "days_tracked": len(self.days),
            "first_day": min(self.days) if self.days else None,
            "last_day": max(self.days) if self.days else None,
            "score_trend": self.score_trend(),
            "metrics": metrics,
            "degrading": [k for k, m in metrics.items()
                          if m["direction"] == "degrading"],
            "improving": [k for k, m in metrics.items()
                          if m["direction"] == "improving"],
            "chronic": [k for k, m in metrics.items() if m["chronic"]],
            "peak_hours": self.peak_hours(),
            "is_peak_now": self.is_peak_now(),
            "regime": self.regime(latest or {}),
            "retain_days": self.retain_days,
        }
