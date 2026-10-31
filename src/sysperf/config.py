"""
Configuration for the System Performance Dashboard.

Single source of truth for paths, server binding, sampling cadence, the success
criteria, and the vmmemWSL trend thresholds. Kept free of runtime side effects
so it is safe to import from anywhere.

Three things are resolved at import, in a fixed precedence so the behaviour is
predictable wherever it runs:

* **Platform** — native Windows, WSL 2, or plain Linux. See platform_paths.py;
  the PowerShell location and path translation differ completely between them.
* **Data directory** — an explicit ``SYSPERF_DATA_DIR``, else an existing
  install's directory, else the per-user platform location. Never the package
  directory: once pip-installed that is site-packages.
* **Settings** — command line (via the environment, set by the CLI) beats
  environment variable, beats the JSON config file, beats the built-in default.
"""
from __future__ import annotations

import json as _json
import os as _os
import re as _re
import shutil as _shutil
import sys as _sys
from pathlib import Path

from .platform_paths import detect_platform, resolve_data_dir, resolve_powershell

# Where the code lives. Read-only: nothing is ever written here.
BASE = Path(__file__).resolve().parent


def lock_packages(lock: Path | None = None) -> list:
    """Every package in requirements.lock, lower-case; [] where none ships (MSIX).

    Components rows, live version feeds and installed versions follow the lock,
    so a dependency an upgrade brings in is tracked and upgraded like the rest.
    """
    lock = lock or BASE.parents[1] / "requirements.lock"
    try:
        text = lock.read_text(encoding="utf-8")
    except OSError:
        return []
    return sorted({n.lower() for n in _re.findall(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", text, _re.M)})


# Shown in the dashboard header and /api/health; stamped by scripts/build_release.py.
APP_VERSION = "5.4.4"

PLATFORM = detect_platform()
# Microsoft Store mode (store_mode.py): the frozen SysPerf.exe of the MSIX build,
# or SYSPERF_STORE_MODE=1. Features that act outside the app start off until the
# user turns them on, because nobody configured them by hand.
STORE_MODE = bool(getattr(_sys, "frozen", False)) or _os.environ.get(
    "SYSPERF_STORE_MODE", "0") not in ("0", "false", "no", "")
# The interpreter that runs the test suite. A frozen build's sys.executable is
# SysPerf.exe, which cannot run a script, so the installed Python is used there.
PYTHON = ((_shutil.which("py") or _shutil.which("python") or _sys.executable)
          if getattr(_sys, "frozen", False) else _sys.executable)
# Where state lives. An existing installation keeps its directory so a version
# bump never orphans a user's history; a fresh one gets the platform location.
DATA_DIR = resolve_data_dir(PLATFORM, legacy=BASE)
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:                            # pragma: no cover - unwritable target
    pass

# --- settings file -------------------------------------------------------
# The Windows installer writes this; before 2.8.0 nothing read it. Keys are
# nested to match what install.ps1 has always produced:
#   {"host": ..., "port": ..., "sample_seconds": ...,
#    "fixes": {"enabled": true},
#    "autoimprove": {"mode": "auto", "threshold": 80, "interval_seconds": 300}}
CONFIG_FILE = Path(_os.environ.get("SYSPERF_CONFIG")
                   or (DATA_DIR / "sysperf.config.json"))


def _load_settings(path: Path) -> dict:
    """The JSON settings file, or {} when absent or malformed.

    A broken config must not stop the service: it falls back to defaults and the
    dashboard still reports the machine, which is more useful than refusing to
    start.
    """
    try:
        data = _json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


SETTINGS = _load_settings(CONFIG_FILE)


def _setting(env_key: str, *json_path, default=None):
    """One setting, resolved environment-first then JSON then default."""
    raw = _os.environ.get(env_key)
    if raw not in (None, ""):
        return raw
    node = SETTINGS
    for key in json_path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return default if node is None else node


def _bounded(env_key: str, json_path: tuple, default: float, lo: float, hi: float) -> float:
    """A numeric setting clamped to [lo, hi]; junk falls back to the default."""
    try:
        value = float(_setting(env_key, *json_path, default=default))
    except (TypeError, ValueError):
        value = float(default)
    return max(lo, min(hi, value))


def _flag(env_key: str, *json_path, default: bool = True) -> bool:
    value = _setting(env_key, *json_path, default=default)
    if isinstance(value, bool):
        return value
    return str(value).lower() not in ("0", "false", "no", "off", "")


# --- paths ---------------------------------------------------------------
# Collector shell. PowerShell 7 is preferred (collect_win.ps1 runs its inventory
# probes as parallel thread jobs, which 5.1 cannot); None means this platform has
# no Windows host to measure, and the collector reports that instead of failing
# once per sample.
PS_EXE = resolve_powershell(PLATFORM)
CAN_COLLECT = PS_EXE is not None

COLLECT_SCRIPT = BASE / "collect_win.ps1"
HISTORY_FILE = DATA_DIR / "history.jsonl"
# 4.1: SQLite history (raw samples for HISTORY_RAW_DAYS, hourly rollups for a
# year+). The JSONL file above is migrated into it once and renamed .migrated.
HISTORY_DB = DATA_DIR / "history.db"
# Maintenance checks (maintenance.py): the cached report, and the .reg backups
# the registry fix writes before it removes anything.
MAINTENANCE_FILE = DATA_DIR / "maintenance.json"
MAINTENANCE_BACKUP_DIR = DATA_DIR / "registry-backups"
HISTORY_RAW_DAYS = int(_bounded("SYSPERF_HISTORY_RAW_DAYS", ("history", "raw_days"), 14, 1, 90))
NOTIFY_STATE_FILE = DATA_DIR / "notifications.json"
# Reuse one PowerShell process for every sample instead of spawning one each time.
PERSISTENT_SHELL = _flag("SYSPERF_PERSISTENT_SHELL", "collector", "persistent_shell", default=True)
# Per-user known-issue list. Lives with the data, not the code: the copy that
# used to ship in the package described the developer's own laptop.
WATCHLIST_FILE = DATA_DIR / "hw_watchlist.json"
ADAPTIVE_FILE = DATA_DIR / "adaptive_state.json"
TRENDS_FILE = DATA_DIR / "trends_daily.json"
AUTO_STATE_FILE = DATA_DIR / "autoimprove_state.json"
AUTO_LOG_FILE = DATA_DIR / "autoimprove_log.jsonl"
# Machine-readable verdict of the last `tests/testrunner.py --json` run. The
# Auto-Improve tab's Test Suite card reads this so the page can report its own
# quality gates instead of only claiming to be self-improving.
SELFTEST_REPORT_FILE = DATA_DIR / "selftest_report.json"
# Where the gate runner lives and runs. Defaults to BASE, which is correct for a
# source checkout and for the systemd unit. A containerised deployment has the
# code on a read-only image layer, so the suite would have nowhere to write its
# pytest cache or coverage data; pointing this at the writable source tree keeps
# the gate working instead of silently refusing to start.
SELFTEST_ROOT = Path(_os.environ.get("PERFDASH_SELFTEST_ROOT") or BASE)
# Self-authoring evolution layer (APE). State plus a directory for the knowledge
# shards it generates. These two paths are the ONLY things the evolution layer
# writes — engine code is never modified. See evolve.py's safety invariants.
# Human action queue: open/closed status per work item, so "I have done this"
# survives a restart and the same task is never presented twice.
ACTIONS_STATE_FILE = DATA_DIR / "actions_state.json"
# Runtime, reversible target revisions decided automatically from telemetry
# (see target_review.py). CRITERIA stays the plan source of truth; this file
# only holds honest overrides for goals proven unreachable.
AUTO_TARGETS_FILE = DATA_DIR / "auto_targets.json"
EVOLVE_STATE_FILE = DATA_DIR / "evolve_state.json"
EVOLVE_DIR = DATA_DIR / "evolutions"
#   PERFDASH_EVOLVE_EVERY   loop cycles between APE passes (default 8, 0 = off)
EVOLVE_EVERY = int(_os.environ.get("PERFDASH_EVOLVE_EVERY", 8))
# Persisted "OneDrive control disabled" master switch. When set, the dashboard
# is completely hands-off with OneDrive (no start-up stop, no lock-state policy,
# manual Stop/Start refused) — OneDrive behaves as if the dashboard were not
# installed. Persisted so the choice survives a service restart.
ONEDRIVE_CONTROL_FILE = DATA_DIR / "onedrive_control.json"
STATIC_DIR = BASE / "static"

# --- server binding ------------------------------------------------------
# Loopback only: nginx proxies /sysperf/ from 127.0.0.1 and WSL
# localhostForwarding exposes it to the Windows browser. Do not widen the bind
# without adding authentication — the API serves read-only telemetry with none.
HOST = str(_setting("PERFDASH_HOST", "host", default="127.0.0.1"))
PORT = int(_setting("PERFDASH_PORT", "port", default=5056))
# Extra Host names the dashboard may be reached by (e.g. the name a reverse
# proxy forwards). Everything else is refused, which blocks DNS rebinding; see
# hostguard.py. Comma-separated in PERFDASH_ALLOWED_HOSTS, or a JSON list.
_extra_hosts = _setting("PERFDASH_ALLOWED_HOSTS", "allowed_hosts", default=())
ALLOWED_HOSTS = tuple(
    str(h).strip() for h in (_extra_hosts.split(",") if isinstance(_extra_hosts, str)
                             else _extra_hosts) if str(h).strip())

# --- common-fix engine ---------------------------------------------------
# The fix endpoints are the only ones that can mutate system state. Execution
# is gated by this flag (set PERFDASH_FIX_ENABLED=0 to make the API strictly
# read-only again — detection + dry-run preview still work). Even when enabled,
# only reversible non-disruptive ("safe"/"caution") fixes ever execute; manual /
# session-killing fixes are only ever described, never run. The endpoint also
# requires a per-process CSRF token to block blind cross-origin POSTs. The
# service still has NO network authentication (loopback bind + nginx proxy), so
# do NOT widen HOST without adding real auth.
FIX_ENABLED = _flag("PERFDASH_FIX_ENABLED", "fixes", "enabled", default=True)
# Daily lookup of each component's newest public version (version_check.py):
# plain GETs of public release feeds, nothing about this PC is sent.
VERSION_CHECK = _flag("PERFDASH_VERSION_CHECK", "version_check", default=True)

# Default OFF since 2026-10-24: stopping OneDrive made sync very slow. Set the
# env var to 1 to opt back in.
# OneDrive.exe is a consistent background CPU/disk consumer (it surfaces in the
# Auto-Improve "applications using CPU consistently" table) and OneDrive-backed
# paths add sync + Files-On-Demand overhead. When true, the dashboard stops
# OneDrive once at service start so it is NOT running by default; the user can
# start/stop it any time from the Auto-Improve tab. Honours FIX_ENABLED — no
# host mutation happens when execution is disabled.
ONEDRIVE_DEFAULT_STOPPED = _os.environ.get(
    "PERFDASH_ONEDRIVE_DEFAULT_STOPPED", "0") not in ("0", "false", "no", "")

# Lock-state policy: run OneDrive while the workstation is LOCKED (sync freely
# while you are away) and stop it while UNLOCKED (keep CPU/disk free while you
# work). Honours FIX_ENABLED.
ONEDRIVE_LOCK_POLICY = _os.environ.get(
    "PERFDASH_ONEDRIVE_LOCK_POLICY", "0") not in ("0", "false", "no", "")
SESSION_POLICY_INTERVAL_S = float(
    _os.environ.get("PERFDASH_SESSION_POLICY_INTERVAL", 15))

# The policy RECONCILES (level-triggered) rather than acting only on lock/unlock
# edges. A purely edge-triggered policy latches after its first evaluation and
# then never corrects drift: observed 2026-09-28, the service came up during
# Windows logon, stopped OneDrive once, and Windows' own autostart relaunched it
# ~3 min later. Because the session never locked again, the policy returned "no
# transition -> do nothing" on every subsequent tick and OneDrive ran unbounded
# while unlocked (~800 CPU-seconds in 16 min). Reconciling re-asserts the desired
# state whenever actual != desired. Set to 0 for the old edge-only behaviour.
SESSION_POLICY_RECONCILE = _os.environ.get(
    "PERFDASH_SESSION_POLICY_RECONCILE", "1") not in ("0", "false", "no", "")

# An explicit Start/Stop from the UI suspends reconciliation for this long, so
# user intent is honoured without giving up drift correction. Previously "respect
# manual control" was an accidental side effect of never re-checking; it is now
# an explicit, bounded hold. A real lock/unlock transition clears the hold — that
# is an unambiguous signal about what the desired state should be.
SESSION_POLICY_MANUAL_GRACE_S = float(
    _os.environ.get("PERFDASH_SESSION_POLICY_MANUAL_GRACE", 1800))

# --- autonomous improvement loop (autoimprove.py) ------------------------
# The loop scans every AUTO_INTERVAL_S, and when the overall score is below
# AUTO_THRESHOLD it runs a root-cause analysis (rca.py) over BOTH the short
# horizon (rolling median vs target) and the long horizon (multi-week daily
# trends from trends.py), then applies the remediations that address the causes.
#
#   PERFDASH_AUTO_MODE      off | observe | auto   (default auto)
#       off      - scan + RCA only, no plan executed and nothing scheduled
#       observe  - full RCA + action plan, but nothing is executed
#       auto     - eligible remediations are applied and then verified
#   PERFDASH_AUTO_ALLOW_CAUTION=1 also lets 'caution' fixes run unattended
#       (default: 'safe' only — 'caution' fixes are user-visible, e.g. restarting
#        Explorer, so they stay opt-in and are additionally deferred out of the
#        measured peak-usage hours). 'manual'/session-killing fixes NEVER run.
# Since 4.1 the settings file is honoured here too (it always documented these
# keys, but only the environment was read). Values are clamped to safe ranges.
AUTO_INTERVAL_S = _bounded("PERFDASH_AUTO_INTERVAL", ("autoimprove", "interval_seconds"),
                           300, 60, 3600)
AUTO_THRESHOLD = _bounded("PERFDASH_AUTO_THRESHOLD", ("autoimprove", "threshold"), 80, 50, 100)
AUTO_MODE = str(_setting("PERFDASH_AUTO_MODE", "autoimprove", "mode", default="auto"))
if AUTO_MODE not in ("off", "observe", "auto"):
    AUTO_MODE = "auto"
_ALLOW_CAUTION = _flag("PERFDASH_AUTO_ALLOW_CAUTION", "autoimprove", "allow_caution",
                       default=False)
AUTO_ALLOW_RISKS = ("safe", "caution") if _ALLOW_CAUTION else ("safe",)
AUTO_MAX_ACTIONS = int(_bounded("PERFDASH_AUTO_MAX_ACTIONS", ("autoimprove", "max_actions"),
                                2, 0, 10))
AUTO_COOLDOWN_S = _bounded("PERFDASH_AUTO_COOLDOWN", ("autoimprove", "cooldown_seconds"),
                           1800, 60, 86400)
# Wait at least this long after applying a fix before measuring its effect, so
# the scorecard's rolling median has refreshed with post-fix samples.
AUTO_SETTLE_S = _bounded("PERFDASH_AUTO_SETTLE", ("autoimprove", "settle_seconds"),
                         120, 30, 3600)
AUTO_LOG_MAX_LINES = 500
# Effectiveness ledger: EMA smoothing of a fix's measured benefit per metric,
# and how many verified attempts are needed before the loop trusts a verdict
# and stops choosing an action that has never helped.
LEDGER_ALPHA = 0.3
LEDGER_MIN_N = 3
# Days of score history required before a declining long-term score trend is
# allowed to raise the per-cycle action budget.
SCORE_TREND_MIN_DAYS = 7

# --- standby-clear elevation broker (on-demand; never a timer) -----------
# Emptying the Windows standby (cached) list needs Administrator, but this
# service runs unprivileged (systemd NoNewPrivileges=yes). A tiny elevated
# watcher - standby_broker.ps1, started once "as Administrator" - performs the
# clear on demand. The auto-improve loop drops a request ONLY when the memory
# scores are low (see fixes._detect_standby), so it fires exactly when needed
# and never on a schedule. If the watcher is not running, clear_standby stays a
# manual action with the RAMMap recipe, so nothing regresses.
# Windows-side view of the exchange directory (used by the pwsh fix command):
STANDBY_BROKER_DIR_WIN = _os.environ.get(
    "PERFDASH_STANDBY_DIR_WIN", r"C:\ProgramData\SysPerf\standby")
# WSL-side view of the same directory (used by the Python heartbeat probe):
STANDBY_BROKER_DIR_WSL = _os.environ.get(
    "PERFDASH_STANDBY_DIR_WSL", "/mnt/c/ProgramData/SysPerf/standby")
# A heartbeat older than this (seconds) means the watcher is not running.
STANDBY_BROKER_HEARTBEAT_MAX_S = float(
    _os.environ.get("PERFDASH_STANDBY_HEARTBEAT_MAX", 90))
# How long the fix command waits (seconds) for the watcher to post a result.
STANDBY_BROKER_WAIT_S = float(_os.environ.get("PERFDASH_STANDBY_WAIT", 25))
# Where standby_broker.ps1 is staged on the Windows host - used only to show the
# relaunch command on the dashboard's Auto-Improve tab.
STANDBY_BROKER_SCRIPT_DIR = _os.environ.get(
    "PERFDASH_STANDBY_SCRIPT_DIR",
    str(BASE) if PLATFORM == "windows" else r"C:\Tools\SysPerf")

# --- runaway-Explorer CPU watchdog (procwatch.py) ------------------------
# Windows Explorer (explorer.exe, the desktop shell) periodically pins CPU when
# a shell extension, thumbnail build, or cloud-sync overlay handler gets stuck.
# It is uniquely safe to auto-restart because Windows relaunches the shell
# immediately, releasing the stuck thread with no data loss. This watchdog watches
# explorer's live per-interval CPU% and restarts it once it stays at/above the
# threshold across several consecutive intervals, with a cooldown so it cannot
# thrash. Honours FIX_ENABLED. State (enabled flag + last action) is persisted.
PROCWATCH_STATE_FILE = DATA_DIR / "procwatch_state.json"
# Tray-app auto-quit (trayquit.py): settings + recent quits, and the quit log.
TRAYQUIT_STATE_FILE = DATA_DIR / "trayquit_state.json"
TRAYQUIT_LOG_FILE = DATA_DIR / "trayquit_log.jsonl"
TRAYQUIT_INTERVAL_S = float(_os.environ.get("PERFDASH_TRAYQUIT_INTERVAL", 15))
PROCWATCH_ENABLED = _os.environ.get(
    "PERFDASH_PROCWATCH_ENABLED", "0" if STORE_MODE else "1") not in ("0", "false", "no", "")
# Restart when explorer's CPU% (share of ALL logical processors) is at/above this
# in each of the last PROCWATCH_SUSTAIN intervals. 25% > normal idle (~0–3%) and
# below the collected 35% spike, so a genuine runaway trips it while a brief blip
# (one interval) does not.
PROCWATCH_CPU_PCT = float(_os.environ.get("PERFDASH_PROCWATCH_CPU_PCT", 25))
PROCWATCH_SUSTAIN = int(_os.environ.get("PERFDASH_PROCWATCH_SUSTAIN", 2))
PROCWATCH_COOLDOWN_S = float(_os.environ.get("PERFDASH_PROCWATCH_COOLDOWN", 600))
PROCWATCH_INTERVAL_S = float(_os.environ.get("PERFDASH_PROCWATCH_INTERVAL", 20))
# Never act on (or trust) a sample older than this. Under heavy load the Windows
# probe can take over a minute, so the newest sample legitimately lags; but a
# multi-minute-old reading must not drive an Explorer restart. Kept comfortably
# above the worst-observed probe time so a busy-but-healthy collector still acts.
PROCWATCH_MAX_AGE_S = float(_os.environ.get("PERFDASH_PROCWATCH_MAX_AGE", 360))

# --- sampling ------------------------------------------------------------
# Cadence. The Windows probe spawns pwsh.exe across the \\wsl.localhost 9p
# boundary, which is itself CPU-heavy AND makes CrowdStrike/Defender/DLP inspect
# every spawn. At the old 20 s cadence — with the probe taking 20–70 s under load
# — a pwsh probe ran almost continuously and measured ~20 % of the whole machine
# (plus the WSL VM's share), i.e. the dashboard was a top CPU consumer on the box
# it was scoring. 60 s cuts that ~3x while the sample-count windows below are
# rescaled to keep their original TIME spans, so scoring behaviour is unchanged.
SAMPLE_SECONDS = _bounded("PERFDASH_SAMPLE_SECONDS", ("sample_seconds",), 60, 30, 600)
SNAPSHOT_TTL = 3.0           # serve a cached snapshot for this long
FULL_EVERY = 5               # slow metrics every Nth sample (5 * 60 s = 5 min)
MAX_SAMPLES = 240            # 240 * 60 s = 4 hours of history in memory
HISTORY_MAX_BYTES = 12_000_000
# Windows-probe timeouts. Launching pwsh.exe over the \\wsl.localhost path and
# running the collector script is slow when the host is under heavy load (a
# 100%-CPU box was measured taking ~76 s for a light probe). The old flat 90 s
# limit made the heavier -Full probe (service inventory + launch-time checks)
# time out under exactly the conditions the dashboard exists to observe, freezing
# all metrics. The Full probe now gets a much larger budget; the light probe a
# generous one. Override with PERFDASH_COLLECT_TIMEOUT / _FULL.
COLLECT_TIMEOUT_S = float(_os.environ.get("PERFDASH_COLLECT_TIMEOUT", 150))
COLLECT_TIMEOUT_FULL_S = float(_os.environ.get("PERFDASH_COLLECT_TIMEOUT_FULL", 300))

# Fluctuating metrics (process/handle/memory counts) are scored on the MEDIAN of
# the last SCORE_SMOOTH_WINDOW samples rather than the single latest reading, so
# a momentary spike cannot collapse the score. ~5 min at the 60s cadence (5x60s).
# The live gauges/charts still use the raw latest value; only the scorecard's
# pass/fail + progress is smoothed. Set to 1 to disable smoothing.
SCORE_SMOOTH_WINDOW = 5

# A collector gap longer than this means the samples before it describe a
# DIFFERENT session (reboot, resume from sleep, or a Flask restart). The scoring
# median must not span such a gap: observed 2026-09-28, a 14.7 h overnight gap
# left 11 of the 15 window slots filled by pre-shutdown samples, so the scorecard
# graded yesterday's loaded session for ~13 min after boot (active app commit
# scored 18,504 MB while the machine was actually at 6,393 MB). The window is
# therefore anchored at the newest gap, exactly as vmmem_trend() already does for
# its slope. Kept independent of TREND_GAP_S so the two can diverge.
SCORE_GAP_S = 120

# Below this many current-session samples the median is correct (it only uses
# post-gap samples) but thin, so it is noisier than usual. The scorecard reports
# it as `warming_up` and the UI labels the score as stabilising rather than
# presenting a single-sample median as a settled result.
SCORE_WARMUP_SAMPLES = 3

# Per-metric smoothing-window overrides (in samples). Some metrics are bursty
# AND sampled sparsely (carry-forward, refreshed every FULL_EVERY samples), so
# the default short window is wrong: it returns whichever single 5-min snapshot
# last landed, making the score whipsaw 0<->100. Keys listed here are smoothed
# even though they are carry-forward, using a window spanning MANY refreshes -> a
# time-weighted median that rejects transient bursts.
#   net_remote: established remote TCP conns spike during OneDrive/Teams syncs and
#   security scans (observed up to 195) then settle to ~30-34. A 60-min median
#   reflects the sustained level and ignores those bursts, while a genuine
#   sustained rise still moves it. Expressed in TIME: the window was written as
#   "180 samples * 20 s = 60 min", the cadence later became 60 s, and the same
#   180 samples silently became 3 hours, so one sync burst lingered all afternoon.
def _samples_for(seconds: float) -> int:
    return max(1, round(seconds / SAMPLE_SECONDS))


SMOOTH_WINDOW_OVERRIDES = {"net_remote": _samples_for(60 * 60)}

# Wi-Fi negotiated link rate: rate adaptation re-negotiates constantly (observed
# 26 -> 780 Mbps across a single session as the client shifts band/MCS/streams),
# so the Summary "Wi-Fi connectivity" category was flapping between score bands
# on whichever instantaneous sample happened to land. The whole scorecard is
# explicitly "scored on a rolling median, not a single sample" — this applies the
# same rule to the Wi-Fi link rate: 30 min of sustained link (in time, see above).
WIFI_LINK_SMOOTH_WINDOW = _samples_for(30 * 60)

# --- Wi-Fi scoring: per network, not an absolute rate ladder ----------------
# An absolute Mbps ladder ("1 Gbps = 100%") scores the ISP plan, not the Wi-Fi.
# On a 100 Mbps home plan a 130 Mbps link is nearly all the plan can deliver, yet
# a universal ladder calls it 45%. Wi-Fi's actual job is to not be the bottleneck
# for the network behind it, so the link is scored against what THAT network can
# deliver — keyed by SSID, because the same laptop roams between very different
# networks (observed here: home p50 98 Mbps vs office p50 613 Mbps).
#
# Declare the subscribed capacity per SSID where it is known. Any network not
# listed falls back to its own learned capability (see WIFI_CAPABILITY_PCTL),
# mirroring the adaptive personal-best scoring used for the other metrics.
# Set in sysperf.config.json as {"wifi": {"plan_mbps": {"<SSID>": <Mbps>}}}.
_wifi_plan = _setting("PERFDASH_WIFI_PLAN", "wifi", "plan_mbps", default={})
if isinstance(_wifi_plan, str):
    try:
        _wifi_plan = _json.loads(_wifi_plan)
    except ValueError:
        _wifi_plan = {}
WIFI_NETWORK_PLAN_MBPS = {str(k): float(v) for k, v in
                          (_wifi_plan if isinstance(_wifi_plan, dict) else {}).items()
                          if isinstance(v, (int, float)) and v > 0}

# TCP over 802.11 realises roughly 60% of the negotiated PHY rate (framing, block
# ACKs, half-duplex airtime, management traffic). A 100 Mbps plan therefore needs
# about 167 Mbps of PHY to be carried without the radio becoming the bottleneck.
WIFI_PHY_EFFICIENCY = 0.60

# Percentile of a network's observed sustained rates taken as its capability when
# no plan is declared. p90 rather than max, so a brief 5 GHz association does not
# permanently set an unreachable bar.
WIFI_CAPABILITY_PCTL = 0.90

# Minimum same-SSID samples before a learned capability is trusted; below this the
# category reports "—" rather than a shaky score.
WIFI_MIN_CAPABILITY_SAMPLES = 30

# --- payload meta --------------------------------------------------------
BASELINE_DATE = "2026-08-25 12:29"
REVISION = "2.1"

# --- success criteria (key, label, baseline, target, higher_is_better, unit) ---
CRITERIA = [
    # committed (2026-09-11): scores the ACTIONABLE application commit (sum of
    # app-process private bytes) instead of the raw "Committed Bytes" counter.
    # The raw counter (~31k MB) is ~58% non-actionable: OS/service/security-agent
    # private commit + a ~9k MB kernel/pagefile-backed shared commit gap, plus WSL
    # guest commit (already scored as vmmem_mb). The old target 26,000 sat at the
    # fresh-boot floor of the raw counter, so the row could only decay as normal
    # work committed memory. Baseline 14,000 = typical heavy app session; target
    # 9,000 = achievable lean session. committed_mb (raw counter) is retained for
    # the hero display + chart. Same fix pattern as handles / net_remote.
    # target 9,000 -> 9,600 (2026-10-05): 14-day telemetry showed the metric
    # NEVER fell below ~9,575 MB (range 9,575-15,106), so 9,000 was unreachable
    # with the current app mix and pinned the row in permanent shortfall. 9,600
    # = the measured lean floor + small headroom: honest and reachable when the
    # session is trimmed. Same unreachable-target correction as handles / net_remote.
    ("committed_app_mb", "Active app commit",  14000, 9600, False, "MB"),
    # compression (2026-09-11): target 1,500 -> 2,750. Memory Compression is a
    # single OS process (the system-wide compressed store) with no per-process
    # attribution, so it cannot be floor-split like commit/handles. Its target of
    # 1,500 MB was reachable only at fresh boot; the realistic working range is
    # 3-4k MB and it falls when memory pressure eases. 2,750 is an achievable lean
    # goal (target-correction pattern, as used for net_remote 20->30, browser 45->72).
    ("compression_mb", "Memory Compression",   4113,  2750, False, "MB"),
    ("available_mb",   "Available physical",   7013, 12000, True,  "MB"),
    ("vmmem_mb",       "vmmemWSL",             5627, 10240, False, "MB"),
    # browser_procs (2026-09-04): baseline corrected 82 -> 105 and target
    # 66 -> 72. The old baseline (82) was captured mid-optimization and sat at
    # the metric's own median, so the count was at/above baseline ~47% of the
    # time and the row scored 0 on every upward breath. 105 is the true
    # pre-optimization baseline (the adaptive layer's recognized start; observed
    # peak 94), giving real, non-collapsing headroom. Target 72 is the realistic
    # floor: non-closable/in-use processes total ~57 (GlobalProtect WebView2 ~30,
    # OneDrive ~7, Windows SearchHost ~6, Teams ~7, Outlook ~7) and Edge adds ~14
    # when open (~71); 66 was only reachable with Edge fully closed. The score is
    # also computed on a rolling median (see SCORE_SMOOTH_WINDOW) so momentary
    # breathing no longer reduces it. Same correction pattern as net_remote.
    ("browser_procs",  "Browser processes",      105,   72, False, ""),
    # handles (2026-09-11): now scores the ACTIONABLE application-handle footprint
    # (total system-wide handles minus the non-actionable OS/kernel/service-host +
    # resident security-agent floor, which was ~84k / ~41% of the raw total and
    # grows with uptime regardless of user action). The old metric summed ALL
    # handles and used target 150,000 — below the machine's fresh-boot floor
    # (~180k total), so the row could never be met and only decayed as the OS/
    # security agents accumulated handles. Same unreachable-target class of bug
    # already corrected for net_remote (20->30) and browser_procs (45->72).
    # Baseline 130k = typical heavy session app footprint; target 95k = achievable
    # by closing discretionary apps / sleeping Edge tabs. Scored on a rolling
    # median so momentary breathing does not collapse the row.
    ("handles",        "Active app handles",  130000, 95000, False, ""),
    ("c_free_gb",      "C: free",              164.5,  180, True,  "GB"),
    # network + startup optimization dimensions (baselines = state when added)
    # net_remote target raised 20 -> 30 (2026-08-31): 20 sat BELOW the machine's
    # mandatory connection floor. Non-closable remote conns during any work
    # session total ~18 (svchost/OS ~7, OneDrive sync ~6, GlobalProtect PanGPS 1,
    # Defender MsSense 1, System 1, corporate vf_agent 2) before a single app
    # opens. 30 credits trimming discretionary chatter while accepting the floor
    # plus core Office apps; 20 was unreachable and stuck the row in regress.
    # net_remote corrected again 40/30 -> 55/40 (2026-09-24): 14-day telemetry
    # (6,899 samples) showed a mean of 32 and a working range of 19-42 with the
    # full cloud-app mix (Teams + new Outlook + OneDrive + Edge/WebView2 + VPN +
    # ChatGPT), each holding multiple keep-alive connections. Target 30 sat at/below
    # the real mandatory-cloud floor, so the rolling typical (42) stayed >= baseline
    # (40) and the row was pinned at 0% (regress) with NO achievable path up. Target
    # 40 = realistic floor: accepts the mandatory cloud-app connections while still
    # crediting a trim of discretionary chatter. Baseline 55 = heavy multi-cloud
    # session (observed peak ~57) giving real, non-collapsing headroom. Same
    # unreachable-target correction pattern as browser_procs (45->72) and handles.
    ("net_remote",     "Network - remote TCP conns", 55, 40, False, ""),
    ("startup_apps",   "Startup programs (enabled)", 19, 10, False, ""),
    # Hardware faults = Device-Manager error-code devices + unhealthy disks.
    ("hw_faults",      "Hardware - device faults",    3,  0, False, ""),
    # startup & launch latency (2026-09-13) — boot and app-launch responsiveness.
    # Measured best-effort by the collector (Diagnostics-Performance boot event
    # 100 for boot; a timed cold `wsl -e true` for WSL; app-launch timings from
    # the same performance log). Rows are omitted until the collector supplies a
    # value, so the dashboard degrades gracefully on machines without the data.
    # Lower is better; baselines = the user-reported slow-start state.
    # Launch/boot targets (3.1.0): relaxed from aspirational consumer values to
    # realistic real-world defaults — the originals (40s boot, 2s Explorer, 5s
    # Outlook) are unreachable on managed machines with security agents + WebView2
    # apps, so they pinned the rows at 0. The per-machine auto-target-realism
    # layer (target_review) refines these further from each machine's own history.
    ("boot_time_s",       "Windows boot time",       150,  75, False, "s"),
    ("wsl_start_s",       "WSL cold-start time",       25,   8, False, "s"),
    ("explorer_launch_s", "Explorer launch time",      25,   8, False, "s"),
    ("outlook_launch_s",  "Outlook launch time",       60,  20, False, "s"),
    ("word_launch_s",     "Word launch time",          25,  10, False, "s"),
    # top-5 previously-unscored metrics (2026-09-13). cpu_pct/cpu_queue already
    # flow from the collector (shown in chips/charts) but were never scored;
    # disk_active_pct/cpu_temp_c/battery_wear_pct are collected best-effort and
    # omitted until present. All lower-is-better.
    ("cpu_pct",           "CPU utilisation",           40,  25, False, "%"),
    ("cpu_queue",         "CPU run-queue depth",        4,   1, False, ""),
    ("disk_active_pct",   "Disk active time",          30,  10, False, "%"),
    ("cpu_temp_c",        "CPU temperature",           85,  70, False, "C"),
    ("battery_wear_pct",  "Battery wear",              20,  10, False, "%"),
]

BROWSER_TARGET_NOTE = (
    "Baseline and target are this machine's own: the baseline is the typical "
    "count when SysPerf first measured it, the target a realistic trim of the "
    "closable part. WebView2 processes owned by mandatory hosts cannot be closed; "
    "list your own under browser_hosts in sysperf.config.json."
)

# WebView2 hosts that cannot be closed vs. those that are simply in use.
# Overridable per machine via "browser_hosts" in sysperf.config.json.
def _host_map(name: str, default: dict) -> dict:
    node = SETTINGS.get("browser_hosts")
    node = node.get(name) if isinstance(node, dict) else None
    return {str(k): str(v) for k, v in node.items()} if isinstance(node, dict) else default


MANDATORY_HOSTS = _host_map("mandatory", {
    "OneDrive": "File sync",
    "SearchHost": "Windows Start / Search",
    "ShellExperienceHost": "Windows shell"})
INUSE_HOSTS = _host_map("in_use", {
    "ms-teams": "Microsoft Teams",
    "OUTLOOK": "Microsoft Outlook"})

# Informational, unscored rows. Empty by default: the one that used to ship here
# (an Intel graphics boot stall) described the developer's laptop, not yours.
STATIC_CRITERIA: list = []

# --- vmmemWSL trend thresholds ------------------------------------------
MIN_TREND_SAMPLES = 6        # minimum points before a slope is trusted
MIN_TREND_SPAN_S = 600       # ...and it must cover at least this much wall time
TREND_WINDOW_S = 3600        # only the last hour feeds the slope
VMMEM_CAP_MB = 10240         # .wslconfig memory=10GB ceiling
TREND_GAP_S = 120            # a collector gap this long == Flask was down (restart)

# --- metrics carried forward between -Full samples ----------------------
CARRY_FORWARD_KEYS = (
    "services_running", "services_total",
    "startup_apps", "startup_apps_total",
    "net_remote", "net_remote_raw", "net_remote_hosts", "net_remote_infra", "net_loopback", "net_established",
    "net_listen", "net_tcp_total", "_full_t",
    "net_adapters", "net_category", "net_ipv4_conn", "net_ipv6_conn",
    "gpu_name", "gpu_driver", "gpu_driver_date",
    "net_driver_name", "net_driver_ver", "net_driver_date", "net_driver_provider",
    "win_build", "wifi_scan",
    "ps_version", "edge_version", "webview2_version", "bios_version", "bios_date",
    "pwsh_version",
    "hw_problem_devices", "hw_problem_list",
    "hw_disks", "hw_disks_unhealthy", "hw_battery",
    "hw_pointing", "hw_touchpad",
    "drv_total", "drv_unsigned", "drv_stale", "drv_stale_list",
    "drv_problem", "drv_problem_list",
    # startup & launch latency (measured on the -Full sweep, carried between)
    "boot_time_s", "wsl_start_s", "explorer_launch_s", "outlook_launch_s",
    "word_launch_s",
    # slow-tier health metrics (thermal / disk / battery), carried between
    "disk_active_pct", "cpu_temp_c", "battery_wear_pct",
)

# --- series exposed to the frontend charts ------------------------------
SERIES_KEYS = [
    "committed_mb", "committed_app_mb", "committed_system_mb", "compression_mb", "available_mb", "vmmem_mb", "handles",
    "handles_total", "handles_system",
    "browser_procs", "edge_procs", "wv2_procs", "pool_paged_mb",
    "pool_nonpaged_mb", "cpu_pct", "processes", "c_free_gb",
    "net_remote", "net_loopback", "net_established", "net_listen", "startup_apps",
    "wifi_signal",
    "boot_time_s", "wsl_start_s", "explorer_launch_s", "outlook_launch_s",
    "word_launch_s",
    "cpu_queue", "disk_active_pct", "cpu_temp_c", "battery_wear_pct",
]

# --- per-machine baseline -------------------------------------------------
# Rows still waiting for this machine's baseline (see machine_profile.py).
PROFILE_PENDING: frozenset = frozenset()

# Replace the shipped defaults with THIS machine's captured baseline and targets,
# and archive state that another computer left in the data directory. Must run
# last, after CRITERIA exists, and before any other module builds from it.
try:
    from .machine_profile import startup as _profile_startup
except ImportError:
    # machine_profile itself is mid-import (it was imported first and pulled
    # config in); it runs startup() once it has finished defining it.
    _profile_startup = None
if _profile_startup is not None:
    _profile_startup()
