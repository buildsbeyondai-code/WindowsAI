"""
Machine profiling, baseline capture, and target derivation.

The original tool shipped hand-tuned baselines and targets for a single laptop.
That is exactly what makes a tool non-portable, so SysPerf learns them instead:

1. **Baseline** — the first full sample after installation is persisted to
   ``baseline.json``. Progress is always measured from *your* starting point.
2. **Targets** — derived from the machine's own capacity (RAM, system-volume
   size, measured browser-process floor) using the fractions in
   ``config["targets"]``, then clamped so a target is never worse than the
   baseline and never physically unreachable.
3. **Criteria** — only metrics the machine actually reports are scored. A
   desktop with no WSL never sees a WSL row; a machine with no battery never
   sees a battery row.

Everything is recomputed on demand from the persisted baseline, so editing
``sysperf.config.json`` and restarting is enough to retune.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Criterion tuple: (key, label, baseline, target, higher_is_better, unit)
Criterion = tuple[str, str, float, float, bool, str]

PROFILE_KEYS = (
    "hostname", "machine_vendor", "machine_model", "cpu_name", "cpu_cores",
    "cpu_threads", "total_ram_mb", "os_caption", "os_build", "c_size_gb",
    "wsl_installed", "wsl_cap_mb",
)


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# baseline persistence
# --------------------------------------------------------------------------- #

def load_baseline(path: Path) -> dict:
    """Read the persisted baseline, or an empty dict when not captured yet."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_baseline(path: Path, baseline: dict) -> None:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


# Metrics captured as the "before" picture. Anything absent is simply skipped.
BASELINE_METRICS = (
    "committed_mb", "compression_mb", "available_mb", "vmmem_mb", "handles",
    "browser_procs", "processes", "c_free_gb", "net_remote", "startup_apps",
    "hw_faults", "pool_paged_mb", "pool_nonpaged_mb", "services_running",
    # responsiveness / thermal / startup-launch (added 2.1.0)
    "cpu_pct", "cpu_queue", "disk_active_pct", "cpu_temp_c", "battery_wear_pct",
    "boot_time_s", "wsl_start_s", "explorer_launch_s", "outlook_launch_s",
    "word_launch_s",
)


def capture_baseline(sample: dict, *, note: str = "auto-captured at first run") -> dict:
    """Build a baseline record from a full sample."""
    metrics = {k: sample.get(k) for k in BASELINE_METRICS if sample.get(k) is not None}
    if "hw_faults" not in metrics:
        metrics["hw_faults"] = ((sample.get("hw_problem_devices") or 0)
                                + (sample.get("hw_disks_unhealthy") or 0))
    return {
        "version": 1,
        "captured": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note,
        "profile": {k: sample.get(k) for k in PROFILE_KEYS if sample.get(k) is not None},
        "metrics": metrics,
    }


def build_profile(sample: dict, baseline: dict) -> dict:
    """Machine identity + capacity, live values preferred over baseline ones."""
    prof = dict(baseline.get("profile") or {})
    for key in PROFILE_KEYS:
        if sample.get(key) is not None:
            prof[key] = sample[key]
    return prof


# --------------------------------------------------------------------------- #
# target derivation
# --------------------------------------------------------------------------- #

def _clamp_target(baseline: float, target: float, higher_is_better: bool,
                  hard_floor: float | None = None) -> float:
    """
    Keep a derived target meaningful.

    A target must be an improvement on the baseline (otherwise progress is
    undefined) and must not cross a physical floor such as the measured
    non-closable browser-process count.
    """
    if higher_is_better:
        target = max(target, baseline * 1.02)
        if hard_floor is not None:
            target = min(target, hard_floor)
    else:
        target = min(target, baseline * 0.98)
        if hard_floor is not None:
            target = max(target, hard_floor)
    return round(target, 1)


def browser_floor(sample: dict, hosts_cfg: dict) -> tuple[int, int]:
    """
    Measured floor of browser-engine processes: (hard_floor, realistic_floor).

    ``hard_floor`` counts embedded-browser (WebView2) processes owned by host
    applications that cannot be closed or are in active use. ``realistic_floor``
    adds the fixed cost of one standalone browser being open (main process plus
    its utility processes), because "close the browser entirely" is not a
    performance target anyone can hold.
    """
    hosts = {str(k).lower(): v for k, v in (sample.get("wv2_hosts") or {}).items()}
    mandatory = {k.lower() for k in (hosts_cfg.get("mandatory") or {})}
    in_use = {k.lower() for k in (hosts_cfg.get("in_use") or {})}
    hard = sum(v for k, v in hosts.items() if k in mandatory or k in in_use)
    edge_fixed = int(_num(sample.get("edge_main"), 1) or 1) + \
        int(_num(sample.get("edge_utility"), 6) or 6)
    return hard, hard + edge_fixed


