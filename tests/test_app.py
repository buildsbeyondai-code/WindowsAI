"""Full-coverage tests for app.py (routes, hardening, main, __main__ guard)."""
import runpy

import flask
import pytest
from tests.test_rca import FakeStore

import sysperf.autoimprove as automod
import sysperf.collector as colmod
import sysperf.trends as trendmod


class FakeCollector:
    """Lightweight stand-in so importing app does no heavy I/O."""
    def __init__(self, scorer, store=None, shell=None):
        self.latest = {"vmmem_mb": 1}
        self.history = [1, 2, 3]
        self.error = None
        self.last_ok_at = None
        self.store, self.shell = store, shell

    def stats(self):
        return {"samples_taken": 3, "store": "sqlite"}

    def loop(self):        # referenced by main()'s thread target
        pass

    def snapshot(self):
        return {"scorecard": {"rows": []}, "latest": {"wsl": {}}, "meta": {}}


class FakeTrendStore(FakeStore):
    """Trend store stand-in: no state file is created or written by the tests."""
    def __init__(self, *a, **kw):
        super().__init__()
        self.backfilled = 0

    def backfill_from_adaptive_file(self, path):
        self.backfilled += 1
        return 0

    def summary(self, latest=None):
        return {"days_tracked": 3, "metrics": {}, "latest_seen": latest}


class FakeImprover:
    """Auto-improve controller stand-in — records calls, touches nothing."""
    def __init__(self, *a, **kw):
        self.threshold = 80.0
        self.mode = "auto"
        self.cycles = []

    def loop(self):        # referenced by main()'s thread target
        pass

    def status(self):
        return {"mode": self.mode, "cycles": len(self.cycles)}

    def recent(self, limit=10):
        return [{"at": "now", "limit": limit}]

    def cycle(self, force=False):
        self.cycles.append(force)
        return {"note": "cycle ran", "force": force}

    def set_mode(self, mode):
        if mode not in ("off", "observe", "auto"):
            raise ValueError("mode must be one of off, observe, auto")
        self.mode = mode
        return mode


# Import app with the fakes bound at module level (avoids real init + file I/O).
_orig = (colmod.Collector, trendmod.TrendStore, automod.AutoImprover)
colmod.Collector, trendmod.TrendStore, automod.AutoImprover = (
    FakeCollector, FakeTrendStore, FakeImprover)
import sysperf.app as app  # noqa: E402

colmod.Collector, trendmod.TrendStore, automod.AutoImprover = _orig


@pytest.fixture
def client():
    return app.app.test_client()


@pytest.fixture(autouse=True)
def _isolate_persisted_state(tmp_path_factory, monkeypatch):
    """Keep tests off the production state files and give each test a clean,
    known baseline: OneDrive control ENABLED and the Explorer watchdog ENABLED.

    ``app.od_control`` / ``app.proc_watchdog`` are module singletons bound to the
    real dashboard files at import; without this, a test that toggles them would
    both leak into later tests and mutate live deployment state.
    """
    d = tmp_path_factory.mktemp("state")
    monkeypatch.setattr(app.od_control, "_path", d / "onedrive_control.json")
    monkeypatch.setattr(app.proc_watchdog, "_path", d / "procwatch.json")
    app.od_control.set(False)                 # control enabled
    app.proc_watchdog.set_enabled(True)       # watchdog enabled
    app.session_policy.manual_until = 0.0
    yield


# -------------------------------------------------------------- routes --------
def test_index(monkeypatch, client):
    monkeypatch.setattr(app, "send_from_directory", lambda d, f: "INDEX")
    assert client.get("/").data == b"INDEX"


def test_metrics(client):
    r = client.get("/api/metrics")
    assert r.status_code == 200 and "scorecard" in r.get_json()


def test_metrics_redacts_identifying_network_fields(monkeypatch, client):
    """SSID/BSSID are masked in the public payload; nested and list values too."""
    monkeypatch.setattr(app.collector, "snapshot", lambda: {
        "latest": {"wifi": {"ssid": "MyHomeNet-5G", "bssid": "aa:bb:cc:dd:ee:ff",
                            "link_mbps": 780},
                   "wifi_scan": [{"ssid": "Neighbour", "rssi": -70},
                                 {"ssid": "", "rssi": -80}]},
    })
    j = client.get("/api/metrics").get_json()
    assert j["latest"]["wifi"]["ssid"] == "home-network"
    assert j["latest"]["wifi"]["bssid"] == ""
    assert j["latest"]["wifi"]["link_mbps"] == 780          # non-identifying kept
    assert j["latest"]["wifi_scan"][0]["ssid"] == "home-network"
    assert j["latest"]["wifi_scan"][1]["ssid"] == ""        # falsy left as-is


