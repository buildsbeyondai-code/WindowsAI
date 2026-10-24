"""
Browser coverage for the whole dashboard.

``test_e2e_playwright.py`` covers the two behaviours it was written for. This
module covers the *interface*: every tab, every collapsible section, every element
the page renders into, and every control that has a handler. ``tests/uicov.py``
measures that surface and fails the build when something is left unexercised, so
this file and that gate are two halves of one check.

Design
------
The page is served from ``static/`` by a throwaway ``http.server`` thread and
every ``api/*`` request is answered from :data:`STUBS`, so the tests are hermetic:
no Flask, no collector, no PowerShell, and no dependence on what the live machine
happens to be doing. The stub payloads mirror the real response shapes, which is
what makes an assertion here meaningful rather than decorative.

Skipped automatically when Playwright or its Chromium build is absent, so a run on
a machine without browsers stays green.
"""
from __future__ import annotations

import http.server
import json
import socketserver
import threading
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
from playwright.sync_api import Error as PWError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
STATIC = _ROOT / "src" / "sysperf" / "static"
if not STATIC.is_dir():
    STATIC = _ROOT / "static"


# --------------------------------------------------------------- static server --
class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site() -> str:
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


# ------------------------------------------------------------- stub payloads ----
# Captured from a live instance and trimmed, rather than hand-written. A guessed
# payload is worse than no test: the first version of this file invented shapes
# the page does not use, so assertions passed against markup no data could ever
# produce. Refresh with: python3 tests/capture_e2e_stubs.py
STUBS = json.loads((Path(__file__).resolve().parent / "e2e_stubs.json")
                   .read_text(encoding="utf-8"))
SELFTEST = STUBS["api/selftest"]
EVOLUTION = STUBS["api/evolution"]


def _open(browser, site, *, hash_="", overrides=None, posts=None):
    """A page with every api/* call answered from the stubs."""
    page = browser.new_page(viewport={"width": 1600, "height": 1100})
    stubs = dict(STUBS)
    stubs.update(overrides or {})
    page.posted = []

    def handler(route, request):
        if request.method == "POST":
            page.posted.append(request.url)
            body = (posts or {}).get("default", {"ok": True, "status": "ok",
                                                 "action": "stop"})
            for fragment, payload in (posts or {}).items():
                if fragment != "default" and fragment in request.url:
                    body = payload
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body))
            return
        body = {}
        for fragment, payload in stubs.items():
            if fragment in request.url:
                body = payload
                break
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body))

    page.route("**/api/**", handler)
    page.goto(site + hash_, wait_until="domcontentloaded")
    # Wait for the first render rather than sleeping a fixed 1.2s. Across 47 tests
    # the fixed wait cost about a minute of the browser gate on its own, and it was
    # simultaneously too long on a fast machine and too short on a loaded one.
    try:
        page.wait_for_function(
            "() => { const f = document.getElementById('footMeta');"
            " return f && f.innerHTML.trim().length > 0; }", timeout=8000)
    except PWError:                       # pragma: no cover - slow/odd environment
        page.wait_for_timeout(1200)
    return page


def _wait_filled(page, element_id, timeout=8000):
    """Wait for one element to have rendered.

    Preferred over a fixed sleep: a test should wait for the thing it asserts on,
    which is both faster and immune to a slow machine.
    """
    page.wait_for_function(
        "(id) => { const e = document.getElementById(id);"
        " return e && e.innerHTML.trim().length > 0; }",
        arg=element_id, timeout=timeout)


def _filled(page, ids):
    """Ids that rendered something. Returned rather than asserted so a failure
    names every empty element at once instead of stopping at the first."""
    return page.evaluate(
        """(ids) => ids.filter(id => {
             const e = document.getElementById(id);
             return e && (e.innerHTML || '').trim().length > 0;
           })""", list(ids))


def _expand_all(page):
    page.click("#tab-auto")
    page.wait_for_timeout(500)
    page.click("#sectExpand")
    page.wait_for_timeout(700)


# ------------------------------------------------------------------- tabs -------
TABS = {
    "tab-summary": "view-summary", "tab-details": "view-details",
    "tab-arch": "view-architecture", "tab-auto": "view-auto",
    "tab-appctl": "view-appctl", "tab-evo": "view-evo",
}


