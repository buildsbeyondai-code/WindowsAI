"""Baseline capture, target derivation and per-machine criteria assembly."""
from __future__ import annotations

import json

from sysperf.profile import (
    browser_floor,
    build_criteria,
    build_profile,
    capture_baseline,
    derive_targets,
    load_baseline,
    save_baseline,
)


def test_baseline_records_profile_and_metrics(sample):
    base = capture_baseline(sample)
    assert base["profile"]["hostname"] == "TESTBOX"
    assert base["profile"]["total_ram_mb"] == 16000
    assert base["metrics"]["committed_mb"] == 14000
    assert base["metrics"]["hw_faults"] == 1
    assert base["captured"]


def test_baseline_roundtrip(tmp_path, sample):
    path = tmp_path / "baseline.json"
    save_baseline(path, capture_baseline(sample))
    assert load_baseline(path)["metrics"]["handles"] == 120000


def test_load_baseline_tolerates_garbage(tmp_path):
    bad = tmp_path / "baseline.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_baseline(bad) == {}
    assert load_baseline(tmp_path / "missing.json") == {}


def test_targets_scale_with_ram_not_with_constants(sample, cfg):
    small = dict(sample, total_ram_mb=8000, committed_mb=7000)
    large = dict(sample, total_ram_mb=64000, committed_mb=40000)
    t_small = derive_targets(capture_baseline(small), small, cfg)
    t_large = derive_targets(capture_baseline(large), large, cfg)
    assert t_small["committed_mb"] < t_large["committed_mb"]
    assert t_small["available_mb"] < t_large["available_mb"]


def test_every_target_is_an_improvement_on_its_baseline(sample, cfg):
    base = capture_baseline(sample)
    targets = derive_targets(base, sample, cfg)
    lower_is_better = {"committed_mb", "compression_mb", "handles", "browser_procs",
                       "net_remote", "startup_apps",
                       # 2.1.0 responsiveness / thermal / startup-launch metrics
                       "cpu_pct", "cpu_queue", "disk_active_pct", "cpu_temp_c",
                       "boot_time_s", "wsl_start_s", "explorer_launch_s",
                       "outlook_launch_s", "word_launch_s"}
    for key, target in targets.items():
        start = base["metrics"].get(key)
        if start is None or key in ("vmmem_mb", "hw_faults"):
            continue
        if key in lower_is_better:
            assert target < start, f"{key}: target {target} not below baseline {start}"
        else:
            assert target > start, f"{key}: target {target} not above baseline {start}"


def test_targets_never_demand_the_impossible(sample, cfg):
    """A target below the measured floor would make the row permanently red."""
    base = capture_baseline(sample)
    targets = derive_targets(base, sample, cfg)
    _hard, realistic = browser_floor(sample, cfg["browser_hosts"])
    assert targets["browser_procs"] >= realistic
    assert targets["available_mb"] <= sample["total_ram_mb"] * 0.75
    assert targets["net_remote"] >= cfg["targets"]["net_remote_floor"]
    assert targets["startup_apps"] >= 2


def test_browser_floor_classifies_by_configured_ownership(sample, cfg):
    # OneDrive(6) + SearchHost(4) are mandatory, ms-teams(5) is in use,
    # SomeApp(3) is discretionary and must not raise the floor.
    hard, realistic = browser_floor(sample, cfg["browser_hosts"])
    assert hard == 15
    assert realistic == hard + sample["edge_main"] + sample["edge_utility"]


def test_browser_floor_is_case_insensitive(sample, cfg):
    sample["wv2_hosts"] = {"onedrive": 4}
    assert browser_floor(sample, cfg["browser_hosts"])[0] == 4


def test_wsl_criterion_absent_without_wsl(sample, cfg):
    sample["vmmem_mb"] = 0
    base = capture_baseline(sample)
    keys = [c[0] for c in build_criteria(base, sample, cfg)]
    assert "vmmem_mb" not in keys


def test_wsl_cap_comes_from_wslconfig_when_present(sample, cfg):
    sample["wsl_cap_mb"] = 6144
    targets = derive_targets(capture_baseline(sample), sample, cfg)
    assert targets["vmmem_mb"] == 6144


def test_wsl_cap_falls_back_to_a_ram_fraction(sample, cfg):
    sample.pop("wsl_cap_mb", None)
    targets = derive_targets(capture_baseline(sample), sample, cfg)
    expected = sample["total_ram_mb"] * cfg["targets"]["wsl_cap_of_ram"]
    assert abs(targets["vmmem_mb"] - expected) < 1


def test_criteria_skip_metrics_with_no_baseline(sample, cfg):
    base = capture_baseline(sample)
    base["metrics"].pop("startup_apps")
    keys = [c[0] for c in build_criteria(base, sample, cfg)]
    assert "startup_apps" not in keys
    assert "committed_mb" in keys


def test_criteria_are_well_formed(sample, cfg):
    for key, label, baseline, target, hib, unit in build_criteria(
            capture_baseline(sample), sample, cfg):
        assert isinstance(key, str) and key
        assert isinstance(label, str) and label
        assert isinstance(baseline, float) and isinstance(target, float)
        assert isinstance(hib, bool) and isinstance(unit, str)


def test_config_overrides_change_derived_targets(sample, tmp_path, monkeypatch):
    monkeypatch.setenv("SYSPERF_DATA_DIR", str(tmp_path))
    from sysperf.config import load_config
    path = tmp_path / "sysperf.config.json"
    path.write_text(json.dumps({"targets": {"commit_of_ram": 0.4}}), encoding="utf-8")
    cfg = load_config(path)
    targets = derive_targets(capture_baseline(sample), sample, cfg)
    assert targets["committed_mb"] <= sample["total_ram_mb"] * 0.4 + 1


def test_profile_prefers_live_values_over_baseline(sample):
    base = capture_baseline(sample)
    live = dict(sample, os_build="99999")
    assert build_profile(live, base)["os_build"] == "99999"


def test_profile_falls_back_to_baseline_on_a_fast_sample(sample):
    base = capture_baseline(sample)
    fast = {"committed_mb": 1}          # fast tier carries no machine facts
    assert build_profile(fast, base)["hostname"] == "TESTBOX"
