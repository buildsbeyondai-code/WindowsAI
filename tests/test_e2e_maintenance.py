"""
Browser coverage for the 5.3 maintenance sections on the Details tab:
Maintenance check, Protection, Cleanup, Registry, Security, Services & tasks and
App updates, driven from the stubbed api/maintenance report.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from tests import test_e2e_ui as ui  # noqa: E402 - after importorskip

STUBS, _open = ui.STUBS, ui._open
browser, site = ui.browser, ui.site
M = STUBS["api/maintenance"]


def _details(browser, site, rep=None, posts=None):
    page = _open(browser, site, hash_="#details/maintenance",
                 overrides={"api/maintenance": rep} if rep is not None else None, posts=posts)
    page.on("dialog", lambda d: d.accept())
    return page


def test_every_maintenance_section_renders_the_scan(browser, site):
    page = _details(browser, site)
    page.wait_for_function("() => document.getElementById('mtStatus').innerText.includes('Last scan')")
    for link in ("Maintenance", "Protection", "Cleanup", "Registry", "Security",
                 "Services & tasks", "App updates"):
        assert page.query_selector(f".det-nav a:text-is('{link}')")
    findings = page.inner_text("#mtFindings")
    assert findings.index("blue-screen") < findings.index("Browser caches")     # worst first
    assert "NEEDS ACTION" in findings.upper() and "DISM" in findings
    assert "none recorded" in page.inner_text("#mtProtection")
    assert "Automatic restore points" in page.inner_text("#mtProtection")
    cleanup = page.inner_text("#mtCleanup")
    assert "677 MB" in cleanup and "unavailable" in cleanup and "ubuntu.iso" in cleanup
    assert "never deletes your files" in cleanup
    reg = page.inner_text("#mtRegistry")
    assert "OldSync" in reg and "machine-wide (admin)" in reg and "registry cleaners" in reg
    assert page.query_selector("#mtRegistry .mt-fix")
    sec = page.inner_text("#mtSecurity")
    assert "0x0000009f" in sec and "Explorer.EXE ×2" in sec and "Public" in sec
    services = page.inner_text("#mtServices")
    assert "Adobe Acrobat Update Service" in services and "delayed" in services.lower()
    assert "0x80070002" in services and "OK" in services
    apps = page.inner_text("#mtApps")
    assert "2.57.0" in apps and "failed (rc 3)" in apps and "--version 1.0 --force" in apps
    page.close()


def test_fix_update_and_scan_buttons_post(browser, site):
    page = _details(browser, site, posts={
        "api/fix": {"mode": "apply", "results": [{"id": "clear_browser_cache", "status": "ok",
                                                  "output": "freed 677 MB"}]},
        "api/maintenance/scan": {"started": True},
        "api/maintenance/upgrade": {"started": True, "id": "Git.Git"}})
    page.wait_for_selector("#mtCleanup .mt-fix")
    page.click("#mtFindings .mt-fix[data-fix='clear_browser_cache']")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('freed 677 MB')")
    assert any("api/fix" in u for u in page.posted)
    page.click("#mtApps .mt-up")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('Updating Git')")
    page.click("#mtScan")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('Scan started')")
    assert any("api/maintenance/upgrade" in u for u in page.posted)
    page.close()


def test_failures_and_a_running_scan_are_reported(browser, site):
    page = _details(browser, site, posts={
        "api/fix": {"mode": "apply", "results": [{"status": "error", "output": "access denied"}]},
        "api/maintenance/scan": {"started": False}})
    page.wait_for_selector("#mtFindings .mt-fix")
    page.click("#mtFindings .mt-fix")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('Failed: access denied')")
    page.click("#mtScan")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('already running')")
    page.close()
    page = _details(browser, site)
    page.route("**/api/maintenance/**", lambda r, q: r.fulfill(
        status=400, content_type="application/json", body='{"error": "not listed"}'))
    page.wait_for_selector("#mtApps .mt-up")
    page.click("#mtApps .mt-up")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('Error: not listed')")
    page.click("#mtScan")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('Error: not listed')")
    page.close()


def test_first_scan_errors_and_empty_results(browser, site):
    first = {"at": None, "scanning": True, "findings": [], "fixes": M["fixes"]}
    page = _details(browser, site, first)
    page.wait_for_function("() => document.getElementById('mtStatus').innerText.includes('First scan running')")
    assert "Measuring" in page.inner_text("#mtRegistry")
    page.close()
    broken = dict(M, error="scan failed (rc 1): boom", scanning=True, findings=[],
                  registry={"error": "Access denied"}, security={"error": "x"}, cleanup={"error": "x"},
                  services=[], tasks=[], job=None, protection={"error": "x"}, crashes={"error": "x"},
                  app_updates={"available": False, "apps": []})
    page = _details(browser, site, broken)
    page.wait_for_function("() => document.getElementById('mtStatus').innerText.includes('boom')")
    assert "scanning now" in page.inner_text("#mtStatus")
    assert "Nothing needs attention" in page.inner_text("#mtFindings")
    assert "Unavailable on this PC: Access denied" in page.inner_text("#mtRegistry")
    assert "None." in page.inner_text("#mtServices") and "not installed" in page.inner_text("#mtApps")
    page.close()
    clean = dict(M, registry={"orphans": [], "checked": {}}, findings=[],
                 cleanup=dict(M["cleanup"], large_files=[]),
                 security={"defender": None, "antivirus": [], "firewall": []},
                 app_updates={"available": True, "apps": []}, job=None)
    page = _details(browser, site, clean)
    page.wait_for_function("() => document.getElementById('mtRegistry').innerText.includes('Every start-up')")
    assert "None found" in page.inner_text("#mtCleanup")
    assert "unavailable" in page.inner_text("#mtSecurity") and "up to date" in page.inner_text("#mtApps")
    page.close()
    nostatus = {"at": None, "scanning": False, "error": "no PowerShell", "findings": []}
    page = _details(browser, site, nostatus)
    page.wait_for_function("() => document.getElementById('mtStatus').innerText.includes('No scan yet. no PowerShell')")
    page.close()
    page = _details(browser, site)
    page.route("**/api/maintenance", lambda r, q: r.fulfill(status=200, content_type="text/plain", body="nope"))
    page.evaluate("document.getElementById('mtScan').click()")
    page.wait_for_function("() => document.getElementById('mtStatus').innerText.includes('unavailable')")
    page.close()


def test_restore_points_are_automatic_and_can_be_set_up_from_the_page(browser, site):
    """5.4: SysPerf creates restore points itself; the card shows the helper's state."""
    page = _details(browser, site, posts={"api/restorepoint": {"started": True}})
    page.wait_for_selector("#mtRpSetup")
    text = page.inner_text("#mtProtection")
    assert "waiting for set-up" in text.lower() and "asks by itself" in text
    assert "the weekly safety net" in text
    page.click("#mtRpSetup")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('choose Yes')")
    page.close()
    on = dict(STUBS["api/restorepoint"], installed=True, newest=1792583000 - 7200, age_s=7200,
              count=3, last=None)
    page = _open(browser, site, hash_="#details/maintenance",
                 overrides={"api/restorepoint": on}, posts={"api/restorepoint": {"started": True}})
    page.wait_for_selector("#mtRpCreate")
    assert "3 kept by Windows" in page.inner_text("#mtProtection")
    page.click("#mtRpCreate")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('Creating a restore point')")
    page.click("#mtRpAuto")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('are off')")
    page.close()
    off = dict(STUBS["api/restorepoint"], auto=False, last=None)
    page = _open(browser, site, hash_="#details/maintenance", overrides={"api/restorepoint": off},
                 posts={"api/restorepoint": {"auto": True}})
    page.wait_for_selector("#mtRpAuto")
    assert "Turned off" in page.inner_text("#mtProtection")
    page.click("#mtRpAuto")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('are on')")
    page.close()
    page = _open(browser, site, hash_="#details/maintenance")
    page.route("**/api/restorepoint", lambda r, q: r.fulfill(
        status=200 if q.method == "GET" else 400, content_type="application/json",
        body=json.dumps(STUBS["api/restorepoint"] if q.method == "GET" else {"error": "no"})))
    page.evaluate("document.getElementById('mtScan').click()")
    page.wait_for_selector("#mtRpSetup")
    page.click("#mtRpSetup")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('Error: no')")
    page.close()
    page = _open(browser, site, hash_="#details/maintenance", overrides={"api/restorepoint": None})
    page.route("**/api/restorepoint", lambda r, q: r.fulfill(status=200, content_type="text/plain", body="x"))
    page.evaluate("document.getElementById('mtScan').click()")
    page.wait_for_function("() => document.getElementById('mtProtection').innerText.includes('status is unavailable')")
    page.close()


