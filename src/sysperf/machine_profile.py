"""
Per-machine baseline and targets.

Until this module existed the scorecard graded every installation against the
numbers of the laptop SysPerf was developed on: CRITERIA carried that machine's
measured baselines (14,000 MB app commit, 164.5 GB free on C:) and goals sized
for its 32 GB of RAM (12,000 MB available, a 10 GB WSL cap). On any other device
the rows were unreachable or trivially met, the baseline date was the developer's,
and the dashboard read as if it were describing a different computer.

Now the first settled stretch of samples on THIS machine becomes its baseline,
and the targets are derived from this machine's own capacity using the fractions
in the ``targets`` block of sysperf.config.json. The result is persisted in
``baseline.json`` together with the machine's name, so:

* a fresh install captures its own baseline (START-HERE: "progress is measured
  from where YOU started");
* a data directory that came from another computer (copied, restored, or synced
  through OneDrive) is detected, archived under ``archive/``, and the new machine
  starts clean instead of displaying the old one's history.

CRITERIA is updated in place, so every module that imported the list sees the
per-machine values without being rewired.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config as _cfg

log = logging.getLogger("perfdash")

PROFILE_FILE = _cfg.DATA_DIR / "baseline.json"
ARCHIVE_DIR = _cfg.DATA_DIR / "archive"
PROFILE_VERSION = 3   # 2: targets from capacity/floors; 3: net_remote ceiling

# Samples needed before a baseline is trusted. One reading is noise; a handful of
# consecutive samples gives a median that reflects the machine's normal state.
CAPTURE_MIN_SAMPLES = 5

# Everything that describes a particular machine's measurements or learned
# behaviour. Settings (sysperf.config.json), secrets and the LLM store are kept.
STATE_FILES = ("history.jsonl", "adaptive_state.json", "trends_daily.json",
               "autoimprove_state.json", "autoimprove_log.jsonl",
               "auto_targets.json", "actions_state.json", "evolve_state.json",
               "procwatch_state.json", "baseline.json",
               # 4.1: SQLite history (with its WAL files), the migrated JSONL, inbox
               "history.db", "history.db-wal", "history.db-shm",
               "history.jsonl.migrated", "notifications.json")

# Fractions of this machine's capacity. Mirrors sysperf.config.example.json.
TARGET_DEFAULTS = {
    "commit_of_ram": 0.75,
    "commit_improve": 0.85,
    "compression_of_ram": 0.05,
    "available_of_ram": 0.3,
    "available_improve": 1.25,
    "handles_improve": 0.85,
    "disk_free_fraction": 0.15,
    "startup_apps_max": 10,
    "startup_apps_improve": 0.55,
    "net_remote_floor": 25,
    "net_remote_improve": 0.7,
    "browser_improve": 0.75,
    "browser_extra_headroom": 4,
    "wsl_cap_of_ram": 0.35,
}

# The shipped CRITERIA, before any machine's values are applied. Kept so a
# re-capture always derives from the generic defaults, never from a previous
# machine's numbers.
DEFAULT_CRITERIA = list(_cfg.CRITERIA)

# Rows whose baseline AND target only make sense relative to this machine's
# capacity. Until the baseline is captured they are reported as "measuring"
# rather than graded against defaults sized for some other computer.
MACHINE_KEYS = frozenset({"committed_app_mb", "compression_mb", "available_mb",
                          "vmmem_mb", "browser_procs", "handles", "c_free_gb",
                          "net_remote", "startup_apps", "hw_faults"})


def machine_id() -> str:
    """A stable name for this computer."""
    name = os.environ.get("COMPUTERNAME") or socket.gethostname() or "unknown"
    return name.strip().upper()


def _fractions() -> dict:
    user = _cfg.SETTINGS.get("targets")
    out = dict(TARGET_DEFAULTS)
    if isinstance(user, dict):
        for k, v in user.items():
            if k in out and isinstance(v, (int, float)):
                out[k] = v
    return out


def _median(vals: list):
    vals = sorted(v for v in vals if isinstance(v, (int, float)))
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


# ------------------------------------------------------------ persistence ---
def load(path: Path | None = None) -> dict | None:
    path = path or PROFILE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(profile: dict, path: Path | None = None) -> None:
    path = path or PROFILE_FILE
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not save %s: %s", path.name, exc)


def _db_owner(path: Path) -> str | None:
    """The ``_host`` of the newest sample in a 4.1 SQLite history, read-only."""
    if not path.is_file():
        return None
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT data FROM samples ORDER BY t DESC LIMIT 1").fetchone()
        finally:
            con.close()
        host = json.loads(row[0]).get("_host") if row else None
    except (sqlite3.Error, ValueError, AttributeError):
        return None
    return str(host).upper() if host else None


def _history_owner(data_dir: Path) -> str | None:
    """The machine that wrote the newest stamped history sample, if any."""
    owner = _db_owner(data_dir / "history.db")
    if owner:
        return owner
    path = data_dir / "history.jsonl"
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 200_000))
            tail = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        try:
            host = json.loads(line).get("_host")
        except (ValueError, AttributeError):
            continue
        if host:
            return str(host).upper()
    return None


def archive_foreign_state(data_dir: Path, owner: str) -> Path | None:
    """Move another machine's state out of the way. Nothing is deleted."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = data_dir / "archive" / f"{owner}-{stamp}"
    moved = 0
    for name in STATE_FILES:
        src = data_dir / name
        if src.exists():
            try:
                dest.mkdir(parents=True, exist_ok=True)
                os.replace(src, dest / name)
                moved += 1
            except OSError as exc:
                log.warning("could not archive %s: %s", name, exc)
    if moved:
        log.warning("data directory belonged to %s; archived %d file(s) to %s",
                    owner, moved, dest)
        return dest
    return None


