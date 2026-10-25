"""Causal aggregation for a declared, sparse GDELT title sampling protocol."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

SCHEMA = 'gdelt-sampled-6h-news4-v1'
NEWS_FEATURES = ['news_count_24h', 'news_sources_24h', 'news_regulation_24h', 'news_security_24h']
POLICY = {
    'id': 'gdelt-6h-title-events-v1', 'schedule_utc_hours': [0, 6, 12, 18], 'window_hours': 24,
    'archive_delay_minutes': 60, 'asset_scope': ['BTC/USDT', 'ETH/USDT'],
    'features': 'log1p_unique_sampled_article_and_source_counts',
    'availability': 'historical:archive_plus_delay; current:max(parsed_at,archive_plus_delay)',
    'revision': 'filter_available_then_latest_per_item_key',
    'classification': 'frozen_title_keyword_rules_v1',
    'observation': 'retain_repeated_archive_observations; unique_URL_within_rolling_window',
    'sampling_notice': 'One 15-minute GKG batch per six hours; not exhaustive news coverage',
}


def utc(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('News timestamps require a timezone')
    return parsed.astimezone(timezone.utc)


def stamp(value):
    return utc(value).strftime('%Y%m%d%H%M%S')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def expected_batches(cutoff):
    cutoff = utc(cutoff)
    lower, upper = cutoff - timedelta(hours=24), cutoff - timedelta(hours=1)
    cursor = lower.replace(hour=(lower.hour // 6)*6, minute=0, second=0, microsecond=0)
    if cursor < lower:
        cursor += timedelta(hours=6)
    result = []
    while cursor <= upper:
        result.append(stamp(cursor))
        cursor += timedelta(hours=6)
    return result


def factor_at(snapshot, symbol, cutoff):
    if symbol not in POLICY['asset_scope']:
        raise ValueError('News event factors currently support BTC/USDT and ETH/USDT only')
    if snapshot.get('schema') != SCHEMA or snapshot.get('policy') != POLICY:
        raise ValueError('News snapshot schema or sampling policy is incompatible')
    cutoff = utc(cutoff)
    expected = expected_batches(cutoff)
    batches = {item['id']: item for item in snapshot.get('batches', [])}
    missing = [key for key in expected if key not in batches or batches[key].get('status') != 'covered'
               or utc(batches[key]['available_at']) > cutoff]
    state = 'source_error' if snapshot.get('error') else 'missing' if missing else 'covered'
    coverage = {'status': state, 'expected': len(expected), 'observed': len(expected)-len(missing),
        'missing': missing, 'from': (cutoff-timedelta(hours=24)).isoformat(), 'to': cutoff.isoformat(),
        'sampling': POLICY['sampling_notice'], 'error': snapshot.get('error')}
    # Crucially, a later revision must not hide the version available at this cutoff.
    latest = {}
    for event in snapshot.get('events', []):
        available = utc(event['available_at'])
        if available > cutoff:
            continue
        key = event['item_key']
        previous = latest.get(key)
        if previous is None or (available, event['id']) > (utc(previous['available_at']), previous['id']):
            latest[key] = event
    asset = symbol.split('/')[0]
    lower = cutoff-timedelta(hours=24)
    events = sorted([event for event in latest.values() if asset in event['assets']
        and lower <= utc(event['archive_at']) <= cutoff-timedelta(hours=1)],
        key=lambda item: (item['available_at'], item['id']))
    counts = [len(events), len({event['source'] for event in events}),
              sum(event['event'] == 'regulation' for event in events),
              sum(event['event'] == 'security' for event in events)]
    return {'schema': SCHEMA, 'cutoff': cutoff.isoformat(),
        'values': dict(zip(NEWS_FEATURES, map(math.log1p, counts), strict=True)) if state == 'covered' else None,
        'counts': dict(zip(NEWS_FEATURES, counts, strict=True)) if state == 'covered' else None,
        'coverage': coverage, 'event_ids': [event['id'] for event in events], 'events': events,
        'max_event_available_at': max((event['available_at'] for event in events), default=None),
        'snapshot_id': snapshot.get('id'), 'mode': snapshot['mode']}
