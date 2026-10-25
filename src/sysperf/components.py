"""
Component-upgrade advisory.

Merges LIVE-measured current versions (Linux/WSL side via subprocess, Windows
driver/build versions passed in from the collector sample) with a maintainable
catalog (components.json) of recommended "latest" targets and per-component
performance-improvement scores. Read-only: every probe is a `--version`-style
query; nothing is installed or changed.

Linux-side versions are cached (they do not change while the service runs) so
the periodic full sweep stays cheap.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .background_process import run_hidden
from .config import BASE, PLATFORM

log = logging.getLogger("perfdash")

COMPONENTS_FILE = BASE / "components.json"
_CACHE_TTL = 3600.0
_cache: dict = {"t": 0.0, "linux": None}

# The systemd unit runs with a minimal PATH, so a `--version` probe can resolve
# an older system-wide binary (e.g. /usr/bin/kiro-cli 2.12.0) instead of the one
# the user actually runs (~/.local/bin/kiro-cli 2.19.1). Prepend the user-local
# bins so reported "current" matches the user's interactive environment.
_HOME = os.path.expanduser("~")
_ENV = {**os.environ,
        "PATH": os.pathsep.join([f"{_HOME}/.local/bin", f"{_HOME}/bin",
                                 os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")])}


def _run(cmd: list[str], timeout: float = 6.0) -> str:
    try:
        r = run_hidden(cmd, capture_output=True, text=True, timeout=timeout, env=_ENV)
        return ((r.stdout or "") + " " + (r.stderr or "")).strip()
    except Exception:
        return ""


def _ver(s: str | None) -> str:
    """Extract the first dotted version token (e.g. '1.24.0') from a string."""
    m = re.search(r"(\d+(?:\.\d+){1,3})", s or "")
    return m.group(1) if m else ""


def _cmp(cur: str, latest: str) -> int | None:
    """Numeric version compare: -1 cur<latest, 0 equal, 1 cur>latest, None if unknown."""
    ca = [int(x) for x in re.findall(r"\d+", cur or "")]
    la = [int(x) for x in re.findall(r"\d+", latest or "")]
    if not ca or not la:
        return None
    n = max(len(ca), len(la))
    ca += [0] * (n - len(ca))
    la += [0] * (n - len(la))
    return (ca > la) - (ca < la)


def _months_old(datestr: str | None) -> float | None:
    for fmt in ("%Y-%m-%d", "%m-%d-%Y", "%d-%m-%Y"):
        try:
            d = datetime.strptime((datestr or "")[:10], fmt).replace(tzinfo=timezone.utc)
            return round((datetime.now(timezone.utc) - d).days / 30.4, 1)
        except (ValueError, TypeError):
            continue
    return None


def _linux_versions() -> dict:
    now = time.time()
    if _cache["linux"] is not None and now - _cache["t"] < _CACHE_TTL:
        return _cache["linux"]
    v: dict = {}
    if PLATFORM == "windows":
        # The Linux-side catalog rows (distro, kernel, apt packages) describe a
        # WSL deployment. Probing the Windows PATH for them reports unrelated
        # binaries against apt channels, so only the WSL platform is checked.
        _cache["linux"] = _toolchain_versions(_wsl_version(v))
        _cache["t"] = now
        return _cache["linux"]
    try:
        for ln in Path("/etc/os-release").read_text().splitlines():
            if ln.startswith("VERSION_ID="):
                v["ubuntu"] = ln.split("=", 1)[1].strip().strip('"')
            elif ln.startswith("VERSION="):        # e.g. "24.04.3 LTS (Noble Numbat)" -> 24.04.3
                pv = _ver(ln.split("=", 1)[1])
                if pv:
                    v["ubuntu"] = pv
    except OSError:
        pass
    osr = _ver(_run(["lsb_release", "-ds"]))          # description carries the point release
    if osr:
        v["ubuntu"] = osr
    v["kernel"] = _ver(_run(["uname", "-r"]))
    v["python3"] = _ver(_run(["python3", "--version"]))
    v["node"] = _ver(_run(["node", "--version"]))
    v["nginx"] = _ver(_run(["nginx", "-v"]))
    v["docker"] = _ver(_run(["docker", "--version"]))
    v["gunicorn"] = _ver(_run(["gunicorn", "--version"]))
    v["git"] = _ver(_run(["git", "--version"]))
    v["npm"] = _ver(_run(["npm", "--version"]))
    v["kiro-cli"] = _ver(_run(["kiro-cli", "--version"]))
    # WSL app version: wsl.exe --version only prints, it does not start a distro.
    # wsl.exe lives on the Windows host: on PATH when running natively or with
    # WSL interop, at the mount path otherwise. Hardcoding /mnt/c meant this
    # probe could only ever work from inside WSL.
    _wsl_version(v)
    _cache["linux"] = _toolchain_versions(v)
    _cache["t"] = now
    return _cache["linux"]


def _toolchain_versions(v: dict) -> dict:
    """SysPerf's own toolchain: the interpreter and packages it runs on, the
    test tools behind the quality gates, and the WSL-side pieces self-evolution
    uses (Claude Code, the bubblewrap sandbox, the Ubuntu distro, Chromium)."""
    import platform
    from importlib import metadata
    v["python"] = platform.python_version()
    for dist in ("flask", "waitress", "pytest", "playwright"):
        try:
            v[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            pass
    if PLATFORM == "windows":
        v["git-windows"] = _ver(_run(["git", "--version"]))
        wsl_exe = shutil.which("wsl.exe") or shutil.which("wsl")
        if wsl_exe:
            probe = ('echo "bwrap=$(bwrap --version 2>/dev/null)"; '
                     'echo "claude=$("$HOME/.local/bin/claude" --version 2>/dev/null)"; '
                     'grep "^VERSION=" /etc/os-release | sed "s/^/ubuntu=/"; '
                     'echo "chromium=$(ls -d "$HOME"/.cache/ms-playwright/chromium-* 2>/dev/null | tail -1)"')
            out = _run([wsl_exe, "-e", "sh", "-c", probe], timeout=25).replace("\x00", "")
            for line in out.splitlines():
                key, _, val = line.strip().partition("=")
                if key == "chromium" and val:
                    v["chromium"] = "build " + val.rsplit("-", 1)[-1]
                elif key in ("bwrap", "claude", "ubuntu") and _ver(val):
                    v[{"bwrap": "bubblewrap", "claude": "claude-code"}.get(key, key)] = _ver(val)
    else:
        chrome = sorted(glob.glob(os.path.join(_HOME, ".cache", "ms-playwright", "chromium-*")))
        if chrome:
            v["chromium"] = "build " + chrome[-1].rsplit("-", 1)[-1]
        v["bubblewrap"] = _ver(_run(["bwrap", "--version"]))
        v["claude-code"] = _ver(_run([f"{_HOME}/.local/bin/claude", "--version"]))
    return {k: val for k, val in v.items() if val}


def _wsl_version(v: dict) -> dict:
    wsl_exe = (shutil.which("wsl.exe") or shutil.which("wsl")
               or "/mnt/c/WINDOWS/System32/wsl.exe")
    wsl = _run([wsl_exe, "--version"], timeout=8).replace("\x00", "")
    m = re.search(r"WSL version:\s*([\d.]+)", wsl)
    if m:
        v["wsl"] = m.group(1)
    return v


def build_components(latest: dict) -> dict:
    """Assemble the upgrade-advisory table from the catalog + live versions."""
    try:
        cat = json.loads(COMPONENTS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("components.json unreadable: %s", exc)
        return {"rows": [], "error": str(exc), "upgrades": 0, "total": 0}

    lv = _linux_versions()
    cur_map = dict(lv)
    cur_map["gpu-driver"] = latest.get("gpu_driver", "")
    cur_map["net-driver"] = latest.get("net_driver_ver", "")
    cur_map["windows"] = _ver(latest.get("win_build", ""))
    cur_map["powershell"] = latest.get("pwsh_version") or latest.get("ps_version", "")
    cur_map["edge"] = latest.get("edge_version", "")
    cur_map["webview2"] = latest.get("webview2_version", "")
    cur_map["bios"] = latest.get("bios_version", "")

    # Name hardware rows after the device actually fitted, not a catalog guess.
    live_names = {"gpu-driver": latest.get("gpu_name"),
                  "net-driver": latest.get("net_driver_name")}

    rows, uplift_sum, upg = [], 0, 0
    for c in cat.get("catalog", []):
        key = c.get("key", "")
        mode = c.get("compare", "version")
        # A component this machine does not have is not an upgrade candidate —
        # unless the catalog marks it as something SysPerf itself relies on, in
        # which case "not installed" is the useful thing to show.
        missing = not cur_map.get(key)
        if missing and not c.get("show_missing"):
            continue
        name = c.get("name", key)
        if live_names.get(key):
            name = f"{live_names[key]} driver"
        row = {
            "key": key, "name": name, "category": c.get("category", ""),
            "latest": c.get("latest", ""), "perf": c.get("perf", 0),
            "owner": c.get("owner", ""), "why": c.get("why", ""),
            "channel": c.get("channel", ""), "upstream": c.get("upstream", ""),
        }
        if missing:
            row.update(current="not installed", missing=True, outdated=False)
        elif mode == "date":
            row["current"] = cur_map.get(key, "") or "—"
            dt = latest.get(c.get("date_key", ""), "")
            mo = _months_old(dt)
            row["current_date"] = dt
            row["months_old"] = mo
            row["outdated"] = (mo is not None and mo >= c.get("stale_months", 9))
        else:
            cur = cur_map.get(key, "")
            row["current"] = cur or "—"
            cmp = _cmp(cur, c.get("latest", ""))
            row["outdated"] = (cmp is not None and cmp < 0)
        if row["outdated"]:
            upg += 1
            uplift_sum += row["perf"]
        rows.append(row)

    # Outdated first, then by performance benefit.
    rows.sort(key=lambda r: (not r["outdated"], -r["perf"]))
    avg_uplift = round(uplift_sum / upg) if upg else 0
    return {
        "rows": rows,
        "assessment_date": cat.get("assessment_date", ""),
        "note": cat.get("note", ""),
        "upgrades": upg,
        "total": len(rows),
        "avg_uplift": avg_uplift,
    }
