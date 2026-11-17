import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'scripts' / 'export_strategy_v2.py'
spec = spec_from_file_location('export_strategy_v2', path)
export = module_from_spec(spec)
spec.loader.exec_module(export)


def test_missing_reports_are_explicit_and_no_values_invented(tmp_path):
    data = export.build(tmp_path/'news.json', tmp_path/'ppo.json')
    assert data['status'] == 'partial'
    assert data['news']['status'] == data['ppo']['status'] == 'failed'
    assert data['news']['windows'] == data['ppo']['windows'] == []


def test_partial_results_keep_metrics_hide_private_fields_and_paths(tmp_path):
    news=tmp_path/'n.json'; ppo=tmp_path/'p.json'
    news.write_text(json.dumps({'schema':'news-threshold-study-v1', 'status':'complete',
        'windows':[{'start':'2025-01-01','end':'2025-01-15','rows':[{'seed':7,'feature':'price_news',
        'policy':'cost_buffer','status':'succeeded','metrics':{'net_return':-.01,'trades':3},
        'private_path':r'C:\\Users\\user\\test.json','predictions':[1,2]}]}]}),encoding='utf-8')
    ppo.write_text(json.dumps({'schema':'ppo-position-state-v2','status':'partial','windows':[{'runs':[{
        'seed':7,'variant':'price_position','status':'failed','error':r'failed C:\\Users\\user\\model.zip'}]}]}),encoding='utf-8')
    data=export.build(news,ppo)
    assert data['status']=='partial' and data['news']['status']=='partial'
    assert data['news']['windows'][0]['rows'][0]['metrics']['net_return']==-.01
    assert len(data['news']['source_sha256'])==64
    serialized=json.dumps(data,ensure_ascii=False)
    assert 'private_path' not in serialized and 'predictions' not in serialized and r'C:\\Users' not in serialized
    assert '[本机路径]' in data['ppo']['windows'][0]['rows'][0]['error']


def test_invalid_schema_rejected(tmp_path):
    path=tmp_path/'bad.json'
    path.write_text(json.dumps({'schema':'other','status':'complete'}),encoding='utf-8')
    data=export.build(path,path)
    assert data['status']=='partial' and data['news']['status']=='failed'


def test_complete_report_checks_every_predeclared_combination(tmp_path):
    news_windows=[]
    ppo_windows=[]
    for index,start in enumerate(('2025-01-01','2025-01-15','2025-04-01','2025-06-01')):
        news_windows.append({'start':start,'rows':[{'seed':seed,'feature':feature,'policy':policy,
            'status':'succeeded','metrics':{'net_return':0.0}}
            for seed in (7,42,99) for feature in ('price_only','price_news') for policy in ('baseline','cost_buffer')]})
    for start in ('2025-01-01','2025-01-15','2025-04-01','2025-07-01'):
        ppo_windows.append({'start':start,'runs':[{'seed':seed,'variant':variant,'status':'succeeded',
            'metrics':{'net_return':0.0}} for seed in (7,42,99,17,52) for variant in ('price_only','price_position')]})
    n=tmp_path/'n.json'; p=tmp_path/'p.json'
    n.write_text(json.dumps({'schema':'news-threshold-study-v1','status':'complete','windows':news_windows}),encoding='utf-8')
    p.write_text(json.dumps({'schema':'ppo-position-state-v2','status':'complete','windows':ppo_windows}),encoding='utf-8')
    assert export.build(n,p)['status']=='complete'
    ppo_windows[0]['runs'][0]=ppo_windows[0]['runs'][1].copy()
    p.write_text(json.dumps({'schema':'ppo-position-state-v2','status':'complete','windows':ppo_windows}),encoding='utf-8')
    data=export.build(n,p)
    assert data['status']=='partial' and data['ppo']['status']=='partial'
    assert '不完整' in data['ppo']['failures'][-1]['error']


