"""Full-coverage tests for autoimprove.py (scan -> RCA -> fix -> verify -> learn)."""
import json

import pytest

import sysperf.autoimprove as autoimprove
import sysperf.rca as rca
from sysperf.autoimprove import AutoImprover, MODES, _typicals, normalised_delta
from sysperf.fixes import FixEngine
from tests.test_rca import FakeStore


class Clock:
    """Controllable monotonic-ish clock so cooldowns/settling are deterministic."""

    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def _row(key, status="partial", scored=120000, target=95000, progress=40.0,
         hib=False, label=None):
    return {"key": key, "label": label or key, "status": status,
            "current": scored, "scored_typical": scored, "target": target,
            "progress": progress, "shortfall": abs(scored - target),
            "higher_is_better": hib, "baseline": target * 1.3}


def _snapshot(rows, score=60.0, latest=None):
    return {"latest": latest or {"_iso": "2026-09-17T09:00:00+00:00"},
            "scorecard": {"rows": rows, "overall_improvement": score,
                          "counts": {"met": 0, "partial": len(rows),
                                     "regress": 0, "measuring": 0}}}


class FakeCollector:
    def __init__(self, snap):
        self.snap = snap
        self.latest = snap.get("latest", {})

    def snapshot(self):
        return self.snap


class FakeTrendStore(FakeStore):
    """FakeStore (from the RCA tests) plus the ingest/peak API the loop needs."""

    def __init__(self, *a, peak=False, **kw):
        super().__init__(*a, **kw)
        self.ingested = 0
        self.peak = peak

    def ingest(self, latest, scorecard, persist=True):
        self.ingested += 1
        return "2026-09-17"

    def is_peak_now(self, key="cpu_pct"):
        return self.peak


def _improver(tmp_path, snap, store=None, clock=None, **kw):
    engine = FixEngine(runner=lambda shell, cmd: (0, "MOCK OK"))
    return AutoImprover(FakeCollector(snap), store or FakeTrendStore(), engine,
                        tmp_path / "auto.json", tmp_path / "auto.jsonl",
                        clock=clock or Clock(), **kw)


# --------------------------------------------------------------- helpers ------
def test_typicals_and_delta():
    sc = {"rows": [{"key": "a", "scored_typical": 5},
                   {"key": "b", "scored_typical": None, "current": 9},
                   {"key": "c", "scored_typical": 1}]}
    assert _typicals(sc, {"a", "b"}) == {"a": 5, "b": 9}
    # lower-is-better: a drop is positive (better)
    assert normalised_delta("handles", 100, 90) == pytest.approx(0.1)
    # higher-is-better: a rise is positive
    assert normalised_delta("available_mb", 100, 110) == pytest.approx(0.1)
    assert normalised_delta("handles", None, 5) is None
    assert normalised_delta("handles", "x", 5) is None


# -------------------------------------------------------------------- io ------
def test_state_roundtrip_and_bad_state(tmp_path):
    snap = _snapshot([_row("handles")])
    imp = _improver(tmp_path, snap)
    imp.state["cycles"] = 7
    imp._save()
    again = _improver(tmp_path, snap)
    assert again.state["cycles"] == 7

    (tmp_path / "auto.json").write_text("{broken")
    broken = _improver(tmp_path, snap)
    assert broken.state["cycles"] == 0


def test_save_and_log_oserror_swallowed(tmp_path):
    snap = _snapshot([_row("handles")])
    imp = AutoImprover(FakeCollector(snap), FakeTrendStore(),
                       FixEngine(runner=lambda s, c: (0, "")),
                       tmp_path / "missing" / "s.json",
                       tmp_path / "missing" / "l.jsonl", clock=Clock())
    imp._save()
    imp._append_log({"a": 1})
    assert imp.recent() == []


