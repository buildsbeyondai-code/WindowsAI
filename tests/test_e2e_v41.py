"""
Browser coverage for the 4.1 surfaces, driven through real interactions:

* Home (score ring, next actions, today, health, notifications, long-term chart,
  Simple view), the notification bell, Settings (generated form, save, restart,
  reset, import, test notification) and Diagnostics;
* tray auto-quit consent / busy / never / Reopen, and its existing controls;
* Self-Evolution Apply & restart, the Deployment card and Roll back, the evolution
  settings controls and the header status strip;
* the redesigned Live Architecture.

Pages are served exactly as in test_e2e_ui.py: static files plus stubbed api/*
answers captured from the real 4.1 application (tests/e2e_stubs.json).
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from tests import test_e2e_ui as ui  # noqa: E402 - after importorskip

STUBS, _open = ui.STUBS, ui._open
browser, site = ui.browser, ui.site          # the shared module-scoped fixtures

SAVED = dict(STUBS["api/settings"], restart_required=["general.sample_seconds"])


def _accept_dialogs(page):
    page.on("dialog", lambda d: d.accept())


def _posted(page, fragment):
    return [u for u in page.posted if fragment in u]


# ------------------------------------------------------------------- Home --
def test_home_is_the_default_view_and_renders_every_panel(browser, site):
    page = _open(browser, site)
    page.wait_for_function("() => document.getElementById('homeTodo').innerText.includes('quit')")
    assert page.eval_on_selector("#view-home", "e => e.style.display") != "none"
    page.wait_for_function("() => /\\d+%/.test(document.getElementById('homeScore').innerText)")
    status = page.inner_text("#homeStatus")
    assert "Improvement score 72%" in status and "agent acts below 80%" in status
    todo = page.inner_text("#homeTodo")
    assert "ChatGPT" in todo and "waiting for review" in todo and "Clear standby" in todo
    assert "Needs administrator rights" in todo and "ErrorActionPreference" not in todo
    page.click("#homeTodo details summary")
    assert "RAMMap.exe -Et" in page.inner_text("#homeTodo")
    assert "Flush DNS" in page.inner_text("#homeToday")
    assert "DEGRADED" in page.inner_text("#homeHealth")
    assert "AI patch ready for review" in page.inner_text("#homeNotes")
    page.wait_for_selector("#homeHistChart svg polyline")
    assert page.eval_on_selector("#homeHistKey", "s => s.options.length") == 3
    page.close()


def test_history_chart_follows_metric_and_range(browser, site):
    page = _open(browser, site)
    page.wait_for_selector("#homeHistChart svg")
    seen = []
    page.on("request", lambda r: seen.append(r.url) if "api/history" in r.url else None)
    page.select_option("#homeHistKey", "available_mb")
    page.select_option("#homeHistRange", "720")
    page.wait_for_timeout(500)
    assert any("key=available_mb" in u and "hours=720" in u for u in seen), seen
    page.close()


def test_history_chart_handles_too_little_data(browser, site):
    page = _open(browser, site, overrides={"api/history": {"points": [{"t": 1, "avg": 1,
                                                                       "min": 1, "max": 1}]}})
    page.wait_for_function("() => document.getElementById('homeHistChart').innerText"
                           ".includes('Not enough history')")
    page.close()


def test_simple_view_hides_expert_tabs_and_is_remembered(browser, site):
    page = _open(browser, site)
    page.check("#homeSimple")
    assert page.is_hidden("#tab-auto") and page.is_hidden("#tab-evo")
    assert page.is_visible("#tab-home") and page.is_visible("#tab-settings")
    page.reload()
    page.wait_for_selector("#homeStatus")
    assert page.is_checked("#homeSimple") and page.is_hidden("#tab-details")
    page.uncheck("#homeSimple")
    assert page.is_visible("#tab-details")
    page.close()


def test_home_with_nothing_to_do(browser, site):
    calm = dict(STUBS["api/home"], todo=[], consent=[], today=[], score=93,
                evolution={"pending": {"id": "c-1", "title": "Faster"}, "awaiting_review": 0},
                notifications={"items": [], "unread": 0}, health={"status": "ok", "problems": []})
    page = _open(browser, site, overrides={"api/home": calm})
    page.wait_for_function("() => document.getElementById('homeStatus').innerText.includes('Improvement score 93%')")
    assert "Verifying the applied AI patch" in page.inner_text("#homeTodo")
    assert "Nothing yet today" in page.inner_text("#homeToday")
    assert "No notifications yet" in page.inner_text("#homeNotes")
    page.close()


# ------------------------------------------------------------------- bell --
def test_bell_shows_unread_lists_items_and_marks_them_read(browser, site):
    page = _open(browser, site, posts={"api/notifications": STUBS["api/notifications"]})
    page.wait_for_function("() => !document.getElementById('notifCount').hidden")
    assert page.inner_text("#notifCount") == "2"
    page.click("#notifBtn")
    assert page.is_visible("#notifPanel")
    assert page.eval_on_selector_all("#notifList .notif-item.unread", "e => e.length") == 2
    page.click("#notifReadAll")
    page.wait_for_timeout(300)
    assert _posted(page, "api/notifications")
    page.click("#notifList .notif-item >> nth=0")
    page.wait_for_timeout(300)
    assert page.is_hidden("#notifPanel") and page.evaluate("location.hash") != ""
    page.click("#notifBtn")
    page.click("header h1")                               # click outside closes it
    assert page.is_hidden("#notifPanel")
    page.close()


# --------------------------------------------------------------- Settings --
def test_settings_form_saves_and_offers_a_restart(browser, site):
    page = _open(browser, site, hash_="#settings",
                 posts={"api/settings": SAVED, "api/restart": {"restarting": True}})
    page.wait_for_selector("#setForm fieldset")
    legends = page.eval_on_selector_all("#setForm legend", "ls => ls.map(l => l.innerText)")
    assert legends == ["General", "Autonomy", "AI self-evolution", "Notifications",
                       "Tray auto-quit"]
    page.fill("#set-general-sample_seconds", "120")
    page.fill("#set-trayquit-apps", "ChatGPT, Teams")
    page.click("#setSave")
    page.wait_for_function("() => document.getElementById('setMsg').innerText.includes('Saved')")
    assert page.is_visible("#setRestart") and "sample_seconds" in page.inner_text("#setRestartTxt")
    page.click("#setRestartBtn")
    page.wait_for_timeout(300)
    assert _posted(page, "api/restart")
    page.close()


def test_settings_reset_import_export_and_test_notification(browser, site, tmp_path):
    page = _open(browser, site, hash_="#settings",
                 posts={"api/settings": STUBS["api/settings"], "api/notifications": {"sent": {}}})
    _accept_dialogs(page)
    page.wait_for_selector("#setForm fieldset")
    assert page.get_attribute("#setExport", "href") == "api/settings/export"
    page.click("#setResetAll")
    page.wait_for_timeout(300)
    assert _posted(page, "api/settings/reset")
    good = tmp_path / "s.json"
    good.write_text(json.dumps({"sysperf_settings": 1, "values": {}}))
    page.set_input_files("#setImportFile", str(good))
    page.wait_for_timeout(400)
    assert _posted(page, "api/settings/import")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    page.set_input_files("#setImportFile", str(bad))
    page.wait_for_function("() => document.getElementById('setMsg').innerText.includes('not valid JSON')")
    page.click("#setTestNotify")
    page.wait_for_function("() => document.getElementById('setMsg').innerText.includes('Test notification')")
    page.close()


def test_settings_error_is_shown(browser, site):
    page = _open(browser, site, hash_="#settings")

    def fail(route, request):
        route.fulfill(status=400, content_type="application/json",
                      body=json.dumps({"error": "Sampling interval (seconds): must be between 30 and 600"}))
    page.route("**/api/settings", lambda r, q: fail(r, q) if q.method == "POST" else r.fallback())
    page.wait_for_selector("#setForm fieldset")
    page.click("#setSave")
    page.wait_for_function("() => document.getElementById('setMsg').innerText.includes('between 30 and 600')")
    page.close()


def test_diagnostics_panels_and_clear(browser, site):
    page = _open(browser, site, hash_="#settings", posts={"api/diagnostics/clear": {"cleared": True}})
    page.wait_for_function("() => document.getElementById('diagErrors').innerText.includes('sample failed')")
    assert "DEGRADED" in page.inner_text("#diagStatus")
    assert "running" in page.inner_text("#diagWorkers")
    foot = page.inner_text("#diagFoot")
    assert "warm session" in foot and "sqlite" in foot
    sup = page.inner_text("#diagSup")
    assert "yes" in sup and "no sample for more than 900 s" in sup and "confirmed" in sup
    page.click("#diagClear")
    page.wait_for_timeout(300)
    assert _posted(page, "api/diagnostics/clear")
    page.close()


# ------------------------------------------------------------- tray-quit --
def test_tray_consent_busy_never_and_reopen(browser, site):
    tq = STUBS["api/trayquit"]
    page = _open(browser, site, hash_="#app-control", posts={"api/trayquit": tq})
    page.wait_for_function("() => document.getElementById('tqApps').innerText.includes('needs your OK')")
    apps = page.inner_text("#tqApps")
    assert "busy" in apps and "never quit" in apps
    assert "Freed (24 h)" in page.inner_text("#tqStatus")
    page.click('#tqApps [data-tq-consent="ChatGPT"][data-decision="allow"]')
    page.wait_for_function("() => document.getElementById('tqMsg').innerText.includes('Saved')")
    assert _posted(page, "api/trayquit")
    assert page.inner_text("#tqRecentPill") == "2"
    page.click("#tqCard details summary >> nth=0")
    assert "reopened" in page.inner_text("#tqRecent")
    page.click('#tqRecent [data-tq-reopen="WhatsApp"]')
    page.wait_for_timeout(300)
    assert "Notion" in page.inner_text("#tqSuggest")
    page.click('#tqSuggest [data-tq-add="Notion"]')
    page.click("#tqToggle")
    page.select_option("#tqGrace", "120")
    page.fill("#tqAdd", "Teams.exe")
    page.click("#tqAddBtn")
    page.fill("#tqAdd", "Zoom")
    page.press("#tqAdd", "Enter")
    page.wait_for_timeout(400)
    assert len(_posted(page, "api/trayquit")) >= 6
    page.close()


# ---------------------------------------------------------- self-evolution --
def _evo_with_deploy(pending):
    cv = dict(STUBS["api/codevolve"])
    hist = [{"at": 1792860000, "event": "confirmed", "id": "c-1"},
            {"at": 1792859000, "event": "applied", "id": "c-1", "title": "Faster scorecard", "by": "user"}]
    cv["deploy"] = {"pending": ({"id": "c-1", "title": "Faster scorecard", "applied_at": 1792859000}
                                if pending else None), "history": hist}
    return cv


def test_apply_and_restart_from_the_candidate_detail(browser, site):
    page = _open(browser, site, hash_="#self-evolution",
                 overrides={"api/codevolve": _evo_with_deploy(False)},
                 posts={"api/codevolve/apply": {"applied": True, "restarting": True},
                        "api/codevolve/review": {"review_state": "rejected"}})
    _accept_dialogs(page)
    page.wait_for_selector("#sevHistory .evo-pick")
    page.click("#sevHistory .evo-pick")
    page.wait_for_selector("#sevApply")
    page.click("#sevApply")
    page.wait_for_function("() => document.getElementById('sevReviewMsg').innerText.includes('restarting')")
    assert _posted(page, "api/codevolve/apply")
    page.click("#sevReject")
    page.wait_for_timeout(300)
    assert _posted(page, "api/codevolve/review")
    page.close()


def test_deployment_card_and_rollback(browser, site):
    page = _open(browser, site, hash_="#self-evolution",
                 overrides={"api/codevolve": _evo_with_deploy(True)},
                 posts={"api/codevolve/rollback": {"rolled_back": True, "restarting": False}})
    _accept_dialogs(page)
    page.wait_for_selector("#sevRollback")
    assert "Verifying" in page.inner_text("#sevDeploy") and "confirmed" in page.inner_text("#sevDeploy")
    page.click("#sevRollback")
    page.wait_for_function("() => document.getElementById('sevDeployMsg').innerText.includes('Rolled back')")
    page.close()


def test_apply_is_hidden_while_a_patch_is_verifying(browser, site):
    page = _open(browser, site, hash_="#self-evolution",
                 overrides={"api/codevolve": _evo_with_deploy(True)})
    page.wait_for_selector("#sevHistory .evo-pick")
    page.click("#sevHistory .evo-pick")
    page.wait_for_selector("#sevApprove")
    assert page.query_selector("#sevApply") is None
    page.close()


def test_evolution_settings_controls_post(browser, site):
    page = _open(browser, site, hash_="#self-evolution",
                 overrides={"api/llm/models": {"models": ["claude-opus-5-5", "claude-sonnet-5-5"]}})
    page.wait_for_selector("#sevSave")
    for sel in ("#sevEnabled", "#sevRepo", "#sevInterval", "#sevMax", "#sevProvider", "#sevBudget"):
        assert page.query_selector(sel), sel
    page.fill("#sevMax", "3")
    page.click("#sevSave")
    page.wait_for_timeout(300)
    assert _posted(page, "api/codevolve/settings")
    page.fill("#sevKey", "sk-ant-test")
    page.click("#sevSaveKey")
    page.wait_for_timeout(300)
    assert _posted(page, "api/llm/key")
    page.click("#sevListModels")
    page.wait_for_function("() => document.getElementById('sevMsg').innerText.includes('2 model')")
    assert page.inner_text("#sevControls") and page.inner_text("#sevEffect")
    assert page.inner_text("#sevAudit")
    page.close()


def test_status_strip_reports_and_opens_self_evolution(browser, site):
    page = _open(browser, site)
    page.wait_for_function("() => document.getElementById('asIso').innerText !== '—'")
    for sel in ("#asLevel", "#asEvo", "#asAI", "#asIso", "#asPending"):
        assert page.inner_text(sel).strip(), sel
    page.click("#asOpen")
    page.wait_for_timeout(300)
    assert page.eval_on_selector("#view-evo", "e => e.style.display") != "none"
    page.close()


# ------------------------------------------------------------ architecture --
def test_architecture_shows_the_three_live_layers(browser, site):
    page = _open(browser, site, hash_="#architecture")
    page.wait_for_function("() => (document.getElementById('archDiagram').textContent || '')"
                           ".toUpperCase().includes('HISTORY · SQLITE')")
    txt = page.text_content("#archDiagram").upper()      # card titles are upper-cased
    for label in ("SENSE & SERVE", "LEARN & REMEMBER", "ACT & EVOLVE", "Supervisor",
                  "Health monitor", "Notifications", "Code self-evolution", "Action policy",
                  "reversible remediation", "warm session"):
        assert label.upper() in txt, label
    assert "NGINX" not in txt
    page.close()


def test_details_trends_hint_names_the_cadence(browser, site):
    page = _open(browser, site, hash_="#details")
    page.wait_for_function("() => document.getElementById('trendsHint').innerText.includes('one point every')")
    page.close()


def test_header_version_baseline_strip_and_launch_times(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    page.wait_for_function("() => document.getElementById('hdrVersion').innerText === 'v4.1.0'")
    assert "baseline" in page.inner_text("#baseSub")
    assert "Autonomy" in page.inner_text("#autoStrip")
    page.wait_for_function("() => document.getElementById('pillLaunch').innerText !== '—'")
    assert page.inner_text("#pillLaunch") in ("no data yet",) or "tracked" in page.inner_text("#pillLaunch")
    assert "Windows boot" in page.inner_text("#autoLaunch")
    page.close()


def test_home_and_summary_always_show_the_same_system_score(browser, site):
    """Both headline rings come from computeCategories(snapshot), so they can't drift."""
    page = _open(browser, site)
    page.wait_for_function("() => /\\d+%/.test(document.getElementById('homeScore').innerText)")
    home = page.inner_text("#homeScore").strip()
    page.click("#tab-summary")
    page.wait_for_selector("#view-summary .sum-ring")
    summary = page.inner_text("#view-summary .sum-ring").split()[0].strip()
    assert home.rstrip("%") == summary.rstrip("%"), (home, summary)
    assert "Improvement score" in page.inner_text("#sumAgent")
    page.close()


