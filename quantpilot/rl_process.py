"""Identify only this job's exact Python commands; prove owned children exited on recovery."""
import os
import time
from pathlib import Path
import psutil


def identity(pid):
    process = psutil.Process(pid)
    return {'pid': pid, 'created': process.create_time()}


def matches_command(command, identifier, directory):
    if not command or len(command) < 4:
        return False
    # Match argument arrays, never command substrings or unrelated Python processes.
    if command[1:3] == ['-m', 'quantpilot.rl_runner']:
        return len(command) == 4 and command[3] == identifier
    if command[1:3] == ['-m', 'quantpilot.rl_train']:
        return len(command) == 4 and Path(command[3]).resolve() == directory.resolve()
    if command[1:4] == ['-m', 'freqtrade', 'backtesting'] and '--backtest-directory' in command:
        index = command.index('--backtest-directory') + 1
        return index < len(command) and Path(command[index]).resolve().is_relative_to((directory / 'replays').resolve())
    return False


def find_owned(identifier, directory):
    found, uncertain = [], False
    for process in psutil.process_iter(['pid', 'name']):
        if 'python' not in (process.info['name'] or '').lower():
            continue
        try:
            if matches_command(process.cmdline(), identifier, directory):
                found.append(process)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            uncertain = True
    return found, uncertain


def recover_owned(identifier, directory, recorded=None):
    owned, uncertain = find_owned(identifier, directory)
    if recorded:
        try:
            process = psutil.Process(recorded['pid'])
            if abs(process.create_time() - recorded['created']) < .001:
                if not matches_command(process.cmdline(), identifier, directory):
                    return False
                owned.append(process)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            return False
    targets = {}
    try:
        # Suspend coordinators before enumerating descendants so they cannot launch new children.
        for process in owned:
            try:
                process.suspend()
                targets[(process.pid, process.create_time())] = process
                for child in process.children(recursive=True):
                    targets[(child.pid, child.create_time())] = child
            except psutil.NoSuchProcess:
                continue
        for process in targets.values():
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(list(targets.values()), timeout=8)
        if alive:
            return False
    except psutil.AccessDenied:
        return False
    finally:
        # A denied kill must not leave a surviving process permanently suspended.
        for process in targets.values():
            try:
                process.resume()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    time.sleep(.1)
    remaining, inaccessible = find_owned(identifier, directory)
    return not remaining and not uncertain and not inaccessible
