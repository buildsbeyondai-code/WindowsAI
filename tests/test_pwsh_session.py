"""The persistent PowerShell session: protocol, recovery and recycling, driven by
a fake process that behaves like ``pwsh -Command -``."""
import queue
import re
import threading

import pytest

from sysperf.pwsh_session import PwshSession, SessionError, ps_quote


class FakeStdout:
    """Line iterator fed by the fake shell; ``None`` ends the stream."""

    def __init__(self):
        self.q = queue.Queue()

    def __iter__(self):
        while True:
            line = self.q.get()
            if line is None:
                return
            yield line


class FakeStdin:
    def __init__(self, proc):
        self.proc = proc

    def write(self, text):
        if self.proc.broken:
            raise BrokenPipeError("pipe closed")
        self.proc.commands.append(text)
        self.proc.respond(text)

    def flush(self):
        pass


class FakeProc:
    """mode: 'ok' echoes output + sentinel; 'silent' never answers; 'die' exits."""

    def __init__(self, mode="ok", output='{"cpu_pct": 7}'):
        self.mode, self.output = mode, output
        self.stdout, self.stdin = FakeStdout(), FakeStdin(self)
        self.commands, self.killed, self.broken, self.rc = [], False, False, None

    def respond(self, cmd):
        token = re.search(r"WriteLine\('([^']+)'\)", cmd).group(1)
        if self.mode == "ok":
            for ln in self.output.splitlines():
                self.stdout.q.put(ln + "\r\n")
            self.stdout.q.put(token + "\n")
        elif self.mode == "die":
            self.rc = 1
            self.stdout.q.put(None)

    def poll(self):
        return self.rc

    def kill(self):
        self.killed = True
        self.rc = -9
        self.stdout.q.put(None)


class Spawner:
    def __init__(self, *modes, **kw):
        self.modes, self.kw, self.procs, self.argv = list(modes), kw, [], None

    def __call__(self, argv, **kwargs):
        self.argv, self.kwargs = argv, kwargs
        p = FakeProc(self.modes.pop(0) if self.modes else "ok", **self.kw)
        self.procs.append(p)
        return p


def test_quote_doubles_single_quotes():
    assert ps_quote("C:\\Julie's\\x.ps1") == "'C:\\Julie''s\\x.ps1'"


def test_run_returns_output_and_reuses_one_process():
    sp = Spawner()
    s = PwshSession("pwsh.exe", popen=sp)
    assert s.run("C:\\a b\\collect.ps1", ("-Full",), timeout=5) == '{"cpu_pct": 7}'
    assert s.run("C:\\a b\\collect.ps1", timeout=5) == '{"cpu_pct": 7}'
    assert len(sp.procs) == 1 and s.alive
    assert "& 'C:\\a b\\collect.ps1' -Full;" in sp.procs[0].commands[0]
    assert sp.argv[-2:] == ["-Command", "-"] and sp.kwargs["stdin"] is not None
    st = s.stats()
    assert st["total_runs"] == 2 and st["starts"] == 1 and st["last_error"] is None


def test_silence_times_out_kills_and_next_run_starts_fresh():
    sp = Spawner("silent", "ok")
    s = PwshSession("pwsh.exe", popen=sp)
    with pytest.raises(SessionError, match="no result within"):
        s.run("x.ps1", timeout=0.3)
    assert sp.procs[0].killed and not s.alive and "no result" in s.stats()["last_error"]
    assert s.run("x.ps1", timeout=5) == '{"cpu_pct": 7}'
    assert len(sp.procs) == 2


def test_shell_exit_mid_request_is_an_error():
    s = PwshSession("pwsh.exe", popen=Spawner("die"))
    with pytest.raises(SessionError, match="exited unexpectedly"):
        s.run("x.ps1", timeout=5)


def test_broken_pipe_is_an_error():
    sp = Spawner()
    s = PwshSession("pwsh.exe", popen=sp)
    s.run("x.ps1", timeout=5)
    sp.procs[0].broken = True
    with pytest.raises(SessionError, match="could not write"):
        s.run("x.ps1", timeout=5)


def test_recycled_after_max_runs_and_close():
    sp = Spawner()
    s = PwshSession("pwsh.exe", popen=sp, max_runs=2)
    for _ in range(3):
        s.run("x.ps1", timeout=5)
    assert len(sp.procs) == 2 and sp.procs[0].killed
    s.close()
    assert not s.alive
    s.close()                                   # idempotent


def test_kill_failure_is_tolerated():
    sp = Spawner()
    s = PwshSession("pwsh.exe", popen=sp)
    s.run("x.ps1", timeout=5)

    def boom():
        raise OSError("gone")
    sp.procs[0].kill = boom
    s.close()
    assert s._proc is None


def test_reader_survives_a_failing_stream():
    class BadOut:
        def __iter__(self):
            raise OSError("pipe")

    class P(FakeProc):
        def __init__(self):
            super().__init__()
            self.stdout = BadOut()

        def respond(self, cmd):          # output never arrives; the reader saw EOF
            pass

        def kill(self):
            self.killed = True

    s = PwshSession("pwsh.exe", popen=lambda argv, **k: P())
    with pytest.raises(SessionError, match="exited unexpectedly"):
        s.run("x.ps1", timeout=5)


def test_concurrent_callers_are_serialised():
    s = PwshSession("pwsh.exe", popen=Spawner())
    out = []
    threads = [threading.Thread(target=lambda: out.append(s.run("x.ps1", timeout=5)))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out == ['{"cpu_pct": 7}'] * 5


def test_windows_start_hides_the_console(monkeypatch):
    import sysperf.pwsh_session as ps
    monkeypatch.setattr(ps.os, "name", "nt")
    sp = Spawner()
    PwshSession("pwsh.exe", popen=sp).run("x.ps1", timeout=5)
    assert sp.kwargs["creationflags"] == ps.CREATE_NO_WINDOW