# ------------------------------------------------------------ Store mode --
def _feature(fid, on, where=None):
    return {"id": fid, "title": fid.replace("_", " ").title(), "what": f"does {fid}",
            "undo": f"undo {fid}", "on": on, "where": where}


def test_store_app_lists_optional_features_and_toggles_them(browser, site):
    opt = [_feature("tray_quit", False), _feature("explorer_watchdog", True),
           _feature("self_evolution", False, "#self-evolution")]
    after = {"store_mode": True, "features": [_feature("tray_quit", True)] + opt[1:]}
    page = _open(browser, site, overrides={"api/home": dict(STUBS["api/home"], optional=opt)},
                 posts={"api/store": after})
    page.wait_for_selector("#homeOptionalCard", state="visible")
    text = page.inner_text("#homeOptional")
    assert "does tray_quit" in text and "undo tray_quit" in text and "Set up" in text
    page.click("#homeOptional .opt-toggle[data-id='tray_quit']")
    page.wait_for_function("() => document.querySelector(\"#homeOptional .opt-toggle[data-id='tray_quit']\").textContent === 'Turn off'")
    assert _posted(page, "api/store")
    page.close()


def test_optional_features_are_hidden_outside_the_store_app_and_errors_show(browser, site):
    page = _open(browser, site)
    page.wait_for_function("() => document.getElementById('homeTodo').innerText.includes('quit')")
    assert not page.is_visible("#homeOptionalCard")
    page.close()
    page = _open(browser, site, overrides={"api/home": dict(
        STUBS["api/home"], optional=[_feature("tray_quit", False)])})
    page.route("**/api/store", lambda r, q: r.fulfill(status=400, content_type="application/json",
                                                      body=json.dumps({"error": "nope"})))
    page.wait_for_selector("#homeOptional .opt-toggle")
    page.click("#homeOptional .opt-toggle")
    page.wait_for_function("() => document.querySelector('#homeOptional .opt-toggle').textContent.includes('Failed: nope')")
    page.close()


