"""Shared pytest fixtures for the perf-dashboard suite."""
import os
import sys
import tempfile

import pytest

# Never let a test run touch a real installation's data directory: importing
# sysperf.app opens the history database, the audit log and other state there.
# Set before any sysperf module is imported (config resolves it at import).
os.environ.setdefault("SYSPERF_DATA_DIR", tempfile.mkdtemp(prefix="sysperf-test-"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _shipped_criteria(monkeypatch):
    """Grade against the shipped CRITERIA, as if a baseline had been captured.

    Whatever machine runs the suite, its own baseline.json must not leak into
    tests that assert on the default rows (see test_machine_profile.py for the
    per-machine behaviour itself).
    """
    from sysperf import config, machine_profile
    saved = list(config.CRITERIA)
    config.CRITERIA[:] = machine_profile.DEFAULT_CRITERIA
    monkeypatch.setattr(config, "PROFILE_PENDING", frozenset())
    yield
    config.CRITERIA[:] = saved


def _row(key, status="met", current=1, target=1, baseline=1):
    return {"key": key, "status": status, "current": current,
            "target": target, "baseline": baseline}


@pytest.fixture
def snapshot_healthy():
    """A snapshot where nothing needs attention (all met)."""
    return {
        "latest": {"wsl": {"cap_active": True}},
        "scorecard": {"rows": [
            _row("net_remote", "met"), _row("c_free_gb", "met"),
            _row("handles", "met"), _row("startup_apps", "met"),
            _row("vmmem_mb", "met"), _row("available_mb", "met"),
            _row("compression_mb", "met"), _row("explorer_launch_s", "met"),
            _row("outlook_launch_s", "met"), _row("word_launch_s", "met"),
            _row("boot_time_s", "met"), _row("wsl_start_s", "met"),
            # remediable since 2.9.1 — previously these had no fix at all
            _row("cpu_pct", "met"), _row("cpu_queue", "met"),
            _row("disk_active_pct", "met"),
        ]},
    }


@pytest.fixture
def snapshot_degraded():
    """A snapshot that triggers every detector."""
    return {
        "latest": {"wsl": {"cap_active": False}},
        "scorecard": {"rows": [
            _row("net_remote", "regress", current=90, target=30),
            _row("c_free_gb", "partial", current=150, target=180),
            _row("handles", "regress", current=200000, target=95000),
            _row("startup_apps", "partial", current=18, target=10),
            _row("vmmem_mb", "regress", current=9000, target=10240),
            _row("available_mb", "partial", current=6000, target=12000),
            _row("compression_mb", "regress", current=5000, target=2750),
            _row("boot_time_s", "regress", current=140, target=40),
            _row("wsl_start_s", "regress", current=30, target=5),
            _row("explorer_launch_s", "partial", current=6, target=2),
            _row("outlook_launch_s", "regress", current=25, target=5),
            _row("word_launch_s", "partial", current=12, target=4),
            # remediable since 2.9.1 — previously these had no fix at all
            _row("cpu_pct", "regress", current=90, target=25),
            _row("cpu_queue", "regress", current=6, target=1),
            _row("disk_active_pct", "regress", current=60, target=10),
        ]},
        # 5.3: what /api/fixes adds; every maintenance fix has a finding
        "maintenance": {"findings": [
            {"fix": f, "text": f"{f} finding"} for f in (
                "clear_browser_cache", "fix_registry_orphans", "create_restore_point",
                "repair_system_files", "windows_update_cleanup")]},
    }

# The evolution fixture is a separate mini-repository with its own suite; it is
# run only inside the sandbox by tests/test_codevolve.py.
collect_ignore = ["fixtures"]
