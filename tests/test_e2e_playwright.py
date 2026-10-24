"""
Playwright end-to-end tests for the front-end behaviour added with the
2026-09-28 fixes:

* the "Score stabilising" badge on #summary (post-reboot scoring provenance), and
* the App Control OneDrive card's policy chips (reconciling / edge-only mode and
  the manual-hold indicator).

Design
------
The page is served from ``static/`` by a throwaway ``http.server`` thread and
every ``api/*`` request is fulfilled by Playwright request interception. That
keeps these tests hermetic: no Flask app, no collector, and crucially no
PowerShell subprocess — the real collector shells out to ``pwsh.exe`` on the
Windows host, which is neither available nor desirable in a test run.

Skipped automatically when Playwright or its Chromium build is absent, so the
default ``pytest tests`` run stays green on a machine without browsers.
"""
from __future__ import annotations

import http.server
import json
import socketserver
import threading
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="playwright is not installed")
from playwright.sync_api import Error as PWError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

# Repo layout keeps the page at <root>/static; an exported distribution keeps it
# at <root>/src/sysperf/static. One test file serves both.
_ROOT = Path(__file__).resolve().parent.parent
STATIC = _ROOT / "src" / "sysperf" / "static"
if not STATIC.is_dir():
    STATIC = _ROOT / "static"


# --------------------------------------------------------------- static server --
class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def log_message(self, *a):        # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def site() -> str:
    """Serve static/ on an ephemeral port for the duration of the module."""
    with socketserver.TCPServer(("127.0.0.1", 0), _QuietHandler) as httpd:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
        httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(args=["--no-sandbox"])
        except PWError as exc:                                  # pragma: no cover
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


# ------------------------------------------------------------------- fixtures --
def _metrics(*, warming_up=False, session_samples=15, stale_latest=False):
    """Smallest /api/metrics payload that renders the summary hero."""
    rows = [
        {"key": "available_mb", "label": "Available physical", "baseline": 7013,
         "current": 12544, "scored_typical": 12544, "target": 12000, "unit": "MB",
         "higher_is_better": True, "change_pct": 23.3, "progress": 100.0,
         "room": 0.0, "status": "met", "note": None, "shortfall": 0},
        {"key": "cpu_pct", "label": "CPU utilisation", "baseline": 40,
         "current": 100.0, "scored_typical": 100.0, "target": 25, "unit": "%",
         "higher_is_better": False, "change_pct": 150.0, "progress": 0.0,
         "room": 100.0, "status": "regress", "note": None, "shortfall": 75.0},
    ]
    return {
        "latest": {"cpu_pct": 100.0, "available_mb": 12544, "vmmem_mb": 4800,
                   "ncpu": 8, "processes": 358},
        "series": {"t": [1, 2, 3], "cpu_pct": [99, 100, 100]},
        "scorecard": {
            "rows": rows, "static": [], "overall_improvement": 50.0,
            "overall_room": 50.0, "scored": 2, "total_criteria": 2,
            "adaptive": {"metrics_tracked": 0, "metrics_scored": 0,
                         "self_improvement_score": 0, "improved_today": 0,
                         "at_personal_best": 0, "learning": 0},
            "vmmem_trend": 0.0, "vmmem_trend_span_min": 0.0,
            "vmmem_trend_points": 0,
            "counts": {"met": 1, "partial": 0, "regress": 1, "measuring": 0},
            "session_samples": session_samples, "smooth_window": 15,
            "warming_up": warming_up,
        },
        "browser": {}, "hardware": {}, "components": {"rows": [], "total": 0},
        "wifi_overlap": {"available": False, "reason": "n/a"}, "net_plan": [],
        "vmmem_trend_mb_per_h": 0.0,
        "meta": {"samples": 3, "sample_seconds": 20, "last_duration_s": 30.0,
                 "error": None, "server_time": "2026-09-28T05:00:00+00:00",
                 "baseline_date": "2026-08-25", "revision": "2.1",
                 "stale_latest": stale_latest, "fresh_samples": session_samples},
    }


def _onedrive(**over):
    base = {"running": True, "known": True, "default_stopped": True,
            "execution_enabled": True, "last_sync": 1790569715.0,
            "session_locked": False,
            "lock_policy": {"enabled": True, "last_locked": False,
                            "reconcile": True, "manual_hold_s": 0}}
    base.update(over)
    return base


