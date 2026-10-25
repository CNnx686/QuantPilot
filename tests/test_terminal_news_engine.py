"""Model boundary tests with explicitly synthetic news; real source tests live separately."""
from copy import deepcopy
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quantpilot import terminal_engine as price_engine, terminal_news_engine as engine

NEWS_COLUMNS = ['news_count_24h', 'news_sources_24h', 'news_regulation_24h', 'news_security_24h']
SCHEMA = 'gdelt-sampled-6h-news4-v1'
POLICY = {'id': 'synthetic-unit-test-policy', 'window_hours': 24}


def config():
    return {'kind': 'prediction', 'symbol': 'BTC/USDT', 'start': '2025-01-01', 'end': '2025-01-21',
            'wallet': 10000, 'stake': 1000, 'fee': .001, 'news': True, 'parameters': {}}


def candles(count=650):
    close = 100 + np.arange(count) * .01 + 3 * np.sin(np.arange(count) / 12)
    return pd.DataFrame({'date': pd.date_range('2024-12-27', periods=count, freq='h', tz='UTC'),
        'open': close, 'high': close + 1, 'low': close - 1, 'close': close, 'volume': 100.})


def snapshot(mode='historical_archive_assumption'):
    return {'schema': SCHEMA, 'mode': mode, 'policy': POLICY, 'content_sha256': 'synthetic-fixture',
            'events': [], 'batches': [], 'coverage': {'status': 'covered'}}


def synthetic_factor(source, symbol, cutoff):
    cutoff = pd.Timestamp(cutoff)
    status = source.get('force_status', 'covered')
    count = int(cutoff.day % 4)
    if source.get('change_from') and cutoff >= pd.Timestamp(source['change_from']):
        count += 30
    if source.get('zero_events'):
        count = 0
    values = dict(zip(NEWS_COLUMNS, [float(np.log1p(count + offset)) for offset in (0, 0, 0, 0)]))
    return {'schema': SCHEMA, 'cutoff': cutoff.isoformat(),
        'values': values if status == 'covered' else None,
        'coverage': {'status': status, 'expected': 4, 'observed': 4 if status == 'covered' else 3, 'missing': []},
        'event_ids': ['fixture-event'] if count else [], 'events': [],
        'max_event_available_at': (cutoff + pd.Timedelta(hours=1) if source.get('future_event') else cutoff).isoformat(),
        'snapshot_id': source['content_sha256'], 'mode': source['mode']}


@pytest.fixture(autouse=True)
def isolated_news_boundary(monkeypatch):
    # Exercise actual model training and artifact validation against a deterministic
    # source adapter. No synthetic news is written into the application's store.
    monkeypatch.setattr(engine, 'news_api', lambda: SimpleNamespace(SCHEMA=SCHEMA,
        NEWS_FEATURES=NEWS_COLUMNS, factor_at=synthetic_factor))


def current_candles():
    frame = candles(81)
    hour = pd.Timestamp.now(tz='UTC').floor('h')
    frame['date'] = pd.date_range(hour - pd.Timedelta(hours=80), periods=81, freq='h')
    return frame


def test_news_models_require_explicit_supported_configuration():
    for override in ({'news': False}, {'kind': 'rule'}, {'symbol': 'SOL/USDT'}, {'end': '2025-03-01'}):
        with pytest.raises(ValueError):
            engine.normalize_config({**config(), **override})


def test_ten_columns_are_actual_lightgbm_inputs_and_news_matrix_is_frozen(tmp_path):
    candidate = engine.train(config(), tmp_path / 'model', candles(), snapshot())
    manifest = engine.verify_model(candidate['artifact_dir'])
    assert len(manifest['features']) == 10
    assert manifest['features'][-4:] == NEWS_COLUMNS
    matrix = pd.read_feather(tmp_path / 'model' / 'feature-matrix.feather')
    assert list(matrix.columns) == ['date', *manifest['features']]
    assert matrix[NEWS_COLUMNS].to_numpy().max() > 0
    assert manifest['training']['purged_rows'] == 6
    assert manifest['news']['coverage']['checked_rows'] == len(matrix)
    current = engine.predict(candidate['artifact_dir'], current_candles(), snapshot('observed'))
    assert current['valid'] and len(current['features']) == 10
    assert current['news']['coverage']['status'] == 'covered'


