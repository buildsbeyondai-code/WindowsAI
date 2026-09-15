# SysPerf User Manual

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

See [the audit record](AUDIT-2.1.1.md) for fixes, tests and platform limitations.


**System Performance Agent — version 2.0.0**

A read-only performance dashboard and advisor for Windows 11.

---

## 1. Introduction

### 1.1 What SysPerf is

SysPerf watches how your Windows 11 machine is actually behaving, compares that
against where it started and what it is capable of, and tells you what to change
to make it faster.

It runs entirely on your machine as a small local web dashboard, plus a background
sampler that reads Windows performance data every twenty seconds.

### 1.2 What SysPerf is not

It is **not** an optimiser that changes settings for you. SysPerf reads. It never
writes to the registry, never disables a service, never deletes a file, never
changes a power plan. When it finds something worth fixing it tells you what it
measured, why that matters, and what to do — and then you decide.

This is a deliberate design constraint rather than an unfinished feature. Tools
that silently modify Windows are impossible to audit: when something breaks a week
later, you cannot tell whether the tool did it. Because SysPerf only reads,
uninstalling it leaves nothing to revert.

### 1.3 Three ideas that make it different

**Your machine sets the standard.** On first run SysPerf records a baseline — the
state of your machine right now — and derives its targets from your actual RAM,
disk size and measured process floor. A 64 GB workstation and an 8 GB laptop get
different targets because the same number cannot be correct for both. There are no
constants copied from someone else's computer.

**It admits what it cannot know.** A measurement that needs more data reports
`measuring` and is left out of the score rather than being assumed good. A fault
that Windows itself reports as "OK" — a touchpad button that sticks, a fan that
rattles — cannot be auto-detected at all, so there is a manual watchlist for it.

**It scores the sustained state, not the moment.** Process and connection counts
fluctuate constantly. Scoring the latest reading would make the score collapse
every time you opened a browser tab, so SysPerf scores a rolling median instead.
The live gauges still show the raw value.

### 1.4 Who it is for

Anyone who wants to know why their Windows machine feels slow and what to do about
it: developers on heavy toolchains, analysts with large spreadsheets, anyone on a
corporate laptop weighed down by management agents, and IT staff who need evidence
rather than an impression.

---

## 2. Requirements

| Requirement | Detail |
|---|---|
| Operating system | Windows 11 (build 22000 or later). Windows 10 works but is not the test target. |
| PowerShell | 5.1, which ships with Windows. PowerShell 7 also works. |
| Python | 3.9 or later. The installer can fetch it via `winget` if it is missing. |
| Privileges | Standard user. Nothing requires administrator rights. |
| Disk space | About 60 MB including the private Python environment, plus up to 12 MB of history. |
| Network | Needed once, to download two Python packages. After installation SysPerf works entirely offline. |

Non-English installations of Windows are fully supported. SysPerf reads
performance data from locale-independent system classes rather than from
performance-counter names, which are translated, and identifies power plans by
their unique identifier rather than by their display name.

---

## 3. Installation

### 3.1 The standard install

Unpack the SysPerf folder somewhere convenient, open PowerShell in it, and run:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

`-ExecutionPolicy Bypass` applies to that one command only. It does not change the
machine's script policy.

The installer will:

1. confirm your Windows and PowerShell versions;
2. find Python 3.9+, offering to install it with `winget` if none is present;
3. create a private virtual environment in `%LOCALAPPDATA%\SysPerf\venv`;
4. copy the application to `%LOCALAPPDATA%\SysPerf\app`;
5. write a starter configuration file;
6. create Start Menu entries for the dashboard and for a one-off report;
7. register a sign-in task so the dashboard is always running;
8. start it and confirm it responds.

When it finishes, the dashboard is at **<http://127.0.0.1:5056/>**.

Everything is written to two places: `%LOCALAPPDATA%\SysPerf` and your own Start
Menu folder. No Windows service is created, no shared location is touched, and no
policy or system setting is changed.

### 3.2 Installer options

