"""Bounded read-only CCXT adapters. All source IDs remain venue-specific."""
import math
import time
import ccxt
from . import public_store

VENUES = ('binance', 'okx', 'kraken')
SYMBOLS = ('BTC/USDT', 'ETH/USDT')


def make_exchange(venue):
    if venue not in VENUES:
        raise ValueError('Unknown exchange')
    return getattr(ccxt, venue)({'timeout': 12000, 'enableRateLimit': True,
        'requests_trust_env': True,
        'maxRetriesOnFailure': 0, 'options': {'defaultType': 'spot', 'fetchMarkets': {'types': ['spot']}}})


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def snapshot(venue, factory=make_exchange):
    exchange = factory(venue)
    markets = exchange.load_markets()
    observations = []
    for symbol in SYMBOLS:
        market = markets.get(symbol)
        if not market or not market.get('spot'):
            observations.append({'symbol': symbol, 'availability': 'unavailable', 'error': 'spot_market_not_listed'})
            continue
        item = {'symbol': symbol, 'venue_symbol': market.get('id'), 'active': market.get('active'),
            'precision_mode': exchange.precisionMode, 'precision': {k: number(v) for k, v in market.get('precision', {}).items()},
            'limits': {k: {kk: number(vv) for kk, vv in v.items()} for k, v in market.get('limits', {}).items() if isinstance(v, dict)},
            'availability': 'available', 'ticker': None, 'candles': [], 'errors': []}
        try:
            ticker = exchange.fetch_ticker(symbol)
            item['ticker'] = {k: number(ticker.get(k)) for k in ('timestamp', 'last', 'bid', 'ask', 'baseVolume', 'quoteVolume', 'percentage')}
            item['ticker']['observed_at'] = public_store.now()
        except Exception:
            item['errors'].append('ticker_request_failed')
        try:
            candles = exchange.fetch_ohlcv(symbol, '1h', limit=48)
            # Last candle can be in progress. closed_at and available_at are explicit.
            observed = public_store.now()
            item['candles'] = [{'timestamp': row[0], 'open': row[1], 'high': row[2], 'low': row[3], 'close': row[4], 'volume': row[5],
                'closed_at_ms': row[0] + 3600000, 'closed': row[0] + 3600000 <= int(time.time() * 1000), 'observed_at': observed}
                for row in candles[-48:] if len(row) >= 6 and all(number(v) is not None for v in row[:6])]
        except Exception:
            item['errors'].append('ohlcv_request_failed')
        if item['errors']:
            item['availability'] = 'partial' if item['ticker'] or item['candles'] else 'unavailable'
        closes = sorted({row['closed_at_ms'] for row in item['candles'] if row['closed']})
        item['last_closed_at_ms'] = closes[-1] if closes else None
        item['missing_interval_count'] = sum(max(0, int((right-left)/3600000)-1) for left,right in zip(closes,closes[1:]))
        observations.append(item)
    return {'source': venue, 'adapter': 'ccxt', 'adapter_version': ccxt.__version__, 'market_type': 'spot', 'timeframe': '1h',
            'observed_at': public_store.now(), 'markets': observations,
            'capabilities': {'public_markets': 'verified', 'public_ticker': 'verified' if any(x.get('ticker') for x in observations) else 'unverified',
                'public_ohlcv': 'verified' if any(x.get('candles') for x in observations) else 'unverified',
                'historical_backtest': 'unverified', 'dry_run': 'unverified', 'live_trading': 'unverified'},
            'execution_enabled': False, 'purpose': 'Public market observation; independent of the fixed historical experiment dataset'}


def refresh(venue):
    try:
        result = snapshot(venue)
    except Exception:
        public_store.save_cache('exchange:' + venue, error='public_exchange_request_failed')
        raise ValueError('public_exchange_request_failed') from None
    public_store.save_cache('exchange:' + venue, result)
    return {'source': venue, 'market_count': len(result['markets'])}


def view(venue):
    if venue not in VENUES:
        raise ValueError('Unknown exchange')
    result = {'source': venue, **public_store.cached('exchange:' + venue), 'execution_enabled': False}
    timestamp = int(time.time()*1000)
    if result['data']:
        for market in result['data']['markets']:
            published = (market.get('ticker') or {}).get('timestamp')
            market['ticker_age_ms'] = timestamp-published if published is not None else None
            market['ticker_stale'] = published is None or timestamp-published > 300000 or published > timestamp+60000
            closed = market.get('last_closed_at_ms')
            market['candles_stale'] = closed is None or timestamp-closed > 7200000
    return result
