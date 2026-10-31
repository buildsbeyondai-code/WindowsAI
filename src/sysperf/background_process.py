"""Run background probes without opening console windows on Windows."""
import os
import subprocess

# Read through this name (not os.name) so tests can simulate the other OS
# without patching the global os module, which breaks pathlib on Windows.
OS_NAME = os.name


def _no_window(kwargs):
    if OS_NAME == 'nt':
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
    return kwargs


def run_hidden(*args, **kwargs):
    return subprocess.run(*args, **_no_window(kwargs))


def run_tree(argv, timeout):
    """Run argv to completion; on timeout stop it *and everything it started*.

    subprocess.run kills only the direct child, so a timed-out winget left its
    installer running, waiting on a UAC prompt nobody would answer. Returns
    (returncode, stdout+stderr, timed_out).
    """
    proc = subprocess.Popen(argv, **_no_window({"stdout": subprocess.PIPE,
                                                "stderr": subprocess.STDOUT, "text": True}))
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", False
    except subprocess.TimeoutExpired:
        if OS_NAME == 'nt':
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           **_no_window({"capture_output": True, "timeout": 60}))
        proc.kill()
        out, _ = proc.communicate()
        return 1, out or "", True