def test_component_upgrades_card_shows_every_component_and_its_state(browser, site):
    page = _open(browser, site, hash_="#self-evolution")
    page.wait_for_function("() => document.getElementById('sevDeps').innerText.includes('Flask')")
    text = page.inner_text("#sevDeps")
    assert text.startswith("On.") and "upgraded" in text
    assert "not upgraded — jinja2 3.2.0 requires Python >=3.11" in text
    assert "applying — SysPerf restarts to finish" in text                   # python
    assert "queued for the next evolution cycle" in text                     # ubuntu
    assert "rolled back — git reports" in text and "handed to its updater" in text
    assert "upgraded with wsl" in text and "up to date" in text and "not installed" in text
    assert "throwaway copy" in text
    assert page.query_selector("#sevDeps a[href='#details/components']")
    off = dict(STUBS["api/codevolve"], components={"enabled": False, "rows": [
        {"key": "waitress", "name": "waitress", "label": "pinned", "current": "3.0.2",
         "latest": "3.0.3", "outdated": True}]})
    page = _open(browser, site, hash_="#self-evolution", overrides={"api/codevolve": off})
    page.wait_for_function("() => document.getElementById('sevDeps').innerText.includes('Off.')")
    assert "auto-upgrade is off" in page.inner_text("#sevDeps")
    page = _open(browser, site, hash_="#self-evolution", overrides={
        "api/codevolve": dict(STUBS["api/codevolve"], components={})})
    page.wait_for_function("() => document.getElementById('sevDeps').innerText.includes('not available yet')")
    page.close()


