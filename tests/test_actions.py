"""Full-coverage tests for actions.py (the human action queue).

Two properties matter most and are asserted explicitly:

* **No duplicates.** Every item has a stable key and the queue is keyed by it, so
  the same work is never presented twice however many sources mention it.
* **Honest status.** ``closed`` means either you marked it done or the evidence
  says it no longer applies — and a closed item reopens by itself if the trigger
  recurs, rather than silently staying hidden.
"""

import pytest

import sysperf.actions as actions
from sysperf.actions import STEP_RECIPES, WHERE, WHY, ActionQueue, _stamp


class FakeFix:
    def __init__(self, fid, title="A fix", risk="manual", shell="pwsh",
                 command="", manual_hint="", reversible=True,
                 session_killing=False, persistent=False):
        self.id = fid
        self.title = title
        self.risk = risk
        self.shell = shell
        self.command = command
        self.manual_hint = manual_hint
        self.reversible = reversible
        self.session_killing = session_killing
        self.persistent = persistent

    @property
    def executable(self):
        return self.risk in ("safe", "caution") and not self.session_killing


class FakeFixEngine:
    def __init__(self, fixes):
        self._by_id = {f.id: f for f in fixes}

    def get(self, fix_id):
        return self._by_id.get(fix_id)


class FakeImprover:
    def __init__(self, state):
        self._state = state

    def status(self):
        return self._state


class FakeEvolution:
    def __init__(self, recipes):
        self._recipes = recipes

    def status(self):
        return {"recipes": self._recipes}


def _skip(fix_id, metric="handles", reason="manual action required"):
    return {"id": fix_id, "for": metric, "eligible": False,
            "skip_reason": reason, "why": f"{metric} contributes 30% of the gap"}


def _state(skipped=None, planned=None, proposals=None):
    return {"mode": "auto",
            "last": {"skipped": skipped or [], "planned": planned or []},
            "proposals": proposals or []}


def _rca(metric="handles", label="Active app handles", share=30.0,
         status="regress", cause="steady_shortfall"):
    return {"findings": [{"key": metric, "label": label, "status": status,
                          "contribution_pct": share, "cause": cause}]}


def _proposal(metric="c_free_gb", seen=5):
    return {"key": metric, "kind": "escalate_manual", "seen": seen,
            "detail": "C: free is regressing long-term",
            "evidence": {"label": "C: free", "target": 180, "min": 172.8,
                         "max": 188.9, "mean": 182.2, "met_days": 3,
                         "window_days": 14, "pct_per_day": -2.09,
                         "direction": "degrading", "higher_is_better": True}}


def _candidate(cid="evolved_cap_cpu_offenders_cpu_pct", state="candidate",
               missing=None, params=None):
    return {"id": cid, "state": state, "title": "Lower a CPU consumer's priority",
            "metric": "cpu_pct", "label": "CPU utilisation", "risk": "caution",
            "cause": "usage_driven", "rationale": "A sustained consumer is a "
            "scheduling problem.", "manual_hint": "Get-Process {process} | ...",
            "params": params if params is not None else {"process": "msedge"},
            "missing_params": missing or [], "evidence_count": 216,
            "promoted_at": "2026-09-29T04:00:00+00:00"}


def _queue(tmp_path, state, fixes=(), evolution=None, clock=None):
    return ActionQueue(FakeImprover(state), FakeFixEngine(list(fixes)), evolution,
                       tmp_path / "actions_state.json",
                       now=clock or (lambda: 1_700_000_000.0))


# ---------------------------------------------------------------- helpers -----
def test_stamp_is_iso_utc():
    assert _stamp(0) == "1970-01-01T00:00:00+00:00"


def test_every_reason_and_place_is_documented():
    for code, (headline, detail) in WHY.items():
        assert headline and detail, code
        assert len(detail) > 60, f"{code} detail is too thin to be useful"
    assert all(WHERE.values())


def test_every_step_recipe_is_complete_and_wired():
    for fix_id, recipe in STEP_RECIPES.items():
        assert recipe["why"] in WHY, fix_id
        assert recipe["where"] in WHERE, fix_id
        assert len(recipe["steps"]) >= 3, fix_id
        assert recipe["verify"] and recipe["expected"] and recipe["effort"], fix_id


