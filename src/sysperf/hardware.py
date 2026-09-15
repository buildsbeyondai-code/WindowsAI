"""
Hardware and driver health panel.

Two sources are combined:

* **auto-detected** — Device Manager error codes, storage health, battery state
  and wear, third-party driver inventory;
* **user-tracked** — ``hw_watchlist.json``, for functional faults Windows cannot
  see. A touchpad button that misfires or a fan that rattles reports "OK" to
  every API; without somewhere to record it, the dashboard would insist the
  machine is fine while the user knows it is not.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import WATCHLIST_FILE

WATCHLIST_TEMPLATE = [
    {
        "component": "example — delete me",
        "issue": "Describe a fault Windows reports as OK (e.g. touchpad click sticks)",
        "severity": "minor",
        "status": "open",
        "note": "Edit hw_watchlist.json to track your own items.",
    }
]


def load_watchlist(path: Path | None = None) -> list:
    """User-maintained hardware issue list. Re-read on every request."""
    try:
        data = json.loads(Path(path or WATCHLIST_FILE).read_text(encoding="utf-8"))
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def ensure_watchlist(path: Path | None = None) -> None:
    """Create a commented template on first run; never overwrite user content."""
    target = Path(path or WATCHLIST_FILE)
    if target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps([], indent=2) + "\n",
                          encoding="utf-8")
    except OSError:
        pass


def battery_health(battery: dict | None) -> float | None:
    """Full-charge capacity as a percentage of design capacity, when known."""
    if not battery:
        return None
    design = battery.get("design_capacity")
    full = battery.get("full_capacity")
    try:
        if design and full and float(design) > 0:
            return round(float(full) / float(design) * 100.0, 1)
    except (TypeError, ValueError):
        return None
    return None


def build_hardware(latest: dict, watchlist_path: Path | None = None) -> dict:
    watchlist = load_watchlist(watchlist_path)
    open_issues = sum(1 for w in watchlist
                      if str(w.get("status", "")).lower() != "resolved")
    battery = latest.get("hw_battery")
    return {
        "problem_devices": latest.get("hw_problem_devices"),
        "problem_list": latest.get("hw_problem_list") or [],
        "disks": latest.get("hw_disks") or [],
        "disks_unhealthy": latest.get("hw_disks_unhealthy", 0),
        "battery": battery,
        "battery_health_pct": battery_health(battery),
        "pointing": latest.get("hw_pointing") or [],
        "touchpad": latest.get("hw_touchpad") or [],
        "watchlist": watchlist,
        "open_issues": open_issues,
        "faults": (latest.get("hw_problem_devices") or 0)
        + (latest.get("hw_disks_unhealthy") or 0),
        "drivers": {
            "total": latest.get("drv_total"),
            "unsigned": latest.get("drv_unsigned", 0),
            "stale": latest.get("drv_stale", 0),
            "stale_list": latest.get("drv_stale_list") or [],
            "problem": latest.get("drv_problem", 0),
            "problem_list": latest.get("drv_problem_list") or [],
        },
    }
