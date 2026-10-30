"""
Structured, allowlisted Windows optimisation actions and the autonomy levels.

Every remediation SysPerf can run is described here as an ``ActionSpec``:
identifier, description, preconditions, risk, required permissions, expected
benefit, execution method, validation method and rollback method. Execution
always goes through an existing ``fixes.py`` handler — this module never builds
a command string, and nothing an LLM writes can become one.

Autonomy levels (the master switch):

* ``off``          — no autonomous behaviour at all.
* ``observe``      — default. Monitor and recommend; nothing is changed.
* ``safe_auto``    — the loop may run actions that are low-risk, reversible and
                     **individually enabled by the user**.
* ``self_evolve``  — ``safe_auto`` plus source-code self-evolution (patches are
                     generated and validated in isolation; never deployed
                     without explicit approval).

Hard limits that no level, setting or evolved code can lift are in ``FORBIDDEN``
and ``never_auto()``.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

LEVELS = ("off", "observe", "safe_auto", "self_evolve")

# Anything touching these is refused outright, whoever asks.
FORBIDDEN = re.compile(
    r"defender|antivirus|\bmpcmdrun\b|set-mppreference|firewall|netsh\s+advfirewall|"
    r"wuauserv|windows\s*update|usosvc|eventlog|wevtutil|auditpol|\bbackup\b|wbadmin|"
    r"vss|shadowcopy|sense\b|securityhealth|bitlocker|uac|enablelua|"
    r"remove-item\s+.*-recurse|format-volume|\bdiskpart\b|bcdedit|"
    r"reg(\.exe)?\s+delete|hklm\\\\system|stop-process\s+.*(csrss|lsass|winlogon|svchost|smss|wininit)",
    re.I)


@dataclass(frozen=True)
class ActionSpec:
    id: str
    description: str
    preconditions: str
    risk: str                 # low | medium | high
    reversible: bool
    permissions: str          # user | admin
    metric: str | None     # metric the action is expected to improve
    higher_is_better: bool
    expected_benefit: str
    execution: str            # handler that runs it
    validation: str
    rollback: str

    @property
    def auto_eligible(self) -> bool:
        return self.risk == "low" and self.reversible


def _spec(id, desc, pre, risk, rev, perm, metric, hib, benefit, validation, rollback):
    return ActionSpec(id, desc, pre, risk, rev, perm, metric, hib, benefit,
                      f"fixes.FixEngine.apply(['{id}'])", validation, rollback)


# One spec per fixes.py remediation. Risk here is the 4.0 policy, which is
# stricter than fixes.py's own 'safe/caution' where data or settings persist.
REGISTRY = {s.id: s for s in (
    _spec("flush_dns", "Flush the DNS resolver cache", "DNS failures or stale entries",
          "low", True, "user", "net_dns_ms", False, "lower DNS latency",
          "DNS latency on next samples", "none needed — the cache rebuilds itself"),
    _spec("trim_working_sets", "Trim working sets of idle background apps",
          "committed memory above target", "low", True, "user", "available_mb", True,
          "more available RAM", "available_mb over the next samples",
          "none needed — pages fault back in on demand"),
    _spec("clear_standby", "Empty the memory standby list via the elevated broker",
          "low available memory, broker online", "low", True, "admin", "available_mb", True,
          "more free RAM", "available_mb over the next samples",
          "none needed — the cache refills from disk"),
    _spec("background_cpu_hog", "Lower the priority of a sustained background CPU hog",
          "one process holding CPU for several intervals", "medium", True, "user",
          "cpu_pct", False, "lower CPU utilisation", "cpu_pct over the next samples",
          "restore Normal priority (or restart the process)"),
    _spec("clear_temp", "Delete files from the user TEMP folder", "low free disk space",
          "medium", False, "user", "c_free_gb", True, "more free disk",
          "c_free_gb", "irreversible — deleted files are not recoverable"),
    _spec("empty_recycle_bin", "Empty the Recycle Bin", "low free disk space",
          "high", False, "user", "c_free_gb", True, "more free disk", "c_free_gb",
          "irreversible — permanently deletes the user's deleted files"),
    _spec("restart_explorer", "Restart the Windows shell (explorer.exe)",
          "explorer leaking handles/memory", "high", True, "user", "handles", False,
          "fewer handles", "handles", "explorer restarts itself; open windows close"),
    _spec("restart_wsl", "Shut down and restart WSL", "WSL VM memory bloat", "high", True,
          "user", "wsl_vm_mb", False, "less VM memory", "wsl_vm_mb",
          "WSL restarts on next use; running Linux processes are lost"),
    _spec("trim_startup", "Disable heavy startup entries", "slow boot", "high", True,
          "user", "boot_time_s", False, "faster boot", "boot_time_s after reboot",
          "re-enable the entries in Task Manager > Startup"),
    _spec("optimize_wsl_boot", "Tune WSL boot configuration", "slow WSL start", "high",
          True, "user", "wsl_start_s", False, "faster WSL start", "wsl_start_s",
          "restore the previous .wslconfig"),
    _spec("enable_sysmain", "Enable the SysMain (Superfetch) service", "slow app launches",
          "medium", True, "admin", "word_launch_s", False, "faster app launches",
          "launch times", "Set-Service SysMain -StartupType Disabled"),
    _spec("trim_office_addins", "Disable slow-loading Office add-ins", "slow Office start",
          "high", True, "user", "word_launch_s", False, "faster Word start",
          "word_launch_s", "re-enable the add-ins in File > Options > Add-ins"),
    # 5.3: the maintenance scan's fixes; each runs only on a confirmed request.
    _spec("clear_browser_cache", "Empty the cache folders of closed browsers",
          "browser caches above 500 MB", "medium", False, "user", "c_free_gb", True,
          "more free disk", "cache sizes on the next maintenance scan",
          "none needed — the cache refills while browsing"),
    _spec("fix_registry_orphans", "Remove the user's start-up and uninstall entries whose "
          "program is gone, after exporting each key", "an entry points to a missing program",
          "medium", True, "user", None, False, "a clean start-up and uninstall list",
          "the entries are gone on the next maintenance scan",
          "reg import the .reg backup the result names (data\\registry-backups)"),
    _spec("create_restore_point", "Create a System Restore point (manual, administrator)",
          "no recent restore point", "medium", True, "admin", None, False,
          "a way back before risky changes", "a new restore point on the next scan",
          "none needed — a restore point changes nothing"),
    _spec("repair_system_files", "Run DISM RestoreHealth and SFC (manual, administrator)",
          "blue screens or repeated unexpected shutdowns", "medium", True, "admin", None,
          False, "repaired Windows files", "fewer crashes over the following weeks",
          "none needed — only Microsoft's original files are restored"),
    _spec("windows_update_cleanup", "Remove superseded update files (manual, administrator)",
          "Windows Update cache above 1 GB", "high", False, "admin", "c_free_gb", True,
          "more free disk", "c_free_gb",
          "irreversible — installed updates can no longer be uninstalled"),
    ActionSpec("winget_upgrade_app", "Update one app the maintenance scan listed, with winget",
               "winget lists a newer version", "medium", True, "user", None, False,
               "a current app", "maintenance.Maintenance.start_upgrade(app_id)",
               "the app leaves the update list on the next scan",
               "winget install --id <id> --exact --version <previous> --force"),
)}


def never_auto(text: str) -> bool:
    """True if a proposed action touches something SysPerf must never automate."""
    return bool(FORBIDDEN.search(text or ""))


class ActionPolicy:
    """Autonomy level + per-action opt-in + execution history and verification."""

    SETTLE_S = 600            # wait this long before judging an action's effect
    NOISE_FRACTION = 0.03     # changes smaller than 3 % count as "no change"

    def __init__(self, state_file: Path, db=None, improver_mode: str | None = None,
                 clock=time.time):
        self.file = Path(state_file)
        self.db = db
        self.clock = clock
        self._lock = threading.Lock()
        self.state = self._load(improver_mode)

    def _load(self, improver_mode):
        try:
            st = json.loads(self.file.read_text())
            if st.get("level") in LEVELS:
                st.setdefault("enabled", [])
                return st
        except (OSError, ValueError):
            pass
        # First run of 4.0. A 3.x install whose user had chosen "auto" keeps
        # unattended execution, but only for the actions 4.0 deems low-risk and
        # reversible; everything else now needs an explicit opt-in.
        if improver_mode == "auto":
            st = {"level": "safe_auto",
                  "enabled": sorted(a for a, s in REGISTRY.items() if s.auto_eligible),
                  "migrated_from": "3.x auto mode"}
        else:
            st = {"level": "observe", "enabled": []}
        self._save(st)
        if self.db:
            self.db.audit("system", "autonomy.init", st)
        return st

    def _save(self, st=None):
        st = st or self.state
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(self.file)

    @property
    def level(self) -> str:
        return self.state["level"]

    def set_level(self, level: str, actor: str = "user") -> str:
        if level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}")
        with self._lock:
            old = self.state["level"]
            self.state["level"] = level
            self._save()
        if self.db:
            self.db.audit(actor, "autonomy.level", {"from": old, "to": level})
        return level

    def set_enabled(self, action_id: str, enabled: bool, actor: str = "user") -> list:
        spec = REGISTRY.get(action_id)
        if spec is None:
            raise ValueError(f"unknown action {action_id}")
        if enabled and not spec.auto_eligible:
            raise ValueError(f"{action_id} is {spec.risk}-risk/"
                             f"{'reversible' if spec.reversible else 'irreversible'}: it can "
                             "only run with confirmation, never unattended")
        with self._lock:
            cur = set(self.state["enabled"])
            (cur.add if enabled else cur.discard)(action_id)
            self.state["enabled"] = sorted(cur)
            self._save()
        if self.db:
            self.db.audit(actor, "autonomy.action", {"id": action_id, "enabled": enabled})
        return self.state["enabled"]

    def improver_mode(self) -> str:
        """The autoimprove mode implied by the autonomy level."""
        return {"off": "off", "observe": "observe"}.get(self.level, "auto")

    def unattended_block(self, action_id: str) -> str | None:
        """None if the loop may run ``action_id`` unattended now, else why not."""
        spec = REGISTRY.get(action_id)
        if self.level in ("off", "observe"):
            return f"autonomy level is '{self.level}'"
        if spec is None:
            return "not in the 4.0 action registry"
        if not spec.auto_eligible:
            return (f"{spec.risk}-risk/{'reversible' if spec.reversible else 'irreversible'}"
                    " action needs confirmation")
        if action_id not in self.state["enabled"]:
            return "not enabled for unattended execution (Self-Evolution tab)"
        return None

    # ------------------------------------------------------------ history
    def record_executions(self, ids, results, snapshot) -> None:
        if not self.db:
            return
        latest = (snapshot or {}).get("latest") or {}
        for res in results or []:
            aid = res.get("id")
            spec = REGISTRY.get(aid)
            metric = spec.metric if spec else None
            before = latest.get(metric) if metric else None
            self.db.record_action(
                action_id=aid or "?", mode=self.level, authorized=1, reason="unattended",
                # FixEngine.apply reports {"status": "ok"}; accept a bare "ok" too.
                ok=1 if (res.get("status") == "ok" or res.get("ok") is True) else 0,
                metric=metric,
                before=before if isinstance(before, (int, float)) else None,
                output=str(res.get("output") or res.get("error") or "")[:2000])

    def verify_pending(self, snapshot) -> list:
        """Judge settled runs against the current measurement (before vs after)."""
        if not self.db:
            return []
        latest = (snapshot or {}).get("latest") or {}
        judged = []
        for run in self.db.action_runs(limit=200):
            if run["verdict"] or run["before"] is None or not run["ok"]:
                continue
            if self.clock() - run["ts"] < self.SETTLE_S:
                continue
            after = latest.get(run["metric"])
            if not isinstance(after, (int, float)):
                continue
            spec = REGISTRY.get(run["action_id"])
            delta = after - run["before"]
            rel = abs(delta) / max(abs(run["before"]), 1e-9)
            better = (delta > 0) == bool(spec and spec.higher_is_better)
            verdict = ("no_change" if rel < self.NOISE_FRACTION
                       else "improved" if better else "worse")
            self.db.update_action(run["id"], after=after, verdict=verdict)
            judged.append({"id": run["id"], "action": run["action_id"], "verdict": verdict})
        return judged

    def effectiveness(self) -> list:
        """Per-action measured outcome counts — only from recorded before/after."""
        if not self.db:
            return []
        agg = {}
        for r in self.db.action_runs(limit=5000):
            a = agg.setdefault(r["action_id"], {"id": r["action_id"], "runs": 0, "ok": 0,
                                                "improved": 0, "no_change": 0, "worse": 0,
                                                "deltas": []})
            a["runs"] += 1
            a["ok"] += r["ok"] or 0
            if r["verdict"] in ("improved", "no_change", "worse"):
                a[r["verdict"]] += 1
                if r["before"] not in (None, 0) and r["after"] is not None:
                    a["deltas"].append(100.0 * (r["after"] - r["before"]) / abs(r["before"]))
        out = []
        for a in agg.values():
            d = sorted(a.pop("deltas"))
            a["median_change_pct"] = round(d[len(d) // 2], 2) if d else None
            a["judged"] = a["improved"] + a["no_change"] + a["worse"]
            out.append(a)
        return sorted(out, key=lambda x: -x["runs"])

    def describe(self) -> dict:
        return {"level": self.level, "levels": list(LEVELS),
                "enabled": list(self.state["enabled"]),
                "migrated_from": self.state.get("migrated_from"),
                "actions": [{**asdict(s), "auto_eligible": s.auto_eligible,
                             "enabled": s.id in self.state["enabled"]}
                            for s in REGISTRY.values()]}
