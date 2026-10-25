"""Per-machine baseline: capture, apply, and foreign-data detection."""
import json

import pytest

from sysperf import config
from sysperf import machine_profile as mp
from sysperf.scoring import build_scorecard


class _Scorer:
    def enrich(self, *a):
        return None

    def summary(self, rows):
        return {}


def _hist(n=8, **over):
    base = {"total_ram_mb": 8000, "c_size_gb": 240.0, "c_free_gb": 30.0,
            "committed_app_mb": 7000, "compression_mb": 200, "available_mb": 600,
            "vmmem_mb": 400, "browser_procs": 30, "handles": 80000,
            "startup_apps": 4, "hw_problem_devices": 0, "hw_disks_unhealthy": 0,
            "cpu_pct": 60.0, "pool_paged_mb": 400, "pool_nonpaged_mb": 500}
    base.update(over)
    return [dict(base, _t=float(i)) for i in range(n)]


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(mp, "PROFILE_FILE", tmp_path / "baseline.json")
    monkeypatch.setenv("COMPUTERNAME", "THIS-PC")
    monkeypatch.setattr(mp, "_current", {})
    # apply() rewrites these module globals; restore them for the other tests.
    monkeypatch.setattr(config, "VMMEM_CAP_MB", config.VMMEM_CAP_MB)
    monkeypatch.setattr(config, "BASELINE_DATE", config.BASELINE_DATE)
    yield tmp_path
    mp.apply(None)


def test_needs_enough_samples():
    assert mp.derive(_hist(2)) is None


def test_targets_are_sized_from_this_machines_capacity():
    p = mp.derive(_hist())
    t, b = p["targets"], p["baselines"]
    # 8 GB machine: the old fixed 12,000 MB "available" goal was impossible here.
    assert b["available_mb"] == 600 and t["available_mb"] == round(8000 * 0.3)
    assert t["vmmem_mb"] == round(8000 * 0.35)            # not a fixed 10 GB cap
    assert t["committed_app_mb"] == round(8000 * 0.75)
    assert t["c_free_gb"] == 36.0                          # 15% of a 240 GB drive
    assert t["hw_faults"] == 0 and b["hw_faults"] == 0
    # Generic row: progress counts from where this machine started.
    assert b["cpu_pct"] == 60.0
    assert p["reference"] == {"pool_paged_mb": 400, "pool_nonpaged_mb": 500}


def test_an_already_healthy_metric_is_asked_to_stay_healthy_not_improve():
    # 200 MB of compression on an 8 GB machine is fine; the goal is the healthy
    # ceiling (5% of RAM), not 15% less than an already-good number.
    p = mp.derive(_hist())
    assert p["targets"]["compression_mb"] == 400


def test_met_rows_score_full_progress(isolated):
    mp.apply(None)
    mp.maybe_capture(_hist())
    out = build_scorecard({"compression_mb": 220}, [], _Scorer())
    row = next(r for r in out["rows"] if r["key"] == "compression_mb")
    assert row["status"] == "met" and row["progress"] == 100.0


def test_browser_target_never_goes_below_the_realistic_floor():
    hosts = {"SearchHost": 7, "WhatsApp.Root": 6}
    p = mp.derive(_hist(browser_procs=26, wv2_hosts=hosts, edge_main=1, edge_utility=8))
    # floor = 7 + 6 + (1 + 8 + 5) = 27 > 26 * 0.75
    assert p["capacity"]["browser_floor"] == 27
    assert p["targets"]["browser_procs"] == 27


def test_run_queue_target_scales_with_logical_processors():
    t = mp.targets_for({"cpu_queue": 3}, {"logical_cpus": 8})
    assert t["cpu_queue"] == 8


def test_an_older_profile_is_retargeted_without_moving_its_baselines(isolated):
    old = mp.derive(_hist())
    old.update(version=1, machine="THIS-PC")
    old["baselines"]["compression_mb"] = 150
    old["targets"]["compression_mb"] = 127               # old "15% better" rule
    mp.apply(old)
    assert mp.maybe_capture(_hist(compression_mb=900)) is True
    cur = mp.current()
    assert cur["version"] == mp.PROFILE_VERSION
    assert cur["baselines"]["compression_mb"] == 150      # baseline kept
    assert cur["targets"]["compression_mb"] == 400        # new rule


def test_rows_wait_for_a_baseline_instead_of_using_another_machines(isolated):
    mp.apply(None)
    assert "available_mb" in config.PROFILE_PENDING
    out = build_scorecard({"available_mb": 600, "cpu_pct": 50}, [], _Scorer())
    rows = {r["key"]: r for r in out["rows"]}
    assert rows["available_mb"]["status"] == "measuring"
    assert rows["available_mb"]["target"] is None
    assert rows["cpu_pct"]["status"] != "measuring"       # generic rows still graded


