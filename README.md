# SysPerf — System Performance Agent

## 2.1.1 behavior and validation notes

SysPerf is **read-only by default**. If `fixes.enabled` is the boolean `true`,
`POST /api/fix` can run explicitly selected DNS flushing, Explorer restart, or
Recycle Bin emptying actions. Explorer restart is disruptive; Recycle Bin
emptying is irreversible. Caution actions are never selected by default.
TEMP deletion and automatic dashboard WSL tuning are manual-only.

- Open a token-protected dashboard with `/?token=YOUR_TOKEN`; the page forwards
  the token in the `X-SysPerf-Token` header on every API request.
- Recalibration requires `X-SysPerf-CSRF`, using the `token` from authenticated
  `GET /api/fixes`. Cross-origin mutation requests are rejected.
- `POST /api/fix` requires a JSON object with a string-list `ids`, a boolean
  `confirm`, and the CSRF `token` to execute. Empty apply selections are rejected.
- `scripts/optimize-startup.ps1` changes nothing without explicit action switches.
  `-TuneWsl` merges settings with a unique backup; `-EnableSysMain` and
  `-HighPerformance` are separate choices. Add `-WhatIf` to preview. High performance
  affects battery operation too. Record previous service/power settings to restore them.
- WSL `autoMemoryReclaim` and `sparseVhd` belong under `[experimental]`;
  `vmIdleTimeout`, `memory` and `processors` belong under `[wsl2]`.
  Existing WSL memory, CPU, networking settings and comments are preserved.
- Component version currency is **not verified online**. No performance-gain
  numbers are inferred from versions. Check the vendor or your managed update channel.
- ACPI thermal-zone readings are not necessarily CPU core temperatures.
  Explorer/Outlook/Word event durations describe startup degradation events from
  the current boot, not interactive launch benchmarks. WSL service events are
  no longer presented as WSL cold-start measurements. Missing probes stay unavailable.
- Anonymization redacts known identifiers across API projections and derived text;
  it leaves raw local history intact. Review exports before sharing: arbitrary
  user-supplied free text is not a formal privacy guarantee.

See [the audit record](docs/AUDIT-2.1.1.md) for fixes, tests and platform limitations.