def test_health(client):
    j = client.get("/api/health").get_json()
    assert j["ok"] is True and j["samples"] == 3


def test_fixes_listing(client):
    j = client.get("/api/fixes").get_json()
    assert j["enabled"] in (True, False)
    assert j["token"] == app.FIX_TOKEN
    assert any(f["id"] == "flush_dns" for f in j["fixes"])


def test_fix_preview(client):
    j = client.post("/api/fix", json={"confirm": False}).get_json()
    assert j["mode"] == "preview" and isinstance(j["results"], list)


def test_fix_apply_ok(monkeypatch, client):
    monkeypatch.setattr(app.fix_engine, "runner", lambda s, c: (0, "mock"))
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    j = client.post("/api/fix",
                    json={"confirm": True, "token": app.FIX_TOKEN,
                          "ids": ["flush_dns"]}).get_json()
    assert j["mode"] == "apply"
    assert j["results"][0]["status"] == "ok"


def test_fix_apply_bad_token(client):
    r = client.post("/api/fix", json={"confirm": True, "token": "wrong"})
    assert r.status_code == 403 and "token" in r.get_json()["error"]


def test_fix_apply_disabled(monkeypatch, client):
    monkeypatch.setattr(app, "FIX_ENABLED", False)
    r = client.post("/api/fix", json={"confirm": True, "token": app.FIX_TOKEN})
    assert r.status_code == 403 and "disabled" in r.get_json()["error"]


# ------------------------------------------------- auto-improve routes --------
def test_trends_route(client):
    j = client.get("/api/trends").get_json()
    assert j["days_tracked"] == 3


def test_rca_route(client):
    j = client.get("/api/rca").get_json()
    assert j["threshold"] == 80.0 and j["findings"] == []


def test_analysis_endpoints_are_memoised(client, monkeypatch):
    """The tab polls these every 30s; they must not recompute every call."""
    calls = {"n": 0}
    real = app.rca_mod.analyze

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(app.rca_mod, "analyze", counting)
    app._analysis_cache.clear()
    first = client.get("/api/rca").get_json()
    second = client.get("/api/rca").get_json()          # served from the memo
    assert calls["n"] == 1 and first == second
    # a forced cycle invalidates it, since the answer may have changed
    client.post("/api/autoimprove/run", json={"token": app.FIX_TOKEN})
    client.get("/api/rca")
    assert calls["n"] == 2


def test_trends_route_is_memoised(client):
    app._analysis_cache.clear()
    assert client.get("/api/trends").get_json()["days_tracked"] == 3
    assert client.get("/api/trends").get_json()["days_tracked"] == 3


def test_autoimprove_status_route(client):
    j = client.get("/api/autoimprove?limit=3").get_json()
    assert j["mode"] == "auto" and j["recent"][0]["limit"] == 3
    # limit is clamped into 1..50
    assert client.get("/api/autoimprove?limit=999").get_json()["recent"][0]["limit"] == 50


def test_autoimprove_run(client):
    j = client.post("/api/autoimprove/run",
                    json={"token": app.FIX_TOKEN, "force": True}).get_json()
    assert j["note"] == "cycle ran" and j["force"] is True
    r = client.post("/api/autoimprove/run", json={"token": "wrong"})
    assert r.status_code == 403


def test_autoimprove_mode(client):
    j = client.post("/api/autoimprove/mode",
                    json={"token": app.FIX_TOKEN, "mode": "observe"}).get_json()
    assert j["mode"] == "observe"
    bad = client.post("/api/autoimprove/mode",
                      json={"token": app.FIX_TOKEN, "mode": "nope"})
    assert bad.status_code == 400 and "mode must be" in bad.get_json()["error"]
    assert client.post("/api/autoimprove/mode", json={"mode": "off"}).status_code == 403


# --------------------------------------------------- OneDrive control --------
def test_onedrive_status_route(client):
    j = client.get("/api/onedrive").get_json()
    assert set(j) == {"running", "known", "default_stopped", "execution_enabled",
                      "control_disabled",
                      "last_sync", "session_locked", "lock_policy"}
    assert j["known"] is False          # FakeCollector.latest has no field
    assert "enabled" in j["lock_policy"]


def test_onedrive_included_in_autoimprove(client):
    j = client.get("/api/autoimprove").get_json()
    assert "onedrive" in j and j["onedrive"]["default_stopped"] in (True, False)


