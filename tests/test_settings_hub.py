"""Unified settings: schema validation, all-or-nothing apply, restart tracking,
export/import/reset, and the config-file writer that preserves other keys."""
import json

import pytest

from sysperf.settings_hub import ConfigFile, Field, Section, SettingsHub


def _hub(store=None, changes=None):
    store = store if store is not None else {"a": {"n": 5, "on": True},
                                              "b": {"mode": "x", "names": ["p"], "r": 1.5}}

    def sec(name, fields, defaults):
        return Section(name, name.upper(), fields, lambda: dict(store[name]),
                       lambda diff: store[name].update(diff), defaults)
    hub = SettingsHub([
        sec("a", [Field("n", "Number", "int", min=1, max=10, restart=True),
                  Field("on", "Switch", "bool")], {"n": 3, "on": False}),
        sec("b", [Field("mode", "Mode", "choice", choices=["x", "y"]),
                  Field("names", "Names", "list"),
                  Field("r", "Ratio", "float", min=0, max=2)], {"mode": "y", "names": [],
                                                              "r": 1.0}),
    ], on_change=(changes.append if changes is not None else None))
    return hub, store


def test_field_validation_rules():
    f = Field("n", "N", "int", min=1, max=10)
    assert f.validate(4) == 4 and f.validate(4.0) == 4
    for bad in (True, "4", 4.5, 0, 11):
        with pytest.raises(ValueError):
            f.validate(bad)
    assert Field("r", "R", "float").validate(2) == 2.0
    with pytest.raises(ValueError):
        Field("b", "B", "bool").validate(1)
    with pytest.raises(ValueError):
        Field("c", "C", "choice", choices=["a"]).validate("z")
    assert Field("l", "L", "list").validate([" a ", "", "b"]) == ["a", "b"]
    with pytest.raises(ValueError):
        Field("l", "L", "list").validate("a,b")


def test_describe_and_export():
    hub, _ = _hub()
    d = hub.describe()
    assert d["sections"]["a"]["values"] == {"n": 5, "on": True}
    assert d["sections"]["a"]["fields"][0]["restart"] is True
    assert hub.export() == {"sysperf_settings": 1, "values": {
        "a": {"n": 5, "on": True}, "b": {"mode": "x", "names": ["p"], "r": 1.5}}}


def test_apply_is_all_or_nothing_and_tracks_restart():
    changes = []
    hub, store = _hub(changes=changes)
    with pytest.raises(ValueError):
        hub.apply({"a": {"n": 7}, "b": {"mode": "zzz"}})
    assert store["a"]["n"] == 5                              # nothing applied
    for bad in ([], {"c": {}}, {"a": {"nope": 1}}, {"a": "x"}):
        with pytest.raises(ValueError):
            hub.apply(bad)
    r = hub.apply({"a": {"n": 7, "on": True}, "b": {"mode": "x"}})
    assert r["changed"] == {"a": {"n": 7}} and store["a"]["n"] == 7
    assert r["restart_required"] == ["a.n"] and changes == [{"a": {"n": 7}}]
    assert hub.apply({"a": {"n": 7}})["changed"] == {}       # no-op: no callback
    assert len(changes) == 1


def test_reset_and_import():
    hub, store = _hub()
    hub.reset("b")
    assert store["b"] == {"mode": "y", "names": [], "r": 1.0} and store["a"]["n"] == 5
    hub.reset()
    assert store["a"] == {"n": 3, "on": False}
    with pytest.raises(ValueError):
        hub.reset("zzz")
    hub.import_({"sysperf_settings": 1, "values": {"a": {"n": 9}}})
    assert store["a"]["n"] == 9
    for bad in ({}, {"sysperf_settings": 2, "values": {}}, {"sysperf_settings": 1}, None):
        with pytest.raises(ValueError):
            hub.import_(bad)


def test_values_fall_back_to_defaults():
    hub, _ = _hub(store={"a": {}, "b": {}})
    assert hub.describe()["sections"]["a"]["values"] == {"n": 3, "on": False}


def test_config_file_preserves_other_keys(tmp_path):
    p = tmp_path / "sysperf.config.json"
    cf = ConfigFile(p)
    assert cf.load() == {} and cf.get("a.b", 1) == 1
    p.write_text(json.dumps({"port": 5056, "drivers": {"x": 1}, "fixes": 3}))
    cf.set_many({"sample_seconds": 60, "fixes.enabled": False, "autoimprove.threshold": 85})
    data = json.loads(p.read_text())
    assert data["port"] == 5056 and data["drivers"] == {"x": 1}
    assert data["fixes"] == {"enabled": False} and data["autoimprove"]["threshold"] == 85
    assert cf.get("autoimprove.threshold") == 85 and cf.get("autoimprove.nope", 7) == 7
    p.write_text("[1, 2]")
    assert cf.load() == {}
    p.write_text("{broken")
    assert cf.load() == {}


def test_describe_carries_the_section_order():
    hub, _ = _hub()
    assert hub.describe()["order"] == ["a", "b"]
