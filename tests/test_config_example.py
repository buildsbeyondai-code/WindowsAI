"""Every key in sysperf.config.example.json must be read by the code.

Up to 4.1 the example documented two dozen keys (auth_token, anonymize, the
advisor block, smoothing and Wi-Fi tuning) that nothing read, so editing them
silently did nothing. Each key must appear as a string literal in the package.
Children of free-form maps (SSIDs, driver names, app names) are user data.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FREE_FORM = {("wifi", "plan_mbps"), ("drivers", "verified_latest"),
             ("browser_hosts", "mandatory"), ("browser_hosts", "in_use")}


def _keys(node, path=()):
    for k, v in node.items():
        if k.startswith("//"):
            continue
        yield (*path, k)
        if isinstance(v, dict) and (*path, k) not in FREE_FORM:
            yield from _keys(v, (*path, k))


def test_every_example_key_is_read_by_the_code():
    example = json.loads((ROOT / "sysperf.config.example.json").read_text(encoding="utf-8"))
    source = "\n".join(p.read_text(encoding="utf-8")
                       for p in (ROOT / "src" / "sysperf").rglob("*.py"))
    unread = [".".join(k) for k in _keys(example) if f'"{k[-1]}"' not in source]
    assert unread == []


def test_example_values_are_the_shipped_defaults():
    example = json.loads((ROOT / "sysperf.config.example.json").read_text(encoding="utf-8"))
    assert example["port"] == 5056 and example["host"] == "127.0.0.1"
    auto = example["autoimprove"]
    assert (auto["max_actions"], auto["cooldown_seconds"], auto["settle_seconds"]) == (2, 1800, 120)
    assert example["history"]["raw_days"] == 14 and example["sample_seconds"] == 60
