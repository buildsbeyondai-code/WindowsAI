"""requirements.lock must pin what requirements.txt asks for, with hashes.

CI regenerates the lock and diffs it (that needs the network); this offline
check catches the common slip of bumping a pin in one file and not the other.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)")


def _pins(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = PIN.match(line.strip())
        if m:
            out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


def test_every_runtime_pin_is_locked_at_the_same_version():
    wanted, locked = _pins(ROOT / "requirements.txt"), _pins(ROOT / "requirements.lock")
    assert wanted, "requirements.txt pins nothing"
    for name, version in wanted.items():
        assert locked.get(name) == version, f"{name}: requirements.txt {version}, lock {locked.get(name)}"


def test_every_locked_package_carries_a_hash():
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    blocks = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", text.split("\n", 4)[-1])
    entries = [b for b in blocks if PIN.match(b.strip())]
    assert entries
    for b in entries:
        assert "--hash=sha256:" in b, b.splitlines()[0]


def test_pyproject_runtime_dependencies_match_requirements():
    toml = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    deps = re.search(r"^dependencies = \[(.*?)\]", toml, re.S | re.M).group(1)
    declared = dict(re.findall(r'"([A-Za-z0-9_.-]+)==([^"]+)"', deps))
    declared = {k.lower(): v for k, v in declared.items()}
    assert declared == _pins(ROOT / "requirements.txt")
