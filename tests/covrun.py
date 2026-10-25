#!/usr/bin/env python3
"""
Coverage runner for the perf-dashboard test suite.

Uses only the Python standard library because this environment has no network
access to PyPI (coverage.py / pytest-cov cannot be installed). A direct
``sys.settrace`` line tracer records executed lines for the target modules while
the pytest suite runs; ``trace._find_executable_linenos`` supplies the set of
executable lines (``# pragma: no cover`` honoured).

Exit code 0 iff every target module reaches 100% line coverage.
"""
import os
import sys
import threading
from collections import defaultdict
from pathlib import Path
from trace import _find_executable_linenos

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TARGETS = [
    "app.py", "config.py", "adaptive.py", "scoring.py", "fixes.py",
    "collector.py", "browser.py", "hardware.py", "netopt.py", "components.py",
    "trends.py", "rca.py", "autoimprove.py", "cpuwatch.py", "onedrive.py",
    "appctl.py", "session_policy.py", "onedrive_control.py", "procwatch.py",
    "learning.py", "selftest.py", "evolve.py", "actions.py",
    "platform_paths.py", "cli.py",
    "target_review.py", "refresh_components.py",          # 3.1 / 3.2
    "llm.py", "evodb.py", "optactions.py", "codevolve.py",  # 4.0
    "trayquit.py", "machine_profile.py", "livestats.py", "background_process.py",
    "pwsh_session.py", "historydb.py", "health.py", "notify.py",  # 4.1
    "settings_hub.py", "supervisor.py", "desktop.py", "store_mode.py",
    "version_check.py",
    "hostguard.py",                                       # 5.0.0
]
# Two layouts: the development repo keeps the modules at the root, an exported
# distribution keeps them in src/sysperf/. Resolving here means one runner covers
# both instead of the export needing a forked copy.
PKG = os.path.join(ROOT, "src", "sysperf")
if not os.path.isdir(PKG):
    PKG = ROOT
target_abs = {os.path.realpath(os.path.join(PKG, t)): t for t in TARGETS}

_executed = defaultdict(set)
_rp_cache = {}


def _realpath(fn):
    rp = _rp_cache.get(fn)
    if rp is None:
        rp = os.path.realpath(fn)
        _rp_cache[fn] = rp
    return rp


def _line_tracer(frame, event, arg):
    if event == "line":
        _executed[_realpath(frame.f_code.co_filename)].add(frame.f_lineno)
    return _line_tracer


def _global_tracer(frame, event, arg):
    if event == "call" and _realpath(frame.f_code.co_filename) in target_abs:
        return _line_tracer
    return None


def pragma_lines(path):
    """Lines excluded by ``# pragma: no cover``.

    A pragma on a compound-statement header (line ending with ``:``) also
    excludes the whole indented block below it, mirroring coverage.py.
    """
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out = set()
    for i, line in enumerate(lines, 1):
        if "pragma: no cover" not in line:
            continue
        out.add(i)
        indent = len(line) - len(line.lstrip())
        if line.split("#", 1)[0].rstrip().endswith(":"):
            j = i               # 0-based index of the next line is lines[i]
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() == "":
                    out.add(j + 1)
                    j += 1
                    continue
                if len(nxt) - len(nxt.lstrip()) > indent:
                    out.add(j + 1)
                    j += 1
                else:
                    break
    return out


def main():
    # threading.settrace too: sys.settrace covers only the calling thread, so code
    # that tests run on worker threads (reader pumps, background senders) would
    # otherwise be reported as never executed.
    threading.settrace(_global_tracer)
    sys.settrace(_global_tracer)
    try:
        # -p no:assertion / no:cacheprovider keep the run trace-friendly and
        # side-effect free; plain asserts still work.
        # The browser suites are excluded deliberately, and this is a large win:
        # they drive Chromium against static HTML and execute *no* runtime module,
        # so they add nothing to line coverage — but running them here meant they
        # ran twice (once under sys.settrace, which is 10-50x slower per line, and
        # again in the browser gate). The browser gate owns them.
        ignores = []
        for suite in sorted(Path(HERE).glob("test_e2e*.py")):
            ignores += ["--ignore", str(suite)]
        rc = pytest.main(["-q", "-p", "no:cacheprovider", "-p", "no:assertion",
                          *ignores, HERE])
    finally:
        sys.settrace(None)
        threading.settrace(None)

    print("\n" + "=" * 58)
    print("COVERAGE (stdlib settrace; # pragma: no cover honoured)")
    print("=" * 58)
    total_exec = total_hit = 0
    failed = []
    for rp, name in sorted(target_abs.items(), key=lambda kv: kv[1]):
        ex = set(_find_executable_linenos(rp)) - pragma_lines(rp)
        ex.discard(0)
        hit = _executed.get(rp, set()) & ex
        miss = sorted(ex - hit)
        total_exec += len(ex)
        total_hit += len(hit)
        pct = 100.0 * len(hit) / len(ex) if ex else 100.0
        print(f"  {name:14} {len(hit):4}/{len(ex):<4} {pct:6.1f}%"
              + ("" if not miss else "  MISSING"))
        if miss:
            print(f"       missing lines: {miss}")
            failed.append(name)
    overall = 100.0 * total_hit / total_exec if total_exec else 100.0
    print("-" * 58)
    print(f"  {'TOTAL':14} {total_hit:4}/{total_exec:<4} {overall:6.2f}%")
    print("=" * 58)

    if rc != 0:
        print(f"TESTS FAILED (pytest rc={rc})")
        return 1
    if failed:
        print("COVERAGE INCOMPLETE for: {}".format(", ".join(failed)))
        return 1
    print("ALL TARGET MODULES AT 100% LINE COVERAGE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