def test_upgrades_work_without_ai_and_upgrade_now_starts_one(browser, site):
    """5.2: on a PC with no AI provider, WSL sandbox or source checkout, the
    Windows-side upgrades still run and the card says which rows cannot."""
    rows = [{"key": "pwsh", "name": "PowerShell", "label": "PowerShell MSIX", "via": "pwsh",
             "standalone": True, "current": "7.6.9", "latest": "7.7.0", "outdated": True},
            {"key": "flask", "name": "Flask", "label": "pinned", "via": "pip-lock",
             "standalone": False, "current": "3.1.3", "latest": "3.2.0", "outdated": True}]
    plain = dict(STUBS["api/codevolve"], components={"enabled": True, "rows": rows},
                 upgrades={"gate": None, "manual": None})
    page = _open(browser, site, hash_="#self-evolution", overrides={"api/codevolve": plain},
                 posts={"api/codevolve/upgrade": {"started": True}})
    page.wait_for_selector("#sevUpgrade")
    text = page.inner_text("#sevDeps")
    assert "need no AI model" in text and "queued for the next upgrade check" in text
    assert "queued for the next evolution cycle (needs a source checkout and the sandbox)" in text
    page.click("#sevUpgrade")
    page.wait_for_function("() => document.getElementById('sevUpgradeMsg').innerText.includes('Upgrade started')")
    assert page.posted and any("api/codevolve/upgrade" in u for u in page.posted)
    page.close()
    waiting = dict(plain, upgrades={"gate": "autonomy level is 'observe'", "manual": "autonomy level is 'off'"})
    page = _open(browser, site, hash_="#self-evolution", overrides={"api/codevolve": waiting},
                 posts={"api/codevolve/upgrade": {"started": False, "reason": "x"}})
    page.wait_for_function("() => document.getElementById('sevUpgradeMsg').innerText.includes('unavailable')")
    assert "waiting — autonomy level is 'observe'" in page.inner_text("#sevDeps")
    assert not page.query_selector("#sevUpgrade")
    page.close()
    page = _open(browser, site, hash_="#self-evolution", overrides={"api/codevolve": plain},
                 posts={"api/codevolve/upgrade": {"started": False, "reason": "a cycle is already running"}})
    page.wait_for_selector("#sevUpgrade")
    page.click("#sevUpgrade")
    page.wait_for_function("() => document.getElementById('sevUpgradeMsg').innerText.includes('Not started: a cycle')")
    page.close()


def test_times_are_shown_in_the_viewers_zone(browser, site):
    page = browser.new_page(timezone_id="Asia/Kolkata", locale="en-IN")
    page.goto(site)
    # epoch seconds and a UTC ISO string are the same instant, shown in IST
    show = "(v, o) => localTime(v, o).replace(/[\\u202f\\u00a0]/g, ' ')"
    epoch = page.evaluate(show, 1793042963)
    assert epoch == page.evaluate(show, "2026-10-26T19:29:23+00:00")
    assert epoch == "27/10/2026, 12:59:23 am IST"
    assert page.evaluate(f"({show})(1793042963, {{timeOnly: true}})") == "12:59 am IST"
    assert page.evaluate("[localTime(null), localTime(''), localTime('not a date')]") == [
        "—", "—", "not a date"]
    page.close()