# --------------------------------------------------------------- derivation ---
def targets_for(base: dict, cap: dict, f: dict | None = None) -> dict:
    """Targets from this machine's capacity, floors and starting point.

    A metric that already sits inside the healthy range for this machine is asked
    to STAY there (target = the capacity goal) rather than to improve on a
    number that is already fine: demanding 15% less than a 130 MB compression
    store on an 8 GB laptop is not a goal, it is a permanent failure. A metric
    outside the range gets the capacity goal as its target.
    """
    f = f or _fractions()
    ram = cap.get("total_ram_mb") or 0
    disk = cap.get("c_size_gb") or 0
    t: dict = {}

    def lower(key, goal):
        if key in base:
            t[key] = max(0, round(goal(base[key])))

    lower("committed_app_mb", lambda b: ram * f["commit_of_ram"])
    lower("compression_mb", lambda b: ram * f["compression_of_ram"])
    lower("handles", lambda b: b * f["handles_improve"])
    # Browser processes cannot go below what mandatory/in-use hosts plus a
    # minimal Edge need (browser.py's realistic floor); a target under it is
    # unreachable without closing apps the user is using.
    floor = cap.get("browser_floor") or 0
    lower("browser_procs", lambda b: max(1, floor, b * f["browser_improve"]))
    # Under the healthy ceiling already: stay under it (not under the start).
    lower("net_remote",
          lambda b: f["net_remote_floor"] if b <= f["net_remote_floor"]
          else max(f["net_remote_floor"], b * f["net_remote_improve"]))
    lower("startup_apps",
          lambda b: min(b, f["startup_apps_max"], max(1, b * f["startup_apps_improve"])))
    lower("hw_faults", lambda b: 0)
    # Processor Queue Length is only a bottleneck when it exceeds the number of
    # logical processors; a fixed "1" was sized for nothing in particular.
    cpus = cap.get("logical_cpus") or 0
    if cpus and "cpu_queue" in base:
        t["cpu_queue"] = cpus
    # vmmem is a ceiling, not a journey: the cap is a share of this machine's RAM.
    if ram:
        t["vmmem_mb"] = round(ram * f["wsl_cap_of_ram"])
    if "available_mb" in base and ram:
        t["available_mb"] = round(ram * f["available_of_ram"])
    if "c_free_gb" in base:
        goal = disk * f["disk_free_fraction"] if disk else base["c_free_gb"] * 1.1
        t["c_free_gb"] = round(goal, 1)
    return t


def derive(history: list[dict], fractions: dict | None = None) -> dict | None:
    """Baselines and targets for this machine from its own samples.

    Returns None until there are enough samples carrying the capacity fields.
    """
    from .browser import browser_breakdown  # lazy: browser imports config
    f = fractions or _fractions()
    samples = [s for s in history if s.get("total_ram_mb")]
    if len(samples) < CAPTURE_MIN_SAMPLES:
        return None
    # hw_faults is computed by the scorer, not collected; derive it the same way.
    samples = [dict(s, hw_faults=(s.get("hw_problem_devices") or 0)
                    + (s.get("hw_disks_unhealthy") or 0))
               if s.get("hw_problem_devices") is not None else s for s in samples]

    def med(key):
        return _median([s.get(key) for s in samples])

    ram = med("total_ram_mb")
    disk = _median([s.get("c_size_gb") for s in samples]) or 0
    floor = _median([browser_breakdown(s)["realistic_floor"] for s in samples
                     if s.get("wv2_hosts") is not None])
    cap = {"total_ram_mb": round(ram), "c_size_gb": round(disk, 1),
           "browser_floor": round(floor) if floor is not None else None,
           "logical_cpus": (os.cpu_count() if _cfg.PLATFORM == "windows" else None)}

    base: dict = {}
    for key in ("committed_app_mb", "compression_mb", "handles", "browser_procs",
                "net_remote", "startup_apps", "hw_faults", "available_mb"):
        b = med(key)
        if b is not None:
            base[key] = round(b)
    vm = med("vmmem_mb")
    base["vmmem_mb"] = round(vm) if vm is not None else 0
    free = med("c_free_gb")
    if free is not None:
        base["c_free_gb"] = round(free, 1)

    # Generic rows (CPU, disk activity, launch times...) keep their default goal,
    # but count progress from this machine's own starting point when it is worse.
    for key, _l, dbase, dtgt, hib, _u in DEFAULT_CRITERIA:
        if key in base:
            continue
        b = med(key)
        if b is None:
            continue
        worse = (b < dtgt) if hib else (b > dtgt)
        base[key] = round(b, 1) if worse else dbase

    return {
        "version": PROFILE_VERSION,
        "machine": machine_id(),
        "captured": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "samples": len(samples),
        "capacity": cap,
        "reference": {k: (round(v) if v is not None else None) for k, v in (
            ("pool_paged_mb", med("pool_paged_mb")),
            ("pool_nonpaged_mb", med("pool_nonpaged_mb")))},
        "baselines": base,
        "targets": targets_for(base, cap, f),
    }


