"""Full-coverage tests for learning.py (the continuous-learning view).

The view is pure derivation, so every test here builds a synthetic loop state and
cycle log and asserts on the shape and the arithmetic. Nothing touches the real
state files, and nothing runs a cycle.
"""
import json

import pytest

import sysperf.learning as learning
from sysperf.learning import (
    CAUSE_GLOSS,
    SUPPRESS_AFTER,
    LearningView,
    _band,
    _day,
    _pct,
    _read_cycles,
)


class FakeFix:
    def __init__(self, fid, title, risk="safe", category="storage",
                 reversible=True):
        self.id = fid
        self.title = title
        self.risk = risk
        self.category = category
        self.reversible = reversible


class FakeFixEngine:
    def __init__(self, fixes=None):
        self._by_id = {f.id: f for f in (fixes or [])}

    def get(self, fix_id):
        return self._by_id.get(fix_id)


class FakeTrends:
    def __init__(self, metrics=None, raises=False):
        self.metrics = metrics or {}
        self.raises = raises

    def summary(self, latest=None):
        if self.raises:
            raise RuntimeError("no daily history")
        return {"metrics": self.metrics}


class FakeImprover:
    def __init__(self, state=None, raises=False):
        self.state = state or {}
        self.raises = raises

    def status(self):
        if self.raises:
            raise RuntimeError("state unreadable")
        return self.state


def _state(**over):
    state = {
        "mode": "auto",
        "cycles": 12,
        "execution_enabled": True,
        "allowed_risks": ["safe"],
        "max_actions_per_cycle": 2,
        "cooldown_s": 1800.0,
        "settle_s": 120.0,
        "ledger": {
            "clear_temp": {"c_free_gb": {"ema": 0.02, "n": 4, "wins": 3,
                                         "losses": 1, "last": 0.01}},
            "flush_dns": {"net_remote": {"ema": -0.0, "n": SUPPRESS_AFTER,
                                         "wins": 0, "losses": 0, "last": 0.0}},
            "mystery_fix": {"handles": {"ema": None, "n": 1, "wins": 0,
                                        "losses": 0, "last": None}},
        },
        "proposals": [{
            "key": "c_free_gb", "kind": "escalate_manual", "seen": 7,
            "detail": "C: free is regressing long-term",
            "first": "2026-09-20T00:00:00+00:00",
            "last": "2026-09-22T00:00:00+00:00",
            "evidence": {"label": "C: free", "direction": "degrading",
                         "pct_per_day": -2.1},
        }],
        "last": {"breach": True},
    }
    state.update(over)
    return state


def _cycle(at, **over):
    rec = {"at": at, "t": 1_000_000.0, "score": 72.5, "breach": True,
           "regime": "normal", "mode": "auto", "summary": "Score under threshold",
           "note": "", "executed": [], "planned": [], "skipped": [],
           "preventive": [], "findings": [], "verification": None,
           "proposals_open": 0, "threshold": 80.0}
    rec.update(over)
    return rec


def _write_log(tmp_path, cycles):
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(c) + "\n" for c in cycles),
                    encoding="utf-8")
    return path


