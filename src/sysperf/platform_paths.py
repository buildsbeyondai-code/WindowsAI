#!/usr/bin/env python3
"""
Platform detection, tool discovery and path resolution.

Until 2.8.0 the dashboard silently assumed it was running inside WSL 2: the
PowerShell candidates were all ``/mnt/c/...`` mount paths, the collector shelled
out to ``wslpath`` to translate its script path, and the data files were written
next to the installed package. On a native Windows install the installer would
succeed, the service would start, and the scorecard would never populate — the
worst kind of failure, because nothing reported an error.

Everything platform-specific now lives here, as **pure functions that take their
environment as arguments**. That is deliberate: it is the only way to test the
Windows branches from a Linux CI box, and untested platform code is exactly how
the previous assumption survived four releases.

    detect_platform()      -> "windows" | "wsl" | "linux"
    resolve_powershell()   -> path to pwsh/powershell, or None
    resolve_data_dir()     -> where state belongs on this platform
    to_host_path()         -> a path PowerShell on the Windows host can open
"""
from __future__ import annotations

import os
import shutil
import subprocess
from .background_process import run_hidden
from pathlib import Path

WINDOWS = "windows"
WSL = "wsl"
LINUX = "linux"

# Where a Windows host keeps the two PowerShell editions. PowerShell 7 is
# preferred: collect_win.ps1 runs its inventory probes as parallel thread jobs,
# which 5.1 cannot do.
_WIN_PS = (
    r"C:\Program Files\PowerShell\7\pwsh.exe",
    r"C:\Program Files (x86)\PowerShell\7\pwsh.exe",
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
)
# The same, reached from inside WSL through the /mnt/c mount. Windows PowerShell
# 5.1 is deliberately last: it cannot run collect_win.ps1's inventory probes as
# parallel thread jobs, so it is a working fallback rather than an equal choice.
_WSL_PS = (
    "/mnt/c/Program Files/PowerShell/7/pwsh.exe",
    "/mnt/c/Program Files (x86)/PowerShell/7/pwsh.exe",
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe",
)
# A Store/winget install of PowerShell 7 lands in the per-user WindowsApps
# directory, not Program Files. From WSL the user name is unknown, so it is found
# by glob — the previous release hardcoded one developer's name here, which both
# leaked it into an open-source archive and failed for everybody else.
_WSL_WINDOWSAPPS = "/mnt/c/Users/*/AppData/Local/Microsoft/WindowsApps/pwsh.exe"


def detect_platform(os_name: str | None = None,
                    proc_version: str | None = None) -> str:
    """Which of the three environments this process is in.

    ``proc_version`` is the contents of /proc/version, read by the caller so this
    stays a pure function. WSL is distinguished from plain Linux because the two
    need completely different tool paths: WSL reaches Windows through /mnt/c and
    the interop layer, plain Linux cannot reach it at all.
    """
    if (os_name or os.name) == "nt":
        return WINDOWS
    if proc_version is None:
        try:
            proc_version = Path("/proc/version").read_text(encoding="utf-8")
        except OSError:
            proc_version = ""
    return WSL if "microsoft" in proc_version.lower() else LINUX


def powershell_candidates(platform: str, env: dict | None = None,
                          which=shutil.which, glob=None) -> list:
    """Ordered PowerShell locations to try for this platform.

    An explicit override always wins, then PATH, then the well-known install
    locations. On plain Linux the list is empty: there is no Windows host to
    query, and pretending otherwise produced the silent failure this replaces.
    """
    env = os.environ if env is None else env
    override = env.get("PERFDASH_PS_EXE") or env.get("SYSPERF_PS_EXE") or ""
    out = [override] if override else []
    if platform == WINDOWS:
        out += [which("pwsh") or "", which("powershell") or ""]
        out += list(_WIN_PS)
        local = env.get("LOCALAPPDATA")
        if local:
            out.append(str(Path(local) / "Programs" / "PowerShell" / "7"
                           / "pwsh.exe"))
            # winget/Store installs land here rather than in Program Files.
            out.append(str(Path(local) / "Microsoft" / "WindowsApps" / "pwsh.exe"))
    elif platform == WSL:
        # Interop puts pwsh.exe/powershell.exe on PATH in most distros, but not in
        # a systemd service, whose PATH has no Windows entries — so the mount
        # paths are a normal case, not just a fallback for disabled interop.
        out += [which("pwsh.exe") or ""]
        finder = glob or _glob_paths
        out += list(_WSL_PS[:2]) + finder(_WSL_WINDOWSAPPS)
        out += [which("powershell.exe") or ""] + list(_WSL_PS[2:])
    return [p for p in out if p]


