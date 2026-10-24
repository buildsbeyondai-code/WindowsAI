"""The real ctypes WinApi, on Windows only: every read against live processes,
and terminate/launch against a throwaway child process (never a user app)."""
import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ctypes API")


@pytest.fixture
def api():
    from sysperf.trayquit import WinApi
    return WinApi()


def test_reads_describe_this_process(api):
    me = os.getpid()
    assert any(p["pid"] == me for p in api.processes())
    assert isinstance(api.visible_pids(), set)
    assert api.mem_mb(me) > 1
    assert abs(api.started(me) - time.time()) < 86400
    assert api.cpu_s(me) >= 0
    assert api.exe_path(me).lower().endswith((".exe",))


def test_unopenable_pid_returns_none(api):
    assert api.mem_mb(0) == 0.0 and api.started(0) is None
    assert api.cpu_s(0) is None and api.exe_path(0) is None and api.terminate(0) is False


def test_terminate_and_launch_a_throwaway_child(api, tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert api.terminate(child.pid) is True
        child.wait(10)
    finally:
        child.kill()
    assert api.launch(str(tmp_path / "missing.exe")) is False
    assert api.launch(os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "whoami.exe"))
