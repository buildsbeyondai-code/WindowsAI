"""Full-coverage tests for evolve.py (the self-authoring evolution layer).

The five safety invariants documented in evolve.py's module docstring each have an
explicit test here, marked ``INVARIANT n``. They are the reason this layer is
allowed to run unattended, so a regression in any of them must fail the suite
rather than merely reduce coverage.
"""
import json

import pytest

import sysperf.evolve as evolve
from sysperf.evolve import (CLUSTER_THRESHOLD, EVOLVE_EVERY, MAPPING_THRESHOLD,
                    MAX_EVOLUTIONS_PER_PASS, MAX_PROPOSALS, MAX_RECIPES,
                    MAX_SHARDS, RECIPE_LIBRARY, RECIPE_THRESHOLD,
                    SEED_CLUSTERS, SHARD_THRESHOLD, EvolutionEngine, _iso,
                    _slug)

# The catalogue map the engine reads (a trimmed copy of rca.REMEDIATION).
BASE_MAP = {"c_free_gb": ["clear_temp"], "handles": ["restart_explorer"],
            "startup_apps": ["trim_startup"]}


class FakeView:
    """Stands in for LearningView, returning fixed derived evidence."""

    def __init__(self, cycles=None, state=None, strategies=None, knowledge=None):
        self._cycles_data = cycles or []
        self._state_data = state or {}
        self._strategies = strategies or []
        self._knowledge = knowledge or []

    def _cycles(self):
        return self._cycles_data

    def _state(self):
        return self._state_data

    def strategies(self, state=None):
        return self._strategies

    def knowledge_sets(self, state=None, strategies=None):
        return self._knowledge


def _know(key, label=None, state="unmapped", **over):
    row = {"key": key, "label": label or key, "state": state,
           "direction": "degrading", "pct_per_day": 1.5, "met_days": 3,
           "chronic": False, "mapped": [], "strategy_count": 0,
           "mapped_count": 0, "strategies": []}
    row.update(over)
    return row


def _cycle(key="cpu_pct", label="CPU utilisation", cause="usage_driven",
           at="2026-09-20T00:00:00+00:00", **over):
    rec = {"at": at, "t": 1000.0, "executed": [], "verification": None,
           "findings": [{"key": key, "label": label, "cause": cause,
                         "contribution_pct": 40.0}]}
    rec.update(over)
    return rec


def _engine(tmp_path, view, every=EVOLVE_EVERY, context=None, clock=None):
    ticks = [1_700_000_000.0]

    def now():
        return ticks[0] if clock is None else clock()

    engine = EvolutionEngine(view, BASE_MAP, tmp_path / "evolve_state.json",
                             tmp_path / "evolutions", every, now=now,
                             context=context)
    engine.ticks = ticks
    return engine


# ------------------------------------------------------------- helpers -------
def test_slug_is_safe_and_bounded():
    assert _slug("CPU utilisation (%)") == "cpu_utilisation"
    assert _slug("../../etc/passwd") == "etc_passwd"
    assert len(_slug("x" * 200)) == 48


def test_iso_formats_utc():
    assert _iso(0).startswith("1970-01-01T00:00:00")


# --------------------------------------------------------------- state -------
def test_state_starts_at_generation_zero(tmp_path):
    engine = _engine(tmp_path, FakeView())
    assert engine.state["generation"] == 0
    assert engine.status()["kpis"]["candidates"] == 0


def test_state_round_trips_through_the_file(tmp_path):
    first = _engine(tmp_path, FakeView())
    first.state["generation"] = 4
    first.state["shards"]["x"] = {"metric": "x", "shortfalls": 1}
    first._save()
    second = _engine(tmp_path, FakeView())
    assert second.state["generation"] == 4
    assert "x" in second.state["shards"]


def test_corrupt_or_mistyped_state_falls_back_to_defaults(tmp_path):
    path = tmp_path / "evolve_state.json"
    path.write_text("{not json", encoding="utf-8")
    assert _engine(tmp_path, FakeView()).state["generation"] == 0
    # a key of the wrong type is ignored rather than crashing the engine
    path.write_text(json.dumps({"generation": "four", "shards": []}),
                    encoding="utf-8")
    engine = _engine(tmp_path, FakeView())
    assert engine.state["generation"] == 0 and engine.state["shards"] == {}


def test_a_non_object_state_file_is_ignored(tmp_path):
    (tmp_path / "evolve_state.json").write_text("[1,2,3]", encoding="utf-8")
    assert _engine(tmp_path, FakeView()).state["generation"] == 0


# ---------------------------------------------------------------- tick -------
def test_tick_reports_due_only_on_the_cadence(tmp_path):
    engine = _engine(tmp_path, FakeView(), every=3)
    assert [engine.tick() for _ in range(3)] == [False, False, True]


def test_tick_accepts_an_absolute_cycle_count(tmp_path):
    engine = _engine(tmp_path, FakeView(), every=8)
    assert engine.tick(cycles=7) is False
    assert engine.tick(cycles=8) is True


