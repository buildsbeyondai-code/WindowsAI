"""
Common-Fix engine for the System Performance Dashboard.

The dashboard is read-only telemetry; this module is the ONE place that can act
on the machine, and it does so under tight, explicit constraints:

Safety model
------------
Every fix declares a ``risk`` class:

  * ``safe``    - non-disruptive, reversible, needs no elevation
                  (e.g. flush DNS, clear the user TEMP folder). Eligible for the
                  one-click "Common Fix" batch.
  * ``caution`` - reversible but user-visible (e.g. restart Explorer, empty the
                  Recycle Bin). Executed only on an explicit confirmed request.
  * ``manual``  - disruptive, session-affecting or needs elevation
                  (e.g. ``wsl --shutdown``, disabling start-up entries). These
                  are NEVER executed by the engine; the exact command is returned
                  for the user to run themselves.

``session_killing`` fixes (``wsl --shutdown`` restarts the very WSL instance this
service and the agent run in) are hard-blocked from execution regardless of
confirmation.

Detection is driven by the live scorecard (a criterion in ``regress``/``partial``
maps to its remediation) plus always-available maintenance fixes, so BOTH
recurring common issues and newly-appearing regressions surface automatically.

The command runner is injected, so execution is fully unit-testable without
touching the real OS.
"""
from __future__ import annotations

import os
from .background_process import run_hidden
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import appctl

# The processes these fixes must never touch. appctl.CRITICAL is imported rather
# than restated — a second copy in PowerShell would drift, and drift here means
# deprioritising a security agent or the window manager.
#
# appctl.CRITICAL answers "may I stop this?", which is not the same question as
# "may I deprioritise or trim this?". A dry run of the selection logic picked
# `pwsh` (the very process taking the measurement) as the top CPU consumer, and
# offered to trim `Memory Compression` (a kernel component, and the metric being
# optimised) and `vmmemWSL` (the VM this service runs inside). Hence these:
_EXTRA_DENY = {
    # the measuring process and its shell — deprioritising these throttles the
    # remediation itself and, in WSL, the dashboard with it
    "pwsh", "powershell", "conhost", "windowsterminal", "python", "python3",
    # kernel/VM pseudo-processes: trimming their working set achieves nothing and
    # starves the host. vmmemWSL *is* the environment this service runs in.
    "memory compression", "vmmem", "vmmemwsl", "vmmemwsl.exe",
    "system interrupts", "secure system", "lsaiso",
}
_DENY_PS = "@(" + ",".join(
    f"'{n}'" for n in sorted(appctl.CRITICAL | _EXTRA_DENY)) + ")"

try:                                    # config import is optional for unit tests
    from .config import PS_EXE
except Exception:                       # pragma: no cover - defensive only
    PS_EXE = "pwsh"

try:                                    # standby elevation-broker exchange paths
    from .config import (STANDBY_BROKER_DIR_WIN, STANDBY_BROKER_DIR_WSL,
                        STANDBY_BROKER_HEARTBEAT_MAX_S, STANDBY_BROKER_WAIT_S)
except Exception:                       # pragma: no cover - defensive only
    STANDBY_BROKER_DIR_WIN = r"C:\ProgramData\SysPerf\standby"
    STANDBY_BROKER_DIR_WSL = "/mnt/c/ProgramData/SysPerf/standby"
    STANDBY_BROKER_HEARTBEAT_MAX_S = 90.0
    STANDBY_BROKER_WAIT_S = 25.0

# risk classes eligible for execution via the API (manual is never executed)
EXECUTABLE_RISKS = ("safe", "caution")


@dataclass
class Fix:
    """One remediation definition."""
    id: str
    title: str
    category: str
    risk: str                                   # safe | caution | manual
    reversible: bool
    shell: str                                  # pwsh | bash
    command: str
    detect: Callable[[dict], Optional[str]]     # snapshot -> reason or None
    manual_hint: str = ""
    session_killing: bool = False
    persistent: bool = False                    # survives reboot once applied
    # Optional runtime gate. When present it is consulted (in addition to the
    # static risk class) to decide whether the engine may run this fix *right
    # now*. Used by clear_standby: it is a runnable "caution" fix only while its
    # elevated helper is online; otherwise it degrades to a manual action.
    runtime_gate: Optional[Callable[[], bool]] = None

    @property
    def executable(self) -> bool:
        """True if the engine is permitted to run this fix itself."""
        base = self.risk in EXECUTABLE_RISKS and not self.session_killing
        if base and self.runtime_gate is not None:
            try:
                return bool(self.runtime_gate())
            except Exception:               # pragma: no cover - gate must never raise
                return False
        return base


