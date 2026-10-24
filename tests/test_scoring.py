"""Full-coverage tests for scoring.py."""
import sysperf.scoring as scoring
from sysperf.scoring import (_smoothed_current, progress_pct, score_vmmem,
                     vmmem_trend, _row_note, build_scorecard)


# --------------------------------------------------------------- smoothing ---
def test_smoothed_window_disabled_returns_latest():
    assert _smoothed_current([], "x", 42, window=1) == 42
    assert _smoothed_current([], "x", 7, window=0) == 7


def test_smoothed_no_values_falls_back():
    hist = [{"x": None}, {"y": 1}]
    assert _smoothed_current(hist, "x", 99, window=5) == 99


def test_smoothed_odd_and_even():
    odd = [{"x": 3}, {"x": 1}, {"x": 2}]
    assert _smoothed_current(odd, "x", 0, window=5) == 2      # median of 1,2,3
    even = [{"x": 4}, {"x": 2}]
    assert _smoothed_current(even, "x", 0, window=5) == 3.0   # (2+4)/2


# ------------------------------------------------- session (gap) anchoring ----
def test_session_slice_no_gap_keeps_everything():
    hist = [{"_t": 100}, {"_t": 130}, {"_t": 160}]
    assert scoring._session_slice(hist) == hist
    assert scoring.session_sample_count(hist) == 3


def test_session_slice_anchors_at_newest_gap():
    hist = [{"_t": 100}, {"_t": 130},          # previous session
            {"_t": 50_000}, {"_t": 50_030}]    # after a long outage
    assert scoring._session_slice(hist) == hist[2:]
    assert scoring.session_sample_count(hist) == 2


def test_session_slice_uses_only_the_newest_gap():
    hist = [{"_t": 0}, {"_t": 9_000},          # gap 1
            {"_t": 9_030}, {"_t": 80_000},     # gap 2 (newest)
            {"_t": 80_030}]
    assert scoring._session_slice(hist) == hist[3:]


def test_session_slice_tolerates_missing_timestamps():
    hist = [{"x": 1}, {"x": 2}, {"_t": 500}]
    assert scoring._session_slice(hist) == hist      # cannot detect a gap
    assert scoring.session_sample_count([]) == 0


def test_smoothed_current_ignores_pre_reboot_samples():
    """The 2026-09-28 regression: an overnight gap must not feed the median.

    11 pre-shutdown samples at ~18,500 MB plus 4 post-boot samples at ~6,300 MB
    gave a median of 18,504 MB — the scorecard graded the previous session for
    ~13 min after boot. Only current-session samples are eligible now.
    """
    old = [{"_t": 1000.0 + i * 30, "committed_app_mb": 18_500 + i}
           for i in range(11)]
    new = [{"_t": 60_000.0 + i * 30, "committed_app_mb": 6_300 + i}
           for i in range(4)]
    hist = old + new
    scored = _smoothed_current(hist, "committed_app_mb", new[-1]["committed_app_mb"],
                               window=15)
    assert scored == 6_301.5          # median of the 4 post-boot samples only
    assert scored < 9_000             # i.e. now correctly scores as MET


def test_smoothed_current_single_post_gap_sample_is_that_sample():
    hist = [{"_t": 1000.0, "x": 900}, {"_t": 1030.0, "x": 910},
            {"_t": 90_000.0, "x": 5}]
    assert _smoothed_current(hist, "x", 5, window=15) == 5


# ---------------------------------------------------------------- progress ---
def test_progress_pct_equal_baseline_target():
    assert progress_pct(5, 5, 5) == 100.0


def test_progress_pct_clamped():
    assert progress_pct(0, 200, 100) == 100.0
    assert progress_pct(0, -50, 100) == 0.0
    assert progress_pct(0, 50, 100) == 50.0


# --------------------------------------------------------------- score_vmmem -
def test_vmmem_above_cap():
    prog, status, note = score_vmmem(11000, 10240, 0, 700, 6)
    assert status == "regress" and prog == 0.0 and "cap" in note


def test_vmmem_measuring():
    prog, status, note = score_vmmem(5000, 10240, 0, 100, 3)
    assert status == "measuring" and prog is None


def test_vmmem_falling_and_flat():
    assert score_vmmem(5000, 10240, -100, 700, 6)[1] == "met"
    assert score_vmmem(5000, 10240, 0, 700, 6)[1] == "met"


