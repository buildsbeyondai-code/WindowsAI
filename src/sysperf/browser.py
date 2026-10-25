"""Browser-process ownership breakdown (mandatory / in-use / discretionary)."""
from __future__ import annotations

from .config import BROWSER_TARGET_NOTE, INUSE_HOSTS, MANDATORY_HOSTS


def browser_breakdown(latest: dict) -> dict:
    """Split browser processes into mandatory / in-use / discretionary (Edge)."""
    hosts = latest.get("wv2_hosts") or {}
    mand = {k: v for k, v in hosts.items() if k in MANDATORY_HOSTS}
    inuse = {k: v for k, v in hosts.items() if k in INUSE_HOSTS}
    other = {k: v for k, v in hosts.items()
             if k not in MANDATORY_HOSTS and k not in INUSE_HOSTS}
    m, u, o = sum(mand.values()), sum(inuse.values()), sum(other.values())
    edge = latest.get("edge_procs", 0)
    edge_floor = (latest.get("edge_main", 1) + latest.get("edge_utility", 7) + 3 + 2)
    return {
        "mandatory": mand, "mandatory_total": m,
        "inuse": inuse, "inuse_total": u,
        "other": other, "other_total": o,
        "edge_total": edge,
        "edge_split": {
            "main": latest.get("edge_main", 0),
            "utility": latest.get("edge_utility", 0),
            "renderer": latest.get("edge_renderer", 0),
            "extension": latest.get("edge_extension", 0),
        },
        "extension_mb": latest.get("edge_ext_mb", 0),
        "hard_floor": m + u + o,
        "realistic_floor": m + u + o + edge_floor,
        "current_total": latest.get("browser_procs", 0),
        "note": BROWSER_TARGET_NOTE,
        "host_labels": {**MANDATORY_HOSTS, **INUSE_HOSTS},
    }
