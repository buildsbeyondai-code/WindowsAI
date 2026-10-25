# Security Policy

## Design posture

SysPerf measures a Windows PC and, when you let it, changes it. Its defences
assume the main attacker is a web page in your own browser, because the
dashboard is a local web server with no login.

- **Loopback only.** The HTTP server binds `127.0.0.1`. There is no
  authentication, so a non-loopback bind is **not a supported configuration**
  (see *Deployment guidance*).
- **Host and Origin checks.** Every request must name this service in its
  `Host` header: `127.0.0.1`, `localhost`, `::1`, the bind address, or a name
  listed in `allowed_hosts` / `PERFDASH_ALLOWED_HOSTS`. Anything else gets
  `403`. That stops DNS rebinding, where a site points its own hostname at
  127.0.0.1 to read the dashboard. POST requests that carry a foreign `Origin`
  are refused as a second layer (`hostguard.py`).
- **CSRF token on every change.** Each process makes a random token, handed to
  the page by `GET /api/fixes`. Every POST route requires it (see *Mutating
  surface*). A test posts to every registered POST route without the token and
  expects `403`.
- **Content-Security-Policy.** Responses allow scripts, styles, images and
  `fetch` only from this origin. The page loads no third-party resources.
- **No injection surface.** The collector script path is fixed in code. No HTTP
  input reaches a shell. Process names for App Control must be in the
  server-computed allowlist. Candidate ids are matched against
  `[A-Za-z0-9_.-]{1,80}`.
- **Outbound traffic is limited and opt-in.** The daily version check reads
  public release feeds (GitHub, PyPI, npm, endoflife.date, winget). AI
  self-evolution, when you enable it, sends source excerpts and metrics to the
  model provider you configure. Nothing else leaves the PC.
- **Elevation is optional and separate.** The service runs as a standard user.
  The standby-memory helper (`standby_broker.ps1`) is the only part that runs
  as Administrator, and only if you start it yourself.

## Changes SysPerf can make to your PC

These are real system changes. Each one is listed on **Home** with what it
does and how to undo it (`GET /api/store`). In a scripted install the
background features start on, because the owner set them up on purpose. In
the Microsoft Store build each one starts **off** until you turn it on.

| Feature | What it does | Scripted install | Store build |
|---|---|---|---|
| Automatic fixes | Flushes DNS, trims idle apps' working sets, clears standby memory | on | off |
| Explorer watchdog | Restarts Explorer when it holds heavy CPU | on | off |
| Tray auto-quit | Quits listed tray apps after their window has been closed for a while, asking first per app | on | off |
| OneDrive lock policy | Pauses OneDrive while you use the PC | environment flag | off |
| AI self-evolution | Proposes code changes, tests them in a sandbox, keeps them for review | off until enabled with a key and a source checkout | off |

Fixes marked `manual` or session-killing are only ever described, never run.
The autonomous loop runs `safe` risk only, unless `autoimprove.allow_caution` is
set. It runs at most two actions per cycle, waits 30 minutes before repeating
an action, and cannot edit targets, criteria or scoring.

## AI autonomy and self-evolution

- **Autonomy defaults to `observe`.** Only low-risk, reversible, individually
  enabled actions ever run unattended. Defender, the firewall, Windows Update,
  event logging, backup/VSS, BitLocker, UAC, recursive deletes and critical
  processes are refused at every level.
- **Model output is untrusted data.** It is schema-validated, and its edits may
  touch only eligible, unprotected files. A diff that adds subprocess, network,
  dynamic-code or security-sensitive constructs is rejected.
- **Generated code runs only in isolation**, meaning bubblewrap (directly or
  through WSL) or a `--network none` read-only container. With neither,
  validation is blocked.
- **Nothing is deployed without you.** A passing candidate is committed to a
  `sysperf-evolve/<id>` review branch. The checked-out code, and so the running
  service, changes only when you choose **Apply & restart**
  (`POST /api/codevolve/apply`). Apply refuses a dirty tree or a moved base,
  tags the previous commit as a backup, and the supervisor rolls back a patch
  that never becomes healthy.
