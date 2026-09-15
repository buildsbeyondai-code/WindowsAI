"""
Background sampler and API snapshot assembler.

The collector polls Windows through the fixed PowerShell script, persists a
rolling JSONL history, captures the machine baseline on first run, and assembles
the API payload by delegating to the scoring / browser / hardware / advisor
modules. All state is instance state — nothing global — so the class is testable
with a stub sampler.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import profile as profile_mod
from .advisor import build_advice
from .browser import browser_breakdown
from .components import build_components
from .config import (
    BASELINE_FILE,
    CARRY_FORWARD_KEYS,
    COLLECT_SCRIPT,
    HISTORY_FILE,
    SERIES_KEYS,
    VERSION,
)
from .hardware import build_hardware
from .netopt import analyze_overlap, optimisation_plan
from .platform_win import run_powershell, runtime_name
from .scoring import build_scorecard, vmmem_trend

log = logging.getLogger("sysperf")


def _anon(name: str) -> str:
    """Stable, non-reversible label used when anonymize is enabled."""
    return "app-" + hashlib.sha256(name.encode("utf-8", "replace")).hexdigest()[:8]


class Collector:
    def __init__(self, cfg: dict, scorer=None, *,
                 history_file: Path | None = None,
                 baseline_file: Path | None = None,
                 script: Path | None = None) -> None:
        self.cfg = cfg
        self.scorer = scorer
        self.history_file = Path(history_file or HISTORY_FILE)
        self.baseline_file = Path(baseline_file or BASELINE_FILE)
        self.script = Path(script or COLLECT_SCRIPT)

        self.history: deque[dict] = deque(maxlen=int(cfg["max_samples"]))
        self.latest: dict = {}
        self.error: str | None = None
        self.sample_count = 0
        self.last_duration = 0.0
        self.started_at = time.time()
        self.lock = threading.RLock()
        self._generation = 0
        self._snap_cache: dict | None = None
        self._snap_cache_t = 0.0
        self.baseline: dict = profile_mod.load_baseline(self.baseline_file)
        self._load_history()

    # ------------------------------------------------------------- history ---
    def _load_history(self) -> None:
        if not self.history_file.exists():
            return
        try:
            keep = int(self.cfg["max_samples"])
            lines = self.history_file.read_text(encoding="utf-8",
                                                errors="replace").splitlines()[-keep:]
            for line in lines:
                try:
                    sample = json.loads(line)
                    if isinstance(sample, dict):
                        self.history.append(sample)
                except json.JSONDecodeError:
                    continue
            if self.history:
                self.latest = dict(self.history[-1])
            log.info("loaded %d historical samples", len(self.history))
            if self.scorer is not None:
                self.scorer.seed(list(self.history))
        except OSError as exc:
            log.warning("could not load history: %s", exc)

    def _persist(self, sample: dict) -> None:
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            with self.history_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(sample) + "\n")
            if self.history_file.stat().st_size > int(self.cfg["history_max_bytes"]):
                keep = int(self.cfg["max_samples"])
                lines = self.history_file.read_text(encoding="utf-8",
                                                   errors="replace").splitlines()[-keep:]
                self.history_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("persist failed: %s", exc)

    # ------------------------------------------------------------- sampling ---
    def read_windows(self, full: bool) -> dict:
        """One PowerShell run. Overridden in tests with a stub."""
        raw = run_powershell(self.script, ["-Full"] if full else None,
                             timeout=int(self.cfg["collector_timeout"]))
        return json.loads(raw)

    def _is_full_sample(self) -> bool:
        """
        Decide whether this sample gathers the slow tier.

        The very first sample is deliberately *fast*. A full sweep enumerates
        services, drivers and hardware and takes tens of seconds, so making it
        first would leave the dashboard blank for that whole time on a new
        install. The second sample is full, which captures the baseline about
        half a minute in, and the cadence continues from there.
        """
        n = self.sample_count
        full_every = int(self.cfg["full_every"])
        if n == 0:
            return False
        return n == 1 or n % full_every == 0

    def sample(self) -> bool:
        """Take one sample. Returns True on success."""
        full = self._is_full_sample()
        started = time.time()
        try:
            data = self.read_windows(full)
            if not isinstance(data, dict) or not data:
                raise ValueError("collector must return a non-empty JSON object")
        except Exception as exc:
            with self.lock:
                self.error = f"{type(exc).__name__}: {exc}"
                self._snap_cache = None
                self._generation += 1
            log.warning("sample failed: %s", self.error)
            return False

        data["_t"] = time.time()
        data["_iso"] = datetime.now(timezone.utc).isoformat()
        guest = self._wsl_guest_metrics()
        if guest:
            data["wsl"] = guest
        self.last_duration = round(time.time() - started, 2)

        with self.lock:
            if not full and self.latest:        # carry the slow tier forward
                for key in CARRY_FORWARD_KEYS:
                    if key in self.latest and key not in data:
                        data[key] = self.latest[key]
            bat = data.get("hw_battery") or {}
            try:
                dc, fc = float(bat.get("design_capacity")), float(bat.get("full_capacity"))
                if dc > 0 and fc >= 0 and math.isfinite(dc) and math.isfinite(fc):
                    data["battery_wear_pct"] = round(max(0.0, (1 - fc / dc) * 100), 1)
            except (TypeError, ValueError, AttributeError):
                pass
            self.history.append(data)
            self.latest = data
            self.error = None
            self.sample_count += 1
            self._snap_cache = None             # invalidate on fresh data
            self._generation += 1
            if self.scorer is not None:
                self.scorer.update(data)
            self._maybe_capture_baseline(data, full)

        self._persist(data)
        return True

    def _maybe_capture_baseline(self, sample: dict, full: bool) -> None:
        """
        Capture the machine's starting state once, on the first full sample.

        Progress is meaningless without a "before" picture, and using someone
        else's numbers is worse than having none. Recalibrate explicitly with
        ``sysperf --recalibrate`` after making improvements you want to keep.
        """
        if self.baseline.get("metrics") or not full:
            return
        self.baseline = profile_mod.capture_baseline(sample)
        profile_mod.save_baseline(self.baseline_file, self.baseline)
        log.info("baseline captured to %s", self.baseline_file)

    def recalibrate(self, sample: dict | None = None) -> dict:
        """Replace the baseline with the current state (explicit user action)."""
        with self.lock:
            src = sample or self.latest
            if not src:
                raise RuntimeError("no sample available to recalibrate from")
            baseline = profile_mod.capture_baseline(src, note="recalibrated by user")
            profile_mod.save_baseline(self.baseline_file, baseline)
            self.baseline = baseline
            self._snap_cache = None
            self._generation += 1
            return copy.deepcopy(baseline)

    @staticmethod
    def _wsl_guest_metrics() -> dict:
        """
        Linux-guest metrics — only available when SysPerf itself runs in WSL.

        On native Windows the WSL panel is driven from the Windows-side facts the
        PowerShell collector reports (vmmem working set, .wslconfig contents), so
        this returning empty is expected and not an error.
        """
        if runtime_name() != "wsl":
            return {}
        out: dict = {"guest": True}
        try:
            vals: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                parts = line.split(":")
                if len(parts) == 2:
                    try:
                        vals[parts[0]] = int(parts[1].strip().split()[0])
                    except (ValueError, IndexError):
                        continue
            out["total_mb"] = round(vals.get("MemTotal", 0) / 1024)
            out["available_mb"] = round(vals.get("MemAvailable", 0) / 1024)
            out["swap_total_mb"] = round(vals.get("SwapTotal", 0) / 1024)
            out["cached_mb"] = round(vals.get("Cached", 0) / 1024)
            out["nproc"] = sum(1 for line in Path("/proc/cpuinfo").read_text().splitlines()
                               if line.startswith("processor"))
            out["load1"] = float(Path("/proc/loadavg").read_text().split()[0])
        except (OSError, ValueError) as exc:
            out["error"] = str(exc)
        return out

    def loop(self) -> None:
        interval = int(self.cfg["sample_seconds"])
        while True:
            self.sample()
            time.sleep(interval)

    # ------------------------------------------------------------- snapshot ---
    def criteria(self) -> list[tuple]:
        return profile_mod.build_criteria(self.baseline, self.latest, self.cfg)

    def snapshot(self) -> dict:
        """Assemble the API payload, cached for snapshot_ttl to keep CPU flat."""
        now = time.time()
        ttl = float(self.cfg["snapshot_ttl"])
        with self.lock:
            cached = self._snap_cache
            if cached is not None and now - self._snap_cache_t < ttl:
                meta = {**cached["meta"],
                        "server_time": datetime.now(timezone.utc).isoformat()}
                return copy.deepcopy({**cached, "meta": meta})
            hist = list(self.history)
            latest = copy.deepcopy(self.latest)
            baseline = copy.deepcopy(self.baseline)
            generation = self._generation

        criteria = profile_mod.build_criteria(baseline, latest, self.cfg)
        scorecard = build_scorecard(latest, hist, criteria, self.cfg, self.scorer)
        prof = profile_mod.build_profile(latest, baseline)
        browser = browser_breakdown(latest, self.cfg)
        target_row = next((r for r in scorecard["rows"] if r["key"] == "browser_procs"), None)
        browser["note"] = profile_mod.browser_target_note(
            latest, self.cfg, target_row["target"] if target_row else None)

        series = {k: [s.get(k) for s in hist] for k in SERIES_KEYS}
        series["t"] = [s.get("_t") for s in hist]

        result = {
            "latest": latest,
            "series": series,
            "scorecard": scorecard,
            "browser": browser,
            "hardware": build_hardware(latest),
            "advice": build_advice(latest, scorecard, self.cfg, prof),
            "components": build_components(latest),
            "wifi_overlap": analyze_overlap(latest.get("wifi_scan"), latest.get("wifi")),
            "net_plan": optimisation_plan(latest.get("wifi"),
                                          analyze_overlap(latest.get("wifi_scan"), latest.get("wifi")),
                                          latest),
            "profile": prof,
            "baseline": baseline,
            "vmmem_trend_mb_per_h": round(vmmem_trend(hist, self.cfg.get("trend") or {})[0], 1),
            "meta": {
                "app": "SysPerf",
                "version": VERSION,
                "runtime": runtime_name(),
                "samples": len(hist),
                "sample_seconds": int(self.cfg["sample_seconds"]),
                "last_duration_s": self.last_duration,
                "error": self.error,
                "server_time": datetime.now(timezone.utc).isoformat(),
                "uptime_s": round(now - self.started_at),
                "baseline_captured": baseline.get("captured"),
                "baseline_note": baseline.get("note"),
                "criteria_count": len(criteria),
                "anonymized": bool(self.cfg.get("anonymize")),
            },
        }
        if self.cfg.get("anonymize"):
            result = _anonymize(result)

        with self.lock:
            if generation == self._generation:
                self._snap_cache = copy.deepcopy(result)
                self._snap_cache_t = now
        return result


def _anonymize(payload: dict) -> dict:
    """Redact identifiers everywhere, including derived prose, without mutating state."""
    sensitive = {"name", "hostname", "machine_model", "machine_vendor", "cpu_name",
                 "ssid", "bssid", "adapter", "desc", "location", "component", "issue"}
    replacements = {}

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in sensitive and isinstance(item, str) and item:
                    replacements[item] = _anon(item)
                if key == "host_labels" and isinstance(item, dict):
                    for label in item.values():
                        if isinstance(label, str) and label:
                            replacements[label] = _anon(label)
                if key == "wv2_hosts" and isinstance(item, dict):
                    for host in item:
                        replacements[host] = _anon(host)
                if key == "watchlist" and isinstance(item, list):
                    for row in item:
                        for field in ("note", "issue", "component"):
                            text = row.get(field)
                            if isinstance(text, str) and text:
                                replacements[text] = _anon(text)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload)
    # One pass avoids hashing already-redacted strings or replacing substrings
    # inside a generated label on subsequent snapshot requests.
    import re
    pattern = re.compile("|".join(re.escape(k) for k in sorted(replacements, key=len, reverse=True))) if replacements else None

    def redact(value):
        if isinstance(value, str):
            return pattern.sub(lambda m: replacements[m.group()], value) if pattern else value
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {replacements.get(k, k): redact(v) for k, v in value.items()}
        return value

    return redact(payload)
