"""Scoring maths: progress, smoothing, the ceiling criterion and the trend."""
from __future__ import annotations

import time

import pytest

from sysperf.scoring import (
    build_scorecard,
    median,
    progress_pct,
    score_ceiling,
    smoothed_current,
    vmmem_trend,
)

TREND = {"min_samples": 6, "min_span_seconds": 600, "window_seconds": 3600,
         "gap_seconds": 120}


def test_progress_is_clamped_both_ends():
    assert progress_pct(100, 100, 50) == 0.0        # no movement
    assert progress_pct(100, 50, 50) == 100.0       # target reached
    assert progress_pct(100, 20, 50) == 100.0       # overshoot does not exceed 100
    assert progress_pct(100, 150, 50) == 0.0        # regression floors at 0


def test_progress_handles_higher_is_better():
    assert progress_pct(10, 15, 20) == 50.0


def test_progress_survives_equal_baseline_and_target():
    assert progress_pct(50, 50, 50) == 100.0


def test_median_ignores_nulls():
    assert median([1, None, 3]) == 2
    assert median([None]) is None


def test_smoothing_rejects_a_single_spike():
    hist = [{"x": 10} for _ in range(14)] + [{"x": 900}]
    assert smoothed_current(hist, "x", 900, 15) == 10


def test_smoothing_follows_a_sustained_shift():
    hist = [{"x": 10} for _ in range(8)] + [{"x": 40} for _ in range(8)]
    assert smoothed_current(hist, "x", 40, 15) > 10


def test_smoothing_disabled_returns_latest():
    assert smoothed_current([{"x": 1}], "x", 99, 1) == 99


def test_ceiling_above_cap_is_a_regression():
    prog, status, note = score_ceiling(11000, 10000, 0, 3600, 20, TREND)
    assert (prog, status) == (0.0, "regress")
    assert "cap" in note


def test_ceiling_abstains_until_the_window_is_long_enough():
    prog, status, _ = score_ceiling(5000, 10000, -100, 120, 3, TREND)
    assert prog is None and status == "measuring"


def test_ceiling_met_when_flat_or_falling():
    assert score_ceiling(5000, 10000, -50, 3600, 20, TREND)[1] == "met"
    assert score_ceiling(5000, 10000, 0, 3600, 20, TREND)[1] == "met"


def test_ceiling_tolerates_a_slow_rise_with_plenty_of_headroom():
    # 2 GB used of a 10 GB cap, rising 100 MB/h: 80 h of runway is not a leak.
    assert score_ceiling(2000, 10000, 100, 3600, 20, TREND)[1] == "met"


def test_ceiling_flags_an_imminent_cap_breach():
    prog, status, _ = score_ceiling(9500, 10000, 1000, 3600, 20, TREND)
    assert status == "regress" and prog < 40


def test_trend_needs_a_minimum_span():
    now = time.time()
    hist = [{"_t": now - 60 + i * 10, "vmmem_mb": 1000 + i * 100} for i in range(6)]
    slope, span, n = vmmem_trend(hist, TREND)
    assert slope == 0.0 and n == 6 and span < 600


def test_trend_measures_a_real_rise():
    now = time.time()
    hist = [{"_t": now - 3600 + i * 300, "vmmem_mb": 1000 + i * 100} for i in range(12)]
    slope, span, n = vmmem_trend(hist, TREND)
    assert slope > 0 and span >= 600 and n == 12


def test_trend_reanchors_after_a_working_set_collapse():
    """A VM restart must not read as a rise from the post-restart low point."""
    now = time.time()
    high = [{"_t": now - 3600 + i * 120, "vmmem_mb": 8000} for i in range(10)]
    after = [{"_t": now - 2400 + i * 120, "vmmem_mb": 500 + i * 10} for i in range(15)]
    slope, _, n = vmmem_trend(high + after, TREND)
    assert n == 15                      # the pre-collapse points were dropped
    # Only the gentle post-collapse ramp is measured (+10 MB per 2 min = 300 MB/h).
    # Including the collapse would have produced a large negative slope.
    assert slope == pytest.approx(300, rel=0.05)


def test_trend_survives_a_slower_configured_cadence():
    """The gap threshold must scale with the sample interval, not be fixed."""
    now = time.time()
    hist = [{"_t": now - 3600 + i * 600, "vmmem_mb": 1000 + i * 50} for i in range(6)]
    slope, span, n = vmmem_trend(hist, TREND)
    assert n == 6 and span >= 600 and slope > 0


def test_trend_empty_history():
    assert vmmem_trend([], TREND) == (0.0, 0.0, 0)


def test_scorecard_excludes_unscored_criteria_from_the_overall(sample, history, cfg):
    criteria = [
        ("committed_mb", "Commit", 20000.0, 12000.0, False, "MB"),
        ("vmmem_mb", "WSL", 4200.0, 10000.0, False, "MB"),   # ceiling, measuring
    ]
    short = history[:3]
    sc = build_scorecard(sample, short, criteria, cfg)
    measuring = [r for r in sc["rows"] if r["status"] == "measuring"]
    assert measuring, "the ceiling row should abstain on a short history"
    assert sc["scored"] == len(sc["rows"]) - len(measuring)


def test_scorecard_computes_hardware_faults(sample, history, cfg):
    criteria = [("hw_faults", "Faults", 3.0, 0.0, False, "")]
    sc = build_scorecard(sample, history, criteria, cfg)
    assert sc["rows"][0]["current"] == 1        # 1 problem device + 0 bad disks


def test_scorecard_reports_met_and_regress(sample, history, cfg):
    criteria = [
        ("c_free_gb", "Free", 40.0, 50.0, True, "GB"),      # 60 -> met
        ("handles", "Handles", 100000.0, 80000.0, False, ""),  # 120k -> regress
    ]
    sc = build_scorecard(sample, history, criteria, cfg)
    by_key = {r["key"]: r for r in sc["rows"]}
    assert by_key["c_free_gb"]["status"] == "met"
    assert by_key["handles"]["status"] == "regress"
    assert sc["counts"]["met"] == 1 and sc["counts"]["regress"] == 1


def test_scorecard_skips_metrics_the_machine_does_not_report(sample, history, cfg):
    sample.pop("vmmem_mb")
    criteria = [("vmmem_mb", "WSL", 1000.0, 2000.0, False, "MB")]
    assert build_scorecard(sample, history, criteria, cfg)["rows"] == []


def test_met_target_always_reads_as_complete(sample, history, cfg):
    """A metric already better than its achievable floor must not read 0%."""
    # net_remote is 55 in the fixture; a target *above* it (a floor that cannot
    # be beaten) is met on arrival and should score 100, not 0.
    criteria = [("net_remote", "Remote conns", 55.0, 60.0, False, "")]
    row = build_scorecard(sample, history, criteria, cfg)["rows"][0]
    assert row["status"] == "met"
    assert row["progress"] == 100.0


def test_noise_within_the_dead_band_is_not_a_regression(sample, history, cfg):
    """On day one the baseline *is* the current value; noise must not read red."""
    criteria = [("compression_mb", "Compression", 1390.0, 800.0, False, "MB")]
    # fixture value is 1400 — 0.7% above baseline, inside the 2% band
    row = build_scorecard(sample, history, criteria, cfg)["rows"][0]
    assert row["status"] == "partial"

    criteria = [("compression_mb", "Compression", 1200.0, 800.0, False, "MB")]
    row = build_scorecard(sample, history, criteria, cfg)["rows"][0]
    assert row["status"] == "regress"       # 16% above baseline is real
