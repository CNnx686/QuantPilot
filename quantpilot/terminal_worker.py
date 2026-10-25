"""Training supervision and isolated local paper accounts using public market prices."""
import json
import math
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psutil

from . import settings, terminal_store as store
from .deployments import child_environment, owned, stop_owned
from .public_worker import terminate_owned

_stop = threading.Event()
_threads = []
MAX_INSTANCES = 4


class NewsInputError(ValueError):
    """Stable dependency failure code with a separately retained diagnostic."""
    def __init__(self, detail):
        self.detail = str(detail)[:1000] or type(detail).__name__
        super().__init__('news_input_error: ' + self.detail)


def model_directory(candidate):
    path = Path(candidate['artifact_dir']).resolve()
    root = store.job_directory(candidate['job_id']).resolve()
    if not path.is_relative_to(root) or path.is_symlink():
        raise ValueError('Model path is outside its frozen job directory')
    return path


def verify_candidate(candidate):
    if candidate['config'].get('news'):
        from .terminal_news_engine import verify_model
    else:
        from .terminal_engine import verify_model
    if candidate['config_sha256'] != store.digest(candidate['config']):
        raise ValueError('Frozen candidate configuration is inconsistent')
    manifest = verify_model(model_directory(candidate))
    if manifest.get('id') != candidate['id'] or manifest.get('config') != candidate['config']:
        raise ValueError('Model package differs from the selected candidate')
    if manifest != candidate.get('model_manifest'):
        raise ValueError('Model manifest differs from the immutable candidate record')
    return manifest


def market_snapshot(symbol, refresh=False):
    from .terminal_market import snapshot
    return snapshot(symbol, refresh=refresh)


def valid_market(market):
    if market.get('stale') or market.get('error'):
        raise ValueError(market.get('error') or 'Public market data is stale')
    ticker = market.get('ticker') or {}
    timestamp = ticker.get('timestamp')
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp() * 1000
    if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        raise ValueError('Public quote has no verifiable timestamp')
    age = time.time() * 1000 - timestamp
    if age < -60000 or age > 300000:
        raise ValueError('Public quote is stale; no simulated fill is allowed')
    if not market.get('candles'):
        raise ValueError('Closed hourly candles are unavailable')
    return market


def price(market, side):
    ticker = market.get('ticker') or {}
    value = ticker.get('ask' if side == 'buy' else 'bid') or ticker.get('last')
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError('Public quote price is unavailable; no simulated fill is allowed')
    return float(value)


def prediction_for(candidate, market):
    from .terminal_market import closed_frame
    verify_candidate(candidate)
    if candidate['config'].get('news'):
        from .terminal_news import current_snapshot
        from .terminal_news_engine import predict
        frame = closed_frame(market)
        try:
            snapshot = current_snapshot(refresh=False)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise NewsInputError(exc) from exc
        try:
            return predict(model_directory(candidate), frame, snapshot)
        except (ValueError, TypeError, KeyError) as exc:
            # Distinguish damaged/incompatible frozen model files from malformed current
            # inputs. Re-verification also closes the change-between-check-and-predict race.
            verify_candidate(candidate)
            raise NewsInputError(exc) from exc
    from .terminal_engine import predict
    return predict(model_directory(candidate), closed_frame(market))


def status(instance):
    if instance['stop_requested'] and instance['position'] is None:
        instance.update(status='stopped', entry_enabled=False)
    elif instance.get('error'):
        instance.update(status='blocked', entry_enabled=False)
    elif instance['stop_requested']:
        instance.update(status='waiting_stop', entry_enabled=False)
    elif instance['user_paused']:
        instance.update(status='paused', entry_enabled=False)
    else:
        instance.update(status='running', entry_enabled=True)


def mark(instance, market):
    ticker = market.get('ticker') or {}
    last = ticker.get('last')
    instance['market'] = market
    if isinstance(last, (int, float)) and math.isfinite(last) and last > 0:
        units = (instance.get('position') or {}).get('quantity', 0)
        instance['equity'] = instance['cash'] + units * last
    instance['updated_at'] = store.now()