def test_onedrive_toggle_ok(monkeypatch, client):
    monkeypatch.setattr(app.fix_engine, "runner", lambda s, c: (0, "mock"))
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    j = client.post("/api/onedrive",
                    json={"action": "stop", "token": app.FIX_TOKEN}).get_json()
    assert j["action"] == "stop" and j["status"] == "ok"


def test_onedrive_toggle_registers_manual_hold(monkeypatch, client):
    """An explicit click must suspend policy reconciliation, or the next tick
    (up to 15 s later) would immediately undo what the user asked for."""
    monkeypatch.setattr(app.fix_engine, "runner", lambda s, c: (0, "mock"))
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    app.session_policy.manual_until = 0.0
    assert app.session_policy.manual_hold_s() == 0
    client.post("/api/onedrive", json={"action": "start", "token": app.FIX_TOKEN})
    assert app.session_policy.manual_hold_s() > 0
    app.session_policy.manual_until = 0.0        # leave no cross-test state


def test_onedrive_toggle_bad_action_records_no_hold(monkeypatch, client):
    """A rejected action must not start a hold — nothing happened on the host."""
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    app.session_policy.manual_until = 0.0
    client.post("/api/onedrive", json={"action": "restart", "token": app.FIX_TOKEN})
    assert app.session_policy.manual_hold_s() == 0


def test_onedrive_toggle_bad_action(monkeypatch, client):
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    r = client.post("/api/onedrive",
                    json={"action": "restart", "token": app.FIX_TOKEN})
    assert r.status_code == 400 and "action must be" in r.get_json()["error"]


def test_onedrive_toggle_bad_token(client):
    r = client.post("/api/onedrive", json={"action": "stop", "token": "wrong"})
    assert r.status_code == 403


def test_onedrive_toggle_disabled(monkeypatch, client):
    monkeypatch.setattr(app, "FIX_ENABLED", False)
    r = client.post("/api/onedrive",
                    json={"action": "stop", "token": app.FIX_TOKEN})
    assert r.status_code == 403 and "disabled" in r.get_json()["error"]


def test_onedrive_control_get(client):
    j = client.get("/api/onedrive/control").get_json()
    assert set(j) == {"control_disabled", "execution_enabled"}
    assert isinstance(j["control_disabled"], bool)


def test_onedrive_control_set_toggles_and_persists(monkeypatch, client):
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    prev = app.od_control.disabled()
    try:
        j = client.post("/api/onedrive/control",
                        json={"disabled": True, "token": app.FIX_TOKEN}).get_json()
        assert j == {"control_disabled": True}
        assert app.od_control.disabled() is True
        assert client.get("/api/onedrive").get_json()["control_disabled"] is True
        j = client.post("/api/onedrive/control",
                        json={"disabled": False, "token": app.FIX_TOKEN}).get_json()
        assert j == {"control_disabled": False}
    finally:
        app.od_control.set(prev)


def test_onedrive_control_blocks_manual_toggle(monkeypatch, client):
    """With control disabled the dashboard is hands-off: Start/Stop must refuse."""
    monkeypatch.setattr(app.fix_engine, "runner", lambda s, c: (0, "mock"))
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    prev = app.od_control.disabled()
    try:
        app.od_control.set(True)
        r = client.post("/api/onedrive",
                        json={"action": "stop", "token": app.FIX_TOKEN})
        assert r.status_code == 409 and "control is disabled" in r.get_json()["error"]
    finally:
        app.od_control.set(prev)


def test_onedrive_control_bad_token(client):
    r = client.post("/api/onedrive/control",
                    json={"disabled": True, "token": "wrong"})
    assert r.status_code == 403


def test_onedrive_control_requires_disabled_field(client):
    r = client.post("/api/onedrive/control", json={"token": app.FIX_TOKEN})
    assert r.status_code == 400 and "disabled" in r.get_json()["error"]


def test_procwatch_status_route(client):
    j = client.get("/api/procwatch").get_json()
    assert j["process"] == "explorer"
    assert "enabled" in j and "threshold_pct" in j and "recent_cpu_pct" in j


