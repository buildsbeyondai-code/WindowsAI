# Changelog

All notable changes to SysPerf are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 5.0.1 — 2026-10-26

A packaging and CI release; the app itself is unchanged.

### Added
- **Native Arm64 Store package.** Partner Center recommends Arm64 (AArch64)
  over 32-bit Arm. `build-msix.ps1` now builds for the build Python's
  architecture (`-Arch x64|arm64` checks it), the manifest takes it from
  `{{ARCH}}`, and the new **MSIX** workflow builds `SysPerf_<ver>_x64.msix`
  and `SysPerf_<ver>_arm64.msix` (on `windows-11-arm`) with the Store
  identity, checking each package's manifest and `SysPerf.exe` machine type.

### Fixed
- **CI on Python 3.10.** `markdown==3.11.2` needs Python 3.11, so both 3.10
  test jobs failed at install. 3.10 now gets `markdown==3.10.3`.

### Removed
- `test_e2e_playwright.py::test_no_console_errors_on_summary`, a subset of
  `test_e2e_ui.py::test_no_console_errors_anywhere_in_the_dashboard`.

## 5.0.0 — 2026-10-25

A security and engineering-quality release from a UAISES v1.0 audit. It is a
major version because requests to an unknown host name are now refused, which
can break a reverse-proxy setup until its name is added to `allowed_hosts`.
A normal desktop install behaves exactly as before.

### Security
- **DNS rebinding closed** (`hostguard.py`). Every request must name SysPerf
  in its `Host` header (loopback names, the bind address or
  `allowed_hosts` / `PERFDASH_ALLOWED_HOSTS`). POSTs with a foreign `Origin`
  are refused. Before this, a page that rebound its hostname to 127.0.0.1
  could read the CSRF token from `/api/fixes` and call any POST route.
- **Content-Security-Policy** on every response: only this origin may load
  scripts, styles, images or be fetched.
- **AI API key encrypted with DPAPI on Windows.** A plaintext key file from
  4.x is encrypted in place on first read.
- **SECURITY.md rewritten from the route table.** It lists all 28 POST
  routes and the system changes each feature makes. It no longer claims an
  `anonymize` option or an off-loopback token, neither of which existed.
  `tests/test_security_doc.py` keeps it in step.

### Added
- `requirements.lock` (hash-locked, all platforms), and a CI `supply-chain`
  job: lock freshness, `pip-audit`, CycloneDX SBOM. Dependabot config.
- `docs/ARCHITECTURE.md`, `DECISIONS.md`, `REQUIREMENTS.md` (acceptance
  criteria traced to tests), `OPERATIONS.md` (objectives, incident response,
  root-cause records), `RELEASE_CHECKLIST.md`, `PROJECT_STATE.md`; `CLAUDE.md`.
- Tests: Host/Origin guard; CSRF on every POST route; requirements
  traceability; accessibility (WCAG AA contrast, control names, focus); heap
  soak; live values against an independent CIM reading (Windows); real DPAPI
  (Windows). `tools/soak.py` watches a running service for hours.

### Changed
- The runtime log keeps the first three identical warnings per 10 minutes
  and reports how many it dropped (`health.RepeatFilter`).
- All POST routes use the single `_csrf()` helper.
- The self-evolution card now says plainly that a patch reaches the running
  app only through **Apply & restart**.
- Python 3.10 is the stated floor everywhere (install-wsl.sh accepted 3.9).
- Classifier `Development Status :: 4 - Beta` until the packaging gate
  (clean-machine MSIX upgrade) has been verified.
- Dev dependency `markdown` 3.7 → 3.11.2 (PYSEC-2026-89).

### Fixed
- **Memory leak on Windows.** Every `/api/diagnostics` call leaked about 1 KB
  (ctypes types rebuilt per call), and tray-quit did the same every 15 s.
  The new soak test found it.
- The in-app frontend gate looked for Node with `which`, so it could never pass
  on Windows. It now uses `shutil.which`, with Playwright's bundled Node as a
  fallback.
- `autoimprove.allow_caution`, `max_actions`, `cooldown_seconds` and
  `settle_seconds` in the settings file were ignored; the *Keep winning
  patches for review* switch did nothing. Both now work.
- Self-evolution's sandboxed baseline was red on Windows checkouts: a CRLF test
  helper failed with "nothing to commit". The suite now passes inside the
  real sandbox (1,169 passed).
- The tray auto-quit "add app" field had no accessible name.
- The rendered HTML manual was four days behind `USER_MANUAL.md`.