def fill(instance, side, market, reason, decision_id=None):
    fill_price = price(market, side)
    fee = instance['config']['fee']
    timestamp = store.now()
    if side == 'buy':
        if instance['position'] is not None:
            return 'already_in_position'
        gross = instance['config']['stake']
        cost = gross * (1 + fee)
        if instance['cash'] + 1e-9 < cost:
            return 'insufficient_virtual_cash_including_fee'
        units = gross / fill_price
        instance['cash'] -= cost
        instance['position'] = {'quantity': units, 'entry_price': fill_price, 'entry_at': timestamp,
            'entry_candle_at': instance.get('last_decision_at'), 'cost': cost,
            'stop_price': fill_price * (1 - instance['config']['parameters']['stop_loss'])}
        realized = None
    else:
        position = instance['position']
        if position is None:
            return 'no_position'
        units = position['quantity']
        gross = units * fill_price
        received = gross * (1 - fee)
        realized = received - position['cost']
        instance['cash'] += received
        instance['realized_pnl'] += realized
        instance['position'] = None
    event = {'id': store.identifier('fill'), 'created_at': timestamp, 'side': side,
             'symbol': instance['symbol'], 'quantity': units, 'price': fill_price,
             'gross': gross, 'fee': gross * fee, 'realized_pnl': realized,
             'reason': reason, 'decision_id': decision_id, 'candidate_id': instance['candidate_id'],
             'quote_timestamp': market['ticker']['timestamp'], 'quote_observed_at': market.get('observed_at'),
             'fill_assumption': 'Observed public ask/bid, or last when side quote absent; no order book impact simulation',
             'mode': 'paper', 'status': 'filled'}
    instance['orders'].append(event)
    instance['trades'].append(event)
    mark(instance, market)
    return 'filled'


def create_instance(strategy_id, candidate_id, token):
    request = {'strategy_id': strategy_id, 'candidate_id': candidate_id}
    with store.connect() as conn:
        old = conn.execute('SELECT request,record FROM instances WHERE token=?', (token,)).fetchone()
        if old:
            if old['request'] != store.dumps(request):
                raise ValueError('Idempotency-Key belongs to another request')
            return json.loads(old['record']), False
    strategy = store.get('strategies', strategy_id)
    candidate = store.get('candidates', candidate_id)
    source_hash = candidate.get('source_config_sha256', candidate['config_sha256'])
    if candidate['strategy_id'] != strategy_id or source_hash != store.digest(strategy['config']):
        raise ValueError('Candidate does not belong to this saved strategy configuration')
    if strategy['selected_candidate_id'] != candidate_id:
        raise ValueError('Select this candidate before starting its simulated instance')
    verify_candidate(candidate)
    market = valid_market(market_snapshot(candidate['symbol']))
    predicted = prediction_for(candidate, market)
    news_wait = candidate['config'].get('news') and str(predicted.get('reason', '')).startswith('news_')
    if not predicted.get('valid') and not news_wait:
        raise ValueError(predicted.get('reason') or 'Current model input is invalid')
    config = candidate['config']
    item = {'id': store.identifier('instance'), **request, 'name': config['name'], 'kind': config['kind'],
            'symbol': config['symbol'], 'config': config, 'mode': 'paper', 'execution_engine': 'local_paper_v1',
            'status': 'starting', 'entry_enabled': False, 'user_paused': False, 'stop_requested': False,
            'created_at': store.now(), 'updated_at': store.now(),
            'error': predicted.get('reason') if not predicted.get('valid') else None,
            'last_decision_at': None, 'cash': config['wallet'], 'equity': config['wallet'],
            'initial_balance': config['wallet'], 'realized_pnl': 0.0, 'position': None,
            'orders': [], 'trades': [], 'decisions': [], 'market': market,
            'news': predicted.get('news'), 'news_blocked': bool(news_wait and not predicted.get('valid')),
            'news_has_run': bool(config.get('news') and predicted.get('valid')),
            'news_resume_required': False}
    with store.connect() as conn:
        # Recheck all mutable bindings after current-data/model preparation.
        strategy = store.read(conn, 'strategies', strategy_id)
        if strategy['selected_candidate_id'] != candidate_id or store.digest(strategy['config']) != source_hash:
            raise ValueError('Strategy changed during startup; refresh before starting')
        old = conn.execute('SELECT request,record FROM instances WHERE token=?', (token,)).fetchone()
        if old:
            if old['request'] != store.dumps(request):
                raise ValueError('Idempotency-Key belongs to another request')
            return json.loads(old['record']), False
        active = [record for record in store.rows(conn, 'instances') if record['status'] != 'stopped']
        if any(record['strategy_id'] == strategy_id for record in active):
            raise ValueError('This strategy already has an active instance; stop it before starting another')
        if len(active) >= MAX_INSTANCES:
            raise ValueError('Four simulated instances are already active')
        status(item)
        conn.execute('INSERT INTO instances VALUES(?,?,?,?)', (item['id'], token, store.dumps(request), store.dumps(item)))
    return tick(item['id'], market=market, predicted=predicted), True