| Option | Effect |
|---|---|
| `-Port 5100` | Serve on a different port. Use this if 5056 is taken. |
| `-NoAutoStart` | Do not register the sign-in task. Start SysPerf yourself when you want it. |
| `-NoStart` | Install without launching. |

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Port 5100 -NoAutoStart
```

### 3.3 Running without installing

To try SysPerf, or to get a report from a machine you would rather not install
anything on:

```powershell
powershell -ExecutionPolicy Bypass -File run.ps1 -Report   # one-off text report
powershell -ExecutionPolicy Bypass -File run.ps1           # dashboard
```

This creates a `.venv` folder inside the SysPerf directory and touches nothing
else. Delete the folder to remove it completely.

### 3.4 Installing with pip

```powershell
py -m pip install --user .
sysperf --report
sysperf
```

Useful for scripted deployment. You will need to arrange autostart yourself.

### 3.5 Running under WSL 2 (optional)

SysPerf can run inside a WSL 2 distribution and reach Windows through interop:

```bash
bash install-wsl.sh
```

Consider this only if you already run local services in WSL, or if you cannot
install Python on the Windows side. The trade-offs: every sample pays the
WSL-to-Windows interop cost of a second or two, and the dashboard is only up while
WSL is running. In exchange, the WSL panel gains Linux-guest memory figures.

### 3.6 If installation fails

**"Python 3.9 or later is required and winget is unavailable."** Install Python
from <https://www.python.org/downloads/windows/>, ticking *Add python.exe to
PATH*, then run the installer again.

**"Python was installed but could not be located."** The new `PATH` is not visible
to the current console. Open a new PowerShell window and re-run the installer.

**"Dependency installation failed."** Usually a corporate proxy. Set it and retry:

```powershell
$env:HTTPS_PROXY = 'http://proxy.example.com:8080'
powershell -ExecutionPolicy Bypass -File install.ps1
```

**"Task Scheduler registration failed."** Some managed machines block task
creation. The installer detects this and falls back to a shortcut in your Startup
folder, which achieves the same thing. No action needed.

---

## 4. First run

### 4.1 What happens in the first minute

The first sample is a *fast* one — memory, processes, handles, disk, CPU — so the
dashboard has something to show within a few seconds.

The second sample is a **full sweep**: it additionally enumerates services,
startup entries, network connections, hardware and drivers. That takes up to a
minute on a busy machine, which is why it is not what you wait for on arrival.
From it SysPerf writes `baseline.json` — your starting point — and derives the
targets for every criterion.

Until the sweep finishes the dashboard says so, and shows live values without
scores. Scores and recommendations appear as soon as the baseline exists.
Afterwards the full sweep runs every fifteenth sample (about every five minutes).

### 4.2 Why everything reads 0%

Because the baseline *is* the current state, no distance has been travelled yet.
That is correct, not a bug. The numbers move when you act on the recommendations.

If you would rather measure from a machine you have already tuned, act on the
recommendations first, then run:

```powershell
sysperf --recalibrate
```

### 4.3 The first ten minutes

Two things need history before they can say anything useful:

- the **WSL memory trend**, which needs at least six samples over ten minutes;
- the **adaptive layer**, which needs about twenty samples before it stops saying
  "learning your pattern".

Leave the dashboard running and check back later.

---

## 5. Reading the dashboard

The page is laid out top to bottom in order of usefulness: what to do, then how
you are doing, then the evidence.

### 5.0 Summary and Details tabs

Two tabs sit at the top-left, above the title.

- **Summary** (default) is the whole dashboard distilled into a **10-category
  scorecard**: memory pressure, WSL 2, browser footprint, application handles,
  CPU & processes, storage, network optimisation, Wi-Fi connectivity, hardware &
  drivers, and component currency. Each row shows a score out of 100 and the
  remaining **room for improvement** (100 − score), with one overall system
  score. Categories that cannot be evaluated yet show "measuring".
- **Details** is the full dashboard: charts, scorecard, browser ownership, WSL,
  Wi-Fi, network optimisation, hardware, drivers and component upgrades.

The tab you last used is remembered. The overall score shown on the Details
header is the *same* number as the Summary's overall score — both come from one
computation, so they never disagree.

### 5.0.1 Wi-Fi, network optimisation and component upgrades (new in 2.0)

- **Wi-Fi connection** — SSID, negotiated link rate, radio generation, and
  band/channel. Signal strength, band, channel and Rx/Tx rates require Windows
  **Location** permission for the WLAN service (Settings → Privacy & security →
  Location). Without it SysPerf still reports the adapter-level link speed and
  tells you what to enable.
- **Band & channel overlap** — scans neighbouring access points and recommends
  the clearest 2.4 GHz channel (only 1, 6 and 11 do not overlap) and the least
  used 5 GHz channel. Channel and width are **access-point settings**: SysPerf
  tells you what to change on the router, it cannot change them for you.
- **Network optimisation** — the TCP connection footprint, active adapters and a
  prioritised fix plan. Each action is tagged **client**, **router** or
  **windows** so it is clear where it applies.
- **Component upgrades** — a version-currency table for the components actually
  present on the machine (Windows build, PowerShell 5.1/7, Edge, WebView2,
  graphics and network drivers, BIOS/UEFI, and Python/Node/Git/WSL when
  installed), each with an estimated performance benefit. Components that are
  not installed are simply not listed. `latest` is a reviewed reference target
  dated in the table, not a live query — treat it as guidance, and confirm driver
  and firmware versions with your OEM's support tool.

### 5.1 Header

Live machine facts — uptime, RAM, current CPU, process and service counts, samples
collected, SysPerf version. The dot on the right is green when data is fresh, amber
when the last sample is stale, red when the collector reported an error.

**Hide descriptions** collapses the long explanatory notes for a denser view. The
setting is remembered. Recommendations are never hidden by it.

### 5.2 Alerts

Only appears when something needs saying: a WSL memory ceiling under threat, a
kernel pool well above your baseline, or a collector error.

### 5.3 Overview cards

- **System score** — the mean of the ten Summary categories; the same number the
  Summary tab shows. Categories still measuring are excluded rather than counted
  as good. **Room for improvement** is 100 − system score.
- **Room for improvement** — the remaining distance to target.
- **Criteria status** — how many are met, partial, regressed or measuring.
- **Memory pressure right now** — commit charge against physical RAM. When commit
  charge exceeds RAM, compression and paging are on the hot path and everything
  feels slower.

### 5.4 Recommendations

The actionable part of the tool. Each card gives:

- a **severity** (critical, high, medium, low) and a **category**;
- **needs admin**, when the fix requires elevation;
- the **finding** — what was measured;
- **why it matters**, in terms of what you experience;
- **numbered steps** to take;
- an optional **command** you can run yourself;
- the **expected effect**.

Work top down. Severity reflects the size of the effect on responsiveness, not how
easy the change is.

### 5.5 Key metrics

Six headline gauges — commit charge, Memory Compression, available memory, WSL
memory, browser processes, handles — each with its live value, its change against
baseline, its target, and a sparkline. Baselines and targets come from the server,
so they always agree with the scorecard.

### 5.6 Self-improving scorecard

Your personal-best layer. For each tracked metric it shows the best daily typical
you have ever recorded, a stretch target just beyond it, and today's typical. The
bar only ever moves in the improving direction, so it tightens as your habits
improve and never rewards a regression. A star marks a new personal best today.

### 5.7 Success-criteria scorecard

One row per criterion: baseline, live value, target, change, progress bar, room
remaining and status.

| Status | Meaning |
|---|---|
| **met** | At or better than target. |
| **partial** | Between baseline and target, or within the noise band. |
| **regress** | Meaningfully worse than baseline (beyond the 2% dead-band). |
| **measuring** | Not enough data yet. Excluded from the overall score. |

The **Live** column is the raw latest reading; **Progress** is computed from the
smoothed value. They can disagree briefly, and that is intentional — see
[section 6](#6-how-scoring-works).

### 5.8 Trends

Rolling history for memory, WSL memory, handles, kernel pools, browser processes,
CPU, network connections and startup entries. Dashed lines mark targets. All
charts are hand-drawn SVG: the page loads no external scripts and works offline.

### 5.9 Browser processes

Usually the biggest single lever on an office machine, and the most misunderstood.

Windows runs two kinds of browser process: the browser you opened, and *embedded*
browser processes (WebView2) started by other applications — file sync, Start menu
search, chat clients, VPN portals. Task Manager shows them all as browser engines,
but only some are yours to close.

- **Embedded browser by host application** — which app owns each group.
- **Closability** — mandatory (cannot be closed) versus in use versus
  discretionary, with the floor arithmetic. Anything above the realistic floor is
  discretionary: tabs and extensions you can close.
- **Edge composition** — extension, renderer, utility and main processes, plus the
  memory extensions are holding. Extensions are the biggest lever, because one
  with a persistent background page never releases its process.

If an application of yours is being counted as discretionary when it should not
be, add it to `browser_hosts` in the configuration — see
[section 8.4](#84-browser-ownership).

### 5.10 WSL 2 and kernel pools

The WSL card only appears when WSL is present.

- **WSL 2 memory** — the working set against the configured cap, whether a
  `.wslconfig` exists, and whether automatic reclaim is enabled.
- **Kernel pool versus baseline** — memory allocated by drivers: antivirus and EDR
  filters, virtual disks, network filters. Applications cannot reach it, so
  closing programs will not reduce it. It is shown because unexplained pool growth
  is worth knowing about, not because you can fix it directly.
- **Disk and background churn** — free space against target, `svchost` count, and
  Windows Search host processes. Search hosts at zero means the index is down and
  searches are scanning the disk.

### 5.11 Hardware health

Device Manager fault codes, storage health, battery charge and wear against design
capacity, and input devices. Plus your **known-issue watchlist** — see
[section 8.5](#85-hardware-watchlist).

### 5.12 Drivers health

Third-party driver inventory, unsigned packages, packages older than four years,
and driver-specific fault codes. Stale storage, chipset, graphics and network
drivers are the ones that measurably affect performance; a stale driver for a
printer you no longer own does not matter.

---

## 6. How scoring works

### 6.1 Progress

For each criterion:

```
progress = (current − baseline) ÷ (target − baseline)     clamped to 0–100
```

Reaching the target always scores 100%, even in the unusual case where your
starting value was already better than the achievable floor.

### 6.2 Where targets come from

Targets are derived once, when the baseline is captured, from your machine's own
capacity. The exact fractions are configurable; the defaults are:

| Criterion | Target |
|---|---|
| Commit charge | The lower of 75% of RAM, or 15% better than baseline |
| Memory Compression | The lower of 5% of RAM, or half of baseline |
| Available memory | The higher of 30% of RAM, or 25% better than baseline |
| Total handles | 15% better than baseline |
| WSL 2 memory | The `.wslconfig` cap, or 35% of RAM if there is none |
| Browser processes | The measured floor plus a little headroom |
| System volume free | The higher of 15% of the volume, or 10% better than baseline |
| Remote connections | The higher of 25, or 30% fewer than baseline |
| Startup programs | The lower of 10, or 45% fewer than baseline |
| Hardware faults | Zero |

Every target is then clamped so it is an improvement on your baseline but never
below a physical floor. That clamping is what stops a criterion from being
permanently red for reasons you cannot influence — asking for fewer browser
processes than the non-closable ones your employer's software requires, for
instance.

### 6.3 Smoothing

Counts breathe. Opening a document, a background sync, an antivirus scan — each
moves process, handle and connection counts for a few seconds. Scoring the latest
sample would make the score collapse and recover constantly and mean nothing.

So the score uses the **median of the last fifteen samples** (about five minutes).
A genuine, sustained change moves the median within one window; a spike does not
move it at all. Live gauges and charts still show the raw value.

Two metrics are both bursty *and* sampled sparsely, so they get a longer window:
remote TCP connections use sixty minutes, because sync and scan activity can
triple the count for several minutes before it settles.

### 6.4 The noise dead-band

On day one your baseline equals your current value, so any upward tick of noise
would cross it and read as a regression. A metric within **2%** of its baseline
counts as unchanged. Configurable as `regress_band_pct`.

### 6.5 Ceiling criteria

WSL memory is not a reduction target. It is a ceiling: *at or below the cap and
not growing*. The generic formula does not apply, because the cap normally sits
above the starting value, so improving would score negative.

Instead it is scored on being under the cap and not trending toward it, with three
guards:

- **A minimum window.** Windows trims working sets opportunistically. A
  multi-gigabyte trim over four minutes extrapolates to a meaningless five-figure
  rate, so at least six samples over ten minutes are required.
- **Re-anchoring.** If the VM restarts, or the working set collapses from a high
  steady state, the window restarts. Otherwise the recovery ramp afterwards would
  read as a leak.
- **Runway, not slope.** A slow rise from a low base is normal churn. The
  criterion only complains when the cap is genuinely threatened — close to it, or
  projected to reach it within a few hours.

Until the window is long enough the criterion reads `measuring` and is left out of
the overall score. The dashboard says "*n* of *m* criteria scored" so you can see
that it abstained rather than assumed.

### 6.6 Adaptive scoring

The plan scorecard answers "how far through the plan am I". The adaptive layer
answers a different and often more useful question: "is today better than my
normal".

For each behaviour-driven metric it tracks:

- **personal best** — your best *daily typical*, so one lucky reading right after
  a reboot does not set an unreachable bar;
- **adaptive target** — 5% beyond that best, always in the improving direction;
- **adaptive baseline** — your worst daily typical;
- **today's typical** — the mean of today's samples.

Structural metrics — disk free space, startup entries, hardware faults — are
deliberately excluded. Adapting them would add noise, not signal.

---

## 7. Recommendations reference

Twenty rules. Each fires only when the evidence is present, because a dashboard of
unconditional advice trains people to ignore it.

### 7.1 Memory

| Recommendation | Fires when |
|---|---|
| Commit charge exceeds physical RAM | Commitments are larger than RAM, so compression or paging must satisfy them. Critical above 85% of the commit limit. |
| Memory Compression is holding a large working set | The compression store exceeds 4% of RAM. It only grows when RAM is oversubscribed, and every hit costs CPU to decompress. |
| Available physical memory is low | Below 15% of RAM. Below roughly 10% the memory manager spends its time reclaiming instead of serving. |
| A single process dominates memory use | One application holds more than 12% of RAM. Usually the cheapest fix available: restart that one app. |

### 7.2 Startup and maintenance

| Recommendation | Fires when |
|---|---|
| Too many programs start automatically | Enabled auto-start entries exceed target. Lists the actual entries. Disabling is reversible and does not uninstall anything. |
| The machine has not been restarted in a long time | Uptime over seven days. A restart clears leaked handles, pool growth and pending updates. |
| A restart is pending | Windows has set a reboot flag from servicing. Until you restart, components run in a mixed state. |
| Open handle count is above your baseline | A steady climb that never falls back points at a process that opens resources without releasing them. |

### 7.3 Browser

| Recommendation | Fires when |
|---|---|
| Browser-engine processes are above target | Above the floor-derived target. Names the resident extension processes and how much memory they hold, since extensions are the biggest lever. |

### 7.4 Storage

| Recommendation | Fires when |
|---|---|
| System volume is running out of free space | Below 15% free; critical below 7%. Windows needs free space for the pagefile, servicing, temp files and the search index. |
| Pagefile usage has approached its allocated size | Peak usage within 20% of the allocation. With a fixed-size pagefile the next spike fails outright and applications crash. |

### 7.5 Network

| Recommendation | Fires when |
|---|---|
| More background network connections than necessary | Remote connections above target. Includes a command that groups connections by owning process so you can see who is talking. |

### 7.6 Drivers and hardware

| Recommendation | Fires when |
|---|---|
| Devices are reporting driver fault codes | Any device with a driver-specific Device Manager error. Graphics, storage and network fallbacks are a common cause of unexplained slowness. |
| Third-party drivers older than four years are installed | Old drivers miss later power-management and stability fixes. Prioritise storage, chipset, graphics and network. |
| Hardware faults detected | Device Manager errors or a disk not reporting Healthy. An unhealthy disk is a data-loss risk before it is a performance problem. |
| Battery capacity has degraded significantly | Full-charge capacity below 70% of design. Firmware caps processor power on a worn battery, which reads as sudden slowness when unplugged. |

### 7.7 Power and platform

| Recommendation | Fires when |
|---|---|
| A power-saving plan is active | Power saver caps processor frequency and delays turbo — the most common cause of a machine that is slow for no visible reason. |
| Real-time protection is reported as disabled | Reported for completeness, not as a performance tip. **Never disable protection to gain performance.** |
| Windows Search is not running | With the index down, searches fall back to scanning the filesystem. |

### 7.8 WSL 2

| Recommendation | Fires when |
|---|---|
| WSL 2 is running without a memory cap | No `.wslconfig` exists. WSL may claim up to half of RAM and not return it. Includes a ready-made file sized for your machine. |
| Automatic memory reclaim is not enabled | `.wslconfig` exists without `autoMemoryReclaim`, so a single build can hold gigabytes for the rest of the day. |

---

## 8. Configuration

### 8.1 Where the file is

```powershell
sysperf --where
```

Typically `%LOCALAPPDATA%\SysPerf\sysperf.config.json`. Every key is optional;
anything omitted keeps its default. Restart SysPerf after editing.
`sysperf.config.example.json` in the source folder is a fully annotated copy.

### 8.2 Server and sampling

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | Bind address. Anything other than loopback requires a token — see [section 9](#9-privacy-and-security). |
| `port` | `5056` | Listening port. |
| `auth_token` | *(empty)* | Shared secret. Generated automatically if you set a non-loopback host. |
| `open_browser` | `false` | Open the dashboard on start. |
| `sample_seconds` | `20` | Fast-tier cadence. Minimum 5. |
| `full_every` | `15` | Run the slow tier every Nth sample. Raise it if samples feel expensive. |
| `max_samples` | `720` | Samples kept in memory (720 × 20 s = 4 hours). |
| `history_max_bytes` | `12000000` | History file is trimmed above this size. |

### 8.3 Scoring

| Key | Default | Meaning |
|---|---|---|
| `score_smooth_window` | `15` | Samples in the rolling median. `1` scores the raw latest value. |
| `smooth_window_overrides` | `{"net_remote": 180}` | Per-metric windows for bursty metrics. |
| `regress_band_pct` | `2.0` | Noise dead-band before a metric counts as regressed. |
| `targets.*` | see table in [6.2](#62-where-targets-come-from) | Fractions of your capacity used to derive targets. Raise to loosen, lower to tighten. |

Editing `targets` only affects newly derived targets. Run `sysperf --recalibrate`
to apply changes to an existing baseline.

### 8.4 Browser ownership

The setting most worth editing. It tells SysPerf which applications' embedded
browser processes you cannot close:

```json
{
  "browser_hosts": {
    "mandatory": {
      "OneDrive": "File sync",
      "SearchHost": "Windows Start / Search",
      "PanGPA": "Enterprise VPN client",
      "MyCorpAgent": "Required by IT"
    },
    "in_use": {
      "ms-teams": "Microsoft Teams",
      "Code": "Visual Studio Code"
    }
  }
}
```

Keys are host process names without `.exe`, matched case-insensitively. Anything
unlisted counts as discretionary, which inflates the number SysPerf expects you to
reduce — so if a mandated corporate agent shows up in the discretionary group, add
it here.

### 8.5 Hardware watchlist

Windows cannot detect a fault it does not consider a fault. Record those yourself
in `hw_watchlist.json` in the data directory:

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

`severity` is `critical`, `major`, `minor` or `low`. Set `status` to `resolved` to
keep the record without counting it as open. The file is re-read on every request,
so edits appear immediately.

### 8.6 Advisor and privacy

| Key | Default | Meaning |
|---|---|---|
| `advisor.enabled` | `true` | Turn recommendations off. |
| `advisor.stale_driver_years` | `4` | Age at which a third-party driver counts as stale. |
| `advisor.reboot_after_hours` | `168` | Uptime at which a restart is suggested. |
| `advisor.max_recommendations` | `20` | Cap on recommendations shown. |
| `anonymize` | `false` | Replace application and device names with stable hashes. |

### 8.7 Environment variables

Override the config file without editing it — useful for scripts and testing.

| Variable | Effect |
|---|---|
| `SYSPERF_DATA_DIR` | Where state is stored. |
| `SYSPERF_CONFIG` | Path to the configuration file. |
| `SYSPERF_HOST`, `SYSPERF_PORT` | Bind address and port. |
| `SYSPERF_TOKEN` | Authentication token. |
| `SYSPERF_SAMPLE_SECONDS` | Sampling cadence. |
| `SYSPERF_ANONYMIZE` | `1` to anonymise names. |
| `SYSPERF_PS_EXE` | Full path to `powershell.exe` if it is somewhere unusual. |

---

## 9. Privacy and security

### 9.1 Nothing leaves your machine

SysPerf has no outbound network code beyond the local HTTP server. Nothing is
uploaded, no telemetry is sent, and there are no accounts. The dashboard loads no
third-party scripts, fonts or images — after installation the whole tool works
with networking switched off.

### 9.2 Read-only

Every system call the collector makes is a query: `Get-CimInstance`,
`Get-Process`, `Get-ItemProperty`, `pnputil /enum-drivers`,
`powercfg /getactivescheme`. The collector is a single readable PowerShell file —
`src\sysperf\scripts\collect_win.ps1` — and you are encouraged to read it.

The only thing SysPerf writes is its own state, in its own data directory:
`baseline.json`, `history.jsonl`, `adaptive_state.json`, `sysperf.config.json` and
`hw_watchlist.json`.

### 9.3 Treat the API as sensitive

`/api/metrics` describes your machine in detail: running process names, top memory
consumers, installed drivers, enabled startup entries, connection counts, hostname
and model. That is a useful reconnaissance picture, which is exactly why the
server binds to loopback by default.

### 9.4 If you expose it on a network

Setting `host` to anything other than loopback makes SysPerf generate an
authentication token, store it in the configuration file, and require it on every
request — as an `X-SysPerf-Token` header or a `?token=` parameter. It will not
serve unauthenticated telemetry to a network.

There is **no TLS**. For anything beyond a trusted LAN, put a reverse proxy in
front of it that terminates TLS. An internet-facing deployment is not a supported
configuration.

### 9.5 Sharing screenshots

Set `"anonymize": true` and restart. Application, device and machine names are
replaced with stable hashes; the numbers are untouched, so the dashboard still
renders and a bug report is still useful.

### 9.6 Reporting a vulnerability

See `SECURITY.md`. Please use a private security advisory rather than a public
issue.

---

## 10. Command-line reference

```
sysperf                     Start the dashboard.
sysperf --port 5100         Serve on another port.
sysperf --host 0.0.0.0      Bind to all interfaces (generates and requires a token).
sysperf --open              Open the dashboard in your browser once it is up.
sysperf --report            Print a text report with recommendations, then exit.
sysperf --once              Take one sample, print the raw JSON, then exit.
sysperf --recalibrate       Reset the baseline to the current state.
sysperf --where             Show the data directory, config path and runtime.
sysperf --verbose           Debug logging.
sysperf --version           Print the version.
```

`--report` needs no browser and is ideal for a quick check, for a support ticket,
or for a scheduled snapshot:

```powershell
sysperf --report | Out-File "$env:USERPROFILE\Desktop\sysperf-report.txt"
```

`--once` prints everything the collector gathered, which is what to attach to a
bug report about collection. Set `"anonymize": true` first if it contains
application names you would rather not share.

---

## 11. API reference

Every endpoint is `GET` and returns JSON, on `http://127.0.0.1:5056` by default.

