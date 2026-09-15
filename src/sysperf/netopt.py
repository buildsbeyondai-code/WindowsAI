"""
Wi-Fi band / channel overlap analysis and the network optimisation plan.

Consumes the neighbour scan (``wifi_scan``) and the associated-link info
(``wifi``) the collector reports, and produces:

* per-band congestion (2.4 / 5 / 6 GHz),
* 2.4 GHz overlap using the only non-overlapping set {1, 6, 11},
* the least-congested recommended channel per band,
* a prioritised, human-readable optimisation / fix plan.

All read-only: analysis only. The actual fixes are router-side (channel, width,
band steering) or elevated client-side settings, surfaced as recommendations —
never applied by SysPerf.
"""
from __future__ import annotations

NONOVERLAP_24 = (1, 6, 11)
COMMON_5 = (36, 40, 44, 48, 149, 153, 157, 161)


def _band_of(net: dict) -> str:
    b = str(net.get("band", "")).lower()
    if "6" in b and "ghz" in b:
        return "6"
    if "5" in b and "ghz" in b:
        return "5"
    if "2.4" in b or "2,4" in b:
        return "2.4"
    ch = net.get("channel", 0) or 0
    if 1 <= ch <= 14:
        return "2.4"
    if 32 <= ch <= 177:
        return "5"
    return "unknown"


def _congestion_24(nets, cand) -> float:
    """Signal-weighted overlap load on a candidate 2.4 GHz channel."""
    load = 0.0
    for n in nets:
        ch = n.get("channel", 0)
        if ch and abs(ch - cand) < 5:
            load += max(1, n.get("signal", 0)) / 100.0
    return round(load, 2)


def analyze_overlap(scan: dict | None, wifi: dict | None) -> dict:
    scan = scan or {}
    wifi = wifi or {}
    if not scan.get("available"):
        return {"available": False, "reason": scan.get("reason", "unavailable")}

    nets = [n for n in scan.get("nets", []) if n.get("channel")]
    own_bssid = str(wifi.get("bssid") or "").lower()
    peers = [n for n in nets if not own_bssid or str(n.get("bssid") or "").lower() != own_bssid]
    bands = {"2.4": [], "5": [], "6": [], "unknown": []}
    for n in nets:
        bands[_band_of(n)].append(n)

    cur_ch = wifi.get("channel")
    cur_band = None
    if wifi.get("band"):
        cur_band = _band_of({"band": wifi.get("band"), "channel": cur_ch or 0})
    elif cur_ch:
        cur_band = _band_of({"channel": cur_ch})

    g24 = bands["2.4"]
    ch_scores = [{"channel": c, "load": _congestion_24(g24, c)} for c in NONOVERLAP_24]
    ch_scores.sort(key=lambda x: x["load"])
    rec_24 = ch_scores[0]["channel"] if g24 else 1

    g5 = bands["5"]
    used5: dict[int, int] = {}
    for n in g5:
        used5[n["channel"]] = used5.get(n["channel"], 0) + 1
    rec_5 = min(COMMON_5, key=lambda c: used5.get(c, 0)) if g5 else None

    cur_overlap = None
    if cur_ch:
        if cur_band == "2.4":
            cur_overlap = sum(1 for n in peers if _band_of(n) == "2.4" and abs(n["channel"] - cur_ch) < 5)
        else:
            cur_overlap = sum(1 for n in peers if _band_of(n) == cur_band and n["channel"] == cur_ch)

    return {
        "available": True,
        "ap_count": len(nets),
        "counts": {"g24": len(g24), "g5": len(g5), "g6": len(bands["6"])},
        "current_channel": cur_ch,
        "current_band": cur_band,
        "current_overlap": cur_overlap,
        "ch_scores_24": ch_scores,
        "recommend_24": rec_24,
        "recommend_5": rec_5,
        "used_5": used5,
        "prefer_5ghz": cur_band == "2.4",
    }


def optimisation_plan(wifi: dict | None, overlap: dict | None, latest: dict | None) -> list[dict]:
    """Prioritised, reversible actions. sev: bad|warn|ok, side: client|router|os."""
    wifi = wifi or {}
    overlap = overlap or {}
    latest = latest or {}
    plan: list[dict] = []
    if not wifi.get("present"):
        return plan

    if wifi.get("present"):
        band = overlap.get("current_band")
        if band == "2.4" or (wifi.get("band") and "2.4" in str(wifi.get("band"))):
            plan.append({"sev": "warn", "side": "client",
                         "t": "Prefer 5 GHz / 6 GHz on the wireless adapter — 2.4 GHz is slower and far "
                              "more congested. Set the adapter's 'Preferred Band' to 5 GHz."})
        lm = wifi.get("link_mbps")
        if lm and lm < 300:
            plan.append({"sev": "warn", "side": "client",
                         "t": f"Negotiated link is only {lm} Mbps — improve signal (move closer / reposition "
                              "the AP), prefer 5/6 GHz, and disable adapter power-saving."})

    if overlap.get("available"):
        co = overlap.get("current_overlap")
        rec24 = overlap.get("recommend_24")
        if co and co > 0 and overlap.get("current_band") == "2.4":
            plan.append({"sev": "bad", "side": "router",
                         "t": f"{co} neighbouring AP(s) overlap your 2.4 GHz channel "
                              f"{overlap.get('current_channel')}. On the router, move 2.4 GHz to the clearest "
                              f"non-overlapping channel {rec24} (only 1/6/11 don't overlap) at 20 MHz width."})
        elif overlap.get("counts", {}).get("g24"):
            plan.append({"sev": "warn", "side": "router",
                         "t": f"On the router, pin 2.4 GHz to channel {rec24} (least congested of 1/6/11) "
                              "at 20 MHz width to avoid overlapping neighbours."})
        rec5 = overlap.get("recommend_5")
        if rec5:
            plan.append({"sev": "ok", "side": "router",
                         "t": f"On 5 GHz, channel {rec5} is currently least used nearby — use it (80 MHz where "
                              "clear) and enable band steering to push clients to 5 GHz."})
    else:
        if overlap.get("reason") == "location":
            plan.append({"sev": "warn", "side": "os",
                         "t": "Enable Windows Location (Settings -> Privacy & security -> Location) so the WLAN "
                              "service can scan neighbours — required for live band/channel overlap analysis."})
        plan.append({"sev": "warn", "side": "router",
                     "t": "On the router: set 2.4 GHz to a fixed non-overlapping channel (1, 6 or 11) at 20 MHz, "
                          "and put 5 GHz on an 80 MHz clear channel to remove 2.4 GHz overlap."})
        plan.append({"sev": "warn", "side": "router",
                     "t": "Enable band steering / a separate 5 GHz SSID so devices prefer 5/6 GHz over 2.4 GHz."})

    plan.append({"sev": "ok", "side": "client",
                 "t": "Flush DNS only for suspected stale name resolution (ipconfig /flushdns). "
                      "It does not close TCP connections or improve link speed. A Winsock reset "
                      "requires administrator review and may require a restart."})
    nd_date = latest.get("net_driver_date")
    if nd_date:
        plan.append({"sev": "ok", "side": "client",
                     "t": f"Keep the wireless driver current (installed {nd_date}) — newer drivers improve "
                          "throughput, roaming and power."})
    return plan
