"""Full-coverage tests for collector.py."""
import json
import os
import subprocess

import pytest

import sysperf.collector as collector
import sysperf.components as components


class FakeScorer:
    def __init__(self):
        self.seeded = None
        self.updates = []

    def seed(self, hist):
        self.seeded = list(hist)

    def update(self, sample, persist=True, only=None):
        self.updates.append(sample)

    def enrich(self, *a, **k):
        return None

    def summary(self, rows):
        return {"self_improvement_score": None}


class FakePath:
    """Controllable stand-in for the HISTORY_FILE Path."""
    def __init__(self, exists=True, text="", raise_read=False,
                 raise_open=False, size=10):
        self._exists = exists
        self._text = text
        self._raise_read = raise_read
        self._raise_open = raise_open
        self._size = size
        self.written = None

    def exists(self):
        return self._exists

    def read_text(self):
        if self._raise_read:
            raise OSError("read boom")
        return self._text

    def write_text(self, data):
        self.written = data

    def open(self, mode):
        if self._raise_open:
            raise OSError("open boom")
        import io
        return io.StringIO()

    def stat(self):
        class S:
            st_size = self._size
        return S()


def _wslpath_run(argv, **kw):
    class R:
        returncode = 0
        stdout = "C:\\path\\collect_win.ps1"
        stderr = ""
    return R()


@pytest.fixture
def col(monkeypatch):
    monkeypatch.setattr(collector.subprocess, "run", _wslpath_run)
    monkeypatch.setattr(collector, "HISTORY_FILE", FakePath(exists=False))
    sc = FakeScorer()
    c = collector.Collector(sc)
    return c, sc


# --------------------------------------------------------------- __init__ -----
def test_init_no_history(col):
    c, sc = col
    assert c.history == c.history and len(c.history) == 0
    assert sc.seeded is None                     # no file -> seed not called


def test_load_history_good_and_bad(monkeypatch):
    monkeypatch.setattr(collector.subprocess, "run", _wslpath_run)
    good = json.dumps({"vmmem_mb": 100, "_t": 1.0})
    fp = FakePath(exists=True, text=good + "\nnot-json\n" + json.dumps({"x": 2}))
    monkeypatch.setattr(collector, "HISTORY_FILE", fp)
    sc = FakeScorer()
    c = collector.Collector(sc)
    assert len(c.history) == 2                    # bad line skipped
    assert c.latest == {"x": 2}
    assert sc.seeded is not None                  # seeded from history


def test_load_history_oserror(monkeypatch):
    monkeypatch.setattr(collector.subprocess, "run", _wslpath_run)
    monkeypatch.setattr(collector, "HISTORY_FILE", FakePath(exists=True, raise_read=True))
    c = collector.Collector(FakeScorer())         # OSError swallowed
    assert len(c.history) == 0


# ---------------------------------------------------------------- _persist -----
def test_persist_writes_and_trims(col, monkeypatch):
    c, _ = col
    fp = FakePath(exists=True, text="a\nb\nc\n", size=10)
    monkeypatch.setattr(collector, "HISTORY_FILE", fp)
    monkeypatch.setattr(collector, "HISTORY_MAX_BYTES", 1)   # force trim branch
    c._persist({"k": 1})
    assert fp.written is not None                 # trim rewrote the file


def test_persist_oserror(col, monkeypatch):
    c, _ = col
    monkeypatch.setattr(collector, "HISTORY_FILE", FakePath(raise_open=True))
    c._persist({"k": 1})                          # OSError swallowed


# ------------------------------------------------------------------ sample -----
def _ps_run_factory(stdout, stderr="", raise_exc=None):
    def run(argv, **kw):
        # docker probe used by _wsl_metrics
        if argv and argv[0] == "docker":
            class D:
                stdout = "abc\n"
                stderr = ""
            return D()
        if raise_exc:
            raise raise_exc
        class R:
            returncode = 0
            stdout = _out
            stderr = _err
        R.stdout = stdout
        R.stderr = stderr
        return R()
    _out, _err = stdout, stderr
    return run