@pytest.fixture
def view(tmp_path):
    """A view over a small but exhaustive synthetic history."""
    cycles = [
        _cycle("2026-09-20T01:00:00+00:00", t=1_000_000.0,
               findings=[{"key": "handles", "label": "Handles",
                          "cause": "transient_spike", "contribution_pct": 10.0}]),
        _cycle("2026-09-20T02:00:00+00:00", t=1_001_000.0,
               executed=[{"id": "clear_temp", "title": "Clear user TEMP folder",
                          "risk": "safe", "status": "ok"}],
               findings=[{"key": "c_free_gb", "label": "C: free",
                          "cause": "long_term_regression",
                          "contribution_pct": 40.0}],
               proposals_open=0),
        _cycle("2026-09-21T02:00:00+00:00", t=1_100_000.0, breach=False,
               verification={"status": "verified", "waited_s": 130.0,
                             "actions": ["clear_temp"],
                             "metrics": {"c_free_gb": {"before": 170,
                                                       "after": 174,
                                                       "delta_pct": 2.35}},
                             "verdicts": {"c_free_gb": "improved"}},
               preventive=[{"key": "handles", "label": "Handles",
                            "reason": "drifting", "remediations": ["x"]}],
               proposals_open=1),
        _cycle("2026-09-21T03:00:00+00:00", t=1_101_000.0,
               skipped=[{"id": "trim_startup", "title": "Trim start-up apps",
                         "risk": "manual", "eligible": False,
                         "skip_reason": "manual action required"}],
               proposals_open=1),
        # No timestamp: must not create a phantom "" day bucket in the timeline.
        _cycle("", t=1_102_000.0, executed=[], verification={"status": "waiting"}),
    ]
    log = _write_log(tmp_path, cycles)
    return LearningView(
        FakeImprover(_state()),
        FakeTrends({
            "c_free_gb": {"key": "c_free_gb", "label": "C: free",
                          "direction": "degrading", "pct_per_day": -2.1,
                          "window_days": 14, "met_days": 3, "chronic": False},
            "net_remote": {"key": "net_remote", "label": "Remote latency",
                           "direction": "stable", "pct_per_day": 0.0,
                           "window_days": 14, "met_days": 14, "chronic": False},
            "cpu_pct": {"key": "cpu_pct", "label": "CPU", "direction": "degrading",
                        "pct_per_day": 5.5, "window_days": 14, "met_days": 0,
                        "chronic": True},
            "bad": "not-a-dict",
            "unkeyed": {"label": "no key"},
        }),
        FakeFixEngine([
            FakeFix("clear_temp", "Clear user TEMP folder"),
            FakeFix("flush_dns", "Flush the DNS cache", category="network"),
        ]),
        log, log_limit=500)


# --------------------------------------------------------------- helpers ---
def test_read_cycles_tolerates_missing_file(tmp_path):
    assert _read_cycles(tmp_path / "nope.jsonl", 10) == []


def test_read_cycles_skips_blank_bad_and_non_dict_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"at":"x"}\n\nnot json\n[1,2]\n', encoding="utf-8")
    assert _read_cycles(path, 10) == [{"at": "x"}]


def test_read_cycles_honours_limit(tmp_path):
    path = _write_log(tmp_path, [_cycle(f"2026-09-2{i}T00:00:00+00:00")
                                 for i in range(5)])
    assert len(_read_cycles(path, 2)) == 2


def test_day_and_pct_and_band_edges():
    assert _day({"at": "2026-09-20T01:00:00+00:00"}) == "2026-09-20"
    assert _day({}) == ""
    assert _pct(None) is None
    assert _pct(0.1234567) == 12.346
    assert [_band(v) for v in (None, 95, 90, 89, 70, 69, 50, 49)] == \
        ["unrated", "90-100", "90-100", "70-89", "70-89", "50-69", "50-69", "<50"]


# ------------------------------------------------------------ strategies ---
def test_strategies_classify_effective_suppressed_and_learning(view):
    rows = view.strategies()
    by_id = {r["id"]: r for r in rows}
    assert by_id["clear_temp"]["status"] == "effective"
    assert by_id["clear_temp"]["title"] == "Clear user TEMP folder"
    assert by_id["clear_temp"]["success_rate"] == 75.0
    assert by_id["clear_temp"]["effect_pct"] == 2.0
    # measured SUPPRESS_AFTER times with no gain -> the loop stops choosing it
    assert by_id["flush_dns"]["status"] == "suppressed"
    assert by_id["flush_dns"]["success_rate"] is None
    # no effect recorded yet, and no catalogue entry -> falls back to the id
    assert by_id["mystery_fix"]["status"] == "learning"
    assert by_id["mystery_fix"]["title"] == "mystery_fix"
    assert by_id["mystery_fix"]["risk"] == "unknown"
    # ordered by attempts, descending
    assert [r["attempts"] for r in rows] == sorted(
        (r["attempts"] for r in rows), reverse=True)


