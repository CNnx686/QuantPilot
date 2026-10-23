import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quantpilot import terminal_engine
from quantpilot.terminal_engine import (evaluate, normalize_config, predict, train,
                                       verify_model)
from quantpilot.terminal_features import (execute_bar, features, new_account,
                                         normalizer)


def configuration(kind='rule'):
    return {'kind': kind, 'symbol': 'BTC/USDT', 'start': '2025-01-01', 'end': '2025-01-21',
            'wallet': 10000, 'stake': 1000, 'fee': .001, 'parameters': {}}


def market(start='2024-12-27', count=650):
    dates = pd.date_range(start, periods=count, freq='h', tz='UTC')
    close = 100 + np.arange(count) * .01 + np.sin(np.arange(count) / 12) * 3
    return pd.DataFrame({'date': dates, 'open': close, 'high': close + 1,
                         'low': close - 1, 'close': close, 'volume': np.full(count, 100.0)})


def test_configuration_rejects_holdout_and_unsupported_news():
    cfg = configuration()
    cfg['end'] = '2025-10-02'
    with pytest.raises(ValueError, match='Q4'):
        normalize_config(cfg)
    cfg = configuration()
    cfg['news_enabled'] = True
    with pytest.raises(ValueError, match='News'):
        normalize_config(cfg)


def test_features_and_normalization_do_not_see_future():
    original = market()
    changed = original.copy()
    changed.loc[400:, 'close'] *= 3
    np.testing.assert_allclose(features(original).iloc[:400], features(changed).iloc[:400], equal_nan=True)
    assert normalizer(features(original).iloc[64:400]) == normalizer(features(changed).iloc[64:400])


def test_closed_candle_stop_does_not_peek_at_intrabar_low():
    config = normalize_config(configuration())
    account = new_account(config['wallet'])
    bar = {'date': '2025-01-01T00:00:00Z', 'open': 100., 'low': 1., 'close': 100.}
    execute_bar(account, 1, bar, config, previous_close=100)
    assert account['units'] == 10
    execute_bar(account, 0, bar, config, previous_close=100)
    assert account['units'] == 10  # low=1 must not trigger hidden intrabar fills
    execute_bar(account, 0, {**bar, 'open': 90., 'close': 90.}, config, previous_close=94)
    assert account['units'] == 0
    assert account['trades'][0]['close_rate'] == 90
    assert account['trades'][0]['exit_reason'] == 'stop_loss_closed_candle'
    assert account['cash'] == pytest.approx(9898.1)


def test_evaluation_next_open_fees_and_cash_reconcile():
    config = normalize_config(configuration())
    frame = market(count=4)
    frame[['open', 'high', 'low', 'close']] = [100., 101., 99., 100.]
    frame.loc[1, 'open'] = 200
    frame.loc[1, 'high'] = 201
    frame.loc[1, 'close'] = 200
    frame.loc[2:, ['open', 'high', 'low', 'close']] = [200., 201., 199., 200.]
    result = evaluate(frame, [1, 2, 0, 0], config)
    assert result['trades'][0]['open_rate'] == 200
    assert result['trades'][0]['amount'] == 5
    assert result['metrics']['trades'] == 1
    assert result['metrics']['final_balance'] == pytest.approx(9998)


def test_rule_frozen_hash_and_zero_trade_candidate_are_valid(tmp_path):
    frame = market()
    frame['close'] = frame['open'] = 100.
    frame['high'], frame['low'] = 101., 99.
    candidate = train(configuration(), tmp_path / 'model', frame)
    assert candidate['metrics']['trades'] == 0
    assert candidate['metrics']['net_return'] == 0
    assert verify_model(candidate['artifact_dir'])['id'] == candidate['id']
    with pytest.raises(ValueError, match='frozen'):
        train(configuration(), tmp_path / 'model', frame)
    (tmp_path / 'model' / 'rule.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='hash mismatch'):
        verify_model(candidate['artifact_dir'])


def test_lightgbm_purges_label_cutoff_and_future_changes_do_not_change_fit(tmp_path):
    config = configuration('prediction')
    original = market()
    first = train(config, tmp_path / 'a', original)
    manifest = first['model_manifest']
    audit = manifest['training']
    cutoff = pd.Timestamp(first['period']['train']['end'])
    assert audit['purged_rows'] == 6
    assert pd.Timestamp(audit['model_label_max_at']) <= cutoff
    changed = original.copy()
    changed.loc[changed['date'] >= cutoff, ['open', 'high', 'low', 'close']] *= 1.5
    second = train(config, tmp_path / 'b', changed)
    assert first['model_manifest']['files']['model.txt'] == second['model_manifest']['files']['model.txt']
    assert first['model_manifest']['files']['snapshot.feather'] != second['model_manifest']['files']['snapshot.feather']


def test_current_prediction_ignores_in_progress_bar_and_rejects_stale(tmp_path):
    candidate = train(configuration('prediction'), tmp_path / 'model', market())
    stale = predict(candidate['artifact_dir'], market().tail(80))
    assert stale['valid'] is False and stale['reason'] == 'closed_candles_stale'
    current = market(count=81)
    now_hour = pd.Timestamp.now(tz='UTC').floor('h')
    current['date'] = pd.date_range(now_hour - pd.Timedelta(hours=80), periods=81, freq='h')
    before = predict(candidate['artifact_dir'], current)
    assert before['valid']
    assert pd.Timestamp(before['decision_at']) == now_hour
    current.loc[80, ['open', 'high', 'low', 'close']] *= 100
    after = predict(candidate['artifact_dir'], current)
    assert before['prediction'] == after['prediction']
    assert before['features'] == after['features']


def test_missing_hour_and_wrong_manifest_path_fail_closed(tmp_path):
    broken = market().drop(index=200)
    with pytest.raises(ValueError, match='contiguous'):
        train(configuration(), tmp_path / 'bad', broken)
    candidate = train(configuration(), tmp_path / 'good', market())
    path = Path(candidate['artifact_dir']) / 'manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    manifest['files']['../outside.txt'] = 'invalid'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='outside workspace'):
        verify_model(candidate['artifact_dir'])


def test_changed_dependency_versions_are_rejected(tmp_path, monkeypatch):
    candidate = train(configuration(), tmp_path / 'model', market())
    previous = terminal_engine._versions()
    monkeypatch.setattr(terminal_engine, '_versions', lambda: {**previous, 'numpy': 'changed'})
    with pytest.raises(ValueError, match='dependency versions changed'):
        verify_model(candidate['artifact_dir'])


def test_source_provenance_is_bound_to_snapshot_and_model(tmp_path):
    source = market()
    source.to_feather(tmp_path / 'market.feather')
    evidence = {'symbol': 'BTC/USDT', 'source': 'Explicit test input provenance',
                'data_sha256': terminal_engine._hash(tmp_path / 'market.feather')}
    (tmp_path / 'data-provenance.json').write_text(json.dumps(evidence), encoding='utf-8')
    candidate = train(configuration(), tmp_path / 'model', source)
    manifest = verify_model(candidate['artifact_dir'])
    assert manifest['data_provenance']['source_verified']
    assert manifest['data_provenance']['source_evidence'] == evidence
    assert manifest['files']['data-provenance.json'] == terminal_engine._hash(tmp_path / 'data-provenance.json')
    (tmp_path / 'model' / 'data-provenance.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='hash mismatch'):
        verify_model(candidate['artifact_dir'])
    changed = source.copy()
    changed.loc[300, 'volume'] += 1
    with pytest.raises(ValueError, match='provenance does not match'):
        train(configuration(), tmp_path / 'wrong', changed)
