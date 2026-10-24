"""run_hidden adds CREATE_NO_WINDOW on Windows and passes everything through."""
import subprocess

from sysperf import background_process


def test_windows_adds_the_no_window_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(background_process.os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seen.update(a=a, k=k) or "r")
    assert background_process.run_hidden(["x"], creationflags=0x10) == "r"
    assert seen["k"]["creationflags"] == 0x08000010 and seen["a"] == (["x"],)


def test_elsewhere_arguments_are_untouched(monkeypatch):
    monkeypatch.setattr(background_process.os, "name", "posix")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: k)
    assert background_process.run_hidden(["x"], timeout=3) == {"timeout": 3}
