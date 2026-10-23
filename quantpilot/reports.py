"""Evidence-validated, deterministic historical reports; never infer decisions."""
import json
from contextlib import closing
from datetime import datetime, timezone
from html import escape

from . import replay, settings
from .data import sha256
from .doctor import read_connection


def linked(table, job_id, keys):
    with closing(read_connection(settings.DB)) as conn:
        records = [json.loads(row[0]) for row in conn.execute(f'SELECT record FROM {table} ORDER BY rowid')]
    return [{k: record.get(k) for k in keys} for record in records if record.get('experiment_id') == job_id]


def build(job):
    verified = replay.build(job)
    result = job['result']
    research = linked('research', job['id'], ('id', 'status', 'created_at', 'experiment_id', 'revision', 'source', 'news_evidence'))
    releases = linked('releases', job['id'], ('id', 'status', 'created_at', 'experiment_id', 'checks', 'policy'))
    limits = [*verified['limitations'], 'Untouched final holdout remains unverified; historical results do not establish future profitability',
              'Saved release decisions are historical records, not a fresh release audit or live deployment verification',
              'Null metrics are undefined or unavailable, never zero; no risk or causal decisions are reconstructed']
    metrics = result['metrics']
    narrative = [f"历史实验 {job['id']}；策略 {result['strategy']}；区间 [{result['period']['start']}, {result['period']['end']})，UTC。",
                 f"已核对原始结果与引擎 ZIP，记录成交 {len(result['trades'])} 笔；净损益 {metrics['profit_abs']} USDT。",
                 f"原始结果绑定诊断状态：{verified['diagnostic_status']}；关联研究 {len(research)} 项，保存的发布审核 {len(releases)} 项。",
                 '本报告仅复述历史证据，不推断风险决策、模型信心或获利原因；最终保留测试仍未验证。']
    if job['config']['strategy'] == 'FreqaiTrend':
        detail = '前视检查仅抽样通过，不证明普遍无偏差' if verified['diagnostic_status'] == 'passed_sampled' else '前视检查未通过或未完成，须继续核查'
        narrative.append(f'ML 仅限历史研究（historical-only）；{detail}；模型尚未实现在线执行，禁止用于模拟部署。')
    if any(item.get('news_evidence') for item in research):
        context_limit = '关联新闻仅用于今天提出研究假设；其可知时间晚于历史回测区间，未作为历史特征，也不能解释该实验收益。'
        narrative.append(context_limit)
        limits.append(context_limit)
    value = dict(schema_version='1', generated_at=datetime.now(timezone.utc).isoformat(),
                 experiment={k: job[k] for k in ('id', 'name', 'config', 'created_at')}, metrics=metrics,
                 research=research, releases=releases, diagnostic_status=verified['diagnostic_status'],
                 diagnostics=verified['diagnostics'], provenance={**verified['provenance'],
                 'result_read_sha256': sha256(settings.ARTIFACTS / job['id'] / 'result.json'),
                 'assumptions': result.get('assumptions', {}), 'period': result['period']},
                 limitations=limits, trades=[{**trade, 'id': f'trade:{index}', 'original_index': index}
                                             for index, trade in enumerate(result['trades'])], narrative=narrative)
    json.dumps(value, allow_nan=False)
    return value


def html(report):
    def show(value):
        return escape(json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (dict, list)) else
                      'null（未定义/不可用）' if value is None else str(value), quote=True)
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">',
             '<title>QuantPilot 历史实验报告</title><style>body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.5}table{border-collapse:collapse;width:100%;font-size:11px}td,th{border:1px solid #999;padding:5px;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere}@media print{body{margin:0}thead{display:table-header-group}tr{break-inside:avoid}}</style><body>',
             '<h1>QuantPilot 历史实验报告</h1><p>使用浏览器打印可保存 PDF。UTC 时间保留原记录。</p>']
    for title, value in [('实验', report['experiment']), ('生成时间', report['generated_at']), ('事实摘要', report['narrative']),
                         ('指标', report['metrics']), ('关联研究', report['research']), ('保存的发布审核', report['releases']),
                         ('诊断状态', report['diagnostic_status']), ('诊断证据', report['diagnostics']),
                         ('证据来源、指标定义与假设', report['provenance']), ('局限', report['limitations'])]:
        parts.append(f'<h2>{title}</h2><pre>{show(value)}</pre>')
    parts.append('<h2>完整成交记录</h2><table><thead><tr>')
    columns = ['id', 'original_index', 'pair', 'open_date', 'close_date', 'open_rate', 'close_rate', 'profit_abs',
               'profit_ratio', 'stake_amount', 'amount', 'fee_open', 'fee_close', 'enter_tag', 'exit_reason']
    parts.extend(f'<th>{key}</th>' for key in columns)
    parts.append('</tr></thead><tbody>')
    for trade in report['trades']:
        parts.append('<tr>' + ''.join(f'<td>{show(trade.get(key))}</td>' for key in columns) + '</tr>')
    parts.append('</tbody></table></body></html>')
    return ''.join(parts)
