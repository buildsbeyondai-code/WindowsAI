"""Full-coverage tests for the per-application control module."""
import pytest

import sysperf.appctl as appctl


def _runner(rc=0):
    calls = []

    def run(shell, command):
        calls.append((shell, command))
        return rc, "mock"

    run.calls = calls
    return run


def _sample(t, procs, ncpu=8):
    return {"_t": float(t), "ncpu": ncpu, "cpu_procs": procs}


def _busy_history(name="chrome", exe="C:\\chrome.exe", rate=8.0, n=5):
    """History where `name` burns `rate` cpu-seconds/interval consistently."""
    return [_sample(i * 8, [
        {"n": name, "pid": 1, "cpu_s": rate * i, "ws_mb": 300, "exe": exe},
    ]) for i in range(n)]


# ------------------------------------------------------- is_controllable -----
def test_is_controllable():
    assert appctl.is_controllable("chrome") is True
    assert appctl.is_controllable("ms-teams") is True
    assert appctl.is_controllable("MsMpEng") is False       # security (denylist)
    assert appctl.is_controllable("svchost") is False
    assert appctl.is_controllable("") is False
    assert appctl.is_controllable("bad;name & rm") is False  # charset reject


# --------------------------------------------------------------- last_exe ----
def test_last_exe_finds_most_recent_and_handles_gaps():
    hist = [
        "junk",                                              # non-dict skipped
        _sample(0, [{"n": "chrome", "pid": 1, "cpu_s": 0, "exe": "C:\\old.exe"}]),
        _sample(8, [{"n": "chrome", "pid": 1, "cpu_s": 8, "exe": "C:\\new.exe"},
                    "x"]),                                   # non-dict proc skipped
    ]
    assert appctl.last_exe(hist, "chrome") == "C:\\new.exe"
    assert appctl.last_exe(hist, "absent") is None
    assert appctl.last_exe([{"cpu_procs": None}], "chrome") is None


# --------------------------------------------------------------- top_apps ----
def test_top_apps_empty_history():
    out = appctl.top_apps([])
    assert out["available"] is False and out["apps"] == []


def test_top_apps_lists_running_controllable_startable():
    out = appctl.top_apps(_busy_history(), top=5)
    assert out["available"] is True
    app = out["apps"][0]
    assert app["name"] == "chrome"
    assert app["running"] is True
    assert app["controllable"] is True
    assert app["can_start"] is True
    assert app["median_cpu_pct"] > 0
    assert "current_cpu_pct" in app


def test_top_apps_marks_system_process_not_controllable():
    out = appctl.top_apps(_busy_history(name="MsMpEng", exe=None), top=5)
    app = out["apps"][0]
    assert app["name"] == "MsMpEng"
    assert app["controllable"] is False
    assert app["can_start"] is False        # not controllable => cannot start


def test_top_apps_ranks_by_cpu_desc():
    # two apps, differing intensity; heavier must come first
    hist = [_sample(i * 8, [
        {"n": "light", "pid": 1, "cpu_s": 2.0 * i, "ws_mb": 10, "exe": "l.exe"},
        {"n": "heavy", "pid": 2, "cpu_s": 8.0 * i, "ws_mb": 20, "exe": "h.exe"},
    ]) for i in range(5)]
    names = [a["name"] for a in appctl.top_apps(hist, top=2)["apps"]]
    assert names == ["heavy", "light"]


def test_controllable_names_is_allowlist():
    hist = [_sample(i * 8, [
        {"n": "chrome", "pid": 1, "cpu_s": 8.0 * i, "ws_mb": 10, "exe": "c.exe"},
        {"n": "svchost", "pid": 2, "cpu_s": 8.0 * i, "ws_mb": 20, "exe": None},
    ]) for i in range(5)]
    names = appctl.controllable_names(hist)
    assert "chrome" in names and "svchost" not in names


# ------------------------------------------------------------- stop/start ----
def test_stop_ok_and_uses_process_name():
    r = _runner(0)
    out = appctl.stop(r, "chrome")
    assert out == {"action": "stop", "name": "chrome", "status": "ok",
                   "returncode": 0, "output": "mock"}
    assert "Stop-Process -Name 'chrome'" in r.calls[0][1]


