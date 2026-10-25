#!/usr/bin/env python3
"""
System Performance Dashboard — Flask entry point.

Read-only by design: every route is GET and no endpoint mutates system state.
Wiring only — the real work lives in focused modules:

    config.py     constants, paths, criteria, thresholds
    scoring.py    progress / vmmemWSL trend / scorecard
    browser.py    browser-process ownership breakdown
    hardware.py   device/disk/battery health + known-issue watchlist
    collector.py  background sampler + snapshot assembler
    adaptive.py   self-improving (personal-best) scoring layer
    trends.py     long-term (multi-week) usage-trend store
    rca.py        root-cause analysis for score shortfalls
    autoimprove.py 5-minute scan -> RCA -> fix -> verify -> learn loop

Mutating endpoints are limited to the fix engine (POST /api/fix) and the
auto-improve controls (POST /api/autoimprove/*); both require the CSRF token and
only ever run reversible, non-disruptive remediations.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

from . import appctl as appctl_mod
from . import codevolve as codevolve_mod
from . import cpuwatch
from . import evodb as evodb_mod
from . import fixes as fixes_mod
from . import health as health_mod
from . import historydb as historydb_mod
from . import livestats as livestats_mod
from . import llm as llm_mod
from . import notify as notify_mod
from . import onedrive as onedrive_mod
from . import onedrive_control as onedrive_control_mod
from . import optactions as optactions_mod
from . import procwatch as procwatch_mod
from . import pwsh_session as pwsh_mod
from . import rca as rca_mod
from . import session_policy as session_policy_mod
from . import settings_hub as settings_mod
from . import store_mode as store_mode_mod
from . import trayquit as trayquit_mod
from .actions import ActionQueue
from .adaptive import AdaptiveScorer
from .autoimprove import AutoImprover
from .collector import Collector
from .config import (
    ACTIONS_STATE_FILE,
    ADAPTIVE_FILE,
    APP_VERSION,
    AUTO_INTERVAL_S,
    AUTO_LOG_FILE,
    AUTO_LOG_MAX_LINES,
    AUTO_STATE_FILE,
    AUTO_TARGETS_FILE,
    CAN_COLLECT,
    COLLECT_TIMEOUT_FULL_S,
    CONFIG_FILE,
    CRITERIA,
    DATA_DIR,
    EVOLVE_DIR,
    EVOLVE_EVERY,
    EVOLVE_STATE_FILE,
    FIX_ENABLED,
    HISTORY_DB,
    HISTORY_FILE,
    HISTORY_RAW_DAYS,
    HOST,
    NOTIFY_STATE_FILE,
    ONEDRIVE_CONTROL_FILE,
    ONEDRIVE_DEFAULT_STOPPED,
    ONEDRIVE_LOCK_POLICY,
    PERSISTENT_SHELL,
    PLATFORM,
    PORT,
    PROCWATCH_COOLDOWN_S,
    PROCWATCH_CPU_PCT,
    PROCWATCH_ENABLED,
    PROCWATCH_INTERVAL_S,
    PROCWATCH_MAX_AGE_S,
    PROCWATCH_STATE_FILE,
    PROCWATCH_SUSTAIN,
    PS_EXE,
    PYTHON,
    SAMPLE_SECONDS,
    SELFTEST_REPORT_FILE,
    SELFTEST_ROOT,
    SERIES_KEYS,
    SESSION_POLICY_INTERVAL_S,
    SESSION_POLICY_MANUAL_GRACE_S,
    SESSION_POLICY_RECONCILE,
    SNAPSHOT_TTL,
    STANDBY_BROKER_DIR_WIN,
    STANDBY_BROKER_SCRIPT_DIR,
    STATIC_DIR,
    STORE_MODE,
    TRAYQUIT_INTERVAL_S,
    TRAYQUIT_LOG_FILE,
    TRAYQUIT_STATE_FILE,
    TRENDS_FILE,
)
from .evolve import EvolutionEngine
from .fixes import FixEngine
from .learning import LearningView
from .platform_paths import describe as platform_describe
from .selftest import SelfTest
from .supervisor import RESTART_CODE
from .target_review import AutoTargetStore
from .trends import TrendStore


class _EvolvedMapView(dict):
    """A live, read-only ``metric -> remediation ids`` view of what evolution has
    promoted.

    A plain dict snapshot would freeze coverage at import time, so a pairing
    learned at 03:00 would not count until the service restarted. Subclassing dict
    keeps the collaborator's type contract while reading through on every access.
    """

    def __init__(self, engine) -> None:
        super().__init__()
        self._engine = engine

    def get(self, key, default=None):
        return self._engine.evolved_map().get(key, default)

    def __getitem__(self, key):
        return self._engine.evolved_map()[key]

    def __contains__(self, key) -> bool:
        return key in self._engine.evolved_map()

    def __bool__(self) -> bool:
        return bool(self._engine.evolved_map())

# 4.1: service self-diagnostics. Installed before anything else is built so
# start-up warnings are captured too.
SERVICE_STARTED = time.time()
error_buffer = health_mod.ErrorBuffer()
logging.getLogger().addHandler(error_buffer)
health_monitor = health_mod.HealthMonitor(error_buffer, started_at=SERVICE_STARTED)

scorer = AdaptiveScorer(ADAPTIVE_FILE)
# 4.1: SQLite history (the old JSONL file is migrated once) and one reusable
# PowerShell process for every sample.
history_store = historydb_mod.open_store(
    HISTORY_DB, [c[0] for c in CRITERIA] + list(SERIES_KEYS), legacy_jsonl=HISTORY_FILE,
    raw_days=HISTORY_RAW_DAYS)
_shell = pwsh_mod.PwshSession(PS_EXE) if PERSISTENT_SHELL and CAN_COLLECT else None
collector = Collector(scorer, store=history_store, shell=_shell)
fix_engine = FixEngine()
trend_store = TrendStore(TRENDS_FILE)
# Long-term reasoning needs history on day one: import the adaptive scorer's
# per-day aggregates for any (day, metric) the trend store does not have yet.
trend_store.backfill_from_adaptive_file(ADAPTIVE_FILE)
# Automatic target-realism: the loop decides whether each goal is realistic from
# telemetry (never asks the user) and auto-revises goals it proves unreachable.
auto_target_store = AutoTargetStore(AUTO_TARGETS_FILE)
collector.target_store = auto_target_store      # scorecard uses honest overrides
improver = AutoImprover(collector, trend_store, fix_engine,
                        AUTO_STATE_FILE, AUTO_LOG_FILE,
                        target_store=auto_target_store)
# Continuous-learning view: turns the loop's ledger, proposals and cycle log into
# KPIs, per-strategy effectiveness, activity history and a live audit of the
# loop's own operating rules. Read-only — it never touches loop state.
learning_view = LearningView(improver, trend_store, fix_engine,
                             AUTO_LOG_FILE, AUTO_LOG_MAX_LINES,
                             remediation_map=rca_mod.REMEDIATION)
# Self-authoring evolution layer: every EVOLVE_EVERY cycles it converts
# accumulated evidence into new capability — learned (fix, metric) pairings,
# candidate remediations drawn from a vetted library, generated knowledge shards,
# cause->family clusters and structural proposals. Generated remediations are
# candidates only: they cannot run until a human promotes them.
evolution = EvolutionEngine(learning_view, rca_mod.REMEDIATION,
                            EVOLVE_STATE_FILE, EVOLVE_DIR, EVOLVE_EVERY,
                            context=lambda: {
                                "cpu_consumers": _cached(
                                    "cpu_consumers",
                                    lambda: cpuwatch.consistent_cpu(
                                        collector.history)),
                                # CRITERIA rows are
                                # (key, label, baseline, target, hib, unit).
                                "targets": {c[0]: c[3] for c in CRITERIA},
                            },
                            auto_adopt=True)
# Coverage in the knowledge-set view must count what evolution has learned, so the
# view reads the engine's promoted mappings live rather than a snapshot.
learning_view.evolved_map = _EvolvedMapView(evolution)
# Integrated test suite: the repo's seven gates (discover, compile, lint, unit,
# coverage, frontend, live), readable and runnable from the dashboard itself.
self_test = SelfTest(SELFTEST_ROOT, SELFTEST_REPORT_FILE, python=PYTHON,
                     fallback_root=lambda: code_evolution.settings.get("repo"))
# Human action queue: every skip, escalation and pending review turned into a
# work item with ordered steps and a specific reason automation is barred.
action_queue = ActionQueue(improver, fix_engine, evolution,
                          ACTIONS_STATE_FILE, surface_evolution=False)
# ---- 4.0: learning store, action policy, LLM client, source self-evolution ---
# None of this is on the monitoring path: every piece degrades to "unavailable"
# and the collector, scorer, RCA and rule-based advice keep working without it.
EVO_DB_FILE = DATA_DIR / "sysperf.db"
EVO_SETTINGS_FILE = DATA_DIR / "evolution_settings.json"
LLM_KEY_FILE = DATA_DIR / "secrets" / "anthropic.key"
logging.getLogger().addFilter(llm_mod.RedactingFilter())
evo_db = evodb_mod.EvoDB(EVO_DB_FILE)
# The 3.x "auto" migration only applies to an install someone upgraded; a Store
# install is new, so it starts at Observe like every other opt-in feature.
action_policy = optactions_mod.ActionPolicy(
    DATA_DIR / "autonomy.json", evo_db,
    improver_mode=None if STORE_MODE else getattr(improver, "state", {}).get("mode"))
improver.action_gate = action_policy.unattended_block
improver.action_hook = action_policy.record_executions


def _llm_settings() -> dict:
    try:
        return json.loads(EVO_SETTINGS_FILE.read_text()).get("llm", {})
    except (OSError, ValueError):
        return {}


def _build_llm():
    s = _llm_settings()
    meter = llm_mod.UsageMeter(int(s.get("daily_token_budget", 200_000)),
                               float(s.get("daily_cost_budget_usd", 2.0)))
    return llm_mod.LLMClient(llm_mod.build_provider(s, LLM_KEY_FILE), meter,
                             on_call=evo_db.record_llm)


code_evolution = codevolve_mod.CodeEvolution(
    evo_db, _build_llm(), EVO_SETTINGS_FILE, DATA_DIR / "evolution-worktrees",
    state_dir=DATA_DIR)

# 4.1: notifications — every one lands in the inbox; Windows toasts on top.
notifier = notify_mod.Notifier(NOTIFY_STATE_FILE, f"http://127.0.0.1:{PORT}/",
                               toast_capable=PLATFORM == "windows")


def _on_retained(cand: dict) -> None:
    notifier.notify("review", "AI patch ready for review",
                    f"{cand.get('title')} — {cand.get('reason')}",
                    key=f"review:{cand.get('id')}", link="#self-evolution")


code_evolution.on_retained = _on_retained

# Master switch: when the user disables OneDrive control the dashboard is fully
# hands-off (no start-up stop, no lock-state policy, manual Stop/Start refused),
# so OneDrive runs exactly as if the dashboard were not installed. Persisted so
# the choice survives a restart.
od_control = onedrive_control_mod.OneDriveControl(ONEDRIVE_CONTROL_FILE)
# Lock-state OneDrive policy: run while locked, stop while unlocked. Reads the
# lock state from the latest collected sample and reconciles drift on every tick
# (an edge-only policy latches and loses to Windows' OneDrive autostart).
session_policy = session_policy_mod.SessionOneDrivePolicy(
    fix_engine.runner, lambda: collector.latest,
    enabled=ONEDRIVE_LOCK_POLICY, fix_enabled=FIX_ENABLED,
    reconcile=SESSION_POLICY_RECONCILE,
    manual_grace_s=SESSION_POLICY_MANUAL_GRACE_S,
    control_disabled=od_control.disabled)
# Runaway-Explorer CPU watchdog: restarts the Windows shell when explorer.exe
# stays a heavy CPU consumer across several intervals (a recurring transient
# spike the scorecard/RCA path does not catch). Reads the live per-process CPU
# history, honours FIX_ENABLED, cools down between restarts, and persists its
# enable flag + action history.
proc_watchdog = procwatch_mod.ExplorerCpuWatchdog(
    fix_engine.runner, lambda: list(collector.history), PROCWATCH_STATE_FILE,
    threshold_pct=PROCWATCH_CPU_PCT, sustain=PROCWATCH_SUSTAIN,
    cooldown_s=PROCWATCH_COOLDOWN_S, enabled=PROCWATCH_ENABLED,
    fix_enabled=FIX_ENABLED, max_age_s=PROCWATCH_MAX_AGE_S)
# Quit listed tray apps (ChatGPT, WhatsApp, ...) once their window has been closed
# for a grace period, so their RAM is returned. Honours FIX_ENABLED.
def _tray_event(kind: str, d: dict) -> None:
    if kind == "quit":
        notifier.notify("action", f"Quit {d['name']} from the tray",
                        f"Freed about {d.get('freed_mb', 0)} MB after "
                        f"{d.get('hidden_s', 0)} s with no window. Reopen it from App Control.",
                        key=f"trayquit:{d['name']}:{int(d.get('at', 0))}", link="#app-control")
    elif kind == "consent":
        notifier.notify("consent", f"May SysPerf quit {d['name']}?",
                        f"It has been in the tray for {d.get('hidden_s', 0)} s holding about "
                        f"{d.get('mem_mb', 0)} MB. Choose Allow or Never in App Control.",
                        key=f"consent:{d['name']}", link="#app-control")


tray_quitter = trayquit_mod.TrayQuitter(TRAYQUIT_STATE_FILE, TRAYQUIT_LOG_FILE,
                                        fix_enabled=FIX_ENABLED, on_event=_tray_event,
                                        default_enabled=not STORE_MODE)
# Store mode: everything that acts outside the app is opt-in and listed on Home.
store_features = store_mode_mod.StoreFeatures(
    DATA_DIR / "store_features.json", store_mode=STORE_MODE, action_policy=action_policy,
    improver=improver, proc_watchdog=proc_watchdog, tray_quitter=tray_quitter,
    session_policy=session_policy)
app = Flask(__name__, static_folder=None)

# Per-process CSRF token: required on POST /api/fix so a mutating action cannot
# be triggered by a blind cross-origin request. Handed to the page via /api/fixes.
FIX_TOKEN = secrets.token_urlsafe(24)


@app.after_request
def harden(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


# Fields that identify the specific machine / network are masked in the public
# /api/metrics response so the open-sourced dashboard (and any live demo) does
# not leak personal data. Internal scoring/RCA still use the un-redacted snapshot.
_REDACT_KEYS = {"ssid": "home-network", "bssid": ""}


def _redact(obj):
    """Return a copy of the snapshot with identifying network fields masked."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _REDACT_KEYS and not isinstance(v, (dict, list)):
                out[k] = (_REDACT_KEYS[k.lower()] if v else v)
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


