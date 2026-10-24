#!/usr/bin/env python3
"""
Integrated test suite — the dashboard's own quality gates, surfaced live.

The repo already ships a seven-gate runner (``tests/testrunner.py``: discover,
compile, lint, unit, coverage, frontend, live). Until now its verdict lived only
in a terminal, so the page could claim to be self-improving while silently
regressing. This module makes the suite a first-class, observable part of the
dashboard:

    status()  the last run's gates, the pytest cache, and how stale it is
    run()     start a run in the background, at most one at a time

Design constraints this file respects:

* **Bounded and non-blocking.** ``run()`` returns immediately; the suite executes
  on a daemon thread and writes a single JSON report. A run already in flight is
  refused rather than queued, so the endpoint cannot be used to fork-bomb the box.
* **No new dependencies.** Report parsing uses the standard library only, matching
  the rest of the repo (this environment has no PyPI access).
* **Truthful when empty.** Before a first run the status reports
  ``state="unknown"`` instead of inventing a green result.
"""
from __future__ import annotations

import json
import subprocess
from .background_process import run_hidden
import sys
import threading
import time
from pathlib import Path

# The gates the runner executes, in order, with a one-line explanation each so
# the UI can describe a gate it has never seen pass.
GATES = {
    "discover": "every module is registered in the coverage gate",
    "compile": "modules byte-compile and carry a docstring",
    "lint": "AST checks: dead imports, bare except, mutable defaults",
    "unit": "the pytest suite",
    "coverage": "100% line coverage on every target module",
    "frontend": "page JS parses, element ids resolve, strings are escaped",
    "browser": "real Chromium drives the page; 100% of the UI is exercised",
    "live": "the running service: status, headers, CSRF, GET purity",
}

# A report older than this is stale: the suite is cheap (~30 s) and the code it
# checks changes often, so a day-old green is not evidence about now.
STALE_AFTER_S = 6 * 3600

# Hard ceiling on a run. The coverage gate line-traces the whole pytest suite,
# which is the slow part; 15 minutes is ~30x the observed duration.
RUN_TIMEOUT_S = 900