### Known limitations
- MSIX install, upgrade from 4.1.0 and uninstall passed on the developer PC
  (test-signed copies), not yet on a clean machine. With a scripted install
  present, the Store app shares its data folder (see PRIVACY.md).
- `tools/soak.py` has not yet completed a 24-hour run on this release.
- Metrics are polled every 60 s, not event-driven (see DECISIONS.md ADR-005).
- There is no authentication. Binding off loopback remains unsupported.

## 4.1.0 — 2026-10-25

### Added
- **Warm PowerShell session** (`pwsh_session.py`): the collector reuses one
  PowerShell process for every sample (measured 2.7 s vs 5.0 s for a light
  sample), recycles it after 500 runs, and falls back to a one-shot process for
  that sample if the session fails.
- **Home view**: score ring, the next three actions (including consent requests
  and patches to review), what the agent did today, service health, the latest
  notifications and a long-term chart; *Simple view* hides the expert tabs.
- **Notifications** (`notify.py`): inbox (`/api/notifications`, bell icon) and
  Windows toasts for score drops, applied fixes and tray quits, consent requests,
  patches to review and service problems; deduplicated per key, quiet hours,
  per-kind switches. Toast text is passed through the environment, never a
  command line.
- **Diagnostics** (`health.py`, `/api/diagnostics`): recent warnings/errors,
  worker liveness and freshness probes, SysPerf's own CPU and memory, collector
  and history statistics, supervisor and deployment history; runtime log rotation.
- **Supervisor** (`supervisor.py`, `--supervise`): the sign-in task now runs the
  server as a child and restarts it on crash (with back-off), unanswered health
  checks, stalled sampling and sleep/resume; it rolls back a just-applied patch
  that never becomes healthy.
- **One-click deploy** for AI patches: *Apply & restart*, post-restart
  confirmation, automatic and manual rollback (`/api/codevolve/apply`,
  `/api/codevolve/rollback`, `/api/restart`).
- **Settings tab** (`settings_hub.py`, `/api/settings`): one validated form for
  general, autonomy, evolution, notification and tray settings, with export,
  import and reset.
- **SQLite history** (`historydb.py`): raw samples for 14 days, hourly rollups for
  400 days, `/api/history` for long-range charts; `history.jsonl` is migrated once.
- **Evolution evidence**: cProfile hotspots from the sandbox, more benchmarks
  (`appctl.top_apps`), benchmarks named after the code they run.

### Changed
- **Tray auto-quit** asks before quitting an app (*Ask* / *Allow* / *Never*),
  leaves busy apps alone and records the executable so a quit can be undone with
  *Reopen*.
- **Live Architecture** redrawn as three live layers (sense & serve, learn &
  remember, act & evolve) including history, health, notifications, the action
  policy, code evolution, the supervisor and the audit trail; the stale nginx
  reference is gone.
- Home is the default view; the CLI opens `#home`.

### Fixed
- `sample_seconds` and `autoimprove.threshold/interval_seconds/mode` in the
  settings file are now honoured (clamped to safe ranges); they were documented
  and written by the installer but ignored. The installer now writes 60 s.
- Unattended action runs reported by the fix engine as `{"status": "ok"}` were
  recorded as failures, so before/after effectiveness was never measured.
- A data directory copied from another computer is recognised from the SQLite
  history too, and the new state files are archived with the rest.
- `refresh_components` no longer configures logging (and creates a log file in
  the package directory) when merely imported.
- The bwrap sandbox now binds the interpreter's prefix, so a venv under a hidden
  directory can run; the baseline suite is green inside the jail.
- Coverage now traces worker threads; every runtime module is at 100% line
  coverage, and the browser suite reaches 100% of the interface.

## 4.0.0 — 2026-10-21

### Added
- **Source-code self-evolution** (`codevolve.py`). This changes code, not
  thresholds. Each cycle measures the baseline, asks the configured LLM for a
  structured find/replace patch, and applies it in a fresh git worktree of an
  approved repository. It then parses the patch, security-scans it, runs the
  test suite and the repository's `evo_bench.py` benchmarks in a sandbox, and
  decides from the evidence. Retained candidates are committed to
  `sysperf-evolve/<id>` branches for review and never merged automatically.
  Tests, benchmarks, CI, packaging and the evolution, LLM and policy modules are
  protected. Engine modules need a ≥ 20 % win and explicit approval.
- **Sandboxed validation.**
  - bubblewrap: no network, read-only filesystem, home and `/mnt` hidden,
    environment cleared, rlimits, timeouts.
  - Or a `--network none --read-only --cap-drop ALL` container.
  - With neither available, executable validation is blocked rather than run
    without isolation.