@app.get("/api/metrics")
def api_metrics():
    return jsonify(_redact(collector.snapshot()))


@app.get("/api/live")
def api_live():
    """Real-time headline values (in-process Win32 reads; see livestats.py)."""
    return jsonify(livestats_mod.read())


def _last_sample_age():
    last = collector.last_ok_at if isinstance(getattr(collector, "last_ok_at", None),
                                              (int, float)) else None
    return None if last is None else round(time.time() - last)


@app.get("/api/health")
def api_health():
    """Liveness plus the platform facts that decide whether collection can work.

    A native-Windows install with no PowerShell used to look identical to a
    healthy one that had simply not sampled yet. Reporting the resolved platform,
    shell and data directory makes that distinguishable without reading the log.
    """
    return jsonify({
        "ok": collector.error is None and bool(collector.latest),
        "samples": len(collector.history),
        "version": APP_VERSION,
        "error": collector.error,
        "platform": platform_describe(PLATFORM),
        "data_dir": str(DATA_DIR),
        "settings_file": str(CONFIG_FILE) if CONFIG_FILE.exists() else None,
        # Used by the supervisor to tell "answering" from "actually sampling".
        "last_sample_age_s": _last_sample_age(),
        "supervised": os.environ.get("SYSPERF_SUPERVISED") == "1",
    })


