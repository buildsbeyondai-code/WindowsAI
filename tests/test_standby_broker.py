"""Tests for the on-demand standby-clear broker gating (clear_standby).

The dashboard service is unprivileged and cannot empty the Windows standby list
itself, so clear_standby is a 'caution' fix that the loop may run *only while*
the elevated standby_broker watcher is online. When the watcher is offline it
degrades to a manual action (the RAMMap recipe), so nothing regresses.
"""
import os
import time

import sysperf.fixes as fixes
from sysperf.fixes import FixEngine


def test_clear_standby_is_caution_and_broker_gated():
    f = FixEngine().get("clear_standby")
    assert f is not None
    assert f.risk == "caution"                 # was 'manual' - now loop-runnable
    assert f.reversible and not f.session_killing and not f.persistent
    assert f.command.strip() != ""             # it now has a real command
    assert f.runtime_gate is fixes._broker_online
    assert f.manual_hint                        # still explains the manual path


def test_clear_standby_command_uses_the_broker_exchange():
    cmd = FixEngine().get("clear_standby").command
    assert ".request" in cmd and ".result" in cmd   # file exchange with watcher
    assert "broker.alive" in cmd                      # heartbeat check first
    assert "exit 0" in cmd and "exit 1" in cmd        # honest success/failure
    # it must NOT attempt to clear the list directly (no elevation here)
    assert "RAMMap" not in cmd and "EmptyStandbyList" not in cmd


def test_broker_online_true_when_heartbeat_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(fixes, "STANDBY_BROKER_DIR_WSL", str(tmp_path))
    monkeypatch.setattr(fixes, "STANDBY_BROKER_HEARTBEAT_MAX_S", 90.0)
    (tmp_path / "broker.alive").write_text("x")
    assert fixes._broker_online() is True


def test_broker_online_false_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(fixes, "STANDBY_BROKER_DIR_WSL", str(tmp_path))
    monkeypatch.setattr(fixes, "STANDBY_BROKER_HEARTBEAT_MAX_S", 90.0)
    assert fixes._broker_online() is False


def test_broker_online_false_when_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(fixes, "STANDBY_BROKER_DIR_WSL", str(tmp_path))
    monkeypatch.setattr(fixes, "STANDBY_BROKER_HEARTBEAT_MAX_S", 1.0)
    hb = tmp_path / "broker.alive"
    hb.write_text("x")
    old = time.time() - 120
    os.utime(hb, (old, old))
    assert fixes._broker_online() is False


def test_executable_follows_broker_state(tmp_path, monkeypatch):
    f = FixEngine().get("clear_standby")
    monkeypatch.setattr(fixes, "STANDBY_BROKER_DIR_WSL", str(tmp_path))
    monkeypatch.setattr(fixes, "STANDBY_BROKER_HEARTBEAT_MAX_S", 90.0)
    assert f.executable is False                 # no watcher -> manual action
    (tmp_path / "broker.alive").write_text("x")
    assert f.executable is True                  # watcher online -> auto-runnable


def test_detector_still_only_fires_on_memory_pressure():
    # 'only when the corresponding scores are low' - unchanged gating
    low = {"scorecard": {"rows": [{"key": "available_mb", "status": "regress"}]},
           "latest": {}}
    fine = {"scorecard": {"rows": [{"key": "available_mb", "status": "met"}]},
            "latest": {}}
    assert fixes._detect_standby(low)
    assert fixes._detect_standby(fine) is None
