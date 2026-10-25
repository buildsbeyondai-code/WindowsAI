"""Full-coverage tests for trends.py (long-term usage-trend store)."""
import json
from datetime import datetime

import sysperf.trends as trends
from sysperf.trends import TrendStore, _day_of, _hour_of, _num, _ols, _ordinal


def _store(tmp_path, name="t.json", **kw):
    return TrendStore(tmp_path / name, **kw)


def _ingest_day(store, day, key="handles", vals=(100, 100, 100),
                status="partial", score=70.0, hour=9):
    """Ingest `vals` samples for one day so the day becomes settled."""
    for i, v in enumerate(vals):
        iso = datetime.fromisoformat(f"{day}T{hour:02d}:{i:02d}:00").astimezone().isoformat()
        store.ingest({"_iso": iso},
                     {"rows": [{"key": key, "scored_typical": v,
                                "status": status}],
                      "overall_improvement": score}, persist=False)


# ------------------------------------------------------------- helpers --------
def test_helpers():
    assert _day_of("2026-09-10T09:00:00+00:00") == "2026-09-10"
    assert len(_day_of(None)) == 10
    assert _hour_of("2026-09-10T09:00:00+00:00").isdigit()
    assert _hour_of("nonsense").isdigit()          # malformed -> local hour
    assert _hour_of(None).isdigit()
    assert _num("12.5") == 12.5 and _num(None) is None and _num("x") is None
    assert _ols([1.0], [2.0]) is None              # too few points
    assert _ols([1.0, 1.0], [2.0, 3.0]) is None    # zero variance in x
    assert _ols([0.0, 1.0], [0.0, 2.0]) == 2.0
    assert _ordinal("2026-09-11") - _ordinal("2026-09-10") == 1


# ------------------------------------------------------------------ io --------
def test_load_bad_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    st = TrendStore(p)
    assert st.days == {} and st.hours == {}


def test_save_oserror_swallowed(tmp_path):
    st = TrendStore(tmp_path / "missing" / "t.json")
    st.days = {"2026-09-10": {"metrics": {}, "score": None}}
    st._save()                                     # must not raise


def test_persist_and_reload(tmp_path):
    st = _store(tmp_path)
    st.ingest({"_iso": "2026-09-10T09:00:00+00:00"},
              {"rows": [{"key": "handles", "scored_typical": 100,
                         "status": "met"}], "overall_improvement": 90.0})
    again = _store(tmp_path)
    assert "2026-09-10" in again.days
    assert again.days["2026-09-10"]["metrics"]["handles"]["met"] == 1
    assert json.loads((tmp_path / "t.json").read_text())["version"] == 1


# -------------------------------------------------------------- ingest --------
def test_ingest_skips_unusable_rows_and_missing_score(tmp_path):
    st = _store(tmp_path)
    day = st.ingest({"_iso": "2026-09-10T09:00:00+00:00"}, {"rows": [
        {"key": None, "scored_typical": 5},            # no key
        {"key": "handles"},                            # no value at all
        {"key": "cpu_pct", "scored_typical": None, "current": 30},  # falls back
    ]}, persist=False)
    assert day == "2026-09-10"
    assert st.days[day]["metrics"]["cpu_pct"]["n"] == 1
    assert "handles" not in st.days[day]["metrics"]
    assert st.days[day]["score"] is None            # no overall_improvement


def test_ingest_no_rows_key(tmp_path):
    st = _store(tmp_path)
    st.ingest({}, {}, persist=False)
    assert next(iter(st.days.values()))["metrics"] == {}


def test_trim_drops_oldest_days(tmp_path):
    st = _store(tmp_path, retain_days=3)
    for d in range(1, 7):
        _ingest_day(st, "2026-09-%02d" % d)
    assert len(st.days) == 3 and min(st.days) == "2026-09-04"


# ------------------------------------------------------------ backfill --------
def test_backfill_from_adaptive(tmp_path):
    st = _store(tmp_path)
    _ingest_day(st, "2026-09-10", vals=(50, 50, 50))       # live data wins
    state = {"metrics": {
        "handles": {"daily": {
            "2026-09-10": {"sum": 999, "n": 3, "min": 999, "max": 999},   # skipped
            "2026-09-09": {"sum": 270000, "n": 3, "min": 90000, "max": 90000},
            "2026-09-08": {"sum": 0, "n": 0, "min": 0, "max": 0},         # empty
        }},
        "available_mb": {"daily": {
            "2026-09-09": {"sum": 39000, "n": 3, "min": 13000, "max": 13000}}},
        "committed_mb": {"daily": {                       # not a criterion key
            "2026-09-09": {"sum": 300, "n": 3, "min": 100, "max": 100}}},
    }}
    added = st.backfill_from_adaptive(state)
    assert added == 2
    assert st.days["2026-09-10"]["metrics"]["handles"]["sum"] == 150
    assert st.days["2026-09-09"]["metrics"]["handles"]["met"] == 1   # 90k <= 95k
    assert st.days["2026-09-09"]["metrics"]["available_mb"]["met"] == 1
    assert "committed_mb" not in st.days["2026-09-09"]["metrics"]
    assert st.backfill_from_adaptive({"metrics": {}}) == 0           # nothing added


