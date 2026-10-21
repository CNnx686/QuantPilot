"""Interactive enrollment. Never accepts credentials through argv, env or HTTP."""
import argparse
import ctypes
import getpass
import hashlib
import json
import os
import secrets
import subprocess
from ctypes import wintypes
from . import live_auth, live_store as ledger


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(payload, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('Credential enrollment requires Windows CurrentUser DPAPI')
    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    method = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise RuntimeError('Windows credential protection failed')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(ctypes.cast(output.data, ctypes.c_void_p))


def restrict(directory):
    # SID is resolved by Windows rather than accepting a supplied user name.
    sid = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True).strip().split(',')[-1].strip('"')
    subprocess.run(['icacls', str(directory), '/inheritance:r', '/grant:r', sid + ':(OI)(CI)F'],
                   check=True, capture_output=True, timeout=10)


def enroll(key, secret):
    if not key or not secret:
        raise ValueError('Both credential fields are required')
    root = ledger.path().parent
    root.mkdir(parents=True, exist_ok=True)
    restrict(root)
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if ledger.get('deployment', db=db):
            raise ValueError('Account changes require removal of the existing deployment after flat recovery')
        old = ledger.get('account', {}, db)
        ref = secrets.token_hex(16)
        ciphertext = dpapi(json.dumps({'apiKey': key, 'secret': secret}).encode())
        (root / (ref + '.dpapi')).write_bytes(ciphertext)
        account = {'ref': ref, 'fingerprint': hashlib.sha256(key.encode()).hexdigest()[:16],
                   'revision': old.get('revision', 0) + 1, 'venue': 'binance'}
        ledger.put('account', account, db)
        ledger.put('preflight', None, db)
    return account


def credentials(reference):
    account = ledger.get('account')
    if not account or account['ref'] != reference:
        raise ValueError('Unknown account reference')
    return json.loads(dpapi((ledger.path().parent / (reference + '.dpapi')).read_bytes(), True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['init-auth', 'enroll-account'])
    args = parser.parse_args()
    if args.command == 'init-auth':
        password = getpass.getpass('New local control password (14+ characters): ')
        if password != getpass.getpass('Repeat password: '):
            raise ValueError('Passwords differ')
        ledger.path().parent.mkdir(parents=True, exist_ok=True)
        restrict(ledger.path().parent)
        live_auth.initialize(password)
        print('Local authentication configured.')
    else:
        result = enroll(getpass.getpass('Binance API key: '), getpass.getpass('Binance API secret: '))
        print('Enrolled reference: ' + result['ref'])


if __name__ == '__main__':
    main()