def test_procwatch_toggle_persists_and_bad_token(monkeypatch, client):
    prev = app.proc_watchdog.enabled
    try:
        assert client.post("/api/procwatch", json={"enabled": False,
                           "token": app.FIX_TOKEN}).get_json() == {"enabled": False}
        assert app.proc_watchdog.enabled is False
        assert client.get("/api/procwatch").get_json()["enabled"] is False
        assert client.post("/api/procwatch", json={"enabled": True,
                           "token": app.FIX_TOKEN}).get_json() == {"enabled": True}
        r = client.post("/api/procwatch", json={"enabled": True, "token": "wrong"})
        assert r.status_code == 403
        r = client.post("/api/procwatch", json={"token": app.FIX_TOKEN})
        assert r.status_code == 400 and "enabled" in r.get_json()["error"]
    finally:
        app.proc_watchdog.set_enabled(prev)


def test_procwatch_run_noop_when_not_offending(monkeypatch, client):
    """With no sustained breach in history, a forced run reports noop, not a restart."""
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    monkeypatch.setattr(app.proc_watchdog, "fix_enabled", True)
    monkeypatch.setattr(app.proc_watchdog, "offending", lambda: None)
    j = client.post("/api/procwatch/run", json={"token": app.FIX_TOKEN}).get_json()
    assert j["status"] == "noop" and j["process"] == "explorer"


def test_procwatch_run_bad_token(client):
    r = client.post("/api/procwatch/run", json={"token": "wrong"})
    assert r.status_code == 403


def test_procwatch_run_fix_disabled(monkeypatch, client):
    monkeypatch.setattr(app, "FIX_ENABLED", False)
    r = client.post("/api/procwatch/run", json={"token": app.FIX_TOKEN})
    assert r.status_code == 403 and "disabled" in r.get_json()["error"]


def test_procwatch_run_returns_restart_result(monkeypatch, client):
    """When the watchdog acts, the run endpoint returns the restart result."""
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    monkeypatch.setattr(app.proc_watchdog, "tick",
                        lambda: {"action": "restart_explorer", "status": "ok"})
    j = client.post("/api/procwatch/run", json={"token": app.FIX_TOKEN}).get_json()
    assert j == {"action": "restart_explorer", "status": "ok"}


def test_startup_onedrive_policy_skipped_when_control_disabled(monkeypatch):
    ran = {"called": False}
    monkeypatch.setattr(app.onedrive_mod, "enforce_default",
                        lambda *a, **k: ran.update(called=True) or {"ok": 1})
    app.od_control.set(True)                       # control disabled
    try:
        assert app._startup_onedrive_policy() is None
        assert ran["called"] is False              # enforce_default never ran
    finally:
        app.od_control.set(False)


def test_appctl_surfaces_cpu_top(monkeypatch, client):
    """The instantaneous top-CPU snapshot from the probe is exposed via appctl."""
    monkeypatch.setattr(app.collector, "latest",
                        {"cpu_top": [{"n": "pwsh", "cpu_pct": 11.6, "of1core": 93.0},
                                     {"n": "searchprotocolhost", "cpu_pct": 10.0,
                                      "of1core": 80.0}]})
    j = client.get("/api/appctl").get_json()
    assert "cpu_top" in j
    assert j["cpu_top"][0]["n"] == "pwsh"
    assert j["cpu_top"][0]["cpu_pct"] == 11.6


def test_startup_onedrive_policy(monkeypatch):
    ran = {}

    def fake_enforce(runner, **kw):
        ran["kw"] = kw
        ran["runner"] = runner
        return {"ok": 1}

    monkeypatch.setattr(app.onedrive_mod, "enforce_default", fake_enforce)
    monkeypatch.setattr(app, "ONEDRIVE_LOCK_POLICY", False)
    out = app._startup_onedrive_policy()
    assert out == {"ok": 1}
    assert set(ran["kw"]) == {"enabled", "fix_enabled"}
    assert ran["runner"] is app.fix_engine.runner


def test_startup_stop_defers_to_the_lock_policy(monkeypatch):
    # With the lock policy on, a blind start-up stop would kill the OneDrive it
    # starts for a locked session; the lock policy alone decides.
    called = []
    monkeypatch.setattr(app.onedrive_mod, "enforce_default",
                        lambda *a, **k: called.append(1))
    monkeypatch.setattr(app, "ONEDRIVE_LOCK_POLICY", True)
    assert app._startup_onedrive_policy() is None and called == []


# ------------------------------------------------- App Control (top CPU) -----
def test_appctl_status_route(client):
    j = client.get("/api/appctl").get_json()
    assert set(j) >= {"available", "top", "apps", "execution_enabled", "total_cpu"}
    assert j["apps"] == []              # FakeCollector.history has no cpu_procs
    assert "good" in j["total_cpu"]     # indicator present (grey/unknown here)


