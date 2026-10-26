#!/usr/bin/env python3
"""
Coverage runner for the perf-dashboard test suite.

Uses only the Python standard library because this environment has no network
access to PyPI (coverage.py / pytest-cov cannot be installed). A direct
``sys.settrace`` line tracer records executed lines for the target modules while
the pytest suite runs; ``_find_executable_linenos`` (below) supplies the set of
executable lines (``# pragma: no cover`` honoured).

Exit code 0 iff every target module reaches 100% line coverage.
"""
import inspect
import os
import sys
import threading
import trace
from collections import defaultdict
from pathlib import Path

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
    "version_check.py", "depupgrade.py", "upgrader.py",
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


def _find_executable_linenos(path):
    """Source lines that can execute, as ``trace._find_executable_linenos`` gives.

    Python 3.14+ compiles annotations into a lazy ``__annotate__`` function that
    runs only if something inspects them (PEP 649). trace counts its lines, so
    every multi-line ``def`` signature read as uncovered on Python 3.15. Those
    code objects are skipped; everything else uses trace's own helpers.
    """
    with open(path, encoding="utf-8") as fh:
        code = compile(fh.read(), path, "exec")
    strs = trace._find_strings(path, "utf-8")
    lines, todo = set(), [code]
    while todo:
        co = todo.pop()
        if co.co_name == "__annotate__":
            continue
        lines.update(trace._find_lines_from_code(co, strs))
        todo.extend(c for c in co.co_consts if inspect.iscode(c))
    return lines


def pragma_lines(path):
    """Lines excluded by ``# pragma: no cover`` (and, on Windows, ``posix only``).

    A pragma on a compound-statement header (line ending with ``:``) also
    excludes the whole indented block below it, mirroring coverage.py.
    """
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out = set()
    for i, line in enumerate(lines, 1):
        # "posix only" marks code Linux CI covers but Windows cannot reach
        # (/proc, bubblewrap, POSIX modes); it is excluded only on Windows, the
        # mirror of "no cover - Windows" for code Linux cannot reach.
        if "pragma: no cover" not in line and not (
                os.name == "nt" and "pragma: posix only" in line):
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


def fresh_basetemp() -> str:
    """A private pytest temp root for this run.

    pytest's default root, %TEMP%\\pytest-of-<user>, is shared with every other
    pytest run on the machine. On 2026-10-25 that folder carried an ACL the
    in-app Self-test could not open (WinError 5), and every test errored. A
    fresh root per run cannot be locked or owned by anything else.
    """
    import tempfile
    return tempfile.mkdtemp(prefix="sysperf-gates-")


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
        basetemp = fresh_basetemp()
        rc = pytest.main(["-q", "-p", "no:cacheprovider", "-p", "no:assertion",
                          "--basetemp", basetemp, *ignores, HERE])
    finally:
        sys.settrace(None)
        threading.settrace(None)
    import shutil
    shutil.rmtree(basetemp, ignore_errors=True)

    print("\n" + "=" * 58)
    print("COVERAGE (stdlib settrace; # pragma: no cover honoured)")
    print("=" * 58)
    total_exec = total_hit = 0
    failed = []
    for rp, name in sorted(target_abs.items(), key=lambda kv: kv[1]):
        ex = set(_find_executable_linenos(rp)) - pragma_lines(rp)
        # Neither is a source line: 0 marks module set-up, and Python 3.15's
        # co_lines() reports None for synthetic instructions (it crashed the
        # sort below on Windows, so the coverage gate could not run there).
        ex.discard(0)
        ex.discard(None)
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
