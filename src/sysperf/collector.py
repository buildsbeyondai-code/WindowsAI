"""
Background sampler + snapshot assembler.

The Collector polls Windows metrics (via the fixed PowerShell script) and WSL
/proc, persists a rolling JSONL history, and assembles the API snapshot by
delegating to the scoring / browser / hardware modules. The adaptive scorer is
injected so this module owns no global state.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import config as _cfg
from . import machine_profile
from .background_process import run_hidden
from .browser import browser_breakdown
from .components import build_components
from .config import (
    CAN_COLLECT,
    CARRY_FORWARD_KEYS,
    COLLECT_SCRIPT,
    COLLECT_TIMEOUT_FULL_S,
    COLLECT_TIMEOUT_S,
    FULL_EVERY,
    HISTORY_FILE,
    HISTORY_MAX_BYTES,
    MAX_SAMPLES,
    PLATFORM,
    PS_EXE,
    REVISION,
    SAMPLE_SECONDS,
    SERIES_KEYS,
    SNAPSHOT_TTL,
    WIFI_CAPABILITY_PCTL,
    WIFI_LINK_SMOOTH_WINDOW,
    WIFI_MIN_CAPABILITY_SAMPLES,
    WIFI_NETWORK_PLAN_MBPS,
    WIFI_PHY_EFFICIENCY,
)
from .hardware import build_hardware
from .netopt import analyze_overlap, optimisation_plan
from .platform_paths import to_host_path
from .pwsh_session import SessionError
from .scoring import build_scorecard, vmmem_trend

log = logging.getLogger("perfdash")


def _median(vals: list) -> float:
    vals = sorted(vals)
    n = len(vals)
    mid = n // 2
    return float(vals[mid]) if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _pctl(vals: list, q: float) -> float:
    vals = sorted(vals)
    return float(vals[min(len(vals) - 1, int(len(vals) * q))])


def _with_wifi_typical(latest: dict, hist: list[dict]) -> dict:
    """Score the Wi-Fi link against the network it is attached to.

    Two things make a single ``link_mbps`` reading useless on its own:

    * rate adaptation renegotiates continuously (band, channel width, MCS and
      spatial streams), so the instantaneous value swings wildly — every other
      criterion here is judged on a rolling median, and this applies the same rule;
    * the same laptop roams between networks of completely different capacity, so
      samples are scoped to the *current SSID* and never pooled across networks.

    The score answers "is the radio the bottleneck for this network?", not "is this
    a fast link in the abstract":

    * a declared plan (``WIFI_NETWORK_PLAN_MBPS``) is the reference where known —
      usable throughput (PHY x efficiency) against the subscribed capacity, capped
      at 100 once the link can carry the whole plan;
    * otherwise the network's own learned capability (p90 of its sustained rates)
      is the reference, so a new network calibrates itself.

    ``link_mbps`` is left untouched for the Details view. All derived fields are
    omitted when there is not enough same-SSID history, so the caller shows "—"
    instead of a shaky number.
    """
    wifi = latest.get("wifi")
    if not isinstance(wifi, dict):
        return latest
    ssid = wifi.get("ssid")

    def rates(samples):
        out = []
        for s in samples:
            w = s.get("wifi")
            if not isinstance(w, dict) or w.get("ssid") != ssid:
                continue
            r = w.get("link_mbps")
            if isinstance(r, (int, float)):
                out.append(float(r))
        return out

    window = rates(hist[-WIFI_LINK_SMOOTH_WINDOW:])
    if not window:
        return latest

    typical = _median(window)
    info = {"link_mbps_typical": round(typical, 1),
            "link_mbps_samples": len(window)}

    plan = WIFI_NETWORK_PLAN_MBPS.get(ssid)
    usable = typical * WIFI_PHY_EFFICIENCY
    info["usable_mbps"] = round(usable, 1)

    if plan:
        info["plan_mbps"] = plan
        info["score_basis"] = "plan"
        info["score"] = int(round(max(0.0, min(100.0, usable / plan * 100.0))))
    else:
        allr = rates(hist)
        if len(allr) >= WIFI_MIN_CAPABILITY_SAMPLES:
            cap = _pctl(allr, WIFI_CAPABILITY_PCTL)
            info["capability_mbps"] = round(cap, 1)
            info["score_basis"] = "capability"
            info["score"] = (int(round(max(0.0, min(100.0, typical / cap * 100.0))))
                             if cap else None)
        else:
            info["score_basis"] = "calibrating"
            info["score"] = None

    out = dict(latest)
    out["wifi"] = {**wifi, **info}
    return out


def _parse_probe(raw: str) -> dict:
    """The metrics object from the probe's output.

    The script prints exactly one compact JSON line, but a persistent shell can
    interleave a stray warning line, so the last line that looks like an object
    is taken when the whole text is not valid JSON.
    """
    raw = raw.replace("\r", "").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        objs = [ln for ln in raw.splitlines() if ln.startswith("{")]
        if not objs:
            raise
        data = json.loads(objs[-1])
    if not isinstance(data, dict) or not data:
        raise ValueError('PowerShell probe did not return a metrics object')
    return data


class Collector:
    def __init__(self, scorer, store=None, shell=None) -> None:
        self.scorer = scorer
        # Optional SQLite history (historydb.HistoryStore). Without one the
        # rolling JSONL file is used, exactly as before 4.1.
        self.store = store
        # Optional persistent PowerShell (pwsh_session.PwshSession). Without one
        # every sample spawns a fresh process.
        self.shell = shell
        self.shell_fallbacks = 0
        self.sample_failures = 0
        self.last_ok_at = None
        # Optional automatic target-realism override store (target_review.py).
        # Injected after construction by app.py; None in unit tests, which keeps
        # the scorecard on the plan targets exactly as before.
        self.target_store = None
        # The probe script has to be named in a form the Windows-side PowerShell
        # can open. Inside WSL that needs wslpath; natively it is already correct.
        # This used to call wslpath unconditionally at construction, so importing
        # the module outside WSL raised before the service could even report why.
        self._ps_script_win = to_host_path(COLLECT_SCRIPT, PLATFORM)
        # Surfaced immediately rather than once per failed sample: on a platform
        # with no PowerShell the dashboard should say why it has no data instead
        # of looking like it is still warming up.
        self.error = None if CAN_COLLECT else (
            "no PowerShell found on this platform — SysPerf measures a Windows "
            "host. Set SYSPERF_PS_EXE, or see `sysperf --where`.")
        self.history: deque[dict] = deque(maxlen=MAX_SAMPLES)
        self.latest: dict = {}
        self.sample_count = 0
        self.last_duration = 0.0
        self.lock = threading.Lock()
        self._snap_cache: dict | None = None
        self._snap_cache_t = 0.0
        # Wall time this collector came up. Anything older belongs to a previous
        # session: _load_history() seeds `latest` from the last persisted sample,
        # so without this marker a restart presents hours-old data as "current"
        # and consumers (the lock policy in particular) act on it.
        self.started_at = time.time()
        self.seeded_latest = False
        self._load_history()
        # A machine with history but no baseline yet (first run of this version,
        # or a new device) captures one straight away instead of waiting.
        machine_profile.maybe_capture(list(self.history))

    # ------------------------------------------------------------- history ---
    def _load_history(self) -> None:
        if self.store is not None:
            try:
                self.history.extend(self.store.recent(MAX_SAMPLES))
            except Exception as exc:
                log.warning("could not load history: %s", exc)
            self._seed_from_history()
            return
        if not HISTORY_FILE.exists():
            return
        try:
            lines = HISTORY_FILE.read_text().splitlines()[-MAX_SAMPLES:]
            for ln in lines:
                try:
                    historical = json.loads(ln)
                    if isinstance(historical, dict):
                        self.history.append(historical)
                except json.JSONDecodeError:
                    continue
            self._seed_from_history()
        except OSError as exc:
            log.warning("could not load history: %s", exc)

    def _seed_from_history(self) -> None:
        if self.history:
            self.latest = dict(self.history[-1])
            self.seeded_latest = True      # not from this session - see started_at
        log.info("loaded %d historical samples", len(self.history))
        self.scorer.seed(list(self.history))

    def _persist(self, sample: dict) -> None:
        if self.store is not None:
            try:
                self.store.append(sample)
            except Exception as exc:
                log.warning("persist failed: %s", exc)
            return
        try:
            with HISTORY_FILE.open("a") as fh:
                fh.write(json.dumps(sample) + "\n")
            if HISTORY_FILE.stat().st_size > HISTORY_MAX_BYTES:
                keep = HISTORY_FILE.read_text().splitlines()[-MAX_SAMPLES:]
                HISTORY_FILE.write_text("\n".join(keep) + "\n")
        except OSError as exc:
            log.warning("persist failed: %s", exc)

    # -------------------------------------------------------------- sampling --
    def sample(self) -> None:
        # The heavy -Full probe (service inventory + launch-time checks) is much
        # slower than a light one, especially under load. Never run it as the
        # FIRST sample after start-up: that would delay every fresh metric until
        # the slow probe returns (minutes on a pegged host). A light sample first
        # gives fast CPU/memory data immediately; Full then runs every FULL_EVERY.
        full = self.sample_count > 0 and self.sample_count % FULL_EVERY == 0
        started = time.time()
        timeout = COLLECT_TIMEOUT_FULL_S if full else COLLECT_TIMEOUT_S
        try:
            data = _parse_probe(self._probe(full, timeout))
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.sample_failures += 1
            log.warning("sample failed: %s", self.error)
            return

        data["_t"] = time.time()
        if full:                              # when the slow tier (network, services…) was last measured
            data["_full_t"] = data["_t"]
        data["_iso"] = datetime.now(timezone.utc).isoformat()
        # Which computer took this sample, so a data directory that travels to
        # another machine (copy, restore, OneDrive sync) is recognised as foreign.
        data["_host"] = machine_profile.machine_id()
        data["wsl"] = self._wsl_metrics()
        self.last_duration = round(time.time() - started, 2)

        with self.lock:
            if not full and self.latest:      # carry forward slow-tier metrics
                for k in CARRY_FORWARD_KEYS:
                    if k in self.latest and k not in data:
                        data[k] = self.latest[k]
            self.history.append(data)
            self.latest = data
            self.error = None
            self.seeded_latest = False          # `latest` is now a live reading
            self.sample_count += 1
            self.last_ok_at = data["_t"]
            self._snap_cache = None            # invalidate cache on fresh sample
        self._persist(data)
        self.scorer.update(data)
        if machine_profile.maybe_capture(list(self.history)):
            with self.lock:
                self._snap_cache = None

    def _probe(self, full: bool, timeout: float) -> str:
        """Run the collector script once and return its stdout.

        The warm session is tried first; if it fails the sample falls back to a
        fresh process, so a broken session costs one slow sample, not a gap.
        """
        args = ("-Full",) if full else ()
        if self.shell is not None:
            try:
                return self.shell.run(self._ps_script_win, args, timeout)
            except SessionError as exc:
                self.shell_fallbacks += 1
                log.warning("persistent PowerShell failed (%s); using a one-shot process",
                            exc)
        cmd = [PS_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", self._ps_script_win, *args]
        res = run_hidden(cmd, capture_output=True, text=True, timeout=timeout)
        raw = res.stdout.replace("\r", "").strip()
        if not raw:
            raise RuntimeError(res.stderr.strip()[:200] or "empty output")
        return raw

    def stats(self) -> dict:
        """Collector health facts for the diagnostics panel."""
        return {"samples_taken": self.sample_count, "failures": self.sample_failures,
                "last_duration_s": self.last_duration, "last_ok_at": self.last_ok_at,
                "error": self.error, "shell": self.shell.stats() if self.shell else None,
                "shell_fallbacks": self.shell_fallbacks,
                "store": "sqlite" if self.store is not None else "jsonl"}

    @staticmethod
    def _wsl_metrics() -> dict:
        out: dict = {}
        if PLATFORM == "windows":
            # Native Windows has no Linux guest to read; WSL, if installed, is
            # measured from the host side as vmmem_mb.
            return {"available": False}
        try:                                                     # pragma: posix only
            mem = Path("/proc/meminfo").read_text()
            vals = {}
            for line in mem.splitlines():
                parts = line.split(":")
                if len(parts) == 2:
                    vals[parts[0]] = int(parts[1].strip().split()[0])
            out["total_mb"] = round(vals.get("MemTotal", 0) / 1024)
            out["available_mb"] = round(vals.get("MemAvailable", 0) / 1024)
            out["swap_total_mb"] = round(vals.get("SwapTotal", 0) / 1024)
            out["cached_mb"] = round(vals.get("Cached", 0) / 1024)
            out["nproc"] = len([ln for ln in Path("/proc/cpuinfo").read_text()
                                .splitlines() if ln.startswith("processor")])
            out["load1"] = float(Path("/proc/loadavg").read_text().split()[0])
            # The cap is a share of the host's RAM (machine_profile), not a
            # fixed 10 GB that only ever fitted the original laptop.
            out["cap_active"] = out["total_mb"] <= _cfg.VMMEM_CAP_MB * 1.05
        except (OSError, ValueError) as exc:                     # pragma: posix only
            out["error"] = str(exc)
        try:                                                     # pragma: posix only
            res = run_hidden(["docker", "ps", "-q"], capture_output=True,
                                 text=True, timeout=10)
            out["containers_running"] = len([x for x in res.stdout.split() if x])
        except (OSError, subprocess.SubprocessError):            # pragma: posix only
            out["containers_running"] = None
        return out

    def loop(self) -> None:
        while True:
            self.sample()
            time.sleep(SAMPLE_SECONDS)

    # -------------------------------------------------------------- snapshot --
    def _target_overrides(self):
        """Honest target overrides from the auto-realism layer, or None."""
        ts = self.target_store
        if ts is None:
            return None
        try:
            return ts.overrides()
        except Exception:                                # pragma: no cover
            return None

    def snapshot(self) -> dict:
        """Assemble the API payload, cached for SNAPSHOT_TTL to keep CPU flat."""
        now = time.time()
        with self.lock:
            cached = self._snap_cache
            if cached is not None and now - self._snap_cache_t < SNAPSHOT_TTL:
                return {**cached,
                        "meta": {**cached["meta"],
                                 "server_time": datetime.now(timezone.utc).isoformat()}}
            hist = list(self.history)
            latest = dict(self.latest)
            seeded = self.seeded_latest
        fresh_n = len([s for s in hist if (s.get("_t") or 0) >= self.started_at])

        series = {k: [s.get(k) for s in hist] for k in SERIES_KEYS}
        series["t"] = [s.get("_t") for s in hist]
        latest = _with_wifi_typical(latest, hist)
        result = {
            "latest": latest,
            "series": series,
            "scorecard": build_scorecard(latest, hist, self.scorer,
                                         self._target_overrides(),
                                         machine_profile.live_targets(latest)),
            "browser": browser_breakdown(latest),
            "hardware": build_hardware(latest),
            "components": build_components(latest),
            "wifi_overlap": analyze_overlap(latest.get("wifi_scan"), latest.get("wifi")),
            "net_plan": optimisation_plan(latest.get("wifi"),
                                          analyze_overlap(latest.get("wifi_scan"), latest.get("wifi")),
                                          latest),
            "vmmem_trend_mb_per_h": round(vmmem_trend(hist)[0], 1),
            "meta": {
                "samples": len(hist),
                "sample_seconds": SAMPLE_SECONDS,
                "last_duration_s": self.last_duration,
                "error": self.error,
                "server_time": datetime.now(timezone.utc).isoformat(),
                "baseline_date": _cfg.BASELINE_DATE,
                "machine": machine_profile.machine_id(),
                "profile": machine_profile.current(),
                "revision": REVISION,
                # Provenance of `latest`: True while it is the sample replayed
                # from history.jsonl at start-up rather than a live reading.
                "stale_latest": seeded,
                "fresh_samples": fresh_n,
            },
        }
        with self.lock:
            self._snap_cache = result
            self._snap_cache_t = now
        return result
