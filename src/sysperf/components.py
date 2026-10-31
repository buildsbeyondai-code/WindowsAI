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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .background_process import run_hidden
from .config import BASE, DATA_DIR, PLATFORM, lock_packages

log = logging.getLogger("perfdash")

COMPONENTS_FILE = BASE / "components.json"
# Newest public versions, refreshed daily by version_check.LatestVersions.
LATEST_FILE = DATA_DIR / "components_latest.json"
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

# The Linux-side catalog rows: the tools of a WSL deployment, probed where they
# live (directly on Linux/WSL, through wsl.exe when SysPerf runs on Windows).
_LINUX_TOOLS = {
    "kernel": "uname -r", "python3": "python3 --version",
    "docker": "docker --version", "git": "git --version",
}


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


_refresh_lock = threading.Lock()


def _linux_versions() -> dict:
    """Installed versions; the cached ones while a stale cache refreshes.

    The probe shells out to wsl.exe and can take tens of seconds when WSL is
    busy. It used to run on whichever web request found the hourly cache
    expired, and every request arriving meanwhile ran it again ("Task queue
    depth" warnings, 5.4.4 live). Now only the first fill blocks; a stale
    cache is served while one background thread refreshes it.
    """
    if _cache["linux"] is not None:
        if time.time() - _cache["t"] >= _CACHE_TTL and _refresh_lock.acquire(blocking=False):
            threading.Thread(target=_refresh_linux, name="components-refresh",
                             daemon=True).start()
        return _cache["linux"]
    with _refresh_lock:
        if _cache["linux"] is None:
            _probe_linux()
    return _cache["linux"]


def _refresh_linux() -> None:
    try:
        _probe_linux()
    except Exception:
        log.exception("component version refresh failed")
    finally:
        _refresh_lock.release()