def derive_targets(baseline: dict, sample: dict, cfg: dict) -> dict[str, float]:
    """
    Turn baseline metrics + machine capacity into per-metric targets.

    Every number below is a *ratio of this machine's own capacity*, never an
    absolute constant, which is what makes the tool portable.
    """
    tcfg = dict(cfg.get("targets") or {})
    base = baseline.get("metrics") or {}
    prof = baseline.get("profile") or {}
    ram = _num(sample.get("total_ram_mb")) or _num(prof.get("total_ram_mb")) or 8192.0
    disk = _num(sample.get("c_size_gb")) or _num(prof.get("c_size_gb")) or 256.0
    out: dict[str, float] = {}

    def b(key: str, fallback: float | None = None) -> float | None:
        return _num(base.get(key), _num(sample.get(key), fallback))

    # --- memory ----------------------------------------------------------
    committed = b("committed_mb")
    if committed:
        out["committed_mb"] = _clamp_target(
            committed,
            min(ram * tcfg["commit_of_ram"], committed * tcfg["commit_improve"]),
            False, hard_floor=ram * 0.25)

    compression = b("compression_mb")
    if compression is not None:
        out["compression_mb"] = _clamp_target(
            max(compression, 1.0),
            min(ram * tcfg["compression_of_ram"], max(compression, 1.0) * 0.5),
            False, hard_floor=64.0)

    available = b("available_mb")
    if available:
        out["available_mb"] = _clamp_target(
            available,
            max(ram * tcfg["available_of_ram"], available * tcfg["available_improve"]),
            True, hard_floor=ram * 0.75)

    handles = b("handles")
    if handles:
        out["handles"] = _clamp_target(handles, handles * tcfg["handles_improve"],
                                       False, hard_floor=20000.0)

    # --- WSL working set (ceiling criterion, only when WSL exists) --------
    if sample.get("vmmem_mb") is not None and (sample.get("vmmem_mb") or 0) > 0:
        cap = _num(sample.get("wsl_cap_mb")) or _num(prof.get("wsl_cap_mb"))
        if not cap or cap <= 0:
            cap = ram * tcfg["wsl_cap_of_ram"]
        out["vmmem_mb"] = round(max(1024.0, cap), 1)

    # --- browser processes ------------------------------------------------
    procs = b("browser_procs")
    if procs:
        _hard, realistic = browser_floor(sample, cfg.get("browser_hosts") or {})
        floor = max(realistic + int(tcfg["browser_extra_headroom"]), 8)
        out["browser_procs"] = _clamp_target(procs, min(procs * 0.75, float(floor)),
                                             False, hard_floor=float(floor))

    # --- storage ----------------------------------------------------------
    free = b("c_free_gb")
    if free is not None:
        out["c_free_gb"] = _clamp_target(
            max(free, 0.1), max(disk * tcfg["disk_free_fraction"], free * 1.10),
            True, hard_floor=disk * 0.5)

    # --- network + startup -------------------------------------------------
    net = b("net_remote")
    if net is not None:
        out["net_remote"] = _clamp_target(
            max(net, 1.0), max(float(tcfg["net_remote_floor"]), net * tcfg["net_remote_improve"]),
            False, hard_floor=float(tcfg["net_remote_floor"]))

    startup = b("startup_apps")
    if startup is not None:
        out["startup_apps"] = _clamp_target(
            max(startup, 1.0),
            min(float(tcfg["startup_apps_max"]),
                math.ceil(max(startup, 1.0) * tcfg["startup_apps_improve"])),
            False, hard_floor=2.0)

    # --- hardware faults ---------------------------------------------------
    out["hw_faults"] = 0.0

    # --- responsiveness: CPU utilisation / run-queue / disk active time ----
    cpu = b("cpu_pct")
    if cpu is not None:
        out["cpu_pct"] = _clamp_target(max(cpu, 1.0), max(cpu, 1.0) * tcfg.get("cpu_improve", 0.75),
                                       False, hard_floor=3.0)
    queue = b("cpu_queue")
    if queue is not None:
        out["cpu_queue"] = _clamp_target(max(queue, 1.0), max(queue, 1.0) * tcfg.get("cpu_improve", 0.75),
                                         False, hard_floor=1.0)
    disk_active = b("disk_active_pct")
    if disk_active is not None:
        out["disk_active_pct"] = _clamp_target(max(disk_active, 1.0),
                                               max(disk_active, 1.0) * tcfg.get("disk_active_improve", 0.70),
                                               False, hard_floor=3.0)
    # --- CPU temperature ---------------------------------------------------
    temp = b("cpu_temp_c")
    if temp:
        out["cpu_temp_c"] = _clamp_target(temp, temp * tcfg.get("temp_improve", 0.92),
                                          False, hard_floor=55.0)

    # --- startup & launch latency (lower is better) -----------------------
    launch_improve = tcfg.get("launch_improve", 0.60)
    for key, floor in (("boot_time_s", 20.0), ("wsl_start_s", 3.0),
                       ("explorer_launch_s", 1.0), ("outlook_launch_s", 3.0),
                       ("word_launch_s", 2.0)):
        v = b(key)
        if v:
            out[key] = _clamp_target(v, v * launch_improve, False, hard_floor=floor)

    return out


