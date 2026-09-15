"""
Component-upgrade advisory.

Builds a version-currency table for the components actually present on this
machine. Windows-side versions (PowerShell, Edge, WebView2, GPU / network
driver, BIOS, Windows build) come from the collector sample; cross-platform
runtimes (Python, Node, Git, WSL) are probed locally and included only when
found — so the table is correct on any Windows 11 machine whether or not it has
developer tooling.

``latest`` is a reviewed reference target, not a live registry query. Driver and
firmware freshness is judged by age. Everything is read-only.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

_CACHE_TTL = 3600.0
_cache: dict = {"t": 0.0, "tools": None}

ASSESSMENT_DATE = None
NOTE = ("Installed versions are measured locally. Latest versions are not queried; "
        "check the vendor or managed update channel. Driver age is informational. "
        "Performance gains require benchmarks and are not estimated here.")

# key -> (label, category, latest, channel, perf, owner, why[, upstream])
CATALOG = {
    "windows":    ("Windows build", "OS", "10.0.26100", "Windows Update", 8, "IT",
                   "Quality/feature updates carry scheduler, memory-compression and driver fixes."),
    "powershell": ("PowerShell", "Tooling", "7.5.0", "winget install Microsoft.PowerShell", 15, "user",
                   "PowerShell 7 (pwsh) starts and parses far faster than the frozen 5.1; install alongside it."),
    "edge":       ("Microsoft Edge", "Browser", "128.x (evergreen)", "auto-update", 15, "user",
                   "Edge + WebView2 dominate process/handle counts; it auto-updates."),
    "webview2":   ("WebView2 Runtime", "Browser", "128.x (evergreen)", "auto-update", 12, "user",
                   "Backs many app UIs (Teams, Outlook); the evergreen runtime self-updates."),
    "python":     ("Python runtime", "Runtime", "3.12.7", "python.org / apt", 8, "user",
                   "Patch releases include interpreter performance and security fixes."),
    "node":       ("Node.js runtime", "Runtime", "22.0.0", "nodejs.org / NodeSource", 18, "user",
                   "Node 22 LTS ships a newer V8 with faster startup, JIT and GC."),
    "git":        ("Git", "Tooling", "2.45.0", "git-scm.com / apt", 4, "user",
                   "Newer Git improves large-repo status/checkout performance."),
    "wsl":        ("WSL 2 platform", "Platform", "2.2.0", "wsl --update", 20, "user",
                   "WSL updates carry autoMemoryReclaim and networking-mode improvements affecting vmmem."),
    "gpu-driver": ("Graphics driver", "Driver", "vendor-latest", "GPU vendor / OEM tool", 22, "IT / user",
                   "Newer GPU drivers fix boot stalls and improve compositor and WebView2 rendering."),
    "net-driver": ("Network adapter driver", "Driver", "vendor-latest", "OEM / vendor tool", 20, "IT / user",
                   "A current NIC/Wi-Fi driver improves throughput, roaming and power, and fixes latency spikes."),
    "bios":       ("UEFI / BIOS firmware", "Firmware", "vendor-latest", "OEM support tool", 12, "IT",
                   "Firmware updates improve power/thermal management and ship microcode and security fixes."),
}
DATE_KEYS = {"gpu-driver": "gpu_driver_date", "net-driver": "net_driver_date", "bios": "bios_date"}
STALE_MONTHS = 9


def _ver(s):
    m = re.search(r"(\d+(?:\.\d+){1,3})", str(s or ""))
    return m.group(1) if m else ""


def _cmp(cur, latest):
    ca = [int(x) for x in re.findall(r"\d+", str(cur or ""))]
    la = [int(x) for x in re.findall(r"\d+", str(latest or ""))]
    if not ca or not la:
        return None
    n = max(len(ca), len(la))
    ca += [0] * (n - len(ca))
    la += [0] * (n - len(la))
    return (ca > la) - (ca < la)


def _months_old(datestr):
    for fmt in ("%Y-%m-%d", "%m-%d-%Y", "%d-%m-%Y"):
        try:
            d = datetime.strptime(str(datestr)[:10], fmt).replace(tzinfo=timezone.utc)
            return round((datetime.now(timezone.utc) - d).days / 30.4, 1)
        except (ValueError, TypeError):
            continue
    return None


def _run(cmd, timeout=6):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return ((r.stdout or "") + " " + (r.stderr or "")).strip()
    except Exception:
        return ""


def _detect_tools() -> dict:
    """Locally-installed cross-platform tool versions (cached; only if present)."""
    now = time.time()
    if _cache["tools"] is not None and now - _cache["t"] < _CACHE_TTL:
        return _cache["tools"]
    out: dict = {"python": platform.python_version()}
    for key, exe in (("node", "node"), ("git", "git")):
        path = shutil.which(exe) or (shutil.which("python3") if exe == "python" else None)
        if path:
            v = _ver(_run([path, "--version"]))
            if v:
                out[key] = v
    _cache["tools"] = out
    _cache["t"] = now
    return out


def build_components(latest: dict) -> dict:
    latest = latest or {}
    tools = _detect_tools()

    current = {
        "windows": _ver(latest.get("os_version") or latest.get("win_build")),
        "powershell": latest.get("pwsh_version") or latest.get("ps_version") or "",
        "edge": latest.get("edge_version") or "",
        "webview2": latest.get("webview2_version") or "",
        "gpu-driver": latest.get("gpu_driver") or "",
        "net-driver": latest.get("net_driver_ver") or "",
        "bios": latest.get("bios_version") or "",
        "python": tools.get("python", ""),
        "node": tools.get("node", ""),
        "git": tools.get("git", ""),
        "wsl": ("installed" if latest.get("wsl_installed") else ""),
    }

    rows = []
    for key, (label, cat, _reference, channel, _perf, owner, why) in CATALOG.items():
        cur = current.get(key, "")
        if not cur:
            continue
        row = {"key": key, "name": label, "category": cat, "current": cur,
               "latest": "Check vendor", "channel": channel, "perf": None,
               "owner": owner, "why": why, "outdated": None}
        if key in DATE_KEYS:
            dt = latest.get(DATE_KEYS[key], "")
            row["current_date"] = dt
            row["months_old"] = _months_old(dt)
        rows.append(row)
    return {"rows": rows, "assessment_date": ASSESSMENT_DATE, "note": NOTE,
            "upgrades": 0, "total": len(rows), "avg_uplift": None}