def test_run_pass_refuses_when_not_due(tmp_path):
    engine = _engine(tmp_path, FakeView(), every=5)
    engine.tick(cycles=2)
    result = engine.run_pass()
    assert result == {"ran": False, "reason": "not due",
                      "cycles_until_due": 3, "evolutions": []}


# ------------------------------------------------------- 1. mappings ---------
def _collateral_cycles(n, metric="handles", fix="clear_temp"):
    """n cycles in which `fix` measurably improved `metric` it is not mapped to."""
    return [_cycle(at=f"2026-09-2{i}T00:00:00+00:00",
                   verification={"status": "verified", "actions": [fix],
                                 "verdicts": {metric: "improved"},
                                 "metrics": {metric: {"delta_pct": 1.0}}})
            for i in range(n)]


def test_mapping_evolution_needs_repeated_evidence(tmp_path):
    view = FakeView(cycles=_collateral_cycles(MAPPING_THRESHOLD - 1))
    engine = _engine(tmp_path, view)
    result = engine.run_pass(force=True)
    assert not [e for e in result["evolutions"] if e["kind"] == "mapping"]
    assert any("measured improvement" in s["reason"]
               for s in result["skipped"] if s["kind"] == "mapping")


def test_mapping_evolution_records_a_learned_pairing(tmp_path):
    view = FakeView(cycles=_collateral_cycles(MAPPING_THRESHOLD))
    engine = _engine(tmp_path, view)
    result = engine.run_pass(force=True)
    mapping = next(e for e in result["evolutions"] if e["kind"] == "mapping")
    assert mapping["fix"] == "clear_temp" and mapping["metric"] == "handles"
    assert engine.state["mappings"]["handles"] == ["clear_temp"]
    assert engine.state["generation"] == 1
    # a second pass must not duplicate it
    engine.run_pass(force=True)
    assert engine.state["mappings"]["handles"] == ["clear_temp"]


def test_a_fix_applied_for_its_own_metric_is_not_a_new_pairing(tmp_path):
    """clear_temp IS mapped to c_free_gb, so improving it teaches nothing new."""
    view = FakeView(cycles=_collateral_cycles(4, metric="c_free_gb"))
    engine = _engine(tmp_path, view)
    result = engine.run_pass(force=True)
    assert not [e for e in result["evolutions"] if e["kind"] == "mapping"]


def test_a_worsened_metric_is_not_a_pairing(tmp_path):
    view = FakeView(cycles=[_cycle(verification={
        "status": "verified", "actions": ["clear_temp"],
        "verdicts": {"handles": "worsened"}, "metrics": {}}) for _ in range(5)])
    engine = _engine(tmp_path, view)
    assert not [e for e in engine.run_pass(force=True)["evolutions"]
                if e["kind"] == "mapping"]


def test_an_unverified_record_is_ignored(tmp_path):
    view = FakeView(cycles=[_cycle(verification={"status": "waiting"})
                            for _ in range(5)])
    engine = _engine(tmp_path, view)
    assert not [e for e in engine.run_pass(force=True)["evolutions"]
                if e["kind"] == "mapping"]


# ------------------------------------------------------- 2. clusters ---------
def test_cluster_evolution_learns_cause_to_family(tmp_path):
    view = FakeView(cycles=[
        _cycle(cause="usage_driven",
               verification={"status": "verified", "actions": ["clear_temp"],
                             "verdicts": {"handles": "improved"}, "metrics": {}})
        for _ in range(CLUSTER_THRESHOLD)])
    engine = _engine(tmp_path, view)
    result = engine.run_pass(force=True)
    cluster = next(e for e in result["evolutions"] if e["kind"] == "cluster")
    assert cluster["cause"] == "usage_driven"
    assert engine.state["clusters"]["usage_driven"] == ["storage"]
    # stable on a second pass — no churn
    assert not [e for e in engine.run_pass(force=True)["evolutions"]
                if e["kind"] == "cluster"]


def test_cluster_evolution_needs_enough_evidence(tmp_path):
    view = FakeView(cycles=[
        _cycle(verification={"status": "verified", "actions": ["clear_temp"],
                             "verdicts": {"handles": "improved"}, "metrics": {}})
        for _ in range(CLUSTER_THRESHOLD - 1)])
    result = _engine(tmp_path, view).run_pass(force=True)
    assert any("measured resolutions" in s["reason"]
               for s in result["skipped"] if s["kind"] == "cluster")


def test_an_unknown_fix_has_no_family_and_forms_no_cluster(tmp_path):
    view = FakeView(cycles=[
        _cycle(verification={"status": "verified", "actions": ["who_knows"],
                             "verdicts": {"handles": "improved"}, "metrics": {}})
        for _ in range(5)])
    engine = _engine(tmp_path, view)
    assert engine.state["clusters"] == {}
    engine.run_pass(force=True)
    assert engine.state["clusters"] == {}