def _probe_linux() -> dict:
    now = time.time()
    v: dict = {}
    if PLATFORM == "windows":
        # The Linux-side catalog rows (distro, kernel, apt packages) describe a
        # WSL deployment. Probing the Windows PATH for them would report
        # unrelated binaries against apt channels, so they are read inside the
        # distro by _toolchain_versions' wsl.exe probe instead.
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
    for key, cmd in _LINUX_TOOLS.items():
        v[key] = _ver(_run(cmd.split()))
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
    # Every runtime package in the CI SBOM (requirements.lock, plus pip).
    for dist in sorted({"flask", "waitress", "werkzeug", "jinja2", "markupsafe", "itsdangerous",
                        "click", "blinker", "pip", "pytest", "playwright", *lock_packages()}):
        try:
            v[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            pass
    if PLATFORM == "windows":
        v["git-windows"] = _ver(_run(["git", "--version"]))
        wsl_exe = shutil.which("wsl.exe") or shutil.which("wsl")
        if wsl_exe:
            # A non-interactive shell skips ~/.bashrc, so add the user bins by
            # hand, as _ENV does for a direct probe.
            probe = ('PATH="$HOME/.local/bin:$HOME/bin:$PATH"; '
                     'echo "bwrap=$(bwrap --version 2>/dev/null)"; '
                     'echo "claude=$("$HOME/.local/bin/claude" --version 2>/dev/null)"; '
                     'grep "^VERSION=" /etc/os-release | sed "s/^/ubuntu=/"; '
                     'echo "chromium=$(ls -d "$HOME"/.cache/ms-playwright/chromium-* 2>/dev/null | tail -1)"; '
                     + "".join(f'echo "{k}=$(command -v {c.split()[0]} >/dev/null && {c} 2>&1 | head -1)"; '
                               for k, c in _LINUX_TOOLS.items()))
            out = _run([wsl_exe, "-e", "sh", "-c", probe], timeout=25).replace("\x00", "")
            for line in out.splitlines():
                key, _, val = line.strip().partition("=")
                if key == "chromium" and val:
                    v["chromium"] = "build " + val.rsplit("-", 1)[-1]
                elif key in ("bwrap", "claude", "ubuntu", *_LINUX_TOOLS) and _ver(val):
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


def _live_latest() -> dict:
    try:
        data = json.loads(LATEST_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _local_date(ts) -> str:
    """YYYY-MM-DD of a Unix time in this machine's zone; "" when not a time."""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def effective_latest(catalog: str, live: str, current: str) -> tuple[str, str]:
    """(target, source) for a version row.

    The vendor's live release feed wins over the hand-reviewed catalog, which
    goes stale. An installed version newer than both (a preview or Insider
    build) is itself the target: "current" can never read higher than "latest".
    """
    target, source = (live, "live") if live else (catalog, "catalog")
    if current and _cmp(current, target) == 1:
        return current, "installed"
    return target, source


def catalog(cat: dict, lock=None) -> list:
    """The reviewed catalog, plus a row for every locked package it lacks.

    An upgrade that brings in a new dependency (5.4.4) adds it to the lock; it
    then shows here, is checked against PyPI daily and is upgraded like the rest.
    """
    rows = list(cat.get("catalog", []))
    keys = {c.get("key") for c in rows}
    for name in lock_packages() if lock is None else lock:
        if name not in keys:
            rows.append({"upgrade": {"via": "pip-lock"}, "key": name, "name": name,
                         "category": "SysPerf toolchain", "compare": "version", "latest": "",
                         "channel": "pip (requirements.lock)", "perf": 1, "owner": "user",
                         "why": "A dependency of SysPerf's pinned packages, added to the "
                                "lock by the resolver; upgraded with them."})
    return rows


def build_components(latest: dict) -> dict:
    """Assemble the upgrade-advisory table from the catalog + live versions."""
    try:
        cat = json.loads(COMPONENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("components.json unreadable: %s", exc)
        return {"rows": [], "error": str(exc), "upgrades": 0, "total": 0}

    lv = _linux_versions()
    live = _live_latest()
    live_latest = live.get("latest") or {}
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
    for c in catalog(cat):
        key = c.get("key", "")
        mode = c.get("compare", "version")
        # A component this machine does not have is listed as "not installed"
        # (hiding it read as if the dashboard did not know about it), but it is
        # never an upgrade candidate and does not count towards the total.
        missing = not cur_map.get(key)
        name = c.get("name", key)
        if live_names.get(key):
            name = f"{live_names[key]} driver"
        row = {
            "key": key, "name": name, "category": c.get("category", ""),
            "latest": c.get("latest", ""), "perf": c.get("perf", 0),
            "owner": c.get("owner", ""), "why": c.get("why", ""),
            "optional": bool(c.get("optional")),
            "channel": c.get("channel", ""), "upstream": c.get("upstream", ""),
            "upgrade": c.get("upgrade") or {},
        }
        if missing:
            row.update(current="not installed", missing=True, outdated=False)
            row["latest"], row["latest_source"] = effective_latest(
                c.get("latest", ""), live_latest.get(key, ""), "")
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
            row["latest"], row["latest_source"] = effective_latest(
                c.get("latest", ""), live_latest.get(key, ""), cur)
            cmp = _cmp(cur, row["latest"])
            row["outdated"] = (cmp is not None and cmp < 0)
        rows.append(row)

    # A component that ships inside another (the WSL kernel in WSL) cannot be
    # upgraded while its carrier is current: the newer build is not released
    # in it yet. It waits for the carrier rather than reading as an upgrade.
    by_key = {r["key"]: r for r in rows}
    for row in rows:
        carrier = by_key.get(row["upgrade"].get("of", "")) \
            if row["upgrade"].get("via") == "follows" else None
        if row["outdated"] and carrier and not carrier.get("missing") \
                and not carrier["outdated"]:
            row.update(outdated=False, waiting_on=carrier["name"])
    for row in rows:
        if row["outdated"]:
            upg += 1
            uplift_sum += row["perf"]

    # Outdated first, then by performance benefit; not-installed rows last.
    rows.sort(key=lambda r: (r.get("missing", False), not r["outdated"], -r["perf"]))
    avg_uplift = round(uplift_sum / upg) if upg else 0
    return {
        "rows": rows,
        # The assessment is as fresh as its last live re-check; the catalog date is
        # only when the hand-reviewed fallbacks were last edited.
        "assessment_date": max(cat.get("assessment_date", ""), _local_date(live.get("checked_at"))),
        "catalog_date": cat.get("assessment_date", ""),
        "latest_checked_at": live.get("checked_at"),
        "note": cat.get("note", ""),
        "upgrades": upg,
        "total": sum(not r.get("missing") for r in rows),
        "not_installed": sum(bool(r.get("missing")) for r in rows),
        "avg_uplift": avg_uplift,
    }