def test_app_updates_say_they_are_automatic(browser, site):
    rep = dict(M, auto_update=True, managed=["Git.Git"], failed={"Git.Git": "2.57.0"})
    page = _details(browser, site, rep)
    page.wait_for_function("() => document.getElementById('mtApps').innerText.includes('Automatic:')")
    text = page.inner_text("#mtApps")
    assert "VIA COMPONENTS" in text.upper() and "FAILED LAST TIME" in text.upper()
    page.close()
    page = _details(browser, site, dict(M, auto_update=False))
    page.wait_for_function("() => document.getElementById('mtApps').innerText.includes('Automatic updates are off')")
    page.close()


def test_machine_wide_apps_wait_for_one_prompt_and_open_apps_wait_to_close(browser, site):
    rep = dict(M, auto_update=True, admin=["Git.Git"],
               job={"id": "OpenAI.ChatGPT", "name": "ChatGPT", "from": "1", "to": "2",
                    "state": "waiting", "rollback": "winget install --id OpenAI.ChatGPT"})
    page = _details(browser, site, rep, posts={
        "api/maintenance/upgrade": {"started": True, "id": "machine-wide", "ids": ["Git.Git"]}})
    page.wait_for_selector("#mtUpAdmin")
    text = page.inner_text("#mtApps")
    assert "Update 1 machine-wide app" in text and "NEEDS ADMINISTRATOR" in text.upper()
    assert "waiting for the app to close" in text and "once the app is closed" in text
    page.click("#mtUpAdmin")
    page.wait_for_function("() => document.getElementById('mtMsg').innerText.includes('administrator once')")
    assert any("api/maintenance/upgrade" in u for u in page.posted)
    page.close()
    batch = {"id": "machine-wide", "name": "2 machine-wide app(s)", "ids": ["A.A", "B.B"],
             "state": "failed", "returncode": 0, "results": {"A.A": 0, "B.B": 5},
             "rollback": "winget install --id <id> --exact --version <old> --force"}
    page = _details(browser, site, dict(M, admin=["A.A", "B.B"], job=batch))
    page.wait_for_selector("#mtUpAdmin")
    page.close()
    own = {"id": "Git.Git", "name": "Git", "state": "publisher", "returncode": 1, "rollback": "x"}
    page = _details(browser, site, dict(M, publisher_only=["Git.Git"], failed={"Git.Git": "2"}, job=own))
    page.wait_for_function("() => document.getElementById('mtApps').innerText.includes('its publisher updates it')")
    assert "USE ITS OWN UPDATER" in page.inner_text("#mtApps").upper()
    page.close()
    page = _details(browser, site, dict(M, admin=["A.A", "B.B"], job=batch))
    page.wait_for_function("() => document.getElementById('mtApps').innerText.includes('B.B ✗ rc 5')")
    assert "Update 2 machine-wide apps" in page.inner_text("#mtApps")
    page.close()