def tick(identifier, *, market=None, predicted=None):
    initial = store.get('instances', identifier)
    if initial['status'] == 'stopped':
        return initial
    candidate = store.get('candidates', initial['candidate_id'])
    failure = None
    failure_detail = None
    try:
        market = valid_market(market or market_snapshot(initial['symbol']))
        latest = market.get('last_closed_at')
        if (predicted is None and initial['last_decision_at'] and latest
                and latest <= initial['last_decision_at'] and not initial.get('error')
                and not candidate['config'].get('news')):
            # Refresh quote/equity without repeatedly loading PPO on an already consumed bar.
            predicted = {'valid': True, 'action': 'hold', 'decision_at': initial['last_decision_at']}
        else:
            predicted = predicted or prediction_for(candidate, market)
        if not predicted.get('valid'):
            failure = predicted.get('reason') or 'Model input is invalid'
    except NewsInputError as exc:
        failure, failure_detail = 'news_input_error', exc.detail
    except Exception as exc:
        failure = str(exc)[:1000] or type(exc).__name__
    with store.connect() as conn:
        item = store.read(conn, 'instances', identifier)
        if item['status'] == 'stopped':
            return item
        if predicted and predicted.get('news') is not None:
            item['news'] = predicted['news']
        item['news_blocked'] = bool(item['config'].get('news') and failure and str(failure).startswith('news_'))
        if item['news_blocked'] and item.get('news_has_run'):
            item['news_resume_required'] = True
        if item['news_blocked']:
            item['news_last_error'] = {'code': failure, 'detail': failure_detail,
                                       'observed_at': store.now(), 'cutoff': (market or {}).get('last_closed_at')}
        if item['config'].get('news') and predicted and predicted.get('valid') and not failure:
            item['news_has_run'] = True
        item.update(error=failure or ('news_resume_required' if item.get('news_resume_required') else None),
                    error_detail=(failure_detail or item.get('error_detail')) if item.get('news_resume_required') else failure_detail,
                    updated_at=store.now())
        if market:
            mark(item, market)
        status(item)
        # Failed input cannot create a model signal. Close-based existing-position risk is
        # still evaluated when fresh market prices exist and model inference alone failed.
        market_ok = False
        try:
            valid_market(market or {})
            market_ok = True
        except (ValueError, TypeError):
            pass
        candles = (market or {}).get('candles') or []
        closed = candles[-1] if candles else None
        decision_at = predicted.get('decision_at') if predicted else (closed or {}).get('closed_at')
        if decision_at and (item['last_decision_at'] is None or decision_at > item['last_decision_at']) and market_ok:
            raw = predicted.get('action', 'hold') if predicted and not failure else 'hold'
            action, reason = raw, failure or (predicted or {}).get('reason') or 'model_signal'
            position = item['position']
            stop_hit = bool(position and closed and closed['close'] <= position['stop_price']
                            and decision_at > (position.get('entry_candle_at') or ''))
            if stop_hit:
                action, reason = 'sell', 'closed_candle_stop_loss'
            elif action == 'buy' and not item['entry_enabled']:
                action, reason = 'hold', ('news_resume_required' if item.get('news_resume_required') else 'new_entries_paused')
            decision = {'id': store.identifier('decision'), 'created_at': store.now(),
                'candle_at': (closed or {}).get('date'), 'decision_at': decision_at,
                'feature_available_at': (predicted or {}).get('feature_available_at'),
                'model_id': item['candidate_id'], 'raw_action': raw, 'action': action,
                'prediction': (predicted or {}).get('prediction'), 'reason': reason,
                'error_detail': failure_detail,
                'news': (predicted or {}).get('news'), 'valid': not bool(failure),
                'execution': 'blocked_input' if failure and not stop_hit else 'no_order'}
            consumed = not failure or stop_hit
            decision['consumed'] = consumed
            if consumed:
                item['last_decision_at'] = decision_at
                item.pop('last_blocked_key', None)
                if action in {'buy', 'sell'}:
                    decision['execution'] = fill(item, action, market, reason, decision['id'])
                item['decisions'].append(decision)
            else:
                blocked_key = decision_at + '|' + str(failure)
                if item.get('last_blocked_key') != blocked_key:
                    item['decisions'].append(decision)
                    item['last_blocked_key'] = blocked_key
        status(item)
        store.save(conn, 'instances', item)
        return item


