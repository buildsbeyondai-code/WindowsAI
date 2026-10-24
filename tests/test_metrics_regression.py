"""Metrics must tolerate numeric channel keys returned by Wi-Fi analysis."""
import sysperf.app as module
from sysperf.config import _load_settings


def test_metrics_with_numeric_wifi_channels(monkeypatch):
    sample = {
        "latest": {"wifi": {"ssid": "private-network"}},
        "wifi_overlap": {"used_5": {36: 2, 149: 1}},
        "meta": {"error": None},
    }
    monkeypatch.setattr(module.collector, "snapshot", lambda: sample)
    response = module.app.test_client().get('/api/metrics')
    assert response.status_code == 200
    data = response.get_json()
    assert data['wifi_overlap']['used_5'] == {'36': 2, '149': 1}
    assert data['latest']['wifi']['ssid'] == 'home-network'
    assert sample['latest']['wifi']['ssid'] == 'private-network'


def test_windows_powershell_settings_encoding(tmp_path):
    settings = tmp_path / 'sysperf.config.json'
    settings.write_text('{"port": 5123}', encoding='utf-8-sig')
    assert _load_settings(settings)['port'] == 5123
