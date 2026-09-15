"""Regression coverage for defects reproduced during the source audit."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sysperf.adaptive import AdaptiveScorer
from sysperf.app import create_app
from sysperf.collector import Collector
from sysperf.config import DEFAULTS, Config, load_config
from sysperf.fixes import FixEngine, build_registry, default_runner
from sysperf.profile import build_criteria, capture_baseline
from sysperf.scoring import build_scorecard, vmmem_trend


@pytest.fixture
def collected(cfg, sample, tmp_path, monkeypatch):
    # Patch import-bound paths too; changing the environment alone is too late.
    monkeypatch.setattr('sysperf.app.ADAPTIVE_FILE', tmp_path / 'adaptive.json')
    monkeypatch.setattr('sysperf.app.ensure_watchlist', lambda: None)
    monkeypatch.setattr('sysperf.collector.build_components', lambda _: {})
    c = Collector(cfg, history_file=tmp_path / 'h.jsonl', baseline_file=tmp_path / 'b.json')
    c.read_windows = lambda full: copy.deepcopy(sample)
    c.sample()
    c.sample()
    return c


@pytest.mark.parametrize('overrides', [{'host': '0.0.0.0', 'port': 6001}, {'host': '::'}])
def test_cli_overrides_enforce_auth_and_keep_port(tmp_path, overrides):
    cfg = load_config(tmp_path / 'c.json', overrides=overrides)
    assert cfg.auth_token and cfg.requires_token
    assert cfg.port == overrides.get('port', DEFAULTS['port'])


def test_cli_passes_overrides_before_config_resolution(tmp_path, monkeypatch):
    from sysperf.__main__ import main
    monkeypatch.setattr('sysperf.__main__.is_supported', lambda: True)
    seen = {}
    monkeypatch.setattr('sysperf.app.serve', lambda cfg: seen.update(cfg))
    assert main(['--config', str(tmp_path / 'c.json'), '--host', '0.0.0.0', '--port', '6001']) == 0
    assert seen['auth_token'] and seen['host'] == '0.0.0.0' and seen['port'] == 6001


def test_config_reads_windows_bom_and_rejects_truthy_opt_in(tmp_path):
    path = tmp_path / 'c.json'
    path.write_text(json.dumps({'port': 6001, 'fixes': {'enabled': 'false'}}), encoding='utf-8-sig')
    cfg = load_config(path)
    assert cfg.port == 6001 and cfg['fixes']['enabled'] is False


@pytest.mark.parametrize('bad', [None, [], 'invalid', True])
def test_invalid_numeric_config_falls_back(tmp_path, bad):
    path = tmp_path / 'c.json'
    path.write_text(json.dumps({'port': bad, 'targets': bad, 'adaptive': bad}))
    cfg = load_config(path)
    assert cfg.port == DEFAULTS['port'] and isinstance(cfg['targets'], dict)


def test_empty_bind_not_considered_loopback():
    assert Config({'host': ''}).requires_token


def test_app_rejects_unprotected_public_config(cfg):
    cfg['host'] = '0.0.0.0'
    with pytest.raises(ValueError, match='token'):
        create_app(cfg)


def test_api_rejects_dns_rebinding_and_cross_origin(collected):
    client = create_app(collected.cfg, collected).test_client()
    assert client.get('/api/metrics', headers={'Host': 'attacker.example'}).status_code == 403
    token = client.get('/api/fixes').get_json()['token']
    assert client.post('/api/recalibrate', headers={'Origin': 'https://attacker.example',
        'X-SysPerf-CSRF': token}).status_code == 403
    assert client.post('/api/recalibrate').status_code == 403
    assert client.post('/api/recalibrate', headers={'X-SysPerf-CSRF': token}).status_code == 200


@pytest.mark.parametrize('body', [[], None, 'x', {'confirm': 'false'}, {'ids': 'flush_dns'},
                                  {'ids': [{}]}, {'ids': ['unknown']}])
def test_malformed_fix_requests_are_400(collected, body):
    client = create_app(collected.cfg, collected).test_client()
    assert client.post('/api/fix', json=body).status_code == 400


@pytest.mark.parametrize('ids', [None, []])
def test_apply_requires_explicit_selection(collected, ids):
    collected.cfg['fixes']['enabled'] = True
    client = create_app(collected.cfg, collected).test_client()
    token = client.get('/api/fixes').get_json()['token']
    assert client.post('/api/fix', json={'confirm': True, 'token': token, 'ids': ids}).status_code == 400


def test_empty_fix_selection_does_not_run_all():
    calls = []
    engine = FixEngine(runner=lambda *args: calls.append(args))
    assert engine.apply([], {}) == [] and calls == []
    assert engine.preview([], {}) == []


def test_implicit_batch_does_not_run_caution_actions():
    calls = []
    engine = FixEngine(runner=lambda sh, cmd: (calls.append(cmd), (0, 'ok'))[1])
    snapshot = {'scorecard': {'rows': [{'key': 'handles', 'status': 'regress'},
                                      {'key': 'c_free_gb', 'status': 'regress'}]}}
    engine.apply(None, snapshot)
    assert calls == ['ipconfig /flushdns']
    assert {f.id: f for f in build_registry()}['clear_temp'].reversible is False
    assert not {f.id: f for f in build_registry()}['optimize_wsl_boot'].executable


def test_fix_selection_deduplicates():
    engine = FixEngine(runner=lambda *args: (0, 'ok'))
    assert len(engine.apply(['flush_dns', 'flush_dns'], {})) == 1


def test_unavailable_powershell_is_reported(monkeypatch):
    def unavailable():
        raise RuntimeError('missing PowerShell')
    monkeypatch.setattr('sysperf.platform_win.powershell_exe', unavailable)
    rc, message = default_runner('pwsh', 'test')
    assert rc == 1 and 'missing PowerShell' in message


def test_nonzero_collector_exit_is_rejected(monkeypatch):
    from sysperf.platform_win import run_powershell
    monkeypatch.setattr('sysperf.platform_win.powershell_exe', lambda: 'powershell')
    monkeypatch.setattr('sysperf.platform_win.to_windows_path', str)
    monkeypatch.setattr('sysperf.platform_win.subprocess.run', lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout='{"partial":true}', stderr='failure'))
    with pytest.raises(RuntimeError, match='exited with 1'):
        run_powershell(Path('collect.ps1'))


@pytest.mark.parametrize('bad', [[], None, 42, {}])
def test_malformed_sample_is_reported_without_crashing(collected, bad):
    collected.read_windows = lambda full: bad
    assert collected.sample() is False
    assert 'non-empty JSON object' in collected.snapshot()['meta']['error']


def test_invalid_history_records_are_skipped(cfg, tmp_path):
    path = tmp_path / 'h.jsonl'
    path.write_text('null\n[]\n42\nnot json\n{"handles":123}\n')
    c = Collector(cfg, history_file=path, baseline_file=tmp_path / 'b.json')
    assert len(c.history) == 1 and c.latest == {'handles': 123}


def test_slow_fields_and_battery_wear_survive_fast_sample(collected, sample):
    sample.update(os_version='10.0.26100', power_plan_guid='test-guid', wsl_cpus_cfg=4)
    collected.read_windows = lambda full: copy.deepcopy(sample)
    collected.sample_count = 1
    collected.sample()
    collected.read_windows = lambda full: {'handles': 100}
    collected.sample()
    assert collected.latest['os_version'] == '10.0.26100'
    assert collected.latest['power_plan_guid'] == 'test-guid'
    assert collected.latest['wsl_cpus_cfg'] == 4
    assert collected.latest['battery_wear_pct'] == pytest.approx(41.5)


def test_anonymization_covers_routes_and_never_mutates_raw_state(collected):
    collected.cfg['anonymize'] = True
    original = copy.deepcopy(collected.latest)
    client = create_app(collected.cfg, collected).test_client()
    token = client.get('/api/fixes').get_json()['token']
    for response in [client.get('/api/metrics'), client.get('/api/profile'),
                     client.post('/api/recalibrate', headers={'X-SysPerf-CSRF': token})]:
        assert 'TESTBOX' not in response.get_data(as_text=True)
        assert 'VendorUpdater' not in response.get_data(as_text=True)
    assert collected.latest == original
    assert collected.history[-1] == original
    assert collected.baseline['profile']['hostname'] == 'TESTBOX'
    assert collected.snapshot()['latest']['top_mem'][0]['name'] == collected.snapshot()['latest']['top_mem'][0]['name']


def test_snapshot_returns_detached_objects(collected):
    first = collected.snapshot()
    first['latest']['top_mem'][0]['name'] = 'changed'
    assert collected.snapshot()['latest']['top_mem'][0]['name'] != 'changed'
    assert collected.latest['top_mem'][0]['name'] != 'changed'


def test_new_fault_does_not_score_perfect(cfg):
    row = build_scorecard({'hw_problem_devices': 1}, [],
                         [('hw_faults', 'Faults', 0, 0, False, '')], cfg)['rows'][0]
    assert row['progress'] == 0 and row['status'] == 'regress'


def test_zero_startup_count_is_a_valid_criterion(sample, cfg):
    sample['startup_apps'] = 0
    criteria = build_criteria(capture_baseline(sample), sample, cfg)
    assert 'startup_apps' in {row[0] for row in criteria}


def test_wsl_zero_resets_trend_window():
    history = [{'_t': i * 120, 'vmmem_mb': 4000} for i in range(8)]
    history += [{'_t': 960, 'vmmem_mb': 0}, {'_t': 1080, 'vmmem_mb': 4000}]
    slope, span, points = vmmem_trend(history, {})
    assert slope == 0 and span == 120 and points == 2


def test_adaptive_best_does_not_regress_or_expire(tmp_path):
    scorer = AdaptiveScorer(tmp_path / 'a.json', {'adaptive': {'min_day_samples': 1}})
    scorer.update({'_iso': '2026-01-01', 'handles': 10})
    scorer.update({'_iso': '2026-01-01', 'handles': 100})
    assert scorer.metrics['handles']['personal_best'] == 10
    for i in range(1, 70):
        scorer.update({'_iso': f'day-{i:05}', 'handles': 100})
    assert scorer.metrics['handles']['personal_best'] == 10


def test_components_do_not_claim_unverified_currency_or_gain(monkeypatch):
    from sysperf.components import build_components
    monkeypatch.setattr('sysperf.components._detect_tools', lambda: {'python': '3.12.7'})
    data = build_components({'edge_version': '128.0', 'gpu_driver': '1.0', 'gpu_driver_date': '2020-01-01'})
    assert all(row['outdated'] is None and row['perf'] is None for row in data['rows'])
    assert data['avg_uplift'] is None


def test_auth_token_is_carried_by_frontend(collected):
    collected.cfg['auth_token'] = 'secret'
    client = create_app(collected.cfg, collected).test_client()
    assert client.get('/').status_code == 401
    page = client.get('/?token=secret').get_data(as_text=True)
    assert 'headers.set("X-SysPerf-Token", apiToken)' in page
    assert 'await fetch("api/' not in page
    assert client.get('/api/metrics', headers={'X-SysPerf-Token': 'secret'}).status_code == 200
    assert client.get('/api/metrics?token=%E2%98%83').status_code == 401


def test_wifi_overlap_counts_cochannel_but_excludes_own_access_point():
    from sysperf.netopt import analyze_overlap
    scan = {'available': True, 'nets': [
        {'band': '2.4 GHz', 'channel': 6, 'bssid': 'self', 'signal': 90},
        {'band': '2.4 GHz', 'channel': 6, 'bssid': 'other', 'signal': 80},
        {'band': '6 GHz', 'channel': 6, 'bssid': 'third', 'signal': 80}]}
    result = analyze_overlap(scan, {'band': '2.4 GHz', 'channel': 6, 'bssid': 'self'})
    assert result['current_overlap'] == 1


def test_no_wifi_advice_when_not_connected():
    from sysperf.netopt import optimisation_plan
    assert optimisation_plan({}, {}, {}) == []


def test_power_plan_uses_guid_for_localized_names(sample, cfg):
    from sysperf.advisor import _power_plan
    sample['power_plan'] = 'Energiesparmodus'
    assert _power_plan(sample, {}, cfg, {}) is not None
