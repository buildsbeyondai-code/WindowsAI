"""
Maintenance checks: system protection and repair, disk cleanup, the registry,
security and crashes, services and scheduled tasks, and app updates.

``maintenance.ps1`` gathers them read-only, in about 30 s: it walks cache
folders and the event logs. What it finds changes over days, not seconds, so
it runs in the background every ``SCAN_EVERY_S`` or when asked, never on the
sampler's 60 s cadence, and the page reads the cached report.

The fixes it leads to are ordinary ``fixes.py`` fixes, registered in
``optactions.py`` like every other change SysPerf can make; the detectors read
``snapshot["maintenance"]``, which only ``/api/fixes`` adds, so none of them is
ever chosen by the unattended loop. An app update runs one app at a time through
``upgrade_app`` and only for an id the last scan listed.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable

from .background_process import run_tree

log = logging.getLogger("sysperf.maintenance")

SCRIPT = Path(__file__).with_name("maintenance.ps1")
SCAN_EVERY_S = 6 * 3600
SCAN_TIMEOUT_S = 300.0
UPGRADE_TIMEOUT_S = 1800.0
DECLINED = 1223                # ERROR_CANCELLED: the UAC prompt was declined
PUBLISHER_ONLY = -1978334956   # 0x8A150114: the manifest denies winget upgrades
WINGET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
# Apps Components upgrades with its own tested upgrader (upgrader.py). winget is
# not allowed to swap them underneath SysPerf: a new Python, say, would leave
# SysPerf's own virtual environment pointing at an interpreter that has gone.
COMPONENT_MANAGED = re.compile(
    r"^(Python\.Python\..*|Microsoft\.PowerShell(\..*)?|Git\.Git|Microsoft\.WSL|"
    r"Microsoft\.Edge(\..*)?|Microsoft\.EdgeWebView2Runtime|Microsoft\.AppInstaller)$", re.I)

# Thresholds for turning measurements into findings.
RESTORE_MAX_AGE_DAYS = 30
BROWSER_CACHE_MB = 500
WU_CACHE_MB = 1024
TEMP_MB = 1024
SIGNATURE_MAX_AGE_DAYS = 7
UNEXPECTED_SHUTDOWNS = 3
# Task Scheduler results that are states, not failures: not yet run, running,
# terminated by the user, no more runs scheduled.
TASK_OK_RESULTS = {0, 0x41300, 0x41301, 0x41303, 0x41306, 0x41325}
BROWSER_IDS = ("msedge", "chrome", "brave", "firefox")


def ps_quote(text: str) -> str:
    """A PowerShell single-quoted literal."""
    return "'" + str(text).replace("'", "''") + "'"


def script_command(mode: str, **params) -> str:
    """The pwsh -Command text that runs maintenance.ps1 in ``mode``."""
    extra = "".join(f" -{k} {ps_quote(v)}" for k, v in params.items())
    return f"& {ps_quote(SCRIPT)} -Mode {mode}{extra}"


def _default_run(argv: list, timeout: float) -> tuple:
    try:
        rc, out, timed_out = run_tree(argv, timeout)
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    if timed_out:
        out += f"\ntimed out after {timeout:.0f} s; stopped it and the installers it started"
    return rc, out


def _section(rep: dict, name: str):
    """A probe section, or None when it is missing or reported an error."""
    v = rep.get(name)
    return None if v is None or (isinstance(v, dict) and "error" in v) else v


def findings(rep: dict, now: float) -> list:
    """What the report means: [{area, level, text, fix}], worst first.

    ``level`` is ``bad``, ``warn`` or ``info``. ``fix`` names the fixes.py fix
    that acts on it, or None.
    """
    out = []

    def add(area, level, text, fix=None):
        out.append({"area": area, "level": level, "text": text, "fix": fix})

    prot = _section(rep, "protection")
    if prot is not None:
        last = prot.get("last_restore_point")
        if not last:
            add("protection", "warn", "No restore point is recorded on this PC. Create one "
                "before changing drivers, the registry or system settings.", "create_restore_point")
        elif (now - last) / 86400 > RESTORE_MAX_AGE_DAYS:
            add("protection", "warn", f"The newest restore point is {int((now - last) / 86400)} "
                "days old.", "create_restore_point")
    crashes = _section(rep, "crashes")
    if crashes is not None:
        bsod = crashes.get("bugchecks_30d") or []
        power = crashes.get("unexpected_shutdowns_30d") or 0
        if bsod:
            codes = ", ".join(sorted({b.get("code") or "unknown" for b in bsod}))
            add("crashes", "bad", f"{len(bsod)} blue-screen crash(es) in 30 days (stop code "
                f"{codes}). Check the system files, then the drivers.", "repair_system_files")
        if power >= UNEXPECTED_SHUTDOWNS:
            add("crashes", "warn", f"{power} unexpected shutdowns in 30 days (power loss, a "
                "hang or a forced power-off).", "repair_system_files")
    clean = _section(rep, "cleanup")
    if clean is not None:
        items = {i.get("id"): i for i in clean.get("items") or []}
        browser = sum(items[b].get("mb") or 0 for b in BROWSER_IDS if b in items)
        if browser >= BROWSER_CACHE_MB:
            add("cleanup", "info", f"Browser caches hold {browser:,.0f} MB. Clearing them is safe: "
                "sign-ins, history and bookmarks stay.", "clear_browser_cache")
        wu = (items.get("wucache") or {}).get("mb") or 0
        if wu >= WU_CACHE_MB:
            add("cleanup", "info", f"Windows Update keeps {wu:,.0f} MB of downloaded updates.",
                "windows_update_cleanup")
        temp = (items.get("temp") or {}).get("mb") or 0
        if temp >= TEMP_MB:
            add("cleanup", "info", f"Your TEMP folder holds {temp:,.0f} MB.", "clear_temp")
    reg = _section(rep, "registry")
    if reg is not None:
        orphans = reg.get("orphans") or []
        mine = [o for o in orphans if o.get("user")]
        if mine:
            add("registry", "warn", f"{len(mine)} start-up or uninstall entr"
                f"{'y' if len(mine) == 1 else 'ies'} of yours point to a program that is no "
                "longer there.", "fix_registry_orphans")
        if len(orphans) > len(mine):
            add("registry", "info", f"{len(orphans) - len(mine)} machine-wide entr"
                f"{'y points' if len(orphans) - len(mine) == 1 else 'ies point'} to a missing "
                "program. Removing them needs an administrator (see the list).")
    sec = _section(rep, "security")
    if sec is not None:
        d = sec.get("defender") or {}
        others = [a for a in sec.get("antivirus") or []
                  if a.get("active") and "defender" not in str(a.get("name", "")).lower()]
        if d and not d.get("realtime") and not others:
            add("security", "bad", "Real-time virus protection is off and no other antivirus "
                "is active. Turn it on in Windows Security → Virus & threat protection.")
        elif d.get("realtime") and (d.get("signature_age_days") or 0) > SIGNATURE_MAX_AGE_DAYS:
            add("security", "warn", f"Defender's virus definitions are "
                f"{d['signature_age_days']} days old. Run Windows Update.")
        off = [f.get("name") for f in sec.get("firewall") or [] if not f.get("enabled")]
        if off:
            add("security", "bad", f"The firewall is off for the {', '.join(off)} network "
                "profile(s). Turn it on in Windows Security → Firewall & network protection.")
    tasks = _section(rep, "tasks")
    if tasks is not None:
        failing = [t for t in tasks if (t.get("last_result") or 0) not in TASK_OK_RESULTS]
        if failing:
            add("tasks", "info", f"{len(failing)} scheduled task(s) failed on their last run: "
                + ", ".join(t.get("name", "?") for t in failing[:3])
                + ("…" if len(failing) > 3 else "") + ".")
    apps = _section(rep, "app_updates")
    if apps is not None and apps.get("apps"):
        add("app_updates", "info", f"{len(apps['apps'])} app(s) have an update available.")
    rank = {"bad": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda f: rank[f["level"]])


class Maintenance:
    """The cached report, its background scan, and one-app updates."""

    def __init__(self, cache_file: Path, ps_exe: str | None, run: Callable = _default_run,
                 clock: Callable = time.time, backup_dir: Path | None = None):
        self.cache_file = Path(cache_file)
        self.backup_dir = Path(backup_dir) if backup_dir else self.cache_file.parent / "backups"
        self.ps_exe = ps_exe
        self.run = run
        self.clock = clock
        self._lock = threading.Lock()
        self.scanning = False
        self.job: dict | None = None
        self.last_error: str | None = None
        # 5.4 (restorepoint.py): a restore point before an app update, and the
        # newest one the helper knows of, which the event log may not record.
        self.before_change: Callable[[str], dict] | None = None
        self.restore_newest: Callable[[], float | None] | None = None
        # 5.4: apps update themselves after each scan while this says so (the
        # app wires it to the autonomy level); a failed version is not retried.
        self.auto_fn: Callable[[], bool] = lambda: False
        self.failed: dict = {}
        self.data_dir = self.cache_file.parent
        self.publisher_only: set = set()   # apps whose manifest denies winget upgrades
        self._auto_for = None             # the scan the last automatic pass followed
        try:
            self.data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    # ------------------------------------------------------------- scanning
    def due(self) -> bool:
        return self.clock() - (self.data.get("at") or 0) >= SCAN_EVERY_S

    def scan(self) -> dict:
        """Run the probe now (blocking). Keeps the previous report on failure."""
        if self.ps_exe is None:
            self.last_error = "no PowerShell on this platform"
            return self.data
        with self._lock:
            if self.scanning:
                return self.data
            self.scanning = True
        try:
            rc, out = self.run([self.ps_exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                                "Bypass", "-File", str(SCRIPT), "-Mode", "probe"], SCAN_TIMEOUT_S)
            line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("{")), "")
            try:
                data = json.loads(line)
            except ValueError:
                data = None
            if rc != 0 or not isinstance(data, dict):
                self.last_error = f"scan failed (rc {rc}): {out.strip()[-200:]}"
                return self.data
            self.data, self.last_error = data, None
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.cache_file)
            return data
        finally:
            self.scanning = False

    def scan_async(self) -> bool:
        if self.scanning:
            return False
        threading.Thread(target=self.scan, name="maintenance-scan", daemon=True).start()
        return True

    def loop(self, stop: threading.Event | None = None, tick_s: float = 300.0) -> None:
        """Background thread: rescan when the report is older than SCAN_EVERY_S,
        then update the apps it lists, once per scan, while auto_fn() allows."""
        stop = stop or threading.Event()
        while True:
            try:
                if self.due():
                    self.scan()
                if self.auto_fn() and self.data.get("at") and self._auto_for != self.data["at"]:
                    self._auto_for = self.data["at"]
                    self.auto_update()
            except Exception:           # pragma: no cover - the loop must survive anything
                log.exception("maintenance scan failed")
            if stop.wait(tick_s):
                return

    # --------------------------------------------------------------- report
    def report(self) -> dict:
        at = self.data.get("at")
        data = dict(self.data)
        known = self.restore_newest() if self.restore_newest else None
        prot = _section(data, "protection")
        if known and prot is not None and known > (prot.get("last_restore_point") or 0):
            data["protection"] = {**prot, "last_restore_point": known}
        return {**{k: v for k, v in data.items() if k != "at"},
                "at": at, "age_s": None if not at else round(self.clock() - at),
                "next_due": None if not at else at + SCAN_EVERY_S,
                "scanning": self.scanning, "error": self.last_error,
                "findings": findings(data, self.clock()) if at else [],
                "job": self.job, "every_s": SCAN_EVERY_S,
                "auto_update": bool(self.auto_fn()), "failed": dict(self.failed),
                "managed": sorted(i for i in self.upgradable() if COMPONENT_MANAGED.match(i)),
                "admin": self.admin_ids(), "publisher_only": sorted(self.publisher_only)}

    # ---------------------------------------------------------- app updates
    def upgradable(self) -> dict:
        apps = (_section(self.data, "app_updates") or {}).get("apps") or []
        return {a["id"]: a for a in apps if WINGET_ID.match(str(a.get("id", "")))}

    def start_upgrade(self, app_id: str, background: bool = True) -> dict:
        """Upgrade one app the last scan listed, with winget.

        The job is claimed under the lock before anything runs, so two requests
        cannot start two upgrades. ``background=False`` runs it inline (tests).
        """
        with self._lock:
            app = self.upgradable().get(app_id)
            if app is None:
                raise ValueError(f"{app_id!r} is not in the last scan's list of app updates")
            if self.job and self.job.get("state") == "running":
                raise ValueError(f"already upgrading {self.job['id']}")
            self.job = {"id": app_id, "name": app.get("name"), "from": app.get("current"),
                        "to": app.get("available"), "state": "running", "started": self.clock(),
                        "rollback": f"winget install --id {app_id} --exact --version "
                                    f"{app.get('current')} --force"}
        if not background:
            return self._upgrade(app_id)
        threading.Thread(target=self._upgrade, args=(app_id,), name="maintenance-upgrade",
                         daemon=True).start()
        return {"started": True, "id": app_id}

    def admin_ids(self) -> list:
        """Listed apps installed machine-wide: updating one needs an administrator."""
        return [i for i, a in self.upgradable().items()
                if a.get("scope") == "machine" and not COMPONENT_MANAGED.match(i)]

    def auto_update(self) -> list:
        """Update every app the last scan listed, one at a time.

        Skips the apps Components manages, any whose update to this version
        already failed, and machine-wide ones: their installer asks for an
        administrator, and unattended that prompt would wait, unanswered, for
        the whole timeout (``start_admin_batch`` updates them after one prompt).
        Stops if a manual update is running.
        """
        done = []
        for app_id, app in list(self.upgradable().items()):
            if (COMPONENT_MANAGED.match(app_id) or self.failed.get(app_id) == app.get("available")
                    or app.get("scope") == "machine"):
                continue
            try:
                job = self.start_upgrade(app_id, background=False)
            except ValueError:
                break
            if job["state"] in ("failed", "publisher"):
                self.failed[app_id] = app.get("available")
            if job["state"] == "publisher":
                self.publisher_only.add(app_id)
            done.append(job)
        return done

    def _upgrade(self, app_id: str) -> dict:
        if self.before_change:
            self.job["restore_point"] = self.before_change(f"updating {self.job['name']}")
        rc, out = self.run(["winget", "upgrade", "--id", app_id, "--exact", "--silent",
                            "--accept-package-agreements", "--accept-source-agreements",
                            "--disable-interactivity"], UPGRADE_TIMEOUT_S)
        state = "done" if rc == 0 else "failed"
        if rc != 0 and (rc == PUBLISHER_ONLY or "cannot be upgraded using WinGet" in out):
            # The manifest denies winget upgrades: the app updates itself.
            state = "publisher"
            out += "\nIts publisher does not allow winget to update it; use the app's own updater."
        if rc == 0 and self._still_outdated(app_id):
            # winget exits 0 when it has staged a Store/MSIX package that Windows
            # would not swap in because the app is open (0x80073D02).
            state = "waiting"
            out += "\nThe update is downloaded; Windows applies it once the app is closed. " \
                   "SysPerf tries again after the next scan."
        self.job.update(state=state, returncode=rc, output=out.strip()[-600:],
                        finished=self.clock())
        if state == "done":
            self._drop(app_id)
        return dict(self.job)

    def _drop(self, app_id: str) -> None:
        apps = self.data.get("app_updates") or {}
        apps["apps"] = [a for a in apps.get("apps") or [] if a.get("id") != app_id]

    def _still_outdated(self, app_id: str) -> bool:
        rc, out = self.run(["winget", "list", "--id", app_id, "--exact", "--upgrade-available",
                            "--accept-source-agreements", "--disable-interactivity"], 120)
        return rc == 0 and app_id in out

    # 5.4: machine-wide apps, all after one administrator prompt.
    def start_admin_batch(self, background: bool = True) -> dict:
        """Update every machine-wide app the last scan listed, elevated, in one go.

        Windows shows one UAC prompt; the elevated PowerShell runs winget for
        each id (each one checked against WINGET_ID by ``upgradable``) and
        writes ``id=rc`` lines that this process reads back.
        """
        with self._lock:
            ids = self.admin_ids()
            if not ids:
                raise ValueError("no machine-wide app needs an update")
            if self.ps_exe is None:
                raise ValueError("app updates need Windows")
            if self.job and self.job.get("state") == "running":
                raise ValueError(f"already upgrading {self.job['id']}")
            self.job = {"id": "machine-wide", "name": f"{len(ids)} machine-wide app(s)",
                        "ids": ids, "state": "running", "started": self.clock(),
                        "rollback": "winget install --id <id> --exact --version <old> --force"}
        if not background:
            return self._admin_batch(ids)
        threading.Thread(target=self._admin_batch, args=(ids,), name="maintenance-admin",
                         daemon=True).start()
        return {"started": True, "id": "machine-wide", "ids": ids}

    def _admin_batch(self, ids: list) -> dict:
        if self.before_change:
            self.job["restore_point"] = self.before_change(f"updating {len(ids)} machine-wide apps")
        res_file = self.data_dir / "admin_upgrade.txt"
        res_file.unlink(missing_ok=True)
        inner = "".join(
            f"winget upgrade --id {ps_quote(i)} --exact --silent --accept-package-agreements "
            f"--accept-source-agreements --disable-interactivity | Out-Null; "
            f"Add-Content -LiteralPath {ps_quote(res_file)} -Value ({ps_quote(i + '=')} + $LASTEXITCODE); "
            for i in ids)
        enc = base64.b64encode(inner.encode("utf-16-le")).decode()
        script = (f"try {{ $p = Start-Process -FilePath powershell.exe -ArgumentList "
                  f"'-NoProfile -ExecutionPolicy Bypass -EncodedCommand {enc}' -Verb RunAs -Wait "
                  f"-PassThru -WindowStyle Hidden; exit $p.ExitCode }} "
                  f"catch {{ Write-Output $_.Exception.Message; exit {DECLINED} }}")
        rc, out = self.run([self.ps_exe, "-NoProfile", "-NonInteractive", "-Command", script],
                           UPGRADE_TIMEOUT_S * len(ids))
        try:
            lines = res_file.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            lines = []
        results = {}
        for ln in lines:
            app_id, _, code = ln.strip().rpartition("=")
            if app_id in ids:
                results[app_id] = int(code) if code.lstrip("-").isdigit() else 1
        apps = self.upgradable()
        for app_id, code in results.items():       # a declined prompt marks none failed
            if code == 0:
                self._drop(app_id)
            else:
                self.failed[app_id] = (apps.get(app_id) or {}).get("available")
        ok = bool(results) and all(v == 0 for v in results.values()) and len(results) == len(ids)
        self.job.update(state="done" if ok else "failed", returncode=rc, results=results,
                        output=("the administrator (UAC) prompt was declined" if rc == DECLINED
                                else out.strip()[-600:]), finished=self.clock())
        return dict(self.job)