| Endpoint | Returns |
|---|---|
| `/api/metrics` | Everything: latest sample, chart series, scorecard, browser breakdown, hardware, recommendations, profile, baseline, metadata. |
| `/api/advice` | Just the prioritised recommendations. |
| `/api/profile` | Machine identity, the stored baseline, and the derived criteria. |
| `/api/health` | Liveness, sample count, whether a baseline exists, last collector error. |
| `/api/config` | Effective configuration with the token removed. |
| `POST /api/recalibrate` | Resets the baseline; requires `X-SysPerf-CSRF` from `/api/fixes`. |

A snapshot is cached for three seconds, so polling is cheap.

```powershell
# Current overall score
(Invoke-RestMethod http://127.0.0.1:5056/api/metrics).scorecard.overall_improvement

# Just the critical and high recommendations
(Invoke-RestMethod http://127.0.0.1:5056/api/advice).items |
  Where-Object severity -in 'critical','high' |
  Select-Object severity, title
```

---

## 12. Troubleshooting

**The dashboard does not load.**

```powershell
Get-ScheduledTask -TaskName 'SysPerf Dashboard'
& "$env:LOCALAPPDATA\SysPerf\sysperf.cmd"     # run in the foreground to see errors
```

**"collector produced no output".** PowerShell could not run the collector. Try it
directly — the error it prints is the real cause:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File "$env:LOCALAPPDATA\SysPerf\app\src\sysperf\scripts\collect_win.ps1" -Full
```

**Everything reads 0%.** Expected on the first day. See
[section 4.2](#42-why-everything-reads-0).

**Progress looks worse than the live value.** Progress is computed from the
smoothed value, not the latest sample. See [section 6.3](#63-smoothing).

**A criterion is stuck on "measuring".** The WSL trend needs six samples over ten
minutes. Leave it running.

**Samples take several seconds.** The full sweep enumerates services and drivers.
Raise `full_every` to run it less often.

**A target looks unreachable.** Check whether the criterion is bounded by a
physical floor — the browser panel shows its floor arithmetic explicitly. If a
mandated corporate application is being counted as discretionary, add it to
`browser_hosts` ([section 8.4](#84-browser-ownership)). Otherwise loosen the
relevant fraction under `targets` and recalibrate.

**Port already in use.** Reinstall with `-Port 5100`, or set `port` in the config
file and restart.

**Battery wear shows nothing.** Design and full-charge capacity come from a WMI
namespace some firmware does not expose. Charge and status still work.

**High CPU from SysPerf itself.** The sampler should be idle between samples. If it
is not, run with `--verbose` and check for a collector that is timing out and
retrying; raising `sample_seconds` is the quick mitigation.

---

## 13. Frequently asked questions

**Does SysPerf slow my machine down?** A fast sample is a few hundred milliseconds
of PowerShell every twenty seconds. The full sweep costs a few seconds every
fifteenth sample. The API caches its snapshot, so an open dashboard is nearly free.

**Why does it not just fix things?** Because then you would not know what changed.
Silent registry and service edits are exactly why "optimiser" tools have the
reputation they do. Recommendations tell you what to do, what it affects, and
whether it can be undone.

**Can I trust the recommendations?** Each one states what it measured, so you can
check the reasoning rather than take it on faith. None of them are secret tweaks:
they are the changes a competent administrator would make, driven by measurements
from your machine.

**Will it work on a managed corporate laptop?** Yes — that is a design target. No
elevation is needed, and if policy blocks scheduled-task creation the installer
falls back to your Startup folder. Note that some recommendations, such as
disabling a security agent's startup entry, are not yours to act on; SysPerf says
so where it can.

**Why is my browser-process target so high?** Because it is derived from what your
machine actually requires. If file sync, Start menu search, a VPN client and a
chat app each run embedded browser processes, the floor is genuinely high and
pretending otherwise would just make the row permanently red.

**Can I run it on several machines?** Yes, independently. There is no aggregation
layer, but the JSON API is stable enough to scrape into one if you want to build it.

**Does the dashboard need to be open?** No. Collection happens in the background
service; the dashboard is only a view of it. `sysperf --report` needs no browser.

**How much disk does the history use?** Up to about 12 MB, trimmed automatically.

**Can I delete the history and start over?** Yes. Stop SysPerf, delete
`history.jsonl`, `baseline.json` and `adaptive_state.json` from the data
directory, and start it again.

---

## 14. Uninstalling

```powershell
powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