# ------------------------------------------------------------- fix items ------
def test_a_manual_fix_becomes_an_item_with_steps_and_a_reason(tmp_path):
    queue = _queue(tmp_path, _state(skipped=[_skip("clear_standby",
                                                   metric="available_mb")]),
                   [FakeFix("clear_standby", "Clear standby memory")])
    item = queue.report(_rca("available_mb", "Available physical", 42.0))["items"][0]
    assert item["kind"] == "fix" and item["status"] == "open"
    assert item["key"] == "fix:clear_standby"
    assert item["why_code"] == "needs_elevation"
    assert item["why_headline"] == WHY["needs_elevation"][0]
    assert item["where"] == WHERE["elevated_pwsh"]
    assert "RAMMap.exe -Et" in " ".join(item["steps"])
    assert item["gap_share_pct"] == 42.0
    assert item["has_recipe"] is True
    assert item["priority"] == 1


def test_an_executable_fix_is_not_a_human_item(tmp_path):
    """A safe fix waiting out a cooldown is the loop managing itself."""
    queue = _queue(tmp_path, _state(skipped=[_skip("clear_temp",
                                                   reason="cooling down")]),
                   [FakeFix("clear_temp", risk="safe")])
    assert queue.report(_rca())["items"] == []


def test_an_eligible_or_unknown_decision_is_skipped(tmp_path):
    state = _state(skipped=[{"id": "clear_standby", "eligible": True},
                            {"id": "", "eligible": False},
                            {"id": "ghost", "eligible": False},
                            "not-a-dict"])
    queue = _queue(tmp_path, state, [FakeFix("clear_standby")])
    assert queue.report(_rca())["items"] == []


def test_a_session_killing_fix_says_so(tmp_path):
    queue = _queue(tmp_path, _state(skipped=[_skip("restart_wsl", "vmmem_mb")]),
                   [FakeFix("restart_wsl", "Restart WSL", session_killing=True)])
    item = queue.report(_rca("vmmem_mb", "vmmemWSL"))["items"][0]
    assert item["why_code"] == "session_killing"
    assert item["where"] == WHERE["windows_pwsh"]


def test_reasons_are_inferred_from_flags_when_no_recipe_exists(tmp_path):
    cases = [
        (FakeFix("a", manual_hint="Run once as admin: do the thing"),
         "needs_elevation"),
        (FakeFix("b", manual_hint="Open Task Manager and look"), "gui_only"),
        (FakeFix("c", manual_hint="", persistent=True), "persistent_config"),
        (FakeFix("d", manual_hint=""), "needs_judgement"),
        (FakeFix("e", manual_hint="", session_killing=True), "session_killing"),
    ]
    for fix, expected in cases:
        queue = _queue(tmp_path, _state(skipped=[_skip(fix.id)]), [fix])
        item = queue.report(_rca())["items"][0]
        assert item["why_code"] == expected, fix.id
        assert item["has_recipe"] is False


def test_a_fix_without_a_recipe_still_gets_usable_steps(tmp_path):
    queue = _queue(tmp_path,
                   _state(skipped=[_skip("mystery")]),
                   [FakeFix("mystery", "Do the mystery thing",
                            manual_hint="Twiddle the knob in the vendor tool")])
    item = queue.report(_rca())["items"][0]
    assert "Twiddle the knob" in " ".join(item["steps"])
    assert item["steps"][0].startswith("Open ")
    assert item["verify"] and item["expected"]


def test_a_fix_with_no_hint_says_no_recipe_exists(tmp_path):
    queue = _queue(tmp_path, _state(skipped=[_skip("bare")]),
                   [FakeFix("bare", "Bare fix")])
    assert "No reviewed step-by-step recipe" in " ".join(
        queue.report(_rca())["items"][0]["steps"])


# ------------------------------------------------------ INVARIANT: no dupes ---
def test_the_same_fix_from_several_sources_yields_one_item(tmp_path):
    """Present in skipped twice (two metrics) and in planned — still one item."""
    state = _state(
        skipped=[_skip("clear_standby", "available_mb"),
                 _skip("clear_standby", "compression_mb")],
        planned=[_skip("clear_standby", "available_mb")])
    queue = _queue(tmp_path, state, [FakeFix("clear_standby")])
    report = queue.report(_rca("available_mb"))
    assert report["count"] == 1
    assert report["unique_keys"] is True


