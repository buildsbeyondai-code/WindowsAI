"""
Advisor: turns measurements into prioritised, actionable recommendations.

This is the part that makes SysPerf a tool for *improving* performance rather
than only observing it. Every rule is derived from a metric the collector
already gathered, and every recommendation states what was measured, why it
matters, and the exact step to take.

**SysPerf never changes your system.** Recommendations are text. Where a command
is given it is shown for you to run deliberately, and each is labelled with
whether it needs administrator rights and whether it is reversible. That is a
design constraint, not an oversight: a tool that silently edits services,
registry keys or scheduled tasks is impossible to audit and impossible to trust.
"""
from __future__ import annotations

from typing import Any, Callable

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

Rule = Callable[[dict, dict, dict, dict], "list[dict] | dict | None"]
_RULES: list[Rule] = []


def rule(func: Rule) -> Rule:
    """Register an advisor rule."""
    _RULES.append(func)
    return func


def _rec(rec_id: str, title: str, severity: str, category: str, finding: str,
         why: str, action: list[str], *, command: str | None = None,
         needs_admin: bool = False, reversible: bool = True,
         impact: str = "") -> dict:
    return {
        "id": rec_id, "title": title, "severity": severity, "category": category,
        "finding": finding, "why": why, "action": action, "command": command,
        "needs_admin": needs_admin, "reversible": reversible, "impact": impact,
    }


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _row(scorecard: dict, key: str) -> dict | None:
    for row in scorecard.get("rows") or []:
        if row.get("key") == key:
            return row
    return None


# --------------------------------------------------------------------------- #
# memory
# --------------------------------------------------------------------------- #

@rule
def _commit_over_ram(latest: dict, sc: dict, cfg: dict, prof: dict):
    committed = _num(latest.get("committed_mb"))
    ram = _num(latest.get("total_ram_mb"))
    limit = _num(latest.get("commit_limit_mb"))
    if not committed or not ram:
        return None
    if committed <= ram:
        return None
    over = committed - ram
    pct_of_limit = (committed / limit * 100.0) if limit else 0.0
    sev = "critical" if pct_of_limit > 85 else "high"
    return _rec(
        "mem.commit-exceeds-ram",
        "Commit charge exceeds physical RAM",
        sev, "memory",
        f"Commit charge is {committed:,.0f} MB against {ram:,.0f} MB of RAM "
        f"({over:,.0f} MB over"
        + (f", {pct_of_limit:.0f}% of the commit limit" if limit else "") + ").",
        "Once commitments exceed RAM the memory manager must compress or page "
        "out to satisfy them. Compression costs CPU on every access and paging "
        "costs disk latency, so everything feels slower even when the CPU looks idle.",
        ["Close the largest memory consumers listed under Top processes that you "
         "do not currently need.",
         "Reduce the number of open browser tabs and background apps.",
         "If this is persistent rather than occasional, the machine is under-"
         "provisioned for its workload — more RAM is the real fix."],
        impact="Removes compression and paging from the hot path.",
    )


@rule
def _compression_high(latest: dict, sc: dict, cfg: dict, prof: dict):
    comp = _num(latest.get("compression_mb"))
    ram = _num(latest.get("total_ram_mb"))
    if not comp or not ram or comp < ram * 0.04:
        return None
    return _rec(
        "mem.compression-high",
        "Memory Compression is holding a large working set",
        "high" if comp > ram * 0.08 else "medium", "memory",
        f"The Memory Compression process holds {comp:,.0f} MB "
        f"({comp / ram * 100:.1f}% of RAM).",
        "Compressed pages are pages that did not fit. The store only grows when "
        "RAM is oversubscribed, and every hit on a compressed page costs CPU to "
        "decompress before the data can be used.",
        ["Close idle applications, then re-check — the store shrinks as pressure falls.",
         "Restart long-running apps that have grown (browsers and Electron apps "
         "in particular) rather than leaving them resident for days.",
         "A restart clears the store completely if it has grown after heavy work."],
        impact="Each MB reclaimed is an MB that stops costing CPU to reach.",
    )


