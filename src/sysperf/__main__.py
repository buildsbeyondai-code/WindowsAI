"""
Command-line entry point.

    sysperf                     start the dashboard
    sysperf --once              take one sample and print the JSON (no server)
    sysperf --report            print a text performance report and exit
    sysperf --recalibrate       reset the baseline to the current state
    sysperf --where             show the data directory and config path
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import APP_TITLE, CONFIG_FILE, DATA_DIR, VERSION, load_config
from .platform_win import UnsupportedPlatformError, is_supported, runtime_name


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sysperf",
        description=f"{APP_TITLE} {VERSION} — read-only Windows performance "
                    "dashboard and advisor.")
    p.add_argument("--host", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="bind port (default 5056)")
    p.add_argument("--config", help="path to sysperf.config.json")
    p.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    p.add_argument("--once", action="store_true",
                   help="take a single full sample, print the raw JSON, and exit")
    p.add_argument("--report", action="store_true",
                   help="print a text report of scores and recommendations, then exit")
    p.add_argument("--recalibrate", action="store_true",
                   help="reset the baseline to the current state")
    p.add_argument("--where", action="store_true",
                   help="print the data directory and config file path")
    p.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"{APP_TITLE} {VERSION}")
    return p


def _bar(pct: float, width: int = 28) -> str:
    filled = int(round(max(0.0, min(100.0, pct)) / 100 * width))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def _machine_line(prof: dict) -> str:
    vendor = str(prof.get("machine_vendor") or "").strip()
    model = str(prof.get("machine_model") or "").strip()
    # Many OEMs repeat the vendor inside the model string (e.g. vendor "Acme" and
    # model "Acme ProBook"), so the duplicate prefix is dropped when present.
    if vendor and model.lower().startswith(vendor.lower()):
        vendor = ""
    return f"{prof.get('hostname', '?')} — {vendor} {model}".replace("  ", " ").rstrip(" —")


def _text_report(snapshot: dict) -> str:
    sc = snapshot.get("scorecard") or {}
    prof = snapshot.get("profile") or {}
    advice = (snapshot.get("advice") or {}).get("items") or []
    meta = snapshot.get("meta") or {}
    lines = [
        f"{APP_TITLE} {VERSION} — performance report",
        "=" * 74,
        f"Machine : {_machine_line(prof)}",
        f"CPU     : {prof.get('cpu_name', '?')} "
        f"({prof.get('cpu_cores', '?')}C/{prof.get('cpu_threads', '?')}T)",
        f"OS      : {prof.get('os_caption', '?')} build {prof.get('os_build', '?')}",
        f"RAM     : {prof.get('total_ram_mb', '?')} MB",
        f"Baseline: {meta.get('baseline_captured', 'not captured')} "
        f"({meta.get('baseline_note', '')})",
        "",
        f"Overall improvement : {sc.get('overall_improvement', 0)}%  "
        f"{_bar(sc.get('overall_improvement') or 0)}",
        f"Criteria scored     : {sc.get('scored', 0)}/{sc.get('total_criteria', 0)}",
        "",
        "Scorecard",
        "-" * 74,
        f"  {'CRITERION':<28}{'NOW':>12}{'TARGET':>14}{'PROGRESS':>11}  STATUS",
    ]
    for row in sc.get("rows") or []:
        arrow = ">" if row["higher_is_better"] else "<"
        target = f"{arrow} {row['target']:,.0f}"
        prog = "measuring" if row["progress"] is None else f"{row['progress']:.0f}%"
        lines.append(
            f"  {row['label'][:27]:<28}{row['current']:>12,}{target:>14}"
            f"{prog:>11}  {row['status']}")

    lines += ["", f"Recommendations ({len(advice)})", "-" * 74]
    if not advice:
        lines.append("  Nothing to act on — every criterion is met.")
    for item in advice:
        lines.append(f"  [{item['severity'].upper():<8}] {item['title']}")
        lines.append(f"     finding: {item['finding']}")
        for step in item["action"]:
            lines.append(f"     - {step}")
        if item.get("command"):
            lines.append(f"     command: {item['command']}"
                         + ("  (needs admin)" if item.get("needs_admin") else ""))
        lines.append("")
    lines.append("SysPerf changed nothing. Every action above is yours to take.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    if args.where:
        print(f"data directory : {DATA_DIR}")
        print(f"config file    : {args.config or CONFIG_FILE}")
        print(f"runtime        : {runtime_name()}")
        return 0

    if not is_supported():
        print(f"{APP_TITLE} measures a Windows host. Run it on Windows 11 "
              "(recommended) or inside WSL 2.", file=sys.stderr)
        return 2

    overrides = {}
    if args.host is not None:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.open:
        overrides["open_browser"] = True
    cfg = load_config(args.config, overrides=overrides)

    # Imported here so --where and --version work on any platform.
    from .adaptive import AdaptiveScorer
    from .collector import Collector
    from .config import ADAPTIVE_FILE

    try:
        if args.once or args.report or args.recalibrate:
            collector = Collector(cfg, AdaptiveScorer(ADAPTIVE_FILE, cfg))
            # The background service takes a fast sample first so its dashboard
            # is not blank; a one-shot run has nothing to show early, so it goes
            # straight to the full sweep. That takes tens of seconds.
            print("sampling (a full sweep takes up to a minute)…", file=sys.stderr)
            collector.sample_count = 1
            if not collector.sample():
                print(f"collection failed: {collector.error}", file=sys.stderr)
                return 1
            if args.once:
                print(json.dumps(collector.latest, indent=2, default=str))
                return 0
            if args.recalibrate:
                collector.recalibrate()
                print(f"baseline reset to the current state "
                      f"({collector.baseline['captured']}).")
                return 0
            print(_text_report(collector.snapshot()))
            return 0

        from .app import serve
        serve(cfg)
        return 0
    except UnsupportedPlatformError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
