"""Tests for automatic target-realism adjudication (target_review.py).

The system decides whether each goal is realistic from past data and never asks
the user: a goal met on >=1 settled day is realistic (kept); a goal never met
across a settled window is unreachable (auto-revised to the proven-achievable
value). The revision is a reversible runtime override, not a config edit.
"""

from sysperf.target_review import AutoTargetStore, adjudicate


def _trend(key="committed_app_mb", met_days=0, settled=14, target=9000,
           lo=9575.0, hi=15106.0, hib=False, label="Active app commit"):
    return {"key": key, "label": label, "higher_is_better": hib,
            "settled_days": settled, "met_days": met_days, "target": target,
            "min": lo, "max": hi}


# ------------------------------------------------------------- adjudicate -----
def test_realistic_when_met_on_any_settled_day():
    v = adjudicate(_trend(met_days=3))
    assert v["verdict"] == "realistic"
    assert v["honest_target"] == v["target"] == 9000   # unchanged


def test_unreachable_when_never_met_lowers_to_best_observed():
    v = adjudicate(_trend(met_days=0, lo=9575.0))
    assert v["verdict"] == "unreachable"
    assert v["honest_target"] == 9575                  # proven-achievable floor
    assert "never met" in v["rationale"].lower()


def test_unreachable_higher_is_better_uses_best_max():
    # higher-is-better: best achieved is the window max
    v = adjudicate(_trend(key="available_mb", met_days=0, target=12000,
                          lo=7000.0, hi=11840.0, hib=True,
                          label="Available physical"))
    assert v["verdict"] == "unreachable"
    assert v["honest_target"] == 11840


def test_no_decision_without_enough_history():
    assert adjudicate(_trend(settled=3)) is None        # < MIN_SETTLED_DAYS


def test_hard_requirement_metrics_are_never_adjusted():
    for key in ("hw_faults", "cpu_temp_c", "battery_wear_pct", "vmmem_mb"):
        assert adjudicate(_trend(key=key, met_days=0)) is None


# ------------------------------------------------------------- store ----------
class _FakeTrends:
    def __init__(self, by_key):
        self.by_key = by_key
    def analyze(self, key):
        return self.by_key[key]


def test_store_applies_override_only_for_unreachable(tmp_path):
    store = AutoTargetStore(tmp_path / "auto_targets.json")
    trends = _FakeTrends({
        "committed_app_mb": _trend(met_days=0, lo=9575.0),   # unreachable
        "net_remote": _trend(key="net_remote", met_days=14, target=40,
                             lo=23.7, hi=40.4, label="Net"),  # realistic
    })
    store.review(trends, ["committed_app_mb", "net_remote"])
    ovr = store.overrides()
    assert ovr == {"committed_app_mb": 9575}             # only the unreachable one
    assert "net_remote" not in ovr


def test_store_persists_and_reloads(tmp_path):
    f = tmp_path / "auto_targets.json"
    store = AutoTargetStore(f)
    store.review(_FakeTrends({"committed_app_mb": _trend(met_days=0)}),
                 ["committed_app_mb"])
    # a fresh instance reads the same decision back
    again = AutoTargetStore(f)
    assert again.overrides() == {"committed_app_mb": 9575}
    assert any(d["verdict"] == "unreachable" for d in again.decisions())


def test_store_drops_a_stale_decision_when_data_recovers(tmp_path):
    f = tmp_path / "auto_targets.json"
    store = AutoTargetStore(f)
    store.review(_FakeTrends({"committed_app_mb": _trend(met_days=0)}),
                 ["committed_app_mb"])
    assert store.overrides()                              # unreachable recorded
    # metric now meets its goal sometimes -> override withdrawn
    store.review(_FakeTrends({"committed_app_mb": _trend(met_days=5)}),
                 ["committed_app_mb"])
    assert store.overrides() == {}


def test_build_scorecard_honours_an_override():
    import sysperf.scoring as scoring
    from sysperf.config import CRITERIA
    key = next(c[0] for c in CRITERIA)                   # any real metric
    latest = {key: 10.0}
    sc = scoring.build_scorecard(latest, [], _NullScorer(),
                                 target_overrides={key: 1234})
    row = next(r for r in sc["rows"] if r["key"] == key)
    assert row["target"] == 1234 and row["target_source"] == "auto"
    assert row["plan_target"] == next(c for c in CRITERIA if c[0] == key)[3]


class _NullScorer:
    def enrich(self, *a, **k):
        return None
    def summary(self, rows):
        return {}


# ------------------------------------------------------- remaining branches --
def test_missing_inputs_give_no_decision():
    assert adjudicate(_trend(met_days=None)) is None
    assert adjudicate({**_trend(met_days=0), "target": None}) is None
    assert adjudicate({**_trend(met_days=0), "min": None}) is None       # no best value


def test_review_drops_stale_updates_changed_and_keeps_unchanged(tmp_path):
    store = AutoTargetStore(tmp_path / "auto_targets.json")
    trends = _FakeTrends({"committed_app_mb": _trend(met_days=0, lo=9575.0),
                          "net_remote": _trend(key="net_remote", met_days=5, target=40)})
    store.review(trends, ["committed_app_mb", "net_remote"])
    assert store.adjudicated_keys() == {"committed_app_mb", "net_remote"}
    first = {d["key"]: d["decided_at"] for d in store.decisions()}
    store.review(trends, ["committed_app_mb"])                 # unchanged: keeps decided_at
    assert {d["key"]: d["decided_at"] for d in store.decisions()} == first
    trends.by_key["committed_app_mb"] = _trend(met_days=0, lo=9200.0)
    store.review(trends, ["committed_app_mb"])                 # honest target moved
    assert store.overrides()["committed_app_mb"] == 9200
    trends.by_key["net_remote"] = _trend(key="net_remote", settled=2)
    store.review(trends, ["net_remote"])                       # no longer decidable
    assert "net_remote" not in store.adjudicated_keys()
    trends.by_key["cpu_pct"] = _trend(key="cpu_pct", settled=1)
    assert store.review(trends, ["cpu_pct"]) == []             # nothing to drop either
