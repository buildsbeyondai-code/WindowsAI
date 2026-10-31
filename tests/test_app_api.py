"""Routes for the 4.0 self-evolution/autonomy API and the 4.1 surfaces: tray-quit
consent/reopen, deploy/rollback/restart, diagnostics, notifications, history,
home, unified settings, and the background hooks that raise notifications.

The app module is imported with the same fakes as test_app.py (no collector,
no state files of the real installation — conftest points the data directory
at a temporary folder)."""
import json
import time

import pytest
from tests.test_app import app
from tests.test_trayquit import FakeApi

from sysperf import codevolve as codevolve_mod
from sysperf import historydb, notify, trayquit

T = {"token": app.FIX_TOKEN}


@pytest.fixture
def client():
    return app.app.test_client()


@pytest.fixture
def notes(tmp_path, monkeypatch):
    n = notify.Notifier(tmp_path / "n.json", "http://127.0.0.1:5056/", toast_capable=False,
                        background=False)
    monkeypatch.setattr(app, "notifier", n)
    return n


@pytest.fixture
def tray(tmp_path, monkeypatch):
    api = FakeApi()
    tq = trayquit.TrayQuitter(tmp_path / "tq.json", tmp_path / "tq.jsonl",
                              api_factory=lambda: api, on_event=app._tray_event)
    monkeypatch.setattr(app, "tray_quitter", tq)
    return tq, api


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = historydb.HistoryStore(tmp_path / "h.db", ["cpu_pct"])
    monkeypatch.setattr(app, "history_store", s)
    return s


def _csrf_refused(client, url):
    assert client.post(url, json={}).status_code == 403


# ---------------------------------------------------------------- tray-quit --
def test_trayquit_status_consent_reopen_and_errors(client, tray, notes, monkeypatch):
    tq, api = tray
    _csrf_refused(client, "/api/trayquit")
    assert client.get("/api/trayquit").get_json()["apps"]
    j = client.post("/api/trayquit", json={**T, "consent": {"name": "ChatGPT",
                                                             "decision": "allow"}}).get_json()
    assert j["consent"]["chatgpt"] == "allow"
    r = client.post("/api/trayquit", json={**T, "consent": {"name": "Nope", "decision": "x"}})
    assert r.status_code == 400
    tq.recent.append({"at": time.time(), "name": "ChatGPT", "exe": "C:\\c.exe"})
    j = client.post("/api/trayquit", json={**T, "reopen": "ChatGPT"}).get_json()
    assert j["reopened"] is True and api.launched == ["C:\\c.exe"] and "status" in j
    assert client.post("/api/trayquit", json={**T, "reopen": "Zed"}).status_code == 400
    monkeypatch.setattr(app, "PLATFORM", "windows")
    j = client.post("/api/trayquit", json={**T, "busy_cpu_pct": 5}).get_json()
    assert j["busy_cpu_pct"] == 5.0 and j["checked_at"] is not None


def test_tray_events_become_notifications(notes):
    app._tray_event("quit", {"name": "ChatGPT", "freed_mb": 1500, "hidden_s": 61, "at": 5})
    app._tray_event("consent", {"name": "Slack", "mem_mb": 300, "hidden_s": 70})
    app._tray_event("other", {})
    items = notes.items()["items"]
    assert [i["kind"] for i in items] == ["consent", "action"]
    assert items[1]["link"] == "#app-control" and "1500 MB" in items[1]["body"]


def test_live_endpoint(client, monkeypatch):
    monkeypatch.setattr(app.livestats_mod, "read", lambda: {"cpu_pct": 4})
    assert client.get("/api/live").get_json() == {"cpu_pct": 4}


# ------------------------------------------------- 4.0 self-evolution routes --
def test_codevolve_read_routes(client, monkeypatch):
    assert "state" in client.get("/api/codevolve").get_json()
    h = client.get("/api/codevolve/history").get_json()
    assert set(h) == {"cycles", "candidates", "audit"}
    assert client.get("/api/codevolve/candidate/bad!id").status_code == 400
    assert client.get("/api/codevolve/candidate/c-missing").status_code == 404
    monkeypatch.setattr(app.evo_db, "candidate", lambda cid: {"id": cid})
    assert client.get("/api/codevolve/candidate/c-1").get_json() == {"id": "c-1"}


