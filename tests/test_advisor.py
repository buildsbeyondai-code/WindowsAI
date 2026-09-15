"""Advisor rules: they must fire on the right evidence and stay read-only."""
from __future__ import annotations

from sysperf.advisor import SEVERITY_ORDER, build_advice
from sysperf.profile import build_criteria, capture_baseline
from sysperf.scoring import build_scorecard


def _advise(sample, cfg, history=None):
    base = capture_baseline(sample)
    criteria = build_criteria(base, sample, cfg)
    sc = build_scorecard(sample, history or [sample], criteria, cfg)
    return build_advice(sample, sc, cfg, base.get("profile"))


def _ids(advice):
    return {item["id"] for item in advice["items"]}


def test_power_saver_is_flagged(sample, cfg):
    assert "power.saver-active" in _ids(_advise(sample, cfg))


def test_power_plan_not_flagged_when_balanced(sample, cfg):
    sample["power_plan"] = "Balanced"
    sample["power_plan_guid"] = "381b4222-f694-41f0-9685-ff5bb260df2e"
    assert "power.saver-active" not in _ids(_advise(sample, cfg))


def test_commit_over_ram_is_flagged(sample, cfg):
    sample["committed_mb"] = 20000       # RAM is 16000
    ids = _ids(_advise(sample, cfg))
    assert "mem.commit-exceeds-ram" in ids


def test_commit_under_ram_is_not_flagged(sample, cfg):
    sample["committed_mb"] = 8000
    assert "mem.commit-exceeds-ram" not in _ids(_advise(sample, cfg))


def test_low_available_memory_is_critical(sample, cfg):
    sample["available_mb"] = 900          # 5.6% of 16 GB
    item = next(i for i in _advise(sample, cfg)["items"]
                if i["id"] == "mem.available-low")
    assert item["severity"] == "critical"


def test_low_disk_space_is_flagged_with_a_cleanup_command(sample, cfg):
    sample["c_free_gb"] = 20.0            # 4.2% of 476 GB
    item = next(i for i in _advise(sample, cfg)["items"]
                if i["id"] == "storage.low-free-space")
    assert item["severity"] == "critical"
    assert "cleanmgr" in item["command"]


def test_healthy_disk_space_is_not_flagged(sample, cfg):
    sample["c_free_gb"] = 300.0
    assert "storage.low-free-space" not in _ids(_advise(sample, cfg))


def test_pagefile_pressure_is_flagged(sample, cfg):
    # peak 1900 of 2048 allocated
    assert "storage.pagefile-pressure" in _ids(_advise(sample, cfg))


def test_startup_bloat_lists_the_offenders(sample, cfg):
    item = next(i for i in _advise(sample, cfg)["items"] if i["id"] == "startup.too-many")
    assert "VendorUpdater" in item["finding"]


def test_worn_battery_is_flagged(sample, cfg):
    # 31000 of 53000 design capacity = 58%
    assert "hardware.battery-worn" in _ids(_advise(sample, cfg))


def test_healthy_battery_is_not_flagged(sample, cfg):
    sample["hw_battery"]["full_capacity"] = 50000
    assert "hardware.battery-worn" not in _ids(_advise(sample, cfg))


def test_missing_battery_data_does_not_crash(sample, cfg):
    sample["hw_battery"] = {"charge": 50, "status": 1}
    _advise(sample, cfg)        # no exception


def test_driver_and_hardware_faults_are_flagged(sample, cfg):
    ids = _ids(_advise(sample, cfg))
    assert "drivers.faults" in ids
    assert "drivers.stale" in ids
    assert "hardware.faults" in ids


def test_clean_machine_produces_no_hardware_advice(sample, cfg):
    sample.update(hw_problem_devices=0, hw_problem_list=[], drv_problem=0,
                  drv_problem_list=[], drv_stale=0, drv_stale_list=[])
    ids = _ids(_advise(sample, cfg))
    assert not {"drivers.faults", "drivers.stale", "hardware.faults"} & ids


def test_wsl_without_a_config_is_flagged_with_a_derived_cap(sample, cfg):
    item = next(i for i in _advise(sample, cfg)["items"] if i["id"] == "wsl.no-memory-cap")
    assert "[wsl2]" in "\n".join(item["action"])
    assert "5GB" in "\n".join(item["action"])   # 35% of 16 GB, rounded to GB


def test_wsl_advice_absent_without_wsl(sample, cfg):
    sample["vmmem_mb"] = 0
    assert not {"wsl.no-memory-cap", "wsl.no-auto-reclaim"} & _ids(_advise(sample, cfg))


def test_auto_reclaim_advice_when_config_exists_without_it(sample, cfg):
    sample.update(wsl_config_present=True, wsl_autoreclaim=False, wsl_cap_mb=8192)
    ids = _ids(_advise(sample, cfg))
    assert "wsl.no-auto-reclaim" in ids
    assert "wsl.no-memory-cap" not in ids


def test_long_uptime_and_pending_restart(sample, cfg):
    sample["uptime_h"] = 400
    sample["restart_pending"] = True
    ids = _ids(_advise(sample, cfg))
    assert {"maint.uptime-long", "maint.restart-pending"} <= ids


def test_search_stopped_is_flagged(sample, cfg):
    sample["search_procs"] = 0
    assert "platform.search-stopped" in _ids(_advise(sample, cfg))


def test_recommendations_are_sorted_by_severity(sample, cfg):
    order = [SEVERITY_ORDER[i["severity"]] for i in _advise(sample, cfg)["items"]]
    assert order == sorted(order)


def test_every_recommendation_is_actionable_and_declares_its_risk(sample, cfg):
    advice = _advise(sample, cfg)
    assert advice["read_only"] is True
    for item in advice["items"]:
        assert item["action"], f"{item['id']} has no action"
        assert item["finding"] and item["why"]
        assert isinstance(item["needs_admin"], bool)
        assert item["severity"] in SEVERITY_ORDER


def test_advisor_can_be_disabled(sample, cfg):
    cfg["advisor"]["enabled"] = False
    assert _advise(sample, cfg) == {"enabled": False, "items": [], "counts": {}}


def test_recommendation_cap_is_honoured(sample, cfg):
    cfg["advisor"]["max_recommendations"] = 2
    assert len(_advise(sample, cfg)["items"]) == 2


def test_a_failing_rule_does_not_break_the_payload(sample, cfg, monkeypatch):
    import sysperf.advisor as adv

    def exploding(*_args):
        raise ValueError("boom")

    monkeypatch.setattr(adv, "_RULES", [exploding, *adv._RULES])
    assert _advise(sample, cfg)["items"]        # still produced


def test_empty_sample_produces_no_recommendations(cfg):
    assert build_advice({}, {"rows": []}, cfg, {})["items"] == []
