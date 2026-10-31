# Project state

_Last updated: 2026-10-25, release 5.0.0._

## 5.4.8 (2026-10-31)

- Hand-cherry-picked pending winners count as shipped; disabling evolution stops auto-apply.

## 5.4.7 (2026-10-31)

- Live Wi-Fi speed (3 s) with charts that have axes, values and times.
- Auto-apply also deploys winners left waiting, re-checked on the current main.

## 5.4.6 (2026-10-31)

- Driver rows follow what Windows Update offers the PC (daily, search only)
  instead of age alone.

## 5.4.5 (2026-10-31)

- Audit of every category: `docs/AUDIT-2026-10-31.md` (41 issues, 22 fixed,
  19 listed with the action each needs).
- Python upgrades unblocked (helper health check), false post-sleep alerts and
  per-minute alert repeats gone, inbox repeats merged, WSL probe off the
  request path, security headers, PSScriptAnalyzer clean.

## 5.4.4 (2026-10-31)

- Store app closes itself when Windows stages its update and reopens on the
  new version (`selfupdate.py`); every update had failed once with 0x80073D02.
- Upgrades bring their dependencies: uv relocks as CI does, the sandbox tests
  the resolved set, new lock packages become Components rows (ARCHITECTURE.md
  step 6). Dev-tool upgrades (pytest 9.2.0) are no longer refused.
- Light theme redesigned to match the dark one.
- GitHub only; Store listing improvements are prepared but not submitted.

## 5.4.3 (2026-10-31)

- Sandbox baseline green again (a test needed 20 GB free in the sandbox's
  small /tmp); `tools/sandbox_suite.py` runs in pre-push. GitHub only.

## 5.4.2 (2026-10-31)

- An upgrade rejected by the test suite is retried once the checkout has a
  newer commit (Python 3.15.2 had been rejected by a mid-edit checkout).

## 5.4.1 (2026-10-31)

- Component upgrades drain in hours: the next cycle follows after `cooldown_s`
  (30 min), not `interval_s` (6 h), and upgrade cycles do not spend `max_per_day`.
- The Components assessment date follows the daily live version check.
- Edit loop: `tools/affected_tests.py` (impact-based selection), full suite in
  `tools/hooks/pre-push`, Linux/sandbox from CI; `docs/LEARNINGS.md`.
- Ubuntu 24.04 -> 26.04.1 upgraded on the live PC by the upgrader (tested in a copy).

## 5.4.0 (2026-10-31)

- SysPerf creates System Restore points itself (`restorepoint.py`,
  `restore_point.ps1`) before the registry fix, app updates and component
  upgrades, and weekly; set up once after one UAC prompt.
- Apps update themselves after each 6-hourly scan from Safe-auto. Machine-wide
  installs need an administrator, so they are updated together after one
  prompt instead of hanging unattended; an update an open app blocked is shown
  as waiting and retried; a timeout stops the installers winget started.
- Components: one pass upgrades every outdated Windows-side component.
- GitHub, then the Store after verification.

## 5.3.0 (2026-10-30)

- Maintenance check (`maintenance.py`, `maintenance.ps1`): seven Details
  sections fed by a read-only scan every 6 hours — protection, cleanup,
  registry, security and crashes, services and tasks, app updates. New fixes:
  clear browser caches, back up and remove registry orphans (HKCU only), and
  one-app winget updates; restore point, DISM/SFC and Windows Update cleanup
  are manual (administrator). GitHub only.
- Next: privacy settings, power plan and battery report, visual effects and
  Game Mode, hosts-file check, network speed test (not started).

## 5.2.1 (2026-10-30)

- The open dashboard costs 0.4% of a core instead of 6.1% (0.1% hidden):
  unchanged samples are not redrawn, polling stops while hidden, and the
  animations no longer repaint every frame. GitHub only.

## 5.2.0 (2026-10-30)

- Windows-side component upgrades work on a PC with no AI provider, no WSL
  and no source checkout (`CodeEvolution.upgrade_pass`): unattended from
  Safe-auto, or with Upgrade now. The AI cycle keeps the repository and WSL
  upgrades.
- GitHub only, not submitted to the Store.

## 5.1.2 (2026-10-30)

- Shipped self-evolution candidate c-20261027-022409-a8b8d7 (vmmem_trend).
- GitHub only, not submitted to the Store.

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