def test_codevolve_run_gated_and_started(client, monkeypatch):
    _csrf_refused(client, "/api/codevolve/run")
    monkeypatch.setattr(app.code_evolution, "gate", lambda trig, lvl: "daily limit")
    assert client.post("/api/codevolve/run", json=T).get_json() == {
        "started": False, "reason": "daily limit"}
    ran = []
    monkeypatch.setattr(app.code_evolution, "gate", lambda trig, lvl: None)
    monkeypatch.setattr(app.code_evolution, "run_cycle", lambda *a: ran.append(a))
    assert client.post("/api/codevolve/run", json=T).get_json() == {"started": True}
    for _ in range(1000):                     # generous: the suite may run on a loaded machine
        if ran:
            break
        time.sleep(0.01)
    assert ran and ran[0][0] == "manual"


def test_maintenance_report_scan_and_upgrade(client, monkeypatch):
    m = app.maintenance
    monkeypatch.setattr(m, "data", {"at": time.time() - 5, "registry": {"orphans": [
        {"user": True}]}, "app_updates": {"available": True, "apps": [
            {"id": "Git.Git", "name": "Git", "current": "1", "available": "2"}]}})
    rep = client.get("/api/maintenance").get_json()
    assert rep["findings"][0]["fix"] == "fix_registry_orphans"
    assert set(rep["fixes"]) >= set(app.MAINTENANCE_FIXES) | {"fix_registry_orphans"}
    assert rep["fixes"]["clear_browser_cache"]["executable"] is True
    assert rep["fixes"]["create_restore_point"]["executable"] is False
    listed = {f["id"] for f in client.get("/api/fixes").get_json()["fixes"]}
    assert "fix_registry_orphans" in listed
    _csrf_refused(client, "/api/maintenance/scan")
    monkeypatch.setattr(m, "scan_async", lambda: True)
    assert client.post("/api/maintenance/scan", json=T).get_json() == {"started": True}
    _csrf_refused(client, "/api/maintenance/upgrade")
    bad = client.post("/api/maintenance/upgrade", json={**T, "id": "Evil.App"})
    assert bad.status_code == 400 and "not in the last scan" in bad.get_json()["error"]
    monkeypatch.setattr(m, "start_upgrade", lambda app_id: {"started": True, "id": app_id})
    assert client.post("/api/maintenance/upgrade", json={**T, "id": "Git.Git"}).get_json() == {
        "started": True, "id": "Git.Git"}
    monkeypatch.setattr(m, "start_admin_batch", lambda: {"started": True, "id": "machine-wide"})
    assert client.post("/api/maintenance/upgrade", json={**T, "machine_wide": True}
                       ).get_json()["id"] == "machine-wide"
    monkeypatch.setattr(app, "FIX_ENABLED", False)
    assert client.post("/api/maintenance/upgrade", json={**T, "id": "Git.Git"}).status_code == 403


def test_restorepoint_routes(client, monkeypatch):
    rp = app.restore_points
    monkeypatch.setattr(rp, "installed", lambda: True)
    assert set(client.get("/api/restorepoint").get_json()) >= {"auto", "installed", "newest"}
    _csrf_refused(client, "/api/restorepoint")
    saved = rp.auto
    try:
        assert client.post("/api/restorepoint", json={**T, "action": "auto", "on": False}
                           ).get_json() == {"auto": False}
    finally:
        rp.set_auto(saved)
    assert client.post("/api/restorepoint", json={**T, "action": "x"}).status_code == 400
    ran = []
    monkeypatch.setattr(rp, "ps_exe", "pwsh")
    monkeypatch.setattr(rp, "setup", lambda: ran.append("setup"))
    monkeypatch.setattr(rp, "ensure", lambda why: ran.append(why))
    monkeypatch.setattr(rp, "remove", lambda: ran.append("remove"))
    for action in ("setup", "create", "remove"):
        assert client.post("/api/restorepoint", json={**T, "action": action}).get_json() == {
            "started": True, "action": action}
    for _ in range(1000):
        if len(ran) == 3:
            break
        time.sleep(0.01)
    assert sorted(ran) == ["a manual request", "remove", "setup"]
    monkeypatch.setattr(rp, "ps_exe", None)
    assert client.post("/api/restorepoint", json={**T, "action": "setup"}).status_code == 400