@rule
def _available_low(latest: dict, sc: dict, cfg: dict, prof: dict):
    avail = _num(latest.get("available_mb"))
    ram = _num(latest.get("total_ram_mb"))
    if not avail or not ram or avail > ram * 0.15:
        return None
    return _rec(
        "mem.available-low",
        "Available physical memory is low",
        "critical" if avail < ram * 0.08 else "high", "memory",
        f"Only {avail:,.0f} MB of {ram:,.0f} MB is available "
        f"({avail / ram * 100:.1f}%).",
        "Below roughly 10% free the memory manager spends its time reclaiming "
        "instead of serving, which shows up as stalls when switching windows or "
        "opening files.",
        ["Close what you are not using — start with the Top processes panel.",
         "Check for a process whose memory only ever grows; that is a leak and "
         "needs a restart of that app, not of Windows."],
        impact="Restores headroom for the file cache and new allocations.",
    )


@rule
def _top_memory(latest: dict, sc: dict, cfg: dict, prof: dict):
    top = latest.get("top_mem") or []
    ram = _num(latest.get("total_ram_mb"))
    if not top or not ram:
        return None
    worst = top[0]
    mb = _num(worst.get("mb"))
    if mb < ram * 0.12:
        return None
    return _rec(
        "mem.single-process-dominant",
        f"{worst.get('name', 'A process')} dominates memory use",
        "medium", "memory",
        f"{worst.get('name')} holds {mb:,.0f} MB "
        f"({mb / ram * 100:.1f}% of RAM) across "
        f"{int(_num(worst.get('count'), 1))} process(es).",
        "One dominant consumer is usually the cheapest thing to fix: restarting "
        "a single application recovers memory without disrupting the session.",
        [f"Save your work in {worst.get('name')} and restart it.",
         "If it grows back to the same size within minutes, look for an add-in, "
         "extension or open document set that is driving it."],
        impact=f"Up to ~{mb:,.0f} MB recoverable.",
    )


# --------------------------------------------------------------------------- #
# startup and background load
# --------------------------------------------------------------------------- #

@rule
def _startup_apps(latest: dict, sc: dict, cfg: dict, prof: dict):
    row = _row(sc, "startup_apps")
    count = _num(latest.get("startup_apps"))
    if not row or not count or row.get("status") == "met":
        return None
    names = [e.get("name") for e in (latest.get("startup_list") or [])[:12]
             if e.get("name")]
    listing = ", ".join(names) if names else "see Task Manager"
    return _rec(
        "startup.too-many",
        "Too many programs start automatically",
        "high" if count > _num(row.get("target")) * 2 else "medium", "startup",
        f"{count:.0f} enabled auto-start entries "
        f"(target {_num(row.get('target')):.0f}). Currently enabled: {listing}.",
        "Every auto-start entry costs boot time once and resident memory "
        "permanently. Most are updaters and tray helpers that the application "
        "does not need in order to work.",
        ["Open Task Manager > Startup apps (Ctrl+Shift+Esc).",
         "Disable updaters, tray helpers and anything you do not recognise as "
         "essential. Disabling is reversible and does not uninstall anything.",
         "Leave security agents and VPN clients enabled — those are managed for "
         "a reason and may be re-enabled by policy anyway."],
        command="Get-CimInstance Win32_StartupCommand | "
                "Select-Object Name, Command, Location | Format-Table -AutoSize",
        impact="Faster sign-in and lower idle memory.",
    )


@rule
def _uptime_long(latest: dict, sc: dict, cfg: dict, prof: dict):
    hours = _num(latest.get("uptime_h"))
    limit = _num((cfg.get("advisor") or {}).get("reboot_after_hours"), 168)
    if hours < limit:
        return None
    return _rec(
        "maint.uptime-long",
        "The machine has not been restarted in a long time",
        "medium", "maintenance",
        f"Uptime is {hours / 24:.1f} days.",
        "Long uptime accumulates fragmented kernel pools, leaked handles and "
        "pending-restart updates. A restart is the single cheapest reset for all "
        "three, and it is what makes the difference after applying other changes.",
        ["Save your work and restart Windows.",
         "Restart rather than shut down and power on if fast startup is enabled — "
         "a shutdown may only hibernate the kernel session."],
        impact="Clears leaked handles, pool growth and pending updates.",
    )


