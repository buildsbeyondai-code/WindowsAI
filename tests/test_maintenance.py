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
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    monkeypatch.setattr(mt, "run_tree", lambda argv, t: (0, "out", False))
    assert mt._default_run(["x"], 1) == (0, "out")
    monkeypatch.setattr(mt, "run_tree", lambda argv, t: (1, "half", True))
    assert mt._default_run(["x"], 30) == (
        1, "half\ntimed out after 30 s; stopped it and the installers it started")

    def boom(argv, t):
        raise OSError("no pwsh")
    monkeypatch.setattr(mt, "run_tree", boom)
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


# ------------------------------------------------- 5.4: automatic app updates --
def test_apps_update_themselves_after_each_scan(tmp_path):
    rep = report(app_updates={"available": True, "apps": [
        {"id": "Git.Git", "name": "Git", "current": "1", "available": "2"},
        {"id": "Python.Python.3.12", "name": "Python", "current": "3.12.1", "available": "3.12.2"},
        {"id": "OpenAI.ChatGPT", "name": "ChatGPT", "current": "1", "available": "2"},
        {"id": "Zoom.Zoom", "name": "Zoom", "current": "5", "available": "6"},
        {"id": "HP.Opt", "name": "HP", "current": "1", "available": "2", "scope": "machine"}]})
    run = Run((0, "ok"), (0, "No installed package found"), (7, "installer failed"))
    m = make(tmp_path, run, data=rep)
    seen = []
    m.before_change = lambda why: seen.append(why) or {"state": "recent"}
    out = m.auto_update()
    # component-managed and machine-wide (would wait on a UAC prompt) skipped
    assert [j["id"] for j in out] == ["OpenAI.ChatGPT", "Zoom.Zoom"]
    assert [j["state"] for j in out] == ["done", "failed"]
    assert run.calls[1][:4] == ["winget", "list", "--id", "OpenAI.ChatGPT"]
    assert seen == ["updating ChatGPT", "updating Zoom"] and out[0]["restore_point"]
    assert m.failed == {"Zoom.Zoom": "6"}
    assert m.auto_update() == []                  # the failed version is not retried
    r = m.report()
    assert r["failed"] == {"Zoom.Zoom": "6"} and r["managed"] == ["Git.Git", "Python.Python.3.12"]
    assert r["admin"] == ["HP.Opt"]
    assert r["auto_update"] is False
    m.job = {"id": "x", "state": "running"}
    m.failed = {}
    assert m.auto_update() == []                  # a manual update is running: wait


def test_loop_runs_the_automatic_pass_once_per_scan(tmp_path):
    m = make(tmp_path, data=report())
    m.due = lambda: False
    m.auto_fn = lambda: True
    calls, stop = [], threading.Event()
    m.auto_update = lambda: (calls.append(1), stop.set())
    m.loop(stop=stop, tick_s=0.01)
    stop.clear()
    m.auto_update = lambda: calls.append(2)
    t = threading.Thread(target=m.loop, kwargs={"stop": stop, "tick_s": 0.01})
    t.start()
    time.sleep(0.05)
    stop.set()
    t.join(5)
    assert calls == [1]                           # same scan: not again


def test_the_helpers_restore_point_overrides_the_event_log(tmp_path):
    m = make(tmp_path, data=report(protection={"last_restore_point": None}))
    assert m.report()["findings"][0]["fix"] == "create_restore_point"
    m.restore_newest = lambda: NOW - 3600
    rep = m.report()
    assert rep["protection"]["last_restore_point"] == NOW - 3600 and rep["findings"] == []
    m.restore_newest = lambda: None
    assert m.report()["findings"]


def test_an_open_app_waits_instead_of_counting_as_updated(tmp_path):
    # winget exits 0 after staging an MSIX that Windows would not swap in (0x80073D02)
    run = Run((0, "Successfully installed"), (0, "Git  Git.Git  2.56  2.57  winget"))
    m = make(tmp_path, run, data=apps_report())
    job = m.start_upgrade("Git.Git", background=False)
    assert job["state"] == "waiting" and "once the app is closed" in job["output"]
    assert "Git.Git" in m.upgradable() and m.failed == {}


def admin_report():
    return report(app_updates={"available": True, "apps": [
        {"id": "HP.Opt", "name": "HP", "current": "1", "available": "2", "scope": "machine"},
        {"id": "VC.Redist", "name": "VC", "current": "1", "available": "3", "scope": "machine"},
        {"id": "Zoom.Zoom", "name": "Zoom", "current": "5", "available": "6", "scope": "user"}]})


def test_machine_wide_apps_update_after_one_prompt(tmp_path):
    m = make(tmp_path, data=admin_report())
    res = m.data_dir / "admin_upgrade.txt"

    def elevated(argv, timeout):
        assert "-Verb RunAs" in argv[-1] and timeout == 2 * mt.UPGRADE_TIMEOUT_S
        res.write_text("HP.Opt=0\nVC.Redist=-1978335189\nnoise\nHP.Opt=x\n")
        return 0, ""
    m.run = elevated
    seen = []
    m.before_change = lambda why: seen.append(why) or {"state": "created"}
    job = m.start_admin_batch(background=False)
    assert job["state"] == "failed" and job["results"] == {"HP.Opt": 1, "VC.Redist": -1978335189}
    assert seen == ["updating 2 machine-wide apps"] and job["restore_point"]
    assert m.failed == {"HP.Opt": "2", "VC.Redist": "3"}