def test_codevolve_upgrade_gated_and_started(client, monkeypatch):
    _csrf_refused(client, "/api/codevolve/upgrade")
    monkeypatch.setattr(app.code_evolution, "upgrade_gate",
                        lambda lvl, manual=False: "auto-upgrade is off")
    assert client.post("/api/codevolve/upgrade", json=T).get_json() == {
        "started": False, "reason": "auto-upgrade is off"}
    ran = []
    monkeypatch.setattr(app.code_evolution, "upgrade_gate", lambda lvl, manual=False: None)
    monkeypatch.setattr(app.code_evolution, "upgrade_pass", lambda *a: ran.append(a))
    assert client.post("/api/codevolve/upgrade", json=T).get_json() == {"started": True}
    for _ in range(1000):
        if ran:
            break
        time.sleep(0.01)
    assert ran and ran[0][1] == "manual"


def test_codevolve_settings_and_llm_settings(client, monkeypatch):
    _csrf_refused(client, "/api/codevolve/settings")
    j = client.post("/api/codevolve/settings", json={**T, "evolution": {"max_per_day": 3},
                                                     "llm": {"provider": "none",
                                                             "model": "auto"}}).get_json()
    assert j["ok"] and j["settings"]["max_per_day"] == 3
    saved = json.loads(app.EVO_SETTINGS_FILE.read_text())
    assert saved["llm"]["provider"] == "none" and saved["evolution"]["max_per_day"] == 3
    for bad in ({"provider": "openai"}, {"nope": 1}, {"timeout_s": True},
                {"base_url": "http://x"}, {"model": "bad model!"}):
        r = client.post("/api/codevolve/settings", json={**T, "llm": bad})
        assert r.status_code == 400, bad
    assert client.post("/api/codevolve/settings",
                       json={**T, "evolution": {"bench_reps": 1}}).status_code == 400
    app.EVO_SETTINGS_FILE.write_text("{corrupt")
    assert client.post("/api/codevolve/settings",
                       json={**T, "llm": {"provider": "none"}}).get_json()["ok"]


def test_llm_key_and_models(client, monkeypatch):
    _csrf_refused(client, "/api/llm/key")
    assert client.post("/api/llm/key", json={**T, "key": "two words"}).status_code == 400
    monkeypatch.setattr(app.llm_mod, "store_api_key", lambda path, key: None)
    assert client.post("/api/llm/key", json={**T, "key": "sk-ant-" + "x" * 40}).get_json() == {
        "ok": True}

    class P:
        def list_models(self):
            return ["m1"]
    monkeypatch.setattr(app.code_evolution.llm, "provider", P())
    assert client.get("/api/llm/models").get_json() == {"models": ["m1"]}

    class Bad:
        def list_models(self):
            raise app.llm_mod.LLMError("auth", "no key")
    monkeypatch.setattr(app.code_evolution.llm, "provider", Bad())
    j = client.get("/api/llm/models").get_json()
    assert j["models"] == [] and j["error"] == "auth"


def test_review_route(client, monkeypatch):
    _csrf_refused(client, "/api/codevolve/review")
    monkeypatch.setattr(app.code_evolution, "review",
                        lambda cid, d: {"id": cid, "review_state": "approved"})
    assert client.post("/api/codevolve/review",
                       json={**T, "id": "c-1", "decision": "approve"}).get_json()["id"] == "c-1"

    def boom(cid, d):
        raise ValueError("only retained candidates can be reviewed")
    monkeypatch.setattr(app.code_evolution, "review", boom)
    assert client.post("/api/codevolve/review", json={**T, "id": "x"}).status_code == 400


def test_autonomy_routes(client):
    j = client.get("/api/autonomy").get_json()
    assert "level" in j and "effectiveness" in j and "runs" in j
    _csrf_refused(client, "/api/autonomy")
    assert client.post("/api/autonomy", json={**T, "level": "observe"}).get_json()[
        "level"] == "observe"
    assert client.post("/api/autonomy", json={**T, "level": "godmode"}).status_code == 400
    j = client.post("/api/autonomy", json={**T, "action": "flush_dns", "enabled": False})
    assert j.status_code == 200
    assert client.post("/api/autonomy", json={**T, "action": "nope",
                                              "enabled": True}).status_code == 400