def action(identifier, action_name, mode, token):
    payload = {'action': action_name, 'mode': mode}
    operation_name = 'instance:' + identifier
    with store.connect() as conn:
        previous = store.operation(conn, token, operation_name, payload)
        if previous is not None:
            return previous
    if action_name == 'tick':
        result = tick(identifier)
        with store.connect() as conn:
            previous = store.operation(conn, token, operation_name, payload)
            if previous is not None:
                return previous
            store.complete_operation(conn, token, operation_name, payload, result)
        return result
    market = None
    initial = store.get('instances', identifier)
    if action_name in {'resume', 'close_position'} or (action_name == 'stop' and mode == 'close'):
        market = valid_market(market_snapshot(initial['symbol']))
        if action_name == 'resume':
            candidate = store.get('candidates', initial['candidate_id'])
            verify_candidate(candidate)
            predicted = prediction_for(candidate, market)
            if not predicted.get('valid'):
                raise ValueError(predicted.get('reason') or 'Current model input is invalid')
    with store.connect() as conn:
        previous = store.operation(conn, token, operation_name, payload)
        if previous is not None:
            return previous
        item = store.read(conn, 'instances', identifier)
        if item['status'] == 'stopped' and action_name != 'stop':
            raise ValueError('Stopped instances are historical; start a new instance with a new virtual wallet')
        if action_name == 'pause':
            item['user_paused'] = True
        elif action_name == 'resume':
            if item['stop_requested']:
                raise ValueError('Instance is waiting to stop; it cannot resume new entries')
            item.update(user_paused=False, error=None, error_detail=None, news_resume_required=False, news_blocked=False)
            if item['config'].get('news'):
                item.update(news=predicted.get('news'), news_has_run=True)
        elif action_name == 'close_position':
            if item['position']:
                fill(item, 'sell', market, 'operator_close_position')
        elif action_name == 'stop':
            item.update(stop_requested=True, user_paused=True)
            if mode == 'close' and item['position']:
                fill(item, 'sell', market, 'operator_close_and_stop')
        else:
            raise ValueError('Unsupported instance action')
        if market:
            mark(item, market)
        item['updated_at'] = store.now()
        status(item)
        store.save(conn, 'instances', item)
        store.complete_operation(conn, token, operation_name, payload, item)
        return item