# --------------------------------------------------------------------------- #
# criteria assembly
# --------------------------------------------------------------------------- #

# (key, label, higher_is_better, unit, requires_key)
CRITERIA_SPEC: tuple[tuple[str, str, bool, str, str | None], ...] = (
    ("committed_mb",   "Commit charge",              False, "MB", None),
    ("compression_mb", "Memory Compression",         False, "MB", None),
    ("available_mb",   "Available physical memory",   True,  "MB", None),
    ("vmmem_mb",       "WSL 2 memory (vmmem)",       False, "MB", "vmmem_mb"),
    ("browser_procs",  "Browser-engine processes",    False, "",   None),
    ("handles",        "Total open handles",          False, "",   None),
    ("c_free_gb",      "System volume free space",    True,  "GB", None),
    ("net_remote",     "Remote TCP connections",      False, "",   "net_remote"),
    ("startup_apps",   "Startup programs (enabled)",  False, "",   "startup_apps"),
    ("hw_faults",      "Hardware / device faults",    False, "",   None),
    # responsiveness / thermal (added 2.1.0)
    ("cpu_pct",           "CPU utilisation",            False, "%", None),
    ("cpu_queue",         "CPU run-queue depth",        False, "",  "cpu_queue"),
    ("disk_active_pct",   "Disk active time",           False, "%", "disk_active_pct"),
    ("cpu_temp_c",        "ACPI thermal zone",            False, "C", "cpu_temp_c"),
    # startup & launch latency (added 2.1.0)
    ("boot_time_s",       "Windows boot time",          False, "s", "boot_time_s"),
    ("wsl_start_s",       "WSL cold-start time",        False, "s", "wsl_start_s"),
    ("explorer_launch_s", "Explorer startup event",       False, "s", "explorer_launch_s"),
    ("outlook_launch_s",  "Outlook startup event",        False, "s", "outlook_launch_s"),
    ("word_launch_s",     "Word startup event",           False, "s", "word_launch_s"),
)


def build_criteria(baseline: dict, sample: dict, cfg: dict) -> list[Criterion]:
    """
    Assemble the criteria list for *this* machine.

    A criterion appears only when both a baseline and a live value exist for it,
    so the scorecard never shows a permanently blank row.
    """
    base = baseline.get("metrics") or {}
    targets = derive_targets(baseline, sample, cfg)
    criteria: list[Criterion] = []
    for key, label, hib, unit, requires in CRITERIA_SPEC:
        if requires and sample.get(requires) is None and base.get(requires) is None:
            continue
        b_val = _num(base.get(key))
        if b_val is None:
            continue
        t_val = targets.get(key)
        if t_val is None:
            continue
        criteria.append((key, label, b_val, float(t_val), hib, unit))
    return criteria


def browser_target_note(sample: dict, cfg: dict, target: float | None) -> str:
    """Explain, from live data, why the browser target sits where it does."""
    hard, realistic = browser_floor(sample, cfg.get("browser_hosts") or {})
    return (
        f"Embedded-browser processes owned by host apps that cannot be closed or "
        f"are in use total {hard}. With one standalone browser open the floor is "
        f"about {realistic}, so the target is set to "
        f"{'—' if target is None else int(target)} rather than an arbitrary number."
    )