def test_every_tab_opens_its_view(browser, site):
    page = _open(browser, site)
    for tab, view in TABS.items():
        page.click(f"#{tab}")
        page.wait_for_timeout(450)
        assert page.eval_on_selector(f"#{view}", "e => e.style.display") != "none", tab
        assert page.eval_on_selector(f"#{tab}", "e => e.getAttribute('aria-selected')") \
            == "true", tab
    page.close()


def test_the_hash_selects_the_tab_on_load(browser, site):
    for hash_, view in (("#summary", "view-summary"), ("#details", "view-details"),
                        ("#architecture", "view-architecture"),
                        ("#auto-improve", "view-auto"),
                        ("#app-control", "view-appctl")):
        page = _open(browser, site, hash_=hash_)
        assert page.eval_on_selector(f"#{view}", "e => e.style.display") != "none", hash_
        page.close()


# ---------------------------------------------------------------- summary -------
def test_the_summary_view_renders_its_cards(browser, site):
    page = _open(browser, site)
    _wait_filled(page, "kpis")
    # The countdown is written by a once-per-second interval, so it is empty for
    # the first tick after render. The old blanket 1.2s sleep hid that.
    _wait_filled(page, "refreshIn", timeout=4000)
    empty = set(["kpis", "charts", "selfimprove", "alerts", "liveTxt",
                 "refreshIn", "footMeta"]) - set(_filled(page, [
                     "kpis", "charts", "selfimprove", "liveTxt", "refreshIn",
                     "footMeta"]))
    # `alerts` is empty when nothing is wrong, which is the stubbed state.
    assert empty == {"alerts"}, f"unrendered: {empty}"
    page.close()


def test_the_details_view_renders_every_section(browser, site):
    page = _open(browser, site, hash_="#details")
    page.wait_for_timeout(800)
    ids = ["drivers", "netopt", "wsl", "cpuTopList"]
    assert set(_filled(page, ids)) == set(ids), \
        f"unrendered: {set(ids) - set(_filled(page, ids))}"
    page.close()


def test_the_architecture_view_draws_its_diagram(browser, site):
    page = _open(browser, site, hash_="#architecture")
    page.wait_for_timeout(900)
    assert _filled(page, ["archDiagram"]) == ["archDiagram"]
    page.close()


# ------------------------------------------------------- auto-improve: render ---
AUTO_ALWAYS = ["autoHead", "autoKpis", "sectCount", "autoRca",
               "actHead", "actTable", "actReasons", "actWhyPill"]
AUTO_SECTIONED = ["autoDonut", "autoActivity", "autoDays", "autoTypes",
                  "pillCharts", "autoHve", "autoStrategies", "autoKnowledge",
                  "autoRules", "autoCompliance", "autoRulePill",
                  "pillStrategies", "evoHead", "evoKpis", "evoNext",
                  "evoCandidates", "evoShards", "evoMappings", "evoClusters",
                  "evoProposals", "evoLibrary", "evoLibPill", "evoLog",
                  "evoLogPill", "pillEvolution", "stRing", "stSummary",
                  "stGates", "stAge", "pillTests", "autoActions",
                  "autoLearnings", "pillLearnings", "autoCpu", "pillCpu",
                  "autoTrends", "pillTrends", "autoLog"]


