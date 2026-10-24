"""Full-coverage tests for rca.py (root-cause analysis)."""
import sysperf.rca as rca
from sysperf.rca import _evidence, _pct_off, _summary, _worse_than, analyze, classify


class FakeStore:
    """Minimal trends.TrendStore stand-in with scripted long-term answers."""

    def __init__(self, trends=None, regime="normal", load=1.0,
                 score_direction="stable", score_slope=0.0, score_days=10):
        self._trends = trends or {}
        self._regime = regime
        self._load = load
        self._sd, self._ss, self._sdays = score_direction, score_slope, score_days

    def analyze(self, key, target=None, window_days=14):
        base = {"key": key, "label": key, "higher_is_better": False,
                "window_days": 14, "settled_days": 10, "met_days": 5,
                "target": target, "samples": 100, "mean": 100.0, "first": 100.0,
                "last": 100.0, "min": 90.0, "max": 110.0, "slope_per_day": 0.0,
                "pct_per_day": 0.0, "direction": "stable", "volatility_pct": 5.0,
                "chronic": False, "days_to_target": None, "pct_change": 0.0}
        base.update(self._trends.get(key, {}))
        return base

    def score_trend(self, window_days=14):
        return {"days": self._sdays, "mean": 75.0, "last": 74.0, "first": 76.0,
                "slope_per_day": self._ss, "direction": self._sd,
                "window_days": 14}

    def regime(self, latest):
        return {"regime": self._regime, "ratios": {}, "load_index": self._load}

    def is_peak_now(self, key="cpu_pct"):
        return False


def _row(key="handles", status="partial", current=120000, scored=120000,
         target=95000, progress=40.0, hib=False, label=None):
    return {"key": key, "label": label or key, "status": status,
            "current": current, "scored_typical": scored, "target": target,
            "progress": progress, "shortfall": abs(scored - target),
            "higher_is_better": hib, "baseline": 130000}


def _snapshot(rows, latest=None):
    return {"latest": latest or {}, "scorecard": {
        "rows": rows, "overall_improvement": 60.0,
        "counts": {"met": 0, "partial": len(rows), "regress": 0, "measuring": 0}}}


# ------------------------------------------------------------- helpers --------
def test_helpers():
    assert _pct_off(120, 100) == 20.0
    assert _pct_off(3, 0) == 300.0                 # zero target (hw_faults)
    assert _worse_than(5, 10, hib=True) is True
    assert _worse_than(15, 10, hib=False) is True


def test_evidence_shapes():
    ev = _evidence("handles", {"handles_system": 84000, "processes": 300})
    assert {e["key"] for e in ev} == {"handles_system", "processes"}
    dict_ev = _evidence("vmmem_mb", {"wsl": {"cap_active": True, "load1": 0.5,
                                             "nested": {"x": 1}}})
    assert "cap_active=True" in dict_ev[0]["value"]
    assert _evidence("nosuchkey", {}) == []


# ------------------------------------------------------------ classify --------
def test_classify_chronic():
    store = FakeStore({"handles": {"chronic": True, "settled_days": 7}})
    cause, conf, why = classify(_row(), store.analyze("handles"),
                                {"regime": "normal", "load_index": 1.0})
    assert cause == "chronic_unreachable" and conf > 0.8 and "never met" in why


def test_classify_chronic_higher_is_better_quotes_max():
    store = FakeStore({"available_mb": {"chronic": True, "settled_days": 6}})
    cause, _, why = classify(
        _row("available_mb", current=9000, scored=9000, target=12000, hib=True),
        store.analyze("available_mb"), {"regime": "normal", "load_index": 1.0})
    assert cause == "chronic_unreachable" and "110.0" in why


def test_classify_near_target():
    cause, _, why = classify(_row(scored=96000), FakeStore().analyze("handles"),
                             {"regime": "normal", "load_index": 1.0})
    assert cause == "near_target" and "no action" in why


def test_classify_long_term_regression_with_and_without_eta():
    t = FakeStore({"handles": {"direction": "degrading", "pct_per_day": 1.4,
                               "days_to_target": None}}).analyze("handles")
    cause, _, why = classify(_row(), t, {"regime": "normal", "load_index": 1.0})
    assert cause == "long_term_regression" and "drifting away" in why
    t["days_to_target"] = 9.0
    _, _, why2 = classify(_row(), t, {"regime": "normal", "load_index": 1.0})
    assert "projection" in why2


