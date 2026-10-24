"""Full-coverage tests for cli.py.

Collaborators are injected, so no test starts a server, opens a browser or takes
a real sample. What is asserted is the contract the shipped installers depend on:
the options exist, they reach config through the environment, and the non-server
modes print and exit instead of serving.
"""
import io

import pytest

import sysperf.cli as cli
from sysperf.cli import ENV_FOR, _report, _where, build_parser, main


def _run(argv, env=None, **kw):
    """Run the CLI with stdout captured and no real server."""
    buf = io.StringIO()
    env = {} if env is None else env
    served = []
    rc = main(argv, env=env, stdout=buf,
              serve=kw.pop("serve", lambda: served.append(True)), **kw)
    return rc, buf.getvalue(), env, served


# ----------------------------------------------------------------- parser ----
def test_every_option_the_installers_pass_is_accepted():
    """install.ps1 and run.ps1 have passed these since 2.x; before 2.8.0 they
    were silently ignored, so -Port produced a dashboard on the default port."""
    args = build_parser().parse_args(
        ["--host", "0.0.0.0", "--port", "5100", "--data-dir", "/d",
         "--config", "/c.json", "--mode", "observe", "--open"])
    assert args.host == "0.0.0.0" and args.port == 5100
    assert args.data_dir == "/d" and args.config == "/c.json"
    assert args.mode == "observe" and args.open is True


def test_report_once_where_and_version_are_accepted():
    for flag in ("--report", "--once", "--where", "--version"):
        assert getattr(build_parser().parse_args([flag]), flag[2:]) is True


def test_no_arguments_is_valid():
    args = build_parser().parse_args([])
    assert args.port is None and args.open is False


def test_an_invalid_mode_is_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--mode", "sideways"])


def test_a_non_numeric_port_is_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--port", "http"])


# ------------------------------------------------------------ environment ----
def test_options_reach_config_through_the_environment():
    _, _, env, _ = _run(["--host", "0.0.0.0", "--port", "5100",
                         "--data-dir", "/d", "--config", "/c.json",
                         "--mode", "off"])
    assert env["PERFDASH_HOST"] == "0.0.0.0"
    assert env["PERFDASH_PORT"] == "5100"
    assert env["SYSPERF_DATA_DIR"] == "/d"
    assert env["SYSPERF_CONFIG"] == "/c.json"
    assert env["PERFDASH_AUTO_MODE"] == "off"


def test_an_option_left_unset_does_not_touch_the_environment():
    _, _, env, _ = _run([])
    assert not set(env) & set(ENV_FOR.values())


def test_the_env_mapping_covers_every_value_option():
    """Declared as data so the precedence is visible; this keeps it honest."""
    args = vars(build_parser().parse_args([]))
    value_options = {k for k, v in args.items()
                     if v is None}          # flags default to False, values None
    assert value_options == set(ENV_FOR)


# ----------------------------------------------------------------- modes -----
def test_version_prints_and_exits_before_importing_config():
    rc, out, env, served = _run(["--version"])
    assert rc == 0 and out.strip()
    assert env == {} and served == []


def test_where_prints_the_resolved_platform_and_paths():
    rc, out, _, served = _run(["--where"])
    assert rc == 0 and served == []
    for label in ("platform", "powershell", "can collect", "code", "data",
                  "settings file", "bind", "fixes enabled", "auto-improve"):
        assert label in out, label


def test_once_prints_the_latest_sample_and_does_not_serve(monkeypatch):
    import sysperf.app as app_module
    monkeypatch.setattr(app_module.collector, "snapshot",
                        lambda: {"latest": {"cpu_pct": 12.5}})
    rc, out, _, served = _run(["--once"])
    assert rc == 0 and served == []
    assert "cpu_pct" in out and "12.5" in out


