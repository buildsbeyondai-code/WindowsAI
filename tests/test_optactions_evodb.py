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