def test_appctl_toggle_bad_token(client):
    r = client.post("/api/appctl", json={"action": "stop", "name": "x",
                                         "token": "wrong"})
    assert r.status_code == 403


def test_appctl_toggle_disabled(monkeypatch, client):
    monkeypatch.setattr(app, "FIX_ENABLED", False)
    r = client.post("/api/appctl", json={"action": "stop", "name": "x",
                                         "token": app.FIX_TOKEN})
    assert r.status_code == 403 and "disabled" in r.get_json()["error"]


def test_appctl_toggle_bad_action(monkeypatch, client):
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    r = client.post("/api/appctl", json={"action": "nope", "name": "x",
                                         "token": app.FIX_TOKEN})
    assert r.status_code == 400 and "action must be" in r.get_json()["error"]


def test_appctl_toggle_not_in_allowlist(monkeypatch, client):
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    r = client.post("/api/appctl", json={"action": "stop", "name": "chrome",
                                         "token": app.FIX_TOKEN})
    assert r.status_code == 400 and "controllable" in r.get_json()["error"]


def test_appctl_toggle_stop_ok(monkeypatch, client):
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    monkeypatch.setattr(app.appctl_mod, "controllable_names",
                        lambda h: {"chrome"})
    monkeypatch.setattr(app.fix_engine, "runner", lambda s, c: (0, "mock"))
    j = client.post("/api/appctl", json={"action": "stop", "name": "chrome",
                                         "token": app.FIX_TOKEN}).get_json()
    assert j["action"] == "stop" and j["name"] == "chrome" and j["status"] == "ok"


def test_appctl_wsl_shutdown_ok(monkeypatch, client):
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    monkeypatch.setattr(app.fix_engine, "runner", lambda s, c: (0, "scheduled"))
    j = client.post("/api/appctl", json={"action": "wsl-shutdown",
                                         "token": app.FIX_TOKEN}).get_json()
    assert j["action"] == "wsl-shutdown" and j["name"] == "vmmemWSL"
    assert j["status"] == "scheduled" and j["delay_s"] >= 1


def test_appctl_wsl_shutdown_needs_token(client):
    r = client.post("/api/appctl", json={"action": "wsl-shutdown",
                                         "token": "wrong"})
    assert r.status_code == 403


def test_appctl_wsl_shutdown_disabled(monkeypatch, client):
    monkeypatch.setattr(app, "FIX_ENABLED", False)
    r = client.post("/api/appctl", json={"action": "wsl-shutdown",
                                         "token": app.FIX_TOKEN})
    assert r.status_code == 403 and "disabled" in r.get_json()["error"]


def test_appctl_toggle_start_without_exe_is_400(monkeypatch, client):
    monkeypatch.setattr(app, "FIX_ENABLED", True)
    monkeypatch.setattr(app.appctl_mod, "controllable_names",
                        lambda h: {"chrome"})
    # FakeCollector.history has no observed exe -> start raises -> 400
    r = client.post("/api/appctl", json={"action": "start", "name": "chrome",
                                         "token": app.FIX_TOKEN})
    assert r.status_code == 400 and "executable" in r.get_json()["error"]


def test_hardening_headers(client):
    h = client.get("/api/health").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "SAMEORIGIN"
    assert h["Cache-Control"] == "no-store"
    assert "camera=()" in h["Permissions-Policy"] and "geolocation=()" in h["Permissions-Policy"]
    assert h["Cross-Origin-Opener-Policy"] == "same-origin"


# ------------------------------------- continuous learning + test suite -------
def test_learning_route_returns_the_full_view(monkeypatch, client):
    seen = {}

    def snapshot(limit):
        seen["limit"] = limit
        return {"kpis": {"cycles": 3}, "strategies": []}

    monkeypatch.setattr(app.learning_view, "snapshot", snapshot)
    app._analysis_cache.clear()
    j = client.get("/api/learning?limit=7").get_json()
    assert j["kpis"]["cycles"] == 3 and seen["limit"] == 7


def test_learning_route_clamps_the_limit(monkeypatch, client):
    seen = []
    monkeypatch.setattr(app.learning_view, "snapshot",
                        lambda limit: seen.append(limit) or {})
    app._analysis_cache.clear()
    client.get("/api/learning?limit=9999")
    app._analysis_cache.clear()
    client.get("/api/learning?limit=-5")
    assert seen == [200, 1]


def test_selftest_route_reports_status(client):
    j = client.get("/api/selftest").get_json()
    assert set(j) >= {"state", "gates", "gate_counts", "running"}
    assert len(j["gates"]) >= 7