@app.get("/api/fixes")
def api_fixes():
    """Detect applicable common fixes for the current snapshot (read-only)."""
    snap = collector.snapshot()
    return jsonify({
        "enabled": FIX_ENABLED,
        "token": FIX_TOKEN,
        "fixes": fix_engine.detect(snap),
    })


@app.post("/api/fix")
def api_fix():
    """Preview (dry-run) or apply common fixes.

    Body: {"ids": [...], "confirm": bool, "token": "<csrf>"}.
    Without confirm=true this only previews. Applying requires the CSRF token,
    FIX_ENABLED, and only executes reversible non-disruptive fixes.
    """
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or None
    confirm = bool(body.get("confirm"))
    snap = collector.snapshot()

    if not confirm:
        return jsonify({"mode": "preview",
                        "results": fix_engine.preview(ids, snap)})

    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    if not FIX_ENABLED:
        return jsonify({"error": "fix execution disabled (PERFDASH_FIX_ENABLED=0)"}), 403

    return jsonify({"mode": "apply", "results": fix_engine.apply(ids, snap)})


@app.get("/api/onedrive")
def api_onedrive():
    """Current OneDrive run state (read-only) + whether it is default-stopped."""
    latest = collector.latest or {}
    return jsonify({**onedrive_mod.status(collector.latest),
                    "default_stopped": ONEDRIVE_DEFAULT_STOPPED,
                    "execution_enabled": FIX_ENABLED,
                    "control_disabled": od_control.disabled(),
                    "last_sync": latest.get("onedrive_last_sync"),
                    "session_locked": latest.get("session_locked"),
                    "lock_policy": session_policy.status()})


