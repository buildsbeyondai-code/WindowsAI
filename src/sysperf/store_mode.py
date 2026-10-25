"""
Microsoft Store mode: every feature kept, everything outside the app opt-in.

Store certification (policies 10.2 and 10.8) rejects an app that changes the
system the user did not ask it to change. A scripted install of SysPerf is
something its owner configured on purpose, so there the background features
start on, as they always have. The packaged app is installed by a click in the
Store, so in Store mode each feature that acts outside the app starts **off**
and is listed on Home with what it does and how to undo it, until the user
turns it on. Nothing is removed: the same switch turns each one on.

Store mode is on for a frozen ``SysPerf.exe`` (the MSIX build) or when
``SYSPERF_STORE_MODE=1``; see ``config.STORE_MODE``.

The on/off state lives with each feature (its own persisted state file), so
this module only describes them and routes a switch to the right owner. The one
exception is the OneDrive lock-state policy, which has no state file of its own
(it is an environment flag in a scripted install), so its choice is kept in
``store_features.json``.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

# id -> (title, what it does outside the app, how to undo it)
FEATURES = {
    "autonomous_fixes": (
        "Automatic fixes",
        "Runs the low-risk fixes you enable (flush DNS, trim idle apps' memory) without "
        "asking each time.",
        "Turn it off here or set Autonomy to Observe in AI Self-Evolution."),
    "explorer_watchdog": (
        "Explorer watchdog",
        "Restarts Windows Explorer when it keeps using heavy CPU. Open File Explorer "
        "windows close.",
        "Turn it off here or in App Control."),
    "tray_quit": (
        "Tray auto-quit",
        "Quits listed tray apps (ChatGPT, WhatsApp, …) after their window has been closed "
        "for a while, asking you first for each app.",
        "Turn it off here; reopen any app from App Control."),
    "onedrive_lock": (
        "OneDrive lock-state policy",
        "Pauses OneDrive while you use the PC and lets it sync while the PC is locked.",
        "Turn it off here, or start OneDrive from the Auto-Improve tab."),
    "self_evolution": (
        "AI self-evolution",
        "Asks an AI model to propose code improvements, tests them in a sandbox and keeps "
        "each one on a review branch of your SysPerf source checkout. Needs that checkout "
        "and an AI key. Nothing reaches the running app until you choose Apply & restart, "
        "and an applied patch can be rolled back.",
        "Set Autonomy below Self-evolve in AI Self-Evolution."),
}

AUTO_LEVELS = ("safe_auto", "self_evolve")


class StoreFeatures:
    """Describes the opt-in features and routes the Home switches to their owners."""

    def __init__(self, state_file: Path, *, store_mode: bool, action_policy, improver,
                 proc_watchdog, tray_quitter, session_policy) -> None:
        self.file = Path(state_file)
        self.store_mode = store_mode
        self.policy = action_policy
        self.improver = improver
        self.watchdog = proc_watchdog
        self.tray = tray_quitter
        self.session = session_policy
        self._lock = threading.Lock()
        if store_mode:
            self.session.enabled = bool(self._load().get("onedrive_lock"))

    def _load(self) -> dict:
        try:
            data = json.loads(self.file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, key: str, value: bool) -> None:
        data = self._load()
        data[key] = value
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.file)

    def state(self, fid: str) -> bool:
        if fid == "autonomous_fixes":
            return self.policy.level in AUTO_LEVELS
        if fid == "explorer_watchdog":
            return bool(self.watchdog.enabled)
        if fid == "tray_quit":
            return bool(self.tray.settings.get("enabled"))
        if fid == "onedrive_lock":
            return bool(self.session.enabled)
        return self.policy.level == "self_evolve"

    def describe(self) -> dict:
        return {"store_mode": self.store_mode,
                "features": [{"id": fid, "title": t, "what": what, "undo": undo,
                              "on": self.state(fid),
                              # Self-evolution needs a checkout and a key first, so
                              # it is set up in its own tab rather than by one click.
                              "where": "#self-evolution" if fid == "self_evolution" else None}
                             for fid, (t, what, undo) in FEATURES.items()]}

    def set(self, fid: str, on: bool) -> dict:
        if fid not in FEATURES:
            raise ValueError(f"unknown feature {fid!r}")
        if fid == "self_evolution":
            raise ValueError("set up AI self-evolution in its tab: it needs a source "
                             "checkout and an AI provider first")
        on = bool(on)
        with self._lock:
            if fid == "autonomous_fixes":
                if on != self.state(fid):
                    self.policy.set_level("safe_auto" if on else "observe")
                    self.improver.set_mode(self.policy.improver_mode())
            elif fid == "explorer_watchdog":
                self.watchdog.set_enabled(on)
            elif fid == "tray_quit":
                self.tray.configure(enabled=on)
            else:
                self.session.enabled = on
                self._save(fid, on)
        return self.describe()