def live_targets(latest: dict) -> dict:
    """Targets that follow what is running now rather than a captured number.

    Browser processes: the floor of what cannot be closed right now (Windows'
    own WebView2 hosts, apps marked in-use, a minimal Edge) plus a small
    allowance. A fixed target captured while one set of apps was open scored 0%
    as soon as a different set was, even with nothing discretionary running.
    """
    if not latest.get("wv2_hosts") and latest.get("edge_procs") is None:
        return {}
    from .browser import browser_breakdown  # lazy: browser imports config
    floor = browser_breakdown(latest).get("realistic_floor")
    if not floor:
        return {}
    return {"browser_procs": round(floor + _fractions()["browser_extra_headroom"])}


# ------------------------------------------------------------------ apply ---
_current: dict = {}


def current() -> dict:
    """The profile in effect, or {} while the baseline is still being captured."""
    return dict(_current)


def apply(profile: dict | None) -> None:
    """Make CRITERIA and the derived constants describe this machine."""
    global _current
    if not profile:
        _cfg.CRITERIA[:] = DEFAULT_CRITERIA
        _cfg.PROFILE_PENDING = MACHINE_KEYS
        _current = {}
    else:
        b, t = profile.get("baselines", {}), profile.get("targets", {})
        _cfg.CRITERIA[:] = [(k, lbl, b.get(k, base), t.get(k, tgt), hib, unit)
                            for k, lbl, base, tgt, hib, unit in DEFAULT_CRITERIA]
        _cfg.BASELINE_DATE = profile.get("captured", _cfg.BASELINE_DATE)
        # Slow-tier rows (only in the periodic full sweep) may not have data yet.
        _cfg.PROFILE_PENDING = MACHINE_KEYS - set(b)
        if t.get("vmmem_mb"):
            _cfg.VMMEM_CAP_MB = t["vmmem_mb"]
        _current = dict(profile)
    # Modules that built lookup tables from CRITERIA at import time.
    trends = sys.modules.get(__package__ + ".trends")
    if trends is not None and hasattr(trends, "refresh_criteria"):
        trends.refresh_criteria()


def maybe_capture(history: list[dict]) -> bool:
    """Capture this machine's baseline once enough samples exist."""
    mine_already = _current.get("machine") == machine_id()
    outdated = mine_already and _current.get("version", 1) < PROFILE_VERSION
    if mine_already and not _cfg.PROFILE_PENDING and not outdated:
        return False
    mine = [s for s in history if (s.get("_host") or machine_id()) == machine_id()]
    profile = derive(mine)
    if profile is None:
        return False
    if outdated:
        # Older profile: keep its captured baselines (progress must not reset),
        # recompute only the targets with the current rules.
        merged = dict(_current)
        base = {**profile["baselines"], **_current.get("baselines", {})}
        cap = {**profile["capacity"], **{k: v for k, v in
               _current.get("capacity", {}).items() if v}}
        merged.update(version=PROFILE_VERSION, baselines=base, capacity=cap,
                      targets=targets_for(base, cap))
        merged.setdefault("reference", profile["reference"])
        profile = merged
    elif mine_already:
        # Fill in only the rows that were still waiting; never move a captured
        # baseline, or progress would silently reset.
        new_keys = set(profile["baselines"]) - set(_current.get("baselines", {}))
        if not new_keys:
            return False
        merged = dict(_current)
        merged["baselines"] = {**_current.get("baselines", {}),
                               **{k: profile["baselines"][k] for k in new_keys}}
        merged["targets"] = {**_current.get("targets", {}),
                             **{k: profile["targets"][k] for k in new_keys
                                if k in profile["targets"]}}
        profile = merged
    save(profile)
    apply(profile)
    log.info("captured baseline for %s from %d samples", profile["machine"],
             profile["samples"])
    return True


def startup(data_dir: Path | None = None) -> None:
    """Run once at import: archive another machine's state, load this one's."""
    _cfg._PROFILE_STARTED = True
    data_dir = data_dir or _cfg.DATA_DIR
    me = machine_id()
    prof = load(data_dir / "baseline.json")
    owner = (prof or {}).get("machine") or _history_owner(data_dir)
    if owner and owner != me:
        archive_foreign_state(data_dir, owner)
        prof = None
    apply(prof if prof and prof.get("machine") == me else None)


# Imported before config: config could not call startup() (this module was only
# half-defined at that point), so run it now.
if not getattr(_cfg, "_PROFILE_STARTED", False):
    startup()
