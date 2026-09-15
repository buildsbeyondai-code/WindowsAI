"""
Browser-process ownership breakdown.

Modern Windows machines run two kinds of browser process: the standalone browser
you opened, and embedded-browser (WebView2) processes started by *other*
applications — file sync, Start menu search, chat clients, VPN portals. Both show
up in Task Manager as browser engines, but only one of them is yours to close.

Splitting them into mandatory / in-use / discretionary is what makes a
browser-process target honest instead of arbitrary. The classification is
configuration, not code: see ``browser_hosts`` in ``sysperf.config.json``.
"""
from __future__ import annotations


def _match(host: str, table: dict) -> str | None:
    """Case-insensitive lookup of a host process name in a label table."""
    low = host.lower()
    for key, label in (table or {}).items():
        if key.lower() == low:
            return label
    return None


def browser_breakdown(latest: dict, cfg: dict) -> dict:
    hosts_cfg = cfg.get("browser_hosts") or {}
    mandatory_tbl = hosts_cfg.get("mandatory") or {}
    inuse_tbl = hosts_cfg.get("in_use") or {}

    hosts = latest.get("wv2_hosts") or {}
    mandatory: dict[str, int] = {}
    in_use: dict[str, int] = {}
    other: dict[str, int] = {}
    labels: dict[str, str] = {}

    for host, count in hosts.items():
        label = _match(host, mandatory_tbl)
        if label is not None:
            mandatory[host] = count
            labels[host] = label
            continue
        label = _match(host, inuse_tbl)
        if label is not None:
            in_use[host] = count
            labels[host] = label
            continue
        other[host] = count

    m, u, o = sum(mandatory.values()), sum(in_use.values()), sum(other.values())
    edge_main = int(latest.get("edge_main") or 0)
    edge_utility = int(latest.get("edge_utility") or 0)
    # Fixed cost of having one standalone browser open at all, even with a single
    # tab: the browser process, its utility processes, a GPU process and a
    # network service.
    browser_fixed = (edge_main or 1) + (edge_utility or 6)

    return {
        "mandatory": mandatory, "mandatory_total": m,
        "inuse": in_use, "inuse_total": u,
        "other": other, "other_total": o,
        "edge_total": int(latest.get("edge_procs") or 0),
        "edge_split": {
            "main": edge_main,
            "utility": edge_utility,
            "renderer": int(latest.get("edge_renderer") or 0),
            "extension": int(latest.get("edge_extension") or 0),
        },
        "extension_mb": latest.get("edge_ext_mb") or 0,
        "hard_floor": m + u,
        "realistic_floor": m + u + browser_fixed,
        "current_total": int(latest.get("browser_procs") or 0),
        "host_labels": labels,
    }
