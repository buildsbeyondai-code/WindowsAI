"""
Browser coverage for the 5.3 maintenance sections on the Details tab:
Maintenance check, Protection, Cleanup, Registry, Security, Services & tasks and
App updates, driven from the stubbed api/maintenance report.
"""
from __future__ import annotations

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
    assert "Checkpoint-Computer" in page.inner_text("#mtProtection")
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
