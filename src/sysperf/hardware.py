"""Hardware health panel: auto-detected faults + user-tracked known issues."""
from __future__ import annotations

import json

from .config import SETTINGS, WATCHLIST_FILE


def load_watchlist() -> list:
    """User-maintained hardware issue list (functional faults Windows can't detect)."""
    try:
        data = json.loads(WATCHLIST_FILE.read_text())
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def verified_latest() -> dict:
    """Old-dated drivers checked to be the newest available ({inf: "YYYY-MM-DD"}).

    A driver's INF date is set by its vendor; many current drivers carry years-old
    dates. Set in sysperf.config.json as
    {"drivers": {"verified_latest": {"gscheci.inf": "2026-10-24"}}} once Windows
    Update and the vendor have been checked. Listed, never hidden.
    """
    node = SETTINGS.get("drivers")
    node = node.get("verified_latest") if isinstance(node, dict) else None
    return {str(k).lower(): str(v) for k, v in node.items()} if isinstance(node, dict) else {}


def _split_stale(latest: dict) -> tuple:
    ok = verified_latest()
    listed = latest.get("drv_stale_list") or []
    verified = [dict(d, verified=ok[str(d.get("name", "")).lower()]) for d in listed
                if str(d.get("name", "")).lower() in ok]
    stale = [d for d in listed if str(d.get("name", "")).lower() not in ok]
    count = max(0, (latest.get("drv_stale") or 0) - len(verified))
    return stale, verified, count


def build_hardware(latest: dict) -> dict:
    """Combine live device/disk/battery state with the known-issue watchlist."""
    wl = load_watchlist()
    open_issues = sum(1 for w in wl if str(w.get("status", "")).lower() != "resolved")
    stale, verified, stale_n = _split_stale(latest)
    return {
        "problem_devices": latest.get("hw_problem_devices"),
        "problem_list": latest.get("hw_problem_list") or [],
        "disks": latest.get("hw_disks") or [],
        "disks_unhealthy": latest.get("hw_disks_unhealthy", 0),
        "battery": latest.get("hw_battery"),
        "pointing": latest.get("hw_pointing") or [],
        "touchpad": latest.get("hw_touchpad") or [],
        "watchlist": wl,
        "open_issues": open_issues,
        "faults": (latest.get("hw_problem_devices") or 0)
        + (latest.get("hw_disks_unhealthy") or 0),
        "drivers": {
            "total": latest.get("drv_total"),
            "unsigned": latest.get("drv_unsigned", 0),
            "stale": stale_n,
            "stale_list": stale,
            "verified_latest": verified,
            "problem": latest.get("drv_problem", 0),
            "problem_list": latest.get("drv_problem_list") or [],
        },
    }
