"""Watch a running SysPerf for memory growth and staleness over hours.

Read-only: it only sends GET requests. Every ``--every`` seconds it records the
service's own RSS, CPU and sample age from /api/diagnostics and /api/health,
writes them to a CSV, and at the end fails (exit 1) if RSS grew more than
``--max-growth-mb`` from the first hour's median or the data went stale.

    python tools/soak.py --hours 24 --out soak.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.request


def get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.load(r)


def sample(base: str) -> dict:
    diag, health = get(base, "/api/diagnostics"), get(base, "/api/health")
    fp = diag.get("footprint") or {}
    return {"t": round(time.time()), "rss_mb": fp.get("rss_mb"),
            "avg_cpu_pct": fp.get("avg_cpu_pct"), "threads": fp.get("threads"),
            "sample_age_s": health.get("last_sample_age_s"), "ok": health.get("ok"),
            "errors_1h": (diag.get("health") or {}).get("errors_1h")}


def verdict(rows: list, max_growth_mb: float, stale_s: float, every: float) -> list:
    """Reasons the soak failed; empty when it passed."""
    problems = []
    rss = [r["rss_mb"] for r in rows if isinstance(r["rss_mb"], (int, float))]
    if len(rss) >= 2:
        first_hour = rss[:max(1, int(3600 / every))]
        base = statistics.median(first_hour)
        if rss[-1] - base > max_growth_mb:
            problems.append(f"RSS grew {rss[-1] - base:.1f} MB (from {base:.1f} to {rss[-1]:.1f})")
    stale = [r for r in rows if (r["sample_age_s"] or 0) > stale_s or r["ok"] is False]
    if stale:
        problems.append(f"{len(stale)} reading(s) stale or not ok")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:5056")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--every", type=float, default=300.0, help="seconds between readings")
    ap.add_argument("--max-growth-mb", type=float, default=50.0)
    ap.add_argument("--stale-s", type=float, default=600.0)
    ap.add_argument("--out", default="soak.csv")
    a = ap.parse_args(argv)
    rows, end = [], time.time() + a.hours * 3600
    with open(a.out, "w", newline="", encoding="utf-8") as fh:
        w = None
        while True:
            row = sample(a.base)
            rows.append(row)
            w = w or csv.DictWriter(fh, fieldnames=list(row))
            if len(rows) == 1:
                w.writeheader()
            w.writerow(row)
            fh.flush()
            if time.time() + a.every > end:
                break
            time.sleep(a.every)
    problems = verdict(rows, a.max_growth_mb, a.stale_s, a.every)
    print(f"{len(rows)} readings over {a.hours} h -> {'FAIL' if problems else 'PASS'}")
    for p in problems:
        print("  -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
