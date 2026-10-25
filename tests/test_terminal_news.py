import io
import math
import zipfile
from datetime import timedelta
from copy import deepcopy

import pytest

from quantpilot import terminal_news as news
from quantpilot.terminal_news_features import POLICY, SCHEMA, factor_at, expected_batches, utc


CUTOFF = '2025-01-02T01:00:00+00:00'


def snapshot(events=None):
    return {'schema':SCHEMA,'policy':deepcopy(POLICY),'mode':'historical_archive_assumption','id':'snapshot-test',
        'events': events or [], 'error':None, 'batches':[{'id':key,'status':'covered',
            'available_at':(news.datetime.strptime(key,'%Y%m%d%H%M%S').replace(tzinfo=news.timezone.utc)+timedelta(hours=1)).isoformat()}
            for key in expected_batches(CUTOFF)]}


def event(identifier='v1', available='2025-01-01T19:00:00+00:00', kind='regulation', **extra):
    return {'id':identifier,'item_key':'same-url','source':'example.org','assets':['BTC'],
        'archive_at':'2025-01-01T18:00:00+00:00','available_at':available,'event':kind,**extra}


def test_asof_filters_before_latest_revision_and_does_not_rewrite_past():
    initial = event()
    future = event('v2','2025-01-02T02:00:00+00:00','security')
    result = factor_at(snapshot([initial,future]),'BTC/USDT',CUTOFF)
    assert result['event_ids'] == ['v1']
    assert result['values']['news_regulation_24h'] == math.log1p(1)
    assert result['values']['news_security_24h'] == 0
    assert factor_at(snapshot([initial]),'BTC/USDT',CUTOFF)['values'] == result['values']


def test_normal_zero_is_different_from_missing_and_source_failure():
    data = snapshot()
    result = factor_at(data,'BTC/USDT',CUTOFF)
    assert result['coverage']['status'] == 'covered'
    assert list(result['values'].values()) == [0]*4
    data['batches'].pop()
    assert factor_at(data,'BTC/USDT',CUTOFF)['values'] is None
    data['error'] = 'network offline'
    assert factor_at(data,'BTC/USDT',CUTOFF)['coverage']['status'] == 'source_error'


def test_current_observation_cannot_be_backdated_to_previous_close():
    data = snapshot([event()])
    data['mode'] = 'observed'
    data['batches'][0]['available_at'] = '2025-01-02T01:00:01+00:00'
    assert factor_at(data,'BTC/USDT',CUTOFF)['coverage']['status'] == 'missing'


def test_duplicate_url_counted_once_and_assets_are_filtered():
    data = snapshot([event(),event('v2','2025-01-01T20:00:00+00:00','security'),
                     event('eth',item_key='other-url',assets=['ETH'])])
    result = factor_at(data,'BTC/USDT',CUTOFF)
    assert result['counts']['news_count_24h'] == 1
    assert result['counts']['news_security_24h'] == 1
    assert result['event_ids'] == ['v2']


def test_policy_and_unsupported_assets_cannot_silently_fallback():
    with pytest.raises(ValueError,match='BTC'):
        factor_at(snapshot(),'SOL/USDT',CUTOFF)
    data = snapshot()
    data['policy']['window_hours'] = 12
    with pytest.raises(ValueError,match='incompatible'):
        factor_at(data,'BTC/USDT',CUTOFF)
    with pytest.raises(ValueError,match='timezone'):
        factor_at(snapshot(),'BTC/USDT','2025-01-01')


def raw_zip(title, *, count=27):
    columns = ['']*count
    columns[4] = 'https://news.example/article'
    columns[-1] = '<PAGE_TITLE>'+title+'</PAGE_TITLE>'
    out = io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('20250101000000.gkg.csv','\t'.join(columns))
    return out.getvalue()


def test_title_parser_preserves_source_and_unclaimed_publication_time():
    events, parsed, malformed = news.parse_archive('20250101000000',raw_zip('Bitcoin &amp; Ethereum regulation'))
    assert parsed == 1 and malformed == 0
    assert events[0]['assets'] == ['BTC','ETH'] and events[0]['event'] == 'regulation'
    assert events[0]['published_at'] is None and events[0]['sentiment'] == 'unknown'
    assert news.parse_archive('20250101000000',raw_zip('Bitcoin airdrop presale'))[0] == []
    with pytest.raises(ValueError,match='quality'):
        news.parse_archive('20250101000000',raw_zip('Bitcoin',count=26))


def test_current_and_historical_availability_are_distinct():
    item = {'id':'20250101000000','archive_at':'2025-01-01T00:00:00+00:00',
        'parsed_at':'2026-10-25T04:00:00+00:00','received_at':'2026-10-25T03:59:59+00:00',
        'events':[event()], 'status':'covered'}
    historic = news.bundle([item],'historical_archive_assumption')
    current = news.bundle([item],'observed')
    assert historic['events'][0]['available_at'] == '2025-01-01T01:00:00+00:00'
    assert current['events'][0]['available_at'] == item['parsed_at']
    assert historic['events'][0]['received_at'] == current['events'][0]['received_at']