# ---------------------------------------------------- 4.1 deploy / restart --
def test_apply_rollback_restart_routes(client, monkeypatch):
    for url in ("/api/codevolve/apply", "/api/codevolve/rollback", "/api/restart"):
        _csrf_refused(client, url)
    monkeypatch.delenv("SYSPERF_SUPERVISED", raising=False)
    monkeypatch.setattr(app.code_evolution, "apply", lambda cid: {"applied": True, "id": cid})
    assert client.post("/api/codevolve/apply", json={**T, "id": "c-1"}).get_json() == {
        "applied": True, "id": "c-1", "restarting": False}
    monkeypatch.setattr(app.code_evolution, "rollback",
                        lambda cid, reason: {"rolled_back": True, "id": cid})
    assert client.post("/api/codevolve/rollback", json=T).get_json()["restarting"] is False

    def boom(*a, **k):
        raise ValueError("nothing applied")
    monkeypatch.setattr(app.code_evolution, "apply", boom)
    monkeypatch.setattr(app.code_evolution, "rollback", boom)
    assert client.post("/api/codevolve/apply", json={**T, "id": "x"}).status_code == 400
    assert client.post("/api/codevolve/rollback", json=T).status_code == 400
    assert client.post("/api/restart", json=T).status_code == 400
    timers = []

    class FakeTimer:
        def __init__(self, delay, fn, args):
            timers.append((delay, fn, args))

        def start(self):
            pass
    monkeypatch.setenv("SYSPERF_SUPERVISED", "1")
    monkeypatch.setattr(app.threading, "Timer", FakeTimer)
    assert client.post("/api/restart", json=T).get_json() == {"restarting": True}
    assert timers[0][1] is app.os._exit and timers[0][2] == (app.RESTART_CODE,)


# ---------------------------------------------------------- diagnostics --
def test_diagnostics_and_clear(client, monkeypatch, store, tmp_path):
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    (tmp_path / "supervisor_log.jsonl").write_text('{"event": "started"}\nbad\n'
                                                   '{"event": "killed"}\n')
    codevolve_mod.append_apply_log(tmp_path, {"event": "applied", "id": "c-1"})
    app.error_buffer.emit(app.logging.LogRecord("x", 40, __file__, 1, "boom", (), None))
    j = client.get("/api/diagnostics").get_json()
    assert j["version"] == app.APP_VERSION and j["health"]["status"] in ("ok", "degraded",
                                                                           "error")
    assert any(e["message"] == "boom" for e in j["errors"])
    assert [e["event"] for e in j["supervisor_log"]] == ["killed", "started"]
    assert j["deploy"]["history"][0]["id"] == "c-1" and j["history"]["raw_samples"] == 0
    assert j["collector"]["store"] == "sqlite" and "cpu_s" in j["footprint"]
    _csrf_refused(client, "/api/diagnostics/clear")
    assert client.post("/api/diagnostics/clear", json=T).get_json() == {"cleared": True}
    assert app.error_buffer.items() == []
    (tmp_path / "supervisor_log.jsonl").unlink()
    assert app._supervisor_log() == []


# -------------------------------------------------------- notifications --
def test_notifications_routes(client, notes):
    notes.notify("health", "a", "b", key="1")
    j = client.get("/api/notifications").get_json()
    assert j["unread"] == 1 and j["settings"]["toast_capable"] is False
    _csrf_refused(client, "/api/notifications")
    sent = client.post("/api/notifications", json={**T, "test": True}).get_json()["sent"]
    assert sent["title"] == "SysPerf test notification"
    assert client.post("/api/notifications", json={**T, "read": [1]}).get_json()["marked"] == 1
    assert client.post("/api/notifications", json={**T, "read": "all"}).get_json()["unread"] == 0
    for bad in ("some", [1, "x"], None):
        assert client.post("/api/notifications",
                           json={**T, "read": bad}).status_code == 400


# --------------------------------------------------------------- history --
def test_history_route(client, store):
    now = time.time()
    store.append({"_t": now, "cpu_pct": 10})
    j = client.get("/api/history?key=cpu_pct&hours=24").get_json()
    assert j["points"][0]["avg"] == 10.0
    assert client.get("/api/history?key=nope").status_code == 400
    assert client.get("/api/history?key=cpu_pct&hours=x").status_code == 400
    assert client.get("/api/history?key=cpu_pct&hours=0").status_code == 400