def test_family_of_resolves_library_and_generated_recipes(tmp_path):
    engine = _engine(tmp_path, FakeView())
    assert engine._family_of("cap_cpu_offenders") == "cpu"
    assert engine._family_of("flush_dns") == "network"
    assert engine._family_of("nothing_like_this") == ""
    engine.state["recipes"]["gen1"] = {"family": "disk"}
    assert engine._family_of("gen1") == "disk"


# -------------------------------------------------------- 3. recipes ---------
def _unmapped_view(metric="cpu_pct", label="CPU utilisation", count=None,
                   cause="usage_driven", knowledge_state="unmapped"):
    count = RECIPE_THRESHOLD if count is None else count
    return FakeView(
        cycles=[_cycle(key=metric, label=label, cause=cause,
                       at=f"2026-09-20T00:{i:02d}:00+00:00")
                for i in range(count)],
        knowledge=[_know(metric, label, state=knowledge_state)])


def test_recipe_evolution_proposes_a_vetted_candidate(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(),
                     context=lambda: {"cpu_consumers": {
                         "consumers": [{"name": "msedge", "consistent": True}]}})
    result = engine.run_pass(force=True)
    recipe = next(e for e in result["evolutions"] if e["kind"] == "recipe")
    assert recipe["needs_promotion"] is True
    candidate = engine.state["recipes"][recipe["candidate"]]
    assert candidate["recipe"] in RECIPE_LIBRARY
    assert candidate["params"] == {"process": "msedge"}
    assert candidate["missing_params"] == []


def test_INVARIANT_2_a_generated_candidate_cannot_run(tmp_path):
    engine = _engine(tmp_path, _unmapped_view())
    engine.run_pass(force=True)
    for candidate in engine.state["recipes"].values():
        assert candidate["state"] == "candidate"
        assert candidate["executable"] is False


def test_INVARIANT_1_a_candidate_only_ever_names_a_library_recipe(tmp_path):
    engine = _engine(tmp_path, _unmapped_view())
    engine.run_pass(force=True)
    for candidate in engine.state["recipes"].values():
        spec = RECIPE_LIBRARY[candidate["recipe"]]
        # every field that could carry an instruction comes from the library
        assert candidate["manual_hint"] == spec["manual_hint"]
        assert candidate["shell"] == spec["shell"]
        assert candidate["risk"] == spec["risk"]
        assert candidate["metric"] in spec["metrics"]


def test_recipe_evolution_needs_enough_shortfalls(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(count=RECIPE_THRESHOLD - 1))
    result = engine.run_pass(force=True)
    assert not [e for e in result["evolutions"] if e["kind"] == "recipe"]
    assert any("needs" in s["reason"] for s in result["skipped"]
               if s["kind"] == "recipe")


def test_a_mapped_metric_never_gets_a_generated_recipe(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(knowledge_state="mapped"))
    assert not [e for e in engine.run_pass(force=True)["evolutions"]
                if e["kind"] == "recipe"]


def test_a_metric_with_no_library_match_is_reported_not_invented(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(metric="net_remote",
                                              label="Remote conns"))
    result = engine.run_pass(force=True)
    assert not [e for e in result["evolutions"] if e["kind"] == "recipe"]
    assert any("no vetted recipe" in s["reason"] for s in result["skipped"])


def test_a_structural_cause_is_not_given_a_recipe(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(cause="chronic_unreachable"))
    result = engine.run_pass(force=True)
    assert not [e for e in result["evolutions"] if e["kind"] == "recipe"]
    assert any("structural" in s["reason"] for s in result["skipped"]
               if s["kind"] == "recipe")


def test_an_incomplete_candidate_records_its_missing_parameter(tmp_path):
    """No consistent CPU consumer means no evidence for `process`."""
    engine = _engine(tmp_path, _unmapped_view(), context=lambda: {})
    engine.run_pass(force=True)
    candidate = next(iter(engine.state["recipes"].values()))
    assert candidate["missing_params"] == ["process"]


def test_limit_gb_is_parameterised_from_the_target(tmp_path):
    view = _unmapped_view(metric="vmmem_mb", label="vmmemWSL")
    engine = _engine(tmp_path, view,
                     context=lambda: {"targets": {"vmmem_mb": 10240}})
    engine.run_pass(force=True)
    candidate = next(iter(engine.state["recipes"].values()))
    assert candidate["params"] == {"limit_gb": 10.0}


def test_a_missing_or_zero_target_leaves_limit_gb_absent(tmp_path):
    view = _unmapped_view(metric="vmmem_mb", label="vmmemWSL")
    engine = _engine(tmp_path, view, context=lambda: {"targets": {"vmmem_mb": 0}})
    engine.run_pass(force=True)
    assert next(iter(engine.state["recipes"].values()))["missing_params"] \
        == ["limit_gb"]


def test_a_context_returning_a_non_dict_is_ignored(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(), context=lambda: "nope")
    engine.run_pass(force=True)
    assert next(iter(engine.state["recipes"].values()))["missing_params"] \
        == ["process"]


