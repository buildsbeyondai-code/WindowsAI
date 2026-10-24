"""Export / release checks.

These validate the *distribution* rather than runtime behaviour:
  * version consistency across __init__, pyproject, CHANGELOG and the user manual,
  * completeness (every core module and the static UI are shipped),
  * shipped features for this release (WSL reclaim control, corrected
    net_remote target).

run-tests.sh runs these alongside the unit/integration suite so a release can be
gated on them.
"""
import os
import re
from pathlib import Path

import sysperf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "sysperf")

CORE_MODULES = (
    "app", "appctl", "config", "scoring", "collector", "fixes", "autoimprove",
    "adaptive", "rca", "trends", "cpuwatch", "onedrive", "session_policy",
    "browser", "components", "hardware", "netopt",
    # 2.7.0: continuous learning, self-authoring evolution, action queue,
    # integrated test suite, Explorer watchdog, OneDrive master switch.
    "learning", "evolve", "actions", "selftest", "procwatch", "onedrive_control",
    # 2.8.0: platform portability and a real command line.
    "platform_paths", "cli",
    # 3.1.0: automatic target-realism adjudication.
    "target_review",
    # 3.2.0: daily component-catalog refresh.
    "refresh_components",
    # 4.0.0: runtime LLM, learning store, action policy, source self-evolution.
    "llm", "evodb", "optactions", "codevolve",
)


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------- version ----
def test_version_is_semver():
    assert re.match(r"^\d+\.\d+\.\d+$", sysperf.__version__)


def test_version_matches_pyproject():
    m = re.search(r'^version\s*=\s*"([^"]+)"', _read("pyproject.toml"), re.M)
    assert m and m.group(1) == sysperf.__version__


def test_version_matches_changelog_top():
    m = re.search(r"^##\s+(\d+\.\d+\.\d+)", _read("CHANGELOG.md"), re.M)
    assert m and m.group(1) == sysperf.__version__


def test_version_matches_user_manual():
    assert sysperf.__version__ in _read("docs", "USER_MANUAL.md")


# --------------------------------------------------------- completeness ---
def test_all_core_modules_shipped():
    for mod in CORE_MODULES:
        assert os.path.exists(os.path.join(SRC, mod + ".py")), f"missing {mod}.py"


def test_static_ui_shipped():
    assert os.path.exists(os.path.join(SRC, "static", "index.html"))


def test_no_module_flat_imports_a_sibling():
    """The export rewrites flat development imports to package-relative ones.
    A missed rewrite works in the repo and fails once installed, so it is gated
    here rather than discovered by a user."""
    names = [f[:-3] for f in os.listdir(SRC) if f.endswith(".py")]
    pattern = re.compile(r"^(?:import|from) (%s)\b" % "|".join(names), re.M)
    for name in names:
        with open(os.path.join(SRC, name + ".py"), encoding="utf-8") as fh:
            hit = pattern.search(fh.read())
        assert not hit, f"{name}.py flat-imports {hit.group(1) if hit else ''}"


def test_the_gate_runner_and_coverage_tracers_are_shipped():
    """The dashboard's Integrated test suite card runs these, so an export
    without them can display gate results but never refresh them."""
    for name in ("testrunner.py", "covrun.py", "uicov.py", "e2e_stubs.json",
                 "capture_e2e_stubs.py"):
        assert os.path.exists(os.path.join(ROOT, "tests", name)), name


def test_the_browser_gate_and_ui_coverage_floor_are_shipped():
    """Static frontend checks cannot tell whether a panel ever renders; the
    browser gate can, and the UI-coverage floor is what stops it rotting."""
    runner = _read("tests", "testrunner.py")
    assert "def gate_browser()" in runner
    assert "uicov" in runner
    import sysperf.selftest as selftest
    assert "browser" in selftest.GATES
    uicov_src = _read("tests", "uicov.py")
    assert "THRESHOLD = 100.0" in uicov_src
    # every browser suite is shipped
    suites = [n for n in os.listdir(os.path.join(ROOT, "tests"))
              if n.startswith("test_e2e")]
    assert len(suites) >= 2, suites


# ---------------------------------------------------- shipped features ----
def test_feature_wsl_shutdown_api_present():
    import sysperf.appctl as appctl
    assert hasattr(appctl, "wsl_shutdown")


