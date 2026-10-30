"""Full-coverage tests for maintenance.py (the scan, its findings, app updates)."""
import json
import threading
import time

import pytest

import sysperf.maintenance as mt

NOW = 1_800_000_000.0
DAY = 86400


def report(**over):
    rep = {
        "at": NOW - 60,
        "protection": {"last_restore_point": NOW - 2 * DAY, "last_servicing": NOW - DAY},
        "cleanup": {"items": [{"id": "temp", "mb": 10, "fix": "clear_temp"},
                              {"id": "msedge", "mb": 100, "fix": "clear_browser_cache"},
                              {"id": "wucache", "mb": 50, "fix": "windows_update_cleanup"}],
                    "large_files": []},
        "registry": {"orphans": [], "checked": {"startup_keys": 5, "uninstall_keys": 3}},
        "security": {"defender": {"realtime": True, "signature_age_days": 0},
                     "antivirus": [{"name": "Windows Defender", "active": True}],
                     "firewall": [{"name": "Public", "enabled": True}]},
        "crashes": {"bugchecks_30d": [], "unexpected_shutdowns_30d": 0, "app_crashes_7d": []},
        "services": [],
        "tasks": [{"name": "ok", "last_result": 0}, {"name": "never", "last_result": 0x41303}],
        "app_updates": {"available": True, "apps": []},
    }
    rep.update(over)
    return rep


class Run:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        return self.answers.pop(0) if self.answers else (0, "")


def make(tmp_path, run=None, data=None, ps="pwsh"):
    f = tmp_path / "maintenance.json"
    if data is not None:
        f.write_text(json.dumps(data))
    return mt.Maintenance(f, ps, run=run or Run(), clock=lambda: NOW)


# ------------------------------------------------------------------ helpers --
def test_quoting_and_script_command():
    assert mt.ps_quote("it's") == "'it''s'"
    cmd = mt.script_command("fix-registry", Backup="C:\\a b")
    assert cmd.startswith("& '") and cmd.endswith("-Mode fix-registry -Backup 'C:\\a b'")
    assert str(mt.SCRIPT) in cmd


def test_default_run(monkeypatch):
    class R:
        returncode, stdout, stderr = 0, "out", None
    monkeypatch.setattr(mt, "run_hidden", lambda argv, **kw: R())
    assert mt._default_run(["x"], 1) == (0, "out")

    def boom(argv, **kw):
        raise OSError("no pwsh")
    monkeypatch.setattr(mt, "run_hidden", boom)
    assert mt._default_run(["x"], 1) == (1, "OSError: no pwsh")


# ----------------------------------------------------------------- findings --
def test_a_healthy_report_has_no_findings():
    assert mt.findings(report(), NOW) == []
    assert mt.findings({"protection": {"error": "x"}}, NOW) == []


def test_every_finding_and_its_fix():
    rep = report(
        protection={"last_restore_point": None},
        crashes={"bugchecks_30d": [{"code": "0x0000009f"}, {"code": ""}],
                 "unexpected_shutdowns_30d": 4},
        cleanup={"items": [{"id": "msedge", "mb": 400}, {"id": "chrome", "mb": 200},
                           {"id": "wucache", "mb": 2048}, {"id": "temp", "mb": 1500}]},
        registry={"orphans": [{"user": True}, {"user": False}]},
        security={"defender": {"realtime": False}, "antivirus": [],
                  "firewall": [{"name": "Public", "enabled": False}]},
        tasks=[{"name": f"t{i}", "last_result": 1} for i in range(4)],
        app_updates={"available": True, "apps": [{"id": "A.B"}]})
    f = mt.findings(rep, NOW)
    assert [x["level"] for x in f] == sorted((x["level"] for x in f),
                                             key={"bad": 0, "warn": 1, "info": 2}.get)
    fixes = {x["fix"] for x in f}
    assert fixes == {"create_restore_point", "repair_system_files", "clear_browser_cache",
                     "windows_update_cleanup", "clear_temp", "fix_registry_orphans", None}
    text = " ".join(x["text"] for x in f)
    assert "No restore point" in text and "0x0000009f, unknown" in text
    assert "4 unexpected shutdowns" in text and "600 MB" in text and "2,048 MB" in text
    assert "1 start-up or uninstall entry of yours" in text
    assert "1 machine-wide entry points" in text and "Real-time virus protection is off" in text
    assert "firewall is off for the Public" in text and "t0, t1, t2…" in text
    assert "1 app(s) have an update" in text