@app.post("/api/onedrive")
def api_onedrive_toggle():
    """Start or stop OneDrive. Body: {"action": start|stop, "token": "<csrf>"}.

    Explicit, user-visible and reversible; requires the CSRF token and honours
    FIX_ENABLED (the same read-only kill switch the fix engine uses).
    """
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    if not FIX_ENABLED:
        return jsonify({"error": "fix execution disabled (PERFDASH_FIX_ENABLED=0)"}), 403
    if od_control.disabled():
        return jsonify({"error": "OneDrive control is disabled — the dashboard "
                        "is hands-off; re-enable control to use Start/Stop"}), 409
    try:
        result = onedrive_mod.apply(fix_engine.runner, str(body.get("action")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    # Explicit user intent: suspend policy reconciliation for the grace window so
    # this click is not reverted on the next tick.
    session_policy.note_manual(str(body.get("action")))
    return jsonify(result)


@app.get("/api/onedrive/control")
def api_onedrive_control():
    """Read the OneDrive-control master switch (read-only)."""
    return jsonify({**od_control.status(), "execution_enabled": FIX_ENABLED})


@app.post("/api/onedrive/control")
def api_onedrive_control_set():
    """Enable/disable all dashboard control of OneDrive.

    Body: {"disabled": bool, "token": "<csrf>"}. When ``disabled`` is true the
    dashboard performs NO OneDrive actions — no stop-at-start-up, no lock-state
    autostart/auto-stop, and the manual Start/Stop endpoint refuses to run.
    OneDrive then behaves exactly as if this dashboard were not installed
    (Windows manages it). The setting is persisted across restarts. This is a
    policy switch only — it never itself starts or stops OneDrive.
    """
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    if "disabled" not in body:
        return jsonify({"error": "body must include boolean 'disabled'"}), 400
    disabled = od_control.set(bool(body.get("disabled")))
    # Clear any lingering manual-hold so a later re-enable starts from a clean
    # policy state rather than an expired hold window.
    if disabled:
        session_policy.manual_until = 0.0
    return jsonify({"control_disabled": disabled})


@app.get("/api/appctl")
def api_appctl():
    """Top CPU applications with per-app start/stop eligibility (read-only)."""
    apps = _cached("appctl", lambda: appctl_mod.top_apps(collector.history))
    rows = collector.snapshot().get("scorecard", {}).get("rows", [])
    cpu_row = next((r for r in rows if r.get("key") == "cpu_pct"), None)
    total = appctl_mod.total_cpu(cpu_row, (collector.latest or {}).get("cpu_pct"))
    # Instantaneous top CPU consumers (all processes incl. system/transient),
    # captured directly by the probe. Surfaces what cumulative-diff attribution
    # misses (pwsh, search, security agents).
    cpu_top = (collector.latest or {}).get("cpu_top") or []
    return jsonify({**apps, "total_cpu": total, "cpu_top": cpu_top,
                    "execution_enabled": FIX_ENABLED})


@app.post("/api/appctl")
def api_appctl_toggle():
    """Start or stop a top-CPU application.

    Body: {"action": start|stop, "name": "<proc>", "token": "<csrf>"}.
    Only names in the current server-computed top-N allowlist are accepted, and
    critical system/security processes are never controllable.
    """
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    if not FIX_ENABLED:
        return jsonify({"error": "fix execution disabled (PERFDASH_FIX_ENABLED=0)"}), 403
    action = str(body.get("action"))
    name = str(body.get("name") or "")
    # WSL shutdown is a system-level, session-killing action (not a top-CPU app),
    # so it bypasses the per-process allowlist but still requires the CSRF token
    # and FIX_ENABLED checked above.
    if action == "wsl-shutdown":
        return jsonify(appctl_mod.wsl_shutdown(fix_engine.runner))
    if action not in ("start", "stop"):
        return jsonify({"error": "action must be one of start, stop, wsl-shutdown"}), 400
    if name not in appctl_mod.controllable_names(collector.history):
        return jsonify({"error": f"{name!r} is not a controllable top-CPU application"}), 400
    try:
        if action == "stop":
            result = appctl_mod.stop(fix_engine.runner, name)
        else:
            result = appctl_mod.start(fix_engine.runner, name,
                                      appctl_mod.last_exe(collector.history, name))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.get("/api/procwatch")
def api_procwatch():
    """Explorer CPU watchdog status (read-only): live CPU, config, last action."""
    return jsonify(proc_watchdog.status())


@app.post("/api/procwatch")
def api_procwatch_set():
    """Enable/disable the Explorer CPU auto-fix. Body: {"enabled": bool, "token"}.

    When enabled, the watchdog restarts Windows Explorer automatically whenever
    explorer.exe stays at/above the CPU threshold across several intervals
    (cooldown-limited). Restarting the shell is reversible and loses no data —
    Windows relaunches it immediately. The choice is persisted across restarts.
    """
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    if "enabled" not in body:
        return jsonify({"error": "body must include boolean 'enabled'"}), 400
    return jsonify({"enabled": proc_watchdog.set_enabled(bool(body.get("enabled")))})


@app.get("/api/trayquit")
def api_trayquit():
    """Tray auto-quit status: settings, live app states, suggestions, recent quits."""
    return jsonify(tray_quitter.status())


@app.post("/api/trayquit")
def api_trayquit_set():
    """Change tray auto-quit.

    Body: {"token", "enabled"?, "grace_s"?, "add"?, "remove"?, "busy_cpu_pct"?,
    "consent"?: {"name", "decision": ask|allow|never}, "reopen"?: "<name>"}.
    ``reopen`` starts again the executable recorded when that app was quit.
    """
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    try:
        if body.get("reopen"):
            return jsonify({**tray_quitter.reopen(str(body["reopen"])),
                            "status": tray_quitter.status()})
        tray_quitter.configure(enabled=body.get("enabled"), grace_s=body.get("grace_s"),
                               add=body.get("add"), remove=body.get("remove"),
                               consent=body.get("consent"),
                               busy_cpu_pct=body.get("busy_cpu_pct"))
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    if PLATFORM == "windows":
        tray_quitter.tick()           # reflect the change immediately
    return jsonify(tray_quitter.status())


@app.post("/api/procwatch/run")
def api_procwatch_run():
    """Force one watchdog evaluation now. Body: {"token": "<csrf>"}.

    Runs the same check the background loop runs: restarts Explorer if it is a
    sustained CPU hog and outside the cooldown. Returns the action result, or a
    note when nothing needed doing.
    """
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    if not FIX_ENABLED:
        return jsonify({"error": "fix execution disabled (PERFDASH_FIX_ENABLED=0)"}), 403
    result = proc_watchdog.tick()
    if result is None:
        return jsonify({"status": "noop", **proc_watchdog.status()})
    return jsonify(result)


# --------------------------------------------------------------------------- #
# autonomous improvement (scan every 5 min -> RCA -> fix -> verify -> learn)   #
# --------------------------------------------------------------------------- #
# The Auto-Improve tab polls /api/rca and /api/trends every 30 s while it is
# open. Both walk the full history and every metric's daily record, so an
# uncached endpoint would add measurable CPU load to the very machine this
# dashboard scores. They are memoised for the same TTL the metrics snapshot uses.
_analysis_cache: dict = {}


def _cached(name: str, builder):
    now = time.time()
    hit = _analysis_cache.get(name)
    if hit is not None and now - hit[0] < SNAPSHOT_TTL:
        return hit[1]
    value = builder()
    _analysis_cache[name] = (now, value)
    return value


@app.get("/api/trends")
def api_trends():
    """Long-term (multi-week) usage trends: per-metric drift + usage profile."""
    return jsonify(_cached("trends",
                           lambda: trend_store.summary(collector.latest)))


@app.get("/api/rca")
def api_rca():
    """On-demand root-cause analysis of the current score (read-only)."""
    return jsonify(_cached("rca", lambda: rca_mod.analyze(
        collector.snapshot(), trend_store, fix_engine, improver.threshold)))


@app.get("/api/autoimprove")
def api_autoimprove():
    """Loop status: mode, cadence, last cycle, learned effectiveness, proposals."""
    limit = min(50, max(1, request.args.get("limit", default=10, type=int)))
    cpu = _cached("cpu_consumers",
                  lambda: cpuwatch.consistent_cpu(collector.history))
    od = {**onedrive_mod.status(collector.latest),
          "default_stopped": ONEDRIVE_DEFAULT_STOPPED,
          "execution_enabled": FIX_ENABLED}
    broker = {
        "online": fixes_mod._broker_online(),
        "exchange_dir": STANDBY_BROKER_DIR_WIN,
        "script_dir": STANDBY_BROKER_SCRIPT_DIR,
        # Exact command to (re)start the elevated standby helper after a reboot.
        # Run in an Administrator PowerShell.
        "relaunch_command": (
            "Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; "
            f"cd '{STANDBY_BROKER_SCRIPT_DIR}'; .\\standby_broker.ps1"),
    }
    return jsonify({**improver.status(), "recent": improver.recent(limit),
                    "cpu_consumers": cpu, "onedrive": od, "broker": broker,
                    "auto_decisions": auto_target_store.decisions()})


@app.post("/api/autoimprove/run")
def api_autoimprove_run():
    """Force a scan/RCA cycle now. Body: {"token": "<csrf>", "force": bool}.

    `force` also overrides mode=off/observe for this one cycle, so a user can
    ask for remediation explicitly without changing the standing mode.
    """
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    record = improver.cycle(force=bool(body.get("force")))
    _analysis_cache.clear()      # the cycle may have changed what RCA would say
    return jsonify(record)


@app.post("/api/autoimprove/mode")
def api_autoimprove_mode():
    """Set the standing mode. Body: {"mode": off|observe|auto, "token": "<csrf>"}."""
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    try:
        mode = improver.set_mode(str(body.get("mode")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"mode": mode})


@app.get("/api/learning")
def api_learning():
    """Continuous-learning view: KPIs, strategies, activity, rulebook audit.

    Derived read-only from the loop's own state and cycle log — see learning.py.
    """
    limit = min(200, max(1, request.args.get("limit", default=40, type=int)))
    return jsonify(_cached(f"learning:{limit}",
                           lambda: learning_view.snapshot(limit)))


@app.get("/api/selftest")
def api_selftest():
    """Live status of the repo's own seven quality gates (read-only)."""
    return jsonify(self_test.status())


@app.post("/api/selftest/run")
def api_selftest_run():
    """Run the test suite in the background.

    Body: {"token": "<csrf>", "live": bool, "quick": bool}. Mutating only in that
    it spawns work, so it is CSRF-guarded like every other POST here. At most one
    run is in flight; a second request is refused, not queued.
    """
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    result = self_test.run(live=bool(body.get("live")),
                           quick=bool(body.get("quick")))
    return jsonify({**result, "status": self_test.status()})


@app.get("/api/actions")
def api_actions():
    """The human action queue: what the loop cannot do, with steps and a reason."""
    return jsonify(_cached("actions", lambda: action_queue.report(
        rca_mod.analyze(collector.snapshot(), trend_store, fix_engine,
                        improver.threshold))))


@app.post("/api/actions/status")
def api_actions_status():
    """Mark a human action item done, or reopen it.

    Body: {"key": "<item key>", "status": "open"|"closed", "note": "...",
    "token": "<csrf>"}. Status is the one thing the queue owns, so it is guarded
    like every other mutating call here.
    """
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    try:
        result = action_queue.set_status(str(body.get("key") or ""),
                                        str(body.get("status") or "closed"),
                                        str(body.get("note") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    _analysis_cache.clear()
    return jsonify(result)


@app.get("/api/evolution")
def api_evolution():
    """Self-authoring layer: generation, candidates, shards, clusters, proposals."""
    return jsonify(evolution.status())


@app.post("/api/evolution/run")
def api_evolution_run():
    """Force an APE pass now. Body: {"token": "<csrf>"}.

    Generating a candidate is not the same as running it: nothing this returns can
    execute without a separate, explicit promotion.
    """
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    result = evolution.run_pass(force=True)
    return jsonify({**result, "status": evolution.status()})


@app.post("/api/evolution/promote")
def api_evolution_promote():
    """Promote or reject a candidate remediation.

    Body: {"id": "<candidate>", "action": "promote"|"reject", "confirm": true,
    "token": "<csrf>"}. ``confirm`` is required for a promotion specifically:
    this is the one call that turns generated capability into something the loop
    may choose, so it must not be reachable by accident.
    """
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    candidate_id = str(body.get("id") or "")
    action = str(body.get("action") or "promote")
    if action == "reject":
        result = evolution.reject(candidate_id)
    elif not body.get("confirm"):
        return jsonify({"error": "promotion requires confirm=true"}), 400
    else:
        result = evolution.promote(candidate_id)
    return jsonify({**result, "status": evolution.status()})


@app.post("/api/evolution/rollback")
def api_evolution_rollback():
    """Undo the most recent evolution. Body: {"token": "<csrf>"}."""
    body = request.get_json(silent=True) or {}
    if body.get("token") != FIX_TOKEN:
        return jsonify({"error": "invalid or missing CSRF token"}), 403
    result = evolution.rollback_last()
    return jsonify({**result, "status": evolution.status()})


def _register_promoted(records) -> int:
    """Make human-promoted, evolution-generated remediations visible to RCA.

    Each is registered as a ``manual`` Fix with no command: the loop will report it
    with the exact instruction and skip it, which is the honest behaviour for a
    recipe whose library entry supplies advice rather than an executable command.
    A detector is attached so it only surfaces while its metric is actually short.

    This mutates ``rca.REMEDIATION`` **in memory only** — it is how the process
    gains the capability for this run. ``rca.py`` on disk is never written, so
    evolution remains confined to its own two paths (evolve.py invariant 4) and a
    restart re-derives the mapping from the promoted records.
    """
    added = 0
    for record in records:
        metric = record["metric"]
        fix = fixes_mod.Fix(
            id=record["id"], title=record["title"], category=record["category"],
            risk="manual", reversible=record["reversible"],
            shell=record["shell"], command="",
            detect=_shortfall_detector(metric, record["title"]),
            manual_hint=record["manual_hint"])
        if fix_engine.register(fix):
            rca_mod.REMEDIATION.setdefault(metric, []).append(record["id"])
            added += 1
    return added


def _shortfall_detector(metric: str, title: str):
    """A Fix detector that fires only while `metric` is below target."""
    def detect(snapshot: dict):
        row = next((r for r in snapshot.get("scorecard", {}).get("rows", [])
                    if r.get("key") == metric), None)
        if row is None or row.get("status") == "met":
            return None
        return (f"{row.get('label') or metric} is {row.get('status')} — "
                f"evolution proposed '{title}' for it")
    return detect


# Register anything a human promoted in an earlier run, so a service restart does
# not silently drop promoted capability.
_register_promoted(evolution.promoted_fixes())
evolution.on_promote = _register_promoted


def _evolution_tick(record=None):
    """Advance the APE counter after a loop cycle and run a pass when due.

    Deliberately failure-isolated: the evolution layer is an enhancement, and a
    bug in it must never stop the remediation loop that keeps the machine healthy.
    """
    if not EVOLVE_EVERY:
        return {"ran": False, "reason": "evolution disabled (PERFDASH_EVOLVE_EVERY=0)"}
    try:
        if evolution.tick(cycles=improver.status().get("cycles")):
            return evolution.run_pass()
        return {"ran": False, "reason": "not due"}
    except Exception:
        logging.exception("evolution tick failed")
        return {"ran": False, "reason": "evolution tick failed (logged)"}


# Bound after definition: the engine needs the learning view, which needs the
# improver, so the hook cannot be passed at construction without a cycle.
def _on_cycle(record=None):
    """Loop-cycle hook: notify what matters, then advance the evolution layer."""
    rec = record or {}
    try:
        if rec.get("breach") and rec.get("score") is not None:
            notifier.notify("score", f"System score {rec['score']:.0f}% is below "
                            f"{rec.get('threshold', 80):.0f}%", rec.get("summary") or "",
                            key="score:breach", link="#home")
        done = [r for r in (rec.get("executed") or [])
                if isinstance(r, dict) and r.get("status") == "ok"]
        if done:
            notifier.notify("action", f"SysPerf applied {len(done)} fix(es)",
                            ", ".join(str(r.get("title") or r.get("id")) for r in done)[:280],
                            key=f"applied:{rec.get('t')}", link="#auto-improve")
    except Exception:
        logging.exception("cycle notification failed")
    return _evolution_tick(record)


improver.on_cycle = _on_cycle


def _startup_onedrive_policy():
    """Apply the 'OneDrive stopped by default' policy at service start."""
    if od_control.disabled():
        return None                    # dashboard OneDrive control is OFF
    if ONEDRIVE_LOCK_POLICY:
        # The lock policy owns OneDrive's state (it stops it while unlocked).
        # A blind start-up stop raced it and killed OneDrive it had just started
        # because the session was locked.
        return None
    return onedrive_mod.enforce_default(
        fix_engine.runner, enabled=ONEDRIVE_DEFAULT_STOPPED,
        fix_enabled=FIX_ENABLED)



# ------------------------------------------------------------- 4.0 API ----
def _csrf(body):
    return None if body.get("token") == FIX_TOKEN else (
        jsonify({"error": "invalid or missing CSRF token"}), 403)


@app.get("/api/codevolve")
def api_codevolve():
    return jsonify(code_evolution.status(action_policy.level))


@app.get("/api/codevolve/history")
def api_codevolve_history():
    return jsonify({"cycles": evo_db.cycles(limit=100),
                    "candidates": evo_db.candidates(limit=100),
                    "audit": evo_db.audit_tail(limit=40)})


@app.get("/api/codevolve/candidate/<cand_id>")
def api_codevolve_candidate(cand_id):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", cand_id):
        return jsonify({"error": "bad id"}), 400
    c = evo_db.candidate(cand_id)
    return (jsonify(c), 200) if c else (jsonify({"error": "not found"}), 404)


@app.post("/api/codevolve/run")
def api_codevolve_run():
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    reason = code_evolution.gate("manual", action_policy.level)
    if reason:
        return jsonify({"started": False, "reason": reason})
    threading.Thread(target=code_evolution.run_cycle,
                     args=("manual", action_policy.level), daemon=True).start()
    return jsonify({"started": True})


@app.post("/api/codevolve/settings")
def api_codevolve_settings():
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    try:
        settings = code_evolution.update_settings(body.get("evolution") or {})
        if body.get("llm") is not None:
            _save_llm_settings(body["llm"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "settings": settings,
                    "llm": code_evolution.status(action_policy.level)["llm"]})


LLM_EDITABLE = {"provider": str, "model": (str, type(None)), "base_url": str,
                "timeout_s": (int, float), "daily_token_budget": int,
                "daily_cost_budget_usd": (int, float), "prices": dict}


def _save_llm_settings(changes: dict) -> None:
    clean = {}
    for k, v in changes.items():
        if k not in LLM_EDITABLE or not isinstance(v, LLM_EDITABLE[k]) or isinstance(v, bool):
            raise ValueError(f"invalid llm setting '{k}'")
        clean[k] = v
    if clean.get("provider") not in (None, "none", "anthropic", "claude_code", "kiro_cli"):
        raise ValueError("provider must be none, anthropic, claude_code or kiro_cli")
    if "base_url" in clean and not clean["base_url"].startswith("https://"):
        raise ValueError("base_url must be https")
    if clean.get("model") and not re.fullmatch(r"[A-Za-z0-9._:@/-]{1,100}", clean["model"]):
        raise ValueError("model id has unexpected characters")
    try:
        all_s = json.loads(EVO_SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        all_s = {}
    all_s.setdefault("llm", {}).update(clean)
    tmp = EVO_SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(all_s, indent=1))
    tmp.replace(EVO_SETTINGS_FILE)
    evo_db.audit("user", "settings.llm", clean)
    code_evolution.llm = _build_llm()


@app.post("/api/llm/key")
def api_llm_key():
    """Store the API key (0600). The key is never echoed, logged or returned."""
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    try:
        llm_mod.store_api_key(LLM_KEY_FILE, str(body.get("key") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    evo_db.audit("user", "llm.key_stored", {"file": LLM_KEY_FILE.name})
    code_evolution.llm = _build_llm()
    return jsonify({"ok": True})


@app.get("/api/llm/models")
def api_llm_models():
    try:
        return jsonify({"models": code_evolution.llm.provider.list_models()})
    except llm_mod.LLMError as exc:
        return jsonify({"models": [], "error": exc.kind, "message": str(exc)})


@app.post("/api/codevolve/review")
def api_codevolve_review():
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    try:
        return jsonify(code_evolution.review(str(body.get("id") or ""),
                                             str(body.get("decision") or "")))
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/autonomy")
def api_autonomy():
    return jsonify({**action_policy.describe(),
                    "effectiveness": action_policy.effectiveness(),
                    "runs": evo_db.action_runs(limit=50)})


@app.post("/api/autonomy")
def api_autonomy_set():
    """Body: {token, level?} or {token, action, enabled}."""
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    try:
        if "level" in body:
            action_policy.set_level(str(body["level"]))
            improver.set_mode(action_policy.improver_mode())
        if "action" in body:
            action_policy.set_enabled(str(body["action"]), bool(body.get("enabled")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(action_policy.describe())


# ------------------------------------------------------------- 4.1 API ----
def _restart_soon(delay_s: float = 1.5) -> bool:
    """Ask the supervisor for a restart (exit RESTART_CODE) after this response."""
    if getattr(sys, "frozen", False):
        # The Store app has no supervisor: start a fresh copy, which waits for
        # this one to free the port, then exit.
        threading.Timer(delay_s, _relaunch).start()
        return True
    if os.environ.get("SYSPERF_SUPERVISED") != "1":
        return False
    threading.Timer(delay_s, os._exit, args=(RESTART_CODE,)).start()
    return True


def _relaunch(spawn=None, exit_fn=None) -> None:
    import subprocess
    (spawn or subprocess.Popen)([sys.executable, "--relaunch"], close_fds=True)
    (exit_fn or os._exit)(0)


@app.post("/api/codevolve/apply")
def api_codevolve_apply():
    """Deploy a retained candidate: approve, fast-forward main, restart, verify."""
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    try:
        res = code_evolution.apply(str(body.get("id") or ""))
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    if STORE_MODE:
        # The installed package is read-only and signed: the patch lands in the
        # user's source checkout, and reaches this app with the next build.
        return jsonify({**res, "restarting": False,
                        "note": "Applied to your source checkout. Rebuild the package "
                                "(packaging/msix/build-msix.ps1) to ship it."})
    return jsonify({**res, "restarting": _restart_soon()})


@app.post("/api/codevolve/rollback")
def api_codevolve_rollback():
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    try:
        res = code_evolution.rollback(body.get("id") or None, reason="rolled back by user")
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({**res, "restarting": _restart_soon()})


@app.post("/api/restart")
def api_restart():
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    if not _restart_soon():
        return jsonify({"error": "SysPerf is not running under its supervisor, so it "
                                 "cannot restart itself; restart the sign-in task"}), 400
    return jsonify({"restarting": True})


def _supervisor_log(limit: int = 20) -> list:
    try:
        lines = (DATA_DIR / "supervisor_log.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return list(reversed(out))


@app.get("/api/diagnostics")
def api_diagnostics():
    """The service's own health: problems, recent errors, workers, footprint."""
    return jsonify({
        "version": APP_VERSION, "health": health_monitor.check(),
        "errors": error_buffer.items()[:60],
        "collector": collector.stats(), "history": history_store.stats(),
        "footprint": health_mod.footprint(SERVICE_STARTED),
        "supervised": os.environ.get("SYSPERF_SUPERVISED") == "1",
        "supervisor_log": _supervisor_log(),
        "deploy": {"pending": code_evolution.pending_apply(),
                   "history": list(reversed(codevolve_mod.read_apply_log(DATA_DIR, 10)))},
    })


@app.post("/api/diagnostics/clear")
def api_diagnostics_clear():
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    error_buffer.clear()
    return jsonify({"cleared": True})


@app.get("/api/notifications")
def api_notifications():
    return jsonify({**notifier.items(), "settings": notifier.describe()})


@app.post("/api/notifications")
def api_notifications_post():
    """Body: {token, read: [ids] | "all"} or {token, test: true}."""
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    if body.get("test"):
        item = notifier.notify("health", "SysPerf test notification",
                               "If you can read this, notifications work.",
                               key=f"test:{time.time()}", link="#settings")
        return jsonify({"sent": item})
    ids = body.get("read")
    if ids != "all" and not (isinstance(ids, list) and all(isinstance(i, int) for i in ids)):
        return jsonify({"error": "read must be a list of ids or \"all\""}), 400
    return jsonify({"marked": notifier.mark_read(None if ids == "all" else ids),
                    **notifier.items()})


@app.get("/api/history")
def api_history():
    """Hourly long-range series for one metric. Query: key, hours (1..8760)."""
    key = request.args.get("key", "")
    try:
        hours = int(request.args.get("hours", "168"))
    except ValueError:
        return jsonify({"error": "hours must be a whole number"}), 400
    if key not in history_store.rollup_keys:
        return jsonify({"error": f"unknown metric {key!r}",
                        "keys": list(history_store.rollup_keys)}), 400
    if not 1 <= hours <= 8760:
        return jsonify({"error": "hours must be between 1 and 8760"}), 400
    return jsonify({"key": key, "hours": hours, "points": history_store.series(key, hours)})


def _today_start() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _agent_today() -> list:
    """What the agent did since local midnight, newest first."""
    since, out = _today_start(), []
    for rec in improver.recent(limit=300):
        if not isinstance(rec, dict) or (rec.get("t") or 0) < since:
            continue
        for r in rec.get("executed") or []:
            if isinstance(r, dict) and r.get("status") == "ok":
                out.append({"at": rec["t"], "kind": "fix",
                            "text": f"Applied {r.get('title') or r.get('id')}"})
    for ev in tray_quitter.status().get("recent", []):
        if (ev.get("at") or 0) >= since:
            out.append({"at": ev["at"], "kind": "trayquit",
                        "text": f"Quit {ev.get('name')} from the tray, freeing about "
                                f"{ev.get('freed_mb', 0)} MB"})
    for c in evo_db.cycles(limit=20):
        if (c.get("started") or 0) >= since:
            out.append({"at": c["started"], "kind": "evolution",
                        "text": f"Self-evolution cycle {c.get('state')}: "
                                f"{(c.get('explanation') or '')[:160]}"})
    return sorted(out, key=lambda e: -e["at"])[:30]


@app.get("/api/home")
def api_home():
    """Everything the Home view needs in one call."""
    snap = collector.snapshot()
    sc = snap.get("scorecard") or {}
    try:
        report = _cached("actions", lambda: action_queue.report(
            rca_mod.analyze(snap, trend_store, fix_engine, improver.threshold)))
        # The human-facing fields, not "command": for an elevation-gated fix that is
        # the service's own helper script, which is meaningless to run by hand.
        todo = [{k: i.get(k) for k in ("key", "title", "kind", "why_code", "why_headline",
                                       "simple_ask", "steps", "effort", "priority")}
                for i in report.get("items", []) if i.get("status") == "open"][:3]
    except Exception:
        logging.exception("home: action queue unavailable")
        todo = []
    consent = tray_quitter.status().get("awaiting_consent", [])
    return jsonify({
        "score": sc.get("overall_improvement"), "counts": sc.get("counts") or {},
        "threshold": improver.threshold,
        "health": health_monitor.check(), "last_sample_age_s": _last_sample_age(),
        "todo": todo, "consent": consent, "today": _agent_today(),
        "notifications": notifier.items(limit=5),
        "evolution": {"pending": code_evolution.pending_apply(),
                      "awaiting_review": sum(1 for c in evo_db.candidates(limit=50)
                                             if c.get("review_state") == "pending"
                                             and c.get("decision") == "retained")},
        "history_keys": list(history_store.rollup_keys),
        "optional": store_features.describe()["features"] if STORE_MODE else None,
    })


@app.get("/api/store")
def api_store():
    return jsonify(store_features.describe())


@app.post("/api/store")
def api_store_set():
    """Body: {token, feature, on} — turn one opt-in feature on or off."""
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    try:
        return jsonify(store_features.set(str(body.get("feature") or ""), bool(body.get("on"))))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


# ---- unified settings ------------------------------------------------------
_config_file = settings_mod.ConfigFile(CONFIG_FILE)
_F = settings_mod.Field
_GENERAL_PATHS = {"sample_seconds": "sample_seconds", "open_browser": "open_browser",
                  "fixes_enabled": "fixes.enabled", "auto_threshold": "autoimprove.threshold",
                  "auto_interval_s": "autoimprove.interval_seconds",
                  "history_raw_days": "history.raw_days",
                  "persistent_shell": "collector.persistent_shell"}
_GENERAL_LIVE = {"sample_seconds": SAMPLE_SECONDS, "open_browser": True,
                 "fixes_enabled": FIX_ENABLED, "auto_threshold": None,
                 "auto_interval_s": AUTO_INTERVAL_S, "history_raw_days": HISTORY_RAW_DAYS,
                 "persistent_shell": PERSISTENT_SHELL}


def _general_get() -> dict:
    live = dict(_GENERAL_LIVE, auto_threshold=improver.threshold)
    return {k: _config_file.get(p, live[k]) for k, p in _GENERAL_PATHS.items()}


def _general_apply(diff: dict) -> None:
    _config_file.set_many({_GENERAL_PATHS[k]: v for k, v in diff.items()})
    if "auto_threshold" in diff:
        improver.threshold = float(diff["auto_threshold"])
    if "history_raw_days" in diff:
        history_store.raw_days = diff["history_raw_days"]


def _autonomy_apply(diff: dict) -> None:
    action_policy.set_level(diff["level"])
    improver.set_mode(action_policy.improver_mode())


_EVO_KEYS = ("enabled", "interval_s", "max_per_day", "auto_retain")


def _notify_get() -> dict:
    s = notifier.describe()
    out = {"toasts": s["toasts"], "min_interval_s": s["min_interval_s"],
           "quiet_start": -1 if s["quiet_start"] is None else s["quiet_start"],
           "quiet_end": -1 if s["quiet_end"] is None else s["quiet_end"]}
    out.update({f"kind_{k}": v for k, v in s["kinds"].items()})
    return out


def _notify_apply(diff: dict) -> None:
    changes = {k: (None if v == -1 else v) for k, v in diff.items()
               if not k.startswith("kind_")}
    kinds = {k[5:]: v for k, v in diff.items() if k.startswith("kind_")}
    if kinds:
        changes["kinds"] = kinds
    notifier.configure(changes)


def _tray_get() -> dict:
    s = tray_quitter.status()
    return {k: s[k] for k in ("enabled", "grace_s", "busy_cpu_pct", "apps")}


def _tray_apply(diff: dict) -> None:
    if "apps" in diff:
        bad = [a for a in diff["apps"] if not trayquit_mod.allowed(a)]
        if bad:
            raise ValueError(f"cannot auto-quit: {', '.join(bad)}")
    tray_quitter.configure(enabled=diff.get("enabled"), grace_s=diff.get("grace_s"),
                           busy_cpu_pct=diff.get("busy_cpu_pct"))
    if "apps" in diff:
        current = {a.lower(): a for a in tray_quitter.settings["apps"]}
        wanted = {a.lower(): a for a in diff["apps"]}
        for low, name in current.items():
            if low not in wanted:
                tray_quitter.configure(remove=name)
        for low, name in wanted.items():
            if low not in current:
                tray_quitter.configure(add=name)


settings_hub = settings_mod.SettingsHub([
    settings_mod.Section("general", "General", [
        _F("sample_seconds", "Sampling interval (seconds)", "int", min=30, max=600,
           restart=True, help="How often the machine is measured. Shorter is more "
                              "detailed but costs more CPU."),
        _F("auto_threshold", "Auto-improve threshold (%)", "int", min=50, max=100,
           help="The agent investigates and acts when the score drops below this."),
        _F("auto_interval_s", "Auto-improve scan interval (seconds)", "int", min=60,
           max=3600, restart=True),
        _F("fixes_enabled", "Allow SysPerf to run fixes", "bool", restart=True,
           help="Off: SysPerf only measures and recommends; nothing is executed."),
        _F("history_raw_days", "Keep raw samples for (days)", "int", min=1, max=90,
           help="Hourly summaries are kept for over a year regardless."),
        _F("persistent_shell", "Reuse one PowerShell process", "bool", restart=True,
           help="About twice as fast and lighter than starting one per sample."),
        _F("open_browser", "Open the dashboard when SysPerf starts", "bool")],
        _general_get, _general_apply,
        {"sample_seconds": 60, "auto_threshold": 80, "auto_interval_s": 300,
         "fixes_enabled": True, "history_raw_days": 14, "persistent_shell": True,
         "open_browser": True}),
    settings_mod.Section("autonomy", "Autonomy", [
        _F("level", "Autonomy level", "choice",
           choices=["off", "observe", "safe_auto", "self_evolve"],
           help="off: nothing autonomous · observe: recommend only · safe_auto: run "
                "enabled low-risk actions · self_evolve: also improve its own code")],
        lambda: {"level": action_policy.level}, _autonomy_apply, {"level": "observe"}),
    settings_mod.Section("evolution", "AI self-evolution", [
        _F("enabled", "Evolution enabled", "bool"),
        _F("interval_s", "Run a cycle every (seconds)", "int", min=300, max=604800),
        _F("max_per_day", "At most cycles per day", "int", min=0, max=48),
        _F("auto_retain", "Keep winning patches for review", "bool")],
        lambda: {k: code_evolution.settings[k] for k in _EVO_KEYS},
        lambda diff: code_evolution.update_settings(diff),
        {"enabled": False, "interval_s": 21600, "max_per_day": 4, "auto_retain": True}),
    settings_mod.Section("notifications", "Notifications", [
        _F("toasts", "Show Windows notifications", "bool"),
        _F("min_interval_s", "Repeat the same alert at most every (seconds)", "int",
           min=60, max=86400),
        _F("quiet_start", "Quiet hours start (hour, -1 = off)", "int", min=-1, max=23),
        _F("quiet_end", "Quiet hours end (hour, -1 = off)", "int", min=-1, max=23)]
        + [_F(f"kind_{k}", f"Notify: {label}", "bool") for k, label in notify_mod.KINDS.items()],
        _notify_get, _notify_apply,
        {"toasts": True, "min_interval_s": 1800, "quiet_start": -1, "quiet_end": -1,
         **{f"kind_{k}": True for k in notify_mod.KINDS}}),
    settings_mod.Section("trayquit", "Tray auto-quit", [
        _F("enabled", "Quit tray apps after their window closes", "bool"),
        _F("grace_s", "Wait before quitting (seconds)", "int", min=15, max=3600),
        _F("busy_cpu_pct", "Leave an app alone above this CPU (%)", "float", min=0.5, max=50),
        _F("apps", "Apps that may be quit", "list")],
        _tray_get, _tray_apply,
        {"enabled": True, "grace_s": 60, "busy_cpu_pct": 3.0,
         "apps": list(trayquit_mod.DEFAULT_APPS)}),
])


@app.get("/api/settings")
def api_settings():
    return jsonify(settings_hub.describe())


@app.get("/api/settings/export")
def api_settings_export():
    resp = jsonify(settings_hub.export())
    resp.headers["Content-Disposition"] = "attachment; filename=sysperf-settings.json"
    return resp


@app.post("/api/settings")
def api_settings_post():
    """Body: {token, values: {section: {key: value}}} — validated all-or-nothing."""
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    try:
        return jsonify(settings_hub.apply(body.get("values")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/settings/reset")
def api_settings_reset():
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    try:
        return jsonify(settings_hub.reset(body.get("section") or None))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/settings/import")
def api_settings_import():
    body = request.get_json(silent=True) or {}
    if (bad := _csrf(body)):
        return bad
    try:
        return jsonify(settings_hub.import_(body.get("doc")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


# ---- background housekeeping -------------------------------------------------
_health_state = {"problems": (), "confirmed": False, "pruned_day": None}


def _housekeeping_tick() -> dict:
    """Once a minute: health alerts, apply confirmation, log rotation, pruning."""
    check = health_monitor.check()
    probs = tuple(check["problems"])
    if probs and probs != _health_state["problems"]:
        notifier.notify("health", "SysPerf needs attention", "; ".join(probs)[:280],
                        key="health:" + "|".join(probs), link="#settings")
    _health_state["problems"] = probs
    if not _health_state["confirmed"] and collector.last_ok_at and \
            collector.last_ok_at >= SERVICE_STARTED:
        _health_state["confirmed"] = True
        done = code_evolution.confirm_apply(healthy=check["status"] != "error")
        if done:
            notifier.notify("review", "AI patch deployed",
                            f"{done.get('title') or done['id']} is now running and healthy.",
                            key=f"deployed:{done['id']}", link="#self-evolution")
    # Under the sign-in task stdout is the runtime log file; keep it bounded.
    log_name = getattr(sys.stdout, "name", None)
    if isinstance(log_name, str) and log_name.endswith(".log"):
        health_mod.rotate_log(log_name, stream=sys.stdout)
    day = time.strftime("%Y-%m-%d")
    if _health_state["pruned_day"] != day:
        _health_state["pruned_day"] = day
        history_store.prune()
    return check


def _housekeeping_loop(interval_s: float = 60.0) -> None:
    while True:
        time.sleep(interval_s)
        try:
            _housekeeping_tick()
        except Exception:
            logging.exception("housekeeping failed")


def _verify_actions_loop(interval_s: float = 120.0) -> None:
    while True:
        time.sleep(interval_s)
        try:
            action_policy.verify_pending({"latest": collector.latest})
            evo_db.prune()
        except Exception:                 # pragma: no cover - never kill the service
            logging.exception("action verification failed")

def _start(name: str, target, *args) -> threading.Thread:
    """Start a daemon worker and register it with the health monitor."""
    th = threading.Thread(target=target, args=args, name=name, daemon=True)
    th.start()
    return health_monitor.register_thread(name, th)


def _log_to_stream() -> logging.Handler:
    """Send INFO+ to stdout/stderr (the runtime log under the sign-in task).

    ``logging.basicConfig`` is a no-op once the root logger has any handler, and
    the diagnostics ErrorBuffer is attached at import — so the stream handler is
    added explicitly, once.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in root.handlers:
        if type(h) is logging.StreamHandler:
            return h
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(h)
    return h


def main() -> None:
    _log_to_stream()
    _start("collector", collector.loop)
    health_monitor.add_probe("collector", lambda: collector.last_ok_at,
                             max(600.0, 3 * SAMPLE_SECONDS + COLLECT_TIMEOUT_FULL_S),
                             "Data collection")
    # Autonomous improvement: scans every AUTO_INTERVAL_S (5 min), runs RCA when
    # the score is under threshold, applies safe remediations, then verifies and
    # learns from the measured effect.
    _start("auto-improve", improver.loop)
    health_monitor.add_probe("auto-improve",
                             lambda: (getattr(improver, "state", {}) or {}).get("last_at"),
                             3 * AUTO_INTERVAL_S + 600, "Auto-improve loop")
    # Enforce "OneDrive not running by default": stop it once at start-up (best
    # effort, off the request path). Reversible from the Auto-Improve tab.
    threading.Thread(target=_startup_onedrive_policy, daemon=True).start()
    # Lock-state OneDrive policy: run while locked, stop while unlocked.
    threading.Thread(
        target=session_policy.loop, args=(SESSION_POLICY_INTERVAL_S,),
        daemon=True).start()
    # Runaway-Explorer CPU watchdog: restart the shell when explorer.exe stays a
    # heavy CPU consumer across several intervals (recurring spike auto-fix).
    threading.Thread(
        target=proc_watchdog.loop, args=(PROCWATCH_INTERVAL_S,),
        daemon=True).start()
    # 4.0: source self-evolution runs on its own schedule, independent of the
    # monitoring loop and of any user prompt; action outcomes are judged here.
    if action_policy.level in ("off", "observe") and hasattr(improver, "set_mode"):
        improver.set_mode(action_policy.improver_mode())
    _start("self-evolution", code_evolution.loop, lambda: action_policy.level)
    _start("action-verification", _verify_actions_loop)
    _start("housekeeping", _housekeeping_loop)
    if PLATFORM == "windows":
        _start("tray-quit", tray_quitter.loop, TRAYQUIT_INTERVAL_S)
        health_monitor.add_probe("tray-quit", lambda: tray_quitter.last_view.get("at"),
                                 max(300.0, 10 * TRAYQUIT_INTERVAL_S), "Tray auto-quit")
    # Loopback bind is deliberate (see config.HOST) — read-only telemetry, no auth.
    serve(app, host=HOST, port=PORT)


def serve(wsgi_app, host: str, port: int) -> None:
    """Serve with waitress when it is installed, else Flask's own server.

    waitress has been a declared dependency since 2.x without ever being
    imported, so every install has been running the Werkzeug development server —
    which prints a warning, is single-purpose and is not meant to be left
    running. Preferring waitress and falling back keeps the package working in a
    bare checkout where only Flask is present.
    """
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        logging.warning("waitress is not installed — falling back to the Flask "
                        "development server. Install it with: pip install waitress")
        wsgi_app.run(host=host, port=port, threaded=True, debug=False)
        return
    logging.info("serving with waitress on http://%s:%s/", host, port)
    waitress_serve(wsgi_app, host=host, port=port, threads=8,
                   channel_timeout=120, ident="SysPerf")


if __name__ == "__main__":
    main()