def test_candidate_selection_prefers_the_learned_cluster_family(tmp_path):
    """browser_procs matches one recipe; committed_app_mb matches it too, so the
    cluster preference is what decides when several recipes match a metric."""
    engine = _engine(tmp_path, _unmapped_view(metric="available_mb",
                                              label="Available physical",
                                              cause="long_term_regression"),
                     context=lambda: {"targets": {"available_mb": 12000}})
    engine.state["clusters"]["long_term_regression"] = ["wsl"]
    engine.run_pass(force=True)
    candidate = next(iter(engine.state["recipes"].values()))
    assert candidate["recipe"] == "cap_wsl_memory"


def test_seed_clusters_apply_before_anything_is_learned(tmp_path):
    engine = _engine(tmp_path, FakeView())
    assert engine._candidate_recipe("cpu_pct", "usage_driven") \
        == "cap_cpu_offenders"
    assert engine._candidate_recipe("cpu_pct", "no_such_cause") \
        == "cap_cpu_offenders"
    assert engine._candidate_recipe("net_remote", "usage_driven") is None


def test_INVARIANT_3_the_recipe_cap_is_enforced(tmp_path):
    engine = _engine(tmp_path, _unmapped_view())
    engine.state["recipes"] = {f"filler{i}": {"state": "candidate",
                                              "family": "cpu", "id": f"f{i}"}
                               for i in range(MAX_RECIPES)}
    result = engine.run_pass(force=True)
    assert any("recipe cap reached" in s["reason"] for s in result["skipped"])
    assert len(engine.state["recipes"]) == MAX_RECIPES


def test_a_candidate_is_not_proposed_twice(tmp_path):
    engine = _engine(tmp_path, _unmapped_view())
    engine.run_pass(force=True)
    before = len(engine.state["recipes"])
    engine.run_pass(force=True)
    assert len(engine.state["recipes"]) == before


# --------------------------------------------------------- 4. shards ---------
def test_shard_evolution_writes_a_rules_file(tmp_path):
    view = _unmapped_view(count=SHARD_THRESHOLD)
    engine = _engine(tmp_path, view)
    result = engine.run_pass(force=True)
    shard = next(e for e in result["evolutions"] if e["kind"] == "shard")
    path = tmp_path / "evolutions" / shard["file"]
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# CPU utilisation — learned rules")
    assert "MUST NOT" in text and "usage_driven" in text
    assert engine.state["shards"]["cpu_pct"]["rules"] >= 1


def test_a_shard_reports_mapped_but_unrun_remediations(tmp_path):
    """Regression: an earlier version claimed "no remediation was mapped" for a
    metric that is mapped but has simply never been selected."""
    view = FakeView(
        cycles=[_cycle(key="startup_apps", label="Startup programs",
                       at=f"2026-09-20T00:{i:02d}:00+00:00")
                for i in range(SHARD_THRESHOLD)],
        knowledge=[_know("startup_apps", "Startup programs", state="mapped",
                         mapped=["trim_startup"])])
    engine = _engine(tmp_path, view)
    engine.run_pass(force=True)
    text = (tmp_path / "evolutions" / "startup_apps-rules.md").read_text(encoding="utf-8")
    assert "`trim_startup` — mapped to this metric but never selected" in text
    assert "No remediation is mapped" not in text


def test_a_shard_records_measured_and_suppressed_strategies(tmp_path):
    view = FakeView(
        cycles=[_cycle(key="handles", label="Handles", cause="transient_spike",
                       at=f"2026-09-20T00:{i:02d}:00+00:00")
                for i in range(SHARD_THRESHOLD)],
        strategies=[{"metric": "handles", "id": "restart_explorer",
                     "attempts": 4, "effect_pct": None, "status": "suppressed"}],
        knowledge=[_know("handles", "Handles", state="observed",
                         direction="stable", mapped=["restart_explorer"])])
    engine = _engine(tmp_path, view)
    engine.run_pass(force=True)
    text = (tmp_path / "evolutions" / "handles-rules.md").read_text()
    assert "MUST NOT choose `restart_explorer`" in text
    assert "SHOULD wait one cycle" in text          # transient-spike rule
    assert "effect n/a" in text
    assert "SHOULD raise a preventive flag" not in text   # direction is stable


def test_a_shard_for_a_metric_with_no_knowledge_row_still_writes(tmp_path):
    view = FakeView(cycles=[_cycle(key="odd", label="Odd",
                                   at=f"2026-09-20T00:{i:02d}:00+00:00")
                            for i in range(SHARD_THRESHOLD)])
    engine = _engine(tmp_path, view)
    engine.run_pass(force=True)
    text = (tmp_path / "evolutions" / "odd-rules.md").read_text()
    assert "No remediation is mapped" in text


def test_shard_evolution_needs_enough_shortfalls(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(count=SHARD_THRESHOLD - 1))
    result = engine.run_pass(force=True)
    assert not [e for e in result["evolutions"] if e["kind"] == "shard"]
    assert any(s["kind"] == "shard" for s in result["skipped"])