Stops the dashboard, removes the sign-in task, Start Menu entries, the virtual
environment and the application files. Your collected data is **kept** so a later
reinstall still knows your baseline.

To remove everything including the data:

```powershell
powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Purge
```

Because SysPerf never changed a Windows setting, there is nothing to revert. Any
improvement you made by acting on its recommendations stays in place.

---

## 15. Licence and credits

SysPerf is free and open-source software under the **Apache License 2.0**. You may
use, modify and redistribute it, including commercially, provided you retain the
copyright and licence notices. See `LICENSE` and `NOTICE`.

Runtime dependencies: **Flask** (BSD-3-Clause) and **Waitress** (ZPL-2.1), both
installed from PyPI. The dashboard uses no third-party JavaScript or CSS.

Windows, Microsoft Edge, WebView2, OneDrive, Teams and Outlook are trademarks of
Microsoft Corporation. Other product names appear only as process names to be
recognised and imply no affiliation or endorsement.

Contributions are welcome — see `CONTRIBUTING.md`. The one rule that is not
negotiable: SysPerf does not write to the system.

---

## Appendix A. Architecture and data flow

SysPerf is a small Flask application wrapped around one PowerShell collector.
Everything platform-specific is isolated in a single module, which is what lets
the same code run natively on Windows or inside WSL 2.

