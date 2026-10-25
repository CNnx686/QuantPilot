"""Paired price/news experiment evidence. Configuration and price snapshots must match."""
from copy import deepcopy


VARIANTS = ('price_only', 'price_news')


def initial(config):
    common = {key: deepcopy(value) for key, value in config.items() if key not in {'name', 'news'}}
    return {'status': 'running', 'arms': {arm: {'status': 'pending'} for arm in VARIANTS},
            'comparable': False, 'reasons': ['Both arms must finish before comparison'],
            'delta': None, 'config': common, 'news_snapshot_id': None}


def arm_result(candidate):
    return {'status': 'succeeded', 'candidate_id': candidate['id'], 'metrics': candidate['metrics'],
            'period': candidate['period'], 'equity': candidate.get('equity', []),
            'features': candidate['model_manifest'].get('features', []),
            'price_snapshot_sha256': candidate['model_manifest']['files'].get('snapshot.feather')}


def complete(summary, candidates):
    indexed = {item['variant']: item for item in candidates}
    if set(indexed) != set(VARIANTS) or len(candidates) != 2:
        raise ValueError('Expected exactly one price arm and one news arm')
    price, news = indexed['price_only'], indexed['price_news']
    reasons = []
    configs = [item['model_manifest']['config'] for item in (price, news)]
    comparable_configs = [{key: value for key, value in config.items() if key != 'news'} for config in configs]
    if comparable_configs[0] != comparable_configs[1] or configs[0].get('news') is not False or configs[1].get('news') is not True:
        reasons.append('The experiment arms differ in more than the news feature switch')
    if price['period'] != news['period']:
        reasons.append('Training/evaluation periods differ')
    hashes = [item['model_manifest']['files'].get('snapshot.feather') for item in (price, news)]
    if not hashes[0] or hashes[0] != hashes[1]:
        reasons.append('Price data snapshots differ')
    result = {**summary, 'status': 'complete' if not reasons else 'failed',
              'arms': {variant: arm_result(indexed[variant]) for variant in VARIANTS},
              'comparable': not reasons, 'reasons': reasons,
              'delta': {key: news['metrics'][key] - price['metrics'][key]
                        for key in ('net_return', 'max_drawdown', 'trades')} if not reasons else None,
              'interpretation': 'News minus price-only on one shared development protocol; no causal or profitability claim'}
    return result
