"""Notifications: an in-app inbox, and Windows toasts for what needs attention.

Until 4.1 the dashboard only told you anything if you opened it. The agent now
raises a notification when the score drops below threshold, when it applies a
fix or quits a tray app, when a tray app needs your consent, when an AI patch
is waiting for review, and when the service itself is unhealthy.

Every notification lands in the inbox (``GET /api/notifications``). A Windows
toast is shown as well when toasts are enabled, the kind is enabled, it is not
quiet hours, and the same ``key`` has not fired within ``min_interval_s`` — so
a metric hovering around its threshold cannot spam the desktop.

Safety: toast text never reaches a command line. Title, body and link are
passed to Windows PowerShell through environment variables and XML-escaped
inside the script, which itself is fixed and sent with ``-EncodedCommand``.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, ClassVar

from .background_process import run_hidden

log = logging.getLogger(__name__)

KINDS = {
    "score": "Score dropped below the threshold",
    "action": "SysPerf applied a fix or quit a tray app",
    "consent": "A tray app is waiting for your permission",
    "review": "An AI patch is waiting for review",
    "health": "The SysPerf service itself has a problem",
}

TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Security.SecurityElement]::Escape($env:SYSPERF_TOAST_TITLE)
$b = [Security.SecurityElement]::Escape($env:SYSPERF_TOAST_BODY)
$u = [Security.SecurityElement]::Escape($env:SYSPERF_TOAST_URL)
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml("<toast activationType='protocol' launch='$u'><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual><actions><action content='Open SysPerf' activationType='protocol' arguments='$u'/></actions></toast>")
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def toast_argv(exe: str = "powershell.exe") -> list:
    enc = base64.b64encode(TOAST_SCRIPT.encode("utf-16-le")).decode("ascii")
    return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", enc]


def send_toast(title: str, body: str, url: str, run: Callable = run_hidden) -> bool:
    """Show one Windows toast. Returns False (never raises) on any failure."""
    env = dict(os.environ, SYSPERF_TOAST_TITLE=title[:120], SYSPERF_TOAST_BODY=body[:300],
               SYSPERF_TOAST_URL=url)
    try:
        r = run(toast_argv(), capture_output=True, text=True, timeout=30, env=env)
        return r.returncode == 0
    except Exception as exc:
        log.warning("toast failed: %s", exc)
        return False


class Notifier:
    DEFAULTS: ClassVar[dict] = {"toasts": True, "min_interval_s": 1800, "quiet_start": None,
                "quiet_end": None, "kinds": {k: True for k in KINDS}}

    def __init__(self, state_file: Path, base_url: str, *, toast_capable: bool,
                 sender: Callable = send_toast, clock: Callable[[], float] = time.time,
                 background: bool = True, capacity: int = 100):
        self.state_file = Path(state_file)
        self.base_url = base_url.rstrip("/") + "/"
        self.toast_capable = toast_capable
        self.sender = sender
        self.clock = clock
        self.background = background
        self.capacity = capacity
        self.lock = threading.Lock()
        self.settings = json.loads(json.dumps(self.DEFAULTS))
        self.inbox: list = []
        self.last_sent: dict = {}
        self._seq = 0
        self._load()

    # ------------------------------------------------------------ persistence --
    def _load(self) -> None:
        try:
            s = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        for k in self.DEFAULTS:
            if k in s:
                self.settings[k] = s[k]
        self.settings["kinds"] = {**self.DEFAULTS["kinds"], **(s.get("kinds") or {})}
        self.inbox = list(s.get("inbox") or [])[-self.capacity:]
        self.last_sent = dict(s.get("last_sent") or {})
        self._seq = max([n.get("id", 0) for n in self.inbox] + [0])

    def _save(self) -> None:
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({**self.settings, "inbox": self.inbox[-self.capacity:],
                                       "last_sent": self.last_sent}, indent=1))
            tmp.replace(self.state_file)
        except OSError as exc:
            log.warning("notify: could not save state: %s", exc)

    # ---------------------------------------------------------------- config --
    def configure(self, changes: dict) -> dict:
        clean = {}
        for k, v in (changes or {}).items():
            if k == "toasts":
                if not isinstance(v, bool):
                    raise ValueError("toasts must be true or false")
                clean[k] = v
            elif k == "min_interval_s":
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not 60 <= v <= 86400:
                    raise ValueError("min_interval_s must be between 60 and 86400")
                clean[k] = int(v)
            elif k in ("quiet_start", "quiet_end"):
                if v is not None and (isinstance(v, bool) or not isinstance(v, int)
                                      or not 0 <= v <= 23):
                    raise ValueError(f"{k} must be an hour 0-23 or null")
                clean[k] = v
            elif k == "kinds":
                if not isinstance(v, dict) or any(
                        kk not in KINDS or not isinstance(vv, bool) for kk, vv in v.items()):
                    raise ValueError("kinds must map " + ", ".join(KINDS) + " to booleans")
                clean[k] = {**self.settings["kinds"], **v}
            else:
                raise ValueError(f"'{k}' is not a notification setting")
        with self.lock:
            self.settings.update(clean)
            self._save()
            return self.describe()

    def describe(self) -> dict:
        return {**json.loads(json.dumps(self.settings)), "toast_capable": self.toast_capable,
                "kind_labels": KINDS}

    def _quiet(self) -> bool:
        a, b = self.settings.get("quiet_start"), self.settings.get("quiet_end")
        if a is None or b is None or a == b:
            return False
        h = datetime.fromtimestamp(self.clock()).hour
        return a <= h < b if a < b else (h >= a or h < b)

    # ---------------------------------------------------------------- notify --
    def notify(self, kind: str, title: str, body: str, *, key: str | None = None,
               link: str = "#home") -> dict | None:
        """Record a notification; toast it when allowed. Returns it, or None if
        an identical ``key`` fired within ``min_interval_s`` (deduplicated)."""
        if kind not in KINDS:
            raise ValueError(f"unknown notification kind {kind!r}")
        now = self.clock()
        key = key or f"{kind}:{title}"
        with self.lock:
            last = self.last_sent.get(key)
            if last is not None and now - last < self.settings["min_interval_s"]:
                return None
            self.last_sent[key] = now
            self._seq += 1
            item = {"id": self._seq, "at": now, "kind": kind, "title": title[:120],
                    "body": body[:300], "link": link if link.startswith("#") else "#home",
                    "read": False, "toast": False}
            want = (self.toast_capable and self.settings["toasts"]
                    and self.settings["kinds"].get(kind, True) and not self._quiet())
            item["toast"] = want
            self.inbox.append(item)
            del self.inbox[:-self.capacity]
            self._save()
        if want:
            url = self.base_url + item["link"]
            if self.background:
                threading.Thread(target=self.sender, args=(item["title"], item["body"], url),
                                 daemon=True).start()
            else:
                self.sender(item["title"], item["body"], url)
        return item

    def items(self, limit: int = 50) -> dict:
        with self.lock:
            rows = list(reversed(self.inbox))[:limit]
            unread = sum(1 for n in self.inbox if not n["read"])
        return {"items": rows, "unread": unread}

    def mark_read(self, ids=None) -> int:
        """Mark the given ids (or all, when ``ids`` is None) as read."""
        with self.lock:
            n = 0
            for item in self.inbox:
                if not item["read"] and (ids is None or item["id"] in ids):
                    item["read"] = True
                    n += 1
            if n:
                self._save()
            return n