def test_INVARIANT_3_the_shard_cap_is_enforced(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(count=SHARD_THRESHOLD))
    engine.state["shards"] = {f"m{i}": {"shortfalls": 1} for i in range(MAX_SHARDS)}
    result = engine.run_pass(force=True)
    assert any("shard cap reached" in s["reason"] for s in result["skipped"])


# ------------------------------------------------------ 5. proposals ---------
def test_a_chronic_metric_becomes_a_structural_proposal(tmp_path):
    view = FakeView(
        cycles=[_cycle(key="cpu_pct", label="CPU utilisation",
                       cause="chronic_unreachable",
                       at=f"2026-09-20T00:{i:02d}:00+00:00") for i in range(4)],
        knowledge=[_know("cpu_pct", "CPU utilisation", chronic=True)])
    engine = _engine(tmp_path, view)
    result = engine.run_pass(force=True)
    proposal = next(e for e in result["evolutions"] if e["kind"] == "proposal")
    assert proposal["needs_human"] is True
    record = engine.state["proposals"][proposal["proposal"]]
    assert record["state"] == "open"
    assert "revise the target" in record["detail"]
    # not re-raised on the next pass
    assert not [e for e in engine.run_pass(force=True)["evolutions"]
                if e["kind"] == "proposal"]


def test_a_non_structural_cause_raises_no_proposal(tmp_path):
    engine = _engine(tmp_path, _unmapped_view())
    assert not [e for e in engine.run_pass(force=True)["evolutions"]
                if e["kind"] == "proposal"]


def test_INVARIANT_3_the_proposal_cap_is_enforced(tmp_path):
    view = FakeView(cycles=[_cycle(cause="chronic_unreachable")],
                    knowledge=[_know("cpu_pct")])
    engine = _engine(tmp_path, view)
    engine.state["proposals"] = {f"p{i}": {"id": f"p{i}", "state": "open"}
                                 for i in range(MAX_PROPOSALS)}
    result = engine.run_pass(force=True)
    assert any("proposal cap reached" in s["reason"] for s in result["skipped"])


# ------------------------------------------------------ INVARIANT 3 ---------
def test_INVARIANT_3_a_pass_spends_at_most_its_budget(tmp_path):
    """Many eligible metrics at once must not produce an unbounded pass."""
    metrics = ["cpu_pct", "cpu_queue", "disk_active_pct", "browser_procs",
               "vmmem_mb", "available_mb"]
    cycles = []
    for i, metric in enumerate(metrics):
        cycles += [_cycle(key=metric, label=metric,
                          at=f"2026-09-2{i}T00:{j:02d}:00+00:00")
                   for j in range(SHARD_THRESHOLD + 2)]
    view = FakeView(cycles=cycles,
                    knowledge=[_know(m, m) for m in metrics])
    engine = _engine(tmp_path, view, context=lambda: {"targets": {}})
    result = engine.run_pass(force=True)
    assert len(result["evolutions"]) <= MAX_EVOLUTIONS_PER_PASS
    assert result["budget_used"] == MAX_EVOLUTIONS_PER_PASS
    assert any("budget" in s["reason"] for s in result["skipped"])


def test_a_pass_with_no_evidence_does_not_bump_the_generation(tmp_path):
    engine = _engine(tmp_path, FakeView())
    result = engine.run_pass(force=True)
    assert result["evolutions"] == []
    assert engine.state["generation"] == 0
    assert engine.state["passes"] == 1


# --------------------------------------------------------- promotion ---------
def test_promotion_makes_a_candidate_selectable(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(),
                     context=lambda: {"cpu_consumers": {
                         "consumers": [{"name": "msedge", "consistent": True}]}})
    engine.run_pass(force=True)
    candidate_id = next(iter(engine.state["recipes"]))
    result = engine.promote(candidate_id)
    assert result["promoted"] is True
    record = result["recipe"]
    assert record["state"] == "promoted" and record["promoted_at"]
    assert engine.evolved_map()["cpu_pct"] == [candidate_id]


def test_INVARIANT_2_promotion_never_makes_a_manual_recipe_executable(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(metric="browser_procs",
                                              label="Browser processes"))
    engine.run_pass(force=True)
    candidate_id = next(iter(engine.state["recipes"]))
    assert RECIPE_LIBRARY[engine.state["recipes"][candidate_id]["recipe"]]["risk"] \
        == "manual"
    record = engine.promote(candidate_id)["recipe"]
    assert record["state"] == "promoted"
    assert record["executable"] is False