@rule
def _restart_pending(latest: dict, sc: dict, cfg: dict, prof: dict):
    if not latest.get("restart_pending"):
        return None
    return _rec(
        "maint.restart-pending",
        "A restart is pending from an update or install",
        "high", "maintenance",
        "Windows reports a pending reboot (servicing or component-store flag set).",
        "Until the restart happens, files are staged rather than in force, and "
        "some components run in a mixed state. Update-related slowness usually "
        "resolves at this restart.",
        ["Restart Windows at your next convenient break."],
        impact="Completes in-flight servicing work.",
    )


@rule
def _handles_growth(latest: dict, sc: dict, cfg: dict, prof: dict):
    row = _row(sc, "handles")
    if not row or row.get("status") != "regress":
        return None
    return _rec(
        "maint.handles-regress",
        "Open handle count is above your baseline",
        "medium", "maintenance",
        f"{_num(row.get('current')):,.0f} handles vs a baseline of "
        f"{_num(row.get('baseline')):,.0f}.",
        "Handle count tracks how many kernel objects processes hold. A steady "
        "climb that never falls back points at a process that opens resources "
        "without releasing them.",
        ["Check the Top processes panel for one process with an unusually high count.",
         "Restart that application; if the pattern repeats daily, update or "
         "replace it."],
        impact="Reduces non-paged pool pressure.",
    )


# --------------------------------------------------------------------------- #
# browser
# --------------------------------------------------------------------------- #

@rule
def _browser_sprawl(latest: dict, sc: dict, cfg: dict, prof: dict):
    row = _row(sc, "browser_procs")
    if not row or row.get("status") == "met":
        return None
    cur = _num(row.get("current"))
    target = _num(row.get("target"))
    ext = int(_num(latest.get("edge_extension")))
    ext_mb = _num(latest.get("edge_ext_mb"))
    actions = ["Close tabs you are finished with — each isolated site costs its "
               "own process.",
               "Use one browser window with fewer tabs rather than several windows."]
    if ext:
        actions.insert(0, f"Review browser extensions: {ext} extension process(es) "
                          f"are resident, holding {ext_mb:,.0f} MB. Remove the ones "
                          "you do not use; extensions with persistent background "
                          "pages never release their process.")
    return _rec(
        "browser.process-sprawl",
        "Browser-engine processes are above target",
        "medium", "browser",
        f"{cur:.0f} browser-engine processes against a floor-derived target of "
        f"{target:.0f}.",
        "Each browser and embedded-browser (WebView2) process carries its own "
        "heap and handle set. The count is the largest single lever on a typical "
        "office machine because it is entirely user-controlled.",
        actions,
        impact="Frees memory and handles proportional to the tabs closed.",
    )


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

@rule
def _disk_free(latest: dict, sc: dict, cfg: dict, prof: dict):
    free = _num(latest.get("c_free_gb"))
    size = _num(latest.get("c_size_gb"))
    if not free or not size:
        return None
    frac = free / size
    if frac > 0.15:
        return None
    return _rec(
        "storage.low-free-space",
        "System volume is running out of free space",
        "critical" if frac < 0.07 else "high", "storage",
        f"{free:,.1f} GB free of {size:,.1f} GB ({frac * 100:.1f}%).",
        "Windows needs free space for the pagefile, servicing, temp files and "
        "the search index. Below about 10% the pagefile stops growing on demand "
        "and updates begin to fail.",
        ["Run Storage Sense: Settings > System > Storage > Cleanup recommendations.",
         "Clear the Windows temp folders and the Delivery Optimization cache.",
         "Uninstall applications you no longer use, and move large media or "
         "archives to another volume."],
        command="cleanmgr.exe /lowdisk",
        impact="Restores headroom for paging, updates and indexing.",
    )


