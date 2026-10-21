"""Immutable historical artifacts to read-only chronological trade events."""
import csv
import json
import math
from datetime import datetime, timezone

from . import settings
from .data import sha256
from .engine import parse_result
from .releases import safe_file


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def diagnostic_evidence(job, result_hash):
    """Expose original reported status only when bound to this exact result file."""
    diagnostics = []
    for path in sorted(settings.ARTIFACTS.glob('diagnostic-*/diagnostic.json')):
        try:
            source = safe_file(settings.ARTIFACTS, path.relative_to(settings.ARTIFACTS))
            report = json.loads(source.read_text(encoding='utf-8'))
            if not isinstance(report, dict) or not (report.get('job_id') == job['id']
                    and report.get('strategy') == job['config']['strategy']
                    and report.get('source_result_sha256') == result_hash):
                continue
            diagnostics.append({'id': path.parent.name, 'report_read_sha256': sha256(source),
                                **{key: report.get(key) for key in ('job_id', 'strategy', 'source_result_sha256',
                                                                  'status', 'scope', 'rows', 'exit_code')}})
        except (OSError, ValueError):
            continue
    statuses = {report['status'] for report in diagnostics if isinstance(report['status'], str)}
    status = 'flagged' if 'flagged' in statuses else 'passed_sampled' if statuses == {'passed_sampled'} else 'unverified'
    return diagnostics, status


def build(job):
    if job['status'] != 'succeeded' or not job.get('result'):
        raise ValueError('Replay requires a completed successful experiment')
    root = settings.ARTIFACTS / job['id']
    result = json.loads(safe_file(root, 'result.json').read_text(encoding='utf-8'))
    if result != job['result']:
        raise ValueError('Saved result differs from experiment')
    provenance = json.loads(safe_file(root, 'provenance.json').read_text(encoding='utf-8'))
    archive = safe_file(root, result['provenance']['raw_artifact'])
    if sha256(archive) != result['provenance']['raw_sha256']:
        raise ValueError('Historical engine archive hash mismatch')
    raw = parse_result(archive, job['config'], provenance)
    for key in ('metrics', 'trades', 'strategy', 'period'):
        if raw[key] != result[key]:
            raise ValueError('Historical engine archive does not reconcile')
    predictions = {}
    replay_provenance = dict(result['provenance'])
    limitations = ['Realized closed-trade balance, not mark-to-market equity',
                   'Null decision evidence means no exact recorded pair/time match',
                   'Risk and order decisions are not reconstructed; replay never controls an engine']
    diagnostics, diagnostic_status = diagnostic_evidence(job, sha256(root / 'result.json'))
    limitations.append('Diagnostic statuses are original result-bound reports, not a new audit or deployment approval')
    if diagnostic_status == 'flagged':
        limitations.append('Unresolved lookahead diagnostic: flagged. Historical replay does not clear this warning')
    if job['config']['strategy'] == 'FreqaiTrend':
        limitations.append('ML remains historical-only and is blocked from simulation deployment; lookahead concerns are unresolved')
    if result.get('ml'):
        path = safe_file(root, result['ml']['artifacts']['predictions'])
        replay_provenance['prediction_csv_read_sha256'] = sha256(path)
        limitations.append('Original experiment did not freeze a prediction CSV hash; current file hash is read-time provenance, not original immutability proof')
        with path.open(encoding='utf-8', newline='') as stream:
            for row in csv.DictReader(stream):
                valid = int(row['do_predict'])
                if valid not in {-2, -1, 0, 1, 2} or (valid == 1 and not math.isfinite(float(row['prediction']))):
                    raise ValueError('Invalid recorded prediction validity or value')
                if not (timestamp(row['model_label_max_at']) <= timestamp(row['model_fit_at'])
                        < timestamp(row['decision_at']) == timestamp(row['feature_available_at'])):
                    raise ValueError('Invalid recorded prediction timing')
                key = row['pair'], timestamp(row['decision_at'])
                if key in predictions:
                    raise ValueError('Duplicate prediction decision evidence')
                predictions[key] = row
    events = []
    for index, trade in enumerate(result['trades']):
        for kind, field in [('trade_open', 'open_date'), ('trade_close', 'close_date')]:
            event_time = timestamp(trade[field])
            events.append({'id': f'{index}:{kind}', 'time': event_time.isoformat(), 'type': kind,
                           'pair': trade['pair'], 'trade': trade, 'risk_state': None,
                           'risk_evidence': 'not_recorded', 'decision_evidence':
                           predictions.get((trade['pair'], event_time)) if kind == 'trade_open' else None})
    events.sort(key=lambda event: (timestamp(event['time']), event['type'] != 'trade_close', event['id']))
    balance = result['metrics']['initial_balance']
    for event in events:
        if event['type'] == 'trade_close':
            balance += event['trade']['profit_abs']
        event['realized_balance'] = balance
    return {'experiment_id': job['id'], 'mode': 'historical_replay', 'strategy': result['strategy'],
            'period': result['period'], 'initial_balance': result['metrics']['initial_balance'],
            'final_balance': balance, 'events': events, 'provenance': replay_provenance,
            'diagnostics': diagnostics, 'diagnostic_status': diagnostic_status,
            'limitations': limitations}
