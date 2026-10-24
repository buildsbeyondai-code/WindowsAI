"""
Wi-Fi band / channel overlap analysis + optimisation plan.

Consumes the neighbour scan (wifi_scan) and the associated-link info (wifi) from
the collector sample and produces:

  * per-band congestion (2.4 / 5 / 6 GHz),
  * 2.4 GHz overlap using the only non-overlapping set {1, 6, 11},
  * the least-congested recommended channel per band,
  * a prioritised, human-readable optimisation / fix plan.

All read-only: analysis only. The actual fixes are router-side (channel/width/
band-steering) or elevated client-side settings, surfaced as recommendations and
in the generated optimize-wifi.ps1 helper — never applied from here.

2.4 GHz model: 20 MHz channels are 5 MHz apart, so two APs overlap when their
channel numbers are < 5 apart; 1/6/11 are the only mutually non-overlapping set.
5/6 GHz model: 20 MHz channels do not overlap, so congestion is co-channel count.
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
    return "6"


def _congestion_24(nets, cand):
    """Weighted overlap load on a candidate 2.4 GHz channel (signal-weighted AP count)."""
    load = 0.0
    for n in nets:
        ch = n.get("channel", 0)
        if ch and abs(ch - cand) < 5:
            load += max(1, n.get("signal", 0)) / 100.0
    return round(load, 2)


def _mac_tail(bssid) -> str | None:
    """Last two octets of a BSSID: shared by all virtual APs of one radio."""
    parts = str(bssid or "").lower().split(":")
    return ":".join(parts[-2:]) if len(parts) == 6 else None


def analyze_overlap(scan: dict | None, wifi: dict | None) -> dict:
    scan = scan or {}
    wifi = wifi or {}
    if not scan.get("available"):
        return {"available": False, "reason": scan.get("reason", "unavailable")}

    nets = [n for n in scan.get("nets", []) if n.get("channel")]
    bands = {"2.4": [], "5": [], "6": []}
    for n in nets:
        bands[_band_of(n)].append(n)

    cur_ch = wifi.get("channel")
    cur_band = None
    if wifi.get("band"):
        cur_band = _band_of({"band": wifi.get("band"), "channel": cur_ch or 0})
    elif cur_ch:
        cur_band = _band_of({"channel": cur_ch})

    # 2.4 GHz: score the non-overlapping trio, recommend the clearest.
    g24 = bands["2.4"]
    ch_scores = [{"channel": c, "load": _congestion_24(g24, c)} for c in NONOVERLAP_24]
    ch_scores.sort(key=lambda x: x["load"])
    rec_24 = ch_scores[0]["channel"] if g24 else 1

    # 5 GHz: co-channel counts.
    g5 = bands["5"]
    used5 = {}
    for n in g5:
        used5[n["channel"]] = used5.get(n["channel"], 0) + 1
    rec_5 = None
    if g5:
        rec_5 = min(COMMON_5, key=lambda c: used5.get(c, 0))

    # Overlap on the CURRENT channel (how many neighbours collide with us).
    # A router's extra virtual networks (hidden mesh/backhaul/guest SSIDs) are
    # the same radio, not neighbours: they share the BSSID's low bytes on the
    # same channel. Counting them reported "overlap" from the router itself.
    own = {_mac_tail(n.get("bssid")) for n in nets
           if wifi.get("ssid") and n.get("ssid") == wifi.get("ssid") and _mac_tail(n.get("bssid"))}
    others = [n for n in nets if not (n.get("ssid") == wifi.get("ssid") and wifi.get("ssid"))
              and _mac_tail(n.get("bssid")) not in own]
    have_macs = bool(own)
    cur_overlap = None
    if cur_ch:
        if cur_band == "2.4":
            pool = others if have_macs else [n for n in g24 if n["channel"] != cur_ch]
            cur_overlap = sum(1 for n in pool if _band_of(n) == "2.4"
                              and abs(n["channel"] - cur_ch) < 5)
        elif have_macs:
            cur_overlap = sum(1 for n in others if n["channel"] == cur_ch)
        else:
            cur_overlap = sum(1 for n in nets if n["channel"] == cur_ch) - (1 if any(
                n["channel"] == cur_ch for n in nets) else 0)

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
    """Prioritised, reversible optimisation actions. sev: bad|warn|ok, side: client|router|os."""
    wifi = wifi or {}
    overlap = overlap or {}
    latest = latest or {}
    plan = []

    band = overlap.get("current_band")
    if wifi.get("present"):
        if band == "2.4" or (wifi.get("band") and "2.4" in str(wifi.get("band"))):
            plan.append({"sev": "warn", "side": "client",
                         "t": "Prefer 5 GHz / 6 GHz on the adapter — 2.4 GHz is slower and far more congested. "
                              "Set the adapter's 'Preferred Band' to 5 GHz (run optimize-wifi.ps1 as admin)."})
        lm = wifi.get("link_mbps")
        if lm and lm < 300:
            plan.append({"sev": "warn", "side": "client",
                         "t": f"Negotiated link is only {lm} Mbps — improve signal (move closer / reposition AP), "
                              "prefer 5/6 GHz, and disable adapter power-saving (optimize-wifi.ps1)."})

    if overlap.get("available"):
        co = overlap.get("current_overlap")
        rec24 = overlap.get("recommend_24")
        if co and co > 0 and overlap.get("current_band") == "2.4":
            plan.append({"sev": "bad", "side": "router",
                         "t": f"{co} neighbouring AP(s) overlap your 2.4 GHz channel "
                              f"{overlap.get('current_channel')}. On the router, set 2.4 GHz to the clearest "
                              f"non-overlapping channel {rec24} (only 1/6/11 don't overlap) and use 20 MHz width."})
        elif overlap.get("counts", {}).get("g24"):
            plan.append({"sev": "warn", "side": "router",
                         "t": f"On the router, pin 2.4 GHz to channel {rec24} (least congested of 1/6/11) "
                              "and keep 2.4 GHz width at 20 MHz to avoid overlapping neighbours."})
        rec5 = overlap.get("recommend_5")
        if rec5:
            plan.append({"sev": "ok", "side": "router",
                         "t": f"On 5 GHz, channel {rec5} is currently the least used nearby — use it (80 MHz "
                              "width where the band is clear) and enable band steering to push clients to 5 GHz."})
    else:
        reason = overlap.get("reason")
        if reason == "location":
            plan.append({"sev": "warn", "side": "os",
                         "t": "Enable Windows Location (Settings → Privacy & security → Location) so the WLAN "
                              "service can scan neighbours — required for live band/channel overlap analysis."})
        # General best-practice fixes that hold regardless of a live scan.
        plan.append({"sev": "warn", "side": "router",
                     "t": "On the router: set 2.4 GHz to a fixed non-overlapping channel (1, 6 or 11) at 20 MHz "
                          "width, and put 5 GHz on an 80 MHz clear channel — this removes 2.4 GHz overlap."})
        plan.append({"sev": "warn", "side": "router",
                     "t": "Enable band steering / separate the 5 GHz SSID so devices prefer 5/6 GHz over 2.4 GHz."})

    # Network-stack fixes (client, reversible).
    plan.append({"sev": "ok", "side": "client",
                 "t": "Flush DNS and reset the socket stack if throughput is erratic "
                      "(ipconfig /flushdns; netsh winsock reset) — included in optimize-wifi.ps1."})

    nd_date = latest.get("net_driver_date")
    if nd_date:
        plan.append({"sev": "ok", "side": "client",
                     "t": f"Keep the {latest.get('net_driver_name') or 'Wi-Fi'} driver current (installed "
                          f"{nd_date}) — newer drivers improve throughput, roaming and power. Check "
                          "Windows Update (optional updates) or the PC maker's support app."})
    return plan