def test_selftest_run_requires_the_csrf_token(client):
    r = client.post("/api/selftest/run", json={"token": "nope"})
    assert r.status_code == 403 and "CSRF" in r.get_json()["error"]


def test_selftest_run_starts_the_suite(monkeypatch, client):
    calls = []
    monkeypatch.setattr(app.self_test, "run",
                        lambda live, quick: calls.append((live, quick))
                        or {"started": True, "running": True})
    r = client.post("/api/selftest/run",
                    json={"token": app.FIX_TOKEN, "live": True, "quick": False})
    j = r.get_json()
    assert r.status_code == 200 and j["started"] is True
    assert calls == [(True, False)]
    assert "state" in j["status"]        # the fresh status rides along


# ------------------------------------------------------- human action queue ---
def test_actions_route_reports_the_queue(client):
    app._analysis_cache.clear()
    j = client.get("/api/actions").get_json()
    assert set(j) >= {"items", "count", "open", "closed", "by_kind", "by_reason",
                      "reasons", "unique_keys"}
    assert j["unique_keys"] is True
    keys = [i["key"] for i in j["items"]]
    assert len(keys) == len(set(keys))
    assert all(i["status"] in ("open", "closed") for i in j["items"])


def test_actions_status_requires_the_csrf_token(client):
    r = client.post("/api/actions/status", json={"key": "x", "status": "closed"})
    assert r.status_code == 403


def test_actions_status_rejects_an_invalid_status(client):
    r = client.post("/api/actions/status",
                    json={"token": app.FIX_TOKEN, "key": "x", "status": "maybe"})
    assert r.status_code == 400 and "open" in r.get_json()["error"]


def test_actions_status_marks_an_item_done(monkeypatch, client):
    calls = []
    monkeypatch.setattr(app.action_queue, "set_status",
                        lambda key, status, note: calls.append((key, status, note))
                        or {"ok": True, "status": status})
    j = client.post("/api/actions/status",
                    json={"token": app.FIX_TOKEN, "key": "fix:x",
                          "status": "closed", "note": "did it"}).get_json()
    assert calls == [("fix:x", "closed", "did it")] and j["ok"] is True


# ----------------------------------------------- self-authoring evolution -----
def test_evolution_route_reports_status(client):
    j = client.get("/api/evolution").get_json()
    assert set(j) >= {"generation", "kpis", "recipes", "shards", "clusters",
                      "proposals", "library", "caps", "thresholds"}


def test_evolution_run_requires_the_csrf_token(client):
    assert client.post("/api/evolution/run", json={"token": "no"}).status_code == 403


def test_evolution_run_forces_a_pass(monkeypatch, client):
    calls = []
    monkeypatch.setattr(app.evolution, "run_pass",
                        lambda force: calls.append(force)
                        or {"ran": True, "evolutions": [], "generation": 2})
    j = client.post("/api/evolution/run", json={"token": app.FIX_TOKEN}).get_json()
    assert calls == [True] and j["ran"] is True and "generation" in j["status"]


def test_promotion_requires_confirm(client):
    r = client.post("/api/evolution/promote",
                    json={"token": app.FIX_TOKEN, "id": "x"})
    assert r.status_code == 400 and "confirm" in r.get_json()["error"]


def test_promotion_requires_the_csrf_token(client):
    r = client.post("/api/evolution/promote",
                    json={"token": "no", "id": "x", "confirm": True})
    assert r.status_code == 403


def test_promotion_with_confirm_calls_the_engine(monkeypatch, client):
    seen = []
    monkeypatch.setattr(app.evolution, "promote",
                        lambda cid: seen.append(cid) or {"promoted": True})
    j = client.post("/api/evolution/promote",
                    json={"token": app.FIX_TOKEN, "id": "cand1",
                          "confirm": True}).get_json()
    assert seen == ["cand1"] and j["promoted"] is True


def test_rejection_needs_no_confirm(monkeypatch, client):
    seen = []
    monkeypatch.setattr(app.evolution, "reject",
                        lambda cid: seen.append(cid) or {"rejected": True})
    j = client.post("/api/evolution/promote",
                    json={"token": app.FIX_TOKEN, "id": "cand1",
                          "action": "reject"}).get_json()
    assert seen == ["cand1"] and j["rejected"] is True


def test_evolution_rollback_route(monkeypatch, client):
    monkeypatch.setattr(app.evolution, "rollback_last",
                        lambda: {"rolled_back": True})
    assert client.post("/api/evolution/rollback",
                       json={"token": app.FIX_TOKEN}).get_json()["rolled_back"]
    assert client.post("/api/evolution/rollback",
                       json={"token": "no"}).status_code == 403