def test_sample_success_full(col, monkeypatch):
    c, sc = col
    payload = json.dumps({"vmmem_mb": 123, "cpu_pct": 5})
    monkeypatch.setattr(collector.subprocess, "run", _ps_run_factory(payload))
    c.sample()
    assert c.latest["vmmem_mb"] == 123
    assert "_t" in c.latest and "wsl" in c.latest
    assert sc.updates                             # scorer.update called
    assert c.error is None


def test_sample_full_sweep_appends_flag(col, monkeypatch):
    """A Full sweep (sample_count a positive multiple of FULL_EVERY) runs the
    heavier probe with -Full; the first sample (count 0) never does."""
    c, _ = col
    seen = {}

    def run(argv, **kw):
        if argv and argv[0] == "docker":
            class D:
                stdout = "abc\n"; stderr = ""
            return D()
        seen["argv"] = list(argv)
        class R:
            returncode = 0; stdout = json.dumps({"vmmem_mb": 1}); stderr = ""
        return R()

    monkeypatch.setattr(collector.subprocess, "run", run)
    c.sample_count = collector.FULL_EVERY         # > 0 and % FULL_EVERY == 0 -> full
    c.sample()
    assert "-Full" in seen["argv"]


def test_sample_carry_forward(col, monkeypatch):
    c, sc = col
    c.latest = {"drv_total": 99, "vmmem_mb": 1}   # prior full-tier value
    c.sample_count = 1                            # -> not a full sweep
    payload = json.dumps({"vmmem_mb": 200})
    monkeypatch.setattr(collector.subprocess, "run", _ps_run_factory(payload))
    c.sample()
    assert c.latest["drv_total"] == 99            # carried forward


def test_sample_empty_output(col, monkeypatch):
    c, _ = col
    monkeypatch.setattr(collector.subprocess, "run",
                        _ps_run_factory("", stderr="explosion"))
    c.sample()
    assert c.error and "explosion" in c.error


def test_sample_exception(col, monkeypatch):
    c, _ = col
    monkeypatch.setattr(collector.subprocess, "run",
                        _ps_run_factory("", raise_exc=RuntimeError("x")))
    c.sample()
    assert c.error and "RuntimeError" in c.error


# ------------------------------------------------------------- _wsl_metrics ----
@pytest.mark.skipif(os.name == "nt", reason="requires Linux /proc; Windows fallback covered separately")
def test_wsl_metrics_success(monkeypatch):
    def run(argv, **kw):
        class D:
            stdout = "id1\nid2\n"
        return D()
    monkeypatch.setattr(collector.subprocess, "run", run)
    out = collector.Collector._wsl_metrics()
    assert "total_mb" in out and out["containers_running"] == 2


