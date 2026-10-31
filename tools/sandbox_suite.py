"""Run the full suite inside the self-evolution sandbox, as a cycle's baseline does.

CI runs the suite on Linux but not inside bubblewrap, whose /tmp is a small
tmpfs and whose network and home are hidden. A test that passes everywhere else
can still fail here, and a red sandbox baseline stops every evolution cycle
(5.4.2: a test needed 20 GB free in tmp_path). The pre-push hook runs this.

    python tools/sandbox_suite.py

Exit code 0 when the suite passes or no sandbox backend exists on this PC.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sysperf import codevolve  # noqa: E402


def main() -> int:
    sb = codevolve.Sandbox()
    if sb.backend == "none":
        print(f"sandbox suite: skipped, no isolation backend ({sb.why_none})")
        return 0
    r = sb.run(ROOT, [sys.executable, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider",
                      "-o", "addopts=", "tests"], timeout_s=1800)
    out = (r.get("stdout") or "").strip().splitlines()
    print(f"sandbox suite ({sb.backend}):")
    print("\n".join(ln for ln in out if ln.startswith("FAILED")) or "", end="")
    print(out[-1] if out else (r.get("stderr") or "")[-500:])
    return 1 if r.get("blocked") else int(r.get("rc") or 0)


if __name__ == "__main__":
    sys.exit(main())
