"""Full-coverage tests for store_mode.py (opt-in features in the Store app)."""
import json

import pytest

from sysperf import store_mode


class _Policy:
    def __init__(self, level="observe"):
        self.level, self.calls = level, []

    def set_level(self, level):
        self.calls.append(level)
        self.level = level

    def improver_mode(self):
        return {"observe": "observe"}.get(self.level, "auto")


class _Improver:
    mode = None

    def set_mode(self, m):
        self.mode = m


class _Watchdog:
    enabled = False

    def set_enabled(self, on):
        self.enabled = on


class _Tray:
    def __init__(self):
        self.settings = {"enabled": False}

    def configure(self, enabled=None):
        self.settings["enabled"] = enabled


class _Session:
    enabled = True


def _make(tmp_path, store=True, level="observe"):
    deps = {"action_policy": _Policy(level), "improver": _Improver(), "proc_watchdog": _Watchdog(),
                "tray_quitter": _Tray(), "session_policy": _Session()}
    return store_mode.StoreFeatures(tmp_path / "sf.json", store_mode=store, **deps), deps


def test_store_mode_starts_the_onedrive_policy_from_its_saved_choice(tmp_path):
    sf, deps = _make(tmp_path)
    assert deps["session_policy"].enabled is False         # nothing saved: off
    (tmp_path / "sf.json").write_text(json.dumps({"onedrive_lock": True}))
    sf, deps = _make(tmp_path)
    assert deps["session_policy"].enabled is True
    (tmp_path / "sf.json").write_text("[1]")
    assert sf._load() == {}
    (tmp_path / "sf.json").write_text("{bad")
    assert sf._load() == {}


def test_outside_store_mode_nothing_is_overridden(tmp_path):
    sf, deps = _make(tmp_path, store=False)
    assert deps["session_policy"].enabled is True
    assert sf.describe()["store_mode"] is False


def test_describe_lists_every_feature_with_its_state(tmp_path):
    sf, _ = _make(tmp_path, level="self_evolve")
    d = sf.describe()
    by = {f["id"]: f for f in d["features"]}
    assert d["store_mode"] is True and set(by) == set(store_mode.FEATURES)
    assert by["autonomous_fixes"]["on"] and by["self_evolution"]["on"]
    assert not by["explorer_watchdog"]["on"] and not by["tray_quit"]["on"]
    assert by["self_evolution"]["where"] == "#self-evolution" and by["tray_quit"]["where"] is None
    assert all(f["what"] and f["undo"] for f in d["features"])


def test_set_routes_each_switch_to_its_owner(tmp_path):
    sf, deps = _make(tmp_path)
    sf.set("autonomous_fixes", True)
    assert deps["action_policy"].level == "safe_auto" and deps["improver"].mode == "auto"
    sf.set("autonomous_fixes", True)                       # already on: no level change
    assert deps["action_policy"].calls == ["safe_auto"]
    sf.set("autonomous_fixes", False)
    assert deps["action_policy"].level == "observe"
    sf.set("explorer_watchdog", True)
    assert deps["proc_watchdog"].enabled is True
    sf.set("tray_quit", True)
    assert deps["tray_quitter"].settings["enabled"] is True
    d = sf.set("onedrive_lock", True)
    assert deps["session_policy"].enabled is True
    assert json.loads((tmp_path / "sf.json").read_text()) == {"onedrive_lock": True}
    assert {f["id"]: f["on"] for f in d["features"]}["onedrive_lock"] is True


def test_set_refuses_unknown_and_self_evolution(tmp_path):
    sf, _ = _make(tmp_path)
    with pytest.raises(ValueError, match="unknown"):
        sf.set("nope", True)
    with pytest.raises(ValueError, match="its tab"):
        sf.set("self_evolution", True)