def test_log_trims_and_recent_skips_bad_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(autoimprove, "AUTO_LOG_MAX_LINES", 3)
    imp = _improver(tmp_path, _snapshot([_row("handles")]))
    for i in range(5):
        imp._append_log({"i": i})
    with (tmp_path / "auto.jsonl").open("a") as fh:
        fh.write("{not json\n")
    lines = (tmp_path / "auto.jsonl").read_text().splitlines()
    assert len(lines) == 4                     # 3 kept + the corrupt one
    assert [r["i"] for r in imp.recent(10)] == [4, 3, 2]


# --------------------------------------------------------------- learning -----
def test_learn_and_suppression(tmp_path):
    imp = _improver(tmp_path, _snapshot([_row("handles")]))
    assert imp._suppressed("flush_dns", "net_remote") is None      # no entry
    imp._learn("flush_dns", "net_remote", 0.05)                    # win
    assert imp._suppressed("flush_dns", "net_remote") is None      # n < min
    imp._learn("flush_dns", "net_remote", -0.04)                   # loss
    imp._learn("flush_dns", "net_remote", 0.0)                     # unchanged
    e = imp.state["ledger"]["flush_dns"]["net_remote"]
    assert e["n"] == 3 and e["wins"] == 1 and e["losses"] == 1
    # net positive so far -> still trusted even though it has enough attempts
    assert e["ema"] > 0 and imp._suppressed("flush_dns", "net_remote") is None
    imp._learn("flush_dns", "net_remote", -0.5)
    assert "learned ineffective" in imp._suppressed("flush_dns", "net_remote")
    imp.state["ledger"]["x"] = {"k": {"ema": None, "n": 9, "wins": 0,
                                      "losses": 0, "last": None}}
    assert imp._suppressed("x", "k") is None                       # ema unknown


# ----------------------------------------------------------- verification -----
def test_verify_waiting_then_measured(tmp_path):
    clock = Clock()
    rows = [_row("net_remote", scored=30, target=30, progress=100.0,
                 status="met")]
    imp = _improver(tmp_path, _snapshot(rows, score=95.0), clock=clock)
    imp.state["pending"] = {"at": clock.t, "actions": ["flush_dns"],
                            "keys": ["net_remote"],
                            "before": {"net_remote": 40, "gone": 5}}
    waiting = imp._verify(_snapshot(rows)["scorecard"])
    assert waiting["status"] == "waiting"

    clock.advance(imp.settle_s + 1)
    done = imp._verify(_snapshot(rows)["scorecard"])
    assert done["status"] == "verified"
    assert done["verdicts"]["net_remote"] == "improved"
    assert "gone" not in done["metrics"]            # no post value -> skipped
    assert imp.state["pending"] is None
    assert imp.state["ledger"]["flush_dns"]["net_remote"]["wins"] == 1
    assert imp._verify({}) is None                  # nothing pending


def test_verify_reports_worsened_and_unchanged(tmp_path):
    clock = Clock()
    rows = [_row("net_remote", scored=60, target=30, progress=10.0),
            _row("handles", scored=120000, progress=40.0)]
    imp = _improver(tmp_path, _snapshot(rows), clock=clock)
    imp.state["pending"] = {"at": clock.t - 10_000, "actions": ["flush_dns"],
                            "keys": ["net_remote", "handles"],
                            "before": {"net_remote": 30, "handles": 120000}}
    out = imp._verify(_snapshot(rows)["scorecard"])
    assert out["verdicts"] == {"net_remote": "worsened", "handles": "unchanged"}


# ---------------------------------------------------------------- policy ------
def test_cooling(tmp_path):
    clock = Clock()
    imp = _improver(tmp_path, _snapshot([_row("handles")]), clock=clock)
    assert imp._cooling("flush_dns") == 0.0
    imp.state["cooldowns"]["flush_dns"] = clock.t + 600
    assert imp._cooling("flush_dns") == 600
    clock.advance(601)
    assert imp._cooling("flush_dns") == 0.0


