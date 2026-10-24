"""Full-coverage tests for cpuwatch.consistent_cpu."""
import sysperf.cpuwatch as cpuwatch


def _sample(t, procs, ncpu=8):
    """A minimal history sample carrying a process table at time t."""
    return {"_t": float(t), "ncpu": ncpu,
            "cpu_procs": [dict(p) if isinstance(p, dict) else p for p in procs]}


def test_unavailable_when_too_few_samples():
    out = cpuwatch.consistent_cpu([])
    assert out == {"available": False, "intervals": 0, "window_min": 0.0,
                   "ncpu": None, "min_pct": cpuwatch.CPU_MIN_PCT,
                   "consumers": []}
    one = [_sample(0, [{"n": "chrome", "pid": 1, "cpu_s": 10, "ws_mb": 100}])]
    assert cpuwatch.consistent_cpu(one)["available"] is False


def test_ignores_non_dict_and_fieldless_history():
    """FakeCollector-style history ([1,2,3]) and dicts without cpu_procs."""
    assert cpuwatch.consistent_cpu([1, 2, 3])["available"] is False
    assert cpuwatch.consistent_cpu(
        [{"_t": 0}, {"_t": 20}])["available"] is False


def test_busy_process_is_consistent():
    # chrome burns ~1 core (8s per 8s interval on 8 cpus => ~12.5%) every interval
    hist = []
    for i in range(6):
        hist.append(_sample(i * 8, [
            {"n": "chrome", "pid": 1, "cpu_s": 8 * i, "ws_mb": 500},
            {"n": "idle_old", "pid": 2, "cpu_s": 1000, "ws_mb": 50},  # no delta
        ]))
    out = cpuwatch.consistent_cpu(hist, min_pct=3.0)
    assert out["available"] is True
    assert out["ncpu"] == 8
    assert out["intervals"] == 5
    names = {c["name"]: c for c in out["consumers"]}
    assert "chrome" in names
    chrome = names["chrome"]
    assert chrome["consistent"] is True
    assert 12.0 <= chrome["median_cpu_pct"] <= 13.0
    assert 12.0 <= chrome["current_cpu_pct"] <= 13.0    # latest interval
    assert chrome["consistency_pct"] == 100.0
    assert "idle_old" not in names          # accrued CPU only before the window


def test_intermittent_process_flagged_not_consistent():
    # busy in 2 of 5 intervals (40% < 60%), at differing intensities so the
    # median of its busy periods sits below its peak.
    cum = [0, 0, 8, 8, 12, 12]        # deltas: -,0,8,0,4,0  => 12.5% then 6.25%
    hist = [_sample(i * 8, [
        {"n": "spiker", "pid": 9, "cpu_s": cum[i], "ws_mb": 80},
    ]) for i in range(6)]
    out = cpuwatch.consistent_cpu(hist, min_pct=3.0)
    sp = {c["name"]: c for c in out["consumers"]}.get("spiker")
    assert sp is not None                # heavy peaks keep it visible
    assert sp["consistent"] is False
    assert sp["active_intervals"] == 2
    assert sp["consistency_pct"] == 40.0
    assert sp["current_cpu_pct"] == 0.0     # idle in the most recent interval
    assert sp["peak_cpu_pct"] > sp["median_cpu_pct"]


def test_noise_below_floor_is_dropped():
    hist = [
        _sample(0, [{"n": "tray", "pid": 5, "cpu_s": 0.0, "ws_mb": 10}]),
        _sample(8, [{"n": "tray", "pid": 5, "cpu_s": 0.01, "ws_mb": 10}]),
    ]
    out = cpuwatch.consistent_cpu(hist, min_pct=3.0)
    assert out["available"] is True and out["consumers"] == []


def test_counter_reset_and_new_pid_are_skipped():
    hist = [
        _sample(0, [{"n": "app", "pid": 1, "cpu_s": 100.0, "ws_mb": 20}]),
        _sample(8, [{"n": "app", "pid": 1, "cpu_s": 1.0, "ws_mb": 20},   # reset
                    {"n": "new", "pid": 2, "cpu_s": 50.0, "ws_mb": 20}]),  # unseen
    ]
    out = cpuwatch.consistent_cpu(hist)
    assert out["available"] is True and out["consumers"] == []


def test_non_positive_dt_and_empty_prev_are_skipped():
    # s0 empty table -> (s0,s1) has empty prev (skipped); (s1,s2) has dt == 0
    # (skipped). No interval is ever counted -> unavailable, but ncpu is observed.
    hist = [
        {"_t": 0, "ncpu": 4, "cpu_procs": []},                        # empty prev
        _sample(8, [{"n": "a", "pid": 1, "cpu_s": 8.0, "ws_mb": 1}], ncpu=4),
        _sample(8, [{"n": "a", "pid": 1, "cpu_s": 9.0, "ws_mb": 1}], ncpu=4),  # dt 0
    ]
    out = cpuwatch.consistent_cpu(hist)
    assert out["available"] is False
    assert out["intervals"] == 0
    assert out["ncpu"] == 4


def test_non_dict_proc_entries_are_ignored():
    hist = [
        _sample(0, [{"n": "a", "pid": 1, "cpu_s": 0.0, "ws_mb": 1}, "junk"]),
        _sample(8, [{"n": "a", "pid": 1, "cpu_s": 8.0, "ws_mb": 1}, 42]),
    ]
    out = cpuwatch.consistent_cpu(hist)
    assert {c["name"] for c in out["consumers"]} == {"a"}


def test_top_limit_and_ranking_order():
    rates = {"p1": 1.0, "p2": 2.0, "p3": 4.0, "p4": 6.0, "p5": 8.0}
    hist = []
    for i in range(5):
        procs = [{"n": n, "pid": j + 1, "cpu_s": rates[n] * i, "ws_mb": 10}
                 for j, n in enumerate(rates)]
        hist.append(_sample(i * 8, procs, ncpu=8))
    out = cpuwatch.consistent_cpu(hist, min_pct=0.5, top=3)
    got = [c["name"] for c in out["consumers"]]
    assert got == ["p5", "p4", "p3"]
    assert all(c["consistent"] for c in out["consumers"])
    assert out["window_min"] == round((4 * 8) / 60.0, 1)