def test_paired_price_and_news_models_share_market_split_and_execution_protocol(tmp_path):
    shared = candles()
    price = price_engine.train({**config(), 'news': False}, tmp_path / 'price', shared)
    augmented = engine.train(config(), tmp_path / 'news', shared, snapshot())
    assert price['period'] == augmented['period']
    assert price['model_manifest']['files']['snapshot.feather'] == augmented['model_manifest']['files']['snapshot.feather']
    assert price['model_manifest']['training']['rows'] == augmented['model_manifest']['training']['rows']
    assert price['model_manifest']['training']['purged_rows'] == augmented['model_manifest']['training']['purged_rows']
    assert price['model_manifest']['effective_config']['parameters'] == augmented['model_manifest']['effective_config']['parameters']
    assert price['assumptions']['engine'] == augmented['assumptions']['engine']
    assert price['assumptions']['fee_per_side'] == augmented['assumptions']['fee_per_side']
    assert price['assumptions']['stop_loss'] == augmented['assumptions']['stop_loss']


def test_news_missing_does_not_become_zero_or_price_only_prediction(tmp_path, monkeypatch):
    candidate = engine.train(config(), tmp_path / 'model', candles(), snapshot())
    def forbidden(*args, **kwargs):
        raise AssertionError('Must never fall back to the selected price-only model')
    monkeypatch.setattr(price_engine, 'predict', forbidden)
    source = snapshot('observed')
    source['force_status'] = 'missing'
    decision = engine.predict(candidate['artifact_dir'], current_candles(), source)
    assert not decision['valid'] and decision['action'] == 'hold'
    assert decision['reason'] == 'news_coverage_missing' and decision['news']['values'] is None
    source['force_status'] = 'source_error'
    assert engine.predict(candidate['artifact_dir'], current_candles(), source)['reason'] == 'news_source_error'
    source.pop('force_status')
    source['zero_events'] = True
    valid = engine.predict(candidate['artifact_dir'], current_candles(), source)
    assert valid['valid'] and all(valid['features'][name] == 0 for name in NEWS_COLUMNS)


def test_missing_historical_coverage_and_future_available_event_are_rejected(tmp_path):
    source = snapshot()
    source['force_status'] = 'missing'
    with pytest.raises(ValueError, match='coverage missing'):
        engine.train(config(), tmp_path / 'missing', candles(), source)
    source = snapshot()
    source['future_event'] = True
    with pytest.raises(ValueError, match='not available'):
        engine.train(config(), tmp_path / 'future', candles(), source)


def test_evaluation_news_changes_do_not_change_training_model(tmp_path):
    first = engine.train(config(), tmp_path / 'first', candles(), snapshot())
    changed = snapshot()
    # Training decisions may use the last training candle at the cutoff itself.
    changed['change_from'] = (pd.Timestamp(first['period']['train']['end']) + pd.Timedelta(hours=1)).isoformat()
    second = engine.train(config(), tmp_path / 'second', candles(), changed)
    assert first['model_manifest']['files']['model.txt'] == second['model_manifest']['files']['model.txt']
    assert first['model_manifest']['files']['feature-matrix.feather'] != second['model_manifest']['files']['feature-matrix.feather']


def test_frozen_news_evidence_and_policy_cannot_be_replaced(tmp_path):
    candidate = engine.train(config(), tmp_path / 'model', candles(), snapshot())
    with pytest.raises(ValueError, match='observed reception'):
        engine.predict(candidate['artifact_dir'], current_candles(), snapshot())
    changed = snapshot('observed')
    changed['policy'] = {**POLICY, 'window_hours': 6}
    with pytest.raises(ValueError, match='policy differs'):
        engine.predict(candidate['artifact_dir'], current_candles(), changed)
    (tmp_path / 'model' / 'news-snapshot.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='hash mismatch'):
        engine.verify_model(candidate['artifact_dir'])