**A read-only-by-default performance dashboard and advisor for Windows 11.** It measures the
machine, learns that machine's own baseline, scores progress against targets
derived from its actual capacity, and tells you exactly what to change.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%2011%20%7C%20WSL%202-informational.svg)](#requirements)

> **New in 2.0** — a tabbed **Summary / Details** UI. The **Summary** distils the
> whole dashboard into a **10-category scorecard** (each with a score and room
> for improvement and one overall system score). New **Wi-Fi**, **band/channel
> overlap** and **network optimisation** panels, and a **component-upgrade
> advisory** (Windows, PowerShell, Edge/WebView2, drivers, BIOS, and
> Python/Node/Git/WSL when present). Everything is portable — machine identity,
> baselines and targets are all derived per machine. See the [changelog](CHANGELOG.md).

## Documentation

- **[SysPerf-User-Manual.html](SysPerf-User-Manual.html)** — the complete manual:
  installation, reading the dashboard, scoring maths, configuration and API, plus
  technical appendices (architecture and data flow, collected-data reference,
  Wi-Fi/network internals, security model, performance, extension points).
  Self-contained HTML: open it in any browser, works offline, prints cleanly.
- Source: [`docs/USER_MANUAL.md`](docs/USER_MANUAL.md) — rebuild with
  `pip install markdown && python docs/build_manual.py`.


```
System score        : 46%  [#############...............]
Criteria scored     : 9/10

  CRITERION                            NOW        TARGET   PROGRESS  STATUS
  Commit charge                     22,140      < 24,336        84%  partial
  Memory Compression                 1,180       < 1,622       100%  met
  Available physical memory         13,004      > 11,212       100%  met
  Browser-engine processes              78          < 74        71%  partial
  Startup programs (enabled)             6           < 6       100%  met

Recommendations (2)
  [HIGH  ] A power-saving plan is active
  [MEDIUM] Third-party drivers older than 4 years are installed
```

---

## Why this exists

Most Windows "optimisers" have two problems. They compare your machine against
numbers that came from somebody else's machine, and they change settings for you
so that when something breaks you cannot tell what happened.

SysPerf does neither.

**It derives everything from your machine.** On first run it captures a baseline
and computes targets from your actual RAM, disk size and measured process floor.
A 64 GB workstation and an 8 GB laptop get different targets, because the same
number cannot be right for both.

**It never changes anything.** Every collector call is a query. The advisor
produces text: what was measured, why it matters, the step to take, whether it
needs administrator rights, and whether it is reversible. You decide and you act,
so you always know what changed and why. Uninstalling leaves nothing to revert.

**It is honest about what it cannot measure.** A criterion that needs more data
reports `measuring` and is excluded from the score rather than assumed good. A
touchpad that misbehaves while Windows reports "OK" cannot be auto-detected, so
there is a manual watchlist for it.

---

## What it measures

| Area | Metrics |
|---|---|
| **Memory** | Commit charge, commit limit, available physical, Memory Compression working set, kernel paged/non-paged pools, page-fault rate |
| **Processes** | Total processes, threads, open handles, top consumers by memory and by handle count |
| **Browser** | Standalone browser and embedded-browser (WebView2) processes, attributed to the host application that started them, split into mandatory / in-use / discretionary, plus extension footprint |
| **Storage** | System-volume free space and size, pagefile allocation and peak usage |
| **Network** | Established remote TCP connections, loopback IPC, listeners (remote and loopback counted separately — loopback is not network load) |
| **Startup** | Auto-start entries that are actually *enabled*, and which ones they are |
| **Hardware** | Device Manager fault codes, storage health, battery charge and wear against design capacity, pointing devices |
| **Drivers** | Third-party driver inventory, unsigned packages, stale packages older than four years, driver-specific fault codes |
| **Platform** | Active power plan, pending restart, real-time protection state, Windows Search health, uptime |
| **WSL 2** | Working set against the configured cap, `.wslconfig` contents, automatic-reclaim state, and Linux-guest memory when running in WSL mode |

Metrics are sampled on two tiers: cheap counters every 20 seconds, expensive
inventories (services, drivers, hardware) every fifteenth sample. The very first
sample is a fast one so the dashboard is usable within seconds; the full sweep
follows and captures the baseline about a minute in.

---

## Install

### Windows 11 (recommended)

```powershell
# From an unzipped copy of this repository
powershell -ExecutionPolicy Bypass -File install.ps1
```

That is the whole installation. It:

- finds Python 3.9+, offering to install it via `winget` if it is missing;
- creates a private virtual environment under `%LOCALAPPDATA%\SysPerf`;
- registers a **per-user sign-in task** so the dashboard is always available;
- adds Start Menu entries for the dashboard and for a one-off report;
- opens <http://127.0.0.1:5056/>.

**No administrator rights are required.** Nothing is written outside
`%LOCALAPPDATA%\SysPerf` and your own Start Menu. No system service is created and
no policy or registry setting is changed.

Options:

```powershell
install.ps1 -Port 5100        # use a different port
install.ps1 -NoAutoStart      # skip the sign-in task; start it yourself
install.ps1 -NoStart          # install without launching
```

### Without installing

```powershell
powershell -ExecutionPolicy Bypass -File run.ps1 -Report   # one-off report
powershell -ExecutionPolicy Bypass -File run.ps1           # dashboard
```

Creates a `.venv\` in the source folder and touches nothing else. Useful on a
machine you would rather not install anything on.

### From PyPI or a checkout

```powershell
py -m pip install --user .
sysperf --report
sysperf                       # dashboard on http://127.0.0.1:5056/
```

### WSL 2 (optional alternative)

```bash
bash install-wsl.sh
```

Runs Python inside a WSL distribution and reaches Windows through interop. Every
sample pays the interop cost and WSL must be running, but you also get
Linux-guest memory metrics. See the comments at the top of the script.

### Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File uninstall.ps1           # keep your data
powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Purge    # delete it too
```

---

## Requirements

| | |
|---|---|
| **OS** | Windows 11 (build 22000+). Windows 10 works but is not the test target. |
| **PowerShell** | 5.1, which ships with Windows. PowerShell 7 also works. |
| **Python** | 3.9 or later. The installer can fetch it via `winget`. |
| **Rights** | Standard user. Nothing needs elevation. |
| **Network** | None. Dependencies are fetched once at install; after that SysPerf is fully offline. |

Non-English Windows is supported: performance data comes from locale-independent
CIM classes rather than from localised performance-counter names, and the power
plan is matched by GUID rather than by its display name.

---

## Usage

```
sysperf                     start the dashboard (http://127.0.0.1:5056/)
sysperf --report            print a text report with recommendations, then exit
sysperf --once              take one sample and print the raw JSON
sysperf --recalibrate       reset the baseline to the current state
sysperf --where             show the data directory and config path
sysperf --port 5100 --open  serve on another port and open a browser
```

### API

Every endpoint is `GET` and returns JSON.

| Endpoint | Returns |
|---|---|
| `/api/metrics` | Everything: latest sample, chart series, scorecard, browser breakdown, hardware, recommendations, profile, baseline |
| `/api/advice` | Just the prioritised recommendations |
| `/api/profile` | Machine identity, the stored baseline, and the derived criteria |
| `/api/health` | Liveness, sample count, last collector error |
| `/api/config` | Effective configuration, with the auth token removed |
| `POST /api/recalibrate` | Resets the baseline; requires `X-SysPerf-CSRF` from `/api/fixes`. |

---

## How scoring works

**Progress** per criterion is `(current − baseline) ÷ (target − baseline)`,
clamped to 0–100. Reaching the target is always 100%.

Three details make the number trustworthy rather than merely reactive:

1. **Smoothing.** Process, handle and connection counts breathe constantly.
   Scoring the latest sample makes the score collapse on every upward breath, so
   the *sustained* state — a rolling median — is scored instead. Live gauges and
   charts still show the raw value. Bursty metrics that are sampled sparsely get
   a longer window (`smooth_window_overrides`).

2. **A noise dead-band.** On day one the baseline *is* the current value, so any
   upward tick would read as a regression. A metric within `regress_band_pct`
   (2% by default) of its baseline counts as unchanged.

3. **Honest abstention.** WSL memory is a *ceiling* criterion — "at or below the
   cap **and not growing**" — not a reduction target, so the generic formula does
   not apply. Establishing a trend needs at least six samples over ten minutes,
   because Windows trims working sets opportunistically and a multi-gigabyte trim
   over four minutes extrapolates to a meaningless five-figure rate. Until the
   window is long enough the criterion reads `measuring` and is left **out** of
   the overall score rather than assumed good.

### Adaptive scoring

Alongside the fixed baseline → target scorecard, SysPerf tracks each metric's own
history: the **personal best** (your best *daily typical* ever recorded), an
**adaptive target** a few percent beyond it, and today's typical value. The bar
only ever moves in the improving direction, so it tightens as your habits improve
and never rewards a regression. This answers a different question from the plan
score: not "how far through the plan am I" but "is today better than my normal".

---

## Configuration

Everything tunable lives in one file. Find it with `sysperf --where`; on Windows
it is `%LOCALAPPDATA%\SysPerf\sysperf.config.json`. See
[`sysperf.config.example.json`](sysperf.config.example.json) for the annotated
version. Omitted keys keep their defaults. Restart SysPerf after editing.

The keys worth knowing about:

| Key | Default | Effect |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `5056` | Bind address. A non-loopback host requires a token — see [Security](#security). |
| `sample_seconds` | `20` | Fast-tier cadence. |
| `full_every` | `15` | Slow-tier metrics every Nth sample. |
| `targets.*` | see example | Fractions of *your* capacity used to derive targets. Raise to loosen, lower to tighten. |
| `browser_hosts` | common apps | Which applications' embedded-browser processes count as unclosable or in-use. **Worth editing** — an unlisted app is treated as discretionary and inflates what SysPerf expects you to reduce. |
| `advisor.enabled` | `true` | Turn recommendations off entirely. |
| `anonymize` | `false` | Replace application and device names with stable hashes so the dashboard is safe to screen-share. |

Environment overrides: `SYSPERF_DATA_DIR`, `SYSPERF_CONFIG`, `SYSPERF_HOST`,
`SYSPERF_PORT`, `SYSPERF_TOKEN`, `SYSPERF_SAMPLE_SECONDS`, `SYSPERF_ANONYMIZE`,
`SYSPERF_PS_EXE`.

### Hardware watchlist

Windows cannot detect a fault it does not consider a fault. A touchpad button
that sticks, a fan that rattles, a port that works only sometimes — all report
"OK" to every API. Record them in `hw_watchlist.json` in the data directory and
they appear on the dashboard:

```json
[
  {
    "component": "Touchpad — left button",
    "issue": "Click registers twice under moderate pressure",
    "severity": "minor",
    "status": "open",
    "note": "Started after the March firmware update."
  }
]
```

The file is re-read on every request, so edits show up immediately.

---

## Security

- **Read-only.** Every collector call is a query: `Get-CimInstance`,
  `Get-Process`, `Get-ItemProperty`, `pnputil /enum-drivers`,
  `powercfg /getactivescheme`. Nothing is written, started, stopped or
  reconfigured. The collector is a single readable PowerShell file —
  [`src/sysperf/scripts/collect_win.ps1`](src/sysperf/scripts/collect_win.ps1) —
  audit it yourself.
- **Local by default.** The server binds `127.0.0.1`. The API describes your
  machine in detail — running processes, installed drivers, open connections —
  and should not be exposed casually.
- **Token required off loopback.** Setting a non-loopback `host` makes SysPerf
  generate a token, store it in the config file, and require it on every request
  (`X-SysPerf-Token` header or `?token=`). It will not serve unauthenticated
  telemetry to a network. There is **no TLS**: put a reverse proxy in front of it
  for anything beyond a trusted LAN.
- **No outbound traffic.** Nothing is uploaded, no telemetry is sent, and the
  front end loads no third-party scripts or fonts. After installation the tool
  works with the network off.
- **No injection surface.** The collector script path is fixed in code and no
  HTTP input reaches a shell.
- **Response headers** on every response: `nosniff`, `SAMEORIGIN`, `no-referrer`,
  `no-store`, and a restrictive `Content-Security-Policy`.

Recalibration writes SysPerf’s own baseline. Optional fix execution can change the system, as described above.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

---

## Architecture

```
src/sysperf/
├── __main__.py          CLI: serve, --report, --once, --recalibrate, --where
├── app.py               Flask factory, auth, security headers, WSGI server
├── collector.py         background sampler, rolling history, snapshot assembly
├── platform_win.py      Windows/WSL detection, PowerShell bridge, path translation
├── config.py            defaults, config file, env overrides, bind-security policy
├── profile.py           baseline capture, target derivation, per-machine criteria
├── scoring.py           progress, smoothing, ceiling criterion, trend estimator
├── adaptive.py          personal-best layer
├── advisor.py           recommendation rules
├── browser.py           browser-process ownership breakdown
├── hardware.py          device/disk/battery/driver panel + watchlist
├── scripts/
│   └── collect_win.ps1  the only Windows-specific code — one JSON object on stdout
└── static/
    └── index.html       dashboard: hand-written SVG charts, no JS dependencies
```

Dependency direction is acyclic: `config` and `platform_win` are leaves;
`scoring`, `profile`, `browser`, `hardware` and `advisor` sit above them;
`collector` composes those; `app` only wires things together.

State lives in the data directory, never in the install directory:

| File | Purpose |
|---|---|
| `baseline.json` | Your starting point, captured once. Progress is measured from here. |
| `history.jsonl` | Samples, auto-trimmed at ~12 MB. |
| `adaptive_state.json` | Personal bests and daily typicals. |
| `sysperf.config.json` | Your configuration. |
| `hw_watchlist.json` | Faults you track by hand. |

---

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
PYTHONPATH=src .venv/bin/python -m pytest      # 96 tests, no Windows required
PYTHONPATH=src .venv/bin/python -m ruff check src tests
```

The test suite replaces the PowerShell layer with a synthetic sample, so
everything above the collector boundary is testable on Linux, macOS or in CI.
The fixture machine is deliberately not anyone's real machine: the whole point is
that no machine is special.

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The one rule
that is not negotiable: **SysPerf does not write to the system.** A pull request
that changes a Windows setting, however helpfully, will not be merged.

---

## Troubleshooting

**The dashboard does not load.** Check the service is up:

```powershell
Get-ScheduledTask -TaskName 'SysPerf Dashboard'
& "$env:LOCALAPPDATA\SysPerf\sysperf.cmd"     # run in the foreground to see errors
```

**"collector produced no output".** PowerShell could not run the collector. Try
it directly:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\SysPerf\app\src\sysperf\scripts\collect_win.ps1" -Full
```

**Everything reads 0%.** Expected on day one: the baseline is the current state,
so there is no distance travelled yet. Numbers move as you act on the
recommendations.

**No scores at all for the first minute.** The baseline needs the first full
sweep, which enumerates drivers and hardware. The dashboard says so while it waits.

**Scores look worse than the live value.** They are measuring the sustained
state, not the latest sample. See [How scoring works](#how-scoring-works).

**A criterion is stuck on `measuring`.** The WSL trend needs six samples over ten
minutes. Leave it running.

**Slow samples.** The full sweep enumerates services and drivers and takes a few
seconds. Raise `full_every` to run it less often.

**Port already in use.** `install.ps1 -Port 5100`, or set `port` in the config.

**Python not found even after installing it.** Open a new terminal so the updated
`PATH` is picked up, then re-run the installer.

---

## FAQ

**Does it slow my machine down?** The fast sample is a few hundred milliseconds
of PowerShell every 20 seconds. The full sweep costs a few seconds every fifteenth
sample. The API caches its snapshot so repeated polling is nearly free.

**Does it send anything anywhere?** No. There is no outbound network code beyond
the local HTTP server.

**Why does it not just fix things?** Because then you would not know what changed.
Silent registry and service edits are exactly why "optimiser" tools have the
reputation they do. Recommendations tell you what to do, what it will affect, and
whether it can be undone.

**Can I run it on a server or a fleet?** It is built for a single desktop. Nothing
prevents multiple instances, but there is no aggregation layer. The JSON API is
stable enough to scrape if you want to build one.

**Does it need my browser open?** No. Collection happens in the background service;
the dashboard is just a view of it. `sysperf --report` needs no browser at all.

---

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
