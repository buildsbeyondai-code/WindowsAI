"""Full-coverage tests for hardware.py."""
import sysperf.hardware as hardware


def test_load_watchlist_ok(tmp_path, monkeypatch):
    wl = tmp_path / "wl.json"
    wl.write_text('[{"status":"open"},{"status":"resolved"}]')
    monkeypatch.setattr(hardware, "WATCHLIST_FILE", wl)
    assert len(hardware.load_watchlist()) == 2


def test_load_watchlist_not_a_list(tmp_path, monkeypatch):
    wl = tmp_path / "wl.json"
    wl.write_text('{"not":"a list"}')
    monkeypatch.setattr(hardware, "WATCHLIST_FILE", wl)
    assert hardware.load_watchlist() == []


def test_load_watchlist_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(hardware, "WATCHLIST_FILE", tmp_path / "nope.json")
    assert hardware.load_watchlist() == []


def test_build_hardware_with_battery(tmp_path, monkeypatch):
    wl = tmp_path / "wl.json"
    wl.write_text('[{"status":"open"},{"status":"Resolved"},{"status":"open"}]')
    monkeypatch.setattr(hardware, "WATCHLIST_FILE", wl)
    latest = {
        "hw_problem_devices": 1, "hw_problem_list": [{"name": "x"}],
        "hw_disks": [{"name": "d"}], "hw_disks_unhealthy": 1,
        "hw_battery": {"charge": 90, "status": 2},
        "drv_total": 100, "drv_stale": 3, "drv_problem": 1,
    }
    h = hardware.build_hardware(latest)
    assert h["faults"] == 2                      # 1 device + 1 disk
    assert h["open_issues"] == 2                 # two non-resolved
    assert h["battery"]["charge"] == 90
    assert h["drivers"]["total"] == 100


def test_build_hardware_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(hardware, "WATCHLIST_FILE", tmp_path / "none.json")
    h = hardware.build_hardware({})
    assert h["faults"] == 0 and h["battery"] is None and h["disks"] == []
    assert h["drivers"]["stale"] == 0


def test_verified_latest_drivers_are_listed_but_not_counted(monkeypatch, tmp_path):
    monkeypatch.setattr(hardware, "WATCHLIST_FILE", tmp_path / "none.json")
    monkeypatch.setattr(hardware, "SETTINGS",
                        {"drivers": {"verified_latest": {"GSCHECI.inf": "2026-10-24"}}})
    latest = {"drv_stale": 3, "drv_stale_list": [
        {"name": "gscheci.inf", "date": "2022-07-14"},
        {"name": "rtux64w10.inf", "date": "2015-10-19"}]}
    dr = hardware.build_hardware(latest)["drivers"]
    assert dr["stale"] == 2                         # 3 found, 1 verified newest
    assert [d["name"] for d in dr["stale_list"]] == ["rtux64w10.inf"]
    assert dr["verified_latest"][0]["verified"] == "2026-10-24"