# ------------------------------------------------------------------ home --
def test_home_aggregates_everything(client, monkeypatch, tray, notes, store):
    tq, _ = tray
    now = time.time()
    monkeypatch.setattr(app.improver, "recent", lambda limit=300: [
        {"t": now, "executed": [{"status": "ok", "title": "Flush DNS"},
                                {"status": "error", "id": "x"}, "junk"]},
        {"t": 0, "executed": [{"status": "ok", "title": "Old"}]}, "junk"], raising=False)
    tq.recent = [{"at": now, "name": "ChatGPT", "freed_mb": 900}, {"at": 0, "name": "Old"}]
    monkeypatch.setattr(app.evo_db, "cycles", lambda limit=20: [
        {"started": now, "state": "completed", "explanation": "rejected"},
        {"started": 0, "state": "x"}])
    monkeypatch.setattr(app.evo_db, "candidates", lambda limit=50: [
        {"review_state": "pending", "decision": "retained"},
        {"review_state": "pending", "decision": "rejected"}])
    monkeypatch.setattr(app, "_cached", lambda name, builder: {"items": [
        {"status": "open", "key": "k1", "title": "Do A", "kind": "fix"},
        {"status": "closed", "key": "k2"}, {"status": "open", "key": "k3"},
        {"status": "open", "key": "k4"}, {"status": "open", "key": "k5"}]})
    notes.notify("health", "n", "b")
    j = client.get("/api/home").get_json()
    assert [t["key"] for t in j["todo"]] == ["k1", "k3", "k4"]
    assert "command" not in j["todo"][0] and "why_headline" in j["todo"][0]
    assert [e["kind"] for e in j["today"]] == ["fix", "trayquit", "evolution"] or \
        {e["kind"] for e in j["today"]} == {"fix", "trayquit", "evolution"}
    assert j["evolution"]["awaiting_review"] == 1 and j["notifications"]["unread"] == 1
    assert j["history_keys"] == ["cpu_pct"] and "health" in j


def test_home_survives_a_failing_action_queue(client, monkeypatch, caplog):
    def boom(name, builder):
        raise RuntimeError("rca down")
    monkeypatch.setattr(app, "_cached", boom)
    assert client.get("/api/home").get_json()["todo"] == []
    assert "action queue unavailable" in caplog.text


# -------------------------------------------------------------- settings --
def test_settings_describe_apply_export_import_reset(client, monkeypatch, tmp_path, tray,
                                                    notes, store):
    cfg = app.settings_mod.ConfigFile(tmp_path / "sysperf.config.json")
    (tmp_path / "sysperf.config.json").write_text('{"port": 5056}')
    monkeypatch.setattr(app, "_config_file", cfg)
    monkeypatch.setattr(app.improver, "threshold", 80.0, raising=False)
    j = client.get("/api/settings").get_json()
    assert set(j["sections"]) == {"general", "autonomy", "evolution", "notifications",
                                  "trayquit"}
    assert j["sections"]["general"]["values"]["auto_threshold"] == 80.0
    _csrf_refused(client, "/api/settings")
    r = client.post("/api/settings", json={**T, "values": {
        "general": {"auto_threshold": 85, "sample_seconds": 120, "history_raw_days": 30},
        "notifications": {"quiet_start": 22, "quiet_end": -1, "kind_score": False},
        "trayquit": {"apps": ["ChatGPT", "Teams"], "grace_s": 90},
        "autonomy": {"level": "safe_auto"}}}).get_json()
    assert app.action_policy.level == "safe_auto"
    assert app.improver.threshold == 85.0 and store.raw_days == 30
    assert "general.sample_seconds" in r["restart_required"]
    assert json.loads((tmp_path / "sysperf.config.json").read_text())["port"] == 5056
    assert notes.settings["quiet_start"] == 22 and notes.settings["quiet_end"] is None
    assert notes.settings["kinds"]["score"] is False
    assert app.tray_quitter.settings["apps"] == ["ChatGPT", "Teams"]
    assert r["sections"]["notifications"]["values"]["quiet_end"] == -1
    bad = client.post("/api/settings", json={**T, "values": {"trayquit": {
        "apps": ["explorer"]}}})
    assert bad.status_code == 400 and "explorer" in bad.get_json()["error"]
    assert client.post("/api/settings", json={**T, "values": {"general": {
        "sample_seconds": 1}}}).status_code == 400
    exp = client.get("/api/settings/export")
    assert "attachment" in exp.headers["Content-Disposition"]
    doc = exp.get_json()
    _csrf_refused(client, "/api/settings/import")
    assert client.post("/api/settings/import", json={**T, "doc": doc}).status_code == 200
    assert client.post("/api/settings/import", json={**T, "doc": {}}).status_code == 400
    _csrf_refused(client, "/api/settings/reset")
    client.post("/api/settings/reset", json={**T, "section": "trayquit"})
    assert app.tray_quitter.settings["apps"] == list(trayquit.DEFAULT_APPS)
    assert client.post("/api/settings/reset", json={**T, "section": "zzz"}).status_code == 400
    client.post("/api/settings", json={**T, "values": {"evolution": {"max_per_day": 5}}})
    assert app.code_evolution.settings["max_per_day"] == 5


