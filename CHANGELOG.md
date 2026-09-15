# Changelog

## 2.1.1 — 2026-09-15

Restore the complete uploaded source into WindowsAI. Fix authentication after CLI
host overrides, authenticated browser API calls, CSRF/Host checks, malformed fix
requests, empty selections, implicit caution batches, unsafe TEMP/WSL automation,
and command-error handling. Correct Windows BOM config loading, sample validation,
carry-forward fields, privacy mutation/leakage, zero-baseline scoring, WSL trend
resets, adaptive target regression, Wi-Fi overlap counts, installer retry/path
handling, and misleading component upgrade/gain claims. Update Flask/Waitress.
Add regression tests, packaging manifest and explicit startup-script tests.

Full evidence and validation limits: [audit record](docs/AUDIT-2.1.1.md).

All notable changes to SysPerf are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.0] — 2026-09-13

Adds a **Live Architecture** view, an opt-in **Common-Fix engine**, and nine new
derived criteria covering **responsiveness, thermal and startup/launch latency** —
all portable and driven entirely by the live snapshot (no hard-coded values).

### Added

- **Live Architecture tab.** A third dashboard view that draws how System
  Performance Agent works end to end — the read-only PowerShell collector →
  Python collector → history store & self-improving scorer → scoring/snapshot →
  advisor → Flask/waitress API → browser — as an animated SVG data-flow diagram
  driven by the live snapshot (sample cadence, samples retained, last sweep,
  criteria scored, self-improvement score, advice count) with a test-suite node
  and the `POST /api/recalibrate` loop. Pure inline SVG, CSP-compliant.
- **Common-Fix engine (opt-in).** `GET /api/fixes` detects recurring issues and
  new regressions from the live scorecard; `POST /api/fix` previews (dry-run) or,
  when `fixes.enabled` is set, applies only reversible non-disruptive fixes
  (CSRF-gated). Manual / session-killing fixes are only ever described. A
  one-click **Common Fix** modal drives it. Read-only remains the default.
- **Responsiveness & thermal criteria** — CPU utilisation, CPU run-queue depth,
  disk active-time and CPU temperature, each scored against a target derived from
  the machine's own baseline.
- **Startup & launch-latency criteria** — Windows boot time and cold-start /
  launch times for WSL, Explorer, Outlook and Word, sourced best-effort from the
  Diagnostics-Performance event log (omitted gracefully when unavailable).
- **Battery-wear** health metric, derived from design vs full-charge capacity.
- **`scripts/optimize-startup.ps1`** — reversible, schedulable startup optimiser
  (tuned `.wslconfig`, SysMain, power plan) so startup/launch stay fast without
  the button.
- **Inline SVG favicon.**

### Changed

- **Details tab restructured** into labelled `<section>`s (Overview, Scorecard,
  Trends, Browser, WSL & kernel, Wi-Fi, Network, Hardware, Drivers, Components)
  with a sticky in-page section navigation and smooth-scroll anchors.
- Architecture/test-suite node values are descriptive or live-bound rather than
  hard-coded counts, so nothing on the dashboard goes stale.

## [2.0.0] — 2026-09-12

Feature release. Adds a tabbed **Summary / Details** experience, first-class
Wi-Fi and network analysis, and a component-upgrade advisory — all portable to
any Windows 11 machine (native or WSL 2).

### Added

- **Summary tab with a 10-category scorecard.** The whole dashboard distilled
  into ten scored categories (memory, WSL, browser, handles, CPU, storage,
  network, Wi-Fi, hardware/drivers, component currency), each with a score and
  the remaining room for improvement, plus one overall system score.
- **Wi-Fi connection panel.** SSID, negotiated link rate, radio generation,
  band/channel and — where Windows Location permits — live signal strength.
  Works unelevated via `Get-NetAdapter` / `Get-NetConnectionProfile`.
- **Band & channel overlap analysis.** Scans neighbouring APs and recommends the
  clearest 2.4 GHz channel (1/6/11 model) and least-used 5 GHz channel.
- **Network optimisation plan.** Prioritised, reversible actions tagged
  client / router / windows, derived live from the metrics.
- **Component-upgrade advisory.** Version-currency table for the components
  actually present (Windows, PowerShell 5.1/7, Edge, WebView2, GPU & network
  drivers, BIOS, and Python/Node/Git/WSL when installed), with per-component
  scores. Only detected components are listed, so it is correct on any machine.
- **`optimize-wifi.ps1`** helper (reversible, admin-run) for client-side Wi-Fi
  tuning.