def test_vmmem_rising_lots_of_headroom():
    prog, status, _ = score_vmmem(5000, 10240, 400, 700, 6)   # ~13h, util<0.5
    assert status == "met" and prog == 100.0


def test_vmmem_rising_high_util_partial():
    # util >= 0.5 so the ">=12h and util<0.5" branch is skipped -> partial
    prog, status, _ = score_vmmem(6000, 10240, 100, 700, 6)
    assert status == "partial"


def test_vmmem_rising_partial_window():
    prog, status, _ = score_vmmem(5000, 10240, 1000, 700, 6)  # ~5h
    assert status == "partial" and 50.0 <= prog <= 95.0


def test_vmmem_rising_regress():
    prog, status, _ = score_vmmem(9000, 10240, 1000, 700, 6)  # ~1.2h to cap
    assert status == "regress"


# ---------------------------------------------------------------- vmmem_trend -
def test_trend_empty():
    assert vmmem_trend([]) == (0.0, 0.0, 0)


def test_trend_too_few():
    assert vmmem_trend([{"_t": 1.0, "vmmem_mb": 100}])[2] == 1


def test_trend_short_span_returns_zero_slope():
    hist = [{"_t": 1000.0 + i, "vmmem_mb": 100 + i} for i in range(6)]  # span=5s
    slope, span, n = vmmem_trend(hist)
    assert slope == 0.0 and n == 6


def test_trend_real_slope():
    hist = [{"_t": 1000.0 + i * 120.0, "vmmem_mb": 1000 + i * 100} for i in range(7)]
    slope, span, n = vmmem_trend(hist)
    assert slope > 0 and span >= 600 and n == 7


def test_trend_reanchor_on_gap():
    # a large collector gap should re-anchor the window to the post-gap points
    hist = [{"_t": i * 100.0, "vmmem_mb": 5000} for i in range(4)]
    hist += [{"_t": 100000.0 + i * 120.0, "vmmem_mb": 3000 + i * 50} for i in range(7)]
    slope, span, n = vmmem_trend(hist)
    assert n == 7


def test_trend_reanchor_on_collapse():
    # a collapse from a high steady state (WSL restart) re-anchors
    hist = [{"_t": i * 120.0, "vmmem_mb": 6000} for i in range(4)]
    hist += [{"_t": (4 + i) * 120.0, "vmmem_mb": 2000 + i * 30} for i in range(7)]
    slope, span, n = vmmem_trend(hist)
    assert n == 7


# ------------------------------------------------------------------ _row_note -
def test_row_note_startup():
    n = _row_note("startup_apps", 10, {"startup_apps_total": 25})
    assert "registered" in n


def test_row_note_net_remote_full():
    n = _row_note("net_remote", 25, {"net_established": 60, "net_remote_raw": 90,
                                     "net_remote_hosts": 12, "net_loopback": 5})
    assert "sustained remote" in n and "distinct remote hosts" in n


def test_row_note_committed_with_and_without():
    assert _row_note("committed_app_mb", 7000,
                     {"committed_system_mb": 7000, "committed_mb": 20000}) is not None
    assert _row_note("committed_app_mb", 7000, {}) is None


def test_row_note_handles():
    assert _row_note("handles", 87000,
                     {"handles_total": 180000, "handles_system": 90000}) is not None
    assert _row_note("handles", 87000, {}) is None


def test_row_note_hw_faults():
    assert "OK" in _row_note("hw_faults", 0, {})
    assert "error codes" in _row_note("hw_faults", 2,
                                      {"hw_problem_devices": 2, "hw_disks_unhealthy": 1})


def test_row_note_unknown_key():
    assert _row_note("something_else", 1, {}) is None


# --------------------------------------------------------------- scorecard ----
class _FakeScorer:
    """Minimal scorer double for build_scorecard."""
    def __init__(self, adaptive=None):
        self._a = adaptive

    def enrich(self, key, cur, base, target, hib):
        if key == "vmmem_mb":
            return {"learning": False, "adaptive_progress": 50.0}
        return None

    def summary(self, rows):
        return {"self_improvement_score": 80.0}


