"""
Platform detection and the PowerShell bridge.

SysPerf measures a Windows host. It supports two runtimes:

  * **native Windows** (recommended) — Python runs on Windows 11 and invokes
    ``powershell.exe`` directly;
  * **WSL 2** — Python runs inside a WSL distribution and invokes the Windows
    ``powershell.exe`` through interop, translating script paths with ``wslpath``.

Everything platform-specific lives here so the rest of the package stays clean.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

__all__ = [
    "UnsupportedPlatformError",
    "is_windows",
    "is_wsl",
    "is_supported",
    "runtime_name",
    "powershell_exe",
    "to_windows_path",
    "run_powershell",
]

# Windows PowerShell 5.1 ships with every Windows 10/11 install, so it is the
# most reliable interpreter to target. pwsh (7+) is used only if 5.1 is absent.
_WIN_PS_RELATIVE = r"System32\WindowsPowerShell\v1.0\powershell.exe"
_WSL_PS_CANDIDATES = (
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe",
    "/mnt/c/windows/system32/WindowsPowerShell/v1.0/powershell.exe",
)


class UnsupportedPlatformError(RuntimeError):
    """Raised when SysPerf cannot reach a Windows PowerShell interpreter."""


@lru_cache(maxsize=1)
def is_windows() -> bool:
    """True when running on native Windows (CPython on nt)."""
    return os.name == "nt"


@lru_cache(maxsize=1)
def is_wsl() -> bool:
    """True when running inside a WSL distribution."""
    if os.name == "nt":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="ignore").lower()
    except OSError:
        return False


def is_supported() -> bool:
    """True when this runtime can collect Windows metrics."""
    return is_windows() or is_wsl()


def runtime_name() -> str:
    if is_windows():
        return "windows"
    if is_wsl():
        return "wsl"
    return "unsupported"


@lru_cache(maxsize=1)
def powershell_exe() -> str:
    """
    Absolute path (or PATH name) of the PowerShell interpreter to use.

    Override with ``SYSPERF_PS_EXE`` when PowerShell lives somewhere unusual
    (custom Windows install root, or a WSL distro with a non-default C: mount).
    """
    override = os.environ.get("SYSPERF_PS_EXE")
    if override:
        return override

    if is_windows():
        root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
        candidate = Path(root) / _WIN_PS_RELATIVE
        if candidate.is_file():
            return str(candidate)
        for name in ("powershell.exe", "pwsh.exe"):
            found = shutil.which(name)
            if found:
                return found
        raise UnsupportedPlatformError(
            "powershell.exe was not found. Set SYSPERF_PS_EXE to its full path."
        )

    if is_wsl():
        for candidate in _WSL_PS_CANDIDATES:
            if Path(candidate).is_file():
                return candidate
        # WSL interop normally exposes the Windows PATH inside the distro.
        for name in ("powershell.exe", "pwsh.exe"):
            found = shutil.which(name)
            if found:
                return found
        raise UnsupportedPlatformError(
            "powershell.exe was not reachable from WSL. Check that interop is "
            "enabled (/etc/wsl.conf: [interop] enabled=true) and that C: is "
            "mounted, or set SYSPERF_PS_EXE."
        )

    raise UnsupportedPlatformError(
        "SysPerf measures a Windows host. Run it on Windows 11 (recommended) "
        f"or inside WSL 2 — detected runtime: {os.name}/{sys.platform}."
    )


def to_windows_path(path: Path | str) -> str:
    """
    Render a local path in a form ``powershell.exe`` understands.

    On native Windows that is the path itself; from WSL it must be translated
    to its ``\\\\wsl$\\...`` or drive-letter equivalent via ``wslpath -w``.
    """
    path = Path(path)
    if is_windows():
        return str(path)
    if is_wsl():
        try:
            res = subprocess.run(
                ["wslpath", "-w", str(path)],
                capture_output=True, text=True, check=True, timeout=15,
            )
            return res.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise UnsupportedPlatformError(f"wslpath failed for {path}: {exc}") from exc
    raise UnsupportedPlatformError("path translation is only defined on Windows/WSL")


def run_powershell(script: Path | str, args: list[str] | None = None,
                   timeout: int = 120) -> str:
    """
    Execute a PowerShell *file* and return its stdout.

    The script path is fixed by the caller and never derived from HTTP input, so
    there is no injection surface. ``-NoProfile`` keeps user profiles out of the
    measurement, and ``-ExecutionPolicy Bypass`` applies to this process only —
    it does not change the machine policy.
    """
    cmd = [
        powershell_exe(), "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-File", to_windows_path(script),
    ]
    cmd.extend(args or [])
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"PowerShell exited with {res.returncode}: {(res.stderr or res.stdout or '')[:300]}")
    out = (res.stdout or "").replace("\r", "").strip()
    if not out:
        raise RuntimeError((res.stderr or "").strip()[:300] or "collector produced no output")
    return out
