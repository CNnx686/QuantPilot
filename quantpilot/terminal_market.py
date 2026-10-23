"""Public Binance spot data for the personal terminal. Never uses account credentials."""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import settings

HOUR = 3_600_000
SOURCE = 'Binance public spot REST'
_lock = threading.RLock()


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_symbol(symbol):
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{2,16}/USDT', symbol):
        raise ValueError('Select a Binance spot BASE/USDT symbol')
    return symbol


def _cache_root():
    path = settings.HOME / 'runtime' / 'terminal-market'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path, value):
    temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'QuantPilot-PersonalTerminal/1.0'})
    with urllib.request.urlopen(request, timeout=15) as response:
        result = response.read(8_000_001)
    if len(result) > 8_000_000:
        raise ValueError('Public data response exceeds the bounded download limit')
    return result


def _json(endpoint, **params):
    query = urllib.parse.urlencode(params)
    return json.loads(_fetch('https://api.binance.com/api/v3/' + endpoint + ('?' + query if query else '')))


def markets(refresh=False):
    """Return real active USDT spot symbols; a failed refresh remains visibly stale."""
    with _lock:
        path = _cache_root() / 'markets.json'
        cached = _read(path)
        age = time.time() - (cached or {}).get('_fetched', 0)
        if cached and age < (15 if refresh else 3600):
            return cached
        try:
            # Omit the large nested permission sets; the explicit spot flag is sufficient.
            info = _json('exchangeInfo', permissions='SPOT', showPermissionSets='false')
            entries = [{'symbol': f"{row['baseAsset']}/USDT", 'base': row['baseAsset'],
                        'active': True, 'exchange': 'binance', 'market_type': 'spot'}
                       for row in info['symbols'] if row.get('quoteAsset') == 'USDT'
                       and row.get('status') == 'TRADING' and row.get('isSpotTradingAllowed')
                       and re.fullmatch(r'[A-Z0-9]{2,16}', row['baseAsset'])]
            preferred = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
            entries.sort(key=lambda x: (preferred.index(x['symbol']) if x['symbol'] in preferred else 99, x['symbol']))
            result = {'symbols': [x['symbol'] for x in entries], 'markets': entries,
                      'source': SOURCE, 'observed_at': now(), 'stale': False, 'error': None,
                      '_fetched': time.time()}
            if not entries:
                raise ValueError('No active spot symbols returned')
            _write(path, result)
            return result
        except Exception as exc:
            return {**(cached or {'symbols': [], 'markets': [], 'source': SOURCE, 'observed_at': None}),
                    'stale': True, 'error': f'Public market directory unavailable ({type(exc).__name__})'}


def _freshness(value):
    result = dict(value)
    stamp = int(time.time() * 1000)
    ticker = result.get('ticker') or {}
    observed = result.get('_fetched', 0) * 1000
    quote_time = ticker.get('timestamp', 0)
    close_time = result.get('last_closed_at_ms', 0)
    quote_age = stamp - quote_time
    result['quote_age_ms'] = quote_age
    result['stale'] = bool(result.get('error') or stamp-observed > 120_000 or
                           quote_age > 120_000 or quote_age < -60_000 or
                           quote_time < close_time or
                           not close_time or stamp-close_time > 2*HOUR or close_time > stamp)
    return result


