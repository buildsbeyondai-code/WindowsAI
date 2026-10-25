# Architecture decisions

Short records of choices that are not obvious from the code. Each one gives
the context, the decision, and what it costs. Add a new record rather than
editing an old one when a decision changes.

## ADR-001 · Local web app on loopback, no login

**Context.** The tool is for one person on their own PC, and the UI must work
in any browser and inside a desktop shell.
**Decision.** Flask on waitress, bound to `127.0.0.1`, with no accounts.
**Consequences.** Any local process running as the user can call it. Other
web pages are the realistic attacker, so ADR-002 and ADR-003 exist. Binding
off loopback is unsupported.

## ADR-002 · A per-process CSRF token on every POST

**Context.** Blind cross-site POSTs can reach loopback servers.
**Decision.** Each process generates a random token, handed out by
`GET /api/fixes`. Every POST route requires it. `test_security_doc.py`
enforces this for every route and keeps SECURITY.md in step.
**Consequences.** The token alone does not stop DNS rebinding (ADR-003).

## ADR-003 · Host allowlist and Origin check (5.0.0)

**Context.** A site that rebinds its own hostname to 127.0.0.1 becomes
same-origin with the dashboard. It could then read the token and POST.
**Decision.** Refuse any request whose `Host` is not a loopback name, the bind
address or a configured `allowed_hosts` entry. Also refuse POSTs with a
foreign `Origin`.
**Consequences.** A reverse proxy must forward an allowed Host, or be added to
`allowed_hosts`.

## ADR-004 · PowerShell collector in one warm session

**Context.** WMI/CIM and performance counters are easiest to reach from
PowerShell, but starting it costs about 5 s per sample.
**Decision.** Keep one PowerShell process and reuse it (`pwsh_session`).
Recycle it after 500 runs, and fall back to a one-shot process for a sample
if the session fails.
**Consequences.** Measured at 2.7 s vs 5.0 s for a light sample. Headline
values that need second-level freshness come from `livestats` (Win32 via
ctypes) instead.

## ADR-005 · Polling, not event-driven, for metrics

**Context.** The standard prefers event-driven updates where reliable.
**Decision.** Sample on an interval (60 s, fast/full tiers), because the
metrics are levels (memory, commit, queue length), not events. Windows has no
reliable change notification for most of them, and WMI event subscriptions
would cost more CPU than one sample a minute.
**Consequences.** Values can be up to one interval old. `livestats` covers
the headline numbers in real time. Revisit if a metric gains a cheap event
source.

## ADR-006 · SQLite for history

**Context.** A JSONL history grew without bound and had to be rewritten to trim.
**Decision.** SQLite (WAL) with raw samples for 14 days and hourly rollups for
400 days, pruned every 500 writes. The JSONL file is migrated once.
**Consequences.** One file of about 20 MB. Corrupt rows are skipped.

## ADR-007 · Autonomy defaults to observe; actions are allow-listed

**Context.** A tool that changes the system must not surprise its owner.
**Decision.** Levels `off < observe < safe_auto < self_evolve`. Only
reversible, low-risk, individually enabled actions run unattended. Some
categories are refused at every level (Defender, firewall, updates, UAC,
recursive deletes, critical processes).
**Consequences.** Some improvements wait for a click. The action queue
explains why.

## ADR-008 · Store build: everything outside the app is opt-in

**Context.** Store policies 10.2 and 10.8.
**Decision.** In Store mode, each feature that changes the PC starts off and
is listed on Home with what it does and how to undo it. The scripted install
keeps its configured behaviour.
**Consequences.** Two default sets, tested by `test_store_mode.py`.

## ADR-009 · AI patches live on review branches; deploying needs Apply

**Context.** Self-modifying code must never change production silently.
**Decision.** Model output is untrusted. Protected files cannot be edited,
diffs are scanned, and generated code runs only in a sandbox (bubblewrap or a
`--network none` container; with neither, nothing runs). A winning candidate
is committed to `sysperf-evolve/<id>`. Only an explicit **Apply & restart**
fast-forwards the checkout. A backup tag and the supervisor's health check
allow automatic rollback.
**Consequences.** The evolution checkout can be the folder the service runs
from (an editable install). This is safe only because Apply is explicit and
refuses a dirty tree.

## ADR-010 · API key encrypted with DPAPI on Windows (5.0.0)

**Context.** `chmod 600` does nothing on Windows, so the key sat in plaintext.
**Decision.** Store a DPAPI blob scoped to the current user. Older plaintext
files are encrypted in place on first read. POSIX keeps `0600` and refuses
group- or world-readable files.
**Consequences.** A copied data directory cannot reveal the key. Moving to a
new PC means entering the key again.

## ADR-011 · 100% line coverage is a gate, not a proof

**Context.** The in-app self-test and CI require full line coverage.
**Decision.** Keep the gate, because it finds dead and untested code. Correctness
claims rest on behaviour tests: E2E in a real browser, Windows-only tests
against live APIs, the traceability table in REQUIREMENTS.md, and the
independent OS comparison in `test_livestats_win.py`.
**Consequences.** Coverage is reported next to those results, never as
"verified" on its own.

## ADR-012 · Hash-locked runtime dependencies (5.0.0)

**Context.** `requirements.txt` pins only the two direct dependencies.
**Decision.** `requirements.lock` holds every runtime package for all platforms
with sha256 hashes. CI checks that it matches `requirements.txt`, audits it
with pip-audit, installs from it with `--require-hashes`, and publishes a
CycloneDX SBOM.
**Consequences.** A dependency bump means regenerating the lock (the command
is in its header). Dependabot proposes bumps weekly.