# ----------------------------------------------------------- background hooks --
def test_cycle_hook_notifies_breach_and_applied_fixes(notes, monkeypatch):
    monkeypatch.setattr(app, "_evolution_tick", lambda rec: {"ran": False})
    out = app._on_cycle({"breach": True, "score": 62.4, "threshold": 80, "summary": "s",
                         "t": 1, "executed": [{"status": "ok", "title": "Flush DNS"},
                                              {"status": "error", "id": "x"}]})
    assert out == {"ran": False}
    kinds = [i["kind"] for i in notes.items()["items"]]
    assert kinds == ["action", "score"]
    assert app._on_cycle(None) == {"ran": False}


def test_cycle_hook_never_raises(notes, monkeypatch, caplog):
    monkeypatch.setattr(app, "_evolution_tick", lambda rec: "tick")
    assert app._on_cycle({"breach": True, "score": "not a number"}) == "tick"
    assert "cycle notification failed" in caplog.text


def test_retained_hook_notifies_review(notes):
    app._on_retained({"id": "c-9", "title": "faster", "reason": "20% faster"})
    assert notes.items()["items"][0]["link"] == "#self-evolution"


def test_housekeeping_alerts_confirms_rotates_and_prunes(monkeypatch, notes, tmp_path, store):
    monkeypatch.setattr(app, "_health_state", {"problems": (), "confirmed": False,
                                               "pruned_day": None})
    minutes = iter(range(66, 100))
    monkeypatch.setattr(app.health_monitor, "check", lambda: {
        "status": "error", "problems": [f"Data collection: no update for {next(minutes)} min"]})
    monkeypatch.setattr(app.collector, "last_ok_at", None)
    app._housekeeping_tick()
    app._housekeeping_tick()   # same problem, a minute older: one alert, not one a minute
    assert [i["kind"] for i in notes.items()["items"]] == ["health"]
    confirmed = []
    monkeypatch.setattr(app.code_evolution, "confirm_apply",
                        lambda healthy: confirmed.append(healthy) or {"id": "c-1", "title": "t"})
    monkeypatch.setattr(app.collector, "last_ok_at", app.SERVICE_STARTED + 1)
    monkeypatch.setattr(app.health_monitor, "check", lambda: {"status": "ok", "problems": []})
    log = tmp_path / "sysperf-runtime.log"
    log.write_text("x\n" * 10)
    rotated = []
    monkeypatch.setattr(app.health_mod, "rotate_log", lambda name, stream: rotated.append(name))
    with log.open("a") as fh:
        monkeypatch.setattr(app.sys, "stdout", fh)
        app._housekeeping_tick()
    assert confirmed == [True] and rotated == [str(log)]
    assert notes.items()["items"][0]["title"] == "AI patch deployed"
    assert app._health_state["pruned_day"] == time.strftime("%Y-%m-%d")
    monkeypatch.setattr(app.code_evolution, "confirm_apply", lambda healthy: None)
    monkeypatch.setattr(app, "_health_state", {"problems": (), "confirmed": False,
                                               "pruned_day": time.strftime("%Y-%m-%d")})
    app._housekeeping_tick()                       # nothing pending to confirm


def test_last_sample_age(monkeypatch):
    monkeypatch.setattr(app.collector, "last_ok_at", None)
    assert app._last_sample_age() is None
    monkeypatch.setattr(app.collector, "last_ok_at", time.time() - 30)
    assert 29 <= app._last_sample_age() <= 31


def test_llm_settings_tolerate_a_corrupt_file(monkeypatch):
    app.EVO_SETTINGS_FILE.write_text("{corrupt")
    app._save_llm_settings({"provider": "none"})
    assert json.loads(app.EVO_SETTINGS_FILE.read_text())["llm"]["provider"] == "none"


def test_home_uses_the_real_action_queue_path(client):
    app._analysis_cache.clear()
    assert isinstance(client.get("/api/home").get_json()["todo"], list)


class _Stop(Exception):
    pass


