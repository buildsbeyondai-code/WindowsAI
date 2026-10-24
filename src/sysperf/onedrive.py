"""
OneDrive process control for the System Performance Dashboard.

OneDrive.exe runs continuously in the background and, on this host, is one of the
applications that use CPU *consistently* (it shows up in the Auto-Improve tab's
consumer table); OneDrive-backed paths also add file-sync and Files-On-Demand
overhead. This module lets the user stop and start it from the Auto-Improve tab
and — when ``ONEDRIVE_DEFAULT_STOPPED`` is set — stops it once at service start
so it is *not running by default*.

Design
------
* Read-only status comes from the collected sample (``onedrive_running``), so the
  UI shows the current state without spawning a process on every poll.
* The mutating actions go through the same injected command runner the fix engine
  uses (``fixes.default_runner``), so they are fully unit-testable and never touch
  the real OS in tests.
* Actions are reversible (stop ⇄ start) and honour ``FIX_ENABLED`` — the global
  read-only kill switch — exactly like the fix engine.
"""
from __future__ import annotations

from typing import Callable

# Stop every OneDrive.exe for the current user. Force + SilentlyContinue so it is
# a no-op (rc 0) when OneDrive is already stopped.
PS_STOP = ("Stop-Process -Name OneDrive -Force -ErrorAction SilentlyContinue; "
           "$null = $?")

# Start OneDrive in the background (its normal autostart mode). It installs per-user (LOCALAPPDATA) or per-machine (Program Files);
# the per-machine copy used to be missed, so the lock policy could never start it.
PS_START = (
    "$e = @((Join-Path $env:LOCALAPPDATA 'Microsoft\\OneDrive\\OneDrive.exe'), "
    "(Join-Path $env:ProgramFiles 'Microsoft OneDrive\\OneDrive.exe'), "
    "(Join-Path ${env:ProgramFiles(x86)} 'Microsoft OneDrive\\OneDrive.exe')) | "
    "Where-Object { Test-Path $_ } | Select-Object -First 1; "
    "if ($e) { Start-Process -FilePath $e -ArgumentList '/background' } "
    "else { Start-Process 'OneDrive.exe' -ArgumentList '/background' }")

ACTIONS = ("start", "stop")


def status(latest: dict) -> dict:
    """Current OneDrive run state from the latest sample.

    ``running`` is None (``known`` False) until a sample collected with the
    ``onedrive_running`` field arrives (older history predates it).
    """
    running = (latest or {}).get("onedrive_running")
    return {"running": running, "known": running is not None}


def apply(runner: Callable[[str, str], tuple], action: str) -> dict:
    """Run start/stop via the injected runner. Raises ValueError on bad action."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    command = PS_STOP if action == "stop" else PS_START
    rc, output = runner("pwsh", command)
    return {"action": action, "status": "ok" if rc == 0 else "error",
            "returncode": rc, "output": output}


def enforce_default(runner: Callable[[str, str], tuple], *,
                    enabled: bool = True, fix_enabled: bool = True) -> dict | None:
    """Stop OneDrive once at startup so it is not running by default.

    Returns the action result, or None when the policy is off or host execution
    is globally disabled (``FIX_ENABLED`` false) — in which case nothing runs.
    """
    if not (enabled and fix_enabled):
        return None
    return apply(runner, "stop")
