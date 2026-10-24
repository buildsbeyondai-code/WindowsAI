"""Keeps the SysPerf service running: restart on crash, hang or stall.

Before 4.1 the sign-in task started the server directly. If it crashed, hung,
or came back from sleep with a wedged collector, it stayed that way until the
next sign-in; restarting it by hand on 2026-10-25 needed two attempts. Now the
task starts this supervisor, which runs the server as a child process and:

* restarts it when it exits — at once for a requested restart (exit code
  ``RESTART_CODE``, used after a patch is applied), with exponential back-off
  for a crash so a broken build cannot spin the CPU;
* restarts it when ``/api/health`` stops answering, or answers but reports
  that no sample has landed for ``stall_s``;
* recognises sleep/resume (the wall clock jumps far past the check interval)
  and gives the child a grace period instead of judging it on stale data;
* rolls a just-applied AI patch back when the patched service does not become
  healthy within ``verify_s`` or crashes repeatedly — the deploy marker
  written by :meth:`codevolve.CodeEvolution.apply` says what to undo.

Everything with side effects (spawning, HTTP, sleeping, git) is injected, so
the decision logic is tested without processes or a network.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable

from . import codevolve

log = logging.getLogger("sysperf.supervisor")

RESTART_CODE = 75                 # child exit code meaning "please start me again"
BACKOFF_S = (5, 10, 30, 60, 120)


def http_health(url: str, timeout: float = 10.0) -> dict | None:
    """The parsed /api/health body, or None if it did not answer properly."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


class Supervisor:
    def __init__(self, child_argv: list, health_url: str, state_dir: Path, *,
                 env: dict | None = None, check_s: float = 15.0, start_grace_s: float = 120.0,
                 stall_s: float = 900.0, verify_s: float = 300.0, fail_checks: int = 3,
                 spawn: Callable = subprocess.Popen, health: Callable = http_health,
                 sleep: Callable = time.sleep, clock: Callable[[], float] = time.time,
                 rollback: Callable = codevolve.git_rollback):
        self.child_argv = list(child_argv)
        self.health_url = health_url
        self.state_dir = Path(state_dir)
        self.env = dict(os.environ if env is None else env)
        self.check_s, self.start_grace_s, self.stall_s = check_s, start_grace_s, stall_s
        self.verify_s, self.fail_checks = verify_s, fail_checks
        self._spawn, self._health, self._sleep, self.clock = spawn, health, sleep, clock
        self._rollback = rollback
        self.child = None
        self.started_at = 0.0
        self.starts = 0
        self.crashes = 0              # consecutive, reset once healthy
        self.bad_checks = 0
        self.last_tick = None
        self.stop = False

    # ------------------------------------------------------------- process --
    def start_child(self) -> None:
        # PYTHONUNBUFFERED: the child's stdout is the runtime log file, which Python
        # would otherwise block-buffer (8 KB), delaying every line by minutes to hours.
        env = dict(self.env, SYSPERF_SUPERVISED="1", PYTHONUNBUFFERED="1")
        if self.starts:
            env["SYSPERF_NO_BROWSER"] = "1"           # only the first start opens a tab
        self.child = self._spawn(self.child_argv, env=env, stdout=sys.stdout,
                                 stderr=subprocess.STDOUT)
        self.started_at = self.clock()
        self.starts += 1
        self.bad_checks = 0
        self._record("started", pid=getattr(self.child, "pid", None))

    def kill_child(self, why: str) -> None:
        log.warning("supervisor: restarting the service — %s", why)
        try:
            self.child.kill()
            self.child.wait(30)
        except Exception:
            pass
        self._record("killed", reason=why)

    def _record(self, event: str, **extra) -> None:
        try:
            with (self.state_dir / "supervisor_log.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": self.clock(), "event": event, **extra}) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------ decisions --
    def maybe_rollback(self, why: str) -> bool:
        """Undo a just-applied patch. Returns True when one was rolled back."""
        m = codevolve.read_marker(self.state_dir)
        if m is None:
            return False
        try:
            self._rollback(Path(m["repo"]), m["backup_sha"], m["applied_sha"])
        except Exception as exc:
            log.error("supervisor: rollback of %s failed: %s", m.get("id"), exc)
            self._record("rollback_failed", id=m.get("id"), error=str(exc))
            return False
        codevolve.clear_marker(self.state_dir)
        codevolve.append_apply_log(self.state_dir, {
            "at": self.clock(), "event": "rolled_back", "id": m.get("id"), "by": "supervisor",
            "reason": why, "backup_sha": m["backup_sha"]})
        self._record("rolled_back", id=m.get("id"), reason=why)
        log.error("supervisor: rolled back patch %s — %s", m.get("id"), why)
        return True

    def tick(self) -> float | None:
        """One supervision step. Returns how long to wait before a restart,
        0 to restart now, or None when nothing needs doing."""
        now = self.clock()
        slept = self.last_tick is not None and now - self.last_tick > max(4 * self.check_s, 120)
        self.last_tick = now
        if slept:
            # Back from sleep: the child's data is old through no fault of its own.
            self.started_at = now
            self.bad_checks = 0
            self._record("resumed")
        rc = self.child.poll()
        if rc is not None:
            if rc == RESTART_CODE:
                self._record("restart_requested")
                self.crashes = 0
                return 0.0
            self.crashes += 1
            self._record("exited", code=rc, consecutive=self.crashes)
            if self.crashes >= 2 and self.maybe_rollback(f"service exited {self.crashes}x "
                                                         f"(last code {rc}) after a patch"):
                self.crashes = 0
                return 0.0
            return float(BACKOFF_S[min(self.crashes - 1, len(BACKOFF_S) - 1)])
        age = now - self.started_at
        if age < self.start_grace_s:
            return None
        h = self._health(self.health_url)
        if h is not None and h.get("ok"):
            last_age = h.get("last_sample_age_s")
            stalled = isinstance(last_age, (int, float)) and last_age > self.stall_s
        else:
            stalled = False
        healthy = h is not None and h.get("ok") and not stalled
        if healthy:
            self.bad_checks = 0
            self.crashes = 0
            return None
        if codevolve.read_marker(self.state_dir) and age > self.verify_s:
            self.kill_child("the patched service did not become healthy")
            self.maybe_rollback(f"not healthy {int(age)} s after applying the patch")
            return 0.0
        self.bad_checks += 1
        if self.bad_checks < self.fail_checks:
            return None
        why = ("no sample for more than %d s" % self.stall_s) if stalled else \
            "health check failed %d times" % self.bad_checks
        self.kill_child(why)
        return 0.0

    def run(self, max_ticks: int | None = None) -> int:
        self.start_child()
        ticks = 0
        while not self.stop and (max_ticks is None or ticks < max_ticks):
            self._sleep(self.check_s)
            ticks += 1
            wait = self.tick()
            if wait is None:
                continue
            if wait:
                self._sleep(wait)
            self.start_child()
        return 0


def child_argv(argv: list) -> list:
    """The command that runs the server itself: same interpreter, same options."""
    return [sys.executable, "-m", "sysperf", *[a for a in argv if a != "--supervise"]]
