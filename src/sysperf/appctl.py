"""
Per-application start/stop control for the top CPU consumers.

Powers the "App Control" tab: the top-N applications by CPU over the recent
window, each with Stop (kill by process name) and Start (relaunch the last-seen
executable) actions — the same idea as onedrive.py, generalised to whatever is
currently burning CPU.

Safety
------
* Injected command runner (``fixes.default_runner``) — unit-testable, no OS
  access in tests. The API layer adds the CSRF token and honours ``FIX_ENABLED``.
* **Never kills arbitrary processes.** Only names in the server-computed top-N
  list are controllable, and a denylist of OS/session/security processes is
  hard-blocked (killing them crashes the session or disables protection). The
  API rejects any name outside the current allowlist.
* Process names are validated against a strict charset and single-quote-escaped
  before entering a PowerShell command, so a name cannot inject shell/PS syntax.
* Start uses the executable path the collector *observed* for that process, never
  a client-supplied path — so it cannot be used to launch an arbitrary binary.
"""
from __future__ import annotations

import re
from typing import Callable

from . import cpuwatch, onedrive

# Processes with a reliable known launcher, used when the collector could not
# observe an executable path (e.g. OneDrive's Win32_Process ExecutablePath comes
# back empty). Keyed by lower-case process name.
KNOWN_START = {"onedrive": onedrive.PS_START}

# OS / session / security processes that must never be stopped.
CRITICAL = {
    "system", "registry", "idle", "smss", "csrss", "wininit", "winlogon",
    "services", "lsass", "fontdrvhost", "dwm", "logonui", "wudfhost", "sihost",
    "taskhostw", "ctfmon", "shellexperiencehost", "startmenuexperiencehost",
    "searchhost", "searchindexer", "runtimebroker", "dllhost", "wmiprvse",
    "conhost", "dashost", "securityhealthservice", "securityhealthsystray",
    "audiodg", "svchost", "spoolsv", "msmpeng", "mssense", "nissrv",
    "csfalconservice", "csfalconcontainer", "cb", "cyberarkepmservice",
    "pangps", "pangpa", "sense", "mpdefendercoreservice", "devicetrust",
    "intunemanagementextension",
}
NAME_RE = re.compile(r"^[A-Za-z0-9 ._+\-]{1,64}$")
TOP_N = 5


def is_controllable(name: str) -> bool:
    """True if the process may be stopped/started via this control."""
    return bool(name) and bool(NAME_RE.match(name)) and name.lower() not in CRITICAL


def _can_start(name: str, exe: str | None) -> bool:
    """Startable if controllable and we have either an observed path or a known
    launcher for it (e.g. OneDrive)."""
    return is_controllable(name) and (exe is not None
                                      or name.lower() in KNOWN_START)


def last_exe(history, name: str) -> str | None:
    """Most recently observed executable path for a process name, or None."""
    for s in reversed(list(history)):
        if not isinstance(s, dict):
            continue
        for p in s.get("cpu_procs") or []:
            if isinstance(p, dict) and p.get("n") == name and p.get("exe"):
                return p["exe"]
    return None


def top_apps(history, *, top: int = TOP_N) -> dict:
    """The top-`top` applications by CPU, with run state and control eligibility."""
    hist = list(history)
    cc = cpuwatch.consistent_cpu(hist, top=50)
    ranked = sorted(cc.get("consumers") or [],
                    key=lambda c: c["median_cpu_pct"], reverse=True)[:top]
    running_now = set()
    for s in reversed(hist):
        if isinstance(s, dict) and isinstance(s.get("cpu_procs"), list):
            running_now = {p.get("n") for p in s["cpu_procs"]
                           if isinstance(p, dict)}
            break
    apps = []
    for c in ranked:
        name = c["name"]
        controllable = is_controllable(name)
        apps.append({
            "name": name,
            "current_cpu_pct": c["current_cpu_pct"],
            "median_cpu_pct": c["median_cpu_pct"],
            "peak_cpu_pct": c["peak_cpu_pct"],
            "consistency_pct": c["consistency_pct"],
            "ws_mb": c["ws_mb"],
            "running": name in running_now,
            "controllable": controllable,
            "can_start": _can_start(name, last_exe(hist, name)),
        })
    return {"available": cc.get("available", False), "top": top,
            "window_min": cc.get("window_min", 0.0), "apps": apps}