def _one_iteration(monkeypatch):
    calls = []

    def sleep(s):
        calls.append(s)
        if len(calls) > 1:
            raise _Stop
    monkeypatch.setattr(app.time, "sleep", sleep)
    return calls


@pytest.mark.parametrize("loop,patch", [("_verify_actions_loop", "action_policy"),
                                        ("_housekeeping_loop", "_housekeeping_tick")])
def test_background_loops_run_and_survive_errors(monkeypatch, loop, patch, caplog):
    ran = []
    if patch == "action_policy":
        monkeypatch.setattr(app.action_policy, "verify_pending", lambda snap: ran.append(snap))
        monkeypatch.setattr(app.evo_db, "prune", lambda: ran.append("prune"))
    else:
        monkeypatch.setattr(app, "_housekeeping_tick", lambda: ran.append("tick"))
    _one_iteration(monkeypatch)
    with pytest.raises(_Stop):
        getattr(app, loop)(0)
    assert ran
    if patch == "_housekeeping_tick":
        def boom():
            raise RuntimeError("tick failed")
        monkeypatch.setattr(app, "_housekeeping_tick", boom)
        _one_iteration(monkeypatch)
        with pytest.raises(_Stop):
            app._housekeeping_loop(0)
        assert "housekeeping failed" in caplog.text


def test_log_stream_handler_is_added_once_despite_the_error_buffer():
    import logging
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        root.handlers[:] = [app.error_buffer]
        h = app._log_to_stream()
        assert type(h) is logging.StreamHandler and h in root.handlers
        assert app._log_to_stream() is h and root.level == logging.INFO
        assert sum(type(x) is logging.StreamHandler for x in root.handlers) == 1
    finally:
        root.handlers[:] = saved


# ------------------------------------------------------------ Store mode --
def test_store_routes_and_home_lists_optional_features(client, monkeypatch, tmp_path):
    from sysperf import store_mode
    sf = store_mode.StoreFeatures(
        tmp_path / "sf.json", store_mode=True, action_policy=app.action_policy,
        improver=app.improver, proc_watchdog=app.proc_watchdog,
        tray_quitter=app.tray_quitter, session_policy=app.session_policy)
    monkeypatch.setattr(app, "store_features", sf)
    monkeypatch.setattr(app.proc_watchdog, "set_enabled",
                        lambda on: setattr(app.proc_watchdog, "enabled", on))
    assert client.get("/api/home").get_json()["optional"] is None
    monkeypatch.setattr(app, "STORE_MODE", True)
    ids = [f["id"] for f in client.get("/api/home").get_json()["optional"]]
    assert "tray_quit" in ids and client.get("/api/store").get_json()["store_mode"] is True
    _csrf_refused(client, "/api/store")
    j = client.post("/api/store", json={**T, "feature": "explorer_watchdog", "on": True}).get_json()
    assert {f["id"]: f["on"] for f in j["features"]}["explorer_watchdog"] is True
    assert client.post("/api/store", json={**T, "feature": "nope"}).status_code == 400


def test_store_apply_lands_in_the_checkout_and_restart_relaunches(client, monkeypatch):
    monkeypatch.setattr(app.code_evolution, "apply", lambda cid: {"applied": True, "id": cid})
    monkeypatch.setattr(app, "STORE_MODE", True)
    j = client.post("/api/codevolve/apply", json={**T, "id": "c-1"}).get_json()
    assert j["restarting"] is False and "source checkout" in j["note"]
    timers = []

    class FakeTimer:
        def __init__(self, delay, fn, args=()):
            timers.append(fn)

        def start(self):
            pass
    monkeypatch.setattr(app.threading, "Timer", FakeTimer)
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    assert client.post("/api/restart", json=T).get_json() == {"restarting": True}
    assert timers == [app._relaunch]
    calls = []
    app._relaunch(spawn=lambda argv, close_fds: calls.append(argv),
                  exit_fn=lambda code: calls.append(code))
    assert calls == [[app.sys.executable, "--relaunch"], 0]



def test_only_an_upgraded_install_carries_its_auto_mode_into_autonomy():
    """A fresh install's default "auto" mode is not a choice anyone made; running
    `sysperf --where` once used to opt every new install into safe_auto."""
    class Imp:
        state = {"mode": "auto"}
    assert app._carried_mode(True, False, Imp()) == "auto"
    assert app._carried_mode(False, False, Imp()) is None
    assert app._carried_mode(True, True, Imp()) is None