```
                      ┌────────────────────────────────┐
   Windows APIs  ───► │  scripts/collect_win.ps1       │  one JSON object
   (CIM / WMI,        │  read-only queries only        │  on stdout
    pnputil, netsh)   └───────────────┬────────────────┘
                                      │
                        platform_win.py (interpreter
                        discovery, path translation)
                                      │
   ┌──────────────────────────────────▼──────────────────────────────────┐
   │ collector.py — sampler thread, rolling history, baseline capture,   │
   │                snapshot assembly (cached for snapshot_ttl)          │
   └───┬───────────┬───────────┬───────────┬───────────┬────────────┬────┘
       │           │           │           │           │            │
   profile.py  scoring.py  browser.py  hardware.py  advisor.py  components.py
   (baseline,  (progress,  (WebView2   (faults,     (rules →    (version
    targets,    ceiling,    ownership)  disks,       advice)     currency)
    criteria)   scorecard)              drivers)                netopt.py
                                                               (Wi-Fi overlap,
                                                                fix plan)
       │
   ┌───▼──────────────────────────────────────────────────────────────┐
   │ app.py — Flask routes, token auth, security headers              │
   │ static/index.html — Summary / Details tabs, charts (no JS deps)  │
   └──────────────────────────────────────────────────────────────────┘
```