def test_strategies_under_suppress_threshold_still_learning(tmp_path):
    state = _state(ledger={"f": {"k": {"ema": -0.01, "n": SUPPRESS_AFTER - 1,
                                       "wins": 0, "losses": 1}}})
    view = LearningView(FakeImprover(state), FakeTrends(), FakeFixEngine(),
                        _write_log(tmp_path, []))
    assert view.strategies()[0]["status"] == "learning"


def test_strategies_ignore_malformed_ledger_entries(tmp_path):
    state = _state(ledger={"a": "not-a-dict", "b": {"k": "not-a-dict"}})
    view = LearningView(FakeImprover(state), FakeTrends(), FakeFixEngine(),
                        _write_log(tmp_path, []))
    assert view.strategies() == []



def test_bands_cover_every_bucket(view):
    bands = view.bands()
    assert set(bands) == {"90-100", "70-89", "50-69", "<50", "unrated"}
    assert bands["70-89"] == 1          # clear_temp at 75%
    assert bands["unrated"] == 2        # flush_dns + mystery_fix


# -------------------------------------------------------------- activity ---
def test_timeline_buckets_by_day_and_skips_unstamped(view):
    tl = view.timeline()
    assert [d["date"] for d in tl] == ["2026-09-20", "2026-09-21"]
    assert tl[0]["cycles"] == 2 and tl[0]["breaches"] == 2
    assert tl[1]["breaches"] == 1
    assert all(d["count"] > 0 for d in tl)


def test_timeline_empty_without_history(tmp_path):
    view = LearningView(FakeImprover(_state()), FakeTrends(), FakeFixEngine(),
                        _write_log(tmp_path, []))
    assert view.timeline() == []


def test_by_type_counts_every_event_kind(view):
    types = view.by_type()
    assert types["fix_applied"] == 1
    assert types["effect_measured"] == 1        # the "waiting" record is excluded
    assert types["preventive_flagged"] == 1
    assert types["escalated_manual"] == 1
    assert types["rca_run"] == 2
    assert types["proposal_opened"] == 1


def test_proposal_opened_needs_a_prior_observation(tmp_path):
    """A first cycle reporting open proposals is a baseline, not an event."""
    view = LearningView(FakeImprover(_state()), FakeTrends(), FakeFixEngine(),
                        _write_log(tmp_path, [
                            _cycle("2026-09-20T00:00:00+00:00", proposals_open=3),
                            _cycle("2026-09-20T01:00:00+00:00", proposals_open=3),
                        ]))
    assert "proposal_opened" not in view.by_type()


def test_events_ignore_malformed_items(tmp_path):
    view = LearningView(FakeImprover(_state()), FakeTrends(), FakeFixEngine(),
                        _write_log(tmp_path, [_cycle(
                            "2026-09-20T00:00:00+00:00",
                            executed=["nope"], preventive=["nope"],
                            skipped=["nope"], proposals_open="not-an-int")]))
    assert view.by_type() == {}


# ------------------------------------------------------------- learnings ---
def test_learnings_describe_each_substantive_cycle(view):
    rows = view.learnings()
    kinds = [r["kind"] for r in rows]
    assert kinds == ["escalated", "effect_measured", "fix_applied"]
    measured = rows[1]["measured"]
    assert measured["actions"] == ["clear_temp"]
    assert measured["metrics"] == [{"metric": "c_free_gb", "delta_pct": 2.35,
                                    "verdict": "improved"}]
    applied = rows[2]
    assert applied["applied"][0]["title"] == "Clear user TEMP folder"
    assert applied["cause"] == "long_term_regression"
    assert applied["cause_gloss"] == CAUSE_GLOSS["long_term_regression"]
    assert rows[0]["escalated"][0]["reason"] == "manual action required"


