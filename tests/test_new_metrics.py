"""Tests for the 2.1.0 metrics: responsiveness, thermal, startup/launch, wear."""
from __future__ import annotations

import copy

import pytest

from sysperf import profile as P
from sysperf.collector import Collector

NEW_LATENCY = {
    "disk_active_pct": 30.0, "cpu_temp_c": 85.0, "boot_time_s": 120.0,
    "wsl_start_s": 25.0, "explorer_launch_s": 8.0, "outlook_launch_s": 20.0,
    "word_launch_s": 15.0,
}


def test_derive_targets_for_new_metrics(sample, cfg):
    s = copy.deepcopy(sample)
    s.update(NEW_LATENCY)                       # cpu_pct/cpu_queue already present
    baseline = P.capture_baseline(s)
    t = P.derive_targets(baseline, s, cfg)
    for k in ("cpu_pct", "cpu_queue", "disk_active_pct", "cpu_temp_c",
              "boot_time_s", "wsl_start_s", "explorer_launch_s",
              "outlook_launch_s", "word_launch_s"):
        assert k in t, f"missing target for {k}"
        assert t[k] <= baseline["metrics"][k]   # lower-is-better -> not worse than baseline


def test_new_metrics_become_criteria(sample, cfg):
    s = copy.deepcopy(sample)
    s.update(NEW_LATENCY)
    baseline = P.capture_baseline(s)
    keys = {c[0] for c in P.build_criteria(baseline, s, cfg)}
    assert {"cpu_pct", "cpu_queue", "disk_active_pct", "cpu_temp_c",
            "boot_time_s", "wsl_start_s", "explorer_launch_s",
            "outlook_launch_s", "word_launch_s"} <= keys


def test_launch_metric_absent_when_not_reported(sample, cfg):
    # A machine that never reports a launch time must not get a permanent row.
    baseline = P.capture_baseline(sample)       # sample has no boot_time_s etc.
    keys = {c[0] for c in P.build_criteria(baseline, sample, cfg)}
    assert "boot_time_s" not in keys and "word_launch_s" not in keys


class _Stub(Collector):
    def __init__(self, *a, sample_data, **k):
        self._s = sample_data
        super().__init__(*a, **k)

    def read_windows(self, full):
        return copy.deepcopy(self._s)


def test_battery_wear_is_derived_from_capacities(cfg, sample, tmp_path):
    # FULL_SAMPLE battery: design 53000, full 31000 -> wear ~ 41.5%
    c = _Stub(cfg, None, history_file=tmp_path / "h.jsonl",
              baseline_file=tmp_path / "b.json", sample_data=sample)
    assert c.sample() is True
    assert c.latest["battery_wear_pct"] == pytest.approx(41.5, abs=0.2)


def test_battery_wear_skipped_without_capacities(cfg, sample, tmp_path):
    s = copy.deepcopy(sample)
    s["hw_battery"] = {"charge": 90, "status": 2}   # no design/full capacity
    c = _Stub(cfg, None, history_file=tmp_path / "h.jsonl",
              baseline_file=tmp_path / "b.json", sample_data=s)
    c.sample()
    assert c.latest.get("battery_wear_pct") is None
