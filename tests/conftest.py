"""Shared fixtures: a synthetic Windows sample so tests run on any platform."""
from __future__ import annotations

import copy
import time

import pytest

# A realistic full sample from a 16 GB laptop. Deliberately not the machine any
# developer happens to be using — the point of the test suite is that no machine
# is special.
FULL_SAMPLE: dict = {
    "ts": "2026-01-01T09:00:00.0000000+00:00",
    "boot": "2026-01-01T08:00:00.0000000+00:00",
    "uptime_h": 1.0,
    "committed_mb": 14000, "commit_limit_mb": 24000,
    "available_mb": 3200, "total_ram_mb": 16000,
    "compression_mb": 1400, "vmmem_mb": 4200,
    "pool_paged_mb": 900, "pool_nonpaged_mb": 700,
    "handles": 120000, "threads": 2400, "processes": 260,
    "edge_procs": 22, "wv2_procs": 18, "browser_procs": 40,
    "edge_main": 1, "edge_renderer": 12, "edge_extension": 3, "edge_utility": 6,
    "edge_ext_mb": 380,
    "wv2_hosts": {"OneDrive": 6, "SearchHost": 4, "ms-teams": 5, "SomeApp": 3},
    "svchost_procs": 90, "search_procs": 3,
    "system_drive": "C:", "c_free_gb": 60.0, "c_size_gb": 476.0,
    "cpu_queue": 4, "cpu_pct": 12.5,
    "top_mem": [{"name": "msedge", "mb": 2600, "count": 22},
                {"name": "Teams", "mb": 900, "count": 5}],
    "top_handles": [{"name": "msedge", "handles": 30000, "count": 22}],
    # slow tier
    "hostname": "TESTBOX", "machine_vendor": "Contoso", "machine_model": "Model X",
    "cpu_name": "Test CPU 3.0GHz", "cpu_cores": 4, "cpu_threads": 8,
    "os_caption": "Microsoft Windows 11 Pro", "os_build": "26100",
    "services_running": 150, "services_total": 260,
    "startup_apps": 18, "startup_apps_total": 25,
    "startup_list": [{"name": "VendorUpdater", "location": "HKCU Run"}],
    "net_remote": 55, "net_loopback": 40, "net_established": 95,
    "net_listen": 30, "net_tcp_total": 200,
    "pagefile_mb": 2048, "pagefile_peak_mb": 1900, "pagefile_current_mb": 1200,
    "pagefile_automatic": True,
    "power_plan": "Power saver", "power_plan_guid": "a1841308-3541-4fab-bc81-f71556f20b4a",
    "restart_pending": False,
    "defender_realtime": True,
    "hw_problem_devices": 1,
    "hw_problem_list": [{"name": "Unknown Device", "code": 28, "status": "Error"}],
    "hw_disks": [{"name": "NVMe SSD", "media": "SSD", "health": "Healthy", "op": "OK"}],
    "hw_disks_unhealthy": 0,
    "hw_battery": {"charge": 88, "status": 2, "design_capacity": 53000,
                   "full_capacity": 31000},
    "hw_pointing": [{"name": "Precision Touchpad", "status": "OK", "code": 0}],
    "hw_touchpad": [],
    "drv_total": 210, "drv_unsigned": 0, "drv_stale": 3,
    "drv_stale_list": [{"name": "old.inf", "version": "1.0", "date": "2016-04-02",
                        "class": "Bluetooth", "provider": "Contoso"}],
    "drv_problem": 1,
    "drv_problem_list": [{"name": "Unknown Device", "code": 28, "status": "Error"}],
    "wsl_installed": True, "wsl_config_present": False,
}


@pytest.fixture
def sample() -> dict:
    return copy.deepcopy(FULL_SAMPLE)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A default config with state redirected into a temp directory."""
    monkeypatch.setenv("SYSPERF_DATA_DIR", str(tmp_path))
    from sysperf.config import load_config
    return load_config(tmp_path / "sysperf.config.json")


@pytest.fixture
def history(sample) -> list[dict]:
    """Twenty samples spanning 20 minutes, all identical apart from time."""
    now = time.time()
    out = []
    for i in range(20):
        s = copy.deepcopy(sample)
        s["_t"] = now - (19 - i) * 60
        s["_iso"] = "2026-01-01T09:00:00+00:00"
        out.append(s)
    return out