def test_learnings_respect_the_limit(view):
    assert len(view.learnings(limit=1)) == 1


def test_learnings_skip_quiet_cycles(tmp_path):
    view = LearningView(FakeImprover(_state()), FakeTrends(), FakeFixEngine(),
                        _write_log(tmp_path, [
                            _cycle("2026-09-20T00:00:00+00:00", breach=False),
                        ]))
    assert view.learnings() == []


def test_measured_tolerates_a_verification_without_metrics():
    flat = LearningView._measured({"status": "verified", "actions": None,
                                   "metrics": "bad", "verdicts": "bad"})
    assert flat == {"actions": [], "waited_s": None, "metrics": []}


def test_learnings_without_findings_have_no_driver(tmp_path):
    view = LearningView(FakeImprover(_state()), FakeTrends(), FakeFixEngine(),
                        _write_log(tmp_path, [_cycle(
                            "2026-09-20T00:00:00+00:00",
                            executed=[{"id": "x"}], findings=["bad"])]))
    row = view.learnings()[0]
    assert row["driver"] == "" and row["cause_gloss"] == ""


# -------------------------------------------------------- knowledge sets ---
def test_knowledge_sets_state_reflects_mapped_strategies(view):
    sets = {k["key"]: k for k in view.knowledge_sets()}
    assert sets["c_free_gb"]["state"] == "learned"      # clear_temp is effective
    assert sets["net_remote"]["state"] == "observed"    # flush_dns is suppressed
    assert sets["cpu_pct"]["state"] == "unmapped"       # no fix maps to it
    assert sets["cpu_pct"]["chronic"] is True
    assert "bad" not in sets and "unkeyed" not in sets
    # most-covered metric first
    assert view.knowledge_sets()[0]["strategy_count"] == 1


def test_a_mapped_but_never_run_metric_is_not_called_unmapped(tmp_path):
    """Regression: "no measured strategy" and "no remediation exists" are
    different facts, and conflating them made coverage look worse than it is."""
    view = LearningView(
        FakeImprover(_state(ledger={})), FakeTrends({
            "startup_apps": {"key": "startup_apps", "label": "Startup programs"},
            "cpu_pct": {"key": "cpu_pct", "label": "CPU"},
        }), FakeFixEngine(), _write_log(tmp_path, []),
        remediation_map={"startup_apps": ["trim_startup"]})
    sets = {k["key"]: k for k in view.knowledge_sets()}
    assert sets["startup_apps"]["state"] == "mapped"
    assert sets["startup_apps"]["mapped"] == ["trim_startup"]
    assert sets["cpu_pct"]["state"] == "unmapped"


def test_an_evolved_mapping_counts_as_coverage(tmp_path):
    view = LearningView(
        FakeImprover(_state(ledger={})), FakeTrends({
            "cpu_pct": {"key": "cpu_pct", "label": "CPU"}}),
        FakeFixEngine(), _write_log(tmp_path, []),
        remediation_map={}, evolved_map={"cpu_pct": ["evolved_cap_cpu"]})
    row = view.knowledge_sets()[0]
    assert row["state"] == "mapped" and row["mapped"] == ["evolved_cap_cpu"]


def test_mapped_remediations_dedupes_catalogue_and_evolved(tmp_path):
    view = LearningView(
        FakeImprover(_state()), FakeTrends(), FakeFixEngine(),
        _write_log(tmp_path, []),
        remediation_map={"k": ["a", "b"]}, evolved_map={"k": ["b", "c"]})
    assert view.mapped_remediations("k") == ("a", "b", "c")
    assert view.mapped_remediations("nope") == ()


def test_knowledge_sets_survive_a_trend_store_without_history(tmp_path):
    view = LearningView(FakeImprover(_state()), FakeTrends(raises=True),
                        FakeFixEngine(), _write_log(tmp_path, []))
    assert view.knowledge_sets() == []