def run_training():
    job = store.claim()
    if job is None:
        return False
    directory = store.job_directory(job['id'])
    directory.mkdir(parents=True, exist_ok=True)
    process = None
    try:
        environment = child_environment() | {'QUANTPILOT_HOME': str(settings.HOME)}
        command = [sys.executable, '-m', 'quantpilot.terminal_job', job['id']]
        with (directory / 'worker.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, cwd=settings.ROOT, env=environment,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=os.name != 'nt', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            identity = psutil.Process(process.pid)
            store.progress(job['id'], 'preparing_data', {'pid': process.pid, 'created': identity.create_time(),
                'exe': identity.exe(), 'cmdline': identity.cmdline()})
            deadline = time.monotonic() + 1800
            while process.poll() is None:
                if _stop.is_set() or (directory / 'CANCEL').exists() or time.monotonic() > deadline:
                    terminate_owned(process)
                    store.finish(job['id'], error='Task cancelled, application stopped, or 30-minute training deadline exceeded')
                    return True
                _stop.wait(.25)
            comparison = None
            if (directory / 'comparison.json').is_file():
                comparison = json.loads((directory / 'comparison.json').read_text(encoding='utf-8'))
            if process.returncode != 0 or not any((directory / name).is_file() for name in ('candidate.json', 'completion.json')):
                error = 'Training process failed; inspect the task log'
                if (directory / 'error.json').is_file():
                    error = json.loads((directory / 'error.json').read_text(encoding='utf-8'))['error']
                store.finish(job['id'], error=error, comparison=comparison)
            else:
                if (directory / 'completion.json').is_file():
                    complete = json.loads((directory / 'completion.json').read_text(encoding='utf-8'))
                    store.finish(job['id'], candidates=complete['candidates'], comparison=complete['comparison'])
                else:
                    candidate = json.loads((directory / 'candidate.json').read_text(encoding='utf-8'))
                    store.finish(job['id'], candidate=candidate)
    except Exception as exc:
        if process and process.poll() is None:
            terminate_owned(process)
        store.finish(job['id'], error=str(exc)[:2000] or type(exc).__name__)
    return True


def recover_training():
    for job in store.list_records('jobs'):
        if job['status'] not in {'running', 'cancelling'}:
            continue
        identity = job.get('process')
        if identity:
            stop_owned({'process': identity})
            if owned({'process': identity}):
                continue
        else:
            # Recover the narrow launch-before-identity crash window by exact typed command.
            expected = [sys.executable, '-m', 'quantpilot.terminal_job', job['id']]
            for process in psutil.process_iter(['pid', 'cmdline', 'exe', 'create_time']):
                if process.info['cmdline'] == expected:
                    stop_owned({'process': {'pid': process.pid, 'created': process.info['create_time'],
                        'exe': process.info['exe'], 'cmdline': process.info['cmdline']}})
        store.finish(job['id'], error='Training interrupted by service restart; partial artifacts retained; retry creates a new task')


@contextmanager
def worker_lock(name):
    path = settings.HOME / 'runtime' / ('terminal-' + name + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def training_loop():
    with worker_lock('training') as acquired:
        if not acquired:
            return
        recover_training()
        while not _stop.is_set():
            try:
                worked = run_training()
            except Exception:
                worked = False
            _stop.wait(.2 if worked else 1)


def paper_loop():
    with worker_lock('paper') as acquired:
        if not acquired:
            return
        while not _stop.is_set():
            for item in store.list_records('instances'):
                if _stop.is_set():
                    break
                if item['status'] != 'stopped':
                    try:
                        tick(item['id'])
                    except Exception as exc:
                        with store.connect() as conn:
                            current = store.read(conn, 'instances', item['id'])
                            current.update(error=str(exc)[:1000] or type(exc).__name__, updated_at=store.now())
                            status(current)
                            store.save(conn, 'instances', current)
            _stop.wait(30)


def news_loop():
    """Capture availability at actual receipt time independently of model inference."""
    with worker_lock('news') as acquired:
        if not acquired:
            return
        while not _stop.is_set():
            try:
                from .terminal_news import current_snapshot
                current_snapshot(refresh=True)
            except Exception:
                # The news adapter persists availability/error; do not invent zero events.
                pass
            _stop.wait(60)


def start():
    global _threads
    if any(thread.is_alive() for thread in _threads):
        return
    _stop.clear()
    _threads = [threading.Thread(target=target, daemon=True, name=name)
                for target, name in ((training_loop, 'terminal-training'), (paper_loop, 'terminal-paper'),
                                     (news_loop, 'terminal-news'))]
    for thread in _threads:
        thread.start()


def stop():
    _stop.set()
    for thread in _threads:
        thread.join(timeout=12)
