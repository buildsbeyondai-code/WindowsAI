"""
Automatic System Restore points.

Windows lets only an administrator create a restore point, and SysPerf runs
without one. ``restore_point.ps1 -Mode install`` registers, once and after one
UAC prompt, a scheduled task that runs elevated as the signed-in user. From
then on SysPerf creates a restore point itself:

* before a change it makes to the system (the registry fix, an app update, a
  component upgrade) — ``ensure()``, called by those code paths;
* weekly, when the newest one is older than ``MAX_AGE_S`` — ``loop()``.

``ensure`` does nothing when a restore point under ``FRESH_S`` old exists (and
Windows itself keeps at most one per 24 hours). When the helper is not set up,
SysPerf asks for it with the UAC prompt the next time someone is at the PC,
at most once per ``PROMPT_EVERY_S``; a declined prompt never blocks the change
that needed it, which keeps its own backup (a .reg export, the previous
installer).

In the Store app this is opt-in like every feature that acts outside the app
(store_mode.py); a scripted install starts with it on.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("sysperf.restorepoint")

SCRIPT = Path(__file__).with_name("restore_point.ps1")
TASK = "SysPerf Restore Point"
FRESH_S = 24 * 3600            # a restore point this recent covers the next change
MAX_AGE_S = 7 * 86400          # weekly safety net
PROMPT_EVERY_S = 24 * 3600     # ask for the UAC prompt at most once a day
REQUEST_TIMEOUT_S = 300.0
DECLINED = 1223                # ERROR_CANCELLED: the UAC prompt was declined


def default_dir() -> Path:
    return Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData") / "SysPerf" / "restore"


class RestorePoints:
    def __init__(self, state_file: Path, ps_exe: str | None, run: Callable,
                 default_auto: bool = True, status_dir: Path | None = None,
                 clock: Callable = time.time):
        self.file = Path(state_file)
        self.ps_exe = ps_exe
        self.run = run                       # run(argv, timeout) -> (rc, text)
        self.clock = clock
        self.status_file = Path(status_dir or default_dir()) / "status.json"
        self._lock = threading.Lock()
        self.last: dict | None = None        # the latest ensure() outcome
        self._installed: tuple | None = None  # (exists, when looked up)
        try:
            st = json.loads(self.file.read_text())
            self.state = st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("auto", bool(default_auto))

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.file)

    # ---------------------------------------------------------------- facts
    @property
    def auto(self) -> bool:
        return bool(self.state.get("auto"))

    def set_auto(self, on: bool) -> bool:
        self.state["auto"] = bool(on)
        self._save()
        return self.auto

    def status(self) -> dict:
        """What the elevated helper last wrote, or {} before it ever ran."""
        try:
            st = json.loads(self.status_file.read_text(encoding="utf-8-sig"))
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            return {}

    def installed(self) -> bool:
        """Whether the helper task exists (looked up at most once a minute)."""
        if self.ps_exe is None:
            return False
        now = self.clock()
        if self._installed is None or now - self._installed[1] >= 60:
            rc, _ = self.run(["schtasks.exe", "/query", "/tn", TASK], 60)
            self._installed = (rc == 0, now)
        return self._installed[0]

    def newest(self) -> float | None:
        return self.status().get("newest")

    def describe(self) -> dict:
        st, installed = self.status(), self.installed()
        newest = st.get("newest")
        return {"auto": self.auto, "installed": installed, "newest": newest,
                "age_s": None if not newest else round(self.clock() - newest),
                "count": st.get("count"), "message": st.get("message"), "ok": st.get("ok"),
                "checked_at": st.get("ts"), "last": self.last,
                "last_prompt": self.state.get("last_prompt"),
                "declined": self.state.get("declined", 0),
                "fresh_s": FRESH_S, "max_age_s": MAX_AGE_S}

    # --------------------------------------------------------------- set-up
    def _ps(self, *args: str) -> list:
        return [self.ps_exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(SCRIPT), *args]

    def _elevated(self, mode: str, *args: str) -> tuple:
        """Run restore_point.ps1 elevated: Windows shows the UAC prompt.

        Start-Process joins -ArgumentList without quoting, and both the profile
        path and many account names contain spaces, so one quoted string is passed.
        """
        line = " ".join(f'"{a}"' if " " in a else a for a in
                        ("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                         "-Mode", mode, *args))
        lit = "'" + line.replace("'", "''") + "'"
        script = (f"try {{ $p = Start-Process -FilePath powershell.exe -ArgumentList {lit} "
                  "-Verb RunAs -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode } "
                  f"catch {{ Write-Output $_.Exception.Message; exit {DECLINED} }}")
        self._installed = None
        return self.run([self.ps_exe, "-NoProfile", "-NonInteractive", "-Command", script], 600)

    def setup(self, actor: str = "user") -> dict:
        """Register the elevated helper (one UAC prompt). Returns the outcome."""
        if self.ps_exe is None:
            return {"ok": False, "reason": "restore points need Windows"}
        self.state["last_prompt"] = self.clock()
        user = (os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")).strip("\\")
        rc, out = self._elevated("install", "-User", user)
        if rc == 0 and self.installed():
            self.state["declined"] = 0
            self._save()
            return {"ok": True, "reason": "set up", "by": actor}
        if rc == DECLINED:
            self.state["declined"] = self.state.get("declined", 0) + 1
        self._save()
        return {"ok": False, "reason": ("the administrator (UAC) prompt was declined"
                                        if rc == DECLINED else f"set-up failed (rc {rc}): "
                                        f"{out.strip()[-200:]}"), "by": actor}

    def remove(self) -> dict:
        rc, out = self._elevated("uninstall")
        if rc == 0:
            self.set_auto(False)
        return {"ok": rc == 0, "reason": "removed" if rc == 0 else
                ("the administrator (UAC) prompt was declined" if rc == DECLINED
                 else f"rc {rc}: {out.strip()[-200:]}")}

    # -------------------------------------------------------------- create
    def create(self, reason: str) -> dict:
        """Ask the helper for a restore point now and wait for its answer."""
        rc, out = self.run(self._ps("-Mode", "request"), REQUEST_TIMEOUT_S)
        line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("{")), "")
        try:
            st = json.loads(line)
        except ValueError:
            st = {}
        ok = rc == 0 and bool(st.get("ok"))
        return {"ok": ok, "created": bool(st.get("created")), "reason": reason,
                "message": st.get("message") or out.strip()[-200:] or f"rc {rc}",
                "newest": st.get("newest"), "at": self.clock()}

    def ensure(self, reason: str, may_prompt: bool = True) -> dict:
        """A restore point no older than FRESH_S before a change. Never raises.

        Returns {"ok", "state", ...}; state is ``recent``, ``created``, ``off``,
        ``not_set_up`` or ``failed``. Callers go ahead either way: each change
        keeps its own backup, so a missing restore point is reported, not fatal.
        """
        with self._lock:
            try:
                out = self._ensure(reason, may_prompt)
            except Exception as exc:             # a helper problem must not stop the change
                log.warning("restore point before %s failed: %s", reason, exc)
                out = {"ok": False, "state": "failed", "reason": reason,
                       "message": f"{type(exc).__name__}: {exc}"}
            self.last = {**out, "at": self.clock()}
            return out

    def _ensure(self, reason: str, may_prompt: bool) -> dict:
        if not self.auto or self.ps_exe is None:
            return {"ok": False, "state": "off", "reason": reason,
                    "message": "automatic restore points are off"}
        newest = self.newest()
        if newest and self.clock() - newest < FRESH_S:
            return {"ok": True, "state": "recent", "reason": reason, "newest": newest,
                    "message": "a restore point from the last 24 hours covers this change"}
        if not self.installed():
            due = self.clock() - (self.state.get("last_prompt") or 0) >= PROMPT_EVERY_S
            if not (may_prompt and due) or not self.setup(actor="auto")["ok"]:
                return {"ok": False, "state": "not_set_up", "reason": reason,
                        "message": "the restore-point helper is not set up yet (one "
                                   "administrator prompt sets it up)"}
        res = self.create(reason)
        return {**res, "state": "created" if res["ok"] else "failed"}

    # ---------------------------------------------------------------- loop
    def loop(self, present: Callable[[], bool] = lambda: True,
             stop: threading.Event | None = None, tick_s: float = 3600.0) -> None:
        """Weekly safety net, plus the one-time set-up while someone is at the PC."""
        stop = stop or threading.Event()
        while True:
            newest = self.newest()
            if self.auto and (not newest or self.clock() - newest >= MAX_AGE_S):
                self.ensure("the weekly safety net", may_prompt=present())
            if stop.wait(tick_s):
                return