def test_report_prints_a_summary_and_does_not_serve(monkeypatch):
    import sysperf.app as app_module
    monkeypatch.setattr(app_module.collector, "snapshot", lambda: {
        "scorecard": {"overall_improvement": 72.5, "counts": {"met": 3},
                      "rows": [{"key": "handles", "label": "Handles",
                                "status": "regress", "scored_typical": 120000.0,
                                "target": 95000, "progress": 10}]}})
    rc, out, _, served = _run(["--report"])
    assert rc == 0 and served == []
    assert "72.5%" in out and "Handles" in out and "met=3" in out


def test_the_default_mode_serves_and_announces_the_url():
    rc, out, _, served = _run([])
    assert rc == 0 and served == [True]
    assert "http://" in out and "data in" in out


def test_open_triggers_the_opener_with_the_bound_url():
    opened = []
    rc, out, _, served = _run(["--open"], opener=opened.append)
    assert rc == 0 and served == [True]
    assert opened and opened[0].startswith("http://")


# ---------------------------------------------------------------- helpers ----
def test_report_says_so_when_there_are_no_samples_yet():
    assert "no samples yet" in _report({})


def test_report_tolerates_a_row_without_a_current_value():
    text = _report({"scorecard": {"rows": [{"key": "x", "scored_typical": None,
                                            "target": 1}]}})
    assert "—" in text


def test_report_orders_the_worst_row_first():
    text = _report({"scorecard": {"rows": [
        {"key": "good", "label": "Good", "progress": 90, "scored_typical": 1.0,
         "target": 1, "status": "met"},
        {"key": "bad", "label": "Bad", "progress": 5, "scored_typical": 9.0,
         "target": 1, "status": "regress"}]}})
    assert text.index("Bad") < text.index("Good")


def test_report_ignores_malformed_rows():
    assert "no samples yet" in _report({"scorecard": {"rows": ["nope", 3]}})


def test_where_reports_a_platform_that_cannot_collect():
    class Config:
        PLATFORM = "linux"
        BASE = "/code"
        DATA_DIR = "/data"
        HOST, PORT = "127.0.0.1", 5056
        FIX_ENABLED = False
        AUTO_MODE, AUTO_INTERVAL_S, AUTO_THRESHOLD = "off", 300.0, 80.0

        class CONFIG_FILE:
            @staticmethod
            def exists():
                return False

            def __str__(self):
                return "/data/sysperf.config.json"

    text = _where(Config, lambda p: {"platform": "linux", "powershell": None,
                                     "can_collect": False,
                                     "why_not": "nothing to measure here"})
    assert "(none found)" in text
    assert "can collect     no" in text
    assert "nothing to measure here" in text
    assert "not present — using defaults" in text
    assert "fixes enabled   no" in text


def test_version_falls_back_to_dev_when_the_package_is_not_installed(monkeypatch):
    """Forced rather than inferred from the layout: in the development tree the
    import fails naturally, but in an exported build it succeeds, so relying on
    the ambient state left this branch uncovered in the release."""
    import builtins
    import sys as _sys
    real_import = builtins.__import__

    def blocked(name, *a, **kw):
        if name == "sysperf":
            raise ImportError("not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.delitem(_sys.modules, "sysperf", raising=False)
    assert cli._version() == "dev"


def test_version_reads_the_package_version_when_installed(monkeypatch):
    class FakePackage:
        __version__ = "9.9.9"

    monkeypatch.setitem(__import__("sys").modules, "sysperf", FakePackage)
    assert cli._version() == "9.9.9"


def test_main_defaults_to_the_real_argv_and_environment(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["sysperf", "--version"])
    assert main(serve=lambda: None) == 0


def test_open_browser_setting_opens_the_summary(monkeypatch):
    from sysperf import config as config_module
    monkeypatch.setitem(config_module.SETTINGS, "open_browser", True)
    opened = []
    rc = cli.main([], env={}, stdout=io.StringIO(), serve=lambda: None,
                  opener=opened.append)
    assert rc == 0 and opened and opened[0].endswith("/#summary")
