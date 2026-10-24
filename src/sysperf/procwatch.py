"""
Runaway-Explorer CPU watchdog for the System Performance Dashboard.

Why this exists
---------------
Windows Explorer (``explorer.exe`` — the desktop shell) intermittently pins CPU:
a stuck shell-extension / context-menu handler, thumbnail generation over a
folder full of media, a misbehaving tray app, or a cloud-sync (OneDrive/iCloud)
overlay handler can put one of Explorer's threads into a busy loop. The symptom
is a *recurring transient spike* — explorer jumps to tens of percent for a while,
then settles — so it does NOT show up as a steady, "consistent" consumer and it
often does not push whole-machine ``cpu_pct`` past its target either. That means
the scorecard → RCA → fix pipeline (which reasons about scored criteria) never
reliably catches it.

The fix is the standard one: **restart the Explorer shell**. Windows relaunches
it immediately, the stuck thread is gone, and CPU returns to baseline — with no
user data loss (open documents run in their own processes; only the taskbar and
desktop redraw). Explorer is uniquely safe to auto-restart for exactly this
reason, which is why this watchdog is scoped to Explorer only rather than killing
arbitrary applications.

Design (mirrors session_policy.py)
----------------------------------
* Level-triggered: every tick reads explorer's recent per-interval CPU% from the
  rolling history (cpuwatch.recent_interval_cpu) and restarts only when it has
  stayed at/above the threshold across ``sustain`` consecutive intervals — a
  single one-off spike is ignored.
* A cooldown after each restart prevents a restart loop if the offending handler
  immediately re-pins CPU.
* Host actions go through the injected command runner (fixes.default_runner), so
  this is fully unit-testable and honours ``FIX_ENABLED`` via ``fix_enabled``.
* Enabled flag + last-action record are persisted so the choice and the history
  survive a service restart.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from . import cpuwatch

# The process this watchdog manages. Restarting the shell is safe; Windows
# relaunches it. Force-stop is a no-op (rc 0) when it is already gone, and the
# guard relaunches it if Windows has not already.
EXPLORER = "explorer"
PS_RESTART_EXPLORER = (
    "Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue; "
    "Start-Sleep -Seconds 1; "
    "if (-not (Get-Process explorer -ErrorAction SilentlyContinue)) "
    "{ Start-Process explorer }")


def _default_apply(runner: Callable[[str, str], tuple]) -> dict:
    """Restart Explorer via the injected runner. Returns a result dict."""
    rc, output = runner("pwsh", PS_RESTART_EXPLORER)
    return {"action": "restart_explorer", "status": "ok" if rc == 0 else "error",
            "returncode": rc, "output": output}


class ExplorerCpuWatchdog:
    """Restart Explorer when it stays a heavy CPU consumer, with a cooldown."""

    def __init__(self, runner: Callable[[str, str], tuple],
                 get_history: Callable[[], list], state_file: str | os.PathLike, *,
                 threshold_pct: float = 25.0, sustain: int = 2,
                 cooldown_s: float = 600.0, enabled: bool = True,
                 fix_enabled: bool = True, max_age_s: float = 240.0,
                 apply_fn=None,
                 clock: Callable[[], float] = time.time) -> None:
        self.runner = runner
        self.get_history = get_history
        self._path = Path(state_file)
        self.threshold_pct = float(threshold_pct)
        self.sustain = max(1, int(sustain))
        self.cooldown_s = float(cooldown_s)
        self.fix_enabled = fix_enabled
        # Newest sample must be at most this old to be acted on. The per-process
        # CPU series is only meaningful for the recent past; when the Windows
        # probe stalls (e.g. host at 100% CPU) the history stops advancing and a
        # frozen "high" reading must NOT drive a restart.
        self.max_age_s = float(max_age_s)
        self.apply_fn = apply_fn or _default_apply
        self.clock = clock
        self._lock = threading.Lock()
        self.last_action_at: float | None = None
        self.action_count = 0
        self.last_result: dict | None = None
        # Persisted enable flag wins over the constructor default when present, so
        # a user's on/off choice survives a restart.
        self.enabled = enabled
        self._load()

    # ------------------------------------------------------------------ io ---
    def _load(self) -> None:
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, ValueError, OSError):
            return
        if "enabled" in data:
            self.enabled = bool(data["enabled"])
        self.action_count = int(data.get("action_count") or 0)
        self.last_action_at = data.get("last_action_at")
        self.last_result = data.get("last_result")

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"enabled": self.enabled, "action_count": self.action_count,
                   "last_action_at": self.last_action_at,
                   "last_result": self.last_result}
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent),
                                   prefix=".procwatch.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self._path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -------------------------------------------------------------- policy ---
    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self.enabled = bool(enabled)
            self._save()
            return self.enabled

    def _cooling_s(self) -> float:
        if self.last_action_at is None:
            return 0.0
        return max(0.0, self.last_action_at + self.cooldown_s - self.clock())

    def recent(self) -> list:
        """The last ``sustain`` per-interval CPU% readings for Explorer."""
        return cpuwatch.recent_interval_cpu(self.get_history() or [], EXPLORER,
                                            count=self.sustain)

    def _newest_t(self) -> float | None:
        """Timestamp of the newest usable sample, or None if history is empty."""
        for s in reversed(self.get_history() or []):
            if isinstance(s, dict) and s.get("_t") is not None:
                return float(s["_t"])
        return None

    def sample_age_s(self) -> float | None:
        """Age of the newest sample in seconds, or None when unknown."""
        t = self._newest_t()
        return None if t is None else max(0.0, self.clock() - t)

    def is_stale(self) -> bool:
        """True when the newest sample is too old to act on (probe stalled)."""
        age = self.sample_age_s()
        return age is None or age > self.max_age_s

    def offending(self) -> dict | None:
        """Detection only (no side effects): is Explorer a sustained CPU hog?

        Returns evidence dict when the last ``sustain`` consecutive intervals are
        all at/above the threshold, else None. Requires a full ``sustain``-length
        window so a cold start with one reading cannot trip it, and ignores stale
        data (a frozen sample from a stalled probe must not trigger a restart).
        """
        if self.is_stale():
            return None
        series = self.recent()
        if len(series) < self.sustain:
            return None
        if all(v >= self.threshold_pct for v in series):
            return {"process": EXPLORER, "recent_cpu_pct": series,
                    "threshold_pct": self.threshold_pct,
                    "current_cpu_pct": series[-1]}
        return None

    def tick(self) -> dict | None:
        """One evaluation. Restarts Explorer on a sustained breach outside the
        cooldown. Returns the action result, or None when nothing was done."""
        with self._lock:
            if not (self.enabled and self.fix_enabled):
                return None
            if self._cooling_s() > 0:
                return None
            evidence = self.offending()
            if evidence is None:
                return None
            result = self.apply_fn(self.runner)
            now = self.clock()
            self.last_action_at = now
            self.action_count += 1
            self.last_result = {"at": now, "trigger": evidence, **result}
            self._save()
            return self.last_result

    def status(self) -> dict:
        with self._lock:
            series = self.recent()
            age = self.sample_age_s()
            return {
                "enabled": self.enabled,
                "effective": self.enabled and self.fix_enabled,
                "execution_enabled": self.fix_enabled,
                "process": EXPLORER,
                "threshold_pct": self.threshold_pct,
                "sustain_intervals": self.sustain,
                "cooldown_s": self.cooldown_s,
                "cooldown_left_s": round(self._cooling_s(), 1),
                "recent_cpu_pct": series,
                "current_cpu_pct": series[-1] if series else None,
                "sample_age_s": None if age is None else round(age, 1),
                "stale": self.is_stale(),
                "max_age_s": self.max_age_s,
                "offending": self.offending() is not None,
                "action_count": self.action_count,
                "last_action_at": self.last_action_at,
                "last_result": self.last_result,
            }

    def loop(self, interval: float) -> None:          # pragma: no cover
        """Evaluate every `interval` seconds forever (daemon thread)."""
        while True:
            try:
                self.tick()
            except Exception:
                pass
            time.sleep(interval)
