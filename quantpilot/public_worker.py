"""Single persisted execution slot, subprocess deadline, no networking in GET routes."""
import os
import subprocess
import sys
import threading
import signal
from . import settings, public_store

_thread = None
_stop = threading.Event()


def execute(job):
    if job['kind'] == 'exchange':
        from .exchange_data import refresh
        return refresh(job['source'])
    if job['kind'] == 'news':
        from .news_data import refresh
        return refresh(job['source'])
    if job['kind'] == 'analysis':
        from .news_analysis import analyze
        return analyze(job['source'], job['payload']['provider'])
    raise ValueError('unknown_job_kind')


def child_environment(job):
    allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'USERPROFILE', 'HOME', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
        'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env['QUANTPILOT_HOME'] = str(settings.HOME)
    if job['kind'] == 'analysis' and job['payload'].get('provider') == 'deepseek':
        from .llm import NAMES
        env.update({key: os.environ[key] for key in NAMES if key in os.environ})
    return env


def run_one():
    job = public_store.claim()
    if not job:
        return False
    env = child_environment(job)
    try:
        process = subprocess.Popen([sys.executable, '-m', 'quantpilot.public_worker', job['id']],
            cwd=settings.ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            returncode = process.wait(timeout=75)
        except subprocess.TimeoutExpired:
            terminate_owned(process)
            raise
        if returncode:
            raise RuntimeError('worker_failed')
        current = public_store.get_job(job['id'])
        if current['status'] == 'running':
            public_store.finish(job['id'], error='worker_returned_without_result')
    except Exception:
        key = job['kind'] + ':' + job['source'] if job['kind'] in {'exchange', 'news'} else None
        public_store.finish(job['id'], error='worker_failed_or_deadline_exceeded',cache_key=key)
    return True


def terminate_owned(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


def loop():
    while not _stop.is_set():
        try:
            worked = run_one()
        except Exception:
            worked = False
        _stop.wait(0.2 if worked else 2)


def start():
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=loop, name='public-data-worker', daemon=True)
        _thread.start()


def stop():
    _stop.set()


if __name__ == '__main__':
    # Independent watchdog survives scheduler/API death (including Windows venv launcher).
    watchdog = threading.Timer(70, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    identifier = sys.argv[1]
    job = public_store.get_job(identifier)
    if not job or job['status'] != 'running':
        sys.exit(2)
    public_store.WORKER_JOB.set(identifier)
    try:
        result = execute(job)
        public_store.finish(identifier, result=result)
    except Exception:
        public_store.finish(identifier, error='public_data_or_analysis_failed')