- **Runtime LLM integration** (`llm.py`).
  - Providers: Anthropic Messages API (stdlib HTTPS) and headless `kiro-cli`
    with no tools trusted.
  - The model id comes from settings and from the provider's model list; none
    is built in.
  - Timeouts, bounded retries with exponential backoff and jitter, 429
    `retry-after` handling, local rate limiting, and daily token and cost
    budgets.
  - Token and cost accounting, using prices you configure.
  - Schema-validated JSON output.
  - API keys are read from the environment or a 0600 key file, are never
    logged (a redacting log filter enforces this), and are never returned.
- **Independent evolution scheduler.**
  - Configurable interval (default 6 h) and a daily cap.
  - Manual, scheduled and event triggers, with a cooldown.
  - Minimum-evidence rule, budget gate and persistent state.
  - Recovery of interrupted cycles, and an explicit reason whenever nothing
    runs.
- **Structured action framework and autonomy levels** (`optactions.py`).
  - Levels: `off`, `observe` (the default), `safe_auto`, `self_evolve`.
  - Every remediation has its preconditions, risk, permission, expected
    benefit, validation and rollback declared.
  - Only low-risk, reversible, individually enabled actions can run unattended.
  - Security-critical operations are permanently refused.
  - Each run's before/after metric is recorded and judged after a settle time.
- **Durable learning store** (`evodb.py`, SQLite). Versioned migrations,
  retention limits, WAL, quarantine of a corrupt database, and a SHA-256
  hash-chained audit log.
- **AI Self-Evolution dashboard tab**:
  - status (monitoring → analyzing → planning → generating → testing →
    benchmarking → awaiting approval → completed/failed/blocked);
  - last and next attempt, candidate counts;
  - AI errors, token usage and estimated cost;
  - per-cycle history (trigger, problem, model, proposal, files, tests,
    security, decision, reason);
  - a diff and benchmark viewer, with Approve/Reject;
  - autonomy and evolution settings, provider/model/key/budget controls;
  - the per-action unattended opt-in;
  - a measured effectiveness table and the audit trail.
- `tools/evolution_demo.py`, `tests/fixtures/evo_demo`, `tools/validate-windows.ps1`,
  `docs/AI_EVOLUTION.md` (AI configuration, security architecture, Windows validation).
- 64 new tests: `test_llm.py`, `test_codevolve.py` (real sandboxed cycles),
  `test_optactions_evodb.py`, and 2 browser tests for the new tab.

### Changed
- Unattended execution goes through the 4.0 action policy as well as Auto-Improve's
  own risk gate. A 3.x install whose user had chosen *auto* migrates to `safe_auto`
  with only `flush_dns`, `trim_working_sets` and `clear_standby` enabled.
  `clear_temp` (irreversible deletion) and every persistent or session-disruptive
  action now need confirmation. The migration is audited.
- `tests/covrun.py` registers `target_review`, `refresh_components` and the four
  4.0 modules, so the *discover* gate covers every shipped module again.

### Fixed
- **CI:** the Windows collector smoke test pointed at
  `src/sysperf/scripts/collect_win.ps1`, which never existed. It now points at
  `src/sysperf/collect_win.ps1`. CI also gained:
  - `pip check` and a `compileall` step;
  - a real bubblewrap backend on Linux, so sandboxed evolution tests run rather
    than skip;
  - a check that the installed wheel contains the UI, the collector and the
    new modules;
  - the Windows validation script.
- The 3.x evolution engine's generated recipes were advisory text only
  (`manual_hint`, no executable handler). That is unchanged by design — recipes
  are still never executed. Executable remediations are now exclusively the
  registered `ActionSpec`s, and source improvements go through `codevolve`.

## 3.2.0 — 2026-10-21

### Added
- **Containerised deployment** (`container/`, optional, WSL 2 + Docker). The
  web/API tier runs in an unprivileged container — `CapDrop: ALL`, default
  seccomp, read-only root, `no-new-privileges`, non-root, 1 CPU / 512 MB / 256
  pids, published on loopback only — with the base image pinned by digest.
  `install-container.sh` seeds state, renders the broker policy, installs the
  broker unit, builds and starts it.
- **Host-exec broker** (`container/broker/`). Running the collector inside a
  container needs `CAP_SYS_ADMIN` plus `seccomp=unconfined` (measured) — host
  root in all but name. Instead, one host process on a unix socket executes only
  allowlisted programs, confines `-File` to the distribution/data roots, rewrites
  container paths (`/app`, `/data`, `/dist`) to host paths and logs every call.
  `-Command` passthrough (needed by remediations) can be switched off for a
  read-only dashboard. The in-container client is installed under the program
  names the app already calls, so no application code knows the broker exists;
  it forwards stdin (otherwise the frontend gate's `node --check -` silently
  passed on empty input).
