"""Bounded public GDELT sampled archives, historical provenance and real observation time."""
from __future__ import annotations

import hashlib
import html
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import settings
from .terminal_news_features import SCHEMA, NEWS_FEATURES, POLICY, digest, expected_batches, factor_at, stamp, utc

_lock = threading.RLock()
MAX_ARCHIVE_BYTES = 24_000_000
MAX_UNCOMPRESSED = 160_000_000
SOURCE = 'GDELT GKG 2.1 sampled title metadata'


def now():
    return datetime.now(timezone.utc)


def root():
    path = settings.HOME / 'runtime' / 'terminal-news'
    path.mkdir(parents=True, exist_ok=True)
    return path


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    replace(temporary, path)


def replace(temporary, path):
    # Windows readers briefly deny replacement; never truncate a visible JSON file.
    for attempt in range(10):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(.05)


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'QuantPilot-NewsResearch/1.0'})
    with urllib.request.urlopen(request, timeout=25) as response:
        data = response.read(MAX_ARCHIVE_BYTES+1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError('News archive exceeds the bounded download budget')
    return data


def plain(value):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', ' ', html.unescape(value))).strip()[:400]


def parse_archive(key, payload):
    filename = key+'.gkg.csv'
    with zipfile.ZipFile(io.BytesIO(payload)) as zipped:
        info = zipped.getinfo(filename)
        if info.file_size > MAX_UNCOMPRESSED:
            raise ValueError('News archive expands beyond the bounded parsing budget')
        data = zipped.read(info).decode('utf-8', errors='replace')
    parsed, malformed, events = 0, 0, []
    archive_time = datetime.strptime(key, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).isoformat()
    for line in data.splitlines():
        columns = line.split('\t')
        if len(columns) != 27:
            malformed += 1
            continue
        parsed += 1
        found = re.search(r'<PAGE_TITLE>(.*?)</PAGE_TITLE>', columns[26], re.S)
        if not found:
            continue
        title = plain(found.group(1))
        text = title.lower()
        assets = []
        if re.search(r'\b(bitcoin|btc)\b', text):
            assets.append('BTC')
        if re.search(r'\b(ethereum|ether|eth)\b', text):
            assets.append('ETH')
        if not assets or re.search(r'\b(presale|airdrop|sponsored)\b', text):
            continue
        try:
            url = urlsplit(columns[4])
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
                continue
            canonical = urlunsplit((url.scheme, url.netloc.lower(), url.path, url.query, ''))[:2000]
        except ValueError:
            continue
        event = 'other'
        if re.search(r'\b(hack(?:ed|s)?|exploit(?:ed|s)?|breach|stolen|theft|vulnerability)\b', text):
            event = 'security'
        elif re.search(r'\b(sec|regulation|regulatory|regulator|ban(?:ned|s)?|legislation|lawsuit|approval)\b', text):
            event = 'regulation'
        elif re.search(r'\b(etf|price|market|rally|selloff|liquidity)\b', text):
            event = 'market'
        item = {'item_key': hashlib.sha256(canonical.encode()).hexdigest(), 'title': title,
            'source': url.hostname.lower(), 'url': canonical, 'assets': assets, 'event': event,
            'published_at': None, 'archive_at': archive_time, 'batch_id': key,
            'classification': POLICY['classification'], 'sentiment': 'unknown',
            'row_sha256': hashlib.sha256(line.encode()).hexdigest()}
        item['content_sha256'] = digest({k:item[k] for k in ('item_key','title','assets','event')})
        item['id'] = 'news-'+digest([key,item['item_key'],item['content_sha256']])[:32]
        events.append(item)
    if parsed < 1 or malformed > max(5, parsed*.01):
        raise ValueError('GDELT archive failed 27-column parsing quality checks')
    return events, parsed, malformed


def archive(key):
    if not re.fullmatch(r'\d{14}', key):
        raise ValueError('Invalid news batch key')
    when = datetime.strptime(key, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    if when.hour not in (0,6,12,18) or when.minute or when.second:
        raise ValueError('News batch is outside the frozen sampling grid')
    folder = root() / 'archives'
    folder.mkdir(exist_ok=True)
    path, meta_path = folder / (key+'.zip'), folder / (key+'.json')
    with _lock:
        cached = read(meta_path)
        if cached and path.is_file():
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != cached['sha256']:
                raise ValueError('Cached news archive hash mismatch')
            if cached.get('metadata_sha256'):
                if digest({k:v for k,v in cached.items() if k != 'metadata_sha256'}) != cached['metadata_sha256']:
                    raise ValueError('Cached news metadata hash mismatch')
            else:
                # Upgrade already downloaded archives without falsifying reception times.
                events, parsed, malformed = parse_archive(key, payload)
                cached.update(events=events, raw_rows=parsed, malformed_rows=malformed,
                              classification=POLICY['classification'])
                cached['metadata_sha256'] = digest(cached)
                write(meta_path, cached)
            if cached.get('id') != key or cached.get('classification') != POLICY['classification']:
                raise ValueError('Cached news metadata schema mismatch')
            return cached
        url = 'https://data.gdeltproject.org/gdeltv2/'+key+'.gkg.csv.zip'
        for attempt in range(3):
            try:
                payload = fetch(url)
                if not zipfile.is_zipfile(io.BytesIO(payload)):
                    raise zipfile.BadZipFile('News source returned a non-ZIP response')
                break
            except (OSError, urllib.error.URLError, zipfile.BadZipFile):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
        received = now().isoformat()
        events, parsed, malformed = parse_archive(key, payload)
        available = now().isoformat()
        metadata = {'id':key,'archive_at':when.isoformat(),'url':url,'sha256':hashlib.sha256(payload).hexdigest(),
            'bytes':len(payload),'raw_rows':parsed,'malformed_rows':malformed,'received_at':received,
            'parsed_at':available,'events':events,'status':'covered','classification':POLICY['classification']}
        metadata['metadata_sha256'] = digest(metadata)
        temporary = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        temporary.write_bytes(payload)
        replace(temporary, path)
        write(meta_path, metadata)
        return metadata


def bundle(archives, mode, error=None):
    events, batches = [], []
    for item in sorted(archives, key=lambda row: row['archive_at']):
        assumed = utc(item['archive_at'])+timedelta(hours=1)
        available = assumed if mode == 'historical_archive_assumption' else max(assumed, utc(item['parsed_at']))
        batch = {k:v for k,v in item.items() if k != 'events'}
        batch['available_at'] = available.isoformat()
        batches.append(batch)
        for source in item['events']:
            # Retain observations; factor_at counts each URL once at the cutoff.
            # Global dedup here would make current rolling windows disagree with history.
            events.append({**source,'available_at':available.isoformat(),'received_at':item['received_at'],
                'parsed_at':item['parsed_at'],'time_evidence':mode})
    value = {'schema':SCHEMA,'source':SOURCE,'policy':POLICY,'mode':mode,'events':events,'batches':batches,
             'error':error,'feature_names':NEWS_FEATURES}
    value['content_sha256'] = digest(value)
    value['id'] = 'news-snapshot-'+value['content_sha256'][:24]
    return value


def prepare_history(config, directory):
    if config['symbol'] not in POLICY['asset_scope']:
        raise ValueError('News models support BTC/USDT and ETH/USDT')
    start, end = utc(config['start']+'T00:00:00+00:00'), utc(config['end']+'T00:00:00+00:00')
    if not 14 <= (end-start).days <= 31 or start < utc('2025-01-01T00:00:00Z') or end > utc('2025-10-01T00:00:00Z'):
        raise ValueError('News research supports 14–31 days inside 2025-01-01 to 2025-10-01; Q4 stays reserved')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    needed = sorted({key for hour in range(int((end-start).total_seconds()/3600)+1)
                     for key in expected_batches(start+timedelta(hours=hour))})
    archives = []
    for index, key in enumerate(needed):
        if (directory/'CANCEL').exists():
            raise RuntimeError('News preparation cancelled')
        write(directory/'news-progress.json', {'phase':'preparing_news','done':index,'total':len(needed),'batch':key})
        try:
            archives.append(archive(key))
        except Exception as exc:
            write(directory/'news-progress.json', {'phase':'failed','done':index,'total':len(needed),'batch':key,'error':str(exc)[:500]})
            raise ValueError(f'News batch {key} unavailable or invalid; incomplete coverage cannot be filled with zeros: {type(exc).__name__}') from exc
    snapshot = bundle(archives, 'historical_archive_assumption')
    snapshot['coverage'] = {'status':'covered','from':start.isoformat(),'to':end.isoformat(),
        'expected':len(needed),'observed':len(archives),'missing':[], 'bytes':sum(a['bytes'] for a in archives),
        'events':len(snapshot['events']), 'sampling':POLICY['sampling_notice']}
    write(directory/'news-snapshot.json', snapshot)
    write(directory/'news-progress.json', {'phase':'succeeded','done':len(needed),'total':len(needed)})
    return snapshot


def current_snapshot(refresh=False):
    path = root()/'current.json'
    cached = read(path)
    moment = now()
    compatible = cached and cached.get('schema') == SCHEMA and cached.get('policy') == POLICY
    # Inference must never wait for downloads: the background collector owns refresh.
    if not refresh:
        if compatible:
            return cached
        empty = bundle([], 'observed')
        empty.update(checked_at=None, next_eligible_at=(moment.replace(minute=0,second=0,microsecond=0)+timedelta(hours=1)).isoformat())
        return empty
    if compatible and (moment-utc(cached['checked_at'])).total_seconds() < 45:
        return cached
    # Prefetch the upcoming batch after 15 minutes; it is not eligible until +1h.
    required = set(expected_batches(moment.replace(minute=0,second=0,microsecond=0)))
    need = set(required)
    floor = moment.replace(hour=(moment.hour//6)*6,minute=0,second=0,microsecond=0)
    if moment >= floor+timedelta(minutes=15):
        need.add(stamp(floor))
    archives, failures, prefetch_failures = [], [], []
    for key in sorted(need):
        try:
            archives.append(archive(key))
        except Exception as exc:
            (failures if key in required else prefetch_failures).append(f'{key}: {type(exc).__name__}')
    result = bundle(archives, 'observed', '; '.join(failures) or None)
    result['checked_at'] = moment.isoformat()
    result['prefetch_errors'] = prefetch_failures
    result['next_eligible_at'] = (moment.replace(minute=0,second=0,microsecond=0)+timedelta(hours=1)).isoformat()
    write(path, result)
    return result


def status(symbol='BTC/USDT', cutoff=None, refresh=False):
    snapshot = current_snapshot(refresh=refresh)
    cutoff = cutoff or now().replace(minute=0,second=0,microsecond=0).isoformat()
    result = factor_at(snapshot, symbol, cutoff)
    return {**result,'symbol':symbol,'source':SOURCE,'policy':POLICY,'checked_at':snapshot['checked_at'],
        'next_eligible_at':snapshot['next_eligible_at'],'historical_bounds':{'start':'2025-01-01','end':'2025-10-01','min_days':14,'max_days':31},
        'notice':'Sampled archive title events, not full news coverage. Current observations cannot be backdated.'}