def test_feature_wsl_card_in_ui():
    """Asserts the feature, not one heading's wording — the 3.0.0 App Control
    redesign renamed the card and this check failed for no real reason."""
    html = _read("src", "sysperf", "static", "index.html")
    assert "wslShutdownBtn" in html          # the control exists
    assert "wslInfo" in html                 # ...and reports the current footprint
    assert "vmmemWSL" in html                # ...and names what it reclaims
    assert "session-killing" in html         # ...and says what it costs


def test_feature_net_remote_target_corrected():
    import sysperf.config as config
    row = next(c for c in config.CRITERIA if c[0] == "net_remote")
    # (key, label, baseline, target, higher_is_better, unit)
    assert row[2] == 55 and row[3] == 40


def test_feature_cpu_category_uses_smoothed_value():
    # the CPU & processes Summary card must score on the rolling cpu_pct
    # (scored_typical), not a single instantaneous sample.
    html = _read("src", "sysperf", "static", "index.html")
    assert "cpuRow" in html and "scored_typical" in html


def test_feature_onedrive_in_app_control():
    html = _read("src", "sysperf", "static", "index.html")
    assert "odACStop" in html and "odACStart" in html      # start/stop kept
    assert "Last completed sync" in html                    # sync age shown


def test_feature_onedrive_lock_detection_and_last_sync():
    ps = _read("src", "sysperf", "collect_win.ps1")
    assert "LockApp" in ps                                  # Win11 lock detection
    assert "LastDAUEventTimestamp_FindChanges" in ps        # completed-sync source


# ------------------------------------------------ 2.7.0 shipped features ---
def test_feature_continuous_learning_api():
    import sysperf.app as app
    routes = {r.rule for r in app.app.url_map.iter_rules()}
    assert {"/api/learning", "/api/actions", "/api/evolution",
            "/api/selftest"} <= routes


def test_feature_human_action_queue_has_reasons_and_steps():
    import sysperf.actions as actions
    assert len(actions.WHY) >= 8
    for code, (headline, detail) in actions.WHY.items():
        assert headline and len(detail) > 60, code
    for fix_id, recipe in actions.STEP_RECIPES.items():
        assert recipe["why"] in actions.WHY, fix_id
        assert len(recipe["steps"]) >= 3, fix_id


def test_feature_evolution_never_synthesises_a_command():
    """The core safety invariant of the self-authoring layer: every proposable
    action comes from the reviewed library, and none carries a command."""
    import sysperf.evolve as evolve
    assert evolve.RECIPE_LIBRARY
    for name, spec in evolve.RECIPE_LIBRARY.items():
        assert spec["risk"] in ("safe", "caution", "manual"), name
        assert "command" not in spec, name
        assert spec["manual_hint"], name


def test_feature_action_queue_and_evolution_cards_in_ui():
    html = _read("src", "sysperf", "static", "index.html")
    assert "actTable" in html and "actHideClosed" in html      # action queue
    assert "evoCandidates" in html and "sect-evolution" in html  # evolution
    assert "stGates" in html                                    # test suite
    assert "autoLearnings" in html                              # learnings


def test_feature_detail_sections_are_collapsible():
    html = _read("src", "sysperf", "static", "index.html")
    assert 'details class="sect"' in html
    assert "sectExpand" in html and "sectCollapse" in html
    # Learning analytics is the one detail section open by default.
    assert 'id="sect-charts" data-sect="charts" open' in html


def test_feature_removed_duplicate_cards_stay_removed():
    html = _read("src", "sysperf", "static", "index.html")
    for dead in ("autoLedger", "autoProposals", "autoEscalations"):
        assert dead not in html, dead


# ------------------------------------------------ 2.8.0 shipped features ---
def test_feature_no_wsl_only_assumptions_remain():
    """The 2.x line resolved PowerShell only through /mnt/c and translated paths
    with wslpath unconditionally, so a native Windows install started and then
    never collected a sample. Both are now platform-resolved."""
    import sysperf.platform_paths as pp
    assert pp.detect_platform(os_name="nt") == "windows"
    assert pp.powershell_candidates("windows", env={}, which=lambda n: None)
    assert all("/mnt/c" not in c
               for c in pp.powershell_candidates("windows", env={},
                                                 which=lambda n: None))
    # no module may hardcode a mount path any more
    for name in ("config", "collector", "components"):
        with open(os.path.join(SRC, name + ".py"), encoding="utf-8") as fh:
            body = fh.read()
        assert "/mnt/c/Users" not in body, f"{name}.py hardcodes a user path"


