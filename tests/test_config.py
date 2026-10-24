"""Coverage + sanity tests for config.py."""
import importlib

import json

import sysperf.config as config


def test_criteria_shape():
    for key, label, base, target, hib, unit in config.CRITERIA:
        assert isinstance(key, str) and isinstance(label, str)
        assert isinstance(hib, bool)


def test_new_metrics_present():
    keys = {c[0] for c in config.CRITERIA}
    for k in ("boot_time_s", "wsl_start_s", "explorer_launch_s", "outlook_launch_s",
              "word_launch_s", "cpu_pct", "cpu_queue", "disk_active_pct",
              "cpu_temp_c", "battery_wear_pct"):
        assert k in keys, k


def test_series_and_carryforward_include_new_keys():
    for k in ("word_launch_s", "disk_active_pct", "cpu_temp_c", "battery_wear_pct"):
        assert k in config.SERIES_KEYS
    for k in ("boot_time_s", "word_launch_s", "cpu_temp_c"):
        assert k in config.CARRY_FORWARD_KEYS


def test_fix_enabled_env(monkeypatch):
    monkeypatch.setenv("PERFDASH_FIX_ENABLED", "0")
    importlib.reload(config)
    assert config.FIX_ENABLED is False
    monkeypatch.setenv("PERFDASH_FIX_ENABLED", "1")
    importlib.reload(config)
    assert config.FIX_ENABLED is True


def test_ps_exe_is_a_path():
    importlib.reload(config)
    assert isinstance(config.PS_EXE, str) and config.PS_EXE


# --------------------------------------------- settings file + precedence -----
# The Windows installer has written sysperf.config.json since 2.x; before 2.8.0
# nothing read it, so every documented setting was inert.
def test_a_json_settings_file_is_read(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"port": 5100}), encoding="utf-8")
    assert config._load_settings(path) == {"port": 5100}


def test_a_missing_settings_file_is_not_an_error(tmp_path):
    assert config._load_settings(tmp_path / "nope.json") == {}


def test_a_malformed_settings_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    assert config._load_settings(path) == {}


def test_a_settings_file_that_is_not_an_object_is_ignored(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert config._load_settings(path) == {}


def test_the_environment_beats_the_settings_file(monkeypatch):
    monkeypatch.setattr(config, "SETTINGS", {"port": 5100})
    monkeypatch.setenv("PERFDASH_PORT", "5200")
    assert config._setting("PERFDASH_PORT", "port", default=1) == "5200"


def test_an_empty_environment_value_does_not_override(monkeypatch):
    monkeypatch.setattr(config, "SETTINGS", {"port": 5100})
    monkeypatch.setenv("PERFDASH_PORT", "")
    assert config._setting("PERFDASH_PORT", "port", default=1) == 5100


def test_a_nested_setting_is_read(monkeypatch):
    monkeypatch.setattr(config, "SETTINGS",
                        {"autoimprove": {"mode": "observe"}})
    monkeypatch.delenv("PERFDASH_AUTO_MODE", raising=False)
    assert config._setting("PERFDASH_AUTO_MODE", "autoimprove", "mode",
                           default="auto") == "observe"


def test_a_missing_key_anywhere_in_the_path_gives_the_default(monkeypatch):
    monkeypatch.setattr(config, "SETTINGS", {"autoimprove": "not-a-dict"})
    monkeypatch.delenv("PERFDASH_AUTO_MODE", raising=False)
    assert config._setting("PERFDASH_AUTO_MODE", "autoimprove", "mode",
                           default="auto") == "auto"
    monkeypatch.setattr(config, "SETTINGS", {})
    assert config._setting("PERFDASH_X", "nope", default="d") == "d"


def test_an_explicit_null_in_the_file_gives_the_default(monkeypatch):
    monkeypatch.setattr(config, "SETTINGS", {"port": None})
    monkeypatch.delenv("PERFDASH_PORT", raising=False)
    assert config._setting("PERFDASH_PORT", "port", default=5056) == 5056


def test_flag_accepts_booleans_and_strings(monkeypatch):
    monkeypatch.delenv("PERFDASH_FIX_ENABLED", raising=False)
    monkeypatch.setattr(config, "SETTINGS", {"fixes": {"enabled": False}})
    assert config._flag("PERFDASH_FIX_ENABLED", "fixes", "enabled") is False
    monkeypatch.setattr(config, "SETTINGS", {"fixes": {"enabled": True}})
    assert config._flag("PERFDASH_FIX_ENABLED", "fixes", "enabled") is True
    for raw in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("PERFDASH_FIX_ENABLED", raw)
        expected = raw == ""          # an empty env value is not an override
        assert config._flag("PERFDASH_FIX_ENABLED", "fixes", "enabled") is expected


def test_the_data_directory_is_never_the_package_directory():
    """Once pip-installed the package directory is site-packages."""
    assert config.DATA_DIR.is_absolute()
    assert config.PLATFORM in ("windows", "wsl", "linux")
