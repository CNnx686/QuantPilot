import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


path = Path(__file__).resolve().parents[1] / 'scripts' / 'export_strategy_research.py'
spec = spec_from_file_location('export_strategy_research', path)
export = module_from_spec(spec)
spec.loader.exec_module(export)


def test_missing_research_is_partial_without_invented_results(tmp_path):
    result = export.build(tmp_path / 'missing-news', tmp_path / 'missing-ppo.json')
    assert result['status'] == 'partial'
    assert result['news']['windows'] == []
    assert len(result['news']['failures']) == 3
    assert result['ppo']['windows'] == []
    assert result['ppo']['status'] == 'failed'


def test_summary_copies_metrics_not_private_paths_or_raw_predictions(tmp_path):
    news = tmp_path / 'news'
    news.mkdir()
    arm = {'weight': .25, 'metrics': {'net_return': -.01, 'trades': 2},
           'prediction': {'rmse': .02, 'predictions': [1, 2]}, 'equity': [1, 2]}
    for name in export.NEWS_NAMES:
        (news / (name + '.json')).write_text(json.dumps({
            'config': {'symbol': 'ETH/USDT'},
            'period': {'requested': {'start': '2025-01-01', 'end': '2025-01-15'}},
            'calibration': {'selected_weight': .25, 'validation_predictions': [1, 2]},
            'arms': {k: arm for k in export.ARM_NAMES},
        }), encoding='utf-8')
    ppo = tmp_path / 'ppo.json'
    ppo.write_text(json.dumps({'status': 'complete', 'windows': [{'start': '2025-01-01', 'end': '2025-01-15',
        'singles': [{'seed': 7, 'metrics': {'net_return': .01}, 'artifact_dir': 'C:\\secret\\data'}],
        'groups': [{'seeds': [7, 42, 99], 'status': 'succeeded', 'metrics': {'net_return': .02}}]}],
        'failures': []}), encoding='utf-8')
    result = export.build(news, ppo)
    assert result['status'] == 'partial'  # a purported complete PPO run has only one partial window
    assert result['ppo']['status'] == 'partial'
    assert result['news']['windows'][0]['arms']['price_only']['metrics']['net_return'] == -.01
    assert len(result['news']['windows'][0]['source_sha256']) == 64
    assert result['ppo']['windows'][0]['groups'][0]['metrics']['net_return'] == .02
    encoded = json.dumps(result)
    assert 'artifact_dir' not in encoded and 'validation_predictions' not in encoded
    assert 'predictions' not in encoded and 'C:\\secret' not in encoded


def test_failed_group_error_hides_local_path(tmp_path):
    ppo = tmp_path / 'ppo.json'
    ppo.write_text(json.dumps({'status': 'partial', 'windows': [{'groups': [{'status': 'failed',
        'error': r'Cannot load C:\Users\name\private.json'}]}],
        'failures': [{'window': 1, 'error': r'Error C:\Users\name\private.json'}]}), encoding='utf-8')
    result = export.build(tmp_path, ppo)
    assert result['ppo']['status'] == 'partial'
    assert '[本机路径]' in result['ppo']['failures'][0]['error']
    assert 'C:\\Users' not in json.dumps(result, ensure_ascii=False)