def _glob_paths(pattern: str) -> list:                # pragma: no cover - thin
    from glob import glob as _g
    return sorted(_g(pattern))


def resolve_powershell(platform: str | None = None, env: dict | None = None,
                       which=shutil.which, exists=os.path.exists,
                       glob=None) -> str | None:
    """The first PowerShell that actually exists, or None.

    Returning None rather than a hopeful default is the point: the collector can
    then say "no PowerShell on this platform" instead of failing once per sample
    with a confusing file-not-found.
    """
    platform = detect_platform() if platform is None else platform
    for candidate in powershell_candidates(platform, env, which, glob):
        # An entry that came from shutil.which is already known to exist, but a
        # bare name from the override is not, so everything is checked.
        if exists(candidate) or (os.sep not in candidate and which(candidate)):
            return candidate
    return None


def resolve_data_dir(platform: str | None = None, env: dict | None = None,
                     legacy: Path | None = None, home: Path | None = None) -> Path:
    """Where mutable state belongs.

    Precedence, in order:

    1. ``SYSPERF_DATA_DIR`` — an explicit choice always wins.
    2. ``legacy`` if it already holds state. An existing install keeps writing
       where it always has; a version bump must not orphan a user's history.
    3. The platform's per-user data location. Never the package directory:
       once pip-installed that is site-packages, which may be read-only, is wiped
       on upgrade, and should not accumulate a multi-megabyte history file.
    """
    env = os.environ if env is None else env
    platform = detect_platform() if platform is None else platform
    override = env.get("SYSPERF_DATA_DIR") or env.get("PERFDASH_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if legacy is not None and legacy_has_state(legacy):
        return legacy
    home = Path(home or Path.home())
    if platform == WINDOWS:
        root = Path(env.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        return root / "SysPerf" / "data"
    xdg = env.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else home / ".local" / "share"
    return root / "sysperf"


# Files whose presence marks a directory as an existing installation's data dir.
STATE_MARKERS = ("history.jsonl", "adaptive_state.json", "trends_daily.json",
                 "autoimprove_state.json")


def legacy_has_state(directory: Path) -> bool:
    """True when this directory already holds a previous run's state."""
    try:
        return any((directory / name).exists() for name in STATE_MARKERS)
    except OSError:                                  # pragma: no cover - defensive
        return False


def to_host_path(path, platform: str | None = None, run=run_hidden) -> str:
    """A path string PowerShell on the Windows host can open.

    On Windows the path is already native. Inside WSL a Linux path has to be
    translated through ``wslpath``, because pwsh.exe runs on the Windows side and
    cannot see ``/home/...``. On plain Linux there is no host, so the path is
    returned unchanged and the caller will find no PowerShell to use it.
    """
    platform = detect_platform() if platform is None else platform
    text = str(path)
    if platform != WSL:
        return text
    try:
        result = run(["wslpath", "-w", text], capture_output=True, text=True,
                     check=True)
        return result.stdout.strip() or text
    except (OSError, subprocess.SubprocessError):
        # Interop or wslpath missing. Returning the untranslated path lets the
        # collector report a real error instead of raising at import time, which
        # used to take the whole service down.
        return text


def describe(platform: str | None = None, env: dict | None = None) -> dict:
    """A summary for `sysperf --where` and the /api/health payload."""
    platform = detect_platform() if platform is None else platform
    shell = resolve_powershell(platform, env)
    return {
        "platform": platform,
        "powershell": shell,
        "can_collect": shell is not None,
        "why_not": None if shell else (
            "no PowerShell found — SysPerf measures a Windows host, so it needs "
            "pwsh.exe or powershell.exe. On plain Linux there is nothing to "
            "measure; inside WSL, enable interop or set SYSPERF_PS_EXE."),
    }
