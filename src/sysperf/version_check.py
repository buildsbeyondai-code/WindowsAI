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
import re
import threading
import time
import urllib.request
from pathlib import Path

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
    """Newest Edge Stable build for Windows x64 (WebView2 Runtime ships the same)."""
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


SOURCES = {
    "wsl": github("microsoft/WSL"),
    "kernel": github("microsoft/WSL2-Linux-Kernel"),
    "powershell": github("PowerShell/PowerShell"),
    "git-windows": github("git-for-windows/git"),
    "flask": pypi("flask"),
    "waitress": pypi("waitress"),
    "pytest": pypi("pytest"),
    "playwright": pypi("playwright"),
    "claude-code": npm("@anthropic-ai/claude-code"),
    "python": python_release,
    "ubuntu": ubuntu_lts,
    "edge": edge_stable,
    "webview2": edge_stable,
}


class LatestVersions:
    """The newest public version of each component, refreshed once a day."""

    def __init__(self, state_file: Path, *, enabled: bool = True, get=None,
                 clock=time.time, sources: dict | None = None) -> None:
        self.file = Path(state_file)
        self.enabled = enabled
        self.get = get or http_get
        self.clock = clock
        self.sources = SOURCES if sources is None else sources
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
