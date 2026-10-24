"""
Lock-state OneDrive policy.

Runs OneDrive while the workstation is **locked** (sync freely while you are
away) and stops it while **unlocked** (keep CPU/disk free while you work). This
sits on top of — and does not replace — onedrive.py's explicit Start/Stop
controls or the "stopped by default at start-up" behaviour.

Lock state comes from the collected sample (``session_locked``, detected from
the presence of ``LogonUI.exe``).

Why this RECONCILES instead of only acting on lock/unlock edges
---------------------------------------------------------------
The original policy was purely edge-triggered: it acted only when the lock state
*changed*, on the theory that this "respects manual control between changes".
That made it latch permanently. Observed on 2026-09-28:

    04:20:09  service starts during Windows logon; first tick sees unlocked
              and OneDrive not yet running -> "desired state already holds",
              last_locked := False
    04:22:48  the one-shot enforce_default stop lands; OneDrive stopped
    04:25:17  Windows' own logon autostart relaunches OneDrive.exe
    04:26 ->  every subsequent tick: locked == last_locked -> return None

OneDrive then ran unbounded while unlocked (~800 CPU-seconds in 16 minutes,
disk 31% active), which is precisely what the policy exists to prevent. An
edge-triggered controller cannot correct drift it did not cause, and on this host
something else (Windows autostart, OneDrive's own relaunch) always causes it.

So the policy is now level-triggered: every tick compares actual against desired
and corrects a mismatch. Manual control is preserved *explicitly* rather than
accidentally — :meth:`note_manual` records a user Start/Stop and suspends
reconciliation for ``manual_grace_s``. A genuine lock/unlock transition clears
that hold, because a lock change is an unambiguous statement of intent.

Staleness guard
---------------
``collector._load_history()`` seeds ``latest`` from the last line of
history.jsonl, so immediately after a restart the "current" sample can be hours
old. The first tick used to latch ``last_locked`` from that stale sample. Samples
older than the policy's own start time are now ignored until a fresh one lands.

Host actions go through onedrive.apply (injected command runner), so this is
unit-testable and honours FIX_ENABLED via its ``enabled``/``fix_enabled`` flags.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from . import onedrive


class SessionOneDrivePolicy:
    """Drive OneDrive from the workstation lock state, reconciling on every tick."""

    def __init__(self, runner: Callable[[str, str], tuple],
                 get_latest: Callable[[], dict], *, apply_fn=None,
                 enabled: bool = True, fix_enabled: bool = True,
                 reconcile: bool = True, manual_grace_s: float = 1800.0,
                 control_disabled: Callable[[], bool] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.runner = runner
        self.get_latest = get_latest
        self.apply_fn = apply_fn or onedrive.apply
        self.enabled = enabled
        self.fix_enabled = fix_enabled
        self.reconcile = reconcile
        self.manual_grace_s = manual_grace_s
        # Master switch: when this returns True the policy is completely inert,
        # so OneDrive is neither started nor stopped by the dashboard. Defaults
        # to "never disabled" so existing behaviour is unchanged.
        self.control_disabled = control_disabled or (lambda: False)
        self.clock = clock
        self.last_locked = None
        self.started_at = clock()
        self.manual_until = 0.0

    @staticmethod
    def desired_action(locked: bool, running) -> str | None:
        """The action needed to reach the target state, or None if already there."""
        if locked and running is not True:
            return "start"                     # locked -> OneDrive should run
        if (not locked) and running is not False:
            return "stop"                      # unlocked -> OneDrive should stop
        return None

    def note_manual(self, action: str | None = None) -> float:
        """Record an explicit user Start/Stop.

        Suspends reconciliation until ``manual_grace_s`` has elapsed so a
        deliberate click is not undone a few seconds later. Returns the epoch
        time the hold expires. ``action`` is accepted for call-site clarity and
        future per-action policy; the hold itself is action-independent.
        """
        del action                             # hold is the same either way
        self.manual_until = self.clock() + self.manual_grace_s
        return self.manual_until

    def _is_current(self, latest: dict) -> bool:
        """False for a sample collected before this policy started (stale seed)."""
        t = latest.get("_t")
        return t is None or t >= self.started_at

    def manual_hold_s(self) -> int:
        """Seconds of manual hold remaining (0 when not holding)."""
        return max(0, int(round(self.manual_until - self.clock())))

    def tick(self) -> dict | None:
        """One evaluation. Corrects any mismatch between actual and desired
        state. Returns the action result, or None when nothing was done."""
        if self.control_disabled():
            return None                        # dashboard OneDrive control is OFF
        if not (self.enabled and self.fix_enabled):
            return None
        latest = self.get_latest() or {}
        locked = latest.get("session_locked")
        if locked is None:
            return None                        # lock state not observed yet
        if not self._is_current(latest):
            return None                        # stale seeded sample - wait for a fresh one
        transition = locked != self.last_locked
        self.last_locked = locked
        action = self.desired_action(locked, latest.get("onedrive_running"))
        if action is None:
            return None                        # desired state already holds
        if transition:
            # An explicit lock change outranks a manual hold: it says plainly
            # what the machine's state should now be.
            self.manual_until = 0.0
            return self.apply_fn(self.runner, action)
        if not self.reconcile:
            return None                        # edge-only mode (opt-in legacy)
        if self.manual_hold_s() > 0:
            return None                        # honour a recent manual Start/Stop
        return self.apply_fn(self.runner, action)

    def status(self) -> dict:
        control_off = self.control_disabled()
        return {"enabled": self.enabled and self.fix_enabled and not control_off,
                "control_disabled": control_off,
                "last_locked": self.last_locked,
                "reconcile": self.reconcile,
                "manual_hold_s": self.manual_hold_s()}

    def loop(self, interval: float) -> None:   # pragma: no cover
        """Evaluate the policy every `interval` seconds forever (daemon thread)."""
        log = logging.getLogger("perfdash")
        while True:
            try:
                result = self.tick()
                if result is not None:
                    log.info("OneDrive lock policy (locked=%s): %s", self.last_locked, result)
            except Exception:
                # Never kill the thread, but never fail silently either: a swallowed
                # error here is exactly how "OneDrive does not start on lock" hid.
                log.exception("OneDrive lock policy tick failed")
            time.sleep(interval)
