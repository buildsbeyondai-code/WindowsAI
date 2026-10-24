"""Full-coverage tests for adaptive.py."""
import json

import sysperf.adaptive as adaptive
from sysperf.adaptive import AdaptiveScorer, _today, _clamp


def _scorer(tmp_path, name="state.json"):
    return AdaptiveScorer(tmp_path / name)


def test_helpers():
    assert len(_today()) == 10
    assert _clamp(-5) == 0.0 and _clamp(150) == 100.0 and _clamp(50) == 50.0


def test_load_reads_persisted_metrics(tmp_path):
    """The success branch of _load().

    Previously covered only by accident: the repo happens to have a live
    adaptive_state.json next to the modules, so a test constructing the scorer
    against the real path exercised it. An exported tree has no such file, so the
    line went uncovered there. Writing the fixture makes the coverage real.
    """
    p = tmp_path / "adaptive.json"
    p.write_text(json.dumps({"metrics": {"handles": {"best": 91000, "n": 7}}}),
                 encoding="utf-8")
    scorer = AdaptiveScorer(p)
    assert scorer.metrics == {"handles": {"best": 91000, "n": 7}}


def test_load_ignores_a_file_without_a_metrics_key(tmp_path):
    p = tmp_path / "adaptive.json"
    p.write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert AdaptiveScorer(p).metrics == {}


def test_load_bad_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    sc = AdaptiveScorer(p)
    assert sc.metrics == {}


def test_save_oserror_is_swallowed(tmp_path):
    # parent dir does not exist -> write_text raises OSError -> swallowed
    sc = AdaptiveScorer(tmp_path / "missing_dir" / "state.json")
    sc.metrics = {"committed_app_mb": sc._blank(False)}
    sc._save()          # must not raise


def test_update_skips_none_and_nonfloat(tmp_path):
    sc = _scorer(tmp_path)
    sc.update({"committed_app_mb": None, "available_mb": "abc", "_iso": "2026-09-13T00:00:00"})
    # both skipped -> committed present but n counts only valid; available skipped
    assert "available_mb" not in sc.metrics or sc.metrics["available_mb"]["n"] == 0


def test_update_only_subset(tmp_path):
    sc = _scorer(tmp_path)
    sc.update({"committed_app_mb": 8000, "available_mb": 12000,
               "_iso": "2026-09-13T00:00:00"}, only={"available_mb"})
    assert "available_mb" in sc.metrics
    assert "committed_app_mb" not in sc.metrics


def test_update_builds_best_and_worst(tmp_path):
    sc = _scorer(tmp_path)
    for i in range(4):
        sc.update({"available_mb": 12000 + i, "_iso": "2026-09-13T00:00:0%d" % i})
    m = sc.metrics["available_mb"]
    assert m["personal_best"] is not None      # hib=True best computed
    assert m["worst"] is not None
    # second day updates worst branch (else-branch of worst update)
    sc.update({"available_mb": 5000, "_iso": "2026-09-14T00:00:00"})
    assert sc.metrics["available_mb"]["worst"] == 5000


def test_update_trims_to_60_days(tmp_path):
    sc = _scorer(tmp_path)
    for d in range(1, 66):
        sc.update({"committed_app_mb": 8000 + d,
                   "_iso": "2026-07-%02dT00:00:00" % d}, persist=False)
    assert len(sc.metrics["committed_app_mb"]["daily"]) <= 60


def test_update_default_day_when_no_iso(tmp_path):
    sc = _scorer(tmp_path)
    sc.update({"committed_app_mb": 8000})       # no _iso -> uses _today()
    assert _today() in sc.metrics["committed_app_mb"]["daily"]


def test_recompute_best_skips_thin_days(tmp_path):
    sc = _scorer(tmp_path)
    m = sc._blank(False)
    m["daily"] = {"2026-09-10": {"sum": 100, "n": 1, "min": 100, "max": 100},   # thin, skipped
                  "2026-09-11": {"sum": 900, "n": 3, "min": 290, "max": 310}}
    sc.metrics["committed_app_mb"] = m
    sc._recompute_best("committed_app_mb", m)
    assert m["best_date"] == "2026-09-11"


