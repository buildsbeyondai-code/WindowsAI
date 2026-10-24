"""Tests for the runaway-Explorer CPU watchdog and its cpuwatch helper."""
import sysperf.cpuwatch as cpuwatch
from sysperf.procwatch import ExplorerCpuWatchdog


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs
        return self.t


def _history(explorer_pcts, ncpu=1, end_t=1000.0):
    """Build a synthetic history whose per-interval explorer CPU% matches the
    given list. cpu_s is cumulative, so each sample adds pct*dt*ncpu/100 seconds.
    dt is a constant 20s. The newest sample is timestamped at ``end_t`` so it is
    fresh relative to a clock at that time; the first sample seeds the baseline."""
    dt, cum = 20.0, 0.0
    n = len(explorer_pcts)
    t0 = end_t - n * dt
    hist = [{"_t": t0, "ncpu": ncpu,
             "cpu_procs": [{"pid": 1, "n": "explorer", "cpu_s": 0.0, "ws_mb": 50}]}]
    for i, pct in enumerate(explorer_pcts, start=1):
        cum += pct * dt * ncpu / 100.0
        hist.append({"_t": t0 + i * dt, "ncpu": ncpu,
                     "cpu_procs": [{"pid": 1, "n": "explorer",
                                    "cpu_s": round(cum, 4), "ws_mb": 50}]})
    return hist


def _wd(tmp_path, hist, clock, **kw):
    calls = []

    def apply_fn(runner):
        calls.append(True)
        return {"action": "restart_explorer", "status": "ok",
                "returncode": 0, "output": "mock"}

    wd = ExplorerCpuWatchdog(
        runner=lambda s, c: (0, ""), get_history=lambda: hist,
        state_file=tmp_path / "procwatch.json", apply_fn=apply_fn, clock=clock,
        **kw)
    wd.calls = calls
    return wd


# ----------------------------------------------------------- cpuwatch helper --
def test_recent_interval_cpu_series():
    hist = _history([35.0, 40.0, 5.0])
    series = cpuwatch.recent_interval_cpu(hist, "explorer", count=3)
    assert series == [35.0, 40.0, 5.0]
    assert cpuwatch.recent_interval_cpu(hist, "explorer", count=1) == [5.0]


def test_recent_interval_cpu_absent_process_is_zero():
    hist = _history([35.0, 40.0])
    assert cpuwatch.recent_interval_cpu(hist, "msedge", count=2) == [0.0, 0.0]


def test_recent_interval_cpu_needs_two_samples():
    assert cpuwatch.recent_interval_cpu([], "explorer") == []


def test_recent_interval_cpu_skips_nonpositive_dt():
    """Consecutive samples with a non-increasing timestamp are skipped (dt<=0)."""
    hist = [
        {"_t": 100.0, "ncpu": 1, "cpu_procs": [{"pid": 1, "n": "explorer", "cpu_s": 0.0}]},
        {"_t": 100.0, "ncpu": 1, "cpu_procs": [{"pid": 1, "n": "explorer", "cpu_s": 5.0}]},
        {"_t": 90.0,  "ncpu": 1, "cpu_procs": [{"pid": 1, "n": "explorer", "cpu_s": 9.0}]},
    ]
    # Both intervals have dt<=0 (0 then -10) -> no usable intervals.
    assert cpuwatch.recent_interval_cpu(hist, "explorer", count=3) == []


def test_recent_interval_cpu_skips_empty_prev():
    """An interval whose earlier sample has no valid pid is skipped (prev empty)."""
    hist = [
        {"_t": 100.0, "ncpu": 1, "cpu_procs": [{"n": "explorer", "cpu_s": 0.0}]},  # no pid
        {"_t": 120.0, "ncpu": 1, "cpu_procs": [{"pid": 1, "n": "explorer", "cpu_s": 5.0}]},
        {"_t": 140.0, "ncpu": 1, "cpu_procs": [{"pid": 1, "n": "explorer", "cpu_s": 9.0}]},
    ]
    # First interval: prev has no pid -> skipped. Second interval is valid.
    out = cpuwatch.recent_interval_cpu(hist, "explorer", count=3)
    assert len(out) == 1


# --------------------------------------------------------------- watchdog ----
def test_no_restart_below_threshold(tmp_path):
    clock = FakeClock()
    wd = _wd(tmp_path, _history([5.0, 10.0]), clock,
             threshold_pct=25.0, sustain=2)
    assert wd.tick() is None
    assert wd.calls == []


def test_no_restart_on_single_spike(tmp_path):
    """One interval over threshold must NOT trip it — needs `sustain` in a row."""
    clock = FakeClock()
    wd = _wd(tmp_path, _history([5.0, 35.0]), clock,
             threshold_pct=25.0, sustain=2)
    assert wd.offending() is None
    assert wd.tick() is None
    assert wd.calls == []


def test_restart_on_sustained_breach(tmp_path):
    clock = FakeClock()
    wd = _wd(tmp_path, _history([35.0, 40.0]), clock,
             threshold_pct=25.0, sustain=2)
    result = wd.tick()
    assert result is not None and result["status"] == "ok"
    assert wd.calls == [True]
    assert wd.action_count == 1
    assert result["trigger"]["recent_cpu_pct"] == [35.0, 40.0]


