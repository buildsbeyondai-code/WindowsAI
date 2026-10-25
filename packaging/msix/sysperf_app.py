"""PyInstaller entry point for SysPerf.exe (the packaged desktop app)."""
import multiprocessing
import os
import sys

from sysperf.desktop import main, profile_dir

if __name__ == "__main__":
    multiprocessing.freeze_support()
    # A windowed (no console) build has no stdout/stderr; logging and print
    # would fail or vanish. Send both to a log next to the WebView2 profile.
    if sys.stdout is None or sys.stderr is None:
        log_dir = os.path.dirname(profile_dir(os.environ))
        os.makedirs(log_dir, exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(log_dir, "app.log"), "a",
                                       encoding="utf-8", buffering=1)
    os._exit(main())
