"""Config resolution, the security posture of the bind, and platform detection."""
from __future__ import annotations

import json

import pytest

from sysperf.config import DEFAULTS, Config, load_config
from sysperf.platform_win import (
    UnsupportedPlatformError,
    is_supported,
    runtime_name,
    to_windows_path,
)


def test_defaults_bind_to_loopback_without_auth():
    """The out-of-the-box posture must not expose telemetry to the network."""
    assert DEFAULTS["host"] == "127.0.0.1"
    assert DEFAULTS["auth_token"] == ""


def test_loopback_needs_no_token(cfg):
    assert cfg.is_loopback
    assert not cfg.requires_token
    assert cfg["auth_token"] == ""


def test_non_loopback_bind_forces_a_generated_token(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSPERF_DATA_DIR", str(tmp_path))
    path = tmp_path / "sysperf.config.json"
    path.write_text(json.dumps({"host": "0.0.0.0"}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.requires_token
    assert len(cfg["auth_token"]) >= 20
    # persisted so the token survives a restart
    assert json.loads(path.read_text())["auth_token"] == cfg["auth_token"]


def test_public_view_hides_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSPERF_DATA_DIR", str(tmp_path))
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"host": "0.0.0.0"}), encoding="utf-8")
    view = load_config(path).public_view()
    assert "auth_token" not in view
    assert view["auth_enabled"] is True


def test_user_config_deep_merges_rather_than_replacing(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSPERF_DATA_DIR", str(tmp_path))
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"targets": {"commit_of_ram": 0.5}}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg["targets"]["commit_of_ram"] == 0.5
    # sibling keys survive the merge
    assert cfg["targets"]["compression_of_ram"] == DEFAULTS["targets"]["compression_of_ram"]


def test_broken_config_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSPERF_DATA_DIR", str(tmp_path))
    path = tmp_path / "c.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert load_config(path)["port"] == DEFAULTS["port"]


def test_environment_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSPERF_DATA_DIR", str(tmp_path))
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"port": 6000}), encoding="utf-8")
    monkeypatch.setenv("SYSPERF_PORT", "7001")
    assert load_config(path)["port"] == 7001


def test_invalid_values_are_clamped(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSPERF_DATA_DIR", str(tmp_path))
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"port": 999999, "sample_seconds": 0,
                                "max_samples": 1}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg["port"] == 65535
    assert cfg["sample_seconds"] >= 5      # a 0 s cadence would spin the CPU
    assert cfg["max_samples"] >= 30


def test_config_attribute_access():
    cfg = Config({"host": "127.0.0.1"})
    assert cfg.host == "127.0.0.1"
    with pytest.raises(AttributeError):
        _ = cfg.nope


def test_runtime_is_reported_honestly():
    assert runtime_name() in ("windows", "wsl", "unsupported")
    assert is_supported() == (runtime_name() != "unsupported")


def test_path_translation_is_refused_on_an_unsupported_platform():
    if is_supported():
        pytest.skip("running on Windows or WSL")
    with pytest.raises(UnsupportedPlatformError):
        to_windows_path("/tmp/x")