def test_archive_cache_hash_is_required(tmp_path, monkeypatch):
    monkeypatch.setattr(news.settings,'HOME',tmp_path)
    monkeypatch.setattr(news,'fetch',lambda url:raw_zip('Bitcoin regulation'))
    first = news.archive('20250101000000')
    assert first['raw_rows'] == 1
    (news.root()/'archives'/'20250101000000.zip').write_bytes(b'altered')
    with pytest.raises(ValueError,match='hash mismatch'):
        news.archive('20250101000000')


def test_archive_metadata_hash_and_transient_invalid_response(tmp_path, monkeypatch):
    monkeypatch.setattr(news.settings,'HOME',tmp_path)
    responses = iter([b'temporary upstream error', raw_zip('Bitcoin regulation')])
    monkeypatch.setattr(news,'fetch',lambda url:next(responses))
    monkeypatch.setattr(news.time,'sleep',lambda seconds:None)
    first = news.archive('20250101000000')
    first['events'][0]['event'] = 'security'
    news.write(news.root()/'archives'/'20250101000000.json',first)
    with pytest.raises(ValueError,match='metadata hash mismatch'):
        news.archive('20250101000000')


def test_full_history_and_rolling_observations_produce_identical_inputs():
    archives = []
    for key in ['20241231180000', *expected_batches(CUTOFF)]:
        at = news.datetime.strptime(key,'%Y%m%d%H%M%S').replace(tzinfo=news.timezone.utc).isoformat()
        archives.append({'id':key,'archive_at':at,'parsed_at':at,'received_at':at,
            'events':[event(key,archive_at=at)],'status':'covered'})
    complete = news.bundle(archives,'historical_archive_assumption')
    rolling = news.bundle(archives[1:],'observed')
    assert len(complete['events']) == len(archives)
    assert factor_at(complete,'BTC/USDT',CUTOFF)['values'] == factor_at(rolling,'BTC/USDT',CUTOFF)['values']
    assert factor_at(complete,'BTC/USDT',CUTOFF)['counts']['news_count_24h'] == 1


def test_failed_future_prefetch_does_not_invalidate_current_covered_input(tmp_path, monkeypatch):
    monkeypatch.setattr(news.settings,'HOME',tmp_path)
    moment = utc('2025-01-02T06:20:00Z')
    monkeypatch.setattr(news,'now',lambda:moment)
    def cached_archive(key):
        if key == '20250102060000':
            raise OSError('future batch not yet available')
        at = news.datetime.strptime(key,'%Y%m%d%H%M%S').replace(tzinfo=news.timezone.utc).isoformat()
        return {'id':key,'archive_at':at,'parsed_at':at,'received_at':at,'events':[],'status':'covered'}
    monkeypatch.setattr(news,'archive',cached_archive)
    data = news.current_snapshot(refresh=True)
    assert data['prefetch_errors'] and data['error'] is None
    assert factor_at(data,'BTC/USDT',moment.replace(minute=0))['coverage']['status'] == 'covered'


def test_inference_reads_only_observed_cache_and_never_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(news.settings,'HOME',tmp_path)
    monkeypatch.setattr(news,'archive',lambda key:pytest.fail('inference must not perform network I/O'))
    data = news.current_snapshot()
    assert data['checked_at'] is None
    assert factor_at(data,'BTC/USDT',CUTOFF)['values'] is None
    cached = snapshot()
    cached.update(checked_at='2025-01-02T01:00:00Z',next_eligible_at='2025-01-02T02:00:00Z',mode='observed')
    news.write(news.root()/'current.json',cached)
    assert news.current_snapshot() == cached


def test_atomic_progress_replace_retries_temporary_windows_reader_lock(tmp_path, monkeypatch):
    target = tmp_path/'news-progress.json'
    news.write(target, {'done':1})
    original = news.Path.replace
    calls = []
    def busy_once(path, destination):
        calls.append(path)
        if len(calls) == 1:
            assert news.read(destination) == {'done':1}
            raise PermissionError('Windows reader sharing violation')
        return original(path, destination)
    monkeypatch.setattr(news.Path,'replace',busy_once)
    monkeypatch.setattr(news.time,'sleep',lambda seconds:None)
    news.write(target,{'done':2})
    assert len(calls) == 2 and news.read(target) == {'done':2}


def test_historical_budget_and_final_period_protected(tmp_path):
    for start,end in [('2025-01-01','2025-04-01'),('2025-10-01','2025-10-15')]:
        with pytest.raises(ValueError,match='Q4'):
            news.prepare_history({'symbol':'BTC/USDT','start':start,'end':end},tmp_path)