def test_supplement_is_separate_and_does_not_cover_preregistered_404(tmp_path):
    main=tmp_path/'n.json'; ppo=tmp_path/'p.json'; supplement=tmp_path/'supp.json'
    main.write_text(json.dumps({'schema':'news-threshold-study-v1','status':'partial','windows':[{
        'start':'2025-06-01','end':'2025-06-15','status':'failed','error':'HTTP Error 404','rows':[]}],
        'failures':[{'window':4,'error':'HTTP Error 404'}]}),encoding='utf-8')
    ppo.write_text(json.dumps({'schema':'ppo-position-state-v2','status':'partial','windows':[]}),encoding='utf-8')
    rows=[{'seed':seed,'feature':feature,'policy':policy,'status':'succeeded','metrics':{'net_return':0.0}}
          for seed in (7,42,99) for feature in ('price_only','price_news') for policy in ('baseline','cost_buffer')]
    supplement.write_text(json.dumps({'schema':'news-threshold-study-v1','status':'complete','windows':[{
        'start':'2025-05-31','end':'2025-06-14','status':'complete','rows':rows}]}),encoding='utf-8')
    data=export.build(main,ppo,supplement)
    assert data['status']=='partial' and data['news']['status']=='partial'
    assert data['news']['windows'][0]['error']=='HTTP Error 404'
    assert data['news']['failures'][0]['error']=='HTTP Error 404'
    assert data['news_supplement']['status']=='complete'
    assert data['news_supplement']['label']=='缺失后补充、非预注册'
    assert len(data['news_supplement']['windows'][0]['rows'])==12
    rows.pop()
    supplement.write_text(json.dumps({'schema':'news-threshold-study-v1','status':'complete','windows':[{
        'start':'2025-05-31','end':'2025-06-14','status':'complete','rows':rows}]}),encoding='utf-8')
    assert export.build(main,ppo,supplement)['news_supplement']['status']=='partial'


def test_missing_supplement_is_visible(tmp_path):
    data=export.build(tmp_path/'main',tmp_path/'ppo',tmp_path/'missing-supp')
    assert data['news_supplement']['status']=='failed' and data['news_supplement']['error']


def test_independent_404_evidence_is_read_only_and_missing_does_not_fabricate(tmp_path):
    diagnosis=tmp_path/'diagnosis.json'
    item={'checked_at':'2026-11-17T04:33:53+00:00','method':'HEAD','http_status':404,
          'batch':'20250614180000','url':'https://data.gdeltproject.org/gdeltv2/20250614180000.gkg.csv.zip',
          'internal_path':r'C:\\Users\\someone\\secret'}
    diagnosis.write_text(json.dumps(item),encoding='utf-8')
    result=export.build(tmp_path/'news',tmp_path/'ppo',tmp_path/'supp',diagnosis)
    assert result['news_diagnosis']['http_status']==404 and len(result['news_diagnosis']['source_sha256'])==64
    assert 'internal_path' not in json.dumps(result)
    item['http_status']=200
    diagnosis.write_text(json.dumps(item),encoding='utf-8')
    assert export.build(tmp_path/'news',tmp_path/'ppo',tmp_path/'supp',diagnosis)['news_diagnosis'] is None
    assert export.build(tmp_path/'news',tmp_path/'ppo',tmp_path/'supp',tmp_path/'missing')['news_diagnosis'] is None


def test_innovation_rows_do_not_invent_completeness(tmp_path):
    source=tmp_path/'innovation.json'
    source.write_text(json.dumps({'schema':'news-innovation-study-v1','status':'complete','windows':[{'start':'2025-05-31','supplemental_after_missing_archive':True,'rows':[{'seed':7,'arm':'news_innovation','status':'succeeded','metrics':{'net_return':.01},'private_path':'hidden'}]}]}),encoding='utf-8')
    result=export.prepare_innovation(source)
    assert result['status']=='partial'
    assert result['windows'][0]['supplemental_after_missing_archive'] is True
    assert result['windows'][0]['rows'][0]['metrics']['net_return']==.01
    assert 'private_path' not in json.dumps(result)
    assert export.prepare_innovation(tmp_path/'absent')['status']=='failed'