# --------------------------------------------------------------------------- #
# detection helpers                                                           #
# --------------------------------------------------------------------------- #
def _rows(snapshot: dict) -> dict:
    """Map criterion key -> row from the snapshot's scorecard."""
    return {r["key"]: r for r in snapshot.get("scorecard", {}).get("rows", [])}


def _needs(row: Optional[dict]) -> bool:
    """A row needs attention when it is regressed or only partially met."""
    return bool(row) and row.get("status") in ("regress", "partial")


def _latest(snapshot: dict) -> dict:
    return snapshot.get("latest", {}) or {}


# --------------------------------------------------------------------------- #
# per-fix detectors                                                           #
# --------------------------------------------------------------------------- #
def _detect_dns(snap: dict) -> Optional[str]:
    row = _rows(snap).get("net_remote")
    if _needs(row):
        return (f"Remote TCP connections at {row.get('current')} "
                f"(target {row.get('target')}) - flushing DNS clears stale "
                "resolver entries that keep chatty connections alive.")
    return "Routine maintenance: clear the DNS resolver cache."


def _detect_temp(snap: dict) -> Optional[str]:
    row = _rows(snap).get("c_free_gb")
    if _needs(row):
        return (f"C: free {row.get('current')} GB below target {row.get('target')} GB "
                "- clearing the user TEMP folder reclaims space safely.")
    return None


def _detect_recycle(snap: dict) -> Optional[str]:
    row = _rows(snap).get("c_free_gb")
    if _needs(row):
        return "C: free below target - emptying the Recycle Bin reclaims disk space."
    return None


def _detect_explorer(snap: dict) -> Optional[str]:
    row = _rows(snap).get("handles")
    if _needs(row):
        return (f"App handle count elevated ({row.get('current')}, target "
                f"{row.get('target')}) - restarting Explorer releases leaked "
                "shell handles without a reboot.")
    return None


def _detect_startup(snap: dict) -> Optional[str]:
    row = _rows(snap).get("startup_apps")
    if _needs(row):
        return (f"{row.get('current')} enabled start-up programs (target "
                f"{row.get('target')}) - trimming non-essential entries shortens "
                "boot and lowers idle memory.")
    return None


def _detect_wsl(snap: dict) -> Optional[str]:
    row = _rows(snap).get("vmmem_mb")
    wsl = _latest(snap).get("wsl") or {}
    if wsl.get("cap_active") is False:
        return ("The .wslconfig memory/CPU cap is written but NOT active - a WSL "
                "restart applies it.")
    if _needs(row):
        return (f"vmmemWSL at {row.get('current')} MB and not falling - a WSL "
                "restart resets the guest working set.")
    return None


def _detect_standby(snap: dict) -> Optional[str]:
    rows = _rows(snap)
    if _needs(rows.get("available_mb")) or _needs(rows.get("compression_mb")):
        return ("Available memory low / compression elevated - clearing the "
                "standby (cached) memory list frees physical RAM.")
    return None


def _detect_wsl_boot(snap: dict) -> Optional[str]:
    rows = _rows(snap)
    wsl = _latest(snap).get("wsl") or {}
    row = rows.get("wsl_start_s")
    if _needs(row):
        return (f"WSL cold-start ~{row.get('current')} s (target {row.get('target')} s) "
                "- tuning .wslconfig (autoMemoryReclaim, vmIdleTimeout, sparseVhd) "
                "makes the guest start and shrink faster. Persists across reboots.")
    if wsl.get("cap_active") is False:
        return ("The .wslconfig memory/CPU cap is not active - writing a tuned "
                ".wslconfig speeds every future WSL start. Persists across reboots.")
    return None


def _detect_sysmain(snap: dict) -> Optional[str]:
    rows = _rows(snap)
    slow = [k for k in ("explorer_launch_s", "outlook_launch_s", "word_launch_s",
                        "boot_time_s")
            if _needs(rows.get(k))]
    if slow:
        return ("App/boot launch is slow - ensuring SysMain (SuperFetch) is enabled "
                "pre-caches Explorer, Outlook, Word and other apps so they launch "
                "faster. Needs admin; persists across reboots.")
    return None