def test_keys_are_unique_across_every_source(tmp_path):
    state = _state(skipped=[_skip("clear_standby", "available_mb")],
                   proposals=[_proposal(), _proposal()])
    queue = _queue(tmp_path, state, [FakeFix("clear_standby")],
                   FakeEvolution([_candidate(), _candidate()]))
    report = queue.report(_rca("available_mb"))
    keys = [i["key"] for i in report["items"]]
    assert len(keys) == len(set(keys))
    assert report["unique_keys"] is True
    # two identical proposals and two identical candidates collapse to one each
    assert report["count"] == 3


# -------------------------------------------------------- decision items ------
def test_a_chronic_escalation_becomes_a_decision_with_the_evidence(tmp_path):
    queue = _queue(tmp_path, _state(proposals=[_proposal()]))
    item = queue.report()["items"][0]
    assert item["kind"] == "decision" and item["key"] == "decision:c_free_gb"
    assert item["why_code"] == "chronic_unreachable"
    assert item["where"] == WHERE["decision"]
    joined = " ".join(item["steps"])
    assert "met its target on only 3" in joined
    assert "target (180)" in joined and "best observed: 172.8" in joined
    assert "revise the target" in joined
    assert item["caveat"]


def test_a_malformed_proposal_is_ignored(tmp_path):
    queue = _queue(tmp_path, _state(proposals=["nope", {}]))
    items = queue.report()["items"]
    assert len(items) == 1 and items[0]["key"] == "decision:"


def test_a_lower_is_better_metric_reports_its_best_as_the_max(tmp_path):
    proposal = _proposal()
    proposal["evidence"]["higher_is_better"] = False
    item = _queue(tmp_path, _state(proposals=[proposal])).report()["items"][0]
    assert "best observed: 188.9" in " ".join(item["steps"])


# -------------------------------------------------------- review items --------
def test_a_candidate_becomes_a_review_item(tmp_path):
    queue = _queue(tmp_path, _state(), evolution=FakeEvolution([_candidate()]))
    item = queue.report()["items"][0]
    assert item["kind"] == "review" and item["key"].startswith("review:")
    assert item["why_code"] == "grant_capability"
    assert item["where"] == WHERE["dashboard"]
    assert "Get-Process msedge" in item["command"]
    assert "Promoting does NOT run it" in " ".join(item["steps"])
    assert item["status"] == "open"


def test_an_incomplete_candidate_is_blocked_on_evidence(tmp_path):
    queue = _queue(tmp_path, _state(),
                   evolution=FakeEvolution([_candidate(missing=["process"],
                                                       params={})]))
    item = queue.report()["items"][0]
    assert item["kind"] == "blocked" and item["why_code"] == "no_evidence"
    assert "needs a value for process" in " ".join(item["steps"])
    assert item["status"] == "open"


def test_an_unfillable_hint_is_left_verbatim(tmp_path):
    candidate = _candidate(params={})
    queue = _queue(tmp_path, _state(), evolution=FakeEvolution([candidate]))
    assert queue.report()["items"][0]["command"] == "Get-Process {process} | ..."


def test_no_evolution_engine_means_no_review_items(tmp_path):
    assert _queue(tmp_path, _state()).report()["items"] == []


# ------------------------------------------------------- status: closed --------
def test_a_decided_candidate_closes_itself(tmp_path):
    queue = _queue(tmp_path, _state(),
                   evolution=FakeEvolution([_candidate(state="promoted")]))
    report = queue.report()
    item = report["items"][0]
    assert item["status"] == "closed"
    assert item["closed_reason"] == "resolved automatically"
    assert item["closed_by"] == "evidence"
    assert item["resolution"].startswith("promoted on")
    assert item["priority"] is None          # closed work is not prioritised
    assert report["open"] == 0 and report["closed"] == 1


def test_a_metric_back_on_target_closes_its_fix_item(tmp_path):
    queue = _queue(tmp_path, _state(skipped=[_skip("clear_standby",
                                                   "available_mb")]),
                   [FakeFix("clear_standby")])
    item = queue.report(_rca("available_mb", status="met"))["items"][0]
    assert item["status"] == "closed"
    assert item["closed_reason"] == "resolved automatically"
    assert item["closed_by"] == "evidence"


def test_a_metric_absent_from_the_analysis_closes_its_fix_item(tmp_path):
    queue = _queue(tmp_path, _state(skipped=[_skip("clear_standby", "gone")]),
                   [FakeFix("clear_standby")])
    assert queue.report(_rca("available_mb"))["items"][0]["status"] == "closed"