def test_plan_gates(tmp_path):
    clock = Clock()
    rows = [_row("net_remote", scored=90, target=30, progress=5.0),
            _row("c_free_gb", scored=150, target=180, progress=20.0, hib=True),
            _row("handles", scored=200000, progress=10.0),
            _row("startup_apps", scored=18, target=10, progress=15.0)]
    store = FakeTrendStore(score_direction="degrading", score_slope=-0.9,
                           score_days=10)
    imp = _improver(tmp_path, _snapshot(rows), store=store, clock=clock,
                    allow_risks=("safe",), max_actions=1)
    report = rca.analyze(imp.collector.snapshot(), store,
                                       imp.fix_engine, imp.threshold)
    decisions = imp.plan(report)
    by_id = {d["id"]: d for d in decisions}
    # budget is 1 + 1 (long-term score decline) = 2 safe actions
    assert sum(1 for d in decisions if d["eligible"]) == 2
    assert by_id["restart_explorer"]["skip_reason"].startswith("risk 'caution'")
    assert by_id["trim_startup"]["skip_reason"] == "manual action required"

    # cooldown and learned-ineffective paths
    imp.state["cooldowns"]["flush_dns"] = clock.t + 1800
    for _ in range(4):
        imp._learn("clear_temp", "c_free_gb", -0.2)
    d2 = {d["id"]: d for d in imp.plan(report)}
    assert "cooling down" in d2["flush_dns"]["skip_reason"]
    assert "learned ineffective" in d2["clear_temp"]["skip_reason"]


def test_plan_skips_non_actionable_findings(tmp_path):
    """A chronic (target-unreachable) shortfall is proposed, never remediated."""
    store = FakeTrendStore({"net_remote": {"chronic": True, "settled_days": 9,
                                           "max": 45.0}})
    rows = [_row("net_remote", scored=45, target=30, progress=5.0)]
    imp = _improver(tmp_path, _snapshot(rows), store=store)
    rec = imp.cycle()
    assert rec["findings"][0]["cause"] == "chronic_unreachable"
    assert rec["planned"] == [] and rec["executed"] == []
    assert [p["kind"] for p in imp.state["proposals"]] == ["retune_target"]


def test_plan_budget_exhausted(tmp_path):
    """Two safe candidates, budget of one -> the second is deferred."""
    rows = [_row("net_remote", scored=90, target=30, progress=5.0),
            _row("c_free_gb", scored=150, target=180, progress=20.0, hib=True)]
    store = FakeTrendStore()                       # stable score -> no bonus
    imp = _improver(tmp_path, _snapshot(rows), store=store, max_actions=1)
    report = rca.analyze(imp.collector.snapshot(), store,
                                       imp.fix_engine, imp.threshold)
    d = {x["id"]: x for x in imp.plan(report)}
    assert d["flush_dns"]["eligible"] is True
    assert "budget reached (1/cycle)" in d["clear_temp"]["skip_reason"]


def test_plan_unknown_id_and_peak_deferral(tmp_path, monkeypatch):
    monkeypatch.setitem(rca.REMEDIATION, "handles",
                        ["no_such_fix", "restart_explorer"])
    rows = [_row("handles", scored=200000, progress=10.0)]
    store = FakeTrendStore(peak=True)
    imp = _improver(tmp_path, _snapshot(rows), store=store,
                    allow_risks=("safe", "caution"))
    # fix_engine=None -> remediations are not filtered by live detection, so the
    # unknown id survives into the plan and exercises the lookup guard.
    report = rca.analyze(imp.collector.snapshot(), store, None,
                                       imp.threshold)
    d = {x["id"]: x for x in imp.plan(report)}
    assert d["no_such_fix"]["skip_reason"] == "unknown remediation id"
    assert "peak usage hours" in d["restart_explorer"]["skip_reason"]