def _detect_office(snap: dict) -> Optional[str]:
    rows = _rows(snap)
    slow = [(k, rows[k]) for k in ("outlook_launch_s", "word_launch_s")
            if _needs(rows.get(k))]
    if slow:
        names = ", ".join(rows[k]["key"].split("_")[0].capitalize() for k, _ in slow)
        worst = max(r.get("current") or 0 for _, r in slow)
        return (f"Office app launch slow ({names}, up to ~{worst} s) - slow COM "
                "add-ins are the usual cause.")
    return None


# --------------------------------------------------------------------------- #
# registry                                                                    #


def _detect_cpu_hog(snap: dict) -> Optional[str]:
    """Fires when CPU or the run queue is short and a consumer is sustaining it."""
    rows = _rows(snap)
    short = [rows.get(k) for k in ("cpu_pct", "cpu_queue", "disk_active_pct")]
    if not any(_needs(r) for r in short):
        return None
    names = [str(r.get("label")) for r in short if _needs(r)]
    return (f"{', '.join(names)} short of target — a sustained consumer can be "
            "moved to background scheduling priority")


def _detect_working_set(snap: dict) -> Optional[str]:
    """Fires on memory pressure, where trimming working sets frees pages."""
    rows = _rows(snap)
    short = [rows.get(k) for k in ("committed_app_mb", "available_mb",
                                   "compression_mb")]
    if not any(_needs(r) for r in short):
        return None
    names = [str(r.get("label")) for r in short if _needs(r)]
    return (f"{', '.join(names)} short of target — trimming the largest working "
            "sets returns pages without stopping anything")


