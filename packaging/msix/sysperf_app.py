"""PyInstaller entry point for SysPerf.exe (the packaged desktop app)."""
import multiprocessing
import os
import sys

from sysperf.desktop import main, profile_dir


def smoke(out_path: str) -> int:
    """CI: prove this frozen build runs on this machine's architecture.

    One real sample (the PowerShell collector included), written as JSON to
    ``out_path``; no window, no service. The release workflow runs it on the
    x64 and the Windows-on-ARM runner (5.5).
    """
    from sysperf.cli import main as cli_main
    with open(out_path, "w", encoding="utf-8") as fh:
        sys.stdout = fh
        return cli_main(["--once"]) or 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke":
        os._exit(smoke(sys.argv[2]))
    # A windowed (no console) build has no stdout/stderr; logging and print
    # would fail or vanish. Send both to a log next to the WebView2 profile.
    if sys.stdout is None or sys.stderr is None:
        log_dir = os.path.dirname(profile_dir(os.environ))
        os.makedirs(log_dir, exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(log_dir, "app.log"), "a",
                                       encoding="utf-8", buffering=1)
    os._exit(main())