# -------------------------------------------------------------- rulebook ---
def test_rulebook_is_clean_for_a_compliant_history(view):
    rb = view.rulebook()
    assert rb["rule_count"] == len(rb["rules"]) == 7
    assert rb["compliance"]["clean"] is True
    assert rb["compliance"]["verifiable_rules"] >= 5
    assert all(r["ok"] for r in rb["rules"])


def test_rulebook_flags_an_unpermitted_risk_class(tmp_path):
    view = LearningView(FakeImprover(_state(allowed_risks=["safe"])),
                        FakeTrends(), FakeFixEngine(), _write_log(tmp_path, [
                            _cycle("2026-09-20T00:00:00+00:00",
                                   executed=[{"id": "restart_explorer",
                                              "risk": "caution"}])]))
    rb = view.rulebook()
    assert rb["compliance"]["clean"] is False
    assert any("caution" in v for v in rb["compliance"]["violations"])


def test_rulebook_flags_a_manual_execution(tmp_path):
    view = LearningView(FakeImprover(_state(allowed_risks=["safe", "manual"])),
                        FakeTrends(), FakeFixEngine(), _write_log(tmp_path, [
                            _cycle("2026-09-20T00:00:00+00:00",
                                   executed=[{"id": "trim_startup",
                                              "risk": "manual"}])]))
    assert any("manual" in v for v in view.rulebook()["compliance"]["violations"])


def test_rulebook_flags_an_over_budget_cycle(tmp_path):
    """The ceiling is N+1: the loop grants one extra while the trend degrades."""
    many = [{"id": f"f{i}", "risk": "safe"} for i in range(4)]
    view = LearningView(FakeImprover(_state(max_actions_per_cycle=2)),
                        FakeTrends(), FakeFixEngine(), _write_log(tmp_path, [
                            _cycle("2026-09-20T00:00:00+00:00", executed=many)]))
    assert any("budget" in v for v in view.rulebook()["compliance"]["violations"])


def test_rulebook_allows_the_one_action_trend_bonus(tmp_path):
    three = [{"id": f"f{i}", "risk": "safe"} for i in range(3)]
    view = LearningView(FakeImprover(_state(max_actions_per_cycle=2)),
                        FakeTrends(), FakeFixEngine(), _write_log(tmp_path, [
                            _cycle("2026-09-20T00:00:00+00:00", executed=three)]))
    assert view.rulebook()["compliance"]["clean"] is True


def test_rulebook_flags_a_cooldown_breach_and_ignores_unstamped(tmp_path):
    view = LearningView(FakeImprover(_state(cooldown_s=1800)),
                        FakeTrends(), FakeFixEngine(), _write_log(tmp_path, [
                            _cycle("2026-09-20T00:00:00+00:00", t=1000.0,
                                   executed=[{"id": "clear_temp", "risk": "safe"}]),
                            _cycle("2026-09-20T00:05:00+00:00", t=1300.0,
                                   executed=[{"id": "clear_temp", "risk": "safe"}]),
                            _cycle("2026-09-20T00:10:00+00:00", t=None,
                                   executed=[{"id": "clear_temp", "risk": "safe"}]),
                        ]))
    violations = view.rulebook()["compliance"]["violations"]
    assert violations == ["clear_temp repeated inside its cooldown"]


def test_rulebook_flags_an_unbounded_log(tmp_path):
    view = LearningView(FakeImprover(_state()), FakeTrends(), FakeFixEngine(),
                        _write_log(tmp_path, [
                            _cycle(f"2026-09-20T00:0{i}:00+00:00") for i in range(3)]),
                        log_limit=2)
    assert any("lines" in v for v in view.rulebook()["compliance"]["violations"])


def test_rulebook_survives_a_missing_log_file(tmp_path):
    """Before the loop's first cycle the log does not exist yet."""
    view = LearningView(FakeImprover(_state()), FakeTrends(), FakeFixEngine(),
                        tmp_path / "never-written.jsonl")
    assert view._log_lines() == 0
    assert view.rulebook()["compliance"]["clean"] is True
    assert view.snapshot()["window_cycles"] == 0


