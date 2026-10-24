"""4.0 action policy and the durable learning store."""
import sqlite3
import time

import pytest

import sysperf.evodb as evodb
import sysperf.optactions as optactions


@pytest.fixture
def db(tmp_path):
    return evodb.EvoDB(tmp_path / "x.db")


def test_default_is_observe_and_nothing_runs_unattended(tmp_path, db):
    p = optactions.ActionPolicy(tmp_path / "a.json", db)
    assert p.level == "observe" and p.improver_mode() == "observe"
    assert "observe" in p.unattended_block("flush_dns")


def test_3x_auto_users_keep_only_low_risk_reversible_actions(tmp_path, db):
    p = optactions.ActionPolicy(tmp_path / "a.json", db, improver_mode="auto")
    assert p.level == "safe_auto"
    assert set(p.state["enabled"]) == {"flush_dns", "trim_working_sets", "clear_standby"}
    assert p.unattended_block("flush_dns") is None
    assert "irreversible" in p.unattended_block("clear_temp")
    assert "needs confirmation" in p.unattended_block("restart_explorer")


def test_opt_in_is_required_and_risky_actions_cannot_be_opted_in(tmp_path, db):
    p = optactions.ActionPolicy(tmp_path / "a.json", db)
    p.set_level("safe_auto")
    assert "not enabled" in p.unattended_block("flush_dns")
    p.set_enabled("flush_dns", True)
    assert p.unattended_block("flush_dns") is None
    for risky in ("empty_recycle_bin", "clear_temp", "restart_wsl", "trim_startup"):
        with pytest.raises(ValueError):
            p.set_enabled(risky, True)
    with pytest.raises(ValueError):
        p.set_enabled("disable_defender", True)
    with pytest.raises(ValueError):
        p.set_level("god_mode")
    p.set_level("off")
    assert p.improver_mode() == "off" and p.unattended_block("flush_dns")
    assert optactions.ActionPolicy(tmp_path / "a.json", db).level == "off"   # persisted


@pytest.mark.parametrize("text", [
    "Set-MpPreference -DisableRealtimeMonitoring $true", "netsh advfirewall set allprofiles state off",
    "Stop-Service wuauserv", "wevtutil cl System", "Remove-Item C:\\Users\\x -Recurse",
    "bcdedit /set", "reg delete HKLM\\SYSTEM\\x", "Stop-Process -Name lsass"])
def test_forbidden_operations_are_recognised(text):
    assert optactions.never_auto(text)


def test_registry_covers_every_fix_and_is_complete():
    import sysperf.fixes as fixes
    ids = {f.id for f in fixes.FixEngine().fixes} if hasattr(fixes.FixEngine(), "fixes") else None
    for s in optactions.REGISTRY.values():
        assert s.description and s.preconditions and s.validation and s.rollback
        assert s.risk in ("low", "medium", "high") and s.permissions in ("user", "admin")
        assert not optactions.never_auto(s.execution)
    if ids:
        assert ids <= set(optactions.REGISTRY), ids - set(optactions.REGISTRY)


def test_before_after_verdicts_and_effectiveness(tmp_path, db):
    now = [1000.0]
    p = optactions.ActionPolicy(tmp_path / "a.json", db, clock=lambda: now[0])
    p.record_executions(["trim_working_sets"], [{"id": "trim_working_sets", "ok": True}],
                        {"latest": {"available_mb": 4000}})
    p.record_executions(["flush_dns"], [{"id": "flush_dns", "ok": True}],
                        {"latest": {"net_dns_ms": 50}})
    db.conn.execute("UPDATE action_runs SET ts=?", (now[0],))
    assert p.verify_pending({"latest": {"available_mb": 5000}}) == []   # not settled
    now[0] += 3600
    judged = p.verify_pending({"latest": {"available_mb": 5000, "net_dns_ms": 49.5}})
    assert {j["action"]: j["verdict"] for j in judged} == {
        "trim_working_sets": "improved", "flush_dns": "no_change"}
    eff = {e["id"]: e for e in p.effectiveness()}
    assert eff["trim_working_sets"]["median_change_pct"] == 25.0
    assert eff["flush_dns"]["no_change"] == 1


def test_migrations_audit_chain_and_tamper_detection(tmp_path):
    d = evodb.EvoDB(tmp_path / "x.db")
    assert d.version() == len(evodb.MIGRATIONS)
    for i in range(5):
        d.audit("t", "e", {"i": i})
    assert d.verify_audit() == {"ok": True, "checked": 5, "head": d.verify_audit()["head"]}
    d.conn.execute("UPDATE audit SET payload='{\"i\": 99}' WHERE seq=3")
    d.conn.commit()
    v = d.verify_audit()
    assert not v["ok"] and v["broken_at"] == 3
    # reopening keeps data and does not re-run migrations
    assert evodb.EvoDB(tmp_path / "x.db").version() == len(evodb.MIGRATIONS)