- **Daily component-catalog refresh** (`refresh_components.py`) with a systemd
  timer: re-assesses each component's `latest` target from its supported channel
  (apt, PyPI, nodejs.org, GitHub) and stamps the assessment date, so the
  upgrade advisory stops going stale. `SYSPERF_COMPONENTS_DIR` chooses which
  catalog it writes.
- `PERFDASH_SELFTEST_ROOT`: where the integrated test suite lives and runs, for
  deployments whose code sits on a read-only layer.

### Changed
- Component catalog re-assessed 2026-10-21: PowerShell target 7.6.6 -> 7.6.7,
  Docker 29.8.2 -> 29.9.1.

### Fixed
- `test_main` / `test_main_guard` only passed when waitress was *absent*: they
  asserted the Flask dev server ran and patched `threading.Thread` module-wide,
  which broke inside waitress once it was installed. They now stub whichever
  server `serve()` selects.
- START-HERE.txt still announced 3.0.0 in its banner.

## 3.1.0 — 2026-10-05

### Added
- **Automatic target realism** (`target_review.py`): the loop now decides whether
  each goal is realistic from your own telemetry and never asks. A goal met on at
  least one settled day is kept; a goal never met across the window is auto-revised
  to the proven-achievable value via a reversible runtime override (CRITERIA is
  never rewritten). Surfaced read-only as "Automatic goal decisions".
- **Launch & boot time tracking**: boot, WSL cold-start and Explorer/Outlook/Word
  cold-starts are collected from the admin-only Diagnostics-Performance log by an
  elevated helper (`standby_broker.ps1`) that publishes `launch_times.json`; the
  collector merges it. New **Launch times** sub-tab and a **Launch & boot times**
  Summary category, with the remediation shown per row. Interactive app launches
  (Word/Excel/PowerPoint) are timed live by the broker (start -> first window),
  since the boot-phase log never records a manually opened app.
- **On-demand standby auto-clear**: the memory-standby list is emptied only when
  memory scores are low, through the elevated broker (no scheduled timer).
- **Daily app-reset maintenance** (`daily-app-reset.ps1`): an idle-only scheduled
  reset of the heavy WebView2 apps that flattens the long-term handle/memory drift.
- **Sticky sub-tab navigation** on Auto-Improve (Details-style pills, Strategy
  performance first), pinned below the main tabs.

### Changed
- **Evolution auto-adoption**: self-authored remediations are decided from evidence
  (adopted only when they fill a real gap with a reversible, low-risk action) and
  are no longer surfaced to the user as manual Promote/Reject reviews.
- **Enterprise KPI hero**: colour-coded per-metric tiles, status-aware health and
  trend, lighter labels (bold reserved for the numbers).
- **"Hide descriptions"** now strips the explanatory prose across the tab, leaving
  a clean, self-explanatory view; each action item shows a plain-language
  "What I need from you" line.
- `committed_app_mb` goal honestly revised 9,000 → 9,600 (the measured lean floor;
  9,000 was never reached in 14 days).