def test_evolution_tick_runs_a_pass_when_due(monkeypatch):
    monkeypatch.setattr(app.evolution, "tick", lambda cycles: True)
    monkeypatch.setattr(app.evolution, "run_pass", lambda: {"ran": True})
    assert app._evolution_tick() == {"ran": True}


def test_evolution_tick_is_a_noop_when_not_due(monkeypatch):
    monkeypatch.setattr(app.evolution, "tick", lambda cycles: False)
    assert app._evolution_tick()["ran"] is False


def test_evolution_tick_can_be_disabled(monkeypatch):
    monkeypatch.setattr(app, "EVOLVE_EVERY", 0)
    assert "disabled" in app._evolution_tick()["reason"]


def test_evolution_tick_never_propagates_a_failure(monkeypatch):
    """INVARIANT: a bug in the enhancement must not break the remediation loop."""
    def boom(cycles):
        raise RuntimeError("evolution is broken")

    monkeypatch.setattr(app.evolution, "tick", boom)
    assert app._evolution_tick() == {
        "ran": False, "reason": "evolution tick failed (logged)"}


# ---------------------------------------- promoted-recipe registration -------
@pytest.fixture
def _isolate_remediation_map():
    """_register_promoted mutates rca.REMEDIATION in-process; keep that out of
    every other test (and out of the live service's view during a test run)."""
    import sysperf.rca as rca_mod
    original = {k: list(v) for k, v in rca_mod.REMEDIATION.items()}
    original_registry = list(app.fix_engine.registry)
    original_by_id = dict(app.fix_engine._by_id)
    yield
    rca_mod.REMEDIATION.clear()
    rca_mod.REMEDIATION.update(original)
    app.fix_engine.registry[:] = original_registry
    app.fix_engine._by_id.clear()
    app.fix_engine._by_id.update(original_by_id)


def _promoted(metric="cpu_pct", fix_id="evolved_test_cpu"):
    return [{"id": fix_id, "title": "Lower a CPU hog's priority",
             "category": "cpu", "risk": "manual", "reversible": True,
             "shell": "pwsh", "command": "", "metric": metric,
             "manual_hint": "Get-Process msedge | ...", "origin": "evolved"}]


def test_a_promoted_recipe_reaches_rca_but_is_never_executable(
        _isolate_remediation_map):
    """The point of promotion: visible to analysis, skipped by the planner."""
    import sysperf.rca as rca_mod
    assert app._register_promoted(_promoted()) == 1
    fix = app.fix_engine.get("evolved_test_cpu")
    assert fix is not None
    assert fix.risk == "manual" and fix.executable is False
    assert fix.command == ""
    assert "evolved_test_cpu" in rca_mod.REMEDIATION["cpu_pct"]


def test_registering_a_promoted_recipe_twice_is_a_noop(_isolate_remediation_map):
    assert app._register_promoted(_promoted()) == 1
    assert app._register_promoted(_promoted()) == 0


def test_a_promoted_recipe_only_surfaces_while_its_metric_is_short():
    detect = app._shortfall_detector("cpu_pct", "Lower CPU priority")
    assert detect({"scorecard": {"rows": []}}) is None
    assert detect({"scorecard": {"rows": [
        {"key": "cpu_pct", "status": "met", "label": "CPU"}]}}) is None
    reason = detect({"scorecard": {"rows": [
        {"key": "cpu_pct", "status": "regress", "label": "CPU utilisation"}]}})
    assert "CPU utilisation is regress" in reason
    assert "Lower CPU priority" in reason


def test_the_evolution_context_supplies_live_cpu_and_targets(client):
    """The context callable is what makes a generated recipe parameterisable; if
    it stops returning these keys, candidates become permanently unpromotable."""
    app._analysis_cache.clear()
    ctx = app.evolution.context()
    assert set(ctx) == {"cpu_consumers", "targets"}
    assert "consumers" in ctx["cpu_consumers"]
    assert ctx["targets"]["cpu_pct"] == 25          # from config.CRITERIA
    assert all(isinstance(k, str) for k in ctx["targets"])