def test_feature_no_hardcoded_user_profile_is_shipped():
    """An Apache-2.0 release must not carry anybody's home directory.

    Matched by shape rather than by one known name, so it catches the next leak
    too, and scans every shipped text payload — an earlier version looked at .py
    files only, which is exactly how a hardcoded profile path survived in
    restart-wsl-and-verify.ps1.
    """
    # C:\Users\<name> or /mnt/c/Users/<name>, excluding the documented
    # placeholders that are meant to be read literally by a user.
    # PLACEHOLDERS are the names that are *meant* to appear: Windows' own
    # well-known accounts, and the stand-ins the fixtures and platform tests use
    # deliberately. Anything else is somebody's real profile and must not ship.
    PLACEHOLDERS = ("Public", "Default", "Default User", "All Users",
                    "testuser", "alice", "USERNAME", "user")
    profile = re.compile(
        r"(?:[A-Za-z]:\\\\|/mnt/[a-z]/)Users[\\\\/]"
        r"(?!(?:%s)\b|<|%%|\$|\{|\*)([A-Za-z0-9._-]+)"
        % "|".join(re.escape(n) for n in PLACEHOLDERS), re.I)
    suffixes = (".py", ".ps1", ".json", ".html", ".md", ".txt", ".sh", ".toml")
    checked, leaks = 0, []
    for directory in (SRC, os.path.join(SRC, "static"), ROOT,
                      os.path.join(ROOT, "tests")):
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if not os.path.isfile(path) or not name.endswith(suffixes):
                continue
            if os.path.basename(path) == os.path.basename(__file__):
                continue                      # this file carries the pattern
            with open(path, encoding="utf-8", errors="replace") as fh:
                for hit in profile.finditer(fh.read()):
                    leaks.append(f"{name}: {hit.group(0)}")
            checked += 1
    assert checked > 20, "the scan found suspiciously little to check"
    assert not leaks, "hardcoded user profile path(s) shipped: " + "; ".join(leaks)


def test_feature_the_documented_cli_exists():
    """install.ps1 and run.ps1 have passed these since 2.x."""
    import sysperf.cli as cli
    parser = cli.build_parser()
    args = parser.parse_args(["--port", "5100", "--open"])
    assert args.port == 5100 and args.open is True
    for flag in ("--where", "--once", "--report", "--version", "--host",
                 "--data-dir", "--config", "--mode"):
        parser.parse_args([flag, "off"] if flag == "--mode" else
                          [flag, "x"] if flag in ("--host", "--data-dir",
                                                  "--config") else [flag])


def test_feature_the_entry_point_is_the_cli():
    body = _read("pyproject.toml")
    assert 'sysperf = "sysperf.__main__:main"' in body
    main_py = _read("src", "sysperf", "__main__.py")
    assert "from .cli import main" in main_py


def test_feature_state_does_not_live_in_the_package():
    """Once pip-installed the package directory is site-packages."""
    import sysperf.config as config
    assert config.DATA_DIR != config.BASE or config.DATA_DIR.exists()
    assert "DATA_DIR" in _read("src", "sysperf", "config.py")
    body = _read("src", "sysperf", "config.py")
    for name in ("HISTORY_FILE", "ADAPTIVE_FILE", "TRENDS_FILE",
                 "AUTO_STATE_FILE", "EVOLVE_STATE_FILE", "ACTIONS_STATE_FILE"):
        assert f"{name} = DATA_DIR /" in body, name


def test_feature_the_settings_file_is_read():
    """install.ps1 has written sysperf.config.json since 2.x."""
    import sysperf.config as config
    assert hasattr(config, "SETTINGS") and hasattr(config, "CONFIG_FILE")
    assert config._load_settings(Path("/nonexistent.json")) == {}


def test_feature_waitress_is_actually_used():
    assert "waitress" in _read("pyproject.toml")
    assert "waitress" in _read("src", "sysperf", "app.py")