### A.1 Module responsibilities

| Module | Responsibility |
|---|---|
| `platform_win.py` | Detects native Windows vs WSL 2, locates the PowerShell interpreter, translates paths with `wslpath`, runs the collector. |
| `config.py` | Defaults, JSON overrides, `SYSPERF_*` environment overrides, data-directory resolution, carry-forward and series key lists. |
| `collector.py` | Background sampler, JSONL history, first-run baseline capture, snapshot assembly and caching. |
| `profile.py` | Machine profile, baseline persistence, target derivation from capacity, criteria assembly. |
| `scoring.py` | Progress maths, rolling-median smoothing, the WSL ceiling score, scorecard assembly. |
| `browser.py` | Attributes embedded-browser processes to host applications; computes the closability floor. |
| `hardware.py` | Device faults, disks, battery health, driver inventory, manual watchlist. |
| `adaptive.py` | Personal-best tracking and adaptive stretch targets. |
| `advisor.py` | Rule engine turning measurements into prioritised recommendations. |
| `components.py` | Version-currency advisory for components present on the machine. |
| `netopt.py` | Wi-Fi band/channel overlap analysis and the network optimisation plan. |
| `app.py` | HTTP layer: routes, authentication, hardening headers. |

### A.2 Sampling tiers

Two tiers keep the cost low. The **fast tier** runs every `sample_seconds`
(default 20 s) and gathers memory, CPU, process and Wi-Fi link data. The **slow
tier** (`-Full`) runs every `full_every`-th sample (default every 15th ≈ 5 min)
and adds services, startup entries, network, hardware, drivers, component
versions and the Wi-Fi neighbour scan.

Slow-tier values are **carried forward** between full sweeps (see
`CARRY_FORWARD_KEYS`) so the dashboard shows the last known value instead of a
gap. The first sample is deliberately fast so the dashboard is not blank while a
full sweep runs; the second sample is full, which is when the baseline is
captured.

---

## Appendix B. Collected data reference

Every field below is produced by read-only queries. Nothing is written.

### B.1 Fast tier (every sample)

