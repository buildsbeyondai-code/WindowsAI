# SysPerf — System Performance Agent

**A self-improving performance dashboard for Windows 11.** It measures the
machine, learns that machine's own baseline, scores progress against targets
derived from its actual capacity, and tells you exactly what to change. With
your consent it makes the safe changes itself, and every one can be undone.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%2011%20%7C%20WSL%202-informational.svg)](#requirements)

> **New in 5.0**: DNS-rebinding protection, a Content-Security-Policy, an encrypted
> API key file, a hash-locked supply chain, and docs and settings that are checked
> against the code. See [What's new in 5.0](#whats-new-in-50) and the
> [changelog](CHANGELOG.md).

## Documentation

- **[docs/SysPerf-User-Manual.html](docs/SysPerf-User-Manual.html)** — the complete
  manual: installation, reading the dashboard, scoring maths, configuration and API,
  and technical appendices. Self-contained HTML that works offline and prints
  cleanly. Source: [`docs/USER_MANUAL.md`](docs/USER_MANUAL.md); rebuild it with
  `pip install markdown && python docs/build_manual.py`.
- **Engineering docs:** [architecture](docs/ARCHITECTURE.md),
  [requirements and acceptance criteria](docs/REQUIREMENTS.md),
  [decisions](docs/DECISIONS.md), [operations and incidents](docs/OPERATIONS.md),
  [release checklist](docs/RELEASE_CHECKLIST.md),
  [project state](docs/PROJECT_STATE.md), [feature audit](docs/FEATURE_AUDIT.md),
  [security policy](SECURITY.md). The project follows the
  [Universal AI Software Engineering Standard](docs/UNIVERSAL_AI_SOFTWARE_ENGINEERING_STANDARD.md).

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

**It changes nothing you have not allowed.** Every collector call is a query,
and by default (`observe`) SysPerf only measures and recommends: what was
measured, why it matters, the step to take, whether it needs administrator rights,
and whether it is reversible. Anything it does on its own is opt-in, logged and
reversible: low-risk fixes you enable one by one (`safe_auto`), quitting tray apps
you have allowed (with a Reopen button), and — at `self_evolve` — AI patches to its
own code that are tested, benchmarked, applied only by you, and rolled back
automatically if the patched service is unhealthy. Security controls are never
touched at any level. Uninstalling leaves nothing to revert.

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

Metrics are sampled on two tiers: cheap counters every 60 seconds by default
(`sample_seconds`, 30–600), expensive inventories (services, drivers, hardware)
every fifth sample. Since 4.1 one PowerShell session is kept warm and reused for
every sample, which roughly halves the cost of each one. The very first
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

- finds Python 3.10+, offering to install it via `winget` if it is missing;
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

### Command line

```
sysperf                      run the dashboard
sysperf --port 5100 --open   another port, and open a browser
sysperf --where              resolved platform, paths, settings — start here when empty
sysperf --once               take one sample, print it as JSON, exit
sysperf --report             print a text score summary, exit
sysperf --host 0.0.0.0       widen the bind (adds no authentication — see Security)
sysperf --data-dir D:\sp     where history and learned state live
sysperf --config C:\s.json   a JSON settings file
sysperf --mode observe       analyse without remediating, for this run
sysperf --version
```

Settings resolve command line → environment variable → JSON settings file →
built-in default. The installer writes the settings file for you:

```jsonc
{
  "host": "127.0.0.1",
  "port": 5056,
  "sample_seconds": 60,
  "fixes":       { "enabled": true },
  "autoimprove": { "mode": "auto", "threshold": 80, "interval_seconds": 300 }
}
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

## Platforms

SysPerf measures a **Windows 11** host. It runs in three places, and resolves its
tools differently in each — run `sysperf --where` to see what it picked.

| Where it runs | PowerShell it uses | Data directory |
|---|---|---|
| Native Windows (`install.ps1`) | `PATH`, then `C:\Program Files\PowerShell\7`, then `%LOCALAPPDATA%`, then Windows PowerShell 5.1 | `%LOCALAPPDATA%\SysPerf\data` |
| WSL 2 (`install-wsl.sh`) | interop `pwsh.exe` on `PATH`, else the `/mnt/c` mounts | `$XDG_DATA_HOME/sysperf` or `~/.local/share/sysperf` |
| Plain Linux | none — there is no Windows host to measure | n/a |

An existing installation keeps writing to the directory it already uses, so
upgrading never orphans your history. `SYSPERF_DATA_DIR` overrides it, and
`SYSPERF_PS_EXE` overrides the shell.

On plain Linux the dashboard still starts, and says plainly that it cannot
collect rather than sitting empty — `/api/health` and `sysperf --where` both
report the reason.

### Container (WSL 2 + Docker, optional)

Since 3.2.0 the web/API tier can also run in an unprivileged container
(`CapDrop: ALL`, default seccomp, read-only root, non-root, loopback only).
Host execution goes through a small allowlisting broker on a unix socket
instead of `--privileged`; every host command is logged.

```bash
bash container/install-container.sh
```

Details, measured trade-offs and limits: [`container/README.md`](container/README.md).

## Requirements

| | |
|---|---|
| **OS** | Windows 11 (build 22000+). Windows 10 works but is not the test target. |
| **PowerShell** | 5.1, which ships with Windows. PowerShell 7 also works. |
| **Python** | 3.10 or later. The installer can fetch it via `winget`. |
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
sysperf --where             show the data directory and config path
sysperf --supervise         run under the supervisor (what the sign-in task does)
sysperf --port 5100 --open  serve on another port and open a browser
```

To capture a new baseline, delete `baseline.json` from the data directory
(`sysperf --where`) and restart. The next settled samples become the baseline.

### API

Read endpoints are `GET`; the handful that change something are `POST`, require
the per-process CSRF token, and are listed as such. All return JSON.

| Endpoint | Returns |
|---|---|
| `/api/metrics` | Everything: latest sample, chart series, scorecard, browser breakdown, hardware, components, profile and baseline |
| `/api/live` | Second-fresh headline values read straight from Win32 APIs |
| `/api/health` | Liveness, platform, sample count and age, last collector error |
| `/api/home` | The Home view: score, next actions, what SysPerf did today |
| `/api/history` | One metric over time (`?key=cpu_pct&hours=24`) |
| `/api/diagnostics` | SysPerf's own errors, workers, probes, footprint, supervisor and deploy history |
| `/api/settings`, `/api/settings/export` | The validated settings form and an export of it |
| `/api/fixes` | Applicable fixes and the CSRF token for this process |
| `/api/trends` | Long-term per-criterion drift, usage profile, load regime. |
| `/api/rca` | Weighted root-cause analysis of the current score. |
| `/api/autoimprove` | Loop status, effectiveness ledger, proposals, cycle log. |
| `/api/learning` | What the loop has learned: per-strategy effectiveness, activity history, knowledge sets, and a live audit of its own operating rules. |
| `/api/actions` | The human action queue — every item the loop cannot do itself, with steps, a reason, and an open/closed status. |
| `POST /api/actions/status` | Record that you took an action, or reopen an item. |
| `/api/evolution` | The self-authoring layer: generation, candidate remediations, generated knowledge shards, learned pairings, structural proposals. |
| `POST /api/evolution/run` | Force an evolution pass now. |
| `POST /api/evolution/promote` | Promote or reject a generated candidate. Promotion requires `confirm=true`. |
| `POST /api/evolution/rollback` | Undo the most recent evolution. |
| `/api/selftest` | This project's own eight quality gates, as last run. |
| `POST /api/selftest/run` | Run the gate suite in the background. |

Every route that changes something is a `POST` that needs the CSRF token.
[SECURITY.md](SECURITY.md#mutating-surface) lists all 28 of them.

---

## How scoring works

**Progress** per criterion is `(current − baseline) ÷ (target − baseline)`,
clamped to 0–100. Reaching the target is always 100%.

Three details make the number trustworthy rather than merely reactive:

1. **Smoothing.** Process, handle and connection counts breathe constantly.
   Scoring the latest sample makes the score collapse on every upward breath, so
   the *sustained* state — a rolling median — is scored instead. Live gauges and
   charts still show the raw value. The window is 5 samples (about 5 minutes);
   remote connections, which burst, use 60 minutes.

2. **Samples from before a gap are ignored.** After a reboot or sleep the
   median uses only samples from the current session, so a stale pre-reboot
   value cannot hold a row at *regress*.

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

## Autonomous improvement

SysPerf can close its own loop. A daemon thread scans every five minutes; when the
overall score is below 80% it runs a root-cause analysis and applies the
remediations that address the cause — then measures the effect on the next scan and
learns which fixes actually work.

Causes are attributed from two horizons: the rolling median against target, and the
multi-week record of daily typicals.

| Cause | Evidence | Response |
|---|---|---|
| `transient_spike` | worse than its own recent daily typical, long-term flat/improving | a burst — cheap reclaim at most |
| `usage_driven` | load above this machine's norm, metric inside its usual band | the measured cost of the work |
| `long_term_regression` | daily typical drifting the wrong way for days | remediate, escalate if the fix is proven useless |
| `chronic_unreachable` | never met in 5+ settled days | propose a target retune; attempt no fix |
| `near_target` | within 5% of target | nothing worth its disruption |
| `steady_shortfall` | stable gap under normal load | needs a structural change |

What the long horizon buys, beyond nicer charts:

- **Preventive action** — a criterion that is *met today* but drifting the wrong
  way is remediated before the score breaches.
- **Measured effectiveness** — every applied fix is verified on the next scan; an
  action with no net benefit over three verified attempts is dropped for that
  metric, and says so.
- **Peak-hour awareness** — user-visible fixes wait until the machine's measured
  busy hours have passed.
- **Escalation, not repetition** — a regression that safe automation cannot close
  becomes a proposal for a human.

What it is allowed to run itself: every remediation declares a risk class, and
only `safe` and `caution` ones can ever execute unattended. Because the service is
deliberately unprivileged, anything needing administrator rights stays `manual` and
is surfaced as an action item with the exact command — see "What it needs you to
do". Two remediations added in 3.0.0 work *without* elevation and so closed the
biggest gap: `background_cpu_hog` moves the measured top consumer to background
CPU and I/O priority (`cpu_pct`, `cpu_queue`, `disk_active_pct`), and
`trim_working_sets` empties the largest application working sets
(`committed_app_mb`, `available_mb`, `compression_mb`). Both exclude kernel and VM
pseudo-processes, security agents, and the measuring shell itself.

Guards: `off` / `observe` / `auto` modes (switchable live, persisted); only `safe`
fixes run unattended (`autoimprove.allow_caution` widens that); `manual` and
session-killing fixes are never executed; execution also requires `fixes.enabled`;
at most two actions per cycle with a 30-minute cooldown each; and the loop never
edits targets or scoring — structural changes are written as proposals.

```jsonc
{
  "fixes":       { "enabled": true },
  "autoimprove": { "mode": "auto", "threshold": 80.0, "interval_seconds": 300 }
}
```

### What it has learned, and what it wants to become

Three layers sit on top of the loop, all on the Auto-Improve tab:

- **Continuous learning.** Every applied fix is scored as a *(fix, metric)* pair —
  the same fix can help one metric and do nothing for another — with a measured
  moving average, a win/loss count and a verdict of `effective`, `learning` or
  `suppressed`. Alongside it: learning activity per day, what each substantive
  cycle actually taught, per-metric knowledge sets, and a live audit of the loop's
  seven operating rules against the retained cycle window. Every number is
  derived from the loop's own ledger and cycle log; nothing is asserted.

- **A human action queue.** Everything the loop may not do becomes a work item
  with ordered steps, the exact command, how to confirm it worked, what should
  change, and a *specific* reason automation is barred — needs elevation, would
  terminate the dashboard, requires knowing which of your apps matter, no safe
  scriptable equivalent, changes persistent configuration, target below the
  machine's floor. Items are `open` until you record `action taken`, or until the
  evidence resolves them; each has a stable identity so the same work is never
  listed twice, and a closed item reopens itself if its trigger returns.

- **Self-authoring evolution.** Every eight cycles the loop converts accumulated
  evidence into capability it was not shipped with: *(fix, metric)* pairings
  learned from measured collateral improvements, candidate remediations for
  metrics nothing currently addresses, generated rule documents, cause→family
  clusters, and structural proposals where no action can help.

  It cannot write commands. Candidates are parameterised from a fixed, reviewed
  recipe library and are inert until you promote them; promotion registers the
  recipe as a `manual` recommendation carrying the exact command, which the
  planner surfaces and skips. Hard caps bound every pass, writes are confined to
  `evolve_state.json` and `evolutions/`, engine code is never modified, and every
  evolution is reversible.

### Its own tests, in the UI

The Auto-Improve tab shows this project's eight quality gates, and can run them:

| Gate | Checks |
|---|---|
| discover | every module is registered with the coverage gate, so none can dodge it |
| compile | every module byte-compiles and carries a docstring |
| lint | AST checks: dead imports, bare `except`, mutable defaults, stray prints |
| unit | the pytest suite |
| coverage | **100% line coverage** on every runtime module |
| frontend | the page's JS parses, every element id it reads exists, every interpolation of a server string is escaped |
| browser | a real Chromium drives every tab, section and control, and **100% of the interface** is exercised |
| live | the running service: status codes, security headers, JSON contracts, CSRF enforcement, GET purity, bounded state files |

The `browser` gate is the one that keeps the others honest about the page. Static
checks can prove the JavaScript parses and escapes; only a browser can prove a panel
actually renders. `tests/uicov.py` measures that reach — every element the page
writes into, every handler, every button, tab and section — and fails the build
below 100%, so a new panel cannot ship untested.

A dashboard that claims to improve itself should be able to show that it has not
regressed.

Endpoints: `/api/trends`, `/api/rca`, `/api/autoimprove`, `/api/learning`,
`/api/actions`, `/api/evolution`, `/api/selftest` and their `POST` counterparts.
Full detail in manual chapter 14; the dashboard's fourth tab shows all of it live.

## AI self-evolution (4.0)

SysPerf 4.0 can improve its own **source code**, not just its thresholds. It
uses a configured LLM to propose a patch, applies it in an isolated git
worktree, and runs tests and benchmarks in a sandbox with no network and a
read-only filesystem. A patch is kept only if the measured win clears both the
threshold and the noise. Kept patches are committed to a review branch; nothing
is merged or deployed without your approval. Since 4.1, **Apply & restart**
deploys an approved patch in one click — the previous version is tagged, the
restarted service must report healthy, and the supervisor rolls the patch back
automatically if it does not (**Roll back** is always one click away).
Since 5.4.6, **Apply winners automatically** (off by default; AI Self-Evolution
tab or Settings) skips the review step: a winner goes straight through the same
Apply path, with the same health check and automatic rollback.

The **autonomy levels** are:

* `off`;
* `observe` (the default);
* `safe_auto` — only low-risk, reversible actions that you enable one by one;
* `self_evolve`.

Security-critical operations are never automated. Without an AI provider,
SysPerf runs exactly as before.

Setup, guarantees and limits are in [`docs/AI_EVOLUTION.md`](docs/AI_EVOLUTION.md).

## What's new in 5.0

| Area | What changed |
|---|---|
| **DNS-rebinding fix** | Requests must name SysPerf in `Host`, and cross-origin POSTs are refused. Before 5.0 a malicious page could reach the mutating API. |
| **Content-Security-Policy** | Only this origin may load scripts, styles or images, or be fetched. |
| **Encrypted API key** | The model API key file is DPAPI-encrypted on Windows. |
| **Honest docs and settings** | SECURITY.md, the README, the manual and the example config describe only what exists. Tests fail if they drift. |
| **Supply chain** | A hash-locked `requirements.lock`, plus `pip-audit`, a CycloneDX SBOM and Dependabot in CI. |
| **Engineering docs** | Architecture, requirements traced to tests, decisions, operations with incident records, and a release checklist. |

## What's new in 4.1

| Area | What changed |
|---|---|
| **Lighter collector** | One PowerShell session is kept warm and reused (≈2.7 s per light sample instead of ≈5 s), with automatic fallback to a fresh process. |
| **Home view** | Score, the three things to do next, what SysPerf did today, service health, notifications and a long-term chart. *Simple view* hides the expert tabs. |
| **Notifications** | An in-app inbox (bell icon) plus Windows notifications for a score drop, an applied fix or tray quit, a consent request, an AI patch to review and a service problem — deduplicated, with quiet hours. |
| **Diagnostics** | Settings › Diagnostics shows recent warnings and errors, every worker and when it last produced data, SysPerf's own CPU and memory, and supervisor and deployment history. The runtime log is rotated. |
| **Safer tray auto-quit** | Per-app consent (*Ask* until you choose *Allow* or *Never*), a busy guard (calls, downloads and syncs are left alone) and *Reopen* to undo a quit. |
| **One-click deploy** | *Apply & restart* for AI patches, verified after restart, rolled back automatically if unhealthy. |
| **Self-healing** | The sign-in task runs a supervisor that restarts SysPerf on crash, hang, stalled sampling or sleep/resume. |
| **Settings tab** | Every setting in one validated form, with export, import and reset. `sample_seconds` and `autoimprove.*` in the settings file are honoured (they were documented but ignored). |
| **SQLite history** | Raw samples for 14 days and hourly summaries for over a year, instead of a few hours of JSONL. The old file is migrated once. |
| **Better evolution evidence** | More benchmarks and cProfile hotspots measured in the sandbox, so the AI targets real hot code. |

## Configuration

Everything tunable lives in one file. Find it with `sysperf --where`; on Windows
it is `%LOCALAPPDATA%\SysPerf\sysperf.config.json`. See
[`sysperf.config.example.json`](sysperf.config.example.json) for the annotated
version. Omitted keys keep their defaults. Restart SysPerf after editing.

The keys worth knowing about:

| Key | Default | Effect |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `5056` | Bind address. Loopback only: there is no authentication. |
| `allowed_hosts` | `[]` | Extra Host names to answer to (e.g. a local reverse proxy). Any other Host gets 403. |
| `sample_seconds` | `60` | Fast-tier cadence, clamped to 30–600 (honoured since 4.1; also on the Settings tab). |
| `autoimprove.threshold` / `.interval_seconds` / `.mode` | `80` / `300` / `auto` | The agent loop (honoured since 4.1). |
| `history.raw_days` | `14` | Raw samples kept in SQLite; hourly summaries are kept for over a year. |
| `collector.persistent_shell` | `true` | Reuse one PowerShell process for every sample. |
| `targets.*` | see example | Fractions of *your* capacity used to derive targets. Raise to loosen, lower to tighten. |
| `browser_hosts` | common apps | Which applications' embedded-browser processes count as unclosable or in-use. **Worth editing** — an unlisted app is treated as discretionary and inflates what SysPerf expects you to reduce. |

Environment overrides (they beat the file): `SYSPERF_DATA_DIR`, `SYSPERF_CONFIG`,
`PERFDASH_HOST`, `PERFDASH_PORT`, `PERFDASH_ALLOWED_HOSTS`, `PERFDASH_SAMPLE_SECONDS`,
`PERFDASH_FIX_ENABLED`, `PERFDASH_AUTO_MODE`, `SYSPERF_PS_EXE`. Every key in
[`sysperf.config.example.json`](sysperf.config.example.json) is one the code reads.

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

SysPerf is a local web server with no login that can change your PC when you
let it. Its defences assume the attacker is another web page in your browser:

- **Loopback only.** It binds `127.0.0.1`. Binding to a network is not supported.
- **Host and Origin checks.** Requests that do not name SysPerf in `Host`, and
  POSTs from another origin, get 403. This blocks DNS rebinding.
- **CSRF token on every change.** All 28 POST routes require a per-process token.
- **Strict response headers.** `Content-Security-Policy` (this origin only),
  `nosniff`, `X-Frame-Options: SAMEORIGIN`, `no-referrer`, `no-store`,
  `Permissions-Policy` (no camera, microphone, location or USB) and
  `Cross-Origin-Opener-Policy: same-origin`.
- **No shell from HTTP.** The collector path is fixed in code, and App Control
  accepts only allow-listed process names.
- **System changes are listed and reversible.** Automatic fixes, the Explorer
  watchdog, tray auto-quit and the OneDrive policy each have a switch and say how
  to undo them: on Home in the Store build (where they start off), and on the
  App Control, Auto-Improve and AI Self-Evolution tabs in a scripted install.
  `fixes.enabled: false` turns off everything that changes the PC.
- **Outbound traffic:** only the daily version check (public release feeds)
  and, if you enable self-evolution, the model provider you configure.
- **Secrets:** the API key file is DPAPI-encrypted on Windows (`0600`
  elsewhere) and never logged or returned.

Details, the full mutating-surface table and how to report a vulnerability are
in [SECURITY.md](SECURITY.md).

---

## Architecture

One Python process (Flask on waitress) serves one self-contained page.
Background workers measure the PC (`collector` → `pwsh_session` →
`collect_win.ps1`, plus `livestats` for Win32 values), score it (`scoring`,
`adaptive`, `machine_profile`), act within limits (`autoimprove`, `fixes`,
`optactions`, `procwatch`, `trayquit`), and record what happened (`historydb`,
`evodb`, `notify`). A supervisor process restarts it and rolls back unhealthy
patches.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the module map, data flow,
trust boundaries, state files and failure modes. Decisions behind the design
are in [docs/DECISIONS.md](docs/DECISIONS.md).

All state lives in the data directory (`sysperf --where`), never in the install
directory.

---

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install pytest==9.1.1 ruff==0.6.9 markdown==3.11.2
.venv/bin/python -m pytest -rs        # about 1,100 tests; Windows-only ones skip elsewhere
.venv/bin/python -m ruff check src tests
```

Above the collector boundary, everything is tested with synthetic samples, so the
suite runs on Linux, macOS or CI. Windows-only tests exercise the real Win32,
DPAPI and PowerShell paths. The in-app Self-test tab runs the same eight gates.
Before a release, follow [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). The rules
that are not negotiable: anything that changes the PC must be reversible,
opt-in or individually enabled, shown in the app with how to undo it, and
covered by tests. No feature may show a value that is not measured.

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

**Slow samples.** The full sweep (every fifth sample) enumerates services and
drivers and takes a few seconds. Raise `sample_seconds` to sample less often.

**Port already in use.** `install.ps1 -Port 5100`, or set `port` in the config.

**Python not found even after installing it.** Open a new terminal so the updated
`PATH` is picked up, then re-run the installer.

---

## FAQ

**Does it slow my machine down?** A fast sample takes about 2–3 seconds in a warm
PowerShell session, once a minute by default; the full sweep costs a few seconds
more every fifth sample. Settings › Diagnostics shows SysPerf's own CPU and memory
use, so you can check. The API caches its snapshot so repeated polling is nearly free.

**Does it send anything anywhere?** Only two things, and only what they need.
The daily version check reads public release feeds, and sends nothing about your
PC. If you turn on AI self-evolution, source excerpts and metrics go to the model
provider you configured. Nothing else leaves the PC.

**Does it change my PC?** Only in ways it lists. Automatic fixes (DNS flush,
memory trims), the Explorer watchdog, tray auto-quit and the OneDrive policy are
each have a switch and say how to undo them (Home → Optional features in the
Store build; App Control, Auto-Improve and AI Self-Evolution tabs otherwise).
Autonomy starts at *Observe*, and the
Store build starts with every one of them off. Set `fixes.enabled` to `false` and
SysPerf only measures and recommends.

**Can I run it on a server or a fleet?** It is built for a single desktop. Nothing
prevents multiple instances, but there is no aggregation layer. The JSON API is
stable enough to scrape if you want to build one.

**Does it need my browser open?** No. Collection happens in the background service;
the dashboard is just a view of it. `sysperf --report` needs no browser at all.

---

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
