"""
OneDrive *control* master switch for the System Performance Dashboard.

This is distinct from the OneDrive *Stop/Start* action (onedrive.py) and the
lock-state policy (session_policy.py):

* onedrive.py / session_policy.py *act on* OneDrive (start it, stop it).
* This module decides whether the dashboard is allowed to act on OneDrive **at
  all**.

When control is **disabled** the dashboard becomes completely hands-off with
respect to OneDrive — no stop-by-default at start-up, no lock-state autostart,
no lock-state auto-stop, and the manual Stop/Start endpoints refuse to run. In
other words, OneDrive behaves exactly as if this dashboard were not installed:
Windows manages it normally.

The flag is persisted to a small JSON file so the "hands-off" choice survives a
service restart — otherwise the next start-up would immediately re-stop
OneDrive, which is precisely the behaviour the user asked to switch off.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


class OneDriveControl:
    """Persisted, thread-safe master switch for dashboard OneDrive control.

    ``disabled() is True`` means "the dashboard must not touch OneDrive".
    Default (fresh install / missing file) is ``False`` — control enabled, so
    existing behaviour is unchanged until the user opts out.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._disabled = self._read()

    def _read(self) -> bool:
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                return bool(json.load(fh).get("control_disabled", False))
        except (FileNotFoundError, ValueError, OSError):
            return False

    def _write(self, disabled: bool) -> None:
        """Atomically persist the flag (temp file + os.replace)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent),
                                   prefix=".onedrive_control.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"control_disabled": disabled}, fh)
            os.replace(tmp, self._path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def disabled(self) -> bool:
        """True when the dashboard must not act on OneDrive."""
        with self._lock:
            return self._disabled

    def set(self, disabled: bool) -> bool:
        """Set and persist the flag. Returns the stored value."""
        with self._lock:
            self._disabled = bool(disabled)
            self._write(self._disabled)
            return self._disabled

    def status(self) -> dict:
        """UI-facing state: ``{"control_disabled": bool}``."""
        return {"control_disabled": self.disabled()}