@rule
def _pagefile(latest: dict, sc: dict, cfg: dict, prof: dict):
    size = _num(latest.get("pagefile_mb"))
    peak = _num(latest.get("pagefile_peak_mb"))
    if not size:
        return None
    if peak < size * 0.8:
        return None
    return _rec(
        "storage.pagefile-pressure",
        "Pagefile usage has approached its allocated size",
        "high", "storage",
        f"Peak usage {peak:,.0f} MB of {size:,.0f} MB allocated"
        + (" (system managed)" if latest.get("pagefile_automatic") else " (fixed size)")
        + ".",
        "A pagefile at its ceiling means allocations were satisfied only just in "
        "time. With a fixed-size pagefile the next spike fails outright and "
        "applications crash with out-of-memory errors.",
        ["Leave the pagefile system-managed unless you have a specific reason not to: "
         "Settings > System > About > Advanced system settings > Performance > "
         "Advanced > Virtual memory.",
         "Reduce concurrent memory demand, or add RAM if this recurs."],
        needs_admin=True,
        impact="Prevents out-of-memory failures under peak load.",
    )


# --------------------------------------------------------------------------- #
# network
# --------------------------------------------------------------------------- #

@rule
def _net_remote(latest: dict, sc: dict, cfg: dict, prof: dict):
    row = _row(sc, "net_remote")
    if not row or row.get("status") == "met":
        return None
    return _rec(
        "network.background-chatter",
        "More background network connections than necessary",
        "low", "network",
        f"{_num(row.get('current')):.0f} established remote connections "
        f"(target {_num(row.get('target')):.0f}); "
        f"{int(_num(latest.get('net_loopback')))} loopback connections are excluded.",
        "Every background connection carries a wake-up cost and keeps its owning "
        "process resident. Sync clients, telemetry and idle chat apps dominate "
        "this number on a typical machine.",
        ["Pause file-sync clients while you need the machine to be responsive.",
         "Quit chat and meeting apps you are not using rather than minimising them.",
         "Inspect the owners: the command below groups connections by process."],
        command="Get-NetTCPConnection -State Established | "
                "Group-Object OwningProcess | "
                "Select-Object Count, @{n='Process';e={(Get-Process -Id $_.Name)"
                ".ProcessName}} | Sort-Object Count -Descending",
        impact="Fewer wake-ups and less resident background code.",
    )


# --------------------------------------------------------------------------- #
# drivers and hardware
# --------------------------------------------------------------------------- #

@rule
def _driver_faults(latest: dict, sc: dict, cfg: dict, prof: dict):
    count = int(_num(latest.get("drv_problem")))
    if not count:
        return None
    names = [p.get("name") for p in (latest.get("drv_problem_list") or [])[:6]]
    return _rec(
        "drivers.faults",
        "Devices are reporting driver fault codes",
        "high", "drivers",
        f"{count} device(s) with driver-specific Device Manager error codes: "
        + ("; ".join(str(n) for n in names if n) or "see Device Manager") + ".",
        "A device with a fault code is either not working or falling back to a "
        "generic driver. Graphics, storage and network fallbacks are a common "
        "cause of unexplained slowness.",
        ["Open Device Manager (devmgmt.msc) and look for the flagged devices.",
         "Install the vendor driver for each — from the OEM support page for your "
         "exact model, not a generic package.",
         "Re-check here after the next restart."],
        command="devmgmt.msc",
        needs_admin=True,
        impact="Restores hardware acceleration and correct device behaviour.",
    )


@rule
def _stale_drivers(latest: dict, sc: dict, cfg: dict, prof: dict):
    count = int(_num(latest.get("drv_stale")))
    if not count:
        return None
    years = int(_num((cfg.get("advisor") or {}).get("stale_driver_years"), 4))
    items = latest.get("drv_stale_list") or []
    listing = "; ".join(
        f"{i.get('name')} ({i.get('date')})" for i in items[:5] if i.get("name"))
    return _rec(
        "drivers.stale",
        f"Third-party drivers older than {years} years are installed",
        "medium", "drivers",
        f"{count} stale driver package(s). Oldest: {listing or 'see the Drivers panel'}.",
        "Old drivers miss the power-management and stability fixes that shipped "
        "later. Storage, chipset and graphics drivers are the ones that measurably "
        "affect performance.",
        ["Run the OEM update utility for your machine, or download drivers from "
         "the support page for your exact model.",
         "Check Windows Update > Advanced options > Optional updates > Driver updates.",
         "Prioritise storage, chipset, graphics and network; ignore stale "
         "peripheral drivers for hardware you no longer own."],
        needs_admin=True,
        impact="Fixes known regressions and improves power behaviour.",
    )


