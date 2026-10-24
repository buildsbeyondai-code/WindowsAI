"""Full-coverage tests for the OneDrive control module."""
import pytest

import sysperf.onedrive as onedrive


def _runner(rc=0):
    """A fake command runner recording the last invocation."""
    calls = []

    def run(shell, command):
        calls.append((shell, command))
        return rc, "mock-output"

    run.calls = calls
    return run


def test_status_unknown_then_known():
    assert onedrive.status({}) == {"running": None, "known": False}
    assert onedrive.status(None) == {"running": None, "known": False}
    assert onedrive.status({"onedrive_running": True}) == {"running": True,
                                                           "known": True}
    assert onedrive.status({"onedrive_running": False}) == {"running": False,
                                                            "known": True}


def test_apply_stop_runs_stop_command():
    r = _runner(rc=0)
    out = onedrive.apply(r, "stop")
    assert out == {"action": "stop", "status": "ok", "returncode": 0,
                   "output": "mock-output"}
    shell, command = r.calls[0]
    assert shell == "pwsh" and command == onedrive.PS_STOP


def test_apply_start_runs_start_command_and_reports_error_rc():
    r = _runner(rc=1)
    out = onedrive.apply(r, "start")
    assert out["action"] == "start" and out["status"] == "error"
    assert out["returncode"] == 1
    assert r.calls[0][1] == onedrive.PS_START


def test_apply_rejects_bad_action():
    with pytest.raises(ValueError):
        onedrive.apply(_runner(), "restart")


def test_enforce_default_stops_when_enabled():
    r = _runner(rc=0)
    out = onedrive.enforce_default(r, enabled=True, fix_enabled=True)
    assert out["action"] == "stop" and out["status"] == "ok"
    assert r.calls[0][1] == onedrive.PS_STOP


def test_enforce_default_noop_when_disabled_or_readonly():
    r = _runner()
    assert onedrive.enforce_default(r, enabled=False, fix_enabled=True) is None
    assert onedrive.enforce_default(r, enabled=True, fix_enabled=False) is None
    assert r.calls == []                 # nothing ran