# --------------------------------------------------------------------------- #
def build_registry() -> list:
    """The ordered list of known fixes (severity-relevant order)."""
    return [
        Fix(id="flush_dns", title="Flush DNS resolver cache",
            category="network", risk="safe", reversible=True, shell="pwsh",
            command="ipconfig /flushdns",
            detect=_detect_dns),
        Fix(id="clear_temp", title="Clear user TEMP folder",
            category="storage", risk="safe", reversible=True, shell="pwsh",
            command=("Get-ChildItem -Path $env:TEMP -Recurse -Force "
                     "-ErrorAction SilentlyContinue | Remove-Item -Recurse -Force "
                     "-ErrorAction SilentlyContinue"),
            detect=_detect_temp),
        Fix(id="empty_recycle_bin", title="Empty the Recycle Bin",
            category="storage", risk="caution", reversible=False, shell="pwsh",
            command="Clear-RecycleBin -Force -ErrorAction SilentlyContinue",
            detect=_detect_recycle),
        Fix(id="restart_explorer", title="Restart Windows Explorer",
            category="handles", risk="caution", reversible=True, shell="pwsh",
            command=("Stop-Process -Name explorer -Force "
                     "-ErrorAction SilentlyContinue; Start-Sleep -Seconds 1; "
                     "if (-not (Get-Process explorer -ErrorAction SilentlyContinue)) "
                     "{ Start-Process explorer }"),
            detect=_detect_explorer),
        Fix(id="background_cpu_hog",
            title="Move the top sustained CPU consumer to background priority",
            category="cpu", risk="caution", reversible=True, shell="pwsh",
            command=(
                # Measured over a real interval: Process.CPU is total seconds since
                # start, so a long-lived idle process would otherwise always win.
                "$deny = " + _DENY_PS + "; "
                "$self = @($PID); "
                "try { $p = (Get-CimInstance Win32_Process -Filter "
                "\"ProcessId=$PID\").ParentProcessId; if ($p) { $self += $p } } "
                "catch { }; "
                "$a = @{}; Get-Process -ErrorAction SilentlyContinue | "
                "ForEach-Object { if ($deny -notcontains $_.ProcessName.ToLower() "
                "-and $self -notcontains $_.Id) { $a[$_.Id] = $_.CPU } }; "
                "Start-Sleep -Seconds 3; "
                "$best = $null; $delta = 0; "
                "Get-Process -ErrorAction SilentlyContinue | ForEach-Object { "
                "if ($a.ContainsKey($_.Id)) { $d = $_.CPU - $a[$_.Id]; "
                "if ($d -gt $delta) { $delta = $d; $best = $_ } } }; "
                "if (-not $best -or $delta -lt 0.5) "
                "{ Write-Output 'no sustained consumer above the floor'; exit 0 }; "
                "Add-Type -Namespace SP -Name Pri -MemberDefinition "
                "'[DllImport(\"kernel32.dll\", SetLastError=true)] "
                "public static extern bool SetPriorityClass(IntPtr h, uint f);'; "
                # 0x00100000 = PROCESS_MODE_BACKGROUND_BEGIN: lowers CPU *and* I/O
                # priority, which is why this also helps disk_active_pct.
                "$ok = [SP.Pri]::SetPriorityClass($best.Handle, 0x00100000); "
                "Write-Output (\"background mode on \" + $best.ProcessName + "
                "\" (pid \" + $best.Id + \", \" + [math]::Round($delta,2) + "
                "\"s CPU in 3s) -> \" + $ok)"),
            detect=_detect_cpu_hog,
            manual_hint=(
                "Reverse it by ending background mode: SetPriorityClass with "
                "0x00200000 (PROCESS_MODE_BACKGROUND_END), or simply restart the "
                "process. Nothing persists across a reboot."),
            ),
        Fix(id="trim_working_sets",
            title="Trim the largest application working sets",
            category="memory", risk="caution", reversible=True, shell="pwsh",
            command=(
                "$deny = " + _DENY_PS + "; "
                "Add-Type -Namespace SP -Name Ws -MemberDefinition "
                "'[DllImport(\"psapi.dll\", SetLastError=true)] "
                "public static extern bool EmptyWorkingSet(IntPtr h);'; "
                "$n = 0; $freed = 0; "
                "Get-Process -ErrorAction SilentlyContinue | Where-Object "
                "{ $deny -notcontains $_.ProcessName.ToLower() -and "
                "$_.WorkingSet64 -gt 200MB } | "
                "Sort-Object WorkingSet64 -Descending | Select-Object -First 5 | "
                "ForEach-Object { $before = $_.WorkingSet64; "
                "if ([SP.Ws]::EmptyWorkingSet($_.Handle)) { $_.Refresh(); "
                "$n++; $freed += ($before - $_.WorkingSet64) } }; "
                "Write-Output (\"trimmed \" + $n + \" working set(s), released \" "
                "+ [math]::Round($freed/1MB) + \" MB\")"),
            detect=_detect_working_set,
            manual_hint=(
                "Nothing to reverse: the pages move to the standby list and each "
                "application faults back whatever it still needs. Expect a brief "
                "slowdown in the trimmed applications on next use."),
            ),
        Fix(id="clear_standby", title="Clear standby (cached) memory list",
            category="memory", risk="caution", reversible=True, shell="pwsh",
            command=_CLEAR_STANDBY_CMD,
            detect=_detect_standby,
            runtime_gate=_broker_online,
            manual_hint=("Auto-cleared on demand when the memory scores are low "
                         "- but ONLY while the elevated helper is running: start "
                         "`standby_broker.ps1` 'as Administrator' once per session. "
                         "Without it this stays manual: clear the list yourself "
                         "with Sysinternals RAMMap `RAMMap.exe -Et`. The dashboard "
                         "service runs unprivileged and cannot elevate.")),
        Fix(id="trim_startup", title="Disable non-essential start-up programs",
            category="startup", risk="manual", reversible=True, shell="pwsh",
            command="",
            detect=_detect_startup,
            manual_hint=("Review in Task Manager -> Start-up, or disable a known "
                         "entry via the registry Run-key. Left manual so essential "
                         "apps are never disabled automatically.")),
        Fix(id="restart_wsl", title="Restart WSL to apply the .wslconfig cap",
            category="wsl", risk="manual", reversible=True, shell="bash",
            command="",
            detect=_detect_wsl, session_killing=True,
            manual_hint=("Run from a Windows PowerShell/CMD prompt: "
                         "`wsl --shutdown`. NOT run automatically - it would "
                         "terminate this dashboard, its collector and any running "
                         "agent inside WSL.")),
        # ---- startup & launch performance (persistent, survive reboot) ----
        Fix(id="optimize_wsl_boot", title="Tune .wslconfig for fast WSL start",
            category="startup", risk="safe", reversible=True, shell="pwsh",
            persistent=True,
            command=(
                # Sized to THIS machine: ~35% of RAM and half the logical
                # processors (a fixed 10 GB / 6 vCPU only fitted one laptop).
                "$cs=Get-CimInstance Win32_ComputerSystem; "
                "$gb=[math]::Max(2,[math]::Floor($cs.TotalPhysicalMemory/1GB*0.35)); "
                "$cpu=[math]::Max(2,[math]::Floor($cs.NumberOfLogicalProcessors/2)); "
                "$p=Join-Path $env:USERPROFILE '.wslconfig'; "
                "if(Test-Path $p){Copy-Item $p \"$p.bak\" -Force}; "
                "Set-Content -Path $p -Encoding ASCII -Value "
                "\"[wsl2]`nmemory=${gb}GB`nprocessors=$cpu`nautoMemoryReclaim=gradual"
                "`nvmIdleTimeout=60000`nsparseVhd=true\""),
            detect=_detect_wsl_boot,
            manual_hint=("Writes a tuned %USERPROFILE%\\.wslconfig (backing up any "
                         "existing one). Takes effect on the next `wsl --shutdown`; "
                         "after that, WSL starts and reclaims memory faster on every "
                         "boot with no further action.")),
        Fix(id="enable_sysmain", title="Enable SysMain (SuperFetch) app pre-caching",
            category="startup", risk="manual", reversible=True, shell="pwsh",
            persistent=True, command="",
            detect=_detect_sysmain,
            manual_hint=("Run once as admin (or via the scheduled optimize-startup.ps1): "
                         "`Set-Service SysMain -StartupType Automatic; "
                         "Start-Service SysMain`. Pre-caches Explorer/Outlook so they "
                         "launch faster; persists across reboots. Needs elevation, which "
                         "this unprivileged service does not have.")),
        Fix(id="trim_office_addins", title="Disable slow Office (Outlook/Word) COM add-ins",
            category="launch", risk="manual", reversible=True, shell="pwsh",
            command="",
            detect=_detect_office,
            manual_hint=("In Outlook/Word -> File -> Options -> Add-ins -> COM Add-ins: "
                         "uncheck add-ins you do not need (e.g. legacy archiving/CRM "
                         "plugins). Left manual so required corporate add-ins are never "
                         "removed automatically.")),
    ]


