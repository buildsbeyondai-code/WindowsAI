"""Full-coverage tests for selftest.py (the integrated test-suite view/runner).

The runner is injected in every test, so nothing here shells out to a real
30-second gate run. What is asserted is the contract the dashboard depends on:
a truthful status before any run, correct gate merging, staleness, and that a
second concurrent run is refused rather than queued.
"""
import json
import sys

import pytest

from sysperf.selftest import GATES, RUN_TIMEOUT_S, STALE_AFTER_S, SelfTest


def _report(gates=None, **over):
    report = {
        "finished_at": 1_000_000.0,
        "duration_s": 30.2,
        "verdict": "ALL GATES PASSED",
        "exit_code": 0,
        "gates": gates if gates is not None else [
            {"name": name, "description": desc, "status": "pass",
             "skip_reason": "", "detail": "", "findings": []}
            for name, desc in GATES.items()
        ],
        "unit": {"detail": "329 passed in 6.2s", "passed": 329,
                 "duration_s": 6.2},
        "coverage": {"detail": "TOTAL 2700/2700 100.00%", "line_pct": 100.0},
    }
    report.update(over)
    return report


@pytest.fixture
def suite(tmp_path):
    """A SelfTest whose runner records the argv it was handed."""
    calls = []
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "testrunner.py").write_text("x", encoding="utf-8")
    st = SelfTest(tmp_path, tmp_path / "report.json",
                  runner=lambda argv: calls.append(argv) or 0)
    st.calls = calls
    return st


def _write(st, report):
    st.report_file.write_text(json.dumps(report), encoding="utf-8")


def _pytest_cache(root, nodeids, lastfailed):
    cache = root / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "nodeids").write_text(json.dumps(nodeids), encoding="utf-8")
    (cache / "lastfailed").write_text(json.dumps(lastfailed), encoding="utf-8")


# ----------------------------------------------------------------- status ---
def test_status_without_a_report_is_honest_not_green(suite):
    status = suite.status()
    assert status["available"] is False
    assert status["state"] == "unknown"
    assert status["gate_counts"] == {"pass": 0, "fail": 0, "skip": 0,
                                     "unknown": len(GATES), "total": len(GATES)}
    assert status["age_s"] is None
    assert status["verdict"] == ""
    assert status["pytest_cache"]["available"] is False


def test_status_reads_a_fresh_passing_report(suite, monkeypatch):
    _write(suite, _report())
    monkeypatch.setattr("sysperf.selftest.time.time", lambda: 1_000_010.0)
    status = suite.status()
    assert status["available"] is True
    assert status["state"] == "ok"
    assert status["age_s"] == 10.0
    assert status["gate_counts"]["pass"] == len(GATES)
    assert status["coverage"]["line_pct"] == 100.0
    assert status["unit"]["passed"] == 329
    assert status["duration_s"] == 30.2
    assert status["report_file"] == str(suite.report_file)


def test_status_is_failing_when_any_gate_failed(suite, monkeypatch):
    gates = [{"name": "frontend", "status": "fail", "findings": ["bad id"]}]
    _write(suite, _report(gates=gates, verdict="1 GATE(S) FAILED", exit_code=1))
    monkeypatch.setattr("sysperf.selftest.time.time", lambda: 1_000_010.0)
    status = suite.status()
    assert status["state"] == "failing"
    assert status["gate_counts"]["fail"] == 1
    # the gates the run never reached are still listed, as unknown
    assert status["gate_counts"]["unknown"] == len(GATES) - 1
    frontend = next(g for g in status["gates"] if g["name"] == "frontend")
    assert frontend["description"] == GATES["frontend"]


def test_status_is_stale_once_the_report_ages_out(suite, monkeypatch):
    _write(suite, _report())
    monkeypatch.setattr("sysperf.selftest.time.time",
                        lambda: 1_000_000.0 + STALE_AFTER_S + 1)
    assert suite.status()["state"] == "stale"


def test_gates_are_ordered_as_the_runner_executes_them(suite):
    _write(suite, _report(gates=[{"name": "live", "status": "skip"},
                                 {"name": "compile", "status": "pass"}]))
    assert [g["name"] for g in suite.status()["gates"]] == list(GATES)


def test_unknown_gate_names_sort_last_and_survive(suite):
    _write(suite, _report(gates=[{"name": "mystery", "status": "pass"},
                                 "not-a-dict"]))
    names = [g["name"] for g in suite.status()["gates"]]
    assert names[-1] == "mystery"
    assert set(GATES).issubset(set(names))


def test_status_tolerates_a_corrupt_or_unreadable_report(suite):
    suite.report_file.write_text("{not json", encoding="utf-8")
    assert suite.status()["state"] == "unknown"
    suite.report_file.write_text('"a string, not an object"', encoding="utf-8")
    assert suite.status()["available"] is False


def test_status_tolerates_a_report_without_a_timestamp(suite):
    _write(suite, _report(finished_at="not-a-number"))
    status = suite.status()
    assert status["age_s"] is None and status["state"] == "ok"


# ---------------------------------------------------------- pytest cache ---
def test_pytest_cache_counts_are_read_independently(suite, tmp_path):
    _pytest_cache(tmp_path, ["t1", "t2", "t3", "t4"], {"t4": True})
    cache = suite.status()["pytest_cache"]
    assert cache == {"available": True, "collected": 4, "failed": 1,
                     "passed": 3, "pass_rate": 75.0, "failing": ["t4"]}


