# Project state

_Last updated: 2026-10-25, release 5.0.0._

## 5.1.1 (2026-10-27)

- Every Components row has an upgrader (`upgrader.py`); self-evolution tests
  each upgrade in isolation, applies it, verifies it and rolls it back on
  failure. Major upgrades (Ubuntu LTS→LTS, Python minor) are automatic, at the
  user's request, with a distro export / previous installer as the backup.
  Drivers, BIOS, the Windows build and Edge are handed to their own updaters.
- GitHub only, not submitted to the Store.

## 5.1.0 (2026-10-27)

- Self-evolution upgrades outdated hash-locked runtime packages
  (`depupgrade.py`, the first lane of a cycle), tested in the sandbox and
  applied automatically when green. Host tools are not upgraded by SysPerf.
- Every timestamp in the UI is local to the viewer, with the zone named.
- Shipped self-evolution candidate c-20261026-202335-a5f0d0 (cpuwatch).
- GitHub only, not submitted to the Store.

## Current scope

5.0.0 brings SysPerf in line with the UAISES v1.0 engineering standard. It
closes the DNS-rebinding hole and makes the docs, the settings and the UI say
only what the code does. Feature work is paused until 5.0.0 ships.

## What changed in 5.0.0

See `CHANGELOG.md`. In short:

- **Security:** Host/Origin guard, CSP, DPAPI key file, SECURITY.md rebuilt
  from the route table.
- **Supply chain:** hash-locked runtime dependencies, pip-audit, SBOM, Dependabot.
- **Honesty:**
  - Settings that were documented but ignored (`autoimprove.*` limits) are now
    honoured. Keys that do nothing were removed from the example config.
  - The `auto_retain` switch now works.
  - README, the manual, START-HERE and CONTRIBUTING no longer describe removed
    features. Tests check all of this.
- **Engineering docs:** architecture, requirements traced to tests, decisions,
  operations and incident records, release checklist, `CLAUDE.md`.
- **Tests added:** Host guard, CSRF on every route, docs truth,
  example-config truth, requirements traceability, accessibility, heap soak,
  live values against CIM (Windows), real DPAPI (Windows).

## Verification status

Full evidence: `docs/releases/5.0.0.md`. On release commit ad5cdd1:

- Linux: 1,173 passed, 0 failed. Windows: 1,250 passed, 0 failed.
  Evolution sandbox: 1,173 passed, 0 failed.
- In-app Self-test: 8/8 gates, 100% line coverage (9,339 lines), 100% UI coverage.
- Live 5.0.0: Host/Origin/CSRF probes refused, CSP present, values match
  Windows, UI clean in a real browser.

| Item | State |
|---|---|
| MSIX install / upgrade from 4.1.0 / uninstall | PASS on the developer PC (see the release record's packaging test); clean machine still UNVERIFIED |
| 24-hour `tools/soak.py` run on 5.0.0 | UNVERIFIED: not yet run |
| Real side effect of standby clear, Explorer watchdog, App Control, OneDrive stop | UNVERIFIED: tested with fakes (see FEATURE_AUDIT.md) |
| Screen-reader walk-through of the dashboard | UNVERIFIED: static checks only |

## Next steps

1. Run `tools/soak.py --hours 24` on the live service and record the result here.
2. Repeat the packaging test on a clean Windows 11 machine (Windows Sandbox
   or a VM). It passed on the developer PC, but there the Store app shares
   the scripted install's data folder.
3. Consider event-driven triggers for any metric that gains a cheap event
   source (ADR-005).
4. Split `app.py` (about 1,650 lines) into blueprints by area.