- **Self-contained HTML manual** (`SysPerf-User-Manual.html`) replaces the PDF —
  sticky table of contents, responsive, offline, prints cleanly — with new
  technical appendices: architecture and data flow, collected-data reference,
  scoring mathematics, Wi-Fi/network internals, security and privacy model,
  performance characteristics and extension points. Build-time dependency is now
  just `markdown` (WeasyPrint no longer required).
- **Environment-neutral defaults.** The non-closable browser-host list now covers
  common Windows shell components and enterprise VPN/management clients generically
  instead of one organisation's endpoint stack.

### Changed

- **Single canonical overall score.** The Details hero and the Summary now share
  one computation, so the headline figure and "room for improvement" always
  agree (fixes an inconsistency where the hero showed baseline-progress while the
  Summary showed the category score).
- **Machine identity is fully dynamic** (hostname, model, CPU, OS from the
  profile) — no hardcoded host anywhere in the UI.
- Collector emits Wi-Fi, neighbour-scan and component-version data; PowerShell 7
  is used automatically when available (5.1 remains fully supported).

## [1.0.0] — 2026-09-10

First public release. SysPerf began as a single-machine remediation tracker with
hand-tuned constants; this release is the portable, machine-agnostic rewrite.

### Added

- **Per-machine baseline.** The first full sample is persisted to
  `baseline.json`. Progress is measured from your own starting point, and
  `--recalibrate` resets it deliberately rather than automatically.
- **Derived targets.** Every target is computed from the machine's own capacity —
  RAM, system-volume size, measured browser-process floor — using ratios in
  `config["targets"]`. No absolute constants remain.
- **Advisor.** Twenty rules turn measurements into prioritised recommendations,
  each stating the finding, why it matters, the steps, whether administrator
  rights are needed, and whether the change is reversible. Read-only by design.
- **Native Windows support.** Runs as a per-user sign-in task with no
  administrator rights and no system service. WSL 2 remains supported as an
  alternative runtime.
- **Locale independence.** Performance data comes from
  `Win32_PerfFormattedData_*` CIM classes instead of localised performance-counter
  paths, and power plans are matched by GUID rather than display name, so SysPerf
  works on non-English Windows.
- **Dynamic criteria.** Only metrics the machine actually reports are scored: no
  WSL row without WSL, no battery wear row on a desktop.
- **Token authentication.** A non-loopback bind generates and requires a token
  instead of silently exposing unauthenticated telemetry.
- **New metrics.** Top consumers by memory and by handle count, pagefile
  allocation and peak, active power plan, pending restart, real-time protection
  state, battery wear against design capacity, `.wslconfig` contents and
  automatic-reclaim state, enabled startup entries by name.
- **Two-phase startup.** The first sample is fast so the dashboard is usable in
  seconds; the full sweep follows and captures the baseline. Previously the first
  sample was a full sweep, leaving the page blank for up to a minute.
- **`--report`, `--once`, `--where`** command-line modes for use without a browser.
- **Anonymised mode** replaces application and device names with stable hashes so
  the dashboard can be screen-shared.
- **Installers** for Windows (`install.ps1`, `uninstall.ps1`, `run.ps1`) and WSL
  (`install-wsl.sh`), plus a `pyproject.toml` for `pip install`.
- **Test suite** of 96 tests that runs on any platform by stubbing the PowerShell
  layer.

### Changed

- Loopback binding, security headers and a strict Content-Security-Policy are now
  applied by the application rather than assumed from a reverse proxy.
- The dashboard reads all baselines, targets and machine identity from the API.
  Nothing is hardcoded in the front end.
- `waitress` serves the app when available; the Flask development server is only a
  fallback.

### Fixed

- **Config defaults could be mutated at runtime.** Nested defaults were shared by
  reference with the resolved configuration, so mutating one instance rewrote the
  module-level defaults for the rest of the process.
- **The trend estimator broke at slower sample cadences.** The "collector was
  down" gap was a fixed 120 s, so any cadence above 40 s re-anchored on every
  sample and the trend never computed. The threshold is now derived from the
  observed sample interval.
- **A criterion already better than its achievable floor scored 0%** while
  reporting "met", because the baseline → target span pointed the wrong way.
  Reaching a target now always scores 100%.
- **Day-one noise read as a regression.** With baseline equal to the current
  value, any upward tick crossed it. A 2% dead-band (`regress_band_pct`) absorbs
  measurement noise.
