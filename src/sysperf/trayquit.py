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
* Consent (4.1): every app is ``ask`` until you decide. An app that would be
  quit but has no decision yet is held as "awaiting consent" and a
  notification asks you; ``allow`` lets it be quit from then on, ``never``
  keeps it running for good.
* Busy guard (4.1): an app using more than ``busy_cpu_pct`` of a core since
  the previous check (a call, a download, a sync) is left alone that round.
* Undo (4.1): the executable path is recorded at quit time, and ``reopen``
  starts exactly that file again — never a path supplied by a request.

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
from typing import Callable

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


class WinApi:  # pragma: no cover - Windows ctypes; tests/test_trayquit_win.py runs it on Windows
    """Thin ctypes wrapper; every call is read-only except ``terminate`` and ``launch``."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.c, self.w = ctypes, wintypes
        self.k32 = ctypes.windll.kernel32
        self.u32 = ctypes.windll.user32
        self.psapi = ctypes.windll.psapi
        try:
            self.dwm = ctypes.windll.dwmapi
        except OSError:
            self.dwm = None
        sid = wintypes.DWORD()
        self.k32.ProcessIdToSessionId(self.k32.GetCurrentProcessId(), ctypes.byref(sid))
        self.session = sid.value
        # Structure types are built once: defining them inside the per-tick
        # methods made a new class every 15 s (see health._win_mem_api).
        c, w = ctypes, wintypes

        class PE(c.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                        ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", c.c_size_t),
                        ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                        ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", c.c_long),
                        ("dwFlags", w.DWORD), ("szExeFile", c.c_wchar * 260)]

        class PMC(c.Structure):
            _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [
                (n, c.c_size_t) for n in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                    "PrivateUsage")]
        self._PE, self._PMC = PE, PMC

    def processes(self) -> list:
        c, w, PE = self.c, self.w, self._PE
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
        c, PMC = self.c, self._PMC
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

    def started(self, pid: int) -> float | None:
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

    def cpu_s(self, pid: int) -> float | None:
        """Kernel + user CPU seconds the process has consumed so far."""
        c, w = self.c, self.w
        h = self._open(pid, 0x1000)
        if not h:
            return None
        try:
            ft = [w.FILETIME() for _ in range(4)]
            if not self.k32.GetProcessTimes(h, *[c.byref(f) for f in ft]):
                return None
            return sum(((f.dwHighDateTime << 32) | f.dwLowDateTime) for f in ft[2:]) / 1e7
        finally:
            self.k32.CloseHandle(h)

    def exe_path(self, pid: int) -> str | None:
        c, w = self.c, self.w
        h = self._open(pid, 0x1000)
        if not h:
            return None
        try:
            buf = c.create_unicode_buffer(1024)
            size = w.DWORD(len(buf))
            if self.k32.QueryFullProcessImageNameW(h, 0, buf, c.byref(size)):
                return buf.value
            return None
        finally:
            self.k32.CloseHandle(h)

    def launch(self, path: str) -> bool:
        """Start an executable detached from this service (used by reopen)."""
        import subprocess
        try:
            subprocess.Popen([path], close_fds=True, creationflags=0x00000008 | 0x00000200)
            return True
        except OSError:
            return False

    def terminate(self, pid: int) -> bool:
        h = self._open(pid, 0x0001)                     # PROCESS_TERMINATE
        if not h:
            return False
        try:
            return bool(self.k32.TerminateProcess(h, 0))
        finally:
            self.k32.CloseHandle(h)


CONSENT = ("ask", "allow", "never")


class TrayQuitter:
    def __init__(self, state_file: Path, log_file: Path, *, fix_enabled: bool = True,
                 api_factory: Callable[[], object] = WinApi,
                 clock: Callable[[], float] = time.time,
                 on_event: Callable[[str, dict], None] | None = None,
                 default_enabled: bool = True):
        self.state_file, self.log_file = Path(state_file), Path(log_file)
        self.fix_enabled = fix_enabled
        self.clock = clock
        self._api_factory = api_factory
        self._api = None
        self.lock = threading.RLock()
        self.hidden_since: dict = {}          # lower name -> first windowless check
        self.last_view: dict = {}
        # Off on a fresh Store install until the user opts in (store_mode.py).
        self.settings = {"enabled": default_enabled, "grace_s": 60, "startup_grace_s": 60,
                         "apps": list(DEFAULT_APPS), "consent": {}, "busy_cpu_pct": 3.0}
        self.recent: list = []
        self.on_event = on_event              # ("quit"|"consent", details) -> None
        self._cpu_seen: dict = {}             # pid -> (cpu seconds, wall time)
        self.asked: set = set()               # consent already requested this run
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
    def consent_of(self, name: str) -> str:
        return self.settings["consent"].get(name.lower(), "ask")

    def configure(self, *, enabled=None, grace_s=None, add=None, remove=None,
                  consent=None, busy_cpu_pct=None) -> dict:
        with self.lock:
            if consent is not None:
                if not isinstance(consent, dict) or consent.get("decision") not in CONSENT \
                        or not isinstance(consent.get("name"), str):
                    raise ValueError("consent must be {name, decision: ask|allow|never}")
                name = consent["name"]
                if name.lower() not in {a.lower() for a in self.settings["apps"]}:
                    raise ValueError(f"'{name}' is not on the auto-quit list")
                self.settings["consent"][name.lower()] = consent["decision"]
            if busy_cpu_pct is not None:
                if isinstance(busy_cpu_pct, bool) or not isinstance(busy_cpu_pct, (int, float)):
                    raise ValueError("busy_cpu_pct must be a number")
                self.settings["busy_cpu_pct"] = max(0.5, min(50.0, float(busy_cpu_pct)))
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
                self.settings["consent"].pop(remove.lower(), None)
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
                    row["consent"] = self.consent_of(label)
                    busy = self._busy_pct(pids, now)
                    row["cpu_pct"] = busy
                    due = (self.settings["enabled"] and self.fix_enabled
                           and now - since >= self.settings["grace_s"])
                    if due and row["consent"] == "never":
                        row["state"] = "kept (never quit)"
                    elif due and busy is not None and busy >= self.settings["busy_cpu_pct"]:
                        row["state"] = "busy"         # a call, download or sync - not now
                    elif due and row["consent"] == "ask":
                        row["state"] = "awaiting consent"
                        if low not in self.asked:
                            self.asked.add(low)
                            self._emit("consent", {"name": label, "mem_mb": mem,
                                                   "hidden_s": round(now - since)})
                    elif due:
                        exe = next((p for p in (api.exe_path(pid) for pid in pids) if p), None)
                        killed = sum(1 for pid in pids if api.terminate(pid))
                        ev = {"at": now, "name": label, "procs": killed,
                              "freed_mb": mem, "hidden_s": round(now - since), "exe": exe}
                        events.append(ev)
                        self.recent.append(ev)
                        self.hidden_since.pop(low, None)
                        row.update(state="quit", running=False)
                        log.info("trayquit: quit %s (%d procs, ~%d MB) after %ds in tray",
                                 label, killed, mem, ev["hidden_s"])
                        self._emit("quit", ev)
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

    def _busy_pct(self, pids: list, now: float) -> float | None:
        """Percent of one core these processes used since the previous check."""
        total, known = 0.0, False
        for pid in pids:
            cpu = self.api.cpu_s(pid)
            if cpu is None:
                continue
            prev = self._cpu_seen.get(pid)
            self._cpu_seen[pid] = (cpu, now)
            if prev and now > prev[1]:
                total += max(0.0, cpu - prev[0]) / (now - prev[1]) * 100.0
                known = True
        return round(total, 1) if known else None

    def _emit(self, kind: str, details: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, details)
        except Exception:
            log.exception("trayquit event hook failed")

    def reopen(self, name: str) -> dict:
        """Start again the app most recently quit under ``name``.

        The path comes only from this service's own quit record, so a request
        can never make it launch an arbitrary program.
        """
        with self.lock:
            ev = next((e for e in reversed(self.recent)
                       if e.get("name", "").lower() == str(name).lower()), None)
            if ev is None:
                raise ValueError(f"no recent auto-quit of '{name}' to undo")
            if not ev.get("exe"):
                raise ValueError(f"the executable of '{ev['name']}' was not recorded")
            ok = self.api.launch(ev["exe"])
            if ok:
                ev["reopened_at"] = self.clock()
                self._save()
            return {"name": ev["name"], "reopened": ok, "exe": ev["exe"]}

    def status(self) -> dict:
        with self.lock:
            day = self.clock() - 86400
            today = [e for e in self.recent if e["at"] >= day]
            pending = [a["name"] for a in self.last_view.get("apps", [])
                       if a.get("state") == "awaiting consent"]
            return {**self.settings, "execution_enabled": self.fix_enabled,
                    "awaiting_consent": pending,
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
            except Exception:
                log.exception("trayquit check failed")
            time.sleep(interval)