def test_feature_health_reports_the_platform():
    body = _read("src", "sysperf", "app.py")
    assert "platform_describe" in body and "data_dir" in body


# ------------------------------------------------ 3.1.0 shipped features ---
def test_feature_target_realism_adjudicator():
    import sysperf.target_review as tr
    assert hasattr(tr, "adjudicate") and hasattr(tr, "AutoTargetStore")
    kept = tr.adjudicate({"key": "x", "label": "X", "settled_days": 14,
                          "met_days": 3, "target": 100, "min": 50, "max": 90,
                          "higher_is_better": False})
    assert kept and kept["verdict"] == "realistic" and kept["honest_target"] == 100
    revised = tr.adjudicate({"key": "y", "label": "Y", "settled_days": 14,
                             "met_days": 0, "target": 40, "min": 55, "max": 80,
                             "higher_is_better": False})
    assert revised and revised["verdict"] == "unreachable" and revised["honest_target"] == 55


def test_feature_launch_metrics_are_scored():
    import sysperf.config as config
    keys = {c[0] for c in config.CRITERIA}
    assert {"boot_time_s", "wsl_start_s", "explorer_launch_s",
            "outlook_launch_s", "word_launch_s"} <= keys


def test_feature_launch_times_tab_in_ui():
    html = _read("src", "sysperf", "static", "index.html")
    assert 'id="sect-launch"' in html and "autoLaunch" in html
    assert "Launch times" in html


def test_feature_committed_target_honestly_revised():
    import sysperf.config as config
    row = next(c for c in config.CRITERIA if c[0] == "committed_app_mb")
    assert row[3] == 9600


def test_feature_sticky_subnav_with_strategy_first():
    html = _read("src", "sysperf", "static", "index.html")
    assert 'id="autoSub"' in html and "auto-subnav" in html
    assert 'href="#sect-charts" class="active"' in html


def test_feature_launch_collection_pipeline_shipped():
    broker = _read("src", "sysperf", "standby_broker.ps1")
    assert "Publish-LaunchTimes" in broker and "launch_times.json" in broker
    assert "launch_times.json" in _read("src", "sysperf", "collect_win.ps1")


def test_feature_evolution_auto_adopts_from_evidence():
    import sysperf.evolve as evolve
    assert "auto_adopt" in evolve.EvolutionEngine.__init__.__code__.co_varnames
    assert hasattr(evolve.EvolutionEngine, "auto_adjudicate")


def test_feature_hide_descriptions_strips_prose_and_simple_ask():
    html = _read("src", "sysperf", "static", "index.html")
    assert "body.compact #view-auto .hint" in html
    assert "simple_ask" in _read("src", "sysperf", "actions.py")


def test_feature_word_addins_measured_and_surfaced():
    assert "word_addins" in _read("src", "sysperf", "collect_win.ps1")
    html = _read("src", "sysperf", "static", "index.html")
    assert "word_addin_list" in html or "Word extensions" in html


def test_feature_launch_targets_are_realistic():
    import sysperf.config as config
    byk = {c[0]: c for c in config.CRITERIA}
    assert byk["boot_time_s"][3] >= 60       # relaxed from the aspirational 40s
    assert byk["outlook_launch_s"][3] >= 15  # relaxed from 5s


# ------------------------------------------------ 3.2.0 shipped features ---
def test_feature_selftest_root_is_configurable():
    import sysperf.config as config
    assert hasattr(config, "SELFTEST_ROOT")
    assert "PERFDASH_SELFTEST_ROOT" in _read("src", "sysperf", "config.py")
    assert "SelfTest(SELFTEST_ROOT" in _read("src", "sysperf", "app.py")


def test_feature_component_refresh_dir_is_configurable(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("SYSPERF_COMPONENTS_DIR", str(tmp_path))
    import sysperf.refresh_components as rc
    rc = importlib.reload(rc)
    try:
        assert rc.COMPONENTS_FILE == tmp_path / "components.json"
    finally:
        import logging
        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.FileHandler):
                logging.getLogger().removeHandler(h)
                h.close()


