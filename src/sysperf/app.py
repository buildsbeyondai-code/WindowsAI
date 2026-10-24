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
import re
import secrets
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

from . import cpuwatch
from . import fixes as fixes_mod
from . import onedrive as onedrive_mod
from . import onedrive_control as onedrive_control_mod
from . import appctl as appctl_mod
from . import procwatch as procwatch_mod
from . import session_policy as session_policy_mod
from . import rca as rca_mod
from . import livestats as livestats_mod
from .config import (HOST, PORT, SNAPSHOT_TTL, STATIC_DIR, ADAPTIVE_FILE,
                    FIX_ENABLED, TRENDS_FILE, AUTO_STATE_FILE, AUTO_LOG_FILE,
                    AUTO_LOG_MAX_LINES, SELFTEST_REPORT_FILE, SELFTEST_ROOT,
                    EVOLVE_STATE_FILE, EVOLVE_DIR, EVOLVE_EVERY, CRITERIA,
                    ACTIONS_STATE_FILE, PLATFORM, DATA_DIR, CONFIG_FILE,
                    ONEDRIVE_DEFAULT_STOPPED, ONEDRIVE_LOCK_POLICY,
                    ONEDRIVE_CONTROL_FILE,
                    PROCWATCH_STATE_FILE, PROCWATCH_ENABLED, PROCWATCH_CPU_PCT,
                    PROCWATCH_SUSTAIN, PROCWATCH_COOLDOWN_S, PROCWATCH_INTERVAL_S,
                    PROCWATCH_MAX_AGE_S,
                    SESSION_POLICY_INTERVAL_S, SESSION_POLICY_RECONCILE,
                    SESSION_POLICY_MANUAL_GRACE_S,
                    STANDBY_BROKER_SCRIPT_DIR, STANDBY_BROKER_DIR_WIN,
                    AUTO_TARGETS_FILE, APP_VERSION)
from .actions import ActionQueue
from . import codevolve as codevolve_mod
from . import evodb as evodb_mod
from . import llm as llm_mod
from . import optactions as optactions_mod
from .adaptive import AdaptiveScorer
from .autoimprove import AutoImprover
from .collector import Collector
from .evolve import EvolutionEngine
from .fixes import FixEngine
from .learning import LearningView
from .platform_paths import describe as platform_describe
from .selftest import SelfTest
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

scorer = AdaptiveScorer(ADAPTIVE_FILE)
collector = Collector(scorer)
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
self_test = SelfTest(SELFTEST_ROOT, SELFTEST_REPORT_FILE)
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
action_policy = optactions_mod.ActionPolicy(DATA_DIR / "autonomy.json", evo_db,
                                            improver_mode=getattr(improver, "state", {}).get("mode"))
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
    evo_db, _build_llm(), EVO_SETTINGS_FILE, DATA_DIR / "evolution-worktrees")

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
    except Exception:                                    # noqa: BLE001
        logging.exception("evolution tick failed")
        return {"ran": False, "reason": "evolution tick failed (logged)"}


# Bound after definition: the engine needs the learning view, which needs the
# improver, so the hook cannot be passed at construction without a cycle.
improver.on_cycle = _evolution_tick


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
    if clean.get("provider") not in (None, "none", "anthropic", "kiro_cli"):
        raise ValueError("provider must be none, anthropic or kiro_cli")
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
            level = action_policy.set_level(str(body["level"]))
            improver.set_mode(action_policy.improver_mode())
        if "action" in body:
            action_policy.set_enabled(str(body["action"]), bool(body.get("enabled")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(action_policy.describe())


def _verify_actions_loop(interval_s: float = 120.0) -> None:
    while True:
        time.sleep(interval_s)
        try:
            action_policy.verify_pending({"latest": collector.latest})
            evo_db.prune()
        except Exception:                 # pragma: no cover - never kill the service
            logging.exception("action verification failed")

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=collector.loop, daemon=True).start()
    # Autonomous improvement: scans every AUTO_INTERVAL_S (5 min), runs RCA when
    # the score is under threshold, applies safe remediations, then verifies and
    # learns from the measured effect.
    threading.Thread(target=improver.loop, daemon=True).start()
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
    threading.Thread(target=code_evolution.loop, args=(lambda: action_policy.level,),
                     daemon=True).start()
    threading.Thread(target=_verify_actions_loop, daemon=True).start()
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
