"""Local password/session authentication; keys never traverse HTTP."""
import hashlib
import hmac
import secrets
import time
from fastapi import HTTPException, Request
from . import live_store as ledger

ORIGIN = 'http://127.0.0.1:8765'
HOST = '127.0.0.1:8765'
COOKIE = 'quantpilot_live'
TTL = 1800


def digest(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def initialize(password):
    if len(password) < 14:
        raise ValueError('Use at least 14 characters')
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if ledger.get('auth', db=db):
            raise ValueError('Local authentication already configured')
        salt = secrets.token_hex(32)
        ledger.put('auth', {'salt': salt, 'hash': digest(password, salt)}, db)


def origin(request):
    if request.headers.get('origin') != ORIGIN or request.headers.get('host') != HOST:
        raise HTTPException(403, 'Exact live control Origin and Host required')


def login(password):
    now = time.time()
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        attempts = [x for x in ledger.get('login_attempts', [], db) if now - x < 300]
        if len(attempts) >= 5:
            raise HTTPException(429, 'Retry after five minutes')
        attempts.append(now)
        ledger.put('login_attempts', attempts, db)
        auth = ledger.get('auth', db=db)
        valid = bool(auth and hmac.compare_digest(digest(password, auth['salt']), auth['hash']))
        if valid:
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            ledger.put('session:' + hashlib.sha256(token.encode()).hexdigest(),
                       {'expires': now + TTL, 'csrf': csrf}, db)
    # Raise outside transaction so failed attempts remain durable.
    if not valid:
        raise HTTPException(401, 'Invalid local credentials')
    return token, csrf


def session(request: Request, unsafe=False):
    token = request.cookies.get(COOKIE, '')
    key = 'session:' + hashlib.sha256(token.encode()).hexdigest()
    record = ledger.get(key)
    if not record or record['expires'] < time.time():
        raise HTTPException(401, 'Local login required')
    if unsafe:
        origin(request)
        if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), record['csrf']):
            raise HTTPException(403, 'Session CSRF token required')
    return key