def test_INVARIANT_2_no_promoted_recipe_is_ever_executable(tmp_path):
    """Even a 'caution' recipe stays non-executable: the library supplies an
    instruction, not a command, so autonomous execution is never possible."""
    engine = _engine(tmp_path, _unmapped_view(),
                     context=lambda: {"cpu_consumers": {
                         "consumers": [{"name": "msedge", "consistent": True}]}})
    engine.run_pass(force=True)
    candidate_id = next(iter(engine.state["recipes"]))
    assert RECIPE_LIBRARY[engine.state["recipes"][candidate_id]["recipe"]]["risk"] \
        == "caution"
    engine.promote(candidate_id)
    assert engine.state["recipes"][candidate_id]["executable"] is False
    exported = engine.promoted_fixes()
    assert [f["risk"] for f in exported] == ["manual"]
    assert [f["command"] for f in exported] == [""]


def test_promoted_fixes_fill_the_hint_with_measured_parameters(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(),
                     context=lambda: {"cpu_consumers": {
                         "consumers": [{"name": "msedge", "consistent": True}]}})
    engine.run_pass(force=True)
    engine.promote(next(iter(engine.state["recipes"])))
    exported = engine.promoted_fixes()
    assert exported[0]["manual_hint"].startswith("Get-Process msedge")
    assert exported[0]["metric"] == "cpu_pct"
    assert exported[0]["origin"] == "evolved"


def test_promoted_fixes_excludes_candidates_and_rejections(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(), context=lambda: {})
    engine.run_pass(force=True)
    assert engine.promoted_fixes() == []            # still a candidate
    engine.reject(next(iter(engine.state["recipes"])))
    assert engine.promoted_fixes() == []


def test_a_hint_with_an_unfillable_placeholder_is_returned_verbatim():
    record = {"manual_hint": "set {nothing_supplies_this}", "params": {}}
    assert EvolutionEngine._hint(record) == "set {nothing_supplies_this}"


def test_the_on_promote_hook_receives_the_exported_fixes(tmp_path):
    seen = []
    engine = _engine(tmp_path, _unmapped_view(),
                     context=lambda: {"cpu_consumers": {
                         "consumers": [{"name": "msedge", "consistent": True}]}})
    engine.on_promote = seen.append
    engine.run_pass(force=True)
    engine.promote(next(iter(engine.state["recipes"])))
    assert len(seen) == 1 and seen[0][0]["risk"] == "manual"


def test_an_incomplete_candidate_cannot_be_promoted(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(), context=lambda: {})
    engine.run_pass(force=True)
    candidate_id = next(iter(engine.state["recipes"]))
    result = engine.promote(candidate_id)
    assert result["promoted"] is False
    assert "no measured value for process" in result["reason"]
    assert engine.state["recipes"][candidate_id]["executable"] is False


def test_promoting_twice_or_an_unknown_id_is_refused(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(),
                     context=lambda: {"cpu_consumers": {
                         "consumers": [{"name": "msedge", "consistent": True}]}})
    engine.run_pass(force=True)
    candidate_id = next(iter(engine.state["recipes"]))
    engine.promote(candidate_id)
    assert engine.promote(candidate_id)["reason"] == "already promoted"
    assert engine.promote("ghost")["reason"] == "no such candidate"


def test_rejection_marks_a_candidate_and_removes_it_from_coverage(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(),
                     context=lambda: {"cpu_consumers": {
                         "consumers": [{"name": "msedge", "consistent": True}]}})
    engine.run_pass(force=True)
    candidate_id = next(iter(engine.state["recipes"]))
    engine.promote(candidate_id)
    assert engine.evolved_map()
    assert engine.reject(candidate_id)["rejected"] is True
    assert engine.state["recipes"][candidate_id]["executable"] is False
    assert engine.evolved_map() == {}       # a rejected recipe is not coverage
    assert engine.reject("ghost")["rejected"] is False


def test_evolved_map_includes_a_learned_pairing_without_a_recipe(tmp_path):
    engine = _engine(tmp_path, FakeView(cycles=_collateral_cycles(MAPPING_THRESHOLD)))
    engine.run_pass(force=True)
    assert engine.evolved_map() == {"handles": ["clear_temp"]}


# ---------------------------------------------------------- rollback ---------
def test_rollback_removes_a_generated_file_and_its_registry_key(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(count=SHARD_THRESHOLD))
    engine.run_pass(force=True)
    shard_file = tmp_path / "evolutions" / "cpu_pct-rules.md"
    assert shard_file.is_file()
    # the shard is not necessarily last; roll back until it is gone
    for _ in range(6):
        if not shard_file.exists():
            break
        engine.rollback_last()
    assert not shard_file.exists()
    assert "cpu_pct" not in engine.state["shards"]


def test_rollback_undoes_a_mapping_entry(tmp_path):
    engine = _engine(tmp_path, FakeView(cycles=_collateral_cycles(MAPPING_THRESHOLD)))
    engine.run_pass(force=True)
    assert engine.state["mappings"]["handles"] == ["clear_temp"]
    engine.rollback_last()
    assert engine.state["mappings"]["handles"] == []


def test_rollback_tolerates_an_already_deleted_file(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(count=SHARD_THRESHOLD))
    engine.run_pass(force=True)
    (tmp_path / "evolutions" / "cpu_pct-rules.md").unlink()
    assert engine.rollback_last()["rolled_back"] is True