# --------------------------------------------------------------------------- #
# standby-clear elevation broker                                              #
# --------------------------------------------------------------------------- #
# Emptying the Windows standby (cached) list needs Administrator, but this
# service runs unprivileged (systemd NoNewPrivileges=yes) and cannot elevate.
# standby_broker.ps1 — a tiny watcher started once "as Administrator" — performs
# the clear on demand. The loop drops a request ONLY when the memory scores are
# low (see _detect_standby), so it fires exactly when needed, never on a timer.
# When the watcher is not running, clear_standby stays a manual action (the
# RAMMap recipe), so nothing regresses.
def _broker_online() -> bool:
    """True when the elevated standby watcher has posted a fresh heartbeat.

    Used as clear_standby's runtime gate so the fix is engine-runnable only while
    the helper is live; otherwise it degrades to a manual action instead of
    failing every cycle.
    """
    hb = os.path.join(STANDBY_BROKER_DIR_WSL, "broker.alive")
    try:
        return (time.time() - os.path.getmtime(hb)) <= STANDBY_BROKER_HEARTBEAT_MAX_S
    except OSError:
        return False


def _build_clear_standby_cmd() -> str:
    """PowerShell run (unprivileged) on the Windows host by the loop. It only
    hands work to the elevated watcher via a request/result file exchange and
    waits for the verdict — it never tries to clear the list itself."""
    template = r"""
$ErrorActionPreference='Stop'
$d='@DIR@'
$hb=Join-Path $d 'broker.alive'
if(-not (Test-Path $hb) -or ((Get-Date)-(Get-Item $hb).LastWriteTime).TotalSeconds -gt @HB@){
  Write-Output 'standby-broker offline - not clearing (start standby_broker.ps1 as Administrator to enable auto-clear)'; exit 1 }
$id=[guid]::NewGuid().ToString('N')
$req=Join-Path $d ($id+'.request'); $res=Join-Path $d ($id+'.result')
Set-Content -LiteralPath $req -Value ('standbylist '+(Get-Date -Format o)) -Encoding ASCII
$deadline=(Get-Date).AddSeconds(@WAIT@)
while((Get-Date) -lt $deadline){
  if(Test-Path $res){
    $o=(Get-Content -LiteralPath $res -Raw).Trim()
    Remove-Item -LiteralPath $res -Force -ErrorAction SilentlyContinue
    Write-Output $o
    if($o -like 'OK*'){exit 0} else {exit 1} }
  Start-Sleep -Milliseconds 500 }
Remove-Item -LiteralPath $req -Force -ErrorAction SilentlyContinue
Write-Output 'standby-broker did not respond in time'; exit 1
""".strip()
    return (template.replace("@DIR@", STANDBY_BROKER_DIR_WIN)
                    .replace("@HB@", str(int(STANDBY_BROKER_HEARTBEAT_MAX_S)))
                    .replace("@WAIT@", str(int(STANDBY_BROKER_WAIT_S))))