def test_capture_applies_and_persists(isolated):
    mp.apply(None)
    assert mp.maybe_capture(_hist()) is True
    crit = {c[0]: c for c in config.CRITERIA}
    assert crit["available_mb"][2:4] == (600, 2400)
    assert config.VMMEM_CAP_MB == 2800
    assert "available_mb" not in config.PROFILE_PENDING
    saved = json.loads((isolated / "baseline.json").read_text())
    assert saved["machine"] == "THIS-PC"
    assert mp.maybe_capture(_hist()) is False             # captured once


def test_slow_tier_rows_are_filled_in_later_without_moving_the_rest(isolated):
    mp.apply(None)
    mp.maybe_capture(_hist())
    assert "net_remote" in config.PROFILE_PENDING
    first = dict(mp.current()["baselines"])
    assert mp.maybe_capture(_hist(available_mb=3000, net_remote=40)) is True
    now = mp.current()["baselines"]
    assert now["net_remote"] == 40
    assert now["available_mb"] == first["available_mb"]   # never re-baselined


def test_samples_from_another_machine_are_not_used(isolated):
    mp.apply(None)
    assert mp.maybe_capture(_hist(_host="OTHER-PC")) is False


def test_foreign_data_directory_is_archived_not_deleted(isolated, tmp_path):
    (tmp_path / "baseline.json").write_text(json.dumps({"machine": "OLD-LAPTOP"}))
    (tmp_path / "history.jsonl").write_text('{"available_mb": 7013}\n')
    (tmp_path / "sysperf.config.json").write_text("{}")
    mp.startup(tmp_path)
    assert not (tmp_path / "history.jsonl").exists()
    assert (tmp_path / "sysperf.config.json").exists()    # settings are kept
    archived = list((tmp_path / "archive").glob("OLD-LAPTOP-*/history.jsonl"))
    assert len(archived) == 1
    assert "available_mb" in config.PROFILE_PENDING


def test_stamped_history_identifies_its_machine(isolated, tmp_path):
    (tmp_path / "history.jsonl").write_text(json.dumps({"_host": "old-laptop"}) + "\n")
    mp.startup(tmp_path)
    assert list((tmp_path / "archive").glob("OLD-LAPTOP-*/history.jsonl"))


def test_own_data_directory_is_left_alone(isolated, tmp_path):
    prof = mp.derive(_hist())
    prof["machine"] = "THIS-PC"
    (tmp_path / "baseline.json").write_text(json.dumps(prof))
    (tmp_path / "history.jsonl").write_text(json.dumps({"_host": "THIS-PC"}) + "\n")
    mp.startup(tmp_path)
    assert (tmp_path / "history.jsonl").exists()
    assert mp.current()["machine"] == "THIS-PC"


def test_net_remote_under_the_healthy_ceiling_targets_the_ceiling(isolated):
    mp.apply(None)
    mp.maybe_capture(_hist(net_remote=21))
    out = build_scorecard({"net_remote": 24}, [], _Scorer())
    row = next(r for r in out["rows"] if r["key"] == "net_remote")
    assert row["target"] == 25 and row["status"] == "met"     # under the ceiling
    out = build_scorecard({"net_remote": 30}, [], _Scorer())
    row = next(r for r in out["rows"] if r["key"] == "net_remote")
    assert row["status"] == "regress"
    assert row["progress"] < 100


def test_equal_baseline_and_target_scores_zero_when_missed(isolated):
    mp.apply(None)
    mp.maybe_capture(_hist(hw_problem_devices=0))
    out = build_scorecard({"hw_problem_devices": 2, "hw_faults": 2}, [], _Scorer())
    row = next(r for r in out["rows"] if r["key"] == "hw_faults")
    assert row["baseline"] == row["target"] == 0 and row["progress"] == 0.0


def _browser_latest(edge_renderers, whatsapp=0):
    hosts = {"SearchHost": 6}
    if whatsapp:
        hosts["WhatsApp.Root"] = whatsapp
    edge = 1 + 8 + 1 + edge_renderers
    return {"wv2_hosts": hosts, "edge_procs": edge, "edge_main": 1, "edge_utility": 8,
            "edge_renderer": edge_renderers, "edge_extension": 1,
            "browser_procs": sum(hosts.values()) + edge}


def test_browser_target_follows_what_is_open_now():
    # floor = SearchHost 6 + minimal Edge (1 + 8 + 5) = 20, + 4 headroom
    assert mp.live_targets(_browser_latest(4)) == {"browser_procs": 24}
    # WhatsApp opened: its WebView2 processes raise the floor with it
    assert mp.live_targets(_browser_latest(4, whatsapp=6)) == {"browser_procs": 30}
    assert mp.live_targets({}) == {}


