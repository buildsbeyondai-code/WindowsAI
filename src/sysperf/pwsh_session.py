"""A long-lived PowerShell process that runs the collector script on request.

Spawning ``pwsh.exe`` for every sample is the single most expensive thing the
dashboard does: process creation, .NET start-up and module loading, plus an
endpoint-security scan of every new process, all before the script's first
line runs. Measured on 2026-10-25 on the reference laptop, a light sample took
5.0 s as a fresh process and 2.7 s in a warm session, with identical output.

Protocol
--------
The shell is started once with ``-Command -`` and reads one command per line
from stdin. Each request runs the script and then prints a unique sentinel
line, so the reader knows exactly where that run's output ends::

    & 'C:\\...\\collect_win.ps1' -Full; [Console]::Out.WriteLine('<<SYSPERF-END-n>>')

The script's variables are script-scoped, so nothing leaks between runs.

Failure handling
----------------
Any timeout, broken pipe or premature exit kills the process; the next request
starts a fresh one. After ``max_runs`` requests the process is recycled anyway
so a slow leak in a probe can never accumulate. Callers fall back to a one-shot
process when :meth:`run` raises, so a broken session costs one slow sample, not
a gap in the data.
"""
from __future__ import annotations

import contextlib
import os
import queue
import subprocess
import threading
import time
from typing import Callable

CREATE_NO_WINDOW = 0x08000000


class SessionError(RuntimeError):
    """The persistent shell could not produce a result for this request."""


def ps_quote(text: str) -> str:
    """A PowerShell single-quoted literal (the only escape is a doubled quote)."""
    return "'" + str(text).replace("'", "''") + "'"


class PwshSession:
    """Runs scripts in one reusable PowerShell process. Thread-safe."""

    def __init__(self, exe: str, *, max_runs: int = 500,
                 popen: Callable = subprocess.Popen,
                 clock: Callable[[], float] = time.monotonic):
        self.exe = exe
        self.max_runs = max_runs
        self._popen = popen
        self._clock = clock
        self._lock = threading.Lock()
        self._proc = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._seq = 0
        self.runs = 0                    # requests served by the current process
        self.total_runs = 0
        self.restarts = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------- process --
    def _argv(self) -> list:
        return [self.exe, "-NoProfile", "-NoLogo", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", "-"]

    def _start(self) -> None:
        kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                  "stderr": subprocess.DEVNULL, "text": True, "encoding": "utf-8",
                  "errors": "replace", "bufsize": 1}
        if os.name == "nt":                                   # pragma: no cover - Windows
            kwargs["creationflags"] = CREATE_NO_WINDOW
        self._proc = self._popen(self._argv(), **kwargs)
        self._lines = queue.Queue()
        lines, out = self._lines, self._proc.stdout

        def pump():
            try:
                for line in out:
                    lines.put(line)
            except (OSError, ValueError):
                pass
            lines.put(None)                                   # EOF marker
        threading.Thread(target=pump, name="pwsh-session-reader", daemon=True).start()
        self.runs = 0
        self.restarts += 1

    def close(self) -> None:
        """Stop the shell (it is restarted on the next request)."""
        with self._lock:
            self._kill()

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        with contextlib.suppress(OSError):
            proc.kill()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------- request --
    def run(self, script: str, args: tuple = (), timeout: float = 120.0) -> str:
        """Run ``script`` with ``args`` and return everything it printed.

        Raises :class:`SessionError` on timeout or if the shell dies; the shell
        is then discarded so the next call starts clean.
        """
        with self._lock:
            if not self.alive or self.runs >= self.max_runs:
                self._kill()
                self._start()
            self._seq += 1
            token = f"<<SYSPERF-END-{os.getpid()}-{self._seq}>>"
            cmd = " ".join(["&", ps_quote(script), *args]) + \
                f"; [Console]::Out.WriteLine({ps_quote(token)}); [Console]::Out.Flush()\n"
            try:
                self._proc.stdin.write(cmd)
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self._fail(f"could not write to the shell: {exc}")
            deadline = self._clock() + timeout
            out = []
            while True:
                left = deadline - self._clock()
                if left <= 0:
                    self._fail(f"no result within {timeout:.0f} s")
                try:
                    line = self._lines.get(timeout=min(left, 1.0))
                except queue.Empty:
                    continue
                if line is None:
                    self._fail("the shell exited unexpectedly")
                line = line.rstrip("\r\n")
                if line == token:
                    break
                out.append(line)
            self.runs += 1
            self.total_runs += 1
            self.last_error = None
            return "\n".join(out)

    def _fail(self, why: str):
        self.last_error = why
        self._kill()
        raise SessionError(why)

    def stats(self) -> dict:
        return {"alive": self.alive, "runs_this_process": self.runs,
                "total_runs": self.total_runs, "starts": self.restarts,
                "last_error": self.last_error}