def test_corrupt_database_is_quarantined_not_fatal(tmp_path):
    f = tmp_path / "x.db"
    f.write_bytes(b"this is not sqlite" * 100)
    d = evodb.EvoDB(f)
    assert d.version() == len(evodb.MIGRATIONS)
    assert list(tmp_path.glob("x.corrupt-*"))


def test_retention_prunes_but_keeps_audit(tmp_path, monkeypatch):
    monkeypatch.setitem(evodb.RETENTION, "action_runs", 3)
    d = evodb.EvoDB(tmp_path / "x.db")
    for i in range(6):
        d.record_action(action_id="a", mode="m", authorized=1)
        d.audit("t", "e", {"i": i})
    assert d.prune()["action_runs"] == 3
    assert len(d.action_runs()) == 3 and d.verify_audit()["checked"] == 6


def test_cycle_bookkeeping(tmp_path):
    d = evodb.EvoDB(tmp_path / "x.db")
    d.start_cycle("c1", "manual")
    d.update_cycle("c1", state="failed", finished=time.time())
    assert d.cycles()[0]["state"] == "failed" and d.cycles_since(0) == 1
    assert d.recover() == 0
    with pytest.raises(sqlite3.IntegrityError):
        d.add_candidate({"id": "x", "cycle_id": "missing", "created": 1})


def test_update_cycle_without_fields_and_filtered_action_runs(tmp_path):
    d = evodb.EvoDB(tmp_path / "x.db")
    d.start_cycle("c1", "manual")
    d.update_cycle("c1")                                   # no-op, no SQL error
    d.record_action(action_id="a", mode="m", authorized=1)
    d.record_action(action_id="b", mode="m", authorized=1)
    assert [r["action_id"] for r in d.action_runs(action_id="b")] == ["b"]


def test_real_fix_engine_results_are_measured(tmp_path, db):
    """FixEngine.apply reports {"status": "ok"}; such runs must be judged."""
    now = [1000.0]
    p = optactions.ActionPolicy(tmp_path / "a.json", db, clock=lambda: now[0])
    p.record_executions(["trim_working_sets"],
                        [{"id": "trim_working_sets", "status": "ok", "output": "done"}],
                        {"latest": {"available_mb": 4000}})
    p.record_executions(["flush_dns"], [{"id": "flush_dns", "status": "error",
                                         "output": "denied"}], {"latest": {}})
    db.conn.execute("UPDATE action_runs SET ts=?", (now[0],))
    db.conn.commit()
    now[0] += p.SETTLE_S + 1
    judged = p.verify_pending({"latest": {"available_mb": 4800}})
    assert [j["verdict"] for j in judged] == ["improved"]
    assert {r["action_id"]: r["ok"] for r in db.action_runs()} == {
        "trim_working_sets": 1, "flush_dns": 0}


def test_verify_skips_unsettled_unmeasurable_and_judged_runs(tmp_path, db):
    now = [1000.0]
    p = optactions.ActionPolicy(tmp_path / "a.json", db, clock=lambda: now[0])
    p.record_executions(["trim_working_sets"], [{"id": "trim_working_sets", "status": "ok"}],
                        {"latest": {"available_mb": 4000}})
    db.conn.execute("UPDATE action_runs SET ts=?", (now[0],))
    db.conn.commit()
    assert p.verify_pending({"latest": {"available_mb": 9000}}) == []      # not settled
    now[0] += p.SETTLE_S + 1
    assert p.verify_pending({"latest": {}}) == []                          # no after value
    assert len(p.verify_pending({"latest": {"available_mb": 4010}})) == 1  # no_change
    assert p.verify_pending({"latest": {"available_mb": 9000}}) == []      # already judged


def test_no_database_means_no_history(tmp_path):
    p = optactions.ActionPolicy(tmp_path / "a.json", None)
    p.record_executions(["flush_dns"], [{"id": "flush_dns", "status": "ok"}], {})
    assert p.verify_pending({}) == [] and p.effectiveness() == []


def test_unregistered_action_and_describe(tmp_path, db):
    p = optactions.ActionPolicy(tmp_path / "a.json", db)
    p.set_level("safe_auto")
    assert p.unattended_block("made_up") == "not in the 4.0 action registry"
    d = p.describe()
    assert d["level"] == "safe_auto" and set(d["levels"]) >= {"off", "observe"}
    row = next(a for a in d["actions"] if a["id"] == "flush_dns")
    assert {"auto_eligible", "enabled", "risk"} <= set(row)