def test_finding_variants():
    old = mt.findings(report(protection={"last_restore_point": NOW - 45 * DAY}), NOW)
    assert "45 days old" in old[0]["text"]
    stale = mt.findings(report(security={"defender": {"realtime": True,
                                                      "signature_age_days": 9}}), NOW)
    assert "9 days old" in stale[0]["text"]
    other_av = report(security={"defender": {"realtime": False},
                                "antivirus": [{"name": "Norton", "active": True}]})
    assert mt.findings(other_av, NOW) == []
    many = mt.findings(report(registry={"orphans": [{"user": True}, {"user": True},
                                                    {}, {}]}), NOW)
    assert "2 start-up or uninstall entries of yours" in many[0]["text"]
    assert "2 machine-wide entries point" in many[1]["text"]
    two = mt.findings(report(tasks=[{"name": "a", "last_result": 5}]), NOW)
    assert two[0]["text"].endswith("a.")


# ------------------------------------------------------------------ scanning --
def test_scan_success_is_cached_and_reloaded(tmp_path):
    run = Run((0, "warning\n" + json.dumps(report())))
    m = make(tmp_path, run)
    assert m.due() and m.report()["findings"] == [] and m.report()["at"] is None
    m.scan()
    assert run.calls[0][-2:] == ["-Mode", "probe"] and not m.due()
    rep = m.report()
    assert rep["age_s"] == 60 and rep["next_due"] == NOW - 60 + mt.SCAN_EVERY_S
    assert rep["error"] is None and rep["scanning"] is False
    again = make(tmp_path)
    assert again.data["at"] == NOW - 60


def test_scan_failures_keep_the_last_report(tmp_path):
    m = make(tmp_path, Run((1, "boom"), (0, "{not json")), data=report())
    m.scan()
    assert "scan failed (rc 1): boom" in m.last_error and m.data["at"] == NOW - 60
    m.scan()
    assert "rc 0" in m.last_error
    (tmp_path / "bad.json").write_text("{")
    assert mt.Maintenance(tmp_path / "bad.json", "pwsh").data == {}
    nops = make(tmp_path, ps=None)
    assert nops.scan() == nops.data and "no PowerShell" in nops.last_error


def test_only_one_scan_at_a_time(tmp_path):
    run = Run()
    m = make(tmp_path, run)
    m.scanning = True
    assert m.scan() == {} and run.calls == []
    assert m.scan_async() is False
    m.scanning = False
    started = threading.Event()
    m.scan = lambda: started.set()
    assert m.scan_async() is True and started.wait(5)


def test_loop_scans_when_due_and_stops(tmp_path):
    m = make(tmp_path)
    stop, seen = threading.Event(), []

    def scan():
        seen.append(1)
        stop.set()
    m.scan = scan
    m.loop(stop=stop, tick_s=0.01)
    assert seen == [1]
    m.due = lambda: False
    stop.clear()
    t = threading.Thread(target=m.loop, kwargs={"stop": stop, "tick_s": 0.01})
    t.start()
    time.sleep(0.05)
    stop.set()
    t.join(5)
    assert not t.is_alive() and seen == [1]


# --------------------------------------------------------------- app updates --
def apps_report():
    return report(app_updates={"available": True, "apps": [
        {"id": "Git.Git", "name": "Git", "current": "2.56", "available": "2.57"},
        {"id": "bad id; rm", "name": "x"}]})


def test_upgrade_one_listed_app(tmp_path):
    run = Run((0, "Successfully installed"))
    m = make(tmp_path, run, data=apps_report())
    assert set(m.upgradable()) == {"Git.Git"}
    with pytest.raises(ValueError, match="not in the last scan"):
        m.start_upgrade("bad id; rm")
    job = m.start_upgrade("Git.Git", background=False)
    assert job["state"] == "done" and job["rollback"].endswith("--version 2.56 --force")
    assert run.calls[0][:5] == ["winget", "upgrade", "--id", "Git.Git", "--exact"]
    assert m.upgradable() == {} and m.report()["job"]["state"] == "done"


def test_failed_upgrade_keeps_the_app_listed_and_one_job_at_a_time(tmp_path):
    m = make(tmp_path, Run((3, "error")), data=apps_report())
    assert m.start_upgrade("Git.Git", background=False)["state"] == "failed"
    assert "Git.Git" in m.upgradable()
    m.job["state"] = "running"
    with pytest.raises(ValueError, match="already upgrading Git.Git"):
        m.start_upgrade("Git.Git")
    m.job = None
    done = threading.Event()
    m._upgrade = lambda app_id: done.set()
    assert m.start_upgrade("Git.Git") == {"started": True, "id": "Git.Git"} and done.wait(5)