def test_wsl_metrics_proc_error(monkeypatch):
    monkeypatch.setattr(collector, "PLATFORM", "wsl")
    class BadPath:
        def __init__(self, p):
            pass

        def read_text(self):
            raise OSError("no proc")
    monkeypatch.setattr(collector, "Path", BadPath)
    monkeypatch.setattr(collector.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no docker")))
    out = collector.Collector._wsl_metrics()
    assert "error" in out and out["containers_running"] is None


def test_wsl_metrics_not_read_on_native_windows(monkeypatch):
    # There is no Linux guest to read from a native install; /proc must not be
    # probed (it used to surface as a confusing '\\proc\\meminfo' error).
    monkeypatch.setattr(collector, "PLATFORM", "windows")
    assert collector.Collector._wsl_metrics() == {"available": False}


# ------------------------------------------------------------------- loop ------
def test_loop_runs_then_stops(col, monkeypatch):
    c, _ = col
    n = {"i": 0}

    def fake_sample():
        n["i"] += 1
        if n["i"] >= 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(c, "sample", fake_sample)
    monkeypatch.setattr(collector.time, "sleep", lambda s: None)
    with pytest.raises(KeyboardInterrupt):
        c.loop()
    assert n["i"] == 2                            # slept once, sampled twice


# ---------------------------------------------------------------- snapshot -----
def test_snapshot_cache_miss_then_hit(col, monkeypatch):
    c, _ = col
    monkeypatch.setattr(components, "_linux_versions", lambda: {})
    sample = {"vmmem_mb": 100, "_t": 1.0, "cpu_pct": 5}
    c.history.append(sample)
    c.latest = dict(sample)
    first = c.snapshot()
    assert "scorecard" in first and "series" in first
    assert first["meta"]["samples"] == 1
    second = c.snapshot()                         # served from cache
    assert second["meta"]["server_time"]          # refreshed timestamp


# ------------------------------------------------- wifi per-network scoring ----
def _wifi_hist(rates, ssid="Airtel_jude_4239"):
    return [{"wifi": {"present": True, "ssid": ssid, "link_mbps": r}}
            for r in rates]


def _latest(rate, ssid="Airtel_jude_4239"):
    return {"wifi": {"present": True, "ssid": ssid, "link_mbps": rate}}


def test_wifi_typical_is_median_not_latest():
    # Rate adaptation swings wildly; the scored value must be the sustained median,
    # not whichever instantaneous sample landed last.
    out = collector._with_wifi_typical(
        _latest(26), _wifi_hist([130, 144, 130, 144, 72, 780, 26]))
    assert out["wifi"]["link_mbps_typical"] == 130
    assert out["wifi"]["link_mbps"] == 26          # live value preserved
    assert out["wifi"]["link_mbps_samples"] == 7


@pytest.fixture
def home_plan(monkeypatch):
    # Plans come from sysperf.config.json ("wifi": {"plan_mbps": ...}); the code
    # no longer ships anyone's home network.
    monkeypatch.setattr(collector, "WIFI_NETWORK_PLAN_MBPS", {"Airtel_jude_4239": 100})


def test_wifi_scored_against_declared_plan_not_absolute_ladder(home_plan):
    # 130 Mbps PHY on a 100 Mbps plan: ~78 Mbps usable, so the radio carries most
    # of what the plan can deliver. The old absolute ladder called this 45%.
    out = collector._with_wifi_typical(_latest(130), _wifi_hist([130] * 40))
    w = out["wifi"]
    assert w["score_basis"] == "plan" and w["plan_mbps"] == 100
    assert w["usable_mbps"] == 78.0
    assert w["score"] == 78


def test_wifi_full_marks_once_link_carries_whole_plan(home_plan):
    # 200 Mbps PHY -> 120 Mbps usable > 100 Mbps plan: the radio is not the
    # bottleneck, so it scores 100 rather than being punished for not being 1 Gbps.
    out = collector._with_wifi_typical(_latest(200), _wifi_hist([200] * 40))
    assert out["wifi"]["score"] == 100


def test_wifi_unknown_network_learns_its_own_capability():
    # The office network has no declared plan, so it calibrates against the p90 of
    # what it has actually reached rather than the home plan.
    hist = _wifi_hist([613] * 60 + [907] * 5, ssid="EWA@INTERNET 8")
    out = collector._with_wifi_typical(_latest(613, "EWA@INTERNET 8"), hist)
    w = out["wifi"]
    assert w["score_basis"] == "capability"
    assert "plan_mbps" not in w
    assert w["capability_mbps"] >= 613 and w["score"] <= 100


def test_wifi_samples_never_pool_across_networks():
    # Home and office samples must not mix — that would score the office link
    # against home rates (and vice versa).
    hist = (_wifi_hist([98] * 50, ssid="Airtel_jude_4239")
            + _wifi_hist([613] * 50, ssid="EWA@INTERNET 8"))
    out = collector._with_wifi_typical(_latest(613, "EWA@INTERNET 8"), hist)
    assert out["wifi"]["link_mbps_typical"] == 613
    # only office samples, bounded by the (time-based) window
    assert out["wifi"]["link_mbps_samples"] == min(50, collector.WIFI_LINK_SMOOTH_WINDOW)


def test_wifi_new_network_calibrating_rather_than_shaky_score():
    hist = _wifi_hist([450] * 5, ssid="SomeCafe")
    out = collector._with_wifi_typical(_latest(450, "SomeCafe"), hist)
    assert out["wifi"]["score_basis"] == "calibrating"
    assert out["wifi"]["score"] is None


def test_wifi_typical_absent_without_history():
    out = collector._with_wifi_typical(_latest(72), [])
    assert "link_mbps_typical" not in out["wifi"]


def test_wifi_typical_ignores_non_numeric_and_missing():
    hist = _wifi_hist([144, None, 130]) + [{"wifi": None}, {}, {"wifi": {}}]
    out = collector._with_wifi_typical(_latest(144), hist)
    assert out["wifi"]["link_mbps_typical"] == 137.0   # median of [130, 144]
    assert out["wifi"]["link_mbps_samples"] == 2


def test_wifi_typical_no_wifi_block_is_passthrough():
    latest = {"cpu_pct": 5}
    assert collector._with_wifi_typical(latest, []) is latest


def test_wifi_typical_window_is_bounded(monkeypatch):
    monkeypatch.setattr(collector, "WIFI_LINK_SMOOTH_WINDOW", 3)
    out = collector._with_wifi_typical(_latest(600), _wifi_hist([26, 26, 26, 600, 600, 600]))
    assert out["wifi"]["link_mbps_typical"] == 600
    assert out["wifi"]["link_mbps_samples"] == 3


def test_snapshot_exposes_wifi_score(col, monkeypatch, home_plan):
    c, _ = col
    monkeypatch.setattr(components, "_linux_versions", lambda: {})
    for r in (130, 144, 72):
        c.history.append({"_t": 1.0, "wifi": {"present": True,
                                              "ssid": "Airtel_jude_4239",
                                              "link_mbps": r}})
    c.latest = {"_t": 1.0, "wifi": {"present": True, "ssid": "Airtel_jude_4239",
                                    "link_mbps": 72}}
    snap = c.snapshot()
    assert snap["latest"]["wifi"]["link_mbps_typical"] == 130
    assert snap["latest"]["wifi"]["score_basis"] == "plan"


def test_a_platform_without_powershell_reports_why_immediately(monkeypatch):
    """Before 2.8.0 this looked identical to a healthy collector that had simply
    not sampled yet, which is how the WSL-only assumption stayed hidden."""
    monkeypatch.setattr(collector, "CAN_COLLECT", False)
    c = collector.Collector(FakeScorer())
    assert c.error and "no PowerShell found" in c.error
    assert "sysperf --where" in c.error


def test_a_platform_with_powershell_starts_with_no_error(monkeypatch):
    monkeypatch.setattr(collector, "CAN_COLLECT", True)
    assert collector.Collector(FakeScorer()).error is None


# ------------------------------------------- 4.1: store, shell, probe parse -----
class FakeStore:
    def __init__(self, rows=(), fail_read=False, fail_write=False):
        self.rows, self.fail_read, self.fail_write = list(rows), fail_read, fail_write
        self.appended = []

    def recent(self, n):
        if self.fail_read:
            raise RuntimeError("db locked")
        return self.rows[-n:]

    def append(self, s):
        if self.fail_write:
            raise RuntimeError("disk full")
        self.appended.append(s)


class FakeShell:
    def __init__(self, out=None, exc=None):
        self.out, self.exc, self.calls = out, exc, []

    def run(self, script, args, timeout):
        self.calls.append((script, args, timeout))
        if self.exc:
            raise self.exc
        return self.out

    def stats(self):
        return {"alive": True, "total_runs": len(self.calls)}


def _plain(monkeypatch):
    monkeypatch.setattr(collector.subprocess, "run", _wslpath_run)
    monkeypatch.setattr(collector, "HISTORY_FILE", FakePath(exists=False))


def test_parse_probe_takes_the_last_object_line():
    assert collector._parse_probe('{"a": 1}\r\n') == {"a": 1}
    assert collector._parse_probe('WARNING: slow\n{"a": 2}') == {"a": 2}
    with pytest.raises(ValueError):
        collector._parse_probe("WARNING only")
    for bad in ("[]", "{}"):
        with pytest.raises(ValueError, match="metrics object"):
            collector._parse_probe(bad)


def test_store_seeds_history_and_receives_samples(monkeypatch):
    _plain(monkeypatch)
    store = FakeStore(rows=[{"_t": 1.0, "vmmem_mb": 5}, {"_t": 2.0, "vmmem_mb": 6}])
    sc = FakeScorer()
    c = collector.Collector(sc, store=store)
    assert len(c.history) == 2 and c.latest["vmmem_mb"] == 6 and c.seeded_latest
    assert sc.seeded and store.appended == []
    monkeypatch.setattr(collector.subprocess, "run", _ps_run_factory('{"vmmem_mb": 7}'))
    c.sample()
    assert store.appended[-1]["vmmem_mb"] == 7 and c.last_ok_at == c.latest["_t"]
    assert c.stats()["store"] == "sqlite" and c.stats()["shell"] is None


def test_store_failures_never_stop_the_collector(monkeypatch, caplog):
    _plain(monkeypatch)
    c = collector.Collector(FakeScorer(), store=FakeStore(fail_read=True, fail_write=True))
    assert len(c.history) == 0 and "could not load history" in caplog.text
    monkeypatch.setattr(collector.subprocess, "run", _ps_run_factory('{"vmmem_mb": 7}'))
    c.sample()
    assert c.latest["vmmem_mb"] == 7 and "persist failed" in caplog.text


def test_warm_shell_is_used_and_falls_back_to_a_fresh_process(monkeypatch, caplog):
    from sysperf.pwsh_session import SessionError
    _plain(monkeypatch)
    sh = FakeShell(out='{"vmmem_mb": 11}')
    c = collector.Collector(FakeScorer(), shell=sh)
    c.sample_count = collector.FULL_EVERY
    c.sample()
    assert c.latest["vmmem_mb"] == 11 and sh.calls[0][1] == ("-Full",)
    sh.exc = SessionError("no result within 120 s")
    monkeypatch.setattr(collector.subprocess, "run", _ps_run_factory('{"vmmem_mb": 12}'))
    c.sample()
    assert c.latest["vmmem_mb"] == 12 and c.shell_fallbacks == 1
    assert "using a one-shot process" in caplog.text
    st = c.stats()
    assert st["shell"]["total_runs"] == 2 and st["shell_fallbacks"] == 1


def test_failed_sample_is_counted(col, monkeypatch):
    c, _ = col
    monkeypatch.setattr(collector.subprocess, "run", _ps_run_factory("[]"))
    c.sample()
    assert c.sample_failures == 1 and "metrics object" in c.error


def test_baseline_capture_after_a_sample_invalidates_the_cache(col, monkeypatch):
    c, _ = col
    monkeypatch.setattr(collector.subprocess, "run", _ps_run_factory('{"vmmem_mb": 1}'))
    monkeypatch.setattr(collector.machine_profile, "maybe_capture", lambda hist: True)
    c._snap_cache = {"stale": True}
    c.sample()
    assert c._snap_cache is None


def test_target_overrides_come_from_the_store(col):
    c, _ = col
    assert c._target_overrides() is None

    class TS:
        def overrides(self):
            return {"handles": 90000}
    c.target_store = TS()
    assert c._target_overrides() == {"handles": 90000}