class SelfTest:
    """Reads and triggers the repo's own test suite.

    ``root`` is the repo root, ``report_file`` the JSON report the runner writes.
    ``runner`` is injectable so tests never shell out to a real 30-second run.
    """

    def __init__(self, root, report_file, runner=None,
                 timeout_s: float = RUN_TIMEOUT_S) -> None:
        self.root = Path(root)
        self.report_file = Path(report_file)
        self.timeout_s = float(timeout_s)
        self._runner = runner or self._default_runner
        self._lock = threading.Lock()
        self._running = False
        self._started_at = None
        self._last_error = None

    # --------------------------------------------------------------- reading
    def _report(self) -> dict:
        try:
            data = json.loads(self.report_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _pytest_cache(self) -> dict:
        """Collected/failed counts from pytest's own cache.

        This is independent evidence: it reflects the most recent *pytest* run
        even if that was a developer running pytest directly, not the gate runner.
        """
        cache = self.root / ".pytest_cache" / "v" / "cache"

        def read(name, default):
            try:
                return json.loads((cache / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return default

        nodeids = read("nodeids", [])
        lastfailed = read("lastfailed", {})
        total = len(nodeids) if isinstance(nodeids, list) else 0
        failed = len(lastfailed) if isinstance(lastfailed, dict) else 0
        passed = max(0, total - failed)
        return {
            "available": total > 0,
            "collected": total,
            "failed": failed,
            "passed": passed,
            "pass_rate": round(100.0 * passed / total, 1) if total else None,
            "failing": sorted(lastfailed)[:20] if isinstance(lastfailed, dict) else [],
        }

    def status(self) -> dict:
        """Everything the Test Suite card renders."""
        report = self._report()
        gates = [g for g in (report.get("gates") or []) if isinstance(g, dict)]
        for gate in gates:
            gate.setdefault("description", GATES.get(gate.get("name"), ""))
        known = {g.get("name") for g in gates}
        # A gate the last run never reached still belongs on the board, greyed.
        for name, description in GATES.items():
            if name not in known:
                gates.append({"name": name, "description": description,
                              "status": "unknown", "findings": [],
                              "detail": ""})
        order = list(GATES)
        gates.sort(key=lambda g: order.index(g["name"])
                   if g.get("name") in order else len(order))

        finished = report.get("finished_at")
        age_s = (round(time.time() - finished, 1)
                 if isinstance(finished, (int, float)) else None)
        passed = sum(1 for g in gates if g.get("status") == "pass")
        failed = sum(1 for g in gates if g.get("status") == "fail")
        skipped = sum(1 for g in gates if g.get("status") == "skip")
        unknown = sum(1 for g in gates if g.get("status") == "unknown")

        if not report:
            state = "unknown"
        elif failed:
            state = "failing"
        elif age_s is not None and age_s > STALE_AFTER_S:
            state = "stale"
        else:
            state = "ok"

        return {
            "available": bool(report),
            "state": state,
            "running": self.running,
            "started_at": self._started_at,
            "last_error": self._last_error,
            "gates": gates,
            "gate_counts": {"pass": passed, "fail": failed,
                            "skip": skipped, "unknown": unknown,
                            "total": len(gates)},
            "verdict": str(report.get("verdict") or ""),
            "exit_code": report.get("exit_code"),
            "duration_s": report.get("duration_s"),
            "finished_at": finished,
            "age_s": age_s,
            "stale_after_s": STALE_AFTER_S,
            "unit": report.get("unit") or {},
            "coverage": report.get("coverage") or {},
            "pytest_cache": self._pytest_cache(),
            "report_file": str(self.report_file),
            "runnable": self.runner_script() is not None,
        }

    # -------------------------------------------------------------- running
    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def _default_runner(self, argv: list) -> int:   # pragma: no cover - subprocess
        proc = run_hidden(argv, cwd=str(self.root), capture_output=True,
                              text=True, timeout=self.timeout_s)
        return proc.returncode

    def runner_script(self):
        """The gate runner, or None when this installation does not ship it.

        Two layouts are supported: the development repo (``<root>/tests``) and an
        exported distribution, where the package sits at ``<dist>/src/sysperf`` and
        the suite at ``<dist>/tests``. Returning None rather than guessing lets
        run() refuse honestly instead of failing with a confusing traceback.
        """
        for base in (self.root, self.root.parent.parent):
            candidate = base / "tests" / "testrunner.py"
            if candidate.is_file():
                return candidate
        return None

    def _argv(self, live: bool, quick: bool) -> list:
        argv = [sys.executable, str(self.runner_script()),
                "--json", str(self.report_file)]
        if quick:
            argv.append("--quick")
        elif not live:
            argv.append("--no-live")
        return argv

    def _work(self, argv: list) -> None:
        error = None
        try:
            self._runner(argv)
        except Exception as exc:                        # noqa: BLE001 - reported
            error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._running = False
            self._last_error = error

    def run(self, live: bool = False, quick: bool = False,
            background: bool = True) -> dict:
        """Start the suite. Refuses (rather than queues) a concurrent run."""
        if self.runner_script() is None:
            return {"started": False, "running": False,
                    "reason": "this installation does not ship the gate runner "
                              "(tests/testrunner.py)"}
        with self._lock:
            if self._running:
                return {"started": False, "reason": "a run is already in flight",
                        "running": True}
            self._running = True
            self._started_at = time.time()
            self._last_error = None
        argv = self._argv(live, quick)
        if not background:
            self._work(argv)
            return {"started": True, "running": False, "mode": "blocking"}
        thread = threading.Thread(target=self._work, args=(argv,),
                                  name="selftest", daemon=True)
        thread.start()
        return {"started": True, "running": True, "mode": "background",
                "gates": len(GATES) if not quick else 2}
