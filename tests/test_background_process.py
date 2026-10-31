"""run_hidden adds CREATE_NO_WINDOW on Windows and passes everything through."""
import subprocess

from sysperf import background_process


def test_windows_adds_the_no_window_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(background_process, "OS_NAME", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seen.update(a=a, k=k) or "r")
    assert background_process.run_hidden(["x"], creationflags=0x10) == "r"
    assert seen["k"]["creationflags"] == 0x08000010 and seen["a"] == (["x"],)


def test_elsewhere_arguments_are_untouched(monkeypatch):
    monkeypatch.setattr(background_process, "OS_NAME", "posix")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: k)
    assert background_process.run_hidden(["x"], timeout=3) == {"timeout": 3}



class Proc:
    pid = 4242

    def __init__(self, hang):
        self.hang, self.killed, self.returncode = hang, False, 3

    def communicate(self, timeout=None):
        if self.hang and timeout is not None:
            raise subprocess.TimeoutExpired("x", timeout)
        return ("partial" if self.hang else "done"), None

    def kill(self):
        self.killed = True


def test_run_tree_returns_output_and_code(monkeypatch):
    monkeypatch.setattr(background_process, "OS_NAME", "posix")
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: Proc(False))
    assert background_process.run_tree(["x"], 5) == (3, "done", False)


def test_a_timeout_stops_the_whole_tree_on_windows(monkeypatch):
    procs, ran = [], []
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: procs.append(Proc(True)) or procs[-1])
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: ran.append(argv))
    monkeypatch.setattr(background_process, "OS_NAME", "nt")
    assert background_process.run_tree(["winget"], 1) == (1, "partial", True)
    assert ran == [["taskkill", "/T", "/F", "/PID", "4242"]] and procs[0].killed
    monkeypatch.setattr(background_process, "OS_NAME", "posix")
    assert background_process.run_tree(["winget"], 1)[2] and len(ran) == 1