def test_plan_preventive_when_not_breaching(tmp_path):
    store = FakeTrendStore({"c_free_gb": {"direction": "degrading",
                                          "pct_per_day": -1.2,
                                          "days_to_target": 3.0}})
    rows = [_row("c_free_gb", status="met", scored=181, target=180,
                 progress=100.0, hib=True)]
    imp = _improver(tmp_path, _snapshot(rows, score=95.0), store=store)
    rec = imp.cycle()
    assert rec["breach"] is False
    assert [r["id"] for r in rec["executed"]] == ["clear_temp"]
    assert "preventive" in rec["planned"][0]["why"]


def test_plan_dedupes_repeated_ids(tmp_path):
    store = FakeTrendStore()
    rows = [_row("available_mb", scored=6000, target=12000, progress=10.0,
                 hib=True),
            _row("compression_mb", scored=5000, target=2750, progress=20.0)]
    imp = _improver(tmp_path, _snapshot(rows), store=store,
                    allow_risks=("safe", "caution", "manual"))
    report = rca.analyze(imp.collector.snapshot(), store,
                                       imp.fix_engine, imp.threshold)
    ids = [d["id"] for d in imp.plan(report)]
    assert ids.count("clear_standby") == 1          # mapped from both metrics


# ------------------------------------------------------------ escalation ------
def test_escalation_proposal(tmp_path):
    store = FakeTrendStore({"net_remote": {"direction": "degrading",
                                           "pct_per_day": 1.8}})
    rows = [_row("net_remote", scored=90, target=30, progress=5.0)]
    imp = _improver(tmp_path, _snapshot(rows), store=store)
    for _ in range(4):
        imp._learn("flush_dns", "net_remote", -0.1)
    rec = imp.cycle()
    kinds = {p["kind"] for p in imp.state["proposals"]}
    assert "escalate_manual" in kinds
    assert rec["note"] == "no eligible action this cycle"
    # a second identical cycle updates the existing proposal instead of adding
    imp.cycle()
    assert len([p for p in imp.state["proposals"]
                if p["kind"] == "escalate_manual"]) == 1
    assert imp.state["proposals"][0]["seen"] == 2


def test_escalation_ignores_other_causes(tmp_path):
    store = FakeTrendStore()                       # stable -> steady_shortfall
    rows = [_row("net_remote", scored=90, target=30, progress=5.0)]
    imp = _improver(tmp_path, _snapshot(rows), store=store)
    for _ in range(4):
        imp._learn("flush_dns", "net_remote", -0.1)
    imp.cycle()
    assert imp.state["proposals"] == []


# ----------------------------------------------------------------- cycle ------
def test_cycle_no_scored_sample(tmp_path):
    imp = _improver(tmp_path, {"latest": {}, "scorecard": {"rows": []}})
    rec = imp.cycle()
    assert rec["score"] is None and "no scored sample" in rec["note"]
    assert imp.store.ingested == 0                 # nothing polluted the rollup


def test_cycle_mode_off_scans_only(tmp_path):
    rows = [_row("net_remote", scored=90, target=30, progress=5.0)]
    imp = _improver(tmp_path, _snapshot(rows), mode="off")
    rec = imp.cycle()
    assert rec["executed"] == [] and rec["planned"] == []
    assert imp.store.ingested == 1 and "off" in rec["note"]


def test_cycle_observe_plans_without_executing(tmp_path):
    rows = [_row("net_remote", scored=90, target=30, progress=5.0)]
    imp = _improver(tmp_path, _snapshot(rows), mode="observe")
    rec = imp.cycle()
    assert rec["executed"] == [] and rec["planned"][0]["eligible"] is True
    assert "observe mode" in rec["note"]


def test_cycle_execution_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(autoimprove, "FIX_ENABLED", False)
    rows = [_row("net_remote", scored=90, target=30, progress=5.0)]
    imp = _improver(tmp_path, _snapshot(rows))
    rec = imp.cycle()
    assert rec["executed"] == [] and "execution disabled" in rec["note"]


