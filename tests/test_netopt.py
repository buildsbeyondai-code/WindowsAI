"""Full-coverage tests for netopt.py."""
from sysperf.netopt import _band_of, _congestion_24, analyze_overlap, optimisation_plan


# --------------------------------------------------------------- _band_of ----
def test_band_of_all_paths():
    assert _band_of({"band": "6 GHz"}) == "6"
    assert _band_of({"band": "5 GHz"}) == "5"
    assert _band_of({"band": "2.4 GHz"}) == "2.4"
    assert _band_of({"band": "2,4 GHz"}) == "2.4"
    assert _band_of({"channel": 6}) == "2.4"       # by channel
    assert _band_of({"channel": 40}) == "5"
    assert _band_of({"channel": 200}) == "6"
    assert _band_of({}) == "6"                      # ch 0 -> falls through


def test_congestion_24():
    nets = [{"channel": 1, "signal": 80}, {"channel": 3, "signal": 50},
            {"channel": 11, "signal": 90}]
    assert _congestion_24(nets, 1) > 0             # ch1 & ch3 within 5
    assert _congestion_24([{"channel": 0}], 1) == 0.0


# ----------------------------------------------------------- analyze_overlap --
def test_analyze_unavailable():
    assert analyze_overlap(None, None)["available"] is False
    assert analyze_overlap({"available": False, "reason": "location"}, {})["reason"] == "location"


def test_analyze_full_24_current():
    scan = {"available": True, "nets": [
        {"channel": 1, "signal": 80, "band": "2.4 GHz"},
        {"channel": 3, "signal": 70, "band": "2.4 GHz"},
        {"channel": 6, "signal": 50, "band": "2.4 GHz"},
        {"channel": 36, "signal": 60, "band": "5 GHz"},
        {"channel": 40, "signal": 40, "band": "5 GHz"},
        {"channel": 5, "band": "6 GHz"}]}
    wifi = {"present": True, "band": "2.4 GHz", "channel": 1, "link_mbps": 100}
    o = analyze_overlap(scan, wifi)
    assert o["available"] and o["current_band"] == "2.4"
    assert o["current_overlap"] >= 1               # ch3 overlaps ch1
    assert o["recommend_5"] in (36, 40, 44, 48, 149, 153, 157, 161)
    assert o["counts"]["g6"] == 1


def test_analyze_5ghz_current_channel_only():
    scan = {"available": True, "nets": [
        {"channel": 36, "signal": 60, "band": "5 GHz"},
        {"channel": 36, "signal": 40, "band": "5 GHz"}]}
    wifi = {"present": True, "channel": 36}         # no band -> derived from channel
    o = analyze_overlap(scan, wifi)
    assert o["current_band"] == "5"
    assert o["current_overlap"] == 1                # 2 on ch36 minus self
    assert o["recommend_24"] == 1                   # no 2.4 nets -> default 1


def test_analyze_no_current_channel():
    scan = {"available": True, "nets": [{"channel": 6, "band": "2.4 GHz"}]}
    o = analyze_overlap(scan, {"present": True})
    assert o["current_overlap"] is None and o["current_band"] is None


# --------------------------------------------------------- optimisation_plan --
def test_plan_24_congested_with_slow_link():
    scan = {"available": True, "nets": [
        {"channel": 1, "signal": 80, "band": "2.4 GHz"},
        {"channel": 3, "signal": 70, "band": "2.4 GHz"},
        {"channel": 36, "signal": 60, "band": "5 GHz"}]}
    wifi = {"present": True, "band": "2.4 GHz", "channel": 1, "link_mbps": 100}
    overlap = analyze_overlap(scan, wifi)
    plan = optimisation_plan(wifi, overlap, {"net_driver_date": "2025-01-01"})
    joined = " ".join(p["t"] for p in plan)
    assert "Prefer 5 GHz" in joined
    assert "only 100 Mbps" in joined
    assert "overlap your 2.4 GHz" in joined
    assert "driver current" in joined


def test_plan_24_present_but_no_current_overlap():
    scan = {"available": True, "nets": [
        {"channel": 6, "signal": 40, "band": "2.4 GHz"},
        {"channel": 36, "signal": 40, "band": "5 GHz"}]}
    wifi = {"present": True, "channel": 36, "band": "5 GHz", "link_mbps": 800}
    overlap = analyze_overlap(scan, wifi)
    plan = optimisation_plan(wifi, overlap, {})
    joined = " ".join(p["t"] for p in plan)
    assert "pin 2.4 GHz to channel" in joined       # elif branch


def test_plan_scan_unavailable_location():
    overlap = analyze_overlap({"available": False, "reason": "location"}, {})
    plan = optimisation_plan({}, overlap, {})
    joined = " ".join(p["t"] for p in plan)
    assert "Enable Windows Location" in joined
    assert "band steering" in joined                # general router fix
    assert "Flush DNS" in joined


def test_a_routers_own_hidden_network_is_not_overlap():
    # Real scan from this laptop: the router's 5 GHz SSID plus a hidden virtual
    # AP of the same radio (locally-administered BSSID, same low bytes).
    scan = {"available": True, "nets": [
        {"ssid": "Home", "channel": 149, "band": "5 GHz", "signal": 87, "bssid": "78:bb:c1:17:fe:42"},
        {"ssid": "Home", "channel": 11, "band": "2.4 GHz", "signal": 89, "bssid": "78:bb:c1:17:fe:41"},
        {"ssid": "", "channel": 149, "band": "5 GHz", "signal": 87, "bssid": "7a:bb:c1:37:fe:42"}]}
    out = analyze_overlap(scan, {"ssid": "Home", "channel": 149, "band": "5 GHz"})
    assert out["current_overlap"] == 0


def test_a_real_neighbour_on_the_same_channel_still_counts():
    scan = {"available": True, "nets": [
        {"ssid": "Home", "channel": 149, "band": "5 GHz", "signal": 87, "bssid": "78:bb:c1:17:fe:42"},
        {"ssid": "Next door", "channel": 149, "band": "5 GHz", "signal": 40, "bssid": "10:20:30:40:50:60"}]}
    out = analyze_overlap(scan, {"ssid": "Home", "channel": 149, "band": "5 GHz"})
    assert out["current_overlap"] == 1