def test_rollback_ignores_an_unknown_registry_bucket(tmp_path):
    engine = _engine(tmp_path, FakeView())
    engine._log("odd", "made up", registry=["nosuchbucket/x", "shards"])
    result = engine.rollback_last()
    assert result["rolled_back"] is True and result["keys_removed"] == []


def test_rollback_with_nothing_to_undo(tmp_path):
    assert _engine(tmp_path, FakeView()).rollback_last() == {
        "rolled_back": False, "reason": "nothing to roll back"}


def test_rollback_never_drives_the_generation_negative(tmp_path):
    engine = _engine(tmp_path, FakeView())
    engine._log("odd", "no generation was ever bumped")
    engine.rollback_last()
    assert engine.state["generation"] == 0


# ------------------------------------------------------------ reporting ------
def test_status_exposes_thresholds_caps_and_the_library(tmp_path):
    engine = _engine(tmp_path, _unmapped_view())
    engine.run_pass(force=True)
    status = engine.status()
    assert status["thresholds"]["recipe"] == RECIPE_THRESHOLD
    assert status["caps"]["per_pass"] == MAX_EVOLUTIONS_PER_PASS
    assert {r["id"] for r in status["library"]} == set(RECIPE_LIBRARY)
    assert set(SEED_CLUSTERS).issubset(status["clusters"])
    assert status["kpis"]["candidates"] >= 1
    assert status["by_kind"]
    # A forced pass resets the anchor to the current cycle count, so the next
    # scheduled pass is a full cadence away again.
    assert status["cycles_until_pass"] == EVOLVE_EVERY


def test_status_log_is_newest_first(tmp_path):
    engine = _engine(tmp_path, FakeView())
    engine._log("a", "first")
    engine._log("b", "second")
    assert [e["kind"] for e in engine.status()["log"]] == ["b", "a"]


def test_the_log_is_bounded(tmp_path):
    engine = _engine(tmp_path, FakeView())
    for i in range(evolve.LOG_KEEP + 25):
        engine._log("noise", f"entry {i}")
    assert len(engine.state["log"]) == evolve.LOG_KEEP
    assert engine.state["log"][-1]["summary"].endswith(
        str(evolve.LOG_KEEP + 24))


# ------------------------------------------- per-step budget exhaustion ------
# Each evolution step must stop mid-list when the pass budget runs out, not just
# be skipped wholesale. Exercised directly with budget=1 and two eligible items.
def test_mapping_step_stops_at_its_budget(tmp_path):
    cycles = _collateral_cycles(MAPPING_THRESHOLD, metric="handles") \
        + _collateral_cycles(MAPPING_THRESHOLD, metric="startup_apps")
    engine = _engine(tmp_path, FakeView(cycles=cycles))
    applied, _ = engine._evolve_mappings(engine._evidence(), 1)
    assert len(applied) == 1


def test_cluster_step_stops_at_its_budget(tmp_path):
    cycles = []
    for cause, fix in (("usage_driven", "clear_temp"),
                       ("steady_shortfall", "restart_explorer")):
        cycles += [_cycle(cause=cause, verification={
            "status": "verified", "actions": [fix],
            "verdicts": {"handles": "improved"}, "metrics": {}})
            for _ in range(CLUSTER_THRESHOLD)]
    engine = _engine(tmp_path, FakeView(cycles=cycles))
    applied, _ = engine._evolve_clusters(engine._evidence(), 1)
    assert len(applied) == 1


def test_recipe_step_stops_at_its_budget(tmp_path):
    cycles, knowledge = [], []
    for i, metric in enumerate(("cpu_pct", "disk_active_pct")):
        cycles += [_cycle(key=metric, label=metric,
                          at=f"2026-09-2{i}T00:{j:02d}:00+00:00")
                   for j in range(RECIPE_THRESHOLD)]
        knowledge.append(_know(metric, metric))
    engine = _engine(tmp_path, FakeView(cycles=cycles, knowledge=knowledge),
                     context=lambda: {})
    applied, _ = engine._evolve_recipes(engine._evidence(), 1)
    assert len(applied) == 1


def test_shard_step_stops_at_its_budget(tmp_path):
    cycles = []
    for i, metric in enumerate(("cpu_pct", "disk_active_pct")):
        cycles += [_cycle(key=metric, label=metric,
                          at=f"2026-09-2{i}T00:{j:02d}:00+00:00")
                   for j in range(SHARD_THRESHOLD)]
    engine = _engine(tmp_path, FakeView(cycles=cycles))
    applied, _ = engine._evolve_shards(engine._evidence(), 1)
    assert len(applied) == 1


def test_proposal_step_stops_at_its_budget(tmp_path):
    cycles = []
    for i, metric in enumerate(("cpu_pct", "disk_active_pct")):
        cycles += [_cycle(key=metric, label=metric, cause="chronic_unreachable",
                          at=f"2026-09-2{i}T00:{j:02d}:00+00:00")
                   for j in range(3)]
    engine = _engine(tmp_path, FakeView(cycles=cycles))
    applied, _ = engine._evolve_proposals(engine._evidence(), 1)
    assert len(applied) == 1