def test_view_without_a_fix_catalogue_falls_back_to_ids(tmp_path):
    view = LearningView(FakeImprover(_state()), FakeTrends(), None,
                        _write_log(tmp_path, []))
    assert view.strategies()[0]["title"] == "clear_temp"


def test_view_survives_an_unreadable_state(tmp_path):
    view = LearningView(FakeImprover(raises=True), FakeTrends(), FakeFixEngine(),
                        _write_log(tmp_path, []))
    assert view.kpis()["cycles"] == 0
    assert view.strategies() == []


def test_rulebook_with_no_policy_values_skips_the_budget_and_cooldown_checks(tmp_path):
    view = LearningView(
        FakeImprover(_state(max_actions_per_cycle=0, cooldown_s=0,
                            allowed_risks=[])),
        FakeTrends(), FakeFixEngine(), _write_log(tmp_path, [
            _cycle("2026-09-20T00:00:00+00:00", t=1.0,
                   executed=[{"id": "x"}]),
            _cycle("2026-09-20T00:01:00+00:00", t=2.0,
                   executed=[{"id": "x"}]),
        ]))
    rb = view.rulebook()
    assert rb["compliance"]["clean"] is True
    assert rb["rules"][0]["expect"] == "none"


# --------------------------------------------------------- heal vs evolve ---
def test_heal_vs_evolve_counts_both_layers(view):
    hve = view.heal_vs_evolve()
    assert hve["healing"] == {
        "fixes_applied": 1, "effects_measured": 1,
        "transient_spikes_absorbed": 1, "breaches_seen": 4,
        "breaches_cleared": 1, "open_breach": True}
    assert hve["evolution"]["strategies_learned"] == 3
    assert hve["evolution"]["strategies_effective"] == 1
    assert hve["evolution"]["strategies_suppressed"] == 1
    assert hve["evolution"]["escalations_open"] == 1
    assert hve["evolution"]["knowledge_sets"] == 3


def test_heal_vs_evolve_ignores_malformed_findings(tmp_path):
    view = LearningView(FakeImprover(_state(last=None)), FakeTrends(),
                        FakeFixEngine(), _write_log(tmp_path, [
                            _cycle("2026-09-20T00:00:00+00:00",
                                   findings=["bad"], breach=False)]))
    hve = view.heal_vs_evolve()
    assert hve["healing"]["transient_spikes_absorbed"] == 0
    assert hve["healing"]["open_breach"] is False


# ------------------------------------------------------------------ kpis ---
def test_kpis_average_only_scored_strategies(view):
    k = view.kpis()
    assert k["cycles"] == 12
    assert k["strategies"] == 3 and k["effective"] == 1
    assert k["avg_success_rate"] == 75.0        # only clear_temp is scored
    assert k["fixes_applied"] == 1
    assert k["escalations"] == 1
    assert k["learning_days"] == 2
    assert k["mode"] == "auto" and k["execution_enabled"] is True


def test_kpis_without_any_scored_strategy(tmp_path):
    view = LearningView(FakeImprover(_state(ledger={}, proposals=[], cycles=0)),
                        FakeTrends(), FakeFixEngine(), _write_log(tmp_path, []))
    k = view.kpis()
    assert k["avg_success_rate"] is None and k["strategies"] == 0


# -------------------------------------------------------------- snapshot ---
def test_snapshot_returns_every_section(view):
    snap = view.snapshot(learnings_limit=2)
    assert set(snap) == {"kpis", "strategies", "bands", "timeline",
                         "by_type", "learnings", "knowledge_sets", "rulebook",
                         "heal_vs_evolve", "cause_gloss", "window_cycles"}
    assert len(snap["learnings"]) == 2
    assert snap["window_cycles"] == 5
    assert snap["cause_gloss"] is CAUSE_GLOSS


def test_module_exposes_the_suppression_threshold():
    assert learning.SUPPRESS_AFTER >= 2