def test_feature_container_deployment_shipped():
    for rel in ("Dockerfile", "docker-compose.yml", "build.sh",
                "install-container.sh", "README.md",
                "broker/hostexec_broker.py", "broker/policy.json.in",
                "broker/sysperf-hostexec.service.in",
                "shims/hostexec_client.py",
                "systemd/sysperf-refresh-components.service.in",
                "systemd/sysperf-refresh-components.timer"):
        assert os.path.isfile(os.path.join(ROOT, "container", rel)), rel


def test_feature_container_is_hardened_and_loopback_only():
    compose = _read("container", "docker-compose.yml")
    assert '"127.0.0.1:' in compose and "read_only: true" in compose
    assert "- ALL" in compose and "no-new-privileges:true" in compose
    assert not re.search(r"^\s*privileged:\s*true", compose, re.M)
    df = _read("container", "Dockerfile")
    assert "PYTHON_DIGEST_PLACEHOLDER" in df and "USER ${SYSPERF_UID}" in df
    assert '"-m", "sysperf"' in df


def test_feature_broker_policy_and_stdin_forwarding():
    import json
    policy = json.loads(_read("container", "broker", "policy.json.in"))
    assert "powershell.exe" in policy["allow"]
    assert set(policy["path_map"]) >= {"/app/sysperf", "/data", "/dist"}
    assert policy["script_roots"]
    assert '"stdin"' in _read("container", "shims", "hostexec_client.py")
    assert "input=stdin_text" in _read("container", "broker", "hostexec_broker.py")
    assert "RuntimeDirectoryPreserve=yes" in _read(
        "container", "broker", "sysperf-hostexec.service.in")


def test_feature_container_files_carry_no_user_profile():
    leaks = []
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "container")):
        for name in files:
            with open(os.path.join(dirpath, name), encoding="utf-8",
                      errors="replace") as fh:
                if re.search(r"/home/(?!<)[A-Za-z0-9._-]+/", fh.read()):
                    leaks.append(name)
    assert not leaks, leaks


# ------------------------------------------------ 4.0.0 shipped features ---
def test_feature_ci_collector_path_is_real():
    ci = _read(".github", "workflows", "ci.yml")
    assert "src/sysperf/scripts/collect_win.ps1" not in ci
    assert "-File src/sysperf/collect_win.ps1" in ci
    assert os.path.isfile(os.path.join(SRC, "collect_win.ps1"))


def test_feature_no_model_id_is_hard_coded():
    body = _read("src", "sysperf", "llm.py")
    assert not re.search(r"claude-[a-z0-9-]*\d", body), "a model id is hard-coded"
    assert "/v1/models" in body


def test_feature_autonomy_defaults_to_observe(tmp_path):
    import sysperf.optactions as oa
    assert oa.ActionPolicy(tmp_path / "a.json").level == "observe"
    assert {s.id for s in oa.REGISTRY.values() if s.auto_eligible} == {
        "flush_dns", "trim_working_sets", "clear_standby"}


def test_feature_evolution_protects_its_own_guard_rails():
    import sysperf.codevolve as cv
    for rel in ("tests/test_x.py", "evo_bench.py", "codevolve.py", "llm.py",
                "optactions.py", "evodb.py", ".github/workflows/ci.yml"):
        assert cv.is_protected(rel), rel
    assert not cv.is_protected("rca.py") and not cv.is_protected("scoring.py")
    for k in ("min_gain", "engine_min_gain", "max_regression", "noise_k"):
        assert k not in cv.CodeEvolution.EDITABLE


def test_feature_self_evolution_tab_in_ui():
    html = _read("src", "sysperf", "static", "index.html")
    assert 'id="tab-evo"' in html and 'id="view-evo"' in html
    for target in ("sevStatus", "sevHistory", "sevDetail", "sevControls", "sevEffect",
                   "sevAudit"):
        assert f'id="{target}"' in html, target


def test_feature_evolution_demo_and_fixture_shipped():
    assert os.path.isfile(os.path.join(ROOT, "tools", "evolution_demo.py"))
    assert os.path.isfile(os.path.join(ROOT, "tools", "validate-windows.ps1"))
    for rel in ("dedupe.py", "evo_bench.py", "tests/test_dedupe.py"):
        assert os.path.isfile(os.path.join(ROOT, "tests", "fixtures", "evo_demo", rel)), rel
    assert os.path.isfile(os.path.join(ROOT, "docs", "AI_EVOLUTION.md"))