def test_cycle_applies_then_verifies_and_learns(tmp_path):
    clock = Clock()
    rows = [_row("net_remote", scored=90, target=30, progress=5.0)]
    imp = _improver(tmp_path, _snapshot(rows), clock=clock)
    first = imp.cycle()
    assert [r["id"] for r in first["executed"]] == ["flush_dns"]
    assert imp.state["cooldowns"]["flush_dns"] == clock.t + imp.cooldown_s
    assert "applied 1 remediation" in first["note"]

    clock.advance(imp.settle_s + 1)
    imp.collector.snap = _snapshot(
        [_row("net_remote", scored=30, target=30, progress=100.0,
              status="met")], score=95.0)
    second = imp.cycle()
    assert second["verification"]["verdicts"]["net_remote"] == "improved"
    assert imp.state["ledger"]["flush_dns"]["net_remote"]["ema"] > 0
    assert imp.state["cycles"] == 2
    log = [json.loads(l) for l in
           (tmp_path / "auto.jsonl").read_text().splitlines()]
    assert len(log) == 2 and log[0]["breach"] is True


def test_cycle_force_overrides_off_mode(tmp_path):
    rows = [_row("net_remote", scored=90, target=30, progress=5.0)]
    imp = _improver(tmp_path, _snapshot(rows), mode="off")
    rec = imp.cycle(force=True)
    assert [r["id"] for r in rec["executed"]] == ["flush_dns"]


# ---------------------------------------------------------------- runtime -----
def test_set_mode(tmp_path):
    imp = _improver(tmp_path, _snapshot([_row("handles")]))
    assert imp.set_mode("observe") == "observe"
    assert imp.state["mode"] == "observe"
    with pytest.raises(ValueError):
        imp.set_mode("nope")
    assert set(MODES) == {"off", "observe", "auto"}


def test_status(tmp_path):
    clock = Clock()
    rows = [_row("net_remote", scored=90, target=30, progress=5.0)]
    imp = _improver(tmp_path, _snapshot(rows), clock=clock)
    st = imp.status()
    assert st["next_scan_in_s"] is None and st["cycles"] == 0
    imp.cycle()
    clock.advance(60)
    st2 = imp.status()
    assert st2["next_scan_in_s"] == pytest.approx(imp.interval - 60)
    # A forced cycle must not move the scheduled scan: the countdown follows the
    # loop's own tick once the loop has run.
    imp.loop_tick = clock.t
    imp.cycle(force=True)
    assert imp.status()["next_scan_in_s"] == pytest.approx(imp.interval)
    assert st2["cooldowns"]["flush_dns"] > 0
    assert st2["threshold"] == imp.threshold and st2["mode"] == "auto"
    assert st2["last"]["score"] == 60.0


# ------------------------------------------------------------ on_cycle hook ----
# The evolution layer advances its APE counter from this hook, so the loop must
# call it exactly once per completed cycle — and must survive it failing.
def test_on_cycle_hook_receives_the_finished_record(tmp_path):
    seen = []
    imp = _improver(tmp_path, _snapshot([_row("handles")]),
                    on_cycle=lambda rec: seen.append(rec) or {"ran": True})
    record = imp.cycle(force=True)
    assert len(seen) == 1
    assert seen[0] is record
    assert seen[0]["score"] == record["score"]
    assert record["evolution"] == {"ran": True}


def test_a_failing_on_cycle_hook_does_not_break_the_cycle(tmp_path):
    def boom(record):
        raise RuntimeError("evolution exploded")

    imp = _improver(tmp_path, _snapshot([_row("handles")]), on_cycle=boom)
    record = imp.cycle(force=True)
    assert "evolution" not in record        # the hook's value is simply absent
    assert record["score"] is not None      # ...and the cycle still completed
    assert imp.state["cycles"] == 1


def test_no_hook_leaves_the_record_untouched(tmp_path):
    imp = _improver(tmp_path, _snapshot([_row("handles")]))
    assert "evolution" not in imp.cycle(force=True)