def snapshot(symbol, refresh=False):
    validate_symbol(symbol)
    with _lock:
        path = _cache_root() / (symbol.replace('/', '_') + '.json')
        cached = _read(path)
        if cached and time.time()-cached.get('_fetched', 0) < (15 if refresh else 30):
            return _freshness(cached)
        try:
            venue_symbol = symbol.replace('/', '')
            rows = _json('klines', symbol=venue_symbol, interval='1h', limit=256)
            ticker = _json('ticker/24hr', symbol=venue_symbol)
            stamp = int(time.time()*1000)
            bars = []
            for row in rows:
                if len(row) < 6:
                    raise ValueError('Malformed candle')
                opened = int(row[0])
                if opened + HOUR > stamp:
                    continue
                values = [float(x) for x in row[1:6]]
                o, h, lo, c, v = values
                if not np.isfinite(values).all() or min(o,h,lo,c) <= 0 or v < 0 or lo > min(o,c) or h < max(o,c):
                    raise ValueError('Invalid public candle')
                bars.append({'date': pd.Timestamp(opened, unit='ms', tz='UTC').isoformat(),
                             'timestamp': opened, 'open': o, 'high': h, 'low': lo, 'close': c, 'volume': v,
                             'closed_at': pd.Timestamp(opened+HOUR, unit='ms', tz='UTC').isoformat()})
            if len(bars) < 64 or any(b['timestamp']-a['timestamp'] != HOUR for a,b in zip(bars,bars[1:])):
                raise ValueError('Current candles have insufficient warmup or missing intervals')
            prices = {name: float(ticker[key]) for name,key in [('last','lastPrice'),('bid','bidPrice'),('ask','askPrice')]}
            if not np.isfinite(list(prices.values())).all() or min(prices.values()) <= 0 or prices['ask'] < prices['bid']:
                raise ValueError('Invalid public quote')
            result = {'symbol': symbol, 'source': SOURCE, 'exchange': 'binance', 'timeframe': '1h',
                      'observed_at': now(), 'stale': False, 'error': None, 'candles': bars,
                      'ticker': {**prices, 'timestamp': int(ticker['closeTime']),
                                 'percentage': float(ticker.get('priceChangePercent', 0))},
                      'last_closed_at': bars[-1]['closed_at'], 'last_closed_at_ms': bars[-1]['timestamp']+HOUR,
                      'timestamp_meaning': 'date is open time; OHLCV usable only at closed_at', '_fetched': time.time()}
            _write(path, result)
            return _freshness(result)
        except Exception as exc:
            failed = {**(cached or {'symbol':symbol,'source':SOURCE,'candles':[],'ticker':None,
                                   'observed_at':None,'last_closed_at':None}),
                      'stale': True, 'error': f'Public quote/candle request failed ({type(exc).__name__})'}
            # Preserve values and their timestamps but persist failed availability until recovery.
            # Otherwise a default read could erase a failed refresh while the old cache is young.
            _write(path, failed)
            return failed


def closed_frame(value):
    if value.get('stale') or value.get('error'):
        raise ValueError(value.get('error') or 'Market data is stale')
    frame = pd.DataFrame(value.get('candles', []))
    if frame.empty:
        raise ValueError('No closed market candles')
    frame['date'] = pd.to_datetime(frame['date'], utc=True)
    frame = frame[frame['date'] + pd.Timedelta(hours=1) <= pd.Timestamp.now(tz='UTC')]
    return frame[['date','open','high','low','close','volume']].reset_index(drop=True)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_history(frame, lower, upper):
    frame = frame.sort_values('date').reset_index(drop=True)
    expected = pd.date_range(lower, upper, freq='h', inclusive='left')
    values = frame[['open','high','low','close','volume']].to_numpy(dtype=float)
    if (len(frame) != len(expected) or frame['date'].duplicated().any()
            or not pd.DatetimeIndex(frame['date']).equals(expected)
            or not np.isfinite(values).all() or (values[:,:4] <= 0).any() or (values[:,4] < 0).any()
            or (frame['low'] > frame[['open','close']].min(axis=1)).any()
            or (frame['high'] < frame[['open','close']].max(axis=1)).any()):
        raise ValueError('Historical data failed contiguous UTC hours / OHLCV quality checks')
    return frame