| Field | Meaning | Source |
|---|---|---|
| `committed_mb`, `commit_limit_mb` | Commit charge and its limit | `Win32_PerfFormattedData_PerfOS_Memory` |
| `available_mb`, `total_ram_mb` | Free and total physical memory | `Win32_OperatingSystem` |
| `compression_mb` | Memory Compression working set | `Get-Process` |
| `vmmem_mb` | WSL 2 VM working set | `Get-Process vmmemWSL`/`vmmem` |
| `pool_paged_mb`, `pool_nonpaged_mb` | Kernel pools | Perf CIM class |
| `cache_faults_s`, `page_faults_s` | Fault rates | Perf CIM class |
| `handles`, `threads`, `processes` | System-wide counts | `Get-Process`, `Win32_OperatingSystem` |
| `cpu_pct`, `cpu_queue` | Utilisation and run-queue depth | Perf CIM classes |
| `edge_procs`, `wv2_procs`, `browser_procs` | Browser-engine process counts | `Win32_Process` |
| `edge_main/renderer/extension/utility`, `edge_ext_mb` | Browser composition | Command-line inspection |
| `wv2_hosts` | Embedded-browser processes per host app | Parent-chain walk |
| `svchost_procs`, `search_procs` | Service-host and indexer counts | `Get-Process` |
| `system_drive`, `c_free_gb`, `c_size_gb` | System volume (never assumes `C:`) | `Win32_LogicalDisk` |
| `top_mem`, `top_handles` | Largest consumers by memory and handles | `Get-Process` grouped |
| `wifi` | SSID, link rate, radio, band/channel, signal, category | `Get-NetAdapter`, `Get-NetConnectionProfile`, `netsh wlan` |
| `wifi_signal` | Signal percentage, for the trend chart | `netsh wlan` (Location permitting) |

### B.2 Slow tier (`-Full`)

| Field | Meaning |
|---|---|
| `hostname`, `machine_vendor`, `machine_model`, `cpu_name`, `cpu_cores`, `cpu_threads`, `os_caption`, `os_build`, `os_version` | Machine profile |
| `services_running`, `services_total` | Service inventory |
| `startup_apps`, `startup_apps_total`, `startup_list` | Enabled auto-start entries (disabled ones excluded via `StartupApproved`) |
| `net_remote`, `net_loopback`, `net_established`, `net_listen`, `net_tcp_total` | TCP footprint (loopback excluded from scoring) |
| `net_adapters`, `net_category`, `net_ipv4_conn`, `net_ipv6_conn` | Active adapters and connection profile |
| `wifi_scan` | Neighbouring access points (band, channel, signal) for overlap analysis |
| `gpu_name`, `gpu_driver`, `gpu_driver_date` | Graphics driver |
| `net_driver_name`, `net_driver_ver`, `net_driver_date`, `net_driver_provider` | Network adapter driver |
| `ps_version`, `pwsh_version` | Windows PowerShell and PowerShell 7 |
| `edge_version`, `webview2_version` | Browser and embedded runtime |
| `bios_version`, `bios_date` | Firmware |
| `pagefile_mb`, `pagefile_peak_mb`, `pagefile_automatic` | Pagefile configuration |
| `power_plan`, `power_plan_guid` | Active power plan (GUID because names are localised) |
| `restart_pending` | Pending-reboot registry markers |
| `defender_realtime`, `defender_av_signature_age_d` | Protection status |
| `hw_problem_devices`, `hw_problem_list`, `hw_disks`, `hw_disks_unhealthy`, `hw_battery`, `hw_pointing`, `hw_touchpad` | Hardware health |
| `drv_total`, `drv_unsigned`, `drv_stale`, `drv_stale_list`, `drv_problem`, `drv_problem_list` | Driver inventory and faults |
| `wsl_installed`, `wsl_config_present`, `wsl_cap_mb`, `wsl_cpus_cfg`, `wsl_autoreclaim` | WSL configuration |

### B.3 Deliberate collection choices

* **Locale independence.** Performance data comes from
  `Win32_PerfFormattedData_*` CIM classes rather than `Get-Counter`, whose
  counter paths are localised and fail on non-English Windows.
* **No assumed drive letter.** The system volume is read from
  `%SystemDrive%`.
* **Power plans by GUID.** Display names are localised and cannot be matched
  reliably.
* **Driver inventory via `pnputil /enum-drivers`.** `Win32_PnPSignedDriver` is
  the obvious alternative and is slow enough to time the sample out.
* **Environment-based program paths.** Edge and PowerShell 7 are located through
  `%ProgramFiles%`, `%ProgramFiles(x86)%`, `%ProgramW6432%` and `%LOCALAPPDATA%`
  with a registry fallback, so a non-`C:` Windows install works.
* **PowerShell 5.1 compatible.** The collector avoids PS7-only syntax, so it
  runs on a stock Windows 11 machine; PowerShell 7 is used automatically when
  present.

---

## Appendix C. Scoring mathematics

### C.1 Criterion progress

For a normal criterion, progress is the fraction of the baseline → target
journey completed, clamped to 0–100:

```
progress = clamp( (current − baseline) / (target − baseline) × 100 , 0 , 100 )
```

If the target is met, progress is forced to 100 — otherwise a metric that was
already better than its achievable floor at baseline time would report "met"
alongside 0 %.

**Smoothing.** Counts breathe constantly, so the *sustained* value is scored:
the rolling median of the last `score_smooth_window` samples (default 15 ≈ 5
min). Per-metric overrides exist (`smooth_window_overrides`); carry-forward
metrics use the raw latest value, because a short median would only return a
stale earlier step. Live gauges and charts always show the raw latest value.

**Dead-band.** A metric within `regress_band_pct` (default 2 %) of its baseline
counts as unchanged rather than regressed. Without it, every criterion reads
"regress" on day one, when the baseline *is* the current value.

### C.2 Ceiling criteria (WSL memory)

"At or below the cap **and not growing**" cannot use the formula above, because a
cap normally sits *above* the starting value. Instead the working-set slope is
estimated by least squares over the last `window_seconds`, and the score is based
on remaining runway:

* above the cap → 0, `regress`;
* insufficient samples or span → `measuring` (excluded from the overall);
* flat or falling → 100, `met`;
* rising with ≥ 12 h of headroom and under half the cap → 100, `met` (churn, not
  a leak);
* rising with ≥ 3 h of headroom → 50–95, `partial`;
* otherwise → `regress`.