def test_pytest_cache_tolerates_malformed_cache_files(suite, tmp_path):
    cache = tmp_path / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    (cache / "nodeids").write_text("{not json", encoding="utf-8")
    (cache / "lastfailed").write_text('"string"', encoding="utf-8")
    assert suite.status()["pytest_cache"]["collected"] == 0


def test_pytest_cache_caps_the_failing_list(suite, tmp_path):
    _pytest_cache(tmp_path, [f"t{i}" for i in range(40)],
                  {f"t{i}": True for i in range(40)})
    assert len(suite.status()["pytest_cache"]["failing"]) == 20


# ------------------------------------------------------------------- run ---
def test_the_gate_runner_is_found_in_the_repo_layout(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "testrunner.py").write_text("x", encoding="utf-8")
    suite = SelfTest(tmp_path, tmp_path / "r.json", runner=lambda argv: 0)
    assert suite.runner_script().name == "testrunner.py"
    assert suite.status()["runnable"] is True


def test_the_gate_runner_is_found_in_an_exported_layout(tmp_path):
    """An export puts the package at <dist>/src/sysperf and the suite at
    <dist>/tests, so the runner is two levels up from the package root."""
    pkg = tmp_path / "src" / "sysperf"
    pkg.mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "testrunner.py").write_text("x", encoding="utf-8")
    suite = SelfTest(pkg, pkg / "r.json", runner=lambda argv: 0)
    assert suite.runner_script() == tmp_path / "tests" / "testrunner.py"


def test_an_installation_without_the_runner_refuses_honestly(tmp_path):
    suite = SelfTest(tmp_path, tmp_path / "r.json", runner=lambda argv: 0)
    assert suite.runner_script() is None
    assert suite.status()["runnable"] is False
    result = suite.run()
    assert result["started"] is False
    assert "does not ship the gate runner" in result["reason"]
    assert suite.running is False


def test_run_blocking_invokes_the_runner_with_no_live_gate(suite):
    result = suite.run(background=False)
    assert result == {"started": True, "running": False, "mode": "blocking"}
    argv = suite.calls[0]
    assert argv[0] == sys.executable
    assert argv[1].replace("\\", "/").endswith("tests/testrunner.py")
    assert "--json" in argv and "--no-live" in argv
    assert suite.running is False


def test_run_with_live_omits_the_no_live_flag(suite):
    suite.run(live=True, background=False)
    assert "--no-live" not in suite.calls[0]
    assert "--quick" not in suite.calls[0]


def test_quick_run_takes_precedence_over_live(suite):
    suite.run(live=True, quick=True, background=False)
    argv = suite.calls[0]
    assert "--quick" in argv and "--no-live" not in argv


def test_run_in_background_returns_immediately_and_finishes(suite):
    result = suite.run()
    assert result["started"] is True and result["mode"] == "background"
    assert result["gates"] == len(GATES)
    for _ in range(200):                      # the injected runner is instant
        if not suite.running:
            break
        import time as _t
        _t.sleep(0.01)
    assert suite.running is False
    assert suite.calls


def test_a_second_run_is_refused_not_queued(suite):
    suite.run(background=False)               # resets running to False
    with suite._lock:                         # simulate a run in flight
        suite._running = True
    result = suite.run()
    assert result == {"started": False, "running": True,
                      "reason": "a run is already in flight"}


def test_a_failing_runner_is_reported_not_swallowed(tmp_path):
    def boom(argv):
        raise RuntimeError("runner exploded")

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "testrunner.py").write_text("x", encoding="utf-8")
    suite = SelfTest(tmp_path, tmp_path / "r.json", runner=boom)
    suite.run(background=False)
    assert suite.running is False
    status = suite.status()
    assert status["last_error"] == "RuntimeError: runner exploded"
    assert status["running"] is False


def test_started_at_is_recorded(suite, monkeypatch):
    monkeypatch.setattr("sysperf.selftest.time.time", lambda: 4242.0)
    suite.run(background=False)
    assert suite.status()["started_at"] == 4242.0


def test_default_timeout_is_generous_relative_to_a_real_run(tmp_path):
    assert SelfTest(tmp_path, tmp_path / "r.json").timeout_s == RUN_TIMEOUT_S
    assert RUN_TIMEOUT_S > 300


def test_every_documented_gate_has_a_description():
    assert set(GATES) == {"discover", "compile", "lint", "unit", "coverage",
                          "frontend", "browser", "live"}
    assert all(desc for desc in GATES.values())


def test_store_app_runs_the_suite_from_the_source_checkout(tmp_path):
    """The Store package ships no tests and its sys.executable is SysPerf.exe."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "testrunner.py").write_text("")
    seen = []
    suite = SelfTest(pkg, tmp_path / "r.json", runner=seen.append, python="py.exe",
                     fallback_root=lambda: str(repo))
    assert suite.root == repo and suite.runner_script() == repo / "tests" / "testrunner.py"
    suite.run(background=False)
    assert seen[0][0] == "py.exe"
    assert SelfTest(pkg, tmp_path / "r.json", fallback_root=lambda: None).root == pkg
    # A root that ships its own suite never falls back.
    assert SelfTest(repo, tmp_path / "r.json", fallback_root=lambda: str(pkg)).root == repo
