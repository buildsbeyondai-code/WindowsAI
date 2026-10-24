"""Tests for the OneDrive-control master switch and its effect on the policy."""
from sysperf.onedrive_control import OneDriveControl
from sysperf.session_policy import SessionOneDrivePolicy


def test_defaults_to_enabled_when_file_missing(tmp_path):
    c = OneDriveControl(tmp_path / "missing.json")
    assert c.disabled() is False
    assert c.status() == {"control_disabled": False}


def test_set_persists_across_instances(tmp_path):
    p = tmp_path / "onedrive_control.json"
    c = OneDriveControl(p)
    assert c.set(True) is True
    assert c.disabled() is True
    # A fresh instance reads the persisted value (survives a "restart").
    assert OneDriveControl(p).disabled() is True
    assert c.set(False) is False
    assert OneDriveControl(p).disabled() is False


def test_corrupt_file_falls_back_to_enabled(tmp_path):
    p = tmp_path / "onedrive_control.json"
    p.write_text("{ not json", encoding="utf-8")
    assert OneDriveControl(p).disabled() is False


def test_write_failure_cleans_up_and_raises(tmp_path, monkeypatch):
    """If the atomic replace fails, the temp file is cleaned up and the error
    propagates (persistence must not fail silently)."""
    import sysperf.onedrive_control as oc
    c = OneDriveControl(tmp_path / "onedrive_control.json")

    def boom(*a, **k):
        raise OSError("read-only fs")

    monkeypatch.setattr(oc.os, "replace", boom)
    monkeypatch.setattr(oc.os, "unlink", boom)      # cleanup also fails -> inner except
    import pytest
    with pytest.raises(OSError):
        c.set(True)


def _policy(disabled, latest):
    actions = []

    def apply_fn(runner, action):
        actions.append(action)
        return {"action": action, "status": "ok"}

    p = SessionOneDrivePolicy(
        runner=lambda s, c: (0, ""), get_latest=lambda: latest,
        apply_fn=apply_fn, control_disabled=lambda: disabled)
    p.actions = actions
    # Make the seeded sample look current so the staleness guard does not skip it.
    p.started_at = 0.0
    return p


def test_policy_inert_when_control_disabled():
    # Unlocked + running would normally trigger a "stop"; control-off suppresses it.
    p = _policy(disabled=True, latest={"session_locked": False,
                                       "onedrive_running": True, "_t": 10.0})
    assert p.tick() is None
    assert p.actions == []
    assert p.status()["control_disabled"] is True
    assert p.status()["enabled"] is False


def test_policy_acts_when_control_enabled():
    p = _policy(disabled=False, latest={"session_locked": False,
                                        "onedrive_running": True, "_t": 10.0})
    assert p.tick() == {"action": "stop", "status": "ok"}
    assert p.actions == ["stop"]
    assert p.status()["control_disabled"] is False