def test_marking_done_closes_an_item_and_persists_it(tmp_path):
    state = _state(proposals=[_proposal(seen=5)])
    queue = _queue(tmp_path, state)
    key = "decision:c_free_gb"
    assert queue.report()["items"][0]["status"] == "open"
    result = queue.set_status(key, "closed", "revised the target")
    assert result["ok"] is True and result["status"] == "closed"
    item = queue.report()["items"][0]
    assert item["status"] == "closed"
    assert item["closed_reason"] == "action taken"
    assert item["closed_by"] == "human"
    assert item["note"] == "revised the target"
    assert item["closed_at"].startswith("2023-")   # from the injected clock
    # ...and survives a reload
    reloaded = _queue(tmp_path, state)
    assert reloaded.report()["items"][0]["status"] == "closed"


def test_reopening_clears_the_stored_status(tmp_path):
    queue = _queue(tmp_path, _state(proposals=[_proposal()]))
    key = "decision:c_free_gb"
    queue.set_status(key, "closed")
    assert queue.set_status(key, "open")["status"] == "open"
    assert queue.report()["items"][0]["status"] == "open"
    assert key not in queue.state


def test_a_closed_item_reopens_when_the_trigger_recurs(tmp_path):
    """Closing a task must not hide it coming back."""
    queue = _queue(tmp_path, _state(proposals=[_proposal(seen=5)]))
    key = "decision:c_free_gb"
    queue.set_status(key, "closed", "handled")
    assert queue.report()["items"][0]["status"] == "closed"
    # the loop re-raises the escalation: seen grows past the value at close time
    queue.improver._state = _state(proposals=[_proposal(seen=9)])
    item = queue.report()["items"][0]
    assert item["status"] == "open"
    assert item["recurred"] is True
    assert queue.report()["recurred"] == 1


def test_closing_a_fix_does_not_instantly_reopen_it(tmp_path):
    """Regression: the recurrence check used the fix's share of the scoring gap,
    a number that moves every cycle and was read in a different analysis context
    than the caller's — so "Done" reopened immediately."""
    state = _state(skipped=[_skip("clear_standby", "available_mb")])
    queue = _queue(tmp_path, state, [FakeFix("clear_standby")])
    rca = _rca("available_mb", "Available physical", 44.1)
    assert queue.report(rca)["items"][0]["status"] == "open"
    queue.set_status("fix:clear_standby", "closed", "ran RAMMap")
    item = queue.report(rca)["items"][0]
    assert item["status"] == "closed"
    assert item["recurred"] is False
    # ...and it stays closed when the share moves, because that is not recurrence
    moved = queue.report(_rca("available_mb", "Available physical", 71.9))["items"][0]
    assert moved["status"] == "closed" and moved["recurred"] is False


def test_set_status_preserves_the_callers_analysis_context(tmp_path):
    queue = _queue(tmp_path, _state(skipped=[_skip("clear_standby", "available_mb")]),
                   [FakeFix("clear_standby")])
    rca = _rca("available_mb", "Available physical", 44.1)
    queue.report(rca)
    queue.set_status("fix:clear_standby", "closed")
    assert queue._rca == rca
    assert queue.report(rca)["items"][0]["gap_share_pct"] == 44.1


def test_an_action_you_took_is_reported_as_such_not_as_auto_resolved(tmp_path):
    """Who closed it is the more useful fact, so a human close wins over the
    evidence catching up afterwards."""
    queue = _queue(tmp_path, _state(skipped=[_skip("clear_standby", "available_mb")]),
                   [FakeFix("clear_standby")])
    queue.set_status("fix:clear_standby", "closed", "ran RAMMap -Et")
    # the metric is now back on target, so the evidence would also close it
    item = queue.report(_rca("available_mb", status="met"))["items"][0]
    assert item["closed_reason"] == "action taken"
    assert item["closed_by"] == "human"
    assert item["closed_at"]
    assert item["note"] == "ran RAMMap -Et"
    assert queue.report(_rca("available_mb", status="met"))["action_taken"] == 1