- **Everything is audited** in a hash-chained (tamper-evident) log.
- **API keys.** They come from the environment or a key file and are never
  logged or returned. On Windows the file is encrypted with DPAPI for the
  current user. Elsewhere it is written `0600`, and a file other users can
  read is ignored.

Details and stated limits are in `docs/AI_EVOLUTION.md` §4.

## What the API exposes

Treat `/api/metrics` as sensitive. It includes running process names, top memory
consumers, installed third-party drivers, enabled startup entries, connection
counts and the machine's hostname and model. That is a useful reconnaissance
picture, which is why the bind is loopback only. Wi-Fi SSID and BSSID are
masked in that response.

## Deployment guidance

| Scenario | Recommendation |
|---|---|
| Single desktop, default install | Nothing to do: loopback only. |
| Behind a reverse proxy on the same PC | Add the name the proxy forwards to `allowed_hosts`. |
| Reachable from a LAN or the internet | Not supported. There is no authentication, TLS or rate limiting. |

## Supported versions

| Version | Supported |
|---|---|
| 5.0.x | Yes |
| 4.1.x and older | No. Upgrade to 5.0 (4.1 is exposed to DNS rebinding). |

## Mutating surface

Every route below changes state and requires the CSRF token. All other routes
are `GET` and have no side effects. `tests/test_security_doc.py` fails if this
list and the route table disagree.

| Route | Effect |
|---|---|
| `POST /api/fix` | Previews fixes; applies them only with `confirm: true` and the token |
| `POST /api/autoimprove/run` | Runs one diagnose/remediate cycle |
| `POST /api/autoimprove/mode` | Sets the loop to `off`, `observe` or `auto` |
| `POST /api/autonomy` | Sets the autonomy level or enables an action |
| `POST /api/actions/status` | Marks a human action item done or reopens it |
| `POST /api/onedrive` | Starts or stops OneDrive |
| `POST /api/onedrive/control` | Turns all OneDrive control on or off |
| `POST /api/appctl` | Starts or stops an allow-listed top-CPU app, or shuts down WSL |
| `POST /api/procwatch` | Turns the Explorer watchdog on or off |
| `POST /api/procwatch/run` | Runs one watchdog check now |
| `POST /api/trayquit` | Changes tray auto-quit settings and per-app consent |
| `POST /api/store` | Turns one opt-in feature on or off |
| `POST /api/selftest/run` | Starts the test suite in the background |
| `POST /api/evolution/run` | Runs one remediation-evolution pass |
| `POST /api/evolution/promote` | Promotes (needs `confirm`) or rejects a candidate remediation |
| `POST /api/evolution/rollback` | Undoes the latest promoted remediation |
| `POST /api/codevolve/run` | Starts one code self-evolution cycle |
| `POST /api/codevolve/settings` | Changes self-evolution settings |
| `POST /api/codevolve/review` | Approves or rejects a code candidate |
| `POST /api/codevolve/apply` | Deploys an approved candidate and restarts |
| `POST /api/codevolve/rollback` | Reverts the last deployed candidate |
| `POST /api/llm/key` | Stores the model API key |
| `POST /api/restart` | Restarts the service under its supervisor |
| `POST /api/diagnostics/clear` | Clears the in-memory error list |
| `POST /api/notifications` | Marks notifications read or sends a test toast |
| `POST /api/settings` | Saves validated settings |
| `POST /api/settings/reset` | Resets settings to defaults |
| `POST /api/settings/import` | Imports a settings document |

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Open a private [GitHub security advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on the repository. Include:

- the version (`sysperf --version`) and Windows build;
- what an attacker gains, and what access they need to start with;
- a reproduction, ideally the smallest one that works.

Expect an acknowledgement within five working days and an assessment within ten.
Fixes for confirmed issues ship in a patch release, with credit unless you prefer
otherwise.

### In scope

Anything that lets a web page or another user read data they should not, bypass
the Host, Origin or token checks, reach a shell through the collector, escalate
privilege, or make SysPerf write outside its own data directory and configured
source checkout.

### Out of scope

- Code already running as you on the same PC. It can read the token and the
  data directory directly.
- Absence of TLS on the loopback bind.
- Findings that require administrator rights you already hold.