def prepare_dataset(config, directory):
    """Create a verified immutable task input, reusing official archives when possible."""
    symbol = validate_symbol(config['symbol'])
    start, end = pd.Timestamp(config['start'], tz='UTC'), pd.Timestamp(config['end'], tz='UTC')
    if start < pd.Timestamp('2025-01-01', tz='UTC') or end > pd.Timestamp('2025-10-01', tz='UTC'):
        raise ValueError('Development dates must be within 2025-01-01 to 2025-10-01; Q4 remains reserved')
    if not 7 <= (end-start).days <= 273:
        raise ValueError('Choose a development interval of 7 to 273 days')
    warmup = max(72, int(config.get('parameters', {}).get('slow', 48)) + 2)
    lower = start-pd.Timedelta(hours=warmup)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    sources, frame = [], None
    for manifest_name in ['binance-week2.json','binance-2025q1.json']:
        manifest = _read(settings.ROOT / 'data' / 'manifests' / manifest_name)
        for entry in (manifest or {}).get('files', []):
            if entry.get('pair') != symbol:
                continue
            source_path = (settings.ROOT / entry['path']).resolve()
            if not source_path.is_relative_to((settings.ROOT / 'data').resolve()) or not source_path.is_file():
                continue
            if _hash(source_path) != entry['sha256']:
                raise ValueError('Existing historical snapshot hash mismatch; refusing altered data')
            existing = pd.read_feather(source_path)
            existing['date'] = pd.to_datetime(existing['date'], utc=True)
            if existing['date'].min() <= lower and existing['date'].max()+pd.Timedelta(hours=1) >= end:
                frame = existing[(existing['date'] >= lower) & (existing['date'] < end)].copy()
                sources.append({'kind':'verified_existing_archive','dataset_id':manifest['id'],
                                'path':entry['path'],'sha256':entry['sha256']})
                break
        if frame is not None:
            break
    if frame is None:
        frames = []
        first = lower.replace(day=1,hour=0,minute=0,second=0)
        for month in pd.date_range(first, end, freq='MS', inclusive='left').strftime('%Y-%m'):
            filename = symbol.replace('/','') + '-1h-' + month + '.zip'
            url = 'https://data.binance.vision/data/spot/monthly/klines/' + symbol.replace('/','') + '/1h/' + filename
            try:
                payload = _fetch(url)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise ValueError(f'{symbol} has no official archive for {month}, including required warmup; choose a covered historical interval') from exc
                raise
            checksum = _fetch(url+'.CHECKSUM').decode('utf-8').split()[0]
            digest = hashlib.sha256(payload).hexdigest()
            if digest != checksum:
                raise ValueError('Official archive checksum mismatch: ' + filename)
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                member = filename.replace('.zip','.csv')
                if archive.getinfo(member).file_size > 10_000_000:
                    raise ValueError('Archive member exceeds bounded hourly data size')
                raw = pd.read_csv(archive.open(member), header=None)
            times = pd.to_numeric(raw[0])
            milliseconds = times.where(times < 10**14, times//1000)
            normalized = pd.DataFrame({'date':pd.to_datetime(milliseconds,unit='ms',utc=True)})
            for i,column in enumerate(['open','high','low','close','volume'],1):
                normalized[column] = pd.to_numeric(raw[i]).astype(float)
            frames.append(normalized)
            sources.append({'kind':'official_monthly_archive','url':url,'checksum_url':url+'.CHECKSUM','sha256':digest})
        frame = pd.concat(frames,ignore_index=True)
        frame = frame[(frame['date'] >= lower) & (frame['date'] < end)].copy()
    frame = _validate_history(frame, lower, end)
    target = directory / 'market.feather'
    frame.to_feather(target)
    _write(directory / 'data-provenance.json', {'symbol':symbol,'source':'Binance public archives',
        'retrieved_at':now(),'sources':sources,'data_sha256':_hash(target),'rows':len(frame),
        'warmup_hours':warmup,'start':lower.isoformat(),'end':end.isoformat(),'timezone':'UTC',
        'quality':{'status':'passed','missing':0,'duplicates':0},
        'timestamp_meaning':'candle open; available one hour later','purpose':'development; Q4 reserved'})
    return frame
