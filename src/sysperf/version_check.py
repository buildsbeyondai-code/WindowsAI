"""
Live "latest available" versions for the Components view.

components.json holds hand-reviewed targets, and on a Windows install nothing
re-assessed them, so the view drifted until the installed versions were newer
than the "latest" it showed (WSL 3.0.3 against a target of 2.6.2). This module
asks each component's public release feed for its newest stable version once a
day and stores the answers in the data directory; components.py prefers them
over the catalog.

Only public version numbers are requested. Nothing about this PC is sent: each
lookup is a plain GET of a public URL. ``PERFDASH_VERSION_CHECK=0`` (or
``"version_check": false`` in sysperf.config.json) turns it off, and the view
falls back to the catalog.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from .config import lock_packages

log = logging.getLogger("perfdash")

REFRESH_S = 24 * 3600
USER_AGENT = "SysPerf-version-check"


def _ver(s) -> str:
    m = re.search(r"(\d+(?:\.\d+){1,3})", str(s or ""))
    return m.group(1) if m else ""


def _key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def http_get(url: str, timeout: float = 15.0) -> str:     # pragma: no cover - network
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json, text/plain"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ------------------------------------------------------------------ feeds --
def github(repo: str):
    """Newest stable (non-prerelease) release tag of a GitHub repository."""
    return lambda get: _ver(json.loads(get(
        f"https://api.github.com/repos/{repo}/releases/latest")).get("tag_name"))


def pypi(project: str):
    return lambda get: _ver(json.loads(get(
        f"https://pypi.org/pypi/{project}/json"))["info"]["version"])


def npm(package: str):
    return lambda get: _ver(json.loads(get(
        f"https://registry.npmjs.org/{package}/latest"))["version"])


def python_release(get) -> str:
    """Newest Python release (endoflife.date lists cycles newest first)."""
    return _ver(json.loads(get("https://endoflife.date/api/python.json"))[0]["latest"])


def ubuntu_lts(get) -> str:
    """Newest supported Ubuntu LTS point release, from Ubuntu's own upgrade feed."""
    newest = ""
    for block in get("https://changelogs.ubuntu.com/meta-release-lts").split("\n\n"):
        fields = dict(ln.split(": ", 1) for ln in block.splitlines() if ": " in ln)
        if fields.get("Supported") == "1" and _ver(fields.get("Version")):
            newest = _ver(fields["Version"])         # the feed is oldest first
    return newest


def edge_stable(get) -> str:
    """Newest Edge Stable build for Windows x64."""
    best = ""
    for product in json.loads(get("https://edgeupdates.microsoft.com/api/products")):
        if product.get("Product") != "Stable":
            continue
        for rel in product.get("Releases", []):
            v = _ver(rel.get("ProductVersion"))
            if rel.get("Platform") == "Windows" and rel.get("Architecture") == "x64" \
                    and _key(v) > _key(best):
                best = v
    return best


def winget(package_path: str):
    """Newest version of a package in the official winget index (microsoft/winget-pkgs).

    WebView2 ships on its own schedule, often days after the Edge build with the
    same number, so Edge's feed would report a runtime that does not exist yet.
    """
    def feed(get) -> str:
        entries = json.loads(get("https://api.github.com/repos/microsoft/winget-pkgs/contents/"
                                 f"manifests/{package_path}"))
        names = [e["name"] for e in entries if e.get("type") == "dir" and _ver(e.get("name"))]
        return max(names, key=_key, default="")
    return feed