def test_a_few_extra_tabs_lose_points_gradually_not_all_at_once(isolated):
    mp.apply(None)
    mp.maybe_capture(_hist(browser_procs=26))
    latest = _browser_latest(12)                       # 28 processes, target 24
    out = build_scorecard(latest, [], _Scorer(), live_targets=mp.live_targets(latest))
    row = next(r for r in out["rows"] if r["key"] == "browser_procs")
    assert row["target"] == 24 and row["target_source"] == "live"
    assert 0 < row["progress"] < 100 and row["status"] == "partial"
    latest = _browser_latest(4)                        # 20 processes: within target
    out = build_scorecard(latest, [], _Scorer(), live_targets=mp.live_targets(latest))
    row = next(r for r in out["rows"] if r["key"] == "browser_procs")
    assert row["status"] == "met" and row["progress"] == 100.0


def test_slipping_far_past_a_target_still_reaches_zero():
    from sysperf.scoring import slipped_progress
    assert slipped_progress(25, 25, False) == 100.0
    assert slipped_progress(30, 25, False) == 60.0      # 20% over -> 60%
    assert slipped_progress(40, 25, False) == 0.0       # 60% over -> 0%
    assert slipped_progress(1, 0, False) == 0.0         # zero target: any miss is 0


# ----------------------------------------------------- 4.1 + remaining paths --
def _db(path, rows):
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE samples (t REAL PRIMARY KEY, data TEXT NOT NULL)")
    for i, r in enumerate(rows):
        con.execute("INSERT INTO samples VALUES (?, ?)", (float(i), r))
    con.commit()
    con.close()


def test_sqlite_history_identifies_and_archives_a_foreign_machine(isolated, tmp_path):
    _db(tmp_path / "history.db", [json.dumps({"_host": "x"}),
                                  json.dumps({"_host": "old-desktop"})])
    (tmp_path / "notifications.json").write_text("{}")
    mp.startup(tmp_path)
    dest = list((tmp_path / "archive").glob("OLD-DESKTOP-*"))
    assert dest and (dest[0] / "history.db").exists() and (dest[0] / "notifications.json").exists()


def test_db_owner_edge_cases(tmp_path):
    assert mp._db_owner(tmp_path / "none.db") is None
    _db(tmp_path / "empty.db", [])
    assert mp._db_owner(tmp_path / "empty.db") is None
    _db(tmp_path / "nohost.db", [json.dumps({"a": 1})])
    assert mp._db_owner(tmp_path / "nohost.db") is None
    _db(tmp_path / "junk.db", ["not json"])
    assert mp._db_owner(tmp_path / "junk.db") is None
    (tmp_path / "notadb.db").write_text("garbage")
    assert mp._db_owner(tmp_path / "notadb.db") is None


def test_history_tail_without_a_host_and_bad_lines(isolated, tmp_path):
    (tmp_path / "history.jsonl").write_text('not json\n[1]\n{"a": 1}\n')
    assert mp._history_owner(tmp_path) is None


def test_user_target_fractions_override_defaults(monkeypatch):
    monkeypatch.setitem(config.SETTINGS, "targets", {"commit_of_ram": 0.5, "junk": 9,
                                                     "available_of_ram": "x"})
    f = mp._fractions()
    assert f["commit_of_ram"] == 0.5 and "junk" not in f
    assert f["available_of_ram"] == mp.TARGET_DEFAULTS["available_of_ram"]


def test_save_and_archive_failures_are_logged(isolated, tmp_path, caplog, monkeypatch):
    mp.save({"a": 1}, tmp_path / "missing" / "baseline.json")
    assert "could not save" in caplog.text
    (tmp_path / "history.jsonl").write_text("{}")

    def boom(src, dst):
        raise OSError("locked")
    monkeypatch.setattr(mp.os, "replace", boom)
    assert mp.archive_foreign_state(tmp_path, "OTHER") is None
    assert "could not archive" in caplog.text


def test_live_targets_without_a_floor(monkeypatch):
    import sysperf.browser as browser
    monkeypatch.setattr(browser, "browser_breakdown", lambda latest: {"realistic_floor": 0})
    assert mp.live_targets({"wv2_hosts": 3}) == {}


def test_apply_refreshes_trend_lookup_tables(isolated, monkeypatch):
    import sys
    import types
    calls = []
    fake = types.SimpleNamespace(refresh_criteria=lambda: calls.append(True))
    monkeypatch.setitem(sys.modules, "sysperf.trends", fake)
    mp.apply(None)
    assert calls == [True]


def test_capture_is_skipped_once_this_machine_is_complete(isolated, monkeypatch):
    prof = mp.derive(_hist())
    prof["machine"] = "THIS-PC"
    mp.apply(prof)
    monkeypatch.setattr(config, "PROFILE_PENDING", frozenset())
    assert mp.maybe_capture(_hist()) is False
