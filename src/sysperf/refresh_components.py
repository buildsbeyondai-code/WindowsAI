#!/usr/bin/env python3
"""
Daily refresh of the component-upgrade advisory catalog (components.json).

The dashboard's #details/components view shows:
  * "current"  -> measured live on every snapshot (already fresh), and
  * "latest / target" + the "Assessment · Dated <date>" line -> read from
    components.json, which was previously only ever edited by hand.

This script re-assesses the "latest" targets once per day from each component's
SUPPORTED channel so the advisory stops going stale:

  * apt packages (nginx, git, python3, docker)  -> `apt-cache policy` candidate
  * pip packages (gunicorn)                     -> PyPI JSON latest release
  * ubuntu (LTS release)                        -> `do-release-upgrade -c`
                                                   (sees new LTS series that
                                                   apt-cache policy cannot)
  * node / npm (pinned NodeSource 22.x)         -> nodejs.org/dist index
  * powershell                                  -> GitHub latest stable release

Components intentionally left to manual review (no reliable probe from inside
WSL, so their reviewed targets are preserved untouched):
  * wsl, kernel      -> delivered by the Store `wsl --update` channel; the
                        Store version scheme differs from upstream GitHub tags.
  * edge, webview2   -> evergreen, auto-updating; no stable public version API,
                        and they are never "behind" so they are not flagged.
  * windows          -> Windows Update / IT managed.
  * kiro-cli         -> internal upgrade channel.
  * net-driver, gpu-driver, bios -> date-based vendor references (judged by age).

It is strictly non-destructive:
  * it only ever RAISES a `latest` value when a newer version is discovered
    (never downgrades, so hand-reviewed targets are preserved),
  * channels that cannot be probed from this machine (Windows / drivers / BIOS /
    Node major / WSL / kernel) keep their reviewed values untouched,
  * `assessment_date` is stamped to today (UTC) to reflect the re-check,
  * the file is written atomically (temp + os.replace).

Scheduled by sysperf-refresh-components.timer (daily). The Flask collector
re-reads components.json on every snapshot (SNAPSHOT_TTL = 3 s), so the UI
reflects any change within seconds — no service restart needed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .background_process import run_hidden

# SYSPERF_COMPONENTS_DIR: write the catalog where the dashboard reads it
# (the container and the systemd timer point it at the data directory).
# Default: next to this file, as components.py resolves it.
BASE = Path(os.environ.get("SYSPERF_COMPONENTS_DIR")
            or Path(__file__).resolve().parent)
COMPONENTS_FILE = BASE / "components.json"
LOG_FILE = BASE / "refresh_components.log"

log = logging.getLogger("refresh_components")

# catalog key -> apt package name (supported channel = apt / vendor apt repo)
APT_PKGS = {
    "nginx": "nginx",
    "git": "git",
    "python3": "python3",
    "docker": "docker-ce",
}
# catalog key -> PyPI project name
PIP_PKGS = {
    "gunicorn": "gunicorn",
}


def _ver(s: str | None) -> str:
    """Extract the first dotted version token (e.g. '1.24.0') from a string."""
    m = re.search(r"(\d+(?:\.\d+){1,3})", s or "")
    return m.group(1) if m else ""


def _cmp(a: str, b: str) -> int | None:
    """Numeric version compare: -1 a<b, 0 equal, 1 a>b, None if unknown."""
    aa = [int(x) for x in re.findall(r"\d+", a or "")]
    bb = [int(x) for x in re.findall(r"\d+", b or "")]
    if not aa or not bb:
        return None
    n = max(len(aa), len(bb))
    aa += [0] * (n - len(aa))
    bb += [0] * (n - len(bb))
    return (aa > bb) - (aa < bb)


def _apt_candidate(pkg: str) -> str:
    """Return the dotted upstream version of the apt candidate, or ''."""
    try:
        out = run_hidden(
            ["apt-cache", "policy", pkg],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception as exc:
        log.warning("apt-cache policy %s failed: %s", pkg, exc)
        return ""
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("Candidate:"):
            cand = ln.split(":", 1)[1].strip()
            if cand in ("(none)", ""):
                return ""
            # strip Debian epoch ("1:2.43.0-...") then take part before '-'
            cand = cand.split(":", 1)[-1]
            cand = cand.split("-", 1)[0]
            return _ver(cand)
    return ""


def _ubuntu_latest_lts() -> str:
    """Return the newest Ubuntu release available to THIS machine, or ''.

    `apt-cache policy` only sees the current series' repos, so it can never
    reveal a new LTS (e.g. 24.04 -> 26.04). The authoritative check is
    `do-release-upgrade -c`, which consults Ubuntu's meta-release and honours
    the local Prompt= policy (/etc/update-manager/release-upgrades). It is a
    check-only probe (-c) and changes nothing.
    """
    try:
        r = run_hidden(
            ["do-release-upgrade", "-c"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:
        log.warning("do-release-upgrade -c failed: %s", exc)
        return ""
    out = (r.stdout or "") + " " + (r.stderr or "")
    m = re.search(r"New release '([^']+)'", out)
    return _ver(m.group(1)) if m else ""  # '' when no new release is offered


def _pypi_latest(project: str) -> str:
    """Return the latest release version from PyPI, or ''."""
    url = f"https://pypi.org/pypi/{project}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.load(r)
        return _ver(data.get("info", {}).get("version", ""))
    except Exception as exc:
        log.warning("PyPI lookup %s failed: %s", project, exc)
        return ""


# The NodeSource channel for this host is pinned to the 22.x line
# (catalog note: "major upgrade - confirm first"), so Node/npm targets track
# the newest 22.x release, NOT the current Node LTS major.
_NODE_MAJOR = "22"
_dist_cache: dict = {}


def _node_dist_newest() -> dict | None:
    """Newest nodejs.org/dist entry for the pinned major, or None."""
    if "e" in _dist_cache:
        return _dist_cache["e"]
    try:
        with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=15) as r:
            data = json.load(r)  # newest-first list
    except Exception as exc:
        log.warning("nodejs dist fetch failed: %s", exc)
        _dist_cache["e"] = None
        return None
    for e in data:
        if str(e.get("version", "")).startswith(f"v{_NODE_MAJOR}."):
            _dist_cache["e"] = e
            return e
    _dist_cache["e"] = None
    return None


def _node_latest() -> str:
    e = _node_dist_newest()
    return _ver(e.get("version")) if e else ""


def _npm_latest() -> str:
    """npm version bundled with the newest pinned-major Node release."""
    e = _node_dist_newest()
    return _ver(e.get("npm")) if e else ""


def _powershell_latest() -> str:
    """Latest stable PowerShell 7 release tag from GitHub, or ''."""
    url = "https://api.github.com/repos/PowerShell/PowerShell/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return _ver(json.load(r).get("tag_name", ""))
    except Exception as exc:
        log.warning("PowerShell GitHub lookup failed: %s", exc)
        return ""


def _preserve_suffix(old_latest: str, new_ver: str) -> str:
    """Keep any trailing annotation on the catalog value (e.g. ' (Intel ref)').

    Replaces only the dotted version token so notes like
    '22.20.0 (major upgrade - confirm first)' survive a bump.
    """
    old_tok = _ver(old_latest)
    if old_tok and old_tok in old_latest:
        return old_latest.replace(old_tok, new_ver, 1)
    return new_ver


def refresh() -> int:
    try:
        catalog = json.loads(COMPONENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("cannot read %s: %s", COMPONENTS_FILE, exc)
        return 1

    changes: list[str] = []
    for entry in catalog.get("catalog", []):
        key = entry.get("key", "")
        if entry.get("compare", "version") != "version":
            continue  # date-based (drivers/BIOS) judged by age, not probed here

        discovered = ""
        if key == "ubuntu":
            discovered = _ubuntu_latest_lts()
        elif key == "node":
            discovered = _node_latest()
        elif key == "npm":
            discovered = _npm_latest()
        elif key == "powershell":
            discovered = _powershell_latest()
        elif key in APT_PKGS:
            discovered = _apt_candidate(APT_PKGS[key])
        elif key in PIP_PKGS:
            discovered = _pypi_latest(PIP_PKGS[key])
        else:
            continue  # channel not probeable from this host -> keep reviewed value

        if not discovered:
            continue
        current_latest = _ver(entry.get("latest", ""))
        if _cmp(discovered, current_latest) == 1:  # strictly newer only
            new_val = _preserve_suffix(entry.get("latest", ""), discovered)
            # A new Ubuntu LTS series is a release upgrade, not an apt point
            # upgrade — correct the channel so the advisory is accurate.
            if key == "ubuntu":
                new_series = discovered.split(".")[0]
                old_series = current_latest.split(".")[0]
                if new_series != old_series:
                    entry["channel"] = "do-release-upgrade (LTS\u2192LTS)"
            changes.append(f"{key}: {entry.get('latest')} -> {new_val}")
            entry["latest"] = new_val

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prev_date = catalog.get("assessment_date")
    catalog["assessment_date"] = today

    # Atomic write, preserving pretty formatting.
    fd, tmp = tempfile.mkstemp(dir=str(BASE), prefix=".components.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(catalog, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, COMPONENTS_FILE)
    except Exception as exc:
        log.error("atomic write failed: %s", exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 1

    if changes:
        log.info("assessment_date %s -> %s; %d target(s) bumped: %s",
                 prev_date, today, len(changes), "; ".join(changes))
    else:
        log.info("assessment_date %s -> %s; no newer targets found", prev_date, today)
    return 0


def main() -> int:
    """Script entry point. Logging is configured here, not at import, so importing
    this module (the test suite, the self-test) never touches the root logger or
    creates a log file next to the code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
    )
    return refresh()


if __name__ == "__main__":
    raise SystemExit(main())