SOURCES = {
    "wsl": github("microsoft/WSL"),
    "kernel": github("microsoft/WSL2-Linux-Kernel"),
    "powershell": github("PowerShell/PowerShell"),
    "git-windows": github("git-for-windows/git"),
    "flask": pypi("flask"),
    "waitress": pypi("waitress"),
    # The rest of the CI SBOM: Flask's locked dependencies and pip.
    **{p: pypi(p) for p in ("werkzeug", "jinja2", "markupsafe", "itsdangerous",
                            "click", "blinker", "pip")},
    "pytest": pypi("pytest"),
    "playwright": pypi("playwright"),
    "claude-code": npm("@anthropic-ai/claude-code"),
    "python": python_release,
    "ubuntu": ubuntu_lts,
    "edge": edge_stable,
    "webview2": winget("m/Microsoft/EdgeWebView2Runtime"),
}
# A dependency an upgrade added to the lock gets its live feed too (5.4.4).
for _p in lock_packages():
    SOURCES.setdefault(_p, pypi(_p))


# Driver updates Windows Update offers this PC (optional ones included). Read-only:
# it searches, never downloads or installs. A standard user may search.
WU_DRIVER_SEARCH = r"""
$s = New-Object -ComObject Microsoft.Update.Session
$q = $s.CreateUpdateSearcher(); $q.ServerSelection = 3
$q.ServiceID = '7971f918-a847-4430-9279-4a52d1efe18d'
$r = $q.Search("IsInstalled=0 and Type='Driver'")
ConvertTo-Json -Compress -InputObject @($r.Updates | ForEach-Object {
  @{ title = $_.Title; manufacturer = $_.DriverManufacturer; model = $_.DriverModel;
     cls = $_.DriverClass; date = if ($_.DriverVerDate) { $_.DriverVerDate.ToString('yyyy-MM-dd') } } })
"""


def offered_drivers(run=subprocess.run) -> list | None:
    """Driver updates Windows Update offers; None when it cannot be asked."""
    if os.name != "nt":
        return None
    try:
        r = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WU_DRIVER_SEARCH],
                capture_output=True, text=True, timeout=600, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        got = json.loads(r.stdout.strip() or "[]")
    except ValueError:
        return None
    return [d for d in (got if isinstance(got, list) else [got]) if isinstance(d, dict)]


class LatestVersions:
    """The newest public version of each component, refreshed once a day."""

    def __init__(self, state_file: Path, *, enabled: bool = True, get=None,
                 clock=time.time, sources: dict | None = None, drivers=None) -> None:
        self.file = Path(state_file)
        self.enabled = enabled
        self.get = get or http_get
        self.clock = clock
        self.sources = SOURCES if sources is None else sources
        self.drivers = drivers
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def due(self) -> bool:
        checked = self.load().get("checked_at")
        return self.enabled and (checked is None or self.clock() - checked >= REFRESH_S)

    def refresh(self) -> dict:
        """Ask every feed; a feed that fails keeps its previous answer."""
        with self._lock:
            prev = self.load().get("latest") or {}
            latest, errors, cache = dict(prev), {}, {}
            for key, feed in self.sources.items():
                try:
                    # edge and webview2 share a feed: ask it once per refresh
                    v = cache[feed] if feed in cache else feed(self.get)
                    cache[feed] = v
                    if v:
                        latest[key] = v
                except Exception as exc:                # one feed down: keep the rest
                    errors[key] = f"{type(exc).__name__}: {exc}"[:200]
            data = {"checked_at": self.clock(), "latest": latest, "errors": errors}
            # Drivers have no public "latest" feed; what Windows Update offers this
            # PC is the honest target (5.4.6). Unreachable: keep the last answer.
            offered = self.drivers() if self.drivers else None
            data["drivers"] = ({"checked_at": self.clock(), "offered": offered}
                               if offered is not None else self.load().get("drivers"))
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            tmp.replace(self.file)
        if errors:
            log.info("version check: %d feed(s) unavailable: %s", len(errors), ", ".join(errors))
        return data

    def loop(self, tick_s: float = 3600.0) -> None:       # pragma: no cover - thread
        while True:
            try:
                if self.due():
                    self.refresh()
            except Exception:                           # keep the loop alive
                log.exception("version check failed")
            time.sleep(tick_s)
