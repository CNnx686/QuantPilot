"""Single OS-locked scheduler with cancellation, timeout and restart recovery."""
import os
import subprocess
import sys
import threading
import time
from . import settings, rl_store
from .rl_config import PROTOCOL, child_environment
from .public_worker import terminate_owned

_thread = None
_stop = threading.Event()


def run_one():
    job = rl_store.claim()
    if job is None:
        return False
    directory = rl_store.directory(job['id'])
    directory.mkdir(parents=True, exist_ok=True)
    env = child_environment()
    env.update({key: value for key, value in os.environ.items()
                if key.upper() in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}})
    process = None
    try:
        with (directory / 'worker.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, '-m', 'quantpilot.rl_runner', job['id']],
                cwd=settings.ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=os.name != 'nt', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            from .rl_process import identity
            rl_store.set_process(job['id'], identity(process.pid))
            deadline = time.monotonic() + PROTOCOL['max_seconds']
            while process.poll() is None:
                if _stop.is_set() or (directory / 'CANCEL').exists():
                    (directory / 'CANCEL').touch()
                    terminate_owned(process)
                    rl_store.finish(job['id'], 'cancelled', 'Cancelled or application shutdown')
                    return True
                if time.monotonic() > deadline:
                    (directory / 'CANCEL').touch()
                    terminate_owned(process)
                    rl_store.finish(job['id'], 'failed', 'Research deadline exceeded')
                    return True
                time.sleep(0.2)
            complete = process.returncode == 0 and (directory / 'inventory.json').is_file()
            rl_store.finish(job['id'], 'succeeded' if complete else 'failed',
                            None if complete else 'Research process failed; inspect worker/training/engine logs')
    except Exception as exc:
        if process and process.poll() is None:
            terminate_owned(process)
        rl_store.finish(job['id'], 'failed', type(exc).__name__)
    return True


def loop():
    path = settings.HOME / 'runtime' / 'rl-worker.lock'
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
            return
        try:
            rl_store.recover()
            recovery_at = time.monotonic()
            while not _stop.is_set():
                try:
                    if time.monotonic() - recovery_at > 30:
                        rl_store.recover()
                        recovery_at = time.monotonic()
                    worked = run_one()
                except Exception:
                    worked = False
                _stop.wait(0.2 if worked else 1)
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def start():
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=loop, daemon=True, name='rl-research-worker')
        _thread.start()


def stop():
    _stop.set()
    if _thread:
        _thread.join(timeout=12)


if __name__ == '__main__':
    loop()