### Fixed
- Health-score tile wrongly showed "below target" when the live score was above the
  threshold (it keyed off the last cycle's per-metric breach flag, not the score).
- "Next scan" showed a misleading "0 min" for sub-minute countdowns; now seconds-aware.
- Auto-Improve tab polled slower (30 s) than the rest of the dashboard; now 20 s.
- Launch-time collection: moved the merge out of the best-effort inventory block,
  which can throw early in restricted environments and skipped everything after it.

## 3.0.0 — 2026-09-29 (the loop can finally act on what actually breaks)

A major version because the autonomous loop's reach changed, not just its
presentation. Until now three of the metrics that drove most of the machine's
breaches had **no remediation at all**, and three more had only a remedy requiring
administrator rights — which the service deliberately does not have. The loop was
choosing correctly every cycle and had almost nothing it was permitted to try. This
release doubles the set of remediations it can learn from, and rebuilds the App
Control tab around the controls rather than the tables.

### Added
- **Two unprivileged remediations.** Both work without elevation, which is the
  whole point: `clear_standby` has always had to stay manual because RAMMap needs
  admin, so the metrics it covered could never be closed automatically.

  | Fix | Mechanism | Addresses |
  |---|---|---|
  | `background_cpu_hog` | `SetPriorityClass(PROCESS_MODE_BACKGROUND_BEGIN)` — lowers CPU **and** I/O priority for the measured top consumer | `cpu_pct`, `cpu_queue`, `disk_active_pct` |
  | `trim_working_sets` | `EmptyWorkingSet` on the five largest application working sets over 200 MB | `committed_app_mb`, `available_mb`, `compression_mb` |

  Both are `caution` (user-visible), reversible, non-session-killing, and bounded.
  **Learnable (fix, metric) pairs rise from 5 to 11**; metrics with no remediation
  fall from 5 to 2 — only `browser_procs` and `hw_faults` remain, and both
  genuinely need a human.

  Verified on a live instance: the loop applied both on its own next cycle,
  measured the effect after the settle delay, and the strategy count went
  **3 → 7**.
- **A status strip on App Control** — four tiles (Total CPU, WSL memory, OneDrive,
  Explorer watchdog), each colour-keyed by state and linking to the control that
  acts on it. The four facts come from four different endpoints, so each refresher
  writes into a shared slice-store rather than the strip guessing from one payload.

### Changed
- **App Control rebuilt around its controls.** It used to open on a wide
  eight-column table and put a twelve-row informational process list second, which
  pushed every actual control below the fold. Now: status strip, then the
  controllable applications, then the three controls, with the full process list as
  a collapsed disclosure carrying a live count (`12 processes · 4 over 10%`) on its
  header. Page height drops from 1,905 px to 1,584 px with nothing removed.
- **One action per application row, not two.** Five rows previously rendered ten
  buttons, half of them disabled — a disabled "Start" beside an enabled "Stop"
  carries no information. Each row now shows the one action that applies, or states
  why there is none (`protected (system)`, `no launcher seen`, `execution
  disabled`).
- **Application rows replace the table.** Name and state read first; working set
  and median sit beneath as secondary detail; the bar shows share-of-window with
  the peak marked as a tick. Eight columns spread across 1,400 px became one
  scannable row that reflows on narrow screens.
- **The three controls (WSL, OneDrive, Explorer) render identically.** They are the
  same shape — state pills, one-line summary, actions, explanation — and were built
  three different ways. Their long explanations moved into disclosures, cutting the
  tab's prose by roughly half.
- The shared snapshot fixtures now include `cpu_pct`, `cpu_queue` and
  `disk_active_pct`, so the "every fix is applicable in a fully-degraded snapshot"
  test still means what it says.

### Fixed
- **The new CPU remediation would have deprioritised its own PowerShell process.**
  A dry run of the selection logic before anything was allowed to execute showed
  `pwsh` as the top consumer — the measuring process is the busiest process during
  its own three-second sample. It now excludes its own process tree by PID *and*
  parent PID, because a renamed or repackaged shell would slip past a name list.
- **The memory remediation would have trimmed `Memory Compression` and
  `vmmemWSL`.** The first is a kernel component and the very metric being
  optimised; the second is the VM this service runs inside. `appctl.CRITICAL`
  answers "may I stop this?", which is not the same question as "may I
  deprioritise or trim this?", so a documented `_EXTRA_DENY` now covers kernel and
  VM pseudo-processes and the measuring shell. The shared list is still imported
  wholesale rather than restated, so it cannot drift.
- **The App Control bar track stayed near-black on a white page.** The override was
  written as `body.light`, but light is the default with no class — only `dark` is
  added. Corrected to `body:not(.dark)`.
- **The Explorer watchdog tile counted CPU samples, not restarts** (`recent`
  instead of `action_count`), so it read "0 restart(s)" beside a card saying 3.
- **"Total CPU usage: Bad (typical 99% vs target 25%, now 99%)" duplicated the new
  tile exactly.** Removed, along with the now-dead render function — which the
  frontend gate caught immediately, because it still read an element that no longer
  existed.
- The OneDrive sync time was split across two lines for no reason.

### Fixed — the test suite ran the browser tests twice
- **`covrun` executed the Playwright suites under `sys.settrace`.** The coverage
  tracer ran the *entire* tests directory, so the 47 browser tests were driven once
  with line tracing active (10-50x slower per line) and then again by the `browser`
  gate. They exercise no runtime module — they drive Chromium against static HTML —
  so they contributed nothing to line coverage while roughly doubling the suite's
  wall time. The coverage run now excludes `test_e2e*.py` and the browser gate owns
  them: **unit + coverage went from 155 s to 5.9 s** on an idle machine, with
  coverage unchanged at 100% (4,698/4,698), which is itself the proof they added
  nothing.
- **The browser harness slept a fixed 1.2 s after every page load.** Across 47
  tests that was about a minute on its own, and it was simultaneously too long on a
  fast machine and too short on a loaded one. It now waits for the first render to
  complete. Two tests were relying on that blanket sleep rather than on what they
  assert — the summary cards and the once-per-second refresh countdown — and now
  wait for those explicitly.

### Tests
- 661 unit tests, **100% line coverage on all 25 runtime modules**.
- 47 browser tests, **100% UI coverage on all 160 measured elements** — the gate
  caught all three new App Control elements as unexercised before they were
  covered.
- 11 new tests for the remediations: both detectors, the deny-list composition
  (asserting `appctl.CRITICAL` is included wholesale so it cannot drift), the
  self-exclusion, the interval differencing, and the bounds.
- Eight gates green in the development tree and from a clean extraction.

### Notes on reading the new strategies
The first measurements are noisy by nature. A `+49%` effect on `cpu_pct` is one
attempt against a spike and the moving average will regress toward the truth. Both
fixes also ran in the same cycle, so each is currently credited with the other's
measured delta — a pre-existing property of attributing a cycle's outcome to every
action in it, which separates as they run independently. Early evidence also
suggests `trim_working_sets` *raises* `compression_mb` (trimmed pages move to the
standby list, where Windows compresses some), so the loop may well learn to keep it
for `available_mb` and suppress it for `compression_mb` — which is exactly why
scoring is per-(fix, metric) rather than per-fix.

## 2.6.0 — 2026-09-24 (WSL reclaim control + network target correction)

Synchronised the distribution with the live dashboard again. Adds a one-click
WSL memory-reclaim control and corrects an unreachable network scoring target.
(Supersedes the mistagged 2.5.0 archive, whose payload was still 2.4.0.)

### Added
- **WSL shutdown control** (App Control tab) — a dedicated "WSL memory
  (vmmemWSL)" card that shows the live vmmemWSL footprint and a confirmed,
  session-killing "Shut down WSL" button that reclaims the entire WSL2 VM memory
  back to Windows. Because the dashboard runs *inside* WSL, the shutdown is
  launched as a detached, window-hidden Windows process after a short delay so
  the HTTP response returns before the VM stops; the stack returns on the next
  WSL start.
- **`appctl.wsl_shutdown()`** and the `POST /api/appctl` action
  `"wsl-shutdown"` — CSRF-protected and gated by the execution kill switch, and
  never invoked by the Auto-Improve loop (session-killing actions stay manual).
- Tests: `wsl_shutdown` unit tests (detached/delayed command shape, error rc,
  delay clamp) and `POST /api/appctl` route tests (ok, missing-token,
  execution-disabled).

### Changed
- **`net_remote` target corrected 40/30 → 55/40.** 14-day telemetry
  (6,899 samples, mean 32, range 19–42) showed the ≤30 target sat at/below the
  mandatory cloud-app connection floor (Teams + new Outlook + OneDrive +
  Edge/WebView2 + VPN + ChatGPT), pinning the row at 0% (regress) with no
  achievable path up. Target 40 is the realistic floor; baseline 55 reflects a
  heavy multi-cloud session (observed peak ~57), giving non-collapsing headroom.
  Same unreachable-target correction pattern as `browser_procs` (45→72) and
  `handles`. Lifts the Network optimisation category from 50% to ~93%.
- Frontend synced with the live dashboard (App Control WSL card, minor UI
  refinements shipped since 2.4.0).

### Fixed
- **"CPU & processes" category flickering to 0.** The Summary category was scored
  on a single instantaneous `cpu_pct` sample, so it dropped to 0 whenever a reading
  landed on a transient 90–100% spike even though sustained CPU was ~38%. It now
  uses the smoothed rolling value (`cpu_pct` row `scored_typical`), falling back to
  the live reading until a typical exists — matching the "rolling median, not one
  sample" principle the rest of the scorecard uses.
- **OneDrive lock policy under-triggering on Windows 11.** Lock state was detected
  only from `LogonUI.exe`, which on Win 11 appears only at the password prompt, not
  when the lock screen (`LockApp.exe`) is showing — so locking and walking away never
  started OneDrive. Detection now uses `LogonUI` **or** `LockApp`.
- **OneDrive last-sync time off by the UTC offset.** The epoch was derived with a
  Local-kind base date, dropping the timezone offset; now sourced from OneDrive's own
  per-account completion timestamps in the correct frame.

### Added (OneDrive in App Control)
- **OneDrive card in the App Control tab** — live run state, session lock state,
  lock-policy status, Stop/Start buttons, and a **"Last completed sync"** age. The
  existing OneDrive control in the Auto-Improve tab is left unchanged.
- **`onedrive_last_sync`** (collector) — the most recent of OneDrive's per-account
  `LastDAUEventTimestamp_FindChanges` / `_Upload` / `_Download` registry values, i.e.
  the last *completed* sync operation rather than incidental disk activity.
- `GET /api/onedrive` now also returns `last_sync`, `session_locked`, and `lock_policy`.

## 2.4.0 — 2026-09-22 (dashboard sync)

Synchronised the distribution with the live dashboard. The package now mirrors
the running product exactly.

### Added
- **Consistent CPU consumers** (`cpuwatch`) — differences per-process cumulative
  CPU across samples to surface the apps that use CPU consistently, with current /
  median / peak usage. Shown in the Auto-Improve tab.
- **App Control tab** (`appctl`) — start/stop the top-5 CPU applications, with a
  server-side allowlist (top-N only) and a hard denylist of critical system /
  security processes; a red/green Total-CPU indicator; Start relaunches the
  observed executable (known launcher for OneDrive).
- **OneDrive control** (`onedrive`) — start/stop from the UI, stopped by default
  at start-up.
- **Lock-state policy** (`session_policy`) — OneDrive runs while the workstation
  is locked and stops while unlocked (edge-triggered; respects manual control).
- Collector now emits `cpu_procs` (top-50 by CPU with pid / cpu-seconds / working
  set / executable path), `ncpu`, `onedrive_running`, and `session_locked`.
- New mutating endpoints (CSRF-protected, honour the execution kill switch):
  `POST /api/onedrive`, `POST /api/appctl`; `GET /api/onedrive`, `GET /api/appctl`;
  `cpu_consumers` added to `GET /api/autoimprove`.

### Changed
- UI redesign: Apple-style theme with a light default and a **Dark mode** toggle,
  responsive 16:9-friendly density, refined header, plain-checkbox controls, and a
  subtle author watermark. "Self-improving" branding throughout.

## 2.4.0 — 2026-09-20

### Changed — Wi-Fi is scored per network, not against an absolute rate ladder

The Wi-Fi connectivity category used a fixed Mbps ladder (1 Gbps for full marks),
which scored the *internet plan* rather than the Wi-Fi link. On a 100 Mbps
connection a 130 Mbps link already carries nearly everything the line can deliver,
yet the ladder rated it under half marks and there was no way to ever reach 100%
without gigabit Wi-Fi. The category now answers the question that matters — *is the
radio the bottleneck for this network?*

- **`sysperf.netopt.score_wifi_link()`** — scores the associated link against the
  capacity of the network it is attached to, keyed by SSID:
  - where the network's capacity is declared (`wifi.plan_mbps`), usable throughput
    (negotiated PHY x `wifi.phy_efficiency`) is measured against the subscribed
    speed and capped at 100 once the link carries the whole plan;
  - any undeclared network calibrates against its own learned capability
    (`wifi.capability_pctl`, p90 of its sustained rates), so roaming to a faster
    network no longer needs configuration;
  - a network with fewer than `wifi.min_capability_samples` samples reports
    `calibrating` instead of a shaky score.
- **Samples are scoped to the current SSID** and never pooled across networks.
  Previously a machine that roamed between, say, a 100 Mbps home line and a
  600 Mbps office link would blend both into one meaningless figure.
- **The negotiated rate is smoothed** over `wifi.smooth_window` samples. Rate
  adaptation renegotiates continuously (band, channel width, MCS, spatial
  streams), so the category was previously jumping between score bands on
  whichever instantaneous reading happened to land. This brings Wi-Fi under the
  same rolling-median rule as every other criterion. The live `link_mbps` shown in
  the Details view is unchanged.
- **New config block `wifi`** (`plan_mbps`, `phy_efficiency`, `capability_pctl`,
  `min_capability_samples`, `smooth_window`), documented in
  `sysperf.config.example.json`. Declaring your plan is a one-line change:
  `"wifi": {"plan_mbps": {"MyNetwork": 100}}`.
- The Summary card now states its own reasoning, e.g. *"~78 of 100 Mbps plan usable
  on MyNetwork"* or *"613 of 907 Mbps this network has reached"*.

## 2.3.0 — 2026-09-20

### Added — deep-linkable dashboard views

- **Hash routing** — every top-level view now has its own shareable URL:
  `#summary`, `#details`, `#architecture`, `#auto-improve`. Details sections are
  addressable as `#details/<section>` (e.g. `#details/scorecard`,
  `#details/trends`, `#details/wsl`). Opening or sharing any of these URLs lands
  directly on that view and scrolls to the section; tab clicks update the URL via
  `history.replaceState` (no back-button clutter). Legacy `#sect-*` anchors and
  bare section names continue to work.

### Added — export test suite & runner

- **`tests/test_export.py`** — an integrated distribution test suite that
  validates the *packaged* artifact rather than behaviour: every module imports,
  the package version is consistent across `pyproject.toml`, `sysperf.config` and
  `sysperf.__version__`, every `package-data` pattern matches real files, the
  shipped dashboard carries the hash router, and the required distribution files
  (LICENSE, README, CHANGELOG, NOTICE, …) are present.
- **`run-tests.sh`** — a one-command export test runner that builds an isolated
  venv, installs the package with dev extras, and runs the full pytest suite plus
  ruff lint against the exported tree.

### Fixed — trend/scorecard consistency for ceiling metrics

- **`sysperf.trends`** — `analyze()` is now ceiling-aware. A ceiling criterion
  (e.g. vmmemWSL, scored by `score_ceiling` as "under cap and not growing") is no
  longer reported as *degrading* with a phantom "days to target" simply because
  its daily typical drifts upward while still far under the cap. The long-term
  view now mirrors the live scorecard: under the cap with headroom is stable;
  falling is improving; degrading (with an ETA to the cap) is flagged only when
  the drift would actually breach the cap within the analysis window. The
  scorecard row now carries an explicit `ceiling` flag, recorded in the trend
  store's per-metric metadata.

## 2.2.0 — 2026-09-18

### Added — autonomous improvement

SysPerf now closes its own loop. A daemon thread scans every five minutes; when
the overall score is below 80% it runs a root-cause analysis and applies the
remediations that address the cause, then measures the effect on the next scan and
learns which fixes actually work.

- **`sysperf.trends`** — long-term usage record: per-criterion daily and hourly
  rollups (180 days), least-squares drift of the daily typical, volatility,
  chronic-miss detection, hour-of-day usage profile, and a usage-regime classifier
  that compares the current load with this machine's own norm. Seeded from the
  adaptive layer's per-day aggregates so the long view exists on first run.
- **`sysperf.rca`** — ranks every criterion under target by its share of the
  scoring gap and attributes a cause from both horizons: `transient_spike`,
  `usage_driven`, `long_term_regression`, `chronic_unreachable`, `near_target`,
  `steady_shortfall`. Findings carry evidence from the same sample and the
  remediation that maps to them. A chronically unmet criterion produces a
  target-retune *proposal* instead of a pointless fix.
- **`sysperf.autoimprove`** — the controller: mode (`off`/`observe`/`auto`),
  safe-risk-only unattended execution, per-action cooldowns, an action budget that
  widens when the long-term score trend declines, verification of every applied
  fix, and an effectiveness ledger that suppresses an action after three verified
  attempts with no net benefit. Criteria that are met but drifting are remediated
  preventively; regressions automation cannot close are escalated as proposals.
- **Auto-Improve dashboard tab** — status, live RCA with evidence, last cycle with
  measured before/after, the long-term trend table, the learned-effectiveness
  ledger, open proposals, and the recent cycle log. Mode switch and manual
  scan/fix buttons are CSRF-protected.
- **API** — `GET /api/trends`, `GET /api/rca`, `GET /api/autoimprove`,
  `POST /api/autoimprove/run`, `POST /api/autoimprove/mode`.
- **Configuration** — new `trends` and `autoimprove` sections; see manual
  chapters 8.7 and 14.

### Fixed

- Essential table cells in the new tab no longer disappear in compact mode
  (`.note keep`), which had hidden the *why*, *verdict* and *outcome* columns.
- `r.status` and the battery status string are HTML-escaped before being written
  into `innerHTML`.
- The dashboard refetches its per-process CSRF token once on a 403, so POSTs no
  longer fail until a manual reload after the service restarts.
- `/api/rca` and `/api/trends` are memoised for the snapshot TTL: the tab polls
  them, and both walk the whole history, so uncached they added measurable load to
  the machine being measured.
- The cycle log is trimmed through an atomic replace, so a concurrent reader can
  no longer observe a half-written file.
- `AutoImprover.status()` snapshots its mutable state under the lock — iterating
  a dict the scan thread was resizing could raise `RuntimeError`.
- The "next scan" countdown follows the loop's own schedule, so a manual cycle no
  longer misreports when the next automatic scan lands.
- All four dashboard views are exposed as `role="tabpanel"` with
  `aria-labelledby`.

### Tests

- New suites for `trends`, `rca` and `autoimprove` (100% line coverage on all
  three), plus HTTP tests for the five new endpoints, memoisation, and CSRF
  enforcement.

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