def test_stop_error_rc():
    out = appctl.stop(_runner(1), "chrome")
    assert out["status"] == "error" and out["returncode"] == 1


def test_stop_rejects_critical():
    with pytest.raises(ValueError):
        appctl.stop(_runner(), "svchost")


def test_start_ok_uses_exe_path():
    r = _runner(0)
    out = appctl.start(r, "chrome", "C:\\Program Files\\chrome.exe")
    assert out["action"] == "start" and out["status"] == "ok"
    assert "Start-Process -FilePath 'C:\\Program Files\\chrome.exe'" in r.calls[0][1]


def test_start_rejects_missing_exe_and_critical():
    with pytest.raises(ValueError):
        appctl.start(_runner(), "chrome", None)     # no exe, no known launcher
    with pytest.raises(ValueError):
        appctl.start(_runner(), "lsass", "x.exe")   # critical


def test_onedrive_uses_known_launcher_without_exe():
    r = _runner(0)
    out = appctl.start(r, "OneDrive", None)         # no observed exe needed
    assert out["action"] == "start" and out["status"] == "ok"
    assert r.calls[0][1] == appctl.onedrive.PS_START


def test_can_start_true_for_onedrive_without_exe():
    hist = [_sample(i * 8, [
        {"n": "OneDrive", "pid": 1, "cpu_s": 8.0 * i, "ws_mb": 800},  # no exe
    ]) for i in range(5)]
    app = appctl.top_apps(hist, top=5)["apps"][0]
    assert app["name"] == "OneDrive"
    assert app["can_start"] is True                 # via known launcher


def test_ps_quote_escapes_single_quotes():
    r = _runner(0)
    appctl.start(r, "chrome", "C:\\o'ddir\\chrome.exe")
    assert "C:\\o''ddir\\chrome.exe" in r.calls[0][1]


# ------------------------------------------------------------- wsl_shutdown --
def test_wsl_shutdown_schedules_detached_delayed_command():
    r = _runner(0)
    out = appctl.wsl_shutdown(r, delay_s=4)
    assert out["action"] == "wsl-shutdown"
    assert out["name"] == "vmmemWSL"
    assert out["status"] == "scheduled"
    assert out["delay_s"] == 4
    cmd = r.calls[0][1]
    # runs via a detached, hidden Windows process so it survives this WSL teardown
    assert "Start-Process" in cmd and "-WindowStyle Hidden" in cmd
    assert "wsl.exe --shutdown" in cmd
    assert "Start-Sleep -Seconds 4" in cmd


def test_wsl_shutdown_error_rc():
    out = appctl.wsl_shutdown(_runner(1))
    assert out["status"] == "error" and out["returncode"] == 1


def test_wsl_shutdown_clamps_delay_to_minimum():
    r = _runner(0)
    out = appctl.wsl_shutdown(r, delay_s=0)
    assert out["delay_s"] == 1
    assert "Start-Sleep -Seconds 1" in r.calls[0][1]


# --------------------------------------------------------------- total_cpu ---
def test_total_cpu_good_bad_unknown():
    # typical at/under target -> good (green)
    good = appctl.total_cpu({"target": 25, "scored_typical": 12.0,
                             "status": "met"}, 9.9)
    assert good["good"] is True and good["current_pct"] == 9.9

    # typical over target -> bad (red)
    bad = appctl.total_cpu({"target": 25, "scored_typical": 74.1,
                            "status": "regress"}, 30)
    assert bad["good"] is False and bad["status"] == "regress"

    # no scored typical yet -> fall back to current reading
    fb = appctl.total_cpu({"target": 25}, 10)
    assert fb["good"] is True and fb["typical_pct"] is None

    # nothing to judge -> unknown (grey)
    assert appctl.total_cpu(None, None)["good"] is None
    assert appctl.total_cpu({"scored_typical": 5.0}, None)["good"] is None
