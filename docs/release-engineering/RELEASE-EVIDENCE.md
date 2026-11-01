# Release evidence: 5.5.0 audit remediation (candidate 5.5.2)

Branch `impl/5.5.x-audit-remediation`, 1 November 2026. Answers
[the 5.5.0 audit](./SysPerf_5_5_0_Audit.md) and
[the implementation prompt](./SysPerf_Claude_Code_Prompt.md). Not released, not
tagged, not submitted to the Store. The working checklist is
[PROGRESS.md](./PROGRESS.md); raw evidence is in [evidence/](./evidence/).

**Decision: HOLD.** The three code blockers (F1 packaging, F2 trust decision,
F3 recovery claim) are fixed and verified locally, in simulated faults and on
hosted Windows x64 and ARM64. Still missing before any release: an installed-app
run on disposable Windows 11 x64 and ARM64 machines, the disposable
test-certificate run, a 24-hour soak, and a decision on Windows 10.

## Evidence kinds

The prompt asks for these to be kept apart. Each result below says which it is.

| Kind | Where | What it can show |
|---|---|---|
| **Local** | Windows 11 x64 (the daily-use PC; isolated data folders, never the live service) and WSL Ubuntu | Code behaviour, real Windows APIs (Authenticode, PowerShell collector) |
| **Simulated fault** | Real git repositories in temp folders with injected failures | Recovery logic; not a real crash or power loss |
| **Hosted Windows** | GitHub Actions `windows-latest` (x64) and `windows-11-arm` | Source tests and the frozen MSIX app on clean runners |
| **Installed app** | Not done | Activation, WebView2, upgrade/uninstall, accounts |
| **Field** | Not done | Real users, time, other hardware |

## Runs

