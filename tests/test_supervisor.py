"""The supervisor's decisions: restart on exit/hang/stall, back-off, sleep/resume,
and rolling back a patch that leaves the service unhealthy."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from sysperf import codevolve, supervisor
from sysperf.supervisor import BACKOFF_S, RESTART_CODE, Supervisor, child_argv, http_health


class Child:
    def __init__(self):
        self.rc, self.killed, self.pid = None, False, 4242

    def poll(self):
        return self.rc

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.rc


class World:
    def __init__(self, tmp_path):
        self.now = 1000.0
        self.health = {"ok": True, "last_sample_age_s": 30}
        self.children, self.spawned_env, self.slept = [], [], []
        self.rolled = []
        self.state = tmp_path
        self.next_rc = None

    def spawn(self, argv, env=None, stdout=None, stderr=None):
        c = Child()
        c.rc = self.next_rc
        self.children.append(c)
        self.spawned_env.append(env)
        return c

    def sup(self, **kw):
        return Supervisor(["py", "-m", "sysperf"], "http://127.0.0.1:5056/api/health",
                          self.state, env={"A": "1"}, spawn=self.spawn,
                          health=lambda url: self.health, sleep=self.slept.append,
                          clock=lambda: self.now,
                          rollback=lambda *a: self.rolled.append(a), **kw)


def test_start_sets_env_and_only_first_start_opens_browser(tmp_path):
    w = World(tmp_path)
    s = w.sup()
    s.start_child()
    s.start_child()
    assert w.spawned_env[0]["SYSPERF_SUPERVISED"] == "1"
    assert "SYSPERF_NO_BROWSER" not in w.spawned_env[0]
    assert w.spawned_env[1]["SYSPERF_NO_BROWSER"] == "1"
    events = [json.loads(x)["event"] for x in (tmp_path / "supervisor_log.jsonl")
              .read_text().splitlines()]
    assert events == ["started", "started"]


def test_requested_restart_is_immediate_crashes_back_off(tmp_path):
    w = World(tmp_path)
    s = w.sup()
    s.start_child()
    w.children[-1].rc = RESTART_CODE
    assert s.tick() == 0.0 and s.crashes == 0
    waits = []
    for _ in range(7):
        w.children[-1].rc = 1
        waits.append(s.tick())
    assert waits == [5.0] + [float(b) for b in BACKOFF_S[1:]] + [120.0, 120.0]


def test_healthy_child_resets_counters_and_grace_period_is_respected(tmp_path):
    w = World(tmp_path)
    s = w.sup(start_grace_s=100)
    s.start_child()
    s.crashes = 2
    w.health = None
    assert s.tick() is None                              # still inside start-up grace
    w.health = {"ok": True}
    for _ in range(8):                                   # normal 15 s cadence, no sleep
        w.now += 15
        assert s.tick() is None
    assert s.crashes == 0


def test_failed_health_checks_restart_after_threshold(tmp_path):
    w = World(tmp_path)
    s = w.sup(start_grace_s=0, fail_checks=3)
    s.start_child()
    w.health = None
    assert [s.tick() for _ in range(3)] == [None, None, 0.0]
    assert w.children[0].killed
    w.health = {"ok": False}
    s.start_child()
    assert s.tick() is None


def test_stalled_sampling_is_treated_as_unhealthy(tmp_path):
    w = World(tmp_path)
    s = w.sup(start_grace_s=0, fail_checks=1, stall_s=600)
    s.start_child()
    w.health = {"ok": True, "last_sample_age_s": 900}
    assert s.tick() == 0.0 and w.children[0].killed
    log = (tmp_path / "supervisor_log.jsonl").read_text()
    assert "no sample for more than 600 s" in log


def test_sleep_resume_gives_a_fresh_grace_period(tmp_path):
    w = World(tmp_path)
    s = w.sup(start_grace_s=100, check_s=15)
    s.start_child()
    w.now += 200
    s.tick()
    w.health = None
    w.now += 3600                                        # laptop slept for an hour
    assert s.tick() is None and s.started_at == w.now
    assert "resumed" in (tmp_path / "supervisor_log.jsonl").read_text()


def _mark(tmp_path):
    codevolve.write_marker(tmp_path, {"id": "c-1", "repo": str(tmp_path), "backup_sha": "aaa",
                                      "applied_sha": "bbb"})


def test_repeated_crashes_after_a_patch_roll_it_back(tmp_path):
    w = World(tmp_path)
    s = w.sup()
    s.start_child()
    _mark(tmp_path)
    w.children[-1].rc = 1
    assert s.tick() == 5.0 and not w.rolled              # one crash: just back off
    w.children[-1].rc = 1
    assert s.tick() == 0.0
    assert w.rolled == [(Path(tmp_path), "aaa", "bbb")]
    assert codevolve.read_marker(tmp_path) is None
    log = codevolve.read_apply_log(tmp_path)
    assert log[-1]["event"] == "rolled_back" and log[-1]["by"] == "supervisor"


def test_patched_service_that_never_gets_healthy_is_rolled_back(tmp_path):
    w = World(tmp_path)
    s = w.sup(start_grace_s=10, verify_s=60)
    s.start_child()
    _mark(tmp_path)
    w.health = {"ok": False}
    w.now += 30
    assert s.tick() is None                              # before verify deadline
    w.now += 60
    assert s.tick() == 0.0 and w.children[0].killed and w.rolled


def test_rollback_without_marker_or_failing_is_reported(tmp_path):
    w = World(tmp_path)
    s = w.sup()
    assert s.maybe_rollback("x") is False
    _mark(tmp_path)

    def boom(*a):
        raise RuntimeError("HEAD moved")
    s._rollback = boom
    assert s.maybe_rollback("x") is False
    assert codevolve.read_marker(tmp_path) is not None   # kept for a human to see
    assert "rollback_failed" in (tmp_path / "supervisor_log.jsonl").read_text()


def test_kill_tolerates_a_dead_child_and_log_write_failures(tmp_path):
    w = World(tmp_path)
    s = w.sup()
    s.start_child()

    def boom():
        raise OSError("already gone")
    w.children[0].kill = boom
    s.kill_child("test")
    s.state_dir = tmp_path / "missing" / "dir"
    s._record("x")                                        # swallowed


def test_run_loop_restarts_and_stops(tmp_path):
    w = World(tmp_path)
    s = w.sup(start_grace_s=0, fail_checks=1)
    w.health = None
    assert s.run(max_ticks=2) == 0
    assert len(w.children) == 3                           # initial + one restart per tick
    s2 = w.sup()
    w.health = {"ok": True}
    w.children[-1].rc = None
    s2.stop = True
    assert s2.run() == 0
    s3 = w.sup()
    first = len(w.children)
    w.next_rc = 2                                         # every child crashes at once
    s3.run(max_ticks=1)
    assert 5.0 in w.slept and len(w.children) == first + 2


def test_child_argv_and_http_health(monkeypatch):
    assert child_argv(["--supervise", "--port", "1"]) == [sys.executable, "-m", "sysperf",
                                                          "--port", "1"]

    class Resp:
        def __init__(self, status, body):
            self.status, self.body = status, body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    monkeypatch.setattr(supervisor.urllib.request, "urlopen",
                        lambda url, timeout: Resp(200, b'{"ok": true}'))
    assert http_health("u") == {"ok": True}
    monkeypatch.setattr(supervisor.urllib.request, "urlopen",
                        lambda url, timeout: Resp(500, b""))
    assert http_health("u") is None

    def refuse(url, timeout):
        raise OSError("refused")
    monkeypatch.setattr(supervisor.urllib.request, "urlopen", refuse)
    assert http_health("u") is None


def test_real_child_process_round_trip(tmp_path):
    """One real spawn: the default Popen path, stdout inheritance and exit codes."""
    s = Supervisor([sys.executable, "-c", f"raise SystemExit({RESTART_CODE})"],
                   "http://127.0.0.1:9/none", tmp_path, sleep=lambda s: None)
    s.start_child()
    s.child.wait(30)
    assert s.tick() == 0.0
    assert isinstance(s.child, subprocess.Popen)


@pytest.fixture(autouse=True)
def _no_real_git(monkeypatch):
    monkeypatch.setattr(codevolve, "_git", lambda *a, **k: pytest.fail("git called"))


def test_run_loop_keeps_waiting_while_healthy(tmp_path):
    w = World(tmp_path)
    s = w.sup(start_grace_s=0)
    assert s.run(max_ticks=3) == 0 and len(w.children) == 1
