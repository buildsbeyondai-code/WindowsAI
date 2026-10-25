"""Live values against an independent Windows reading, on Windows only.

livestats reads Win32 APIs directly; this asks PowerShell/CIM for the same
facts through a different path. Agreement shows the dashboard's numbers are the
machine's, not plausible-looking constants.
"""
import json
import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Win32 APIs")

PS = ("$o = Get-CimInstance Win32_OperatingSystem; "
      "@{total_mb = [math]::Round($o.TotalVisibleMemorySize / 1024); "
      "procs = (Get-Process).Count; "
      "uptime_h = ((Get-Date) - $o.LastBootUpTime).TotalHours} | ConvertTo-Json")


def test_live_values_match_an_independent_os_reading():
    from sysperf import livestats
    os_view = json.loads(subprocess.run(
        ["powershell", "-NoProfile", "-Command", PS], capture_output=True, text=True,
        timeout=60, check=True).stdout)
    live = livestats.read()
    assert live["total_ram_mb"] == pytest.approx(os_view["total_mb"], abs=2)
    assert live["uptime_h"] == pytest.approx(os_view["uptime_h"], abs=0.05)
    # Processes come and go between the two reads.
    assert live["processes"] == pytest.approx(os_view["procs"], rel=0.1)
    assert 0 < live["available_mb"] <= live["total_ram_mb"]
    assert live["committed_mb"] <= live["commit_limit_mb"]