def test_backfill_from_file(tmp_path):
    st = _store(tmp_path)
    p = tmp_path / "adaptive.json"
    p.write_text(json.dumps({"metrics": {"net_remote": {"daily": {
        "2026-09-09": {"sum": 90, "n": 3, "min": 30, "max": 30}}}}}))
    assert st.backfill_from_adaptive_file(p) == 1
    assert st.backfill_from_adaptive_file(tmp_path / "nope.json") == 0


# ------------------------------------------------------------ analysis --------
def test_analyze_needs_settled_days(tmp_path):
    st = _store(tmp_path)
    _ingest_day(st, "2026-09-10", vals=(100,))          # 1 sample -> unsettled
    info = st.analyze("handles")
    assert info["settled_days"] == 0 and info["direction"] == "unknown"
    assert st.day_typicals("handles", settled_only=False)[0][1] == 100
    assert st.day_typicals("nosuchkey") == []


def test_analyze_improving_and_degrading(tmp_path):
    st = _store(tmp_path)
    for i, day in enumerate(("2026-09-08", "2026-09-09", "2026-09-10")):
        _ingest_day(st, day, key="handles", vals=(130000 - i * 10000,) * 3)
    info = st.analyze("handles")
    assert info["direction"] == "improving" and info["pct_per_day"] < 0
    assert info["days_to_target"] is None
    assert info["met_days"] == 0 and info["settled_days"] == 3
    assert info["volatility_pct"] > 0 and info["pct_change"] < 0

    st2 = _store(tmp_path, name="b.json")
    for i, day in enumerate(("2026-09-08", "2026-09-09", "2026-09-10")):
        _ingest_day(st2, day, key="handles", vals=(100000 + i * 5000,) * 3)
    bad = st2.analyze("handles")
    assert bad["direction"] == "degrading" and bad["days_to_target"] is None


def test_analyze_degrading_projects_days_to_target(tmp_path):
    st = _store(tmp_path)
    # available_mb is higher-is-better: falling means degrading, and the target
    # (12,000) is still ahead of the last value -> a projection exists.
    for i, day in enumerate(("2026-09-08", "2026-09-09", "2026-09-10")):
        _ingest_day(st, day, key="available_mb", vals=(13000 - i * 200,) * 3,
                    status="met")
    info = st.analyze("available_mb")
    assert info["direction"] == "degrading" and info["days_to_target"] > 0
    assert info["met_days"] == 3


def test_analyze_ceiling_metric_rising_under_cap_is_not_degrading(tmp_path):
    # vmmem_mb is a CEILING criterion (target 10,240 = the cap, which sits ABOVE
    # the 5,627 baseline). A gentle rise while far under the cap must NOT be
    # reported as "degrading" — that contradicts the live scorecard's
    # "under cap and falling == met" verdict. Ample headroom -> stable, no ETA.
    st = _store(tmp_path)
    for i, day in enumerate(("2026-09-08", "2026-09-09", "2026-09-10")):
        _ingest_day(st, day, key="vmmem_mb", vals=(1300 + i * 100,) * 3,
                    status="met")
    info = st.analyze("vmmem_mb")
    assert info["direction"] == "stable"          # not "degrading"
    assert info["days_to_target"] is None          # no phantom "days to cap"


def test_analyze_ceiling_metric_flags_imminent_cap_breach(tmp_path):
    # If the drift really is on track to breach the cap within the analysis
    # window, the ceiling metric SHOULD flag degrading + an ETA to the cap.
    st = _store(tmp_path)
    for i, day in enumerate(("2026-09-08", "2026-09-09", "2026-09-10")):
        _ingest_day(st, day, key="vmmem_mb", vals=(9800 + i * 150,) * 3)
    info = st.analyze("vmmem_mb")
    assert info["direction"] == "degrading"
    assert info["days_to_target"] is not None and info["days_to_target"] >= 0


def test_analyze_ceiling_metric_over_cap_is_degrading(tmp_path):
    # Sitting at/over the cap is unambiguously bad.
    st = _store(tmp_path)
    for day in ("2026-09-08", "2026-09-09", "2026-09-10"):
        _ingest_day(st, day, key="vmmem_mb", vals=(10300, 10300, 10300))
    info = st.analyze("vmmem_mb")
    assert info["direction"] == "degrading"
    assert info["days_to_target"] == 0.0


def test_analyze_ceiling_metric_falling_improves(tmp_path):
    # Under the cap and genuinely falling -> improving (mirrors "falling == good").
    st = _store(tmp_path)
    for i, day in enumerate(("2026-09-08", "2026-09-09", "2026-09-10")):
        _ingest_day(st, day, key="vmmem_mb", vals=(5000 - i * 400,) * 3,
                    status="met")
    info = st.analyze("vmmem_mb")
    assert info["direction"] == "improving"
    assert info["days_to_target"] is None