The window is re-anchored at a genuine VM restart or working-set reset (detected
as a collector gap or a collapse from a high steady state) so the recovery ramp
afterwards does not read as a leak.

### C.3 Target derivation

Targets are ratios of the machine's own capacity, never absolute constants (see
`config["targets"]`), then clamped so a target is always an improvement on the
baseline and never crosses a physical floor. Examples: commit charge ≤ 75 % of
RAM, available ≥ 30 % of RAM, free space ≥ 15 % of the volume, browser processes
≥ the measured non-closable floor plus headroom.

### C.4 The 10-category Summary score

The Summary distils everything into ten categories scored 0–100. Categories that
map to a criterion use that criterion's progress; the rest are computed from live
values. **Room for improvement is `100 − score`.**

| Category | How it is scored |
|---|---|
| Memory pressure | Mean progress of commit, compression and available memory |
| WSL 2 | The vmmem ceiling score, or headroom against the configured cap |
| Browser footprint | Browser-process criterion progress |
| Application handles | Handle criterion progress |
| CPU & processes | `100 − CPU%`, less a penalty when the run queue is non-zero |
| Storage | Mean of free-space progress and disk health |
| Network optimisation | Mean of remote-TCP and startup-entry progress, less a penalty for channel overlap |
| Wi-Fi connectivity | Signal percentage, or a band from the negotiated link rate when Location is denied |
| Hardware & drivers | 100 less penalties for device faults, driver faults, stale drivers and unhealthy disks |
| Component currency | Share of detected components on their recommended version |

The overall score is the mean of the categories that could be evaluated;
`measuring` categories are excluded rather than assumed good. **The Details
header and the Summary tab display the same overall number** — both call one
shared computation, so they cannot disagree.

### C.5 Adaptive (personal-best) layer

Independently of the fixed criteria, SysPerf tracks the best value you have
actually achieved per metric and sets a stretch goal slightly beyond it
(`adaptive.stretch`, default 5 %). It reports today's typical value, whether you
are at or beat your personal best, and a self-improvement percentage. It needs
`adaptive.learn_samples` observations before it will score a metric.

---

## Appendix D. Wi-Fi and network analysis internals

### D.1 Why two data sources

`Get-NetAdapter` and `Get-NetConnectionProfile` work without elevation or any
privacy permission, and yield the SSID, negotiated link rate, adapter model,
network category and IPv4 connectivity. The richer detail — signal percentage,
band, channel, Rx/Tx rates and the neighbour scan — comes from `netsh wlan`,
which on Windows 11 is gated behind **Location** permission for the WLAN service
and may also require elevation.

When `netsh` is unavailable SysPerf sets `detail_limited` and the dashboard shows
adapter-level data plus an explanation of what to enable. It never reports a
fabricated signal value.

### D.2 Channel overlap model

* **2.4 GHz** — 20 MHz channels are spaced 5 MHz apart, so two access points
  overlap when their channel numbers differ by less than 5. Only **1, 6 and 11**
  are mutually non-overlapping. Each candidate is scored by signal-weighted
  neighbour load, and the clearest is recommended.
* **5 / 6 GHz** — 20 MHz channels do not overlap, so congestion is simply the
  co-channel count; the least-used common channel is recommended.

### D.3 Why SysPerf cannot "fix" overlap

Channel and channel width are **access-point** settings. A client can only choose
which band to prefer and which access point to join. The optimisation plan
therefore tags every action by where it applies — `client`, `router` or
`windows` — and SysPerf changes none of them.

---

## Appendix E. Security and privacy model

| Property | Behaviour |
|---|---|
| System writes | None. Every system call is a query. The only file writes are inside SysPerf's own data directory. |
| Bind address | `127.0.0.1` by default. |
| Non-loopback binds | A token is generated and **required** — the API is never silently exposed unauthenticated. Sent as `X-SysPerf-Token` or `?token=`. |
| Token comparison | Constant-time (`secrets.compare_digest`). |
| TLS | None. Use a reverse proxy for anything beyond a trusted host. |
| Response headers | `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Cache-Control: no-store`, and a restrictive `Content-Security-Policy`. |
| Third-party assets | None. No CDN, no external JavaScript or CSS; the dashboard works offline. |
| Telemetry | None. No accounts, no network egress. |
| `anonymize` | Replaces process, host and device names with stable hashes in the API payload, so the dashboard can be screen-shared safely. |
| Runtime data | History, baseline, adaptive state and config live in the per-user data directory and are excluded from version control. |
| `POST /api/recalibrate` | Rewrites SysPerf’s baseline; requires a CSRF header. Optional `/api/fix` is also a POST route. |

### E.1 What the data contains

The payload describes your machine: process names, installed drivers, startup
entries, connection counts, hostname, model and the Wi-Fi SSID. It never leaves
the machine unless you expose the port or share the output. Enable `anonymize`
before sharing screenshots or bug reports.

---

## Appendix F. Performance characteristics

| Aspect | Typical |
|---|---|
| Fast sample | Well under a second |
| Full sweep | Seconds to tens of seconds, dominated by WMI/CIM and `pnputil` latency rather than by SysPerf |
| API snapshot | Cached for `snapshot_ttl` (3 s), so polling is cheap |
| Memory | A few tens of MB for the Python process |
| History | Ring buffer of `max_samples` in memory; JSONL trimmed at `history_max_bytes` |

The full sweep runs on a background thread, so its duration never blocks the
dashboard or the API. If a sweep fails, the previous slow-tier values are carried
forward and `meta.error` reports the reason.

---

## Appendix G. Extending SysPerf

* **Retune thresholds** — edit `sysperf.config.json`; nothing needs recompiling.
* **Adjust browser ownership** — add host process names under
  `browser_hosts.mandatory` / `in_use` so the closability floor matches your
  environment.
* **Change the component targets** — edit `CATALOG` in `components.py`. Only
  components detected on the machine are listed, so adding an entry is safe.
* **Add a criterion** — add a spec row in `profile.CRITERIA_SPEC`, derive its
  target in `profile.derive_targets`, and collect the field in
  `collect_win.ps1`. The scorecard, KPI cards and category score pick it up
  automatically.
* **Add a recommendation** — add a rule in `advisor.py`.
* **Run the tests** — `python -m pytest` from the project root.