def test_machine_wide_batch_all_good_declined_and_refused(tmp_path):
    m = make(tmp_path, data=admin_report())
    m.run = lambda argv, t: ((m.data_dir / "admin_upgrade.txt").write_text("HP.Opt=0\nVC.Redist=0"), (0, ""))[1]
    assert m.start_admin_batch(background=False)["state"] == "done"
    assert set(m.upgradable()) == {"Zoom.Zoom"}
    with pytest.raises(ValueError, match="no machine-wide app"):
        m.start_admin_batch()
    m = make(tmp_path / "d", data=admin_report())
    m.run = lambda argv, t: (mt.DECLINED, "The operation was canceled by the user.")
    job = m.start_admin_batch(background=False)
    assert job["state"] == "failed" and "declined" in job["output"] and m.failed == {}
    m.job = {"id": "x", "state": "running"}
    with pytest.raises(ValueError, match="already upgrading"):
        m.start_admin_batch()
    m.job = None
    done = threading.Event()
    m._admin_batch = lambda ids: done.set()
    assert m.start_admin_batch()["ids"] == ["HP.Opt", "VC.Redist"] and done.wait(5)
    with pytest.raises(ValueError, match="need Windows"):
        make(tmp_path / "w", data=admin_report(), ps=None).start_admin_batch()


def test_the_batch_script_is_one_encoded_command(tmp_path):
    import base64
    m = make(tmp_path, data=admin_report())
    seen = []
    m.run = lambda argv, t: (seen.append(argv), (0, ""))[1]
    assert m.start_admin_batch(background=False)["results"] == {}
    enc = seen[0][-1].split("-EncodedCommand ")[1].split("'")[0]
    inner = base64.b64decode(enc).decode("utf-16-le")
    assert inner.count("winget upgrade --id ") == 2 and "'HP.Opt=' + $LASTEXITCODE" in inner



def test_an_app_whose_publisher_denies_winget_is_not_called_failed(tmp_path):
    rep = report(app_updates={"available": True, "apps": [
        {"id": "Some.Publisher", "name": "Pub", "current": "23.4", "available": "25.12", "scope": "user"},
        {"id": "Other.App", "name": "Other", "current": "1", "available": "2", "scope": "user"}]})
    run = Run((1, "The package cannot be upgraded using WinGet."), (mt.PUBLISHER_ONLY, ""))
    m = make(tmp_path, run, data=rep)
    out = m.auto_update()
    assert [j["state"] for j in out] == ["publisher", "publisher"]
    assert "own updater" in out[0]["output"] and m.report()["publisher_only"] == ["Other.App", "Some.Publisher"]
    assert m.auto_update() == []                  # not retried


def test_no_newer_package_falls_back_to_installing_the_exact_package(tmp_path):
    # 5.4.8 live: winget listed Zoom 7.2.5 but "upgrade" found no newer package
    rep = report(app_updates={"available": True, "apps": [
        {"id": "Zoom.Zoom.EXE", "name": "Zoom", "current": "7.2.1", "available": "7.2.5",
         "scope": "user"}]})
    run = Run((mt.NO_NEWER, "No newer package versions are available"), (0, "Successfully installed"))
    m = make(tmp_path, run, data=rep)
    assert m.start_upgrade("Zoom.Zoom.EXE", background=False)["state"] == "done"
    assert run.calls[1][:4] == ["winget", "install", "--id", "Zoom.Zoom.EXE"]
    assert "--force" in run.calls[1] and run.calls[1][-2:] == ["--scope", "user"]


def test_miktex_is_updated_with_its_own_package_manager(tmp_path, monkeypatch):
    # 5.4.8 live: MiKTeX's winget package refuses upgrades, so it stayed on 23.4
    monkeypatch.setattr(mt.shutil, "which", lambda name: "C:/mik/miktex.exe")
    rep = report(app_updates={"available": True, "apps": [
        {"id": "MiKTeX.MiKTeX", "name": "MiKTeX", "current": "23.4", "available": "25.12",
         "scope": "user"}]})
    run = Run((0, "db updated"), (0, "Installing package xcolor..."))
    m = make(tmp_path, run, data=rep)
    job = m.start_upgrade("MiKTeX.MiKTeX", background=False)
    assert job["state"] == "done" and job["via"] == "publisher updater"
    assert run.calls == [["C:/mik/miktex.exe", "packages", "update-package-database"],
                         ["C:/mik/miktex.exe", "packages", "update"]]
    assert m.upgradable() == {}
    run = Run((3, "no network"))
    m = make(tmp_path / "b", run, data=rep)
    assert m.start_upgrade("MiKTeX.MiKTeX", background=False)["state"] == "failed"
    assert len(run.calls) == 1                                   # stops at the first failure
    monkeypatch.setattr(mt.shutil, "which", lambda name: None)
    assert mt._miktex().endswith("miktex.exe")                   # the per-user install path
