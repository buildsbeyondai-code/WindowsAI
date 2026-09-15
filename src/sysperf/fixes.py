"""
Common-Fix engine.

SysPerf is read-only by default; this module is the one place that can *act* on
the machine, and only when explicitly enabled (``config["fixes"]["enabled"]``).

Safety model
------------
Every fix declares a ``risk`` class:

  * ``safe``    - non-disruptive maintenance (flush the DNS
                  resolver cache). Eligible for the one-click batch.
  * ``caution`` - reversible but user-visible (restart Explorer, empty the
                  Recycle Bin). Executed only on an explicit confirmed request.
  * ``manual``  - disruptive / needs elevation (``wsl --shutdown``, disabling
                  start-up entries). NEVER executed; the exact command is
                  returned for the user to run.

``session_killing`` fixes (``wsl --shutdown`` restarts the WSL instance SysPerf
may be running in) are hard-blocked from execution regardless of confirmation.

Detection is driven by the live scorecard, so both recurring common issues and
newly-appearing regressions surface automatically. The command runner is
injected, so execution is fully unit-testable without touching the real OS.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

EXECUTABLE_RISKS = ("safe", "caution")


@dataclass
class Fix:
    id: str
    title: str
    category: str
    risk: str
    reversible: bool
    shell: str                                  # pwsh | bash
    command: str
    detect: Callable[[dict], str | None]
    manual_hint: str = ""
    session_killing: bool = False
    persistent: bool = False

    @property
    def executable(self) -> bool:
        return self.risk in EXECUTABLE_RISKS and not self.session_killing


# --------------------------------------------------------------------------- #
# detection helpers
# --------------------------------------------------------------------------- #
def _rows(snapshot: dict) -> dict:
    return {r["key"]: r for r in snapshot.get("scorecard", {}).get("rows", [])}


def _needs(row: dict | None) -> bool:
    return bool(row) and row.get("status") in ("regress", "partial")


def _latest(snapshot: dict) -> dict:
    return snapshot.get("latest", {}) or {}


def _detect_dns(snap: dict) -> str | None:
    row = _rows(snap).get("net_remote")
    if _needs(row):
        return (f"Remote TCP connections at {row.get('current')} "
                f"(target {row.get('target')}) - flushing DNS refreshes cached name lookups but does not close "
                "connections or reduce their count.")
    return "Routine maintenance: clear the DNS resolver cache."


def _detect_temp(snap: dict) -> str | None:
    row = _rows(snap).get("c_free_gb")
    if _needs(row):
        return (f"System volume free {row.get('current')} GB below target "
                f"{row.get('target')} GB - clearing the user TEMP folder reclaims space.")
    return None


def _detect_recycle(snap: dict) -> str | None:
    row = _rows(snap).get("c_free_gb")
    if _needs(row):
        return "Free space below target - emptying the Recycle Bin reclaims disk space."
    return None


def _detect_explorer(snap: dict) -> str | None:
    row = _rows(snap).get("handles")
    if _needs(row):
        return (f"Open handles elevated ({row.get('current')}, target {row.get('target')}) "
                "- restarting Explorer releases leaked shell handles without a reboot.")
    return None


def _detect_startup(snap: dict) -> str | None:
    row = _rows(snap).get("startup_apps")
    if _needs(row):
        return (f"{row.get('current')} enabled start-up programs (target {row.get('target')}) "
                "- trimming non-essential entries shortens boot and lowers idle memory.")
    return None


def _detect_standby(snap: dict) -> str | None:
    rows = _rows(snap)
    if _needs(rows.get("available_mb")) or _needs(rows.get("compression_mb")):
        return ("Available memory low / compression elevated - clearing the standby "
                "(cached) memory list frees physical RAM.")
    return None


def _detect_wsl(snap: dict) -> str | None:
    row = _rows(snap).get("vmmem_mb")
    wsl = _latest(snap).get("wsl") or {}
    if wsl.get("cap_active") is False:
        return ("The .wslconfig memory/CPU cap is written but NOT active - a WSL restart "
                "applies it.")
    if _needs(row):
        return (f"WSL memory at {row.get('current')} MB and not falling - a WSL restart "
                "resets the guest working set.")
    return None


def _detect_wsl_boot(snap: dict) -> str | None:
    rows = _rows(snap)
    wsl = _latest(snap).get("wsl") or {}
    row = rows.get("wsl_start_s")
    if _needs(row):
        return (f"WSL cold-start ~{row.get('current')} s (target {row.get('target')} s) "
                "- tuning .wslconfig (autoMemoryReclaim, vmIdleTimeout, sparseVhd) makes "
                "the guest start and shrink faster. Persists across reboots.")
    if wsl.get("cap_active") is False:
        return ("The .wslconfig cap is not active - writing a tuned .wslconfig speeds every "
                "future WSL start. Persists across reboots.")
    return None


def _detect_sysmain(snap: dict) -> str | None:
    rows = _rows(snap)
    if any(_needs(rows.get(k)) for k in ("explorer_launch_s", "outlook_launch_s",
                                         "word_launch_s", "boot_time_s")):
        return ("App/boot launch is slow - ensuring SysMain (SuperFetch) is enabled "
                "pre-caches Explorer, Outlook and Word so they launch faster. Needs admin; "
                "persists across reboots.")
    return None


def _detect_office(snap: dict) -> str | None:
    rows = _rows(snap)
    slow = [rows[k] for k in ("outlook_launch_s", "word_launch_s") if _needs(rows.get(k))]
    if slow:
        names = ", ".join(r["key"].split("_")[0].capitalize() for r in slow)
        worst = max(r.get("current") or 0 for r in slow)
        return (f"Office app launch slow ({names}, up to ~{worst} s) - slow COM add-ins are "
                "the usual cause.")
    return None


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def build_registry() -> list:
    return [
        Fix(id="flush_dns", title="Flush DNS resolver cache",
            category="network", risk="safe", reversible=True, shell="pwsh",
            command="ipconfig /flushdns", detect=_detect_dns),
        Fix(id="clear_temp", title="Clear user TEMP folder",
            category="storage", risk="manual", reversible=False, shell="pwsh",
            manual_hint="Review temporary files in Windows Settings > System > Storage > Temporary files. Deletion cannot be undone.",
            command=("Get-ChildItem -Path $env:TEMP -Recurse -Force -ErrorAction SilentlyContinue "
                     "| Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"),
            detect=_detect_temp),
        Fix(id="empty_recycle_bin", title="Empty the Recycle Bin",
            category="storage", risk="caution", reversible=False, shell="pwsh",
            command="Clear-RecycleBin -Force -ErrorAction SilentlyContinue",
            detect=_detect_recycle),
        Fix(id="restart_explorer", title="Restart Windows Explorer",
            category="handles", risk="caution", reversible=True, shell="pwsh",
            command=("Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue; "
                     "Start-Sleep -Seconds 1; if (-not (Get-Process explorer -ErrorAction "
                     "SilentlyContinue)) { Start-Process explorer }"),
            detect=_detect_explorer),
        Fix(id="clear_standby", title="Clear standby (cached) memory list",
            category="memory", risk="manual", reversible=True, shell="pwsh", command="",
            detect=_detect_standby,
            manual_hint=("Needs elevation + Sysinternals RAMMap: `RAMMap.exe -Et`. The "
                         "unprivileged SysPerf service cannot elevate.")),
        Fix(id="trim_startup", title="Disable non-essential start-up programs",
            category="startup", risk="manual", reversible=True, shell="pwsh", command="",
            detect=_detect_startup,
            manual_hint=("Review in Task Manager -> Start-up, or disable a known Run-key "
                         "entry. Left manual so essential apps are never disabled.")),
        Fix(id="restart_wsl", title="Restart WSL to apply the .wslconfig cap",
            category="wsl", risk="manual", reversible=True, shell="bash", command="",
            detect=_detect_wsl, session_killing=True,
            manual_hint=("Run from Windows PowerShell/CMD: `wsl --shutdown`. NOT run "
                         "automatically - it would terminate SysPerf and its collector if "
                         "they run inside WSL.")),
        Fix(id="optimize_wsl_boot", title="Tune .wslconfig for fast WSL start",
            category="startup", risk="manual", reversible=True, shell="pwsh", persistent=True,
            command="", detect=_detect_wsl_boot,
            manual_hint=("Review and merge .wslconfig settings manually; preserve memory, processors "
                         "and networking settings. See Microsoft's WSL configuration documentation. "
                         "Restart WSL only after saving work in every distribution.")),
        Fix(id="enable_sysmain", title="Enable SysMain (SuperFetch) app pre-caching",
            category="startup", risk="manual", reversible=True, shell="pwsh", persistent=True,
            command="", detect=_detect_sysmain,
            manual_hint=("Run once as admin (or via scripts/optimize-startup.ps1): "
                         "`Set-Service SysMain -StartupType Automatic; Start-Service SysMain`. "
                         "Pre-caches apps; persists across reboots. Needs elevation.")),
        Fix(id="trim_office_addins", title="Disable slow Office (Outlook/Word) COM add-ins",
            category="launch", risk="manual", reversible=True, shell="pwsh", command="",
            detect=_detect_office,
            manual_hint=("In Outlook/Word -> File -> Options -> Add-ins -> COM Add-ins: uncheck "
                         "add-ins you do not need. Left manual so required corporate add-ins "
                         "are never removed automatically.")),
    ]


# --------------------------------------------------------------------------- #
# command runner
# --------------------------------------------------------------------------- #
def default_runner(shell: str, command: str, timeout: float = 60.0) -> tuple:
    """Execute a remediation command. Returns (returncode, combined_output)."""
    try:
        if shell == "pwsh":
            from .platform_win import powershell_exe
            argv = [powershell_exe(), "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-Command", command]
        elif shell == "bash":
            argv = ["/bin/bash", "-c", command]
        else:
            raise ValueError(f"unsupported shell: {shell}")
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        out = ((res.stdout or "") + (res.stderr or "")).strip()
        return res.returncode, out[:2000]
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
class FixEngine:
    def __init__(self, runner: Callable[[str, str], tuple] = default_runner,
                 registry: list | None = None) -> None:
        self.runner = runner
        self.registry = registry if registry is not None else build_registry()
        self._by_id = {f.id: f for f in self.registry}

    def detect(self, snapshot: dict) -> list:
        out = []
        for f in self.registry:
            reason = f.detect(snapshot)
            if not reason:
                continue
            out.append({
                "id": f.id, "title": f.title, "category": f.category, "risk": f.risk,
                "reversible": f.reversible, "reason": reason, "executable": f.executable,
                "session_killing": f.session_killing, "persistent": f.persistent,
                "manual_hint": f.manual_hint,
            })
        return out

    def _select(self, ids: list | None, snapshot: dict) -> list:
        if ids is not None:
            return [self._by_id[i] for i in dict.fromkeys(ids) if i in self._by_id]
        applicable = {d["id"] for d in self.detect(snapshot)}
        return [f for f in self.registry if f.id in applicable]

    def preview(self, ids: list | None, snapshot: dict) -> list:
        out = []
        for f in self._select(ids, snapshot):
            out.append({
                "id": f.id, "title": f.title, "risk": f.risk, "executable": f.executable,
                "would_run": (f.command if f.executable else None),
                "shell": f.shell if f.executable else None, "manual_hint": f.manual_hint,
            })
        return out

    def apply(self, ids: list | None, snapshot: dict) -> list:
        out = []
        for f in self._select(ids, snapshot):
            if not f.executable or (ids is None and f.risk != "safe"):
                out.append({"id": f.id, "title": f.title, "risk": f.risk,
                            "status": "skipped", "reason": "manual action required",
                            "manual_hint": f.manual_hint})
                continue
            rc, output = self.runner(f.shell, f.command)
            out.append({"id": f.id, "title": f.title, "risk": f.risk,
                        "status": "ok" if rc == 0 else "error",
                        "returncode": rc, "output": output})
        return out
