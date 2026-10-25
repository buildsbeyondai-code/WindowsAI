# Project state

_Last updated: 2026-10-25, release 5.0.0._

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

The 5.0.0 release record (the filled-in `RELEASE_CHECKLIST.md`) lists each
gate with the command run and its result. Open items:

| Item | State |
|---|---|
| Clean-machine MSIX install / upgrade / uninstall | UNVERIFIED: tested on the developer PC only |
| 24-hour `tools/soak.py` run on 5.0.0 | UNVERIFIED: not yet run |
| Screen-reader walk-through of the dashboard | UNVERIFIED: static checks only |

## Next steps

1. Run `tools/soak.py --hours 24` on the live service and record the result here.
2. Install the 5.0.0 MSIX on a clean Windows 11 VM: install, first launch,
   upgrade from 4.1.0, uninstall.
3. Consider event-driven triggers for any metric that gains a cheap event
   source (ADR-005).
4. Split `app.py` (about 1,650 lines) into blueprints by area.
