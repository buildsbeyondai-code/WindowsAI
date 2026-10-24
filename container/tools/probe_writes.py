#!/usr/bin/env python3
"""
Exercise the POST endpoints and report whether each reached the host.

Deliberately avoids the three genuinely disruptive actions -- restarting
Explorer, stopping OneDrive, killing a ranked app and shutting WSL down -- since
confirming the plumbing does not require causing the side effect. Everything
else is run for real, using dry-run where the endpoint offers it.
"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5056"


def post(path, payload, timeout=120):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"_raw": "non-json"}
    except Exception as exc:                                  # noqa: BLE001
        return "EXC", {"error": str(exc)[:90]}


def show(label, code, data, keys=()):
    bits = []
    for k in keys:
        if k in data:
            bits.append(f"{k}={str(data[k])[:60]}")
    if not bits:
        bits = [f"keys={list(data)[:6]}"]
    refused = data.get("reason") or data.get("error")
    if refused:
        bits.append(f"REFUSED/ERR={str(refused)[:80]}")
    print(f"{label:34} {str(code):4}  {'; '.join(bits)[:150]}")


# Read current mode first so setting it back is a no-op rather than a change.
_, cur = post("/api/autoimprove/mode", {})
mode = (cur.get("mode") if isinstance(cur, dict) else None) or "auto"

print("--- safe / reversible write paths ---")
show("POST /api/autoimprove/mode", *post("/api/autoimprove/mode",
                                         {"mode": mode}), keys=("mode", "ok"))
show("POST /api/fix (dry-run)", *post("/api/fix",
                                      {"ids": [], "dry_run": True}),
     keys=("dry_run", "results", "applied", "ok"))
show("POST /api/evolution/run", *post("/api/evolution/run", {}),
     keys=("ok", "ran", "evolutions", "generated"))
show("POST /api/selftest/run", *post("/api/selftest/run",
                                     {"live": False, "quick": True}),
     keys=("started", "running", "reason"))
show("POST /api/actions/status", *post("/api/actions/status",
                                       {"id": "__probe__", "status": "open"}),
     keys=("ok", "updated"))
show("POST /api/procwatch (toggle)", *post("/api/procwatch", {}),
     keys=("enabled", "ok"))
show("POST /api/onedrive/control", *post("/api/onedrive/control", {}),
     keys=("enabled", "ok"))
show("POST /api/appctl (no action)", *post("/api/appctl", {}),
     keys=("ok", "error"))
show("POST /api/onedrive (no action)", *post("/api/onedrive", {}),
     keys=("ok", "error"))
print("\nskipped on purpose (real side effects): procwatch/run (restarts "
      "Explorer),\n  autoimprove/run (applies remediations), appctl stop/start, "
      "onedrive stop/start,\n  evolution/promote + rollback (mutate evolution state)")