def controllable_names(history, *, top: int = TOP_N) -> set:
    """The allowlist: controllable names currently in the top-`top` list."""
    return {a["name"] for a in top_apps(history, top=top)["apps"]
            if a["controllable"]}


def total_cpu(cpu_row: dict | None, current) -> dict:
    """Overall-CPU health indicator for the App Control tab.

    Judged the same way the dashboard scores ``cpu_pct``: against its target,
    using the rolling ``scored_typical`` when available (so a single spike does
    not flip the light) and falling back to the current reading otherwise.
    ``good`` is None (grey) until CPU has been scored.
    """
    row = cpu_row or {}
    target = row.get("target")
    typical = row.get("scored_typical")
    judged = typical if typical is not None else current
    good = None
    if target is not None and judged is not None:
        good = judged <= target
    return {"current_pct": current, "typical_pct": typical,
            "target_pct": target, "status": row.get("status"), "good": good}


def _ps_quote(s: str) -> str:
    """Single-quote-escape a value for safe embedding in a PowerShell command."""
    return s.replace("'", "''")


def stop(runner: Callable[[str, str], tuple], name: str) -> dict:
    """Kill every instance of `name` by process name. Rejects bad/critical names."""
    if not is_controllable(name):
        raise ValueError(f"{name!r} is not a controllable application")
    cmd = (f"Stop-Process -Name '{_ps_quote(name)}' -Force "
           "-ErrorAction SilentlyContinue; $null = $?")
    rc, output = runner("pwsh", cmd)
    return {"action": "stop", "name": name,
            "status": "ok" if rc == 0 else "error",
            "returncode": rc, "output": output}


# Seconds to wait before the detached WSL shutdown fires. The dashboard runs
# *inside* WSL, so `wsl --shutdown` terminates this very process; the delay lets
# the API response reach the browser before the VM (and this service) stops.
WSL_SHUTDOWN_DELAY_S = 4


def wsl_shutdown(runner: Callable[[str, str], tuple],
                 delay_s: int = WSL_SHUTDOWN_DELAY_S) -> dict:
    """Shut down the WSL2 VM to reclaim its ``vmmemWSL`` memory.

    ``wsl --shutdown`` stops the lightweight WSL2 VM and frees the entire
    ``vmmemWSL`` footprint back to Windows. Because this dashboard runs *inside*
    WSL, the command also stops this service — so it is launched as a detached,
    window-hidden Windows process that waits ``delay_s`` seconds first, allowing
    the HTTP response to return before the VM goes down. The dashboard comes back
    on the next WSL start (e.g. opening a terminal or its systemd unit at boot).

    This is a session-killing action: it is never run automatically by the
    Auto-Improve loop and is only reachable through the explicit, confirmed
    button in the App Control tab.
    """
    delay = max(1, int(delay_s))
    # Inner script runs in a *separate*, detached Windows powershell so it
    # survives this WSL instance being torn down.
    inner = f"Start-Sleep -Seconds {delay}; wsl.exe --shutdown"
    cmd = ("Start-Process -WindowStyle Hidden -FilePath 'powershell.exe' "
           "-ArgumentList '-NoProfile','-NonInteractive','-WindowStyle','Hidden',"
           f"'-Command','{_ps_quote(inner)}'; 'scheduled'")
    rc, output = runner("pwsh", cmd)
    return {"action": "wsl-shutdown", "name": "vmmemWSL",
            "status": "scheduled" if rc == 0 else "error",
            "delay_s": delay, "returncode": rc, "output": output}


def start(runner: Callable[[str, str], tuple], name: str,
          exe: str | None) -> dict:
    """Relaunch `name`. Uses a known launcher (e.g. OneDrive) when available,
    else the observed executable `exe`. Rejects bad/critical names."""
    if not is_controllable(name):
        raise ValueError(f"{name!r} is not a controllable application")
    known = KNOWN_START.get(name.lower())
    if known:
        cmd = known
    elif exe:
        cmd = f"Start-Process -FilePath '{_ps_quote(exe)}'"
    else:
        raise ValueError(f"no known executable path for {name!r}")
    rc, output = runner("pwsh", cmd)
    return {"action": "start", "name": name,
            "status": "ok" if rc == 0 else "error",
            "returncode": rc, "output": output}
