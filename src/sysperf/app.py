"""
HTTP layer — Flask application factory.

Read-only by default. Recalibration writes the local baseline; explicitly enabled
and confirmed fix requests may mutate Windows state. Both mutation routes are
guarded against cross-origin requests.

Security posture
----------------
* Binds to ``127.0.0.1`` by default. The API is telemetry about your machine —
  running processes, installed drivers, open connections — and should not be
  reachable from the network without a reason.
* If the bind host is **not** loopback, a token is required. One is generated
  and stored in the config file on first start rather than silently exposing an
  unauthenticated endpoint. Send it as ``X-SysPerf-Token`` or ``?token=``.
* There is no TLS. Over a network, put a reverse proxy in front of it.
"""
from __future__ import annotations

import logging
import secrets
import threading
from urllib.parse import urlencode, urlsplit

from flask import Flask, jsonify, request, send_from_directory

from .adaptive import AdaptiveScorer
from .collector import Collector
from .config import ADAPTIVE_FILE, APP_TITLE, STATIC_DIR, VERSION, Config
from .fixes import FixEngine
from .hardware import ensure_watchlist
from .platform_win import runtime_name

log = logging.getLogger("sysperf")


def create_app(cfg: Config, collector: Collector | None = None) -> Flask:
    if cfg.requires_token and not cfg.get("auth_token"):
        raise ValueError("non-loopback binds require an authentication token")
    scorer = AdaptiveScorer(ADAPTIVE_FILE, cfg)
    collector = collector or Collector(cfg, scorer)
    ensure_watchlist()

    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 16_384
    app.config["SYSPERF"] = {"cfg": cfg, "collector": collector, "scorer": scorer}
    token = str(cfg["auth_token"] or "")

    # Common-Fix engine. Detection + dry-run preview always work; execution is
    # opt-in (config fixes.enabled) and CSRF-gated. The dashboard stays
    # read-only unless the operator turns fixes on.
    fix_engine = FixEngine()
    fixes_enabled = bool((cfg.get("fixes") or {}).get("enabled"))
    fix_csrf = secrets.token_urlsafe(24)
    fix_lock = threading.Lock()

    @app.before_request
    def authenticate():
        # Host validation prevents DNS rebinding into an unauthenticated local API.
        hostname = urlsplit(request.host_url).hostname
        if cfg.is_loopback and hostname not in ("localhost", "127.0.0.1", "::1"):
            return jsonify({"error": "untrusted host"}), 403
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if request.headers.get("Sec-Fetch-Site") == "cross-site" or (
                origin and origin != request.host_url.rstrip("/")
            ):
                return jsonify({"error": "cross-origin mutation forbidden"}), 403
        if not token:
            return None
        supplied = request.headers.get("X-SysPerf-Token") or request.args.get("token", "")
        # Constant-time compare: a naive == leaks the token byte by byte to a
        # patient caller.
        if not secrets.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
            return jsonify({"error": "unauthorized"}), 401
        return None

    @app.after_request
    def harden(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return resp

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/favicon.ico")
    def favicon():
        return ("", 204)

    @app.get("/api/metrics")
    def api_metrics():
        return jsonify(collector.snapshot())

    @app.get("/api/advice")
    def api_advice():
        return jsonify(collector.snapshot().get("advice") or {})

    @app.get("/api/profile")
    def api_profile():
        snap = collector.snapshot()
        return jsonify({"profile": snap.get("profile"),
                        "baseline": snap.get("baseline"),
                        "criteria": [
                            {"key": k, "label": lbl, "baseline": b, "target": t,
                             "higher_is_better": hib, "unit": u}
                            for k, lbl, b, t, hib, u in collector.criteria()],
                        })

    @app.get("/api/config")
    def api_config():
        return jsonify(cfg.public_view())

    @app.get("/api/health")
    def api_health():
        return jsonify({
            "ok": collector.error is None and bool(collector.latest),
            "app": APP_TITLE,
            "version": VERSION,
            "runtime": runtime_name(),
            "samples": len(collector.history),
            "baseline_captured": bool(collector.baseline.get("metrics")),
            "error": collector.error,
        })

    @app.post("/api/recalibrate")
    def api_recalibrate():
        """
        Reset the baseline to the current state.

        Writes only SysPerf's own baseline.json. It is destructive to *history
        comparisons*, not to the system, so it is deliberately explicit rather
        than automatic.
        """
        if request.headers.get("X-SysPerf-CSRF") != fix_csrf:
            return jsonify({"error": "invalid or missing CSRF token"}), 403
        try:
            collector.recalibrate()
            return jsonify({"ok": True, "baseline": collector.snapshot()["baseline"]})
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.errorhandler(404)
    def not_found(_exc):
        return jsonify({"error": "not found"}), 404

    @app.get("/api/fixes")
    def api_fixes():
        """Detect applicable common fixes for the current snapshot (read-only)."""
        snap = collector.snapshot()
        return jsonify({"enabled": fixes_enabled, "token": fix_csrf,
                        "fixes": fix_engine.detect(snap)})

    @app.post("/api/fix")
    def api_fix():
        """
        Preview (dry-run) or apply common fixes.

        Body: ``{"ids": [...], "confirm": bool, "token": "<csrf>"}``. Without
        ``confirm`` this only previews. Applying requires ``fixes.enabled`` in
        the config plus the CSRF token, and only ever runs reversible,
        non-disruptive fixes; manual/session-killing ones are described, never run.
        """
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "JSON object required"}), 400
        ids = body.get("ids")
        confirm = body.get("confirm", False)
        if not isinstance(confirm, bool) or (ids is not None and (
            not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)
        )):
            return jsonify({"error": "ids must be a list of strings and confirm a boolean"}), 400
        if ids is not None and any(i not in fix_engine._by_id for i in ids):
            return jsonify({"error": "unknown fix id"}), 400
        snap = collector.snapshot()
        if not confirm:
            return jsonify({"mode": "preview", "results": fix_engine.preview(ids, snap)})
        if not fixes_enabled:
            return jsonify({"error": "fix execution disabled (set fixes.enabled=true)"}), 403
        if body.get("token") != fix_csrf:
            return jsonify({"error": "invalid or missing CSRF token"}), 403
        if not ids:
            return jsonify({"error": "explicit non-empty fix selection required"}), 400
        if not fix_lock.acquire(blocking=False):
            return jsonify({"error": "another fix is in progress"}), 409
        try:
            return jsonify({"mode": "apply", "results": fix_engine.apply(ids, snap)})
        finally:
            fix_lock.release()

    return app