def test_cooldown_blocks_second_restart(tmp_path):
    clock = FakeClock()
    wd = _wd(tmp_path, _history([35.0, 40.0]), clock,
             threshold_pct=25.0, sustain=2, cooldown_s=600.0, max_age_s=1e9)
    assert wd.tick() is not None            # first restart
    assert wd.tick() is None                # still cooling down
    assert wd.calls == [True]
    clock.advance(601)
    assert wd.tick() is not None            # cooldown elapsed -> restart again
    assert wd.calls == [True, True]


def test_disabled_does_nothing(tmp_path):
    clock = FakeClock()
    wd = _wd(tmp_path, _history([35.0, 40.0]), clock,
             threshold_pct=25.0, sustain=2, enabled=False)
    assert wd.tick() is None
    assert wd.calls == []


def test_fix_disabled_does_nothing(tmp_path):
    clock = FakeClock()
    wd = _wd(tmp_path, _history([35.0, 40.0]), clock,
             threshold_pct=25.0, sustain=2, fix_enabled=False)
    assert wd.tick() is None
    assert wd.calls == []


def test_enabled_flag_persists(tmp_path):
    clock = FakeClock()
    p = tmp_path / "procwatch.json"
    wd = ExplorerCpuWatchdog(runner=lambda s, c: (0, ""), get_history=lambda: [],
                             state_file=p, clock=clock, enabled=True)
    assert wd.set_enabled(False) is False
    wd2 = ExplorerCpuWatchdog(runner=lambda s, c: (0, ""), get_history=lambda: [],
                              state_file=p, clock=clock, enabled=True)
    assert wd2.enabled is False


def test_status_shape(tmp_path):
    clock = FakeClock()
    wd = _wd(tmp_path, _history([35.0, 40.0]), clock,
             threshold_pct=25.0, sustain=2)
    st = wd.status()
    assert st["process"] == "explorer"
    assert st["offending"] is True
    assert st["current_cpu_pct"] == 40.0
    assert st["recent_cpu_pct"] == [35.0, 40.0]
    assert st["enabled"] is True and st["threshold_pct"] == 25.0
    assert st["stale"] is False and st["sample_age_s"] is not None


def test_stale_history_never_restarts(tmp_path):
    """A high reading from a stalled probe (old newest sample) must not act."""
    clock = FakeClock(t=5000.0)              # far ahead of the sample timestamps
    wd = _wd(tmp_path, _history([35.0, 40.0], end_t=1000.0), clock,
             threshold_pct=25.0, sustain=2, max_age_s=240.0)
    assert wd.is_stale() is True
    assert wd.offending() is None
    assert wd.tick() is None
    assert wd.calls == []
    assert wd.status()["stale"] is True


def test_fresh_after_stale_acts(tmp_path):
    """Once fresh samples arrive (newest near 'now'), the breach trips again."""
    clock = FakeClock(t=1000.0)
    wd = _wd(tmp_path, _history([35.0, 40.0], end_t=1000.0), clock,
             threshold_pct=25.0, sustain=2, max_age_s=240.0)
    assert wd.is_stale() is False
    assert wd.tick() is not None
    assert wd.calls == [True]


def test_empty_history_is_stale(tmp_path):
    clock = FakeClock()
    wd = _wd(tmp_path, [], clock, threshold_pct=25.0, sustain=2)
    assert wd.is_stale() is True
    assert wd.sample_age_s() is None
    assert wd.tick() is None


def test_short_series_no_action(tmp_path):
    """Fresh data but fewer than `sustain` intervals -> no action (guard line)."""
    clock = FakeClock(t=1000.0)
    wd = _wd(tmp_path, _history([35.0], end_t=1000.0), clock,
             threshold_pct=25.0, sustain=2, max_age_s=1e9)
    assert wd.is_stale() is False
    assert len(wd.recent()) < wd.sustain
    assert wd.offending() is None
    assert wd.tick() is None


def test_default_apply_restarts_via_runner(tmp_path):
    """With no apply_fn injected, the real _default_apply runs the restart
    command through the runner and reports the result."""
    calls = []

    def runner(shell, command):
        calls.append((shell, command))
        return 0, "restarted"

    wd = ExplorerCpuWatchdog(
        runner=runner, get_history=lambda: _history([35.0, 40.0], end_t=1000.0),
        state_file=tmp_path / "pw.json", threshold_pct=25.0, sustain=2,
        max_age_s=1e9, clock=FakeClock(t=1000.0))
    res = wd.tick()
    assert res["action"] == "restart_explorer" and res["status"] == "ok"
    assert calls and calls[0][0] == "pwsh"
    import sysperf.procwatch as procwatch
    assert calls[0][1] == procwatch.PS_RESTART_EXPLORER


def test_save_oserror_is_swallowed(tmp_path, monkeypatch):
    """A failing atomic write is swallowed (state stays in memory)."""
    import sysperf.procwatch as procwatch
    wd = _wd(tmp_path, [], FakeClock(), threshold_pct=25.0, sustain=2)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(procwatch.os, "replace", boom)
    monkeypatch.setattr(procwatch.os, "unlink", boom)   # cleanup also fails
    assert wd.set_enabled(False) is False               # no exception propagates
