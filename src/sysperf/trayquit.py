"""Quit tray apps once their window is closed, so their RAM is freed.

Apps such as ChatGPT, WhatsApp, Slack or Discord keep running in the system
tray after their window is closed — ChatGPT alone was measured holding ~1.5 GB
on 2026-10-24 with no window open. This watcher quits a listed app once it has
had NO visible top-level window for ``grace_s`` seconds.

Safety
------
* Only apps the user listed are ever touched; names are validated and the
  appctl CRITICAL denylist plus BLOCKED (shell, browsers, WSL, this dashboard)
  can never be added.
* An app that started less than ``startup_grace_s`` ago is left alone (it may
  not have drawn its window yet); a minimized window still counts as open.
* Only processes in the dashboard's own Windows session are considered.
* Honours FIX_ENABLED: with execution disabled it only reports.

Windows reads (process list, visible windows, memory, start time) are made
in-process with ctypes — no PowerShell spawn per check. ``WinApi`` is injected
so the logic is unit-testable off Windows.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .appctl import CRITICAL, NAME_RE

log = logging.getLogger(__name__)

DEFAULT_APPS = ["ChatGPT", "WhatsApp.Root", "WhatsApp", "Slack", "Discord", "Spotify"]
# Never quit these even if listed: the shell, browsers (their windowless helpers
# are normal), WSL / its VM, interpreters (this dashboard runs in pythonw), and
# Claude Code / editors that legitimately run without a window of their own,
# security agents and WSLg.
BLOCKED = {"explorer", "msedge", "msedgewebview2", "chrome", "firefox", "wsl",
           "wslhost", "wslservice", "vmmem", "vmmemwsl", "python", "pythonw",
           "py", "pyw", "claude", "code", "node", "powershell", "pwsh", "cmd",
           "windowsterminal", "openconsole", "onedrive", "onedrive.sync.service",
           # security agent UI, and WSLg's window host for Linux GUI apps
           "microsoftsecurityapp", "securityhealthsystray", "msrdc"}
# Suggestions skip these well-known windowless background components.
NOT_SUGGESTED = BLOCKED | {"textinputhost", "widgets", "widgetservice", "phoneexperiencehost",
                           "crossdeviceservice", "gamebar", "gamebarftserver",
                           "systemsettings", "applicationframehost", "smartscreen",
                           "sdxhelper", "searchprotocolhost", "searchfilterhost",
                           "useroobebroker", "lockapp", "codex", "mobaxterm",
                           "xwin_mobax", "motty"}
SUGGEST_MIN_MB = 80


def allowed(name: str) -> bool:
    n = (name or "").lower()
    return bool(NAME_RE.match(name or "")) and n not in CRITICAL and n not in BLOCKED


class WinApi:
    """Thin ctypes wrapper; every call is read-only except ``terminate``."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.c, self.w = ctypes, wintypes
        self.k32 = ctypes.windll.kernel32
        self.u32 = ctypes.windll.user32
        self.psapi = ctypes.windll.psapi
        try:
            self.dwm = ctypes.windll.dwmapi
        except OSError:                                  # pragma: no cover
            self.dwm = None
        sid = wintypes.DWORD()
        self.k32.ProcessIdToSessionId(self.k32.GetCurrentProcessId(), ctypes.byref(sid))
        self.session = sid.value

    def processes(self) -> list:
        c, w = self.c, self.w

        class PE(c.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                        ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", c.c_size_t),
                        ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                        ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", c.c_long),
                        ("dwFlags", w.DWORD), ("szExeFile", c.c_wchar * 260)]
        self.k32.CreateToolhelp32Snapshot.restype = w.HANDLE
        snap = self.k32.CreateToolhelp32Snapshot(2, 0)          # TH32CS_SNAPPROCESS
        out = []
        try:
            pe = PE()
            pe.dwSize = c.sizeof(PE)
            ok = self.k32.Process32FirstW(snap, c.byref(pe))
            while ok:
                sid = w.DWORD()
                if self.k32.ProcessIdToSessionId(pe.th32ProcessID, c.byref(sid)) \
                        and sid.value == self.session:
                    exe = pe.szExeFile
                    out.append({"pid": pe.th32ProcessID,
                                "name": exe[:-4] if exe.lower().endswith(".exe") else exe})
                ok = self.k32.Process32NextW(snap, c.byref(pe))
        finally:
            self.k32.CloseHandle(snap)
        return out

    def visible_pids(self) -> set:
        c, w = self.c, self.w
        pids: set = set()
        rect = w.RECT()
        cloaked = w.DWORD()

        @c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        def cb(hwnd, _):
            if not self.u32.IsWindowVisible(hwnd):
                return True
            if self.u32.GetWindowLongW(hwnd, -20) & 0x80:          # WS_EX_TOOLWINDOW
                return True
            if self.dwm is not None and self.dwm.DwmGetWindowAttribute(
                    hwnd, 14, c.byref(cloaked), c.sizeof(cloaked)) == 0 and cloaked.value:
                return True                                      # DWMWA_CLOAKED
            # Minimized windows keep their visible flag; only skip zero-size stubs.
            if not self.u32.IsIconic(hwnd):
                self.u32.GetWindowRect(hwnd, c.byref(rect))
                if rect.right - rect.left < 2 or rect.bottom - rect.top < 2:
                    return True
            pid = w.DWORD()
            self.u32.GetWindowThreadProcessId(hwnd, c.byref(pid))
            pids.add(pid.value)
            return True
        self.u32.EnumWindows(cb, 0)
        return pids

    def _open(self, pid: int, access: int):
        self.k32.OpenProcess.restype = self.w.HANDLE
        return self.k32.OpenProcess(access, False, pid)

    def mem_mb(self, pid: int) -> float:
        c, w = self.c, self.w

        class PMC(c.Structure):
            _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [
                (n, c.c_size_t) for n in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                    "PrivateUsage")]
        h = self._open(pid, 0x1000)                 # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return 0.0
        try:
            pmc = PMC()
            pmc.cb = c.sizeof(PMC)
            if self.psapi.GetProcessMemoryInfo(h, c.byref(pmc), pmc.cb):
                return pmc.PrivateUsage / (1024 * 1024)
            return 0.0
        finally:
            self.k32.CloseHandle(h)

    def started(self, pid: int) -> Optional[float]:
        c, w = self.c, self.w
        h = self._open(pid, 0x1000)
        if not h:
            return None
        try:
            ft = [w.FILETIME() for _ in range(4)]
            if not self.k32.GetProcessTimes(h, *[c.byref(f) for f in ft]):
                return None
            v = (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime
            return v / 1e7 - 11644473600.0               # FILETIME -> Unix seconds
        finally:
            self.k32.CloseHandle(h)

    def terminate(self, pid: int) -> bool:
        h = self._open(pid, 0x0001)                     # PROCESS_TERMINATE
        if not h:
            return False
        try:
            return bool(self.k32.TerminateProcess(h, 0))
        finally:
            self.k32.CloseHandle(h)


class TrayQuitter:
    def __init__(self, state_file: Path, log_file: Path, *, fix_enabled: bool = True,
                 api_factory: Callable[[], object] = WinApi,
                 clock: Callable[[], float] = time.time):
        self.state_file, self.log_file = Path(state_file), Path(log_file)
        self.fix_enabled = fix_enabled
        self.clock = clock
        self._api_factory = api_factory
        self._api = None
        self.lock = threading.RLock()
        self.hidden_since: dict = {}          # lower name -> first windowless check
        self.last_view: dict = {}
        self.settings = {"enabled": True, "grace_s": 60, "startup_grace_s": 60,
                         "apps": list(DEFAULT_APPS)}
        self.recent: list = []
        self._load()

    # ------------------------------------------------------------ persistence --
    def _load(self) -> None:
        try:
            s = json.loads(self.state_file.read_text())
            self.settings.update({k: s[k] for k in self.settings if k in s})
            self.recent = list(s.get("recent") or [])[-50:]
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            self.state_file.write_text(json.dumps({**self.settings, "recent": self.recent[-50:]},
                                                  indent=2))
        except OSError as exc:
            log.warning("trayquit: could not save state: %s", exc)

    @property
    def api(self):
        if self._api is None:
            self._api = self._api_factory()
        return self._api

    # ---------------------------------------------------------------- config --
    def configure(self, *, enabled=None, grace_s=None, add=None, remove=None) -> dict:
        with self.lock:
            if enabled is not None:
                self.settings["enabled"] = bool(enabled)
            if grace_s is not None:
                self.settings["grace_s"] = max(15, min(3600, int(grace_s)))
            if add:
                if not allowed(add):
                    raise ValueError(f"'{add}' cannot be auto-quit (system, shell, browser "
                                     "or this dashboard)")
                if add.lower() not in {a.lower() for a in self.settings["apps"]}:
                    self.settings["apps"].append(add)
            if remove:
                self.settings["apps"] = [a for a in self.settings["apps"]
                                         if a.lower() != remove.lower()]
                self.hidden_since.pop(remove.lower(), None)
            self._save()
            return dict(self.settings)

    # ------------------------------------------------------------------ check --
    def tick(self) -> list:
        """One check; returns the quit events it produced."""
        with self.lock:
            api = self.api
            now = self.clock()
            procs = api.processes()
            visible = api.visible_pids()
            by_name: dict = {}
            for p in procs:
                by_name.setdefault(p["name"].lower(), []).append(p["pid"])
            listed = {a.lower(): a for a in self.settings["apps"] if allowed(a)}
            view, events = {}, []
            for low, label in listed.items():
                pids = by_name.get(low, [])
                if not pids:
                    self.hidden_since.pop(low, None)
                    view[low] = {"name": label, "running": False}
                    continue
                window = any(pid in visible for pid in pids)
                starts = [s for s in (api.started(pid) for pid in pids) if s]
                age = now - min(starts) if starts else None
                mem = round(sum(api.mem_mb(pid) for pid in pids))
                row = {"name": label, "running": True, "procs": len(pids),
                       "window": window, "mem_mb": mem}
                if window or (age is not None and age < self.settings["startup_grace_s"]):
                    self.hidden_since.pop(low, None)
                    row["state"] = "open" if window else "starting"
                else:
                    since = self.hidden_since.setdefault(low, now)
                    row["hidden_for_s"] = round(now - since)
                    row["state"] = "in tray"
                    if (self.settings["enabled"] and self.fix_enabled
                            and now - since >= self.settings["grace_s"]):
                        killed = sum(1 for pid in pids if api.terminate(pid))
                        ev = {"at": now, "name": label, "procs": killed,
                              "freed_mb": mem, "hidden_s": round(now - since)}
                        events.append(ev)
                        self.recent.append(ev)
                        self.hidden_since.pop(low, None)
                        row.update(state="quit", running=False)
                        log.info("trayquit: quit %s (%d procs, ~%d MB) after %ds in tray",
                                 label, killed, mem, ev["hidden_s"])
                view[low] = row
            # Suggestions: other windowless apps holding real memory.
            sugg = []
            for low, pids in by_name.items():
                if low in listed or low in NOT_SUGGESTED or not allowed(low) \
                        or any(pid in visible for pid in pids):
                    continue
                mem = sum(api.mem_mb(pid) for pid in pids)
                if mem >= SUGGEST_MIN_MB:
                    name = next(p["name"] for p in procs if p["name"].lower() == low)
                    sugg.append({"name": name, "procs": len(pids), "mem_mb": round(mem)})
            self.last_view = {"at": now, "apps": list(view.values()),
                              "suggestions": sorted(sugg, key=lambda s: -s["mem_mb"])[:8]}
            if events:
                self._save()
                try:
                    with self.log_file.open("a") as fh:
                        for ev in events:
                            fh.write(json.dumps(ev) + "\n")
                except OSError:
                    pass
            return events

    def status(self) -> dict:
        with self.lock:
            day = self.clock() - 86400
            today = [e for e in self.recent if e["at"] >= day]
            return {**self.settings, "execution_enabled": self.fix_enabled,
                    "checked_at": self.last_view.get("at"),
                    "apps_live": self.last_view.get("apps", []),
                    "suggestions": self.last_view.get("suggestions", []),
                    "recent": list(reversed(self.recent[-12:])),
                    "freed_24h_mb": round(sum(e.get("freed_mb", 0) for e in today)),
                    "quits_24h": len(today)}

    def loop(self, interval: float = 15.0) -> None:       # pragma: no cover
        while True:
            try:
                self.tick()
            except Exception:                                # noqa: BLE001
                log.exception("trayquit check failed")
            time.sleep(interval)
