"""Watch a running SysPerf for memory growth and staleness over hours.

Read-only: it only sends GET requests. Every ``--every`` seconds it records the
service's own RSS, CPU and sample age from /api/diagnostics and /api/health and
writes them to a CSV. A reading that could not be taken is recorded too.

The verdict is one of three (5.5.2, audit F5: missing data used to pass):

* ``fail`` (exit 1)         a measured problem: RSS grew more than
                            ``--max-growth-mb`` from the warm-up median, data
                            went stale, or health said not ok;
* ``inconclusive`` (exit 2) nothing failed, but the evidence is too thin to
                            say it passed: covered time or complete readings
                            below ``--min-coverage``, a gap between readings,
                            too few RSS readings to judge growth, or the
                            service restarted (a restart resets RSS, which
                            could hide a leak);
* ``pass`` (exit 0)         neither.

A missing value is never read as a healthy one. Sleep is not tolerated: the
host must stay awake for the whole run, so a suspended period shows as a gap
and the run is inconclusive. A restart is detected from the service's uptime
going down between readings.

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

WARMUP_S = 3600          # the RSS baseline is the median of the first hour
GAP_FACTOR = 2.0         # readings further apart than 2 x --every are a gap


def get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.load(r)


def sample(base: str, fetch=get, clock=time.time) -> dict:
    row = {"t": round(clock()), "rss_mb": None, "avg_cpu_pct": None, "threads": None,
           "uptime_s": None,
           "sample_age_s": None, "ok": None, "errors_1h": None, "error": ""}
    try:
        diag, health = fetch(base, "/api/diagnostics"), fetch(base, "/api/health")
    except Exception as exc:                     # recorded, judged as missing
        row["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return row
    fp = diag.get("footprint") or {}
    row.update(rss_mb=fp.get("rss_mb"), avg_cpu_pct=fp.get("avg_cpu_pct"),
               threads=fp.get("threads"), uptime_s=fp.get("uptime_s"), sample_age_s=health.get("last_sample_age_s"),
               ok=health.get("ok"), errors_1h=(diag.get("health") or {}).get("errors_1h"))
    return row


def _dur(s: float) -> str:
    return f"{s / 3600:.2f} h" if s >= 7200 else f"{s / 60:.1f} min"


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _complete(r: dict) -> bool:
    return _num(r.get("rss_mb")) and _num(r.get("sample_age_s")) and isinstance(r.get("ok"), bool)


def verdict(rows: list, max_growth_mb: float, stale_s: float, every: float,
            hours: float | None = None, min_coverage: float = 0.95) -> dict:
    """{"state": pass|fail|inconclusive, "problems", "gaps_in_evidence", "coverage"}."""
    problems, thin = [], []
    rows = [r for r in rows if _num(r.get("t"))]
    ts = [r["t"] for r in rows]
    span = (ts[-1] - ts[0]) if len(ts) >= 2 else 0.0
    wanted_s = hours * 3600 if hours else span
    expected_n = int(wanted_s / every) + 1 if every > 0 else len(rows)
    complete = [r for r in rows if _complete(r)]
    gaps = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > GAP_FACTOR * every]
    cov = {"readings": len(rows), "complete": len(complete), "expected": expected_n,
           "covered_s": span, "wanted_s": wanted_s, "gaps": len(gaps),
           "gap_s": sum(b - a for a, b in gaps)}

    # measured problems (only from values that were actually measured)
    stale = [r for r in complete if r["sample_age_s"] > stale_s]
    not_ok = [r for r in complete if r["ok"] is False]
    if stale:
        problems.append(f"{len(stale)} reading(s) with data older than {stale_s:g} s")
    if not_ok:
        problems.append(f"{len(not_ok)} reading(s) where health was not ok")
    rss = [(r["t"], r["rss_mb"]) for r in rows if _num(r.get("rss_mb"))]
    t0 = ts[0] if ts else 0
    warm = [v for t, v in rss if t - t0 <= WARMUP_S]
    late = [v for t, v in rss if t - t0 > max(WARMUP_S, span - WARMUP_S)]
    if len(warm) >= 2 and late:
        base, end = statistics.median(warm), statistics.median(late)
        cov["rss_base_mb"], cov["rss_end_mb"] = base, end
        if end - base > max_growth_mb:
            problems.append(f"RSS grew {end - base:.1f} MB (median {base:.1f} in the first "
                            f"hour, {end:.1f} in the last)")
    else:
        thin.append(f"memory growth not judged: {len(warm)} RSS reading(s) in the warm-up "
                    f"hour and {len(late)} after it (need 2 and 1)")

    # completeness of the evidence
    if not rows:
        thin.append("no readings")
    if span < min_coverage * wanted_s:
        thin.append(f"covered {_dur(span)} of {_dur(wanted_s)}")
    if len(complete) < min_coverage * expected_n:
        thin.append(f"{len(complete)} complete reading(s) of {expected_n} expected")
    if gaps:
        thin.append(f"{len(gaps)} gap(s) between readings, {cov['gap_s'] / 60:.0f} min in total")
    ups = [r["uptime_s"] for r in rows if _num(r.get("uptime_s"))]
    cov["restarts"] = sum(1 for a, b in zip(ups, ups[1:]) if b < a)
    if cov["restarts"]:
        thin.append(f"the service restarted {cov['restarts']} time(s): memory growth "
                    "across a restart cannot be judged")
    errors = sum(1 for r in rows if r.get("error"))
    if errors:
        thin.append(f"{errors} reading(s) could not be taken")
    state = "fail" if problems else "inconclusive" if thin else "pass"
    return {"state": state, "problems": problems, "gaps_in_evidence": thin, "coverage": cov}


EXIT = {"pass": 0, "fail": 1, "inconclusive": 2}


def main(argv=None, fetch=get, clock=time.time, sleep=time.sleep) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:5056")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--every", type=float, default=300.0, help="seconds between readings")
    ap.add_argument("--max-growth-mb", type=float, default=50.0)
    ap.add_argument("--stale-s", type=float, default=600.0)
    ap.add_argument("--min-coverage", type=float, default=0.95)
    ap.add_argument("--out", default="soak.csv")
    a = ap.parse_args(argv)
    # readings on fixed ticks from the start, so request time does not
    # accumulate and the last reading lands at the end of the period
    rows, start, k = [], clock(), 0
    with open(a.out, "w", newline="", encoding="utf-8") as fh:
        w = None
        while True:
            row = sample(a.base, fetch, clock)
            rows.append(row)
            if w is None:
                w = csv.DictWriter(fh, fieldnames=list(row))
                w.writeheader()
            w.writerow(row)
            fh.flush()
            k += 1
            due = start + k * a.every
            if due > start + a.hours * 3600 + 1e-6:
                break
            sleep(max(0.0, due - clock()))
    v = verdict(rows, a.max_growth_mb, a.stale_s, a.every, a.hours, a.min_coverage)
    c = v["coverage"]
    print(f"{c['readings']} readings ({c['complete']} complete of {c['expected']} expected) "
          f"over {_dur(c['covered_s'])} of {_dur(c['wanted_s'])} -> "
          f"{v['state'].upper()}")
    for p in v["problems"]:
        print("  - FAIL:", p)
    for p in v["gaps_in_evidence"]:
        print("  - evidence:", p)
    with open(a.out + ".verdict.json", "w", encoding="utf-8") as fh:
        json.dump(v, fh, indent=1)
    return EXIT[v["state"]]


if __name__ == "__main__":
    sys.exit(main())