@rule
def _hardware_faults(latest: dict, sc: dict, cfg: dict, prof: dict):
    devices = int(_num(latest.get("hw_problem_devices")))
    disks = int(_num(latest.get("hw_disks_unhealthy")))
    if not devices and not disks:
        return None
    sev = "critical" if disks else "high"
    finding = []
    if devices:
        finding.append(f"{devices} device(s) with Device Manager error codes")
    if disks:
        finding.append(f"{disks} disk(s) not reporting Healthy")
    return _rec(
        "hardware.faults",
        "Hardware faults detected",
        sev, "hardware",
        "; ".join(finding) + ".",
        "An unhealthy disk is a data-loss risk before it is a performance "
        "problem, and a failing device can hold the whole bus in a retry loop.",
        ["Back up your data now if a disk is flagged.",
         "Check the vendor's storage utility and the SMART attributes.",
         "For flagged devices, reinstall the vendor driver; if the code persists, "
         "the hardware itself needs attention."],
        command="Get-PhysicalDisk | Select-Object FriendlyName, MediaType, "
                "HealthStatus, OperationalStatus",
        impact="Prevents data loss and removes bus-level stalls.",
    )


@rule
def _battery_health(latest: dict, sc: dict, cfg: dict, prof: dict):
    bat = latest.get("hw_battery") or {}
    design = _num(bat.get("design_capacity"))
    full = _num(bat.get("full_capacity"))
    if not design or not full:
        return None
    health = full / design * 100.0
    if health >= 70:
        return None
    return _rec(
        "hardware.battery-worn",
        "Battery capacity has degraded significantly",
        "medium", "hardware",
        f"Full-charge capacity is {health:.0f}% of design capacity.",
        "A worn battery cannot sustain turbo power draw. Firmware responds by "
        "capping processor power on battery, which reads as sudden slowness when "
        "the charger is unplugged.",
        ["Keep the machine on AC for sustained workloads.",
         "Have the battery replaced if the machine must run unplugged."],
        impact="Restores full processor power when running on battery.",
    )


# --------------------------------------------------------------------------- #
# power and platform
# --------------------------------------------------------------------------- #

@rule
def _power_plan(latest: dict, sc: dict, cfg: dict, prof: dict):
    plan = str(latest.get("power_plan") or "")
    guid = str(latest.get("power_plan_guid") or "").lower()
    if (guid and guid != "a1841308-3541-4fab-bc81-f71556f20b4a") or (not guid and "saver" not in plan.lower()):
        return None
    return _rec(
        "power.saver-active",
        "A power-saving plan is active",
        "high", "power",
        f"Active power plan: {plan}.",
        "Power saver caps processor frequency and delays turbo. It is the most "
        "common cause of a machine that is slow for no visible reason.",
        ["Settings > System > Power & battery > Power mode: choose Balanced or "
         "Best performance.",
         "On a laptop, keep Balanced on battery and Best performance on AC."],
        command="powercfg /list",
        impact="Removes an artificial ceiling on processor frequency.",
    )


@rule
def _defender_realtime_off(latest: dict, sc: dict, cfg: dict, prof: dict):
    state = latest.get("defender_realtime")
    if state is None or state:
        return None
    return _rec(
        "security.realtime-off",
        "Real-time protection is reported as disabled",
        "high", "security",
        "Microsoft Defender real-time protection is off.",
        "This is reported for completeness, not as a performance tip. If a "
        "third-party product is managing protection this is expected; if not, "
        "the machine is unprotected.",
        ["Confirm which product provides protection: Windows Security > "
         "Virus & threat protection.",
         "Re-enable protection if nothing else is managing it."],
        needs_admin=True,
        impact="Security posture, not speed — do not disable protection to gain performance.",
    )