def _open(browser, site, *, metrics=None, onedrive=None, hash_=""):
    """New page with all api/* calls stubbed. Returns the page."""
    page = browser.new_page()

    def handler(route, request):
        url = request.url
        if request.method == "POST":
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"action": "stop", "status": "ok"}))
            return
        if "api/metrics" in url:
            body = metrics if metrics is not None else _metrics()
        elif "api/onedrive" in url:
            body = onedrive if onedrive is not None else _onedrive()
        elif "api/fixes" in url:
            body = {"enabled": True, "token": "test-token", "fixes": []}
        elif "api/appctl" in url:
            body = {"apps": [], "total_cpu": 100.0, "execution_enabled": True}
        elif "api/autoimprove" in url:
            body = {"mode": "auto", "recent": [], "cpu_consumers": [],
                    "onedrive": {"running": True, "known": True}}
        elif "api/trends" in url:
            body = {"days_tracked": 0, "metrics": {}}
        elif "api/rca" in url:
            body = {"findings": [], "overall": 50.0}
        elif "api/health" in url:
            body = {"ok": True, "samples": 3, "error": None}
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body))

    page.route("**/api/**", handler)
    page.goto(site + hash_, wait_until="domcontentloaded")
    return page


# ------------------------------------------------------------ summary warm-up --
def test_summary_renders_without_warmup_badge_when_settled(browser, site):
    page = _open(browser, site, metrics=_metrics(warming_up=False))
    page.wait_for_selector("#view-summary .sum-ring", timeout=15_000)
    assert page.locator("#warmupBadge").count() == 0
    page.close()


def test_warmup_badge_shows_sample_counts_after_a_reboot(browser, site):
    """The indicator that would have explained the low post-reboot score."""
    page = _open(browser, site,
                 metrics=_metrics(warming_up=True, session_samples=4))
    badge = page.locator("#warmupBadge")
    badge.wait_for(timeout=15_000)
    text = badge.inner_text()
    assert "stabilising" in text.lower()
    assert "4 of 15 samples" in text
    page.close()


def test_warmup_badge_reports_a_stale_latest_sample(browser, site):
    """Before the first live sample lands, `latest` is replayed from history."""
    page = _open(browser, site,
                 metrics=_metrics(warming_up=True, session_samples=0,
                                  stale_latest=True))
    badge = page.locator("#warmupBadge")
    badge.wait_for(timeout=15_000)
    assert "first live sample" in badge.inner_text()
    page.close()


# --------------------------------------------------- app-control policy chips --
def _od_status(page):
    page.wait_for_selector("#odACCard", timeout=15_000)
    page.wait_for_function(
        "() => document.querySelector('#odACStatus')"
        "  && document.querySelector('#odACStatus').innerText.includes('OneDrive')",
        timeout=15_000)
    return page.locator("#odACStatus").inner_text()


def test_app_control_shows_reconciling_mode(browser, site):
    page = _open(browser, site, hash_="#app-control")
    text = _od_status(page)
    assert "running" in text                       # od fixture has running=True
    assert "unlocked" in text
    assert "reconciling" in text                   # policy will correct the drift
    assert "manual hold" not in text
    page.close()


def test_app_control_shows_edge_only_mode(browser, site):
    od = _onedrive(lock_policy={"enabled": True, "last_locked": False,
                                "reconcile": False, "manual_hold_s": 0})
    page = _open(browser, site, onedrive=od, hash_="#app-control")
    assert "edge-only" in _od_status(page)
    page.close()


def test_app_control_shows_manual_hold_countdown(browser, site):
    """A recent manual Start/Stop pauses reconciliation — say so, and for how long."""
    od = _onedrive(lock_policy={"enabled": True, "last_locked": False,
                                "reconcile": True, "manual_hold_s": 1500})
    page = _open(browser, site, onedrive=od, hash_="#app-control")
    text = _od_status(page)
    assert "manual hold 25m" in text               # ceil(1500/60)
    assert "policy paused" in text
    page.close()


def test_app_control_hides_mode_when_policy_disabled(browser, site):
    od = _onedrive(lock_policy={"enabled": False, "last_locked": None,
                                "reconcile": True, "manual_hold_s": 0})
    page = _open(browser, site, onedrive=od, hash_="#app-control")
    text = _od_status(page)
    assert "Lock-policy off" in text.replace("\n", " ")
    assert "reconciling" not in text
    page.close()


def test_stop_button_posts_the_stop_action(browser, site):
    page = _open(browser, site, hash_="#app-control")
    _od_status(page)
    # Wait on the request itself: #odACMsg is set to "Stopping OneDrive…"
    # synchronously before the fetch, so polling its text would race the POST.
    with page.expect_request(
            lambda r: r.method == "POST" and "api/onedrive" in r.url,
            timeout=15_000) as info:
        page.click("#odACStop")
    assert info.value.post_data_json["action"] == "stop"
    # and the CSRF token from /api/fixes is attached
    assert info.value.post_data_json["token"] == "test-token"
    page.wait_for_function(
        "() => document.querySelector('#odACMsg').innerText.includes('stop')",
        timeout=15_000)
    page.close()


def test_no_console_errors_on_summary(browser, site):
    """A JS exception during render would silently blank the hero."""
    page = _open(browser, site)
    errors: list = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.wait_for_selector("#view-summary .sum-ring", timeout=15_000)
    assert errors == []
    page.close()