def test_the_report_counts_actions_you_took(tmp_path):
    state = _state(proposals=[_proposal()], skipped=[_skip("clear_standby",
                                                          "available_mb")])
    queue = _queue(tmp_path, state, [FakeFix("clear_standby")])
    assert queue.report(_rca("available_mb"))["action_taken"] == 0
    queue.set_status("decision:c_free_gb", "closed")
    assert queue.report(_rca("available_mb"))["action_taken"] == 1


def test_closing_an_unknown_key_is_refused(tmp_path):
    queue = _queue(tmp_path, _state())
    result = queue.set_status("fix:nothing", "closed")
    assert result == {"ok": False, "reason": "no such action item",
                      "key": "fix:nothing"}
    assert queue.state == {}


def test_an_invalid_status_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="open"):
        _queue(tmp_path, _state()).set_status("x", "maybe")


def test_a_long_note_is_truncated(tmp_path):
    queue = _queue(tmp_path, _state(proposals=[_proposal()]))
    queue.set_status("decision:c_free_gb", "closed", "x" * 900)
    assert len(queue.report()["items"][0]["note"]) == 400


# ---------------------------------------------------------- persistence -------
def test_a_missing_or_corrupt_state_file_starts_empty(tmp_path):
    assert _queue(tmp_path, _state()).state == {}
    (tmp_path / "actions_state.json").write_text("{not json", encoding="utf-8")
    assert _queue(tmp_path, _state()).state == {}
    (tmp_path / "actions_state.json").write_text('{"items": []}', encoding="utf-8")
    assert _queue(tmp_path, _state()).state == {}


def test_without_a_state_file_status_is_in_memory_only(tmp_path):
    queue = ActionQueue(FakeImprover(_state(proposals=[_proposal()])),
                        FakeFixEngine([]), None, None)
    assert queue.set_status("decision:c_free_gb", "closed")["ok"] is True
    assert queue.report()["items"][0]["status"] == "closed"
    assert not list(tmp_path.iterdir())


# -------------------------------------------------------------- ordering ------
def test_open_work_is_ordered_by_impact_and_closed_work_sinks(tmp_path):
    state = _state(skipped=[_skip("low", "compression_mb"),
                            _skip("high", "available_mb")],
                   proposals=[_proposal()])
    rca = {"findings": [
        {"key": "available_mb", "label": "Available", "status": "regress",
         "contribution_pct": 50.0, "cause": "steady_shortfall"},
        {"key": "compression_mb", "label": "Compression", "status": "regress",
         "contribution_pct": 5.0, "cause": "steady_shortfall"}]}
    queue = _queue(tmp_path, state, [FakeFix("low"), FakeFix("high")],
                   FakeEvolution([_candidate()]))
    queue.set_status("review:evolved_cap_cpu_offenders_cpu_pct", "closed")
    items = queue.report(rca)["items"]
    assert [i["key"] for i in items] == [
        "fix:high", "fix:low", "decision:c_free_gb",
        "review:evolved_cap_cpu_offenders_cpu_pct"]
    assert [i["priority"] for i in items] == [1, 2, 3, None]


def test_the_report_summarises_open_work_only(tmp_path):
    state = _state(skipped=[_skip("clear_standby", "available_mb")],
                   proposals=[_proposal()])
    queue = _queue(tmp_path, state, [FakeFix("clear_standby")])
    queue.set_status("decision:c_free_gb", "closed")
    report = queue.report(_rca("available_mb", share=30.0))
    assert report["open"] == 1 and report["closed"] == 1
    assert report["by_kind"] == {"fix": 1, "decision": 0, "review": 0,
                                "blocked": 0}
    assert report["by_reason"] == {"needs_elevation": 1}
    assert report["blocked_gap_share_pct"] == 30.0


def test_an_empty_queue_reports_zeroes(tmp_path):
    report = _queue(tmp_path, _state()).report()
    assert report == {"items": [], "count": 0, "open": 0, "closed": 0,
                      "recurred": 0, "action_taken": 0, "unique_keys": True,
                      "by_kind": {"fix": 0, "decision": 0, "review": 0,
                                  "blocked": 0},
                      "by_reason": {}, "blocked_gap_share_pct": 0.0,
                      "reasons": report["reasons"]}
    assert set(report["reasons"]) == set(WHY)


def test_the_module_documents_its_four_kinds():
    assert "``fix``" in actions.__doc__ and "``decision``" in actions.__doc__
    assert "``review``" in actions.__doc__ and "``blocked``" in actions.__doc__