_CLEAR_STANDBY_CMD = _build_clear_standby_cmd()


# --------------------------------------------------------------------------- #
# command runner                                                              #
# --------------------------------------------------------------------------- #
def default_runner(shell: str, command: str, timeout: float = 60.0) -> tuple:
    """Execute a remediation command. Returns (returncode, combined_output)."""
    if shell == "pwsh":
        argv = [PS_EXE, "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        argv = ["/bin/bash", "-c", command]
    try:
        res = run_hidden(argv, capture_output=True, text=True, timeout=timeout)
        out = ((res.stdout or "") + (res.stderr or "")).strip()
        return res.returncode, out[:2000]
    except Exception as exc:                    # noqa: BLE001 - report any failure
        return 1, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# engine                                                                      #
# --------------------------------------------------------------------------- #
class FixEngine:
    def __init__(self, runner: Callable[[str, str], tuple] = default_runner,
                 registry: Optional[list] = None) -> None:
        self.runner = runner
        self.registry = registry if registry is not None else build_registry()
        self._by_id = {f.id: f for f in self.registry}

    # ----------------------------------------------------------- registration
    def register(self, fix) -> bool:
        """Add a Fix at runtime. Used to make a human-promoted, evolution-generated
        remediation visible to detection and planning.

        Returns False rather than raising when the id already exists: promotion is
        idempotent and re-registering on service start must be a no-op.
        """
        if fix.id in self._by_id:
            return False
        self.registry.append(fix)
        self._by_id[fix.id] = fix
        return True

    # ------------------------------------------------------------- detect ---
    def get(self, fix_id: str):
        """The Fix with this id, or None. Used by the auto-improve planner."""
        return self._by_id.get(fix_id)

    def detect(self, snapshot: dict) -> list:
        """Return applicable fixes for the current snapshot (read-only)."""
        out = []
        for f in self.registry:
            reason = f.detect(snapshot)
            if not reason:
                continue
            out.append({
                "id": f.id, "title": f.title, "category": f.category,
                "risk": f.risk, "reversible": f.reversible,
                "reason": reason, "executable": f.executable,
                "session_killing": f.session_killing,
                "persistent": f.persistent,
                "manual_hint": f.manual_hint,
            })
        return out

    def _select(self, ids: Optional[list], snapshot: dict) -> list:
        """Resolve requested ids to Fix objects, defaulting to all applicable."""
        if ids:
            return [self._by_id[i] for i in ids if i in self._by_id]
        applicable = {d["id"] for d in self.detect(snapshot)}
        return [f for f in self.registry if f.id in applicable]

    # ------------------------------------------------------------ preview ---
    def preview(self, ids: Optional[list], snapshot: dict) -> list:
        """Dry-run: describe exactly what WOULD run. Never executes anything."""
        out = []
        for f in self._select(ids, snapshot):
            out.append({
                "id": f.id, "title": f.title, "risk": f.risk,
                "executable": f.executable,
                "would_run": (f.command if f.executable else None),
                "shell": f.shell if f.executable else None,
                "manual_hint": f.manual_hint,
            })
        return out

    # -------------------------------------------------------------- apply ---
    def apply(self, ids: Optional[list], snapshot: dict) -> list:
        """Execute eligible fixes. Manual/session-killing fixes are skipped."""
        out = []
        for f in self._select(ids, snapshot):
            if not f.executable:
                out.append({
                    "id": f.id, "title": f.title, "risk": f.risk,
                    "status": "skipped", "reason": "manual action required",
                    "manual_hint": f.manual_hint,
                })
                continue
            rc, output = self.runner(f.shell, f.command)
            out.append({
                "id": f.id, "title": f.title, "risk": f.risk,
                "status": "ok" if rc == 0 else "error",
                "returncode": rc, "output": output,
            })
        return out
