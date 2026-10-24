#!/usr/bin/env python3
"""Compact parity probe: hit every GET endpoint and report only health signals."""
import json
import urllib.request

BASE = "http://127.0.0.1:5056"
GETS = ["/api/actions", "/api/appctl", "/api/autoimprove", "/api/evolution",
        "/api/fixes", "/api/health", "/api/learning", "/api/metrics",
        "/api/onedrive", "/api/onedrive/control", "/api/procwatch",
        "/api/rca", "/api/selftest", "/api/trends"]

# Keys that indicate a feature produced no data at all.
EMPTY_MARKERS = ("", None, [], {}, 0)


def probe(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=25) as r:
            raw = r.read()
            code = r.status
    except Exception as exc:                                  # noqa: BLE001
        return path, "EXC", str(exc)[:80], ""
    try:
        d = json.loads(raw)
    except ValueError:
        return path, code, "non-json", f"{len(raw)}B"
    notes = []
    if isinstance(d, dict):
        for k in ("error", "err", "message", "reason", "why_not"):
            v = d.get(k)
            if v:
                notes.append(f"{k}={str(v)[:70]}")
        empty = [k for k, v in d.items()
                 if isinstance(v, (list, dict)) and len(v) == 0]
        if empty:
            notes.append("empty:" + ",".join(sorted(empty)[:6]))
    return path, code, "; ".join(notes) or "ok", f"{len(raw)}B"


for p in GETS:
    path, code, note, size = probe(p)
    print(f"{path:26} {str(code):4} {size:>9}  {note[:120]}")