def test_build_scorecard_full(monkeypatch):
    monkeypatch.setattr(scoring, "CRITERIA", [
        ("committed_app_mb", "Commit", 14000, 9000, False, "MB"),
        ("available_mb", "Avail", 7013, 12000, True, "MB"),
        ("vmmem_mb", "vmmem", 5627, 10240, False, "MB"),
        ("net_remote", "Net", 40, 30, False, ""),
        ("startup_apps", "Startup", 19, 10, False, ""),
        ("missing_key", "Missing", 1, 2, True, ""),
    ])
    # net_remote: carry-forward WITH an override window (hits the override branch)
    # startup_apps: carry-forward WITHOUT an override (hits the elif `scored=cur`)
    monkeypatch.setattr(scoring, "CARRY_FORWARD_KEYS", ("net_remote", "startup_apps"))
    monkeypatch.setattr(scoring, "SMOOTH_WINDOW_OVERRIDES", {"net_remote": 3})
    latest = {"committed_app_mb": 8000, "available_mb": 12500, "vmmem_mb": 4000,
              "net_remote": 25, "startup_apps": 10,
              "hw_problem_devices": 1, "hw_disks_unhealthy": 1}
    hist = [dict(latest, _t=i * 120.0, vmmem_mb=4000 + i) for i in range(7)]
    out = build_scorecard(latest, hist, _FakeScorer())
    keys = {r["key"] for r in out["rows"]}
    assert "missing_key" not in keys                      # None current skipped
    assert out["counts"]["met"] >= 1
    assert "hw_faults" in latest                          # derived field set
    vm = next(r for r in out["rows"] if r["key"] == "vmmem_mb")
    assert "adaptive" in vm                               # enrich attached
    # scoring provenance for the UI's "stabilising" label
    assert out["session_samples"] == 7
    assert out["smooth_window"] == scoring.SCORE_SMOOTH_WINDOW
    assert out["warming_up"] is False


def test_build_scorecard_flags_warming_up_after_a_gap(monkeypatch):
    """Only 2 post-reboot samples -> honest but thin, so flag it."""
    monkeypatch.setattr(scoring, "CRITERIA", [
        ("available_mb", "Avail", 7013, 12000, True, "MB"),
    ])
    latest = {"available_mb": 12500}
    hist = ([dict(latest, _t=1000.0 + i * 30) for i in range(10)]     # old session
            + [dict(latest, _t=90_000.0), dict(latest, _t=90_030.0)])  # after reboot
    out = build_scorecard(latest, hist, _FakeScorer())
    assert out["session_samples"] == 2
    assert out["warming_up"] is True


def test_build_scorecard_regress_and_measuring(monkeypatch):
    monkeypatch.setattr(scoring, "CRITERIA", [
        ("available_mb", "Avail", 7013, 12000, True, "MB"),   # higher better, regress
        ("vmmem_mb", "vmmem", 5627, 10240, False, "MB"),      # measuring (short span)
    ])
    latest = {"available_mb": 5000, "vmmem_mb": 4000}
    hist = [{"available_mb": 5000, "vmmem_mb": 4000, "_t": 1.0}]
    out = build_scorecard(latest, hist, _FakeScorer())
    av = next(r for r in out["rows"] if r["key"] == "available_mb")
    assert av["status"] == "regress"
    vm = next(r for r in out["rows"] if r["key"] == "vmmem_mb")
    assert vm["status"] == "measuring" and vm["progress"] is None


def test_locked_samples_do_not_count_while_unlocked_ones_exist():
    # OneDrive syncs on purpose while locked; that burst must not be scored.
    hist = ([{"_t": float(i), "net_remote": 12, "session_locked": False} for i in range(5)]
            + [{"_t": float(5 + i), "net_remote": 72, "session_locked": True} for i in range(10)])
    assert _smoothed_current(hist, "net_remote", 72, 15) == 12


def test_only_locked_samples_are_still_used_when_nothing_else_exists():
    hist = [{"_t": float(i), "net_remote": 30, "session_locked": True} for i in range(5)]
    assert _smoothed_current(hist, "net_remote", 30, 5) == 30


def test_smoothing_windows_are_defined_in_time_not_sample_counts():
    from sysperf import config
    assert config.SMOOTH_WINDOW_OVERRIDES["net_remote"] * config.SAMPLE_SECONDS == 3600
    assert config.WIFI_LINK_SMOOTH_WINDOW * config.SAMPLE_SECONDS == 1800