| Run | Commit | Result |
|---|---|---|
| Baseline CI [41219931292](https://github.com/buildsbeyondai-code/WindowsAI/actions/runs/41219931292) | 83d718f (v5.5.0) | 7/7 jobs passed |
| Baseline MSIX [41219931132](https://github.com/buildsbeyondai-code/WindowsAI/actions/runs/41219931132) | 83d718f (v5.5.0) | **x64 and arm64 failed** (smoke: "no live sample: {}"); release skipped |
| Candidate CI [41263716773](https://github.com/buildsbeyondai-code/WindowsAI/actions/runs/41263716773) | 51f1f6b | 7/7 passed: Ubuntu and Windows × py3.10/3.13 (Windows jobs incl. collector and end-to-end smoke), PowerShell analysis, distributions, dependencies/advisories/SBOM |
| Candidate MSIX [41263716838](https://github.com/buildsbeyondai-code/WindowsAI/actions/runs/41263716838) | 51f1f6b | x64 and arm64 built, machine type checked, **frozen app took one real sample on each**; release job skipped (tag-only) |
| Candidate MSIX [41265192911](https://github.com/buildsbeyondai-code/WindowsAI/actions/runs/41265192911) | 9a45dee (adds the stale-profile smoke) | x64 and arm64: **clean and stale profiles both pass with a sample taken after the call began**; release job skipped |
| Candidate CI [41265193192](https://github.com/buildsbeyondai-code/WindowsAI/actions/runs/41265193192) | 9a45dee | 6/7 passed; **PSScriptAnalyzer failed** on the two evidence scripts added in that commit (`$profile` is an automatic variable; unapproved function name). Fixed in the next commit; see the final run below |
| Candidate CI and MSIX (final) | the commit that adds this report | recorded in [PROGRESS.md](./PROGRESS.md), item 5 |
| Release gates, Windows (`tests/testrunner.py --no-live`) | 51f1f6b | 7/7 executed gates passed: 100% line coverage, 100% UI coverage; live gate skipped here, run below ([gates-windows.json](./evidence/gates-windows.json)) |
| Release gates, Linux, live gate against a disposable candidate service on :5099 | 51f1f6b | live, unit, discover, compile, lint passed. Not passing here: coverage (2 lines that need PowerShell; covered on Windows) and frontend (no `node` on that PATH; passed on Windows). Browser not run (no Playwright). ([gates-linux.json](./evidence/gates-linux.json)) |
| Pre-push suite, Windows | 51f1f6b, 9a45dee | 1,448 passed, 2 skipped (Linux `/proc`; POSIX permission bits) |
| Sandbox suite (bubblewrap, as self-evolution's baseline) | 51f1f6b, 9a45dee | 1,356 passed, 12 skipped |

Package digests (GitHub artifact SHA-256, run 41263716838; unsigned build
artifacts, not release packages): `msix-x64` 0aad1585cffd28ab13be5e8ee80a4a15ee7ef0b09fef6ecdfe0e6ff44d2b1536,
`msix-arm64` edc952a0cb54cddaa9623a495dc1567483165dbd40c2ef13888aede0f995cff0.

## Findings

| # | Finding | Fix (commit) | Evidence |
|---|---|---|---|
| F1 | `--once` printed the cached snapshot (empty on a new profile) and exited 0 | Fresh, bounded collection in a new process; `{"error","detail"}` and exit 2/3/4 on failure; packaging runs on CLI/collector changes; smoke runs a clean and a stale profile and needs `_t` after the call began (ea6b3bd, 51f1f6b, 9a45dee) | Local: fresh and stale data folders rc 0, sample 6 s after start, 48 keys. Hosted: x64 and arm64 frozen sample (run 41263716838). Rehearsal of the new step on Windows: both profiles pass, the planted 1 MB value is not shown; the 5.5.1 code fails it with `{}` ([smoke-rehearsal.json](./evidence/smoke-rehearsal.json)) |
| F2 | Signature acceptance depended on the word "signed" | JSON verdict; status `Valid` and exact CN and O; NuGet pinned to catalog SHA-512; PowerShell and PortableGit archives to GitHub SHA-256, refused without one (56ddc75) | Parser: 12 rejection cases incl. `NotTrusted CN=Unrelated signed Company`. Local OS (real Authenticode): genuine PSF and Git signers accepted; wrong publisher, one-byte-tampered copy and unsigned file refused. Real NuGet download: 3.15.2 accepted, 3.15.1 against 3.15.2's hash refused. **Test-certificate run: not done** (needs admin; script ready: [testcert-run.ps1](./evidence/testcert-run.ps1)) |
| F3 | Code changed before recovery state was durable; failed dependency restore reported as success | Intent marker before any change; phases intent/merged/applied/rollback_incomplete; start-up recovery; one operation at a time; moved HEAD refused; `RecoveryIncomplete` (33a5ebf, 92c74e1, be97e96) | Simulated faults in real git repositories: marker write fails before and after merge, dependency install then restore fails (incomplete, retried to completion), rollback restore fails, moved HEAD, failed merge, concurrent request, user's uncommitted edits kept (`reset --keep`) |
| F4 | One result credited to every fix in a batch; EMA called an average | Credit only for a fix that ran alone and succeeded; batches kept as group observations; failed actions get nothing; v2 ledger keeps before/after/time; text says recency-weighted, shows last before → after, age, "not yet reliable" (< 3 runs), "may be out of date" (> 30 days), and labels older entries (cc30965) | Unit: the audit's case (CPU 50 → 40 after two fixes) credits neither. Browser (Windows, stubbed backend, real `explain()` output): isolated, batch, failed, worse, unchanged, stale, legacy and single-run records each shown as stated. Fixtures check logic, not a real performance benefit |
| F5 | Missing telemetry could pass a soak | pass / fail / inconclusive (exit 0/1/2); coverage, completeness, gaps, warm-up and restart rules; failed requests recorded (fb3bb2d) | Unit: the audit's two blank rows are inconclusive. Real 6-minute trial against a disposable service with one real restart: restart detected, inconclusive ([soak-restart-trial.*](./evidence/)). **24-hour soak: not done** |
| F6 | Distribution and field coverage | Not addressed in code | See "Needs you" |

### Incident during this work

The first push ran the pre-push suite from a git worktree. Git exports
`GIT_DIR` to hooks run from a worktree, so test fixtures' `git -C <tmp> commit`
committed to the real branch, and `git init` set `core.bare=true` in the shared
repository config (also used by the live install's checkout). The suite then
failed and the push was refused; nothing reached GitHub. Reverted to the exact
prior state (one fixture commit removed from the branch, one test-created branch
deleted, `core.bare=false`); `main` and every working file were untouched.
Fixed in be97e96: `tests/conftest.py` drops the repository variables and the
hook unsets them. Verified by running the git-heavy tests with `GIT_DIR` pointed
at a decoy repository, which stayed unchanged.

A second issue was caught by the same hook: nine collector tests faked
`subprocess.run` but silently needed PowerShell on PATH, which the sandbox
lacks (51f1f6b).

## Five-category reassessment

Pass conditions are the prompt's requirements, fixed before editing. The
baseline is re-scored under the same conditions using the new evidence; the
audit's 7.3 is kept for reference. A 10 needs every material condition passed.

### 1. Update security — baseline 7.0 (audit) → re-scored 5.5; candidate **8.5**

| Condition | Baseline 5.5.0 | Candidate |
|---|---|---|
| Structured verdict with explicit failure | Fail (free text, "signed") | Pass |
| Exact approved signer identity and a documented rotation policy | Fail (substring match) | Pass (CN and O exact, subject-pinned) |
| Regression tests: wrong publisher, invalid status, malformed output, missing verifier, digest mismatch, valid signer | Partial | Pass |
| Whole-package integrity for the NuGet and PowerShell archives | Fail | Pass (real NuGet download; GitHub digests unit-tested) |
| Windows path checked with disposable test certificates | Not done | **Not done** (blocked: admin) |

Counterexamples outstanding: none known. Reason: the trust decision is now
structural, but the OS path with a crafted certificate chain is unproven, and
CN/O pinning accepts any valid certificate issued to that exact name (the
chosen trade-off for yearly certificate renewal). The PowerShell upgrader's
MSIX route cannot succeed (7.6.9 publishes no MSIX on GitHub) and now fails
closed.

### 2. Recommendation quality — 7.5 → re-scored 6.0; candidate **8.0**

| Condition | Baseline | Candidate |
|---|---|---|
| Per-action outcome recorded; failures excluded | Fail | Pass |
| Multi-action results are group observations | Fail | Pass |
| Legacy entries labelled conservatively | n/a | Pass |
| 10-point drop distinguishable from 20% | Fail | Pass (last before → after shown) |
| EMA labelled accurately | Fail ("average") | Pass ("recency-weighted") |
| Sample count, freshness, uncertainty | Count only | Pass (count, wins/losses, age, < 3 runs flagged); uncertainty is a threshold, not an interval |
| "Not measured" said when nothing usable | Pass | Pass |
| UI shown for single, batch, partial failure, worse, no change, missing, stale | Not done | Pass with a stubbed backend; not on an installed app with the real backend |
| Repeated paired trials of one optimisation | Not done | **Not done** |

Reason: attribution is now honest about what was observed; no measured
benefit of any fix is claimed by this work.

### 3. Release verification — 7.0 → re-scored 4.5; candidate **8.0**

| Condition | Baseline | Candidate |
|---|---|---|
| Fresh, bounded `--once`; machine-readable failure; nonzero exit | Fail | Pass |
| Clean and prepopulated profiles pass with a current timestamp | Fail | Pass (local rehearsal; hosted x64 and arm64, run 41265192911) |
| Packaging workflow runs on CLI-only changes | Fail | Pass |
| Frozen app runs on hosted x64 and ARM64 | **Fail** (both) | Pass |
| Package identity and hash recorded | Partial | Pass (artifact digests above) |
| Baseline and candidate in comparable environments | — | Pass (same workflows, same runners) |
| Prior release (v5.4.9) upgraded to the candidate on disposable x64 and ARM64 | Not done | **Not done** |

Reason: the release blocker is gone and the gate now tests the property that
failed; the installed upgrade path is unproven.

### 4. Recovery and user control — 7.0 → re-scored 5.0; candidate **8.5**

| Condition | Baseline | Candidate |
|---|---|---|
| Durable intent before mutation | Fail | Pass |
| Expected HEAD validated; no forced moves | Partial | Pass |
| Apply/rollback/recovery serialised | Fail | Pass |
| Recovery retryable and idempotent, also after restart | Fail | Pass |
| Failed dependency restore reported as incomplete | **Fail** (false success) | Pass |
| Backups and diagnostic state kept until verified | Partial | Pass |
| Unrelated user changes preserved | Pass (untested) | Pass (tested) |
| Native: consent/undo, revoked consent, interrupted upgrade, relaunch on an installed app | Not done | **Not done** |

Reason: the false recovery claim is removed and every fault the prompt names
is exercised, in simulated faults rather than real crashes; the installed-app
user-control journeys are untested.

### 5. Windows compatibility and field evidence — 8.0 → re-scored 5.5; candidate **6.5**

| Condition | Baseline | Candidate |
|---|---|---|
| Hosted Windows x64 and ARM64 execution of the package | Fail | Pass |
| Installed app on disposable Windows 11 x64 and ARM64 (activation, WebView2, counters vs Windows) | Not done | **Not done** |
| Minimum OS tested, or manifest narrowed (manifest says 10.0.19041) | Not done | **Not done** (your decision) |
| Genuine 24-hour soak of the installed candidate | Not done (harness could pass blank data) | **Not done** (harness fixed and trialled) |
| Second account, sleep/resume, offline collector, occupied port, upgrade while running, uninstall/reinstall | Not done | **Not done** |

Reason: everything here needs hardware or machines this environment does not
have. The 8.0 was not supported by field evidence; one PC and CI are not a
field.

### Summary

| Category | Audit 5.5.0 | Re-scored 5.5.0 | Candidate |
|---|---:|---:|---:|
| Update security | 7.0 | 5.5 | 8.5 |
| Recommendation quality | 7.5 | 6.0 | 8.0 |
| Release verification | 7.0 | 4.5 | 8.0 |
| Recovery / user control | 7.0 | 5.0 | 8.5 |
| Windows compatibility / field | 8.0 | 5.5 | 6.5 |
| **Mean** | **7.3** | **5.3** | **7.9** |

The re-scored baseline is lower than 7.3 because the conditions are counted
strictly and the reproductions confirmed every finding. No category is a 10: each
has at least one material condition not run.

## What a Windows user would see differently

- The packaged app's one-shot sample is real on a new install, and a failure is
  reported as an error, not as an empty success.
- An upgrade downloads nothing it cannot verify: a file must be validly signed
  by the exact expected publisher, and the archives must match their published
  hashes. Upgrades that relied on a missing hash or on the PowerShell MSIX now
  stop with a reason instead of installing.
- If SysPerf is stopped mid-way through applying a self-evolution change, it
  finishes or reverses it at the next start, and says "Recovery not finished"
  rather than "Verifying" when it cannot.
- Recommendations no longer credit a fix with a change it shared with others;
  effects show the values before and after, how old they are, and when they
  rest on too few runs. Existing records are kept and labelled.

## Remaining defects and blockers

- **No release blocker remains in code.** Release readiness is blocked on
  evidence: installed-app runs (x64, ARM64), the test-certificate run, a 24-hour
  soak, and the Windows 10 decision.
- The version is still 5.5.1 in the code; a release needs the bump to 5.5.2
  (the recommendation labels say "before 5.5.2").
- PowerShell 7.6.9 has no GitHub MSIX; the PowerShell upgrader's MSIX apply
  fails closed until that path is changed or removed.
- Two `app.py` lines are covered only where PowerShell exists (unchanged from
  the baseline); the Windows gate is the authority for coverage.
