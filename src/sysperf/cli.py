#!/usr/bin/env python3
"""
Command line for SysPerf.

The Windows installer has passed ``--port``, ``--open``, ``--report`` and
``--once`` since 2.x, and the manual documented ``sysperf --where`` — none of
which existed. The arguments were accepted and silently ignored, so ``-Port 5100``
produced a dashboard on 5056 and ``sysperf-report.cmd`` started a server instead
of printing a report. This module implements the interface that was already
promised.

    sysperf                     run the dashboard
    sysperf --port 5100 --open  run on another port and open a browser
    sysperf --where             print the resolved platform, paths and settings
    sysperf --once              take one sample, print it, exit
    sysperf --report            print a text summary of the current score
    sysperf --version

Options are applied by setting the corresponding environment variables *before*
:mod:`config` is imported, which keeps one precedence chain — command line beats
environment beats settings file beats default — rather than two competing ones.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Command-line option -> the environment variable config.py reads. Declared as
# data so the precedence is visible and testable rather than buried in branches.
ENV_FOR = {
    "host": "PERFDASH_HOST",
    "port": "PERFDASH_PORT",
    "data_dir": "SYSPERF_DATA_DIR",
    "config": "SYSPERF_CONFIG",
    "mode": "PERFDASH_AUTO_MODE",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sysperf",
        description="Self-improving Windows 11 performance dashboard.")
    parser.add_argument("--host", help="interface to bind (default 127.0.0.1; "
                                      "widening it adds no authentication)")
    parser.add_argument("--port", type=int, help="port to serve on (default 5056)")
    parser.add_argument("--data-dir", help="directory for history and learned "
                                          "state (default: per-user location)")
    parser.add_argument("--config", help="path to a JSON settings file")
    parser.add_argument("--mode", choices=("off", "observe", "auto"),
                        help="auto-improve mode for this run")
    parser.add_argument("--open", action="store_true",
                        help="open the dashboard in a browser once it is up")
    parser.add_argument("--where", action="store_true",
                        help="print the resolved platform, paths and settings, "
                             "then exit")
    parser.add_argument("--once", action="store_true",
                        help="take a single sample, print it as JSON, then exit")
    parser.add_argument("--report", action="store_true",
                        help="print a text summary of the current score, then exit")
    parser.add_argument("--version", action="store_true",
                        help="print the version and exit")
    return parser


def _apply_env(args, env: dict) -> None:
    """Push command-line choices into the environment config.py reads."""
    for attr, key in ENV_FOR.items():
        value = getattr(args, attr, None)
        if value is not None:
            env[key] = str(value)


def _version() -> str:
    try:
        import sysperf                       # present only in an installed build
        return sysperf.__version__
    except Exception:                        # noqa: BLE001 - dev tree
        return "dev"


def _where(config, describe) -> str:
    facts = describe(config.PLATFORM)
    lines = [
        f"platform        {facts['platform']}",
        f"powershell      {facts['powershell'] or '(none found)'}",
        f"can collect     {'yes' if facts['can_collect'] else 'no'}",
    ]
    if facts["why_not"]:
        lines.append(f"                {facts['why_not']}")
    lines += [
        f"code            {config.BASE}",
        f"data            {config.DATA_DIR}",
        f"settings file   {config.CONFIG_FILE}"
        f"{'' if config.CONFIG_FILE.exists() else '  (not present — using defaults)'}",
        f"bind            http://{config.HOST}:{config.PORT}/",
        f"fixes enabled   {'yes' if config.FIX_ENABLED else 'no'}",
        f"auto-improve    {config.AUTO_MODE} every {config.AUTO_INTERVAL_S:.0f}s, "
        f"threshold {config.AUTO_THRESHOLD:.0f}%",
    ]
    return "\n".join(lines)


def _report(snapshot: dict) -> str:
    """A plain-text score summary, for `sysperf --report` and a scheduled task."""
    card = snapshot.get("scorecard") or {}
    rows = [r for r in (card.get("rows") or []) if isinstance(r, dict)]
    overall = card.get("overall_improvement")
    lines = [f"SysPerf — overall {'—' if overall is None else f'{overall:.1f}%'}"]
    counts = card.get("counts") or {}
    if counts:
        lines.append("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if not rows:
        lines.append("  no samples yet — the first one lands within a minute")
    for row in sorted(rows, key=lambda r: r.get("progress") or 0):
        current = row.get("scored_typical", row.get("current"))
        lines.append(
            f"  {str(row.get('status', '?')):<9} "
            f"{str(row.get('label', row.get('key', ''))):<28} "
            f"{'—' if current is None else f'{current:>10.1f}'} "
            f"/ target {row.get('target')}")
    return "\n".join(lines)


def main(argv=None, env=None, stdout=None, serve=None, opener=None) -> int:
    """Entry point. Collaborators are injectable so every branch is testable."""
    env = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    if args.version:
        print(_version(), file=out)
        return 0

    _apply_env(args, env)
    # Imported only now: config resolves paths and settings at import time, so it
    # has to see the environment the options just set.
    from . import app as app_module
    from . import config as config_module
    from .platform_paths import describe

    if args.where:
        print(_where(config_module, describe), file=out)
        return 0
    if args.once:
        snapshot = app_module.collector.snapshot()
        print(json.dumps(snapshot.get("latest") or {}, indent=1, default=str),
              file=out)
        return 0
    if args.report:
        print(_report(app_module.collector.snapshot()), file=out)
        return 0

    url = f"http://{config_module.HOST}:{config_module.PORT}/"
    # "open_browser": true in sysperf.config.json (documented in the example
    # config but never read until now) opens the Summary at every start, which
    # is what the sign-in task needs for the page to appear after a restart.
    if args.open or config_module.SETTINGS.get("open_browser") is True:
        (opener or _open_browser)(url + "#summary")
    print(f"SysPerf on {url}  (data in {config_module.DATA_DIR})", file=out)
    (serve or app_module.main)()
    return 0


def _open_browser(url: str) -> None:                 # pragma: no cover - UI side
    """Open a browser shortly after start, off the serving thread."""
    import threading
    import webbrowser
    threading.Timer(1.5, webbrowser.open, args=(url,)).start()