def test_evidence_ignores_malformed_and_unkeyed_findings(tmp_path):
    engine = _engine(tmp_path, FakeView(cycles=[
        _cycle(findings=["not-a-dict", {"label": "no key"}, {"key": ""}])]))
    ev = engine._evidence()
    assert ev["shortfalls"] == {} and ev["labels"] == {}


def test_status_counts_only_open_proposals(tmp_path):
    engine = _engine(tmp_path, FakeView())
    engine.state["proposals"] = {
        "a": {"id": "a", "state": "open"},
        "b": {"id": "b", "state": "accepted"},
    }
    assert engine.status()["kpis"]["proposals"] == 1


# ---------------------------------------------------------- INVARIANT 4 ------def test_INVARIANT_4_writes_stay_inside_the_state_file_and_evolutions_dir(tmp_path):
    """Nothing outside the two declared paths may be created or touched."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fixes.py").write_text("ORIGINAL", encoding="utf-8")
    (repo / "rca.py").write_text("ORIGINAL", encoding="utf-8")
    engine = EvolutionEngine(
        _unmapped_view(count=SHARD_THRESHOLD), BASE_MAP,
        repo / "evolve_state.json", repo / "evolutions", EVOLVE_EVERY,
        context=lambda: {})
    engine.run_pass(force=True)
    engine.rollback_last()
    assert (repo / "fixes.py").read_text() == "ORIGINAL"
    assert (repo / "rca.py").read_text() == "ORIGINAL"
    created = {p.name for p in repo.iterdir()}
    assert created == {"fixes.py", "rca.py", "evolve_state.json", "evolutions"}


def test_INVARIANT_5_every_log_entry_is_marked_reversible(tmp_path):
    engine = _engine(tmp_path, _unmapped_view(count=SHARD_THRESHOLD),
                     context=lambda: {})
    engine.run_pass(force=True)
    assert engine.state["log"]
    assert all(e["reversible"] for e in engine.state["log"])
    assert all("generation" in e and "at" in e for e in engine.state["log"])


def test_the_library_is_internally_consistent():
    """Every recipe declares the fields the engine and UI read."""
    for name, spec in RECIPE_LIBRARY.items():
        assert spec["risk"] in ("safe", "caution", "manual"), name
        assert spec["metrics"] and isinstance(spec["metrics"], tuple), name
        assert spec["family"] and spec["title"] and spec["rationale"], name
        assert isinstance(spec["params"], tuple), name
        assert spec["shell"] in ("pwsh", "bash"), name
        for param in spec["params"]:
            assert "{" + param + "}" in spec["manual_hint"], (name, param)


# ------------------------------------- automatic adjudication (auto_adopt) --
def _candidate(cid, metric, risk="safe", reversible=True, missing=None, state="candidate"):
    return {"id": cid, "recipe": cid, "title": cid, "family": "f", "metric": metric,
            "label": metric, "risk": risk, "reversible": reversible, "shell": "pwsh",
            "rationale": "r", "manual_hint": "h", "params": {},
            "missing_params": missing or [], "state": state, "executable": False,
            "generation": 1}


def test_auto_adopt_adopts_gap_fillers_and_holds_the_rest(tmp_path):
    state = {"recipes": {
        "gap": _candidate("gap", "cpu_pct"),
        "dup": _candidate("dup", "handles"),                    # already mapped
        "manual": _candidate("manual", "disk_active_pct", risk="manual"),
        "oneway": _candidate("oneway", "cpu_queue", reversible=False),
        "done": _candidate("done", "x", state="promoted")}}
    (tmp_path / "evolve_state.json").write_text(json.dumps(state))
    promoted = []
    eng = EvolutionEngine(FakeView(), BASE_MAP, tmp_path / "evolve_state.json",
                          tmp_path / "evolutions", EVOLVE_EVERY, auto_adopt=True,
                          on_promote=promoted.append)
    r = eng.state["recipes"]
    assert r["gap"]["state"] == "promoted" and promoted
    assert r["dup"]["held_reason"].startswith("redundant")
    assert r["manual"]["held_reason"] == "needs human judgement"
    assert r["oneway"]["held_reason"] == "not reversible / incomplete"
    assert r["done"]["state"] == "promoted"
    assert eng.auto_adjudicate() == []                          # nothing left to decide
    saved = json.loads((tmp_path / "evolve_state.json").read_text())
    assert saved["recipes"]["dup"]["state"] == "held"


def test_auto_adjudicate_is_off_without_auto_adopt_and_runs_after_a_pass(tmp_path):
    eng = _engine(tmp_path, FakeView())
    assert eng.auto_adjudicate() == []
    eng.auto_adopt = True
    calls = []
    eng.auto_adjudicate = lambda: calls.append(True) or []
    eng.run_pass(force=True)
    assert calls == [True]