def serve(cfg: Config, collector: Collector | None = None) -> None:
    """Start the sampler thread and run a WSGI server until interrupted."""
    app = create_app(cfg, collector)
    collector = app.config["SYSPERF"]["collector"]
    threading.Thread(target=collector.loop, daemon=True, name="sampler").start()

    host, port = cfg["host"], int(cfg["port"])
    url = f"http://{'127.0.0.1' if cfg.is_loopback else host}:{port}/"
    if not cfg.is_loopback:
        log.warning("Binding to %s — reachable from the network. Token auth is "
                    "enabled; there is no TLS. Use a reverse proxy for anything "
                    "beyond a trusted LAN.", host)
    log.info("%s %s — %s (runtime: %s)", APP_TITLE, VERSION, url, runtime_name())

    if cfg.get("open_browser"):
        import webbrowser
        browser_url = url
        if cfg.get("auth_token"):
            browser_url += "?" + urlencode({"token": cfg["auth_token"]})
        threading.Timer(1.5, lambda: webbrowser.open(browser_url)).start()

    try:
        # waitress is a real WSGI server and is pure Python, so it installs
        # cleanly on Windows. The Flask dev server is the fallback.
        from waitress import serve as waitress_serve
        waitress_serve(app, host=host, port=port, threads=8, ident="SysPerf",
                       clear_untrusted_proxy_headers=True)
    except ImportError:
        log.info("waitress not installed — using the Flask development server")
        app.run(host=host, port=port, threaded=True, debug=False)
