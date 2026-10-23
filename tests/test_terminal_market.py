import time
import pandas as pd
import pytest
from quantpilot import terminal_market as market


def test_market_directory_omits_large_permission_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(market.settings, 'HOME', tmp_path)
    def directory(endpoint, **params):
        assert endpoint == 'exchangeInfo'
        assert params['showPermissionSets'] == 'false'
        return {'symbols': [
            {'baseAsset': 'SOL', 'quoteAsset': 'USDT', 'status': 'TRADING', 'isSpotTradingAllowed': True},
            {'baseAsset': 'OLD', 'quoteAsset': 'USDT', 'status': 'BREAK', 'isSpotTradingAllowed': True},
        ]}
    monkeypatch.setattr(market, '_json', directory)
    result = market.markets()
    assert result['symbols'] == ['SOL/USDT'] and not result['stale']


def fake_api(endpoint, **params):
    stamp = int(time.time()*1000)
    current_open = stamp//market.HOUR*market.HOUR
    if endpoint == 'ticker/24hr':
        return {'lastPrice':'101','bidPrice':'100','askPrice':'102','closeTime':stamp,'priceChangePercent':'1'}
    return [[current_open-(80-i)*market.HOUR,'100','103','99','101','2'] for i in range(81)]


def test_current_closed_bars_and_fresh_quote(tmp_path, monkeypatch):
    monkeypatch.setattr(market.settings,'HOME',tmp_path)
    monkeypatch.setattr(market,'_json',fake_api)
    data = market.snapshot('SOL/USDT')
    assert not data['stale']
    assert len(data['candles']) == 80
    assert data['last_closed_at_ms'] <= int(time.time()*1000)
    assert len(market.closed_frame(data)) == 80


def test_failed_refresh_does_not_advance_cached_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(market.settings,'HOME',tmp_path)
    monkeypatch.setattr(market,'_json',fake_api)
    good = market.snapshot('BTC/USDT')
    path = market._cache_root() / 'BTC_USDT.json'
    stored = market._read(path)
    stored['_fetched'] -= 20
    market._write(path,stored)
    def fail(*args,**kwargs):
        raise OSError('offline')
    monkeypatch.setattr(market,'_json',fail)
    failed = market.snapshot('BTC/USDT',refresh=True)
    assert failed['stale'] and failed['error']
    assert failed['observed_at'] == good['observed_at']
    subsequent = market.snapshot('BTC/USDT')
    assert subsequent['stale'] and subsequent['error']
    with pytest.raises(ValueError):
        market.closed_frame(failed)


def test_missing_current_hour_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(market.settings,'HOME',tmp_path)
    def missing(endpoint,**params):
        value = fake_api(endpoint,**params)
        if endpoint == 'klines':
            del value[10]
        return value
    monkeypatch.setattr(market,'_json',missing)
    result = market.snapshot('ETH/USDT')
    assert result['stale'] and not result['candles']


def test_quote_before_latest_candle_closed_is_not_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(market.settings, 'HOME', tmp_path)
    monkeypatch.setattr(market, '_json', fake_api)
    data = market.snapshot('BTC/USDT')
    data['ticker']['timestamp'] = data['last_closed_at_ms'] - 1
    assert market._freshness(data)['stale']


def test_historical_reserved_window_rejected(tmp_path):
    with pytest.raises(ValueError, match='Q4'):
        market.prepare_dataset({'symbol':'BTC/USDT','start':'2025-09-01','end':'2025-11-01'},tmp_path)


def test_existing_source_hash_mismatch_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(market.settings, 'ROOT', tmp_path)
    source = tmp_path / 'data' / 'altered.feather'
    source.parent.mkdir()
    source.write_bytes(b'altered source')
    monkeypatch.setattr(market, '_read', lambda path: {'id': 'fixture', 'files': [
        {'pair': 'BTC/USDT', 'path': 'data/altered.feather', 'sha256': '0'*64}]})
    with pytest.raises(ValueError, match='hash mismatch'):
        market.prepare_dataset({'symbol':'BTC/USDT','start':'2025-01-01','end':'2025-02-01'}, tmp_path / 'job')


def test_downloaded_archive_checksum_is_required(tmp_path, monkeypatch):
    monkeypatch.setattr(market, '_read', lambda path: None)
    monkeypatch.setattr(market, '_fetch', lambda url: b'0'*64 if url.endswith('.CHECKSUM') else b'altered archive')
    with pytest.raises(ValueError, match='checksum mismatch'):
        market.prepare_dataset({'symbol':'SOL/USDT','start':'2025-01-01','end':'2025-02-01'}, tmp_path)


def test_candle_quality_and_symbol_boundaries():
    with pytest.raises(ValueError):
        market.validate_symbol('../BTC/USDT')
    dates = pd.date_range('2025-01-01',periods=3,freq='h',tz='UTC')
    frame = pd.DataFrame({'date':dates,'open':[1]*3,'high':[2]*3,'low':[.5]*3,'close':[1]*3,'volume':[1]*3})
    assert len(market._validate_history(frame,dates[0],dates[-1]+pd.Timedelta(hours=1))) == 3
    frame.loc[1,'date'] = dates[0]
    with pytest.raises(ValueError):
        market._validate_history(frame,dates[0],dates[-1]+pd.Timedelta(hours=1))
