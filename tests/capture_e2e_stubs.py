#!/usr/bin/env python3
"""
Refresh the browser-test stub payloads from a running instance.

The stubs in ``tests/e2e_stubs.json`` are captured rather than hand-written. The
first version of the browser suite invented response shapes, and its assertions
passed against markup that no real payload could produce — a test that proves
nothing is worse than no test at all.

    python3 tests/capture_e2e_stubs.py                       # against localhost
    python3 tests/capture_e2e_stubs.py --base http://host/p/  # anywhere

Lists are trimmed so the fixture stays readable, machine identity is scrubbed, and
a few states the tests depend on are forced so the suite does not become flaky
when the live instance happens to have nothing open.
"""
from __future__ import annotations

import argparse
import copy
import getpass
import json
import re
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:5056/"
OUT = Path(__file__).resolve().parent / "e2e_stubs.json"

ENDPOINTS = {
    "api/metrics": "", "api/rca": "", "api/autoimprove": "?limit=3",
    "api/trends": "", "api/learning": "?limit=3", "api/actions": "",
    "api/evolution": "", "api/selftest": "", "api/appctl": "",
    "api/onedrive": "", "api/procwatch": "", "api/health": "", "api/fixes": "",
}
# Long lists make the fixture unreadable without making it more faithful.
CAPS = {"learnings": 3, "recent": 3, "log": 3, "items": 4, "rows": 6,
        "timeline": 3, "knowledge_sets": 4, "consumers": 3, "apps": 3,
        "library": 3, "rules": 4, "strategies": 3, "recipes": 3, "shards": 2,
        "proposals": 2, "findings": 3, "preventive": 2, "gates": 7,
        "skipped": 3, "planned": 3, "executed": 2, "networks": 2}
# Identity that must never reach a committed fixture. Matched by shape and by the
# capturing user's own name rather than a hardcoded literal — a scrubber that names
# the thing it scrubs has simply moved the leak into itself.
PROFILE_RE = re.compile(r"((?:[A-Za-z]:\\\\|/mnt/[a-z]/)Users[\\\\/])"
                        r"(?!Public\b|Default\b|All\b)[A-Za-z0-9._-]+")
PLACEHOLDER = "testuser"


def trim(node, key=None):
    if isinstance(node, dict):
        return {k: trim(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [trim(v, key) for v in node[:CAPS.get(key, 4)]]
    return node


def fetch(base: str) -> dict:
    out = {}
    for path, query in ENDPOINTS.items():
        with urllib.request.urlopen(base.rstrip("/") + "/" + path + query,
                                    timeout=30) as response:
            out[path] = trim(json.loads(response.read()))
    series = out["api/metrics"].get("series")
    if isinstance(series, dict):
        out["api/metrics"]["series"] = {
            k: (v[:8] if isinstance(v, list) else v) for k, v in series.items()}
    return out


def force_states(data: dict) -> dict:
    """Guarantee the states the suite asserts on, whatever the live instance holds."""
    acts = data["api/actions"]
    opened = next((i for i in acts["items"] if i["status"] == "open"),
                  acts["items"][0])
    closed = copy.deepcopy(opened)
    closed.update({"key": "decision:handles", "id": "decision_handles",
                   "kind": "decision",
                   "title": "Decide what to do about handles",
                   "metric": "handles", "metric_label": "Active app handles",
                   "gap_share_pct": None, "cause": "chronic_unreachable",
                   "why_code": "chronic_unreachable",
                   "why_headline":
                       acts["reasons"]["chronic_unreachable"]["headline"],
                   "why_detail":
                       acts["reasons"]["chronic_unreachable"]["detail"],
                   "status": "closed", "closed_by": "human",
                   "closed_reason": "action taken",
                   "closed_at": "2026-09-29T07:00:00+00:00",
                   "note": "revised the target", "priority": None})
    acts["items"] = [opened, closed]
    acts.update({"count": 2, "open": 1, "closed": 1, "action_taken": 1,
                 "recurred": 0, "unique_keys": True,
                 "by_kind": {"fix": 0, "decision": 0, "review": 0, "blocked": 0},
                 "by_reason": {opened["why_code"]: 1}})
    acts["by_kind"][opened["kind"]] = 1

    evo = data["api/evolution"]
    base = evo["recipes"][0]
    ready = copy.deepcopy(base)
    ready.update({"state": "candidate", "executable": False,
                  "missing_params": [], "params": {"process": "msedge"},
                  "promoted_at": None})
    blocked = copy.deepcopy(base)
    blocked.update({"id": ready["id"] + "_blocked", "state": "candidate",
                    "executable": False, "missing_params": ["process"],
                    "params": {}, "promoted_at": None})
    promoted = copy.deepcopy(evo["recipes"][-1])
    promoted.update({"state": "promoted", "executable": False,
                     "missing_params": [],
                     "promoted_at": "2026-09-29T07:45:00+00:00"})
    evo["recipes"] = [ready, blocked, promoted]
    evo["kpis"].update({"candidates": 2, "promoted": 1})
    if not evo["proposals"]:
        evo["proposals"] = [{
            "id": "structural_handles", "metric": "handles",
            "label": "Active app handles", "kind": "structural",
            "detail": "The target sits below this hardware's floor.",
            "evidence": {"shortfalls": 400}, "state": "open",
            "created": "2026-09-29T07:30:00+00:00", "generation": 3}]
        evo["kpis"]["proposals"] = 1

    if not data["api/procwatch"].get("recent"):
        data["api/procwatch"]["recent"] = [
            {"at": "2026-09-29T07:00:00+00:00", "action": "restart",
             "status": "ok", "cpu_pct": 18.0}]

    if not data["api/fixes"].get("fixes"):
        data["api/fixes"]["fixes"] = [{
            "id": "flush_dns", "title": "Flush DNS resolver cache",
            "category": "network", "risk": "safe", "reversible": True,
            "reason": "stale cache", "executable": True,
            "session_killing": False, "persistent": False, "manual_hint": ""}]
    data["api/fixes"]["token"] = "test-token"
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help=f"instance to capture from (default {DEFAULT_BASE})")
    args = parser.parse_args(argv)

    data = force_states(fetch(args.base))
    text = json.dumps(data, indent=1, sort_keys=True)
    text = PROFILE_RE.sub(rf"\1{PLACEHOLDER}", text)
    # The capturing user's own name can appear outside a profile path too.
    me = getpass.getuser()
    if me and len(me) > 2:
        text = text.replace(me, PLACEHOLDER)
    OUT.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, "
          f"{len(data)} endpoints)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