def test_analyze_ceiling_metric_barely_falling_is_stable(tmp_path):
    # Under the cap and falling, but by less than FLAT_PCT_PER_DAY (0.3%/day) of
    # the mean -> "stable", not "improving". Normal working-set breathing must not
    # be reported as a trend.
    st = _store(tmp_path)
    for i, day in enumerate(("2026-09-08", "2026-09-09", "2026-09-10")):
        _ingest_day(st, day, key="vmmem_mb", vals=(5000 - i,) * 3, status="met")
    info = st.analyze("vmmem_mb")
    assert info["direction"] == "stable"
    assert info["days_to_target"] is None


def test_analyze_flat_zero_mean_is_stable(tmp_path):
    st = _store(tmp_path)
    for day in ("2026-09-08", "2026-09-09", "2026-09-10"):
        _ingest_day(st, day, key="hw_faults", vals=(0, 0, 0), status="met")
    info = st.analyze("hw_faults")
    assert info["direction"] == "stable"
    assert info["volatility_pct"] is None and info["pct_change"] is None


def test_analyze_chronic(tmp_path):
    st = _store(tmp_path)
    for d in range(4, 4 + trends.CHRONIC_DAYS):
        _ingest_day(st, "2026-09-%02d" % d, key="net_remote", vals=(40, 40, 40))
    info = st.analyze("net_remote")
    assert info["chronic"] is True and info["met_days"] == 0


# --------------------------------------------------------- score trend --------
def test_score_trend_stages(tmp_path):
    st = _store(tmp_path)
    assert st.score_trend()["days"] == 0                      # no data at all
    _ingest_day(st, "2026-09-09", score=70.0)
    assert st.score_trend()["direction"] == "unknown"          # <3 days
    for i, day in enumerate(("2026-09-10", "2026-09-11")):
        _ingest_day(st, day, score=75.0 + i * 5)
    assert st.score_trend()["direction"] == "improving"

    flat = _store(tmp_path, name="flat.json")
    for day in ("2026-09-09", "2026-09-10", "2026-09-11"):
        _ingest_day(flat, day, score=80.0)
    assert flat.score_trend()["direction"] == "stable"

    down = _store(tmp_path, name="down.json")
    for i, day in enumerate(("2026-09-09", "2026-09-10", "2026-09-11")):
        _ingest_day(down, day, score=90.0 - i * 5)
    assert down.score_trend()["direction"] == "degrading"


# ---------------------------------------------------------- usage shape -------
def test_hour_profile_and_peaks(tmp_path):
    st = _store(tmp_path)
    assert st.peak_hours("cpu_pct") == []                     # <4 hours known
    for h, val in ((8, 10), (9, 10), (10, 60), (11, 10)):
        _ingest_day(st, "2026-09-10", key="cpu_pct", vals=(val,) * 3, hour=h)
    assert st.peak_hours("cpu_pct") == [10]
    assert st.hour_profile("cpu_pct")[10] == 60
    assert isinstance(st.is_peak_now("cpu_pct"), bool)


def test_peak_hours_zero_mean_and_hib(tmp_path):
    st = _store(tmp_path)
    for h in (8, 9, 10, 11):
        _ingest_day(st, "2026-09-10", key="hw_faults", vals=(0,) * 3, hour=h)
    assert st.peak_hours("hw_faults") == []                   # mean 0 -> no peak
    for h, val in ((8, 15000), (9, 15000), (10, 5000), (11, 15000)):
        _ingest_day(st, "2026-09-10", key="available_mb",
                    vals=(val,) * 3, hour=h)
    # higher-is-better: the LOW hour is the busy (peak) one
    assert st.peak_hours("available_mb") == [10]


def test_regime(tmp_path):
    st = _store(tmp_path)
    assert st.regime({})["regime"] == "unknown"
    for day in ("2026-09-08", "2026-09-09", "2026-09-10"):
        _ingest_day(st, day, key="cpu_pct", vals=(20, 20, 20))
    assert st.regime({"cpu_pct": 40})["regime"] == "heavy"
    assert st.regime({"cpu_pct": 10})["regime"] == "light"
    assert st.regime({"cpu_pct": 20})["regime"] == "normal"


def test_summary(tmp_path):
    st = _store(tmp_path)
    for i, day in enumerate(("2026-09-08", "2026-09-09", "2026-09-10")):
        _ingest_day(st, day, key="handles", vals=(130000 - i * 10000,) * 3,
                    score=70.0 + i)
    s = st.summary({"cpu_pct": 30})
    assert s["days_tracked"] == 3 and s["first_day"] == "2026-09-08"
    assert "handles" in s["improving"] and s["chronic"] == []
    assert s["retain_days"] == trends.RETAIN_DAYS
    assert s["score_trend"]["direction"] == "improving"
    assert isinstance(s["is_peak_now"], bool) and s["regime"]["regime"]