def test_classify_transient_spike_both_directions():
    low = FakeStore({"handles": {"last": 90000, "direction": "improving"}})
    cause, _, why = classify(_row(scored=130000), low.analyze("handles"),
                             {"regime": "light", "load_index": 0.7})
    assert cause == "transient_spike" and "burst" in why

    hib = FakeStore({"available_mb": {"last": 13000, "direction": "stable"}})
    cause2, _, _ = classify(
        _row("available_mb", current=8000, scored=8000, target=12000, hib=True),
        hib.analyze("available_mb"), {"regime": "light", "load_index": 0.7})
    assert cause2 == "transient_spike"


def test_classify_usage_driven_and_steady():
    row = _row(scored=105000)
    t = FakeStore({"handles": {"last": 104000}}).analyze("handles")
    cause, _, why = classify(row, t, {"regime": "heavy", "load_index": 1.4})
    assert cause == "usage_driven" and "workload" in why
    cause2, _, why2 = classify(row, t, {"regime": "normal", "load_index": 1.0})
    assert cause2 == "steady_shortfall" and "structural change" in why2


def test_classify_uses_current_when_typical_missing():
    row = _row(current=120000)
    row["scored_typical"] = None
    t = FakeStore({"handles": {"last": None}}).analyze("handles")
    cause, _, _ = classify(row, t, {"regime": "normal", "load_index": 1.0})
    assert cause == "steady_shortfall"


# ------------------------------------------------------------- analyze --------
def test_analyze_breach_ranks_and_maps_remediation():
    rows = [_row("handles", progress=30.0, label="Active app handles"),
            _row("net_remote", status="regress", current=90, scored=90,
                 target=30, progress=10.0, label="Network - remote TCP conns"),
            _row("c_free_gb", status="met", progress=100.0, label="C: free"),
            {"key": "vmmem_mb", "label": "vmmemWSL", "status": "measuring",
             "current": 5000, "scored_typical": 5000, "target": 10240,
             "progress": None, "higher_is_better": False}]
    snap = _snapshot(rows, {"handles_system": 84000})
    rep = analyze(snap, FakeStore(), threshold=80.0)
    assert rep["breach"] is True
    assert [f["key"] for f in rep["findings"]] == ["net_remote", "handles"]
    assert rep["findings"][0]["contribution_pct"] > rep["findings"][1]["contribution_pct"]
    assert rep["findings"][0]["remediations"] == ["flush_dns"]
    assert rep["actionable_count"] == 2
    assert "dominated by" in rep["summary"]


def test_analyze_filters_remediation_by_fix_engine():
    class Engine:
        def detect(self, snap):
            return [{"id": "clear_temp"}]

    rows = [_row("c_free_gb", current=150, scored=150, target=180,
                 progress=20.0, hib=True)]
    rep = analyze(_snapshot(rows), FakeStore(), Engine())
    assert rep["findings"][0]["remediations"] == ["clear_temp"]


def test_analyze_preventive_and_proposals():
    store = FakeStore({
        "handles": {"chronic": True, "settled_days": 8, "min": 100000},
        "c_free_gb": {"direction": "degrading", "pct_per_day": -0.9,
                      "days_to_target": 4.0},
        "startup_apps": {"direction": "improving"},
    })
    rows = [_row("handles", progress=20.0),
            _row("c_free_gb", status="met", current=181, scored=181,
                 target=180, progress=100.0, hib=True),
            _row("startup_apps", status="met", current=10, scored=10,
                 target=10, progress=100.0)]
    rep = analyze(_snapshot(rows), store)
    assert [p["kind"] for p in rep["proposals"]] == ["retune_target"]
    assert [p["key"] for p in rep["preventive"]] == ["c_free_gb"]
    assert rep["findings"][0]["actionable"] is False       # chronic -> no chasing


def test_analyze_no_score():
    snap = {"latest": {}, "scorecard": {"rows": [], "overall_improvement": None}}
    rep = analyze(snap, FakeStore())
    assert rep["breach"] is False and "No score yet" in rep["summary"]


def test_summary_variants():
    store = FakeStore(score_direction="degrading", score_slope=-0.8)
    rows = [_row("startup_apps", status="met", current=10, scored=10,
                 target=10, progress=100.0)]
    rep = analyze(_snapshot(rows), store)
    assert "every scored criterion is met" in rep["summary"]
    assert "long-term score trend is degrading" in rep["summary"]

    prev = _summary(95.0, 80.0, False, [], store.score_trend(),
                    {"regime": "normal"},
                    [{"key": "handles", "label": "h", "reason": "r"}])
    assert "drifting the wrong way" in prev
    assert "meets the 80% threshold" in _summary(
        95.0, 80.0, False, [], FakeStore().score_trend(), {}, [])


def test_no_action_causes_are_declared():
    assert "chronic_unreachable" in rca.NO_ACTION_CAUSES
    assert "near_target" in rca.NO_ACTION_CAUSES