def test_the_evolved_map_view_reads_through_live(monkeypatch):
    """A dict snapshot would freeze coverage at import time."""
    view = app._EvolvedMapView(app.evolution)
    monkeypatch.setattr(app.evolution, "evolved_map", lambda: {})
    assert not view and view.get("cpu_pct") is None
    assert "cpu_pct" not in view
    monkeypatch.setattr(app.evolution, "evolved_map",
                        lambda: {"cpu_pct": ["evolved_x"]})
    assert bool(view) is True
    assert view.get("cpu_pct") == ["evolved_x"]
    assert view["cpu_pct"] == ["evolved_x"]
    assert "cpu_pct" in view


# -------------------------------------------------------------- serving -------
# waitress has been a declared dependency since 2.x without ever being imported,
# so every install ran the Werkzeug development server. Both paths are covered.
def test_serve_prefers_waitress(monkeypatch):
    called = {}

    class FakeWaitress:
        @staticmethod
        def serve(wsgi, **kw):
            called.update(kw)

    monkeypatch.setitem(__import__("sys").modules, "waitress", FakeWaitress)
    app.serve(object(), "127.0.0.1", 5056)
    assert called["host"] == "127.0.0.1" and called["port"] == 5056
    assert called["ident"] == "SysPerf" and called["threads"] == 8


def test_serve_falls_back_to_flask_when_waitress_is_absent(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_waitress(name, *a, **kw):
        if name == "waitress":
            raise ImportError("no waitress")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_waitress)
    ran = {}

    class FakeApp:
        def run(self, **kw):
            ran.update(kw)

    app.serve(FakeApp(), "127.0.0.1", 5100)
    assert ran == {"host": "127.0.0.1", "port": 5100, "threaded": True,
                   "debug": False}


def test_health_reports_the_platform_and_data_directory(client):
    j = client.get("/api/health").get_json()
    assert j["platform"]["platform"] in ("windows", "wsl", "linux")
    assert "can_collect" in j["platform"]
    assert j["data_dir"]


# --------------------------------------------------------------- main ---------
def _stub_wsgi_server(monkeypatch, started=None):
    """Neutralise whichever WSGI server ``serve()`` will actually pick.

    ``serve()`` prefers waitress and falls back to Flask's development server.
    Patching only the Flask path made these tests pass by accident on any
    installation where waitress was absent -- which, as serve()'s own docstring
    notes, was every installation until it was really installed. Once present,
    waitress is used instead, so ``app.app.run`` is never called, and waitress's
    own internal ``threading.Thread(...)`` calls hit the FakeThread stub these
    tests install module-wide and raise TypeError.

    Returns the recorder so a caller can assert the app was served either way.
    """
    def _record(*_args, **_kwargs):
        if started is not None:
            started.setdefault("run", True)

    try:
        import waitress
    except ImportError:
        pass                        # Flask fallback path; nothing to stub
    else:
        monkeypatch.setattr(waitress, "serve", _record)
    return _record


def test_main(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
            self.target = target

        def start(self):
            started["thread"] = True

        def is_alive(self):
            return True

    monkeypatch.setattr(app.threading, "Thread", FakeThread)
    monkeypatch.setattr(app.app, "run", lambda **k: started.setdefault("run", True))
    monkeypatch.setattr(app, "PLATFORM", "windows")
    _stub_wsgi_server(monkeypatch, started)
    app.main()
    assert started.get("thread") and started.get("run")
    # every worker is registered with the health monitor, with freshness probes
    names = {t["name"] for t in app.health_monitor.check()["threads"]}
    assert {"collector", "auto-improve", "self-evolution", "housekeeping",
            "tray-quit"} <= names
    assert {p["name"] for p in app.health_monitor.check()["probes"]} >= {
        "collector", "auto-improve", "tray-quit"}


def test_main_guard(monkeypatch):
    # The module is re-executed from scratch here, so EVERY heavy collaborator
    # must be faked — a real TrendStore/AutoImprover would create live state
    # files in the service's working directory during the test run.
    monkeypatch.setattr(colmod, "Collector", FakeCollector)
    monkeypatch.setattr(trendmod, "TrendStore", FakeTrendStore)
    monkeypatch.setattr(automod, "AutoImprover", FakeImprover)
    monkeypatch.setattr(flask.Flask, "run", lambda self, **k: None)
    _stub_wsgi_server(monkeypatch)

    class FakeThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr("threading.Thread", FakeThread)
    # On Windows the sandbox probe runs wsl.exe, and subprocess reads its output
    # on threads, which FakeThread would break; the probe is not under test here.
    monkeypatch.setattr("sysperf.codevolve.Sandbox._detect", lambda self: "none")
    # executes the module as __main__, covering `if __name__ == "__main__": main()`
    runpy.run_module("sysperf.app", run_name="__main__", alter_sys=True)