def test_the_auto_improve_tab_renders_everything_above_the_sections(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    page.wait_for_timeout(900)
    missing = set(AUTO_ALWAYS) - set(_filled(page, AUTO_ALWAYS))
    assert not missing, f"unrendered: {sorted(missing)}"
    page.close()


def test_expanding_the_sections_renders_all_of_their_contents(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    _expand_all(page)
    missing = set(AUTO_SECTIONED) - set(_filled(page, AUTO_SECTIONED))
    assert not missing, f"unrendered: {sorted(missing)}"
    page.close()


SECTIONS = ["charts", "hve", "strategies", "evolution", "tests", "lastcycle",
            "learnings", "cpu", "trends", "log"]


def test_every_detail_section_toggles_and_reports_itself(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    page.wait_for_timeout(700)
    # Learning analytics is the one open by default; the rest start closed.
    state = page.evaluate(
        """(names) => names.map(n => {
             const d = document.querySelector(`details.sect[data-sect="${n}"]`);
             return {n, present: !!d, open: d && d.open};
           })""", SECTIONS)
    assert all(s["present"] for s in state), state
    assert [s["n"] for s in state if s["open"]] == ["charts"]

    for name in SECTIONS:
        sel = f'details.sect[data-sect="{name}"] > summary'
        before = page.eval_on_selector(
            f'details.sect[data-sect="{name}"]', "e => e.open")
        page.click(sel)
        page.wait_for_timeout(200)
        after = page.eval_on_selector(
            f'details.sect[data-sect="{name}"]', "e => e.open")
        assert after is not before, name
    assert "detail sections open" in page.eval_on_selector(
        "#sectCount", "e => e.innerText")
    page.close()


def test_expand_all_and_collapse_all(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    page.wait_for_timeout(600)
    page.click("#sectExpand")
    page.wait_for_timeout(400)
    assert page.eval_on_selector_all("#view-auto details.sect",
                                    "ds => ds.every(d => d.open)")
    page.click("#sectCollapse")
    page.wait_for_timeout(400)
    assert page.eval_on_selector_all("#view-auto details.sect",
                                    "ds => ds.every(d => !d.open)")
    page.close()


def test_a_section_choice_survives_a_reload(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    page.wait_for_timeout(600)
    page.click('details.sect[data-sect="learnings"] > summary')
    page.wait_for_timeout(300)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(900)
    assert page.eval_on_selector('details.sect[data-sect="learnings"]',
                                "e => e.open") is True
    page.close()


# ----------------------------------------------------- auto-improve: controls ---
def test_the_mode_select_posts_the_new_mode(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    page.wait_for_timeout(700)
    page.select_option("#autoMode", "observe")
    page.wait_for_timeout(600)
    assert any("autoimprove/mode" in u for u in page.posted)
    page.close()


def test_scan_now_posts_a_cycle(browser, site):
    page = _open(browser, site, hash_="#auto-improve",
                 posts={"autoimprove/run": {"note": "cycle complete"}})
    page.wait_for_timeout(700)
    page.click("#autoScan")
    page.wait_for_timeout(700)
    assert any("autoimprove/run" in u for u in page.posted)
    assert "cycle complete" in page.eval_on_selector("#autoMsg", "e => e.innerText")
    page.close()


def test_scan_and_fix_asks_before_remediating(browser, site):
    page = _open(browser, site, hash_="#auto-improve",
                 posts={"autoimprove/run": {"note": "remediated"}})
    page.wait_for_timeout(700)
    page.on("dialog", lambda d: d.accept())
    page.click("#autoFix")
    page.wait_for_timeout(700)
    assert any("autoimprove/run" in u for u in page.posted)
    page.close()


def test_declining_the_fix_confirmation_posts_nothing(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    page.wait_for_timeout(700)
    page.on("dialog", lambda d: d.dismiss())
    page.click("#autoFix")
    page.wait_for_timeout(500)
    assert not any("autoimprove/run" in u for u in page.posted)
    page.close()


# ------------------------------------------------------- action queue controls --
def test_marking_an_action_taken_posts_the_status(browser, site):
    page = _open(browser, site, hash_="#auto-improve",
                 posts={"actions/status": {"ok": True, "status": "closed"}})
    page.wait_for_timeout(800)
    page.on("dialog", lambda d: d.accept("ran it"))
    page.click("#actTable .act-mark")
    page.wait_for_timeout(700)
    assert any("actions/status" in u for u in page.posted)
    assert "action taken" in page.eval_on_selector("#actMsg", "e => e.innerText").lower()
    page.close()


def test_hiding_closed_items_filters_the_table(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    page.wait_for_timeout(800)
    hidden = page.eval_on_selector_all("#actTable tbody tr", "rs => rs.length")
    page.uncheck("#actHideClosed")
    page.wait_for_timeout(700)
    shown = page.eval_on_selector_all("#actTable tbody tr", "rs => rs.length")
    assert shown > hidden, "unchecking 'Hide closed' must reveal the closed item"
    assert "action taken" in page.eval_on_selector("#actTable", "e => e.innerText")
    page.close()


def test_an_action_item_exposes_its_steps_and_reason(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    page.wait_for_timeout(800)
    page.click("#actTable details.collapse > summary")
    page.wait_for_timeout(300)
    # innerText reflects CSS text-transform, so these come back upper-cased.
    text = page.eval_on_selector("#actTable", "e => e.innerText").lower()
    # Structural labels the renderer always emits, rather than the wording of
    # whichever item the captured fixture happens to have open.
    for expected in ("how to confirm it worked", "what should change",
                     "evidence"):
        assert expected in text, expected
    steps = page.eval_on_selector_all("#actTable details.collapse ol li",
                                     "ls => ls.length")
    assert steps >= 3, f"an action item should carry real steps, got {steps}"
    page.close()


# ---------------------------------------------------------- evolution controls --
def test_running_an_evolution_pass_posts_and_reports(browser, site):
    page = _open(browser, site, hash_="#auto-improve",
                 posts={"evolution/run": {"ran": True, "generation": 3,
                                          "evolutions": [{"kind": "recipe",
                                                          "needs_promotion": True}],
                                          "status": EVOLUTION}})
    _expand_all(page)
    page.click("#evoRun")
    page.wait_for_timeout(700)
    assert any("evolution/run" in u for u in page.posted)
    assert "evolution" in page.eval_on_selector("#evoMsg", "e => e.innerText").lower()
    page.close()


def test_promoting_a_candidate_requires_confirmation_and_posts(browser, site):
    page = _open(browser, site, hash_="#auto-improve",
                 posts={"evolution/promote": {"promoted": True,
                                              "status": EVOLUTION}})
    _expand_all(page)
    page.on("dialog", lambda d: d.accept())
    page.click("#evoCandidates .evo-act[data-act=promote]")
    page.wait_for_timeout(700)
    assert any("evolution/promote" in u for u in page.posted)
    page.close()


def test_declining_a_promotion_posts_nothing(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    _expand_all(page)
    page.on("dialog", lambda d: d.dismiss())
    page.click("#evoCandidates .evo-act[data-act=promote]")
    page.wait_for_timeout(500)
    assert not any("evolution/promote" in u for u in page.posted)
    page.close()


def test_rejecting_a_candidate_needs_no_confirmation(browser, site):
    page = _open(browser, site, hash_="#auto-improve",
                 posts={"evolution/promote": {"rejected": True,
                                              "status": EVOLUTION}})
    _expand_all(page)
    page.click("#evoCandidates .evo-act[data-act=reject]")
    page.wait_for_timeout(700)
    assert any("evolution/promote" in u for u in page.posted)
    page.close()


def test_rolling_back_asks_first(browser, site):
    page = _open(browser, site, hash_="#auto-improve",
                 posts={"evolution/rollback": {"rolled_back": True,
                                               "reverted": {"kind": "recipe"},
                                               "status": EVOLUTION}})
    _expand_all(page)
    page.on("dialog", lambda d: d.accept())
    page.click("#evoRollback")
    page.wait_for_timeout(700)
    assert any("evolution/rollback" in u for u in page.posted)
    assert "Reverted" in page.eval_on_selector("#evoMsg", "e => e.innerText")
    page.close()


def test_a_candidate_that_cannot_run_says_so(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    _expand_all(page)
    text = page.eval_on_selector("#evoCandidates", "e => e.innerText")
    assert "cannot run" in text
    # an incomplete candidate's Promote button is disabled
    assert page.eval_on_selector_all(
        "#evoCandidates .evo-act[data-act=promote]",
        "bs => bs.some(b => b.disabled)")
    page.close()


# ------------------------------------------------------- test-suite controls ----
def test_the_test_suite_card_shows_the_gates(browser, site):
    page = _open(browser, site, hash_="#auto-improve")
    _expand_all(page)
    assert page.eval_on_selector_all("#stGates .gate", "gs => gs.length") == 7
    assert "ALL GATES PASSED" in page.eval_on_selector("#stSummary",
                                                      "e => e.innerText")
    assert "last run" in page.eval_on_selector("#stAge", "e => e.innerText")
    page.close()


def test_running_the_suite_posts_and_honours_the_live_checkbox(browser, site):
    page = _open(browser, site, hash_="#auto-improve",
                 posts={"selftest/run": {"started": True, "running": True,
                                         "status": SELFTEST}})
    _expand_all(page)
    page.check("#stLive")
    page.click("#stRun")
    page.wait_for_timeout(800)
    assert any("selftest/run" in u for u in page.posted)
    assert page.eval_on_selector("#stMsg", "e => e.innerText").strip()
    page.close()


# ------------------------------------------------------------ app control -------
def test_the_app_control_view_renders_its_cards(browser, site):
    page = _open(browser, site, hash_="#app-control")
    page.wait_for_timeout(900)
    ids = ["acStrip", "appctlList", "wslInfo", "odACSync",
           "pwStatus", "pwThresh", "pwSustain", "pwLast", "pwRecent"]
    missing = set(ids) - set(_filled(page, ids))
    assert not missing, f"unrendered: {sorted(missing)}"
    page.close()


def test_the_status_strip_summarises_all_four_subsystems(browser, site):
    """The tab used to open on a wide table, so "is anything wrong?" took reading
    to answer. The four facts come from four different endpoints, so this also
    proves the slice-store is wired to each refresher."""
    page = _open(browser, site, hash_="#app-control")
    page.wait_for_timeout(1200)
    # A raw string: a plain Python "\n" would put a real newline inside the JS
    # regex literal, which fails with "Invalid regular expression: missing /".
    tiles = page.eval_on_selector_all(
        ".ac-tile", r"ts => ts.map(t => t.innerText.replace(/\s+/g, ' '))")
    assert len(tiles) == 4, tiles
    joined = " ".join(tiles).lower()
    for expected in ("total cpu", "wsl memory", "onedrive", "explorer watchdog"):
        assert expected in joined, expected
    # every tile carries a state class, so the colour is never left neutral by accident
    classes = page.eval_on_selector_all(".ac-tile", "ts => ts.map(t => t.className)")
    assert all(any(k in c for k in ("ok", "warn", "bad", "idle")) for c in classes)
    page.close()


def test_each_application_row_offers_exactly_one_action(browser, site):
    """Two buttons per row with one always disabled was ten controls for five
    apps, and the disabled halves carried no information."""
    page = _open(browser, site, hash_="#app-control")
    page.wait_for_timeout(1200)
    rows = page.eval_on_selector_all(".ac-app", "rs => rs.length")
    assert rows >= 1
    per_row = page.eval_on_selector_all(
        ".ac-app .ac-acts",
        "as => as.map(a => a.querySelectorAll('button').length)")
    assert all(n <= 1 for n in per_row), per_row
    # and no control is rendered disabled — an unavailable action is omitted
    assert page.eval_on_selector_all(
        ".ac-app .ac-acts button", "bs => bs.every(b => !b.disabled)")
    page.close()


def test_the_process_reference_table_starts_collapsed_with_a_count(browser, site):
    """12 rows of every process including system ones is reference data, not a
    control, so it must not push the actual controls below the fold."""
    page = _open(browser, site, hash_="#app-control")
    page.wait_for_timeout(1200)
    assert page.eval_on_selector("#sect-cputop", "e => e.open") is False
    badge = page.eval_on_selector("#pillCpuTop", "e => e.innerText")
    assert "process" in badge.lower(), badge
    page.click("#sect-cputop > summary")
    page.wait_for_timeout(350)
    assert page.eval_on_selector("#sect-cputop", "e => e.open") is True
    assert page.eval_on_selector_all("#cpuTopList tbody tr", "rs => rs.length") >= 1
    page.close()


def test_the_onedrive_banner_appears_only_when_control_is_off(browser, site):
    """The banner is the page's way of saying "I am deliberately hands-off", so it
    must be absent when control is on and present when it is not."""
    page = _open(browser, site, hash_="#app-control")
    page.wait_for_timeout(900)
    assert _filled(page, ["odACControlBanner"]) == []
    page.close()

    off = dict(STUBS["api/onedrive"])
    off["control_disabled"] = True
    page = _open(browser, site, hash_="#app-control",
                 overrides={"api/onedrive": off})
    page.wait_for_timeout(900)
    assert _filled(page, ["odACControlBanner"]) == ["odACControlBanner"]
    page.close()


def test_stopping_an_app_posts_the_action(browser, site):
    page = _open(browser, site, hash_="#app-control",
                 posts={"appctl": {"action": "stop", "status": "ok"}})
    page.wait_for_timeout(900)
    page.on("dialog", lambda d: d.accept())
    page.click("#appctlList button")
    page.wait_for_timeout(700)
    assert any("appctl" in u for u in page.posted)
    assert page.eval_on_selector("#appctlMsg", "e => e.innerText").strip()
    page.close()


def test_the_wsl_shutdown_button_confirms_first(browser, site):
    page = _open(browser, site, hash_="#app-control",
                 posts={"appctl": {"action": "wsl-shutdown", "status": "ok"}})
    page.wait_for_timeout(900)
    page.on("dialog", lambda d: d.accept())
    page.click("#wslShutdownBtn")
    page.wait_for_timeout(700)
    assert any("appctl" in u for u in page.posted)
    assert page.eval_on_selector("#wslMsg", "e => e.innerText").strip()
    page.close()


def test_the_onedrive_controls_post(browser, site):
    page = _open(browser, site, hash_="#app-control",
                 posts={"onedrive": {"action": "start", "status": "ok"}})
    page.wait_for_timeout(900)
    page.on("dialog", lambda d: d.accept())
    page.click("#odACStart")
    page.wait_for_timeout(600)
    assert any("onedrive" in u for u in page.posted)
    page.click("#odACControl")
    page.wait_for_timeout(600)
    page.close()


def test_the_explorer_watchdog_controls_post(browser, site):
    page = _open(browser, site, hash_="#app-control",
                 posts={"procwatch": {"enabled": False, "status": "idle",
                                      "recent": []}})
    page.wait_for_timeout(900)
    page.on("dialog", lambda d: d.accept())
    page.click("#pwToggle")
    page.wait_for_timeout(600)
    page.click("#pwRun")
    page.wait_for_timeout(600)
    assert any("procwatch" in u for u in page.posted)
    assert page.eval_on_selector("#pwMsg", "e => e.innerText").strip()
    page.close()


# --------------------------------------------------------- header + modal -------
def test_the_header_toggles_change_the_body_class(browser, site):
    page = _open(browser, site)
    page.check("#hideDesc")
    page.wait_for_timeout(250)
    assert "compact" in page.eval_on_selector("body", "e => e.className")
    page.uncheck("#hideDesc")
    page.wait_for_timeout(250)
    assert "compact" not in page.eval_on_selector("body", "e => e.className")
    page.check("#darkMode")
    page.wait_for_timeout(250)
    dark = page.eval_on_selector("body", "e => e.className")
    page.uncheck("#darkMode")
    page.wait_for_timeout(250)
    assert dark != page.eval_on_selector("body", "e => e.className")
    page.close()


def test_the_common_fix_modal_opens_previews_applies_and_closes(browser, site):
    page = _open(browser, site, overrides={
        "api/fixes": {"enabled": True, "token": "t", "fixes": [
            {"id": "flush_dns", "title": "Flush DNS", "risk": "safe",
             "category": "network", "reversible": True, "reason": "stale cache",
             "executable": True, "session_killing": False, "persistent": False,
             "manual_hint": ""}]}},
        posts={"api/fix": {"mode": "preview", "results": [
            {"id": "flush_dns", "status": "ok", "output": "dry run"}]}})
    page.click("#commonFixBtn")
    page.wait_for_timeout(700)
    assert page.eval_on_selector("#fixModal", "e => !e.hidden")
    assert _filled(page, ["fixList"]) == ["fixList"]
    page.click("#fixPreview")
    page.wait_for_timeout(600)
    assert _filled(page, ["fixResult"]) == ["fixResult"]
    page.on("dialog", lambda d: d.accept())
    page.click("#fixApply")
    page.wait_for_timeout(600)
    assert any("api/fix" in u for u in page.posted)
    page.click("#fixClose")
    page.wait_for_timeout(300)
    assert page.eval_on_selector("#fixModal", "e => e.hidden")
    page.close()


# ------------------------------------------------- runtime exhaustiveness -------
# The coverage gate matches ids named in this file, which a lazy test could game by
# mentioning one without asserting anything. This test cannot be gamed: it reads
# the render targets out of the page at runtime and requires every one of them to
# have produced content, after visiting every tab and expanding every section.
#
# A *render target* that is legitimately empty in the stubbed state is listed here
# with its reason. Each entry is a hole in the check, so it has to be argued for —
# and the test fails if an entry stops being a render target, so the list cannot
# quietly rot into a dumping ground. Controls belong in the behavioural tests, not
# here.
CONDITIONAL = {
    "alerts": "empty when nothing is wrong, which is the stubbed state",
    "autoMsg": "a status line, written only after an action",
    "evoMsg": "a status line, written only after an action",
    "sevMsg": "a status line, written only after a self-evolution control is used",
    "sevReviewMsg": "written only after Approve / Reject on a pending candidate",
    "sevModels": "model suggestions, filled only after 'List models' asks the provider",
    "stMsg": "a status line, written only after a run is started",
    "actMsg": "a status line, written only after marking an item",
    "appctlMsg": "a status line, written only after an app action",
    "pwMsg": "a status line, written only after a watchdog action",
    "wslMsg": "a status line, written only after a shutdown request",
    "fixResult": "written only after a preview or apply",
    "odACControlBanner": "shown only when OneDrive control is switched off",
    "fixList": "populated only once the Common Fix modal is opened",
    "fixModal": "the modal body is static markup, not a render target",
    "commonFixBtn": "a button label, not a render target",
    "darkMode": "a checkbox, not a render target",
    "hideDesc": "a checkbox, not a render target",
    "pwRun": "a button label",
    "pwToggle": "a button label",
    "odACStart": "a button label",
    "odACControl": "a button label",
    "wslShutdownBtn": "a button label",
    "fixApply": "a button label",
    "fixPreview": "a button label",
    "fixClose": "a button label",
    "actTable": "covered by the action-queue tests",
    "appctlList": "covered by the app-control tests",
    "odACMsg": "a status line, written only after a OneDrive action",
    "brokerCard": "shown only while the standby-clear helper has reported state",
    "brokerCopy": "a button rendered inside brokerCard, so only when it shows",
    "autoDecisionsCard": "shown only once the loop has made an auto-goal decision",
}


def test_every_render_target_the_page_declares_is_actually_reached(browser, site):
    # Enumerated by tests/uicov.py from the page source — one source of truth for
    # what the surface *is*, so the gate and this test cannot disagree.
    import uicov
    targets = sorted(uicov.surface(
        (STATIC / "index.html").read_text(encoding="utf-8"))["render"])
    assert len(targets) > 80, f"the enumerator found only {len(targets)} targets"

    page = _open(browser, site)
    for tab in TABS:
        page.click(f"#{tab}")
        page.wait_for_timeout(420)
    page.click("#tab-auto")
    page.wait_for_timeout(300)
    page.click("#sectExpand")
    page.wait_for_timeout(900)
    page.click("#tab-appctl")
    page.wait_for_timeout(700)

    filled = set(_filled(page, targets))
    unexplained = [t for t in targets if t not in filled and t not in CONDITIONAL]
    assert not unexplained, (
        "these render targets produced nothing and have no documented reason: "
        + ", ".join(unexplained))
    # The exclusion list must not rot: every entry has to still be real.
    stale = [t for t in CONDITIONAL if t not in targets]
    assert not stale, f"CONDITIONAL lists ids the page no longer renders: {stale}"
    page.close()


# ------------------------------------------------------------- no JS errors -----
def test_no_console_errors_anywhere_in_the_dashboard(browser, site):
    """Every tab, every section expanded, in both themes."""
    errors = []
    page = _open(browser, site)
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    for tab in TABS:
        page.click(f"#{tab}")
        page.wait_for_timeout(500)
    page.click("#tab-auto")
    page.wait_for_timeout(300)
    page.click("#sectExpand")
    page.wait_for_timeout(700)
    page.check("#darkMode")
    page.wait_for_timeout(400)
    page.uncheck("#darkMode")
    page.wait_for_timeout(400)
    assert not errors, errors
    page.close()


def test_self_evolution_tab_shows_recorded_cycle_and_diff(browser, site):
    """4.0: the tab renders what the API recorded — state, history, diff, review."""
    page = _open(browser, site, hash_="#self-evolution",
                 posts={"api/codevolve/review": {"review_state": "approved",
                                                 "merge_command": "git merge --ff-only x"}})
    page.wait_for_timeout(800)
    assert "Awaiting approval" in page.inner_text("#sevStatus")
    assert "retained" in page.inner_text("#sevHistory")
    page.click("#sevHistory .sev-pick" if page.query_selector("#sevHistory .sev-pick")
               else "#sevHistory .evo-pick")
    page.wait_for_timeout(500)
    detail = page.inner_text("#sevDetail")
    assert "seen = set()" in detail and "Benchmark" in detail
    page.click("#sevApprove")
    page.wait_for_timeout(400)
    assert any("api/codevolve/review" in u for u in page.posted)
    assert "Approved" in page.inner_text("#sevReviewMsg")
    page.close()


def test_self_evolution_run_and_autonomy_controls_post(browser, site):
    page = _open(browser, site, hash_="#self-evolution",
                 posts={"api/codevolve/run": {"started": False, "reason": "daily limit"}})
    page.wait_for_timeout(800)
    page.click("#sevRun")
    page.wait_for_timeout(400)
    assert "daily limit" in page.inner_text("#sevMsg")
    page.select_option("#sevLevel", "observe")
    page.wait_for_timeout(400)
    assert sum("api/autonomy" in u for u in page.posted) >= 1
    page.close()