def test_seed_cold_then_backfill(tmp_path):
    # samples carry EVERY adaptive key so a cold seed fully populates state
    keys = ("committed_app_mb", "compression_mb", "available_mb", "vmmem_mb",
            "browser_procs", "handles", "net_remote")
    hist = [{**{k: 100 + i for k in keys}, "_iso": "2026-09-13T00:00:0%d" % i}
            for i in range(4)]
    sc = _scorer(tmp_path)
    sc.seed(hist)                               # cold -> seeds all keys
    n_before = sc.metrics["available_mb"]["n"]
    # simulate a renamed/added key: drop one, re-seed should backfill only it
    del sc.metrics["available_mb"]
    sc.seed(hist)
    assert sc.metrics["available_mb"]["n"] == n_before      # not double counted
    # fully populated (no missing keys) -> early return, state unchanged
    before_n = sc.metrics["committed_app_mb"]["n"]
    sc.seed(hist)
    assert sc.metrics["committed_app_mb"]["n"] == before_n


# ------------------------------------------------------------------- enrich ---
def test_enrich_not_adaptive_key(tmp_path):
    assert _scorer(tmp_path).enrich("c_free_gb", 1, 1, 1, False) is None


def test_enrich_current_none(tmp_path):
    assert _scorer(tmp_path).enrich("available_mb", None, 1, 1, True) is None


def test_enrich_learning_when_no_state(tmp_path):
    r = _scorer(tmp_path).enrich("available_mb", 12000, 7000, 12000, True)
    assert r["learning"] is True and r["n"] == 0


def test_enrich_scored(tmp_path):
    sc = _scorer(tmp_path)
    today = _today()
    sc.metrics["available_mb"] = {
        "hib": True, "personal_best": 15000, "best_date": today,
        "ema_typical": 14000, "worst": 7000,
        "daily": {today: {"sum": 42000, "n": 3, "min": 13000, "max": 15000}},
        "n": 25}
    r = sc.enrich("available_mb", 15000, 7013, 12000, True)
    assert r["learning"] is False
    assert r["adaptive_progress"] is not None
    assert r["improved_today"] is True
    assert r["at_personal_best"] is True


def test_enrich_target_equals_baseline(tmp_path):
    sc = _scorer(tmp_path)
    today = _today()
    # worst == pb*(1+STRETCH) -> target==baseline -> prog forced to 100
    pb = 15000
    sc.metrics["available_mb"] = {
        "hib": True, "personal_best": pb, "best_date": today,
        "ema_typical": pb, "worst": pb * (1 + adaptive.STRETCH),
        "daily": {today: {"sum": pb * 3, "n": 3, "min": pb, "max": pb}}, "n": 30}
    r = sc.enrich("available_mb", pb, 7013, 12000, True)
    assert r["adaptive_progress"] == 100.0


def test_enrich_learning_flag_with_pb(tmp_path):
    sc = _scorer(tmp_path)
    today = _today()
    sc.metrics["committed_app_mb"] = {
        "hib": False, "personal_best": 7000, "best_date": today,
        "ema_typical": 7500, "worst": 14000,
        "daily": {today: {"sum": 22500, "n": 3, "min": 7000, "max": 8000}}, "n": 5}
    r = sc.enrich("committed_app_mb", 7500, 14000, 9000, False)
    assert r["learning"] is True and r["adaptive_progress"] is None


def test_enrich_today_typical_falls_back_to_ema(tmp_path):
    sc = _scorer(tmp_path)
    # personal best set on an OLD day; today has no daily entry -> uses ema_typical
    sc.metrics["committed_app_mb"] = {
        "hib": False, "personal_best": 7000, "best_date": "2026-01-01",
        "ema_typical": 7800, "worst": 14000,
        "daily": {"2026-01-01": {"sum": 21000, "n": 3, "min": 7000, "max": 7000}}, "n": 30}
    r = sc.enrich("committed_app_mb", 7800, 14000, 9000, False)
    assert r["today_typical"] == 7800.0
    assert r["improved_today"] is False


# ------------------------------------------------------------------ summary ---
def test_summary_mixed():
    sc = AdaptiveScorer.__new__(AdaptiveScorer)   # no init needed for summary
    rows = [
        {"adaptive": {"learning": True, "adaptive_progress": None}},
        {"adaptive": {"learning": False, "adaptive_progress": 90.0,
                      "at_personal_best": True, "improved_today": True}},
        {"adaptive": {"learning": False, "adaptive_progress": 70.0,
                      "at_personal_best": False, "improved_today": False}},
        {"no_adaptive": 1},
    ]
    s = sc.summary(rows)
    assert s["self_improvement_score"] == 80.0
    assert s["learning"] == 1 and s["at_personal_best"] == 1 and s["improved_today"] == 1
    assert s["metrics_scored"] == 2


def test_summary_empty_score_none():
    sc = AdaptiveScorer.__new__(AdaptiveScorer)
    assert sc.summary([])["self_improvement_score"] is None