@rule
def _search_degraded(latest: dict, sc: dict, cfg: dict, prof: dict):
    procs = latest.get("search_procs")
    if procs is None or _num(procs) > 0:
        return None
    return _rec(
        "platform.search-stopped",
        "Windows Search is not running",
        "low", "platform",
        "No Windows Search host processes are present.",
        "With the index down, Start menu and File Explorer searches fall back to "
        "scanning the filesystem, which is slow and drives disk load.",
        ["Check the Windows Search service: services.msc > Windows Search.",
         "If the index is corrupt, rebuild it: Settings > Privacy & security > "
         "Searching Windows > Advanced indexing options > Advanced > Rebuild."],
        impact="Restores fast search without full-disk scans.",
    )


# --------------------------------------------------------------------------- #
# WSL
# --------------------------------------------------------------------------- #

@rule
def _wsl_uncapped(latest: dict, sc: dict, cfg: dict, prof: dict):
    if not latest.get("vmmem_mb"):
        return None
    if latest.get("wsl_cap_mb"):
        return None
    ram = _num(latest.get("total_ram_mb"))
    cap = int(max(2048, ram * 0.35))
    return _rec(
        "wsl.no-memory-cap",
        "WSL 2 has no explicit memory cap",
        "medium", "wsl",
        f"WSL is using {_num(latest.get('vmmem_mb')):,.0f} MB and no "
        "explicit .wslconfig memory limit was found.",
        "By default WSL 2 may claim up to half of physical RAM (older builds: "
        "up to 80%) and does not return it promptly. A cap plus automatic "
        "reclaim keeps the Windows side responsive.",
        [f"Create %USERPROFILE%\\.wslconfig with a cap of about {cap} MB.",
         "Run `wsl --shutdown` afterwards — the setting is read when the VM starts.",
         "Suggested contents:\n[wsl2]\nmemory=" + f"{cap // 1024}GB\n"
         "[experimental]\nautoMemoryReclaim=gradual\nsparseVhd=true"],
        impact="Returns idle Linux memory to Windows automatically.",
    )


@rule
def _wsl_reclaim_off(latest: dict, sc: dict, cfg: dict, prof: dict):
    if not latest.get("wsl_config_present") or not latest.get("vmmem_mb"):
        return None
    if latest.get("wsl_autoreclaim"):
        return None
    return _rec(
        "wsl.no-auto-reclaim",
        "WSL 2 automatic memory reclaim is not enabled",
        "low", "wsl",
        ".wslconfig exists but does not set autoMemoryReclaim.",
        "Without it, the WSL VM keeps memory it has finished using until the VM "
        "is shut down, so a single build can hold gigabytes for the rest of the day.",
        ["Add `autoMemoryReclaim=gradual` under `[experimental]` in %USERPROFILE%\\.wslconfig.",
         "Run `wsl --shutdown` to apply it."],
        impact="Idle Linux memory is returned without manual shutdowns.",
    )


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def build_advice(latest: dict, scorecard: dict, cfg: dict,
                 profile: dict | None = None) -> dict:
    """
    Run every rule and return a prioritised recommendation payload.

    Rules are independent and defensive: a rule that raises is skipped rather
    than taking the dashboard down with it.
    """
    acfg = cfg.get("advisor") or {}
    if not acfg.get("enabled", True):
        return {"enabled": False, "items": [], "counts": {}}

    items: list[dict] = []
    for fn in _RULES:
        try:
            produced = fn(latest, scorecard, cfg, profile or {})
        except Exception:
            continue
        if not produced:
            continue
        if isinstance(produced, dict):
            items.append(produced)
        else:
            items.extend(produced)

    items.sort(key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["title"]))
    limit = int(acfg.get("max_recommendations", 20))
    items = items[:limit]
    counts: dict[str, int] = {}
    for item in items:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1
    return {
        "enabled": True,
        "items": items,
        "counts": counts,
        "total": len(items),
        "read_only": True,
    }
