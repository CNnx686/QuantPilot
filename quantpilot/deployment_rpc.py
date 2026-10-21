"""Authenticated loopback-only RPC; exception text never includes private connection data."""
import base64
import json
import math
import time
import urllib.error
import urllib.request


class Unavailable(RuntimeError):
    pass


def call(private, endpoint, *, method='GET'):
    if endpoint not in {'show_config', 'health', 'balance', 'status', 'trades', 'stopentry', 'start'}:
        raise ValueError('Unsupported engine operation')
    auth = base64.b64encode((private['username'] + ':' + private['password']).encode()).decode()
    request = urllib.request.Request(f"http://127.0.0.1:{int(private['port'])}/api/v1/{endpoint}",
                                     method=method, headers={'Authorization': 'Basic ' + auth})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=2) as response:
            return json.load(response)
    except (OSError, ValueError) as exc:
        # Freqtrade reports an empty open-position list as an RPC error in some versions.
        if endpoint == 'status' and isinstance(exc, urllib.error.HTTPError):
            try:
                if json.loads(exc.read()).get('error', '').endswith('no active trade'):
                    return []
            except (ValueError, OSError):
                pass
        raise Unavailable('Engine RPC unavailable') from None


def ready(config, health):
    timestamp = health.get('last_process_ts')
    return (config.get('dry_run') is True and config.get('runmode') == 'dry_run'
            and config.get('strategy') == 'QuantPilotSmaDryRun'
            and config.get('force_entry_enable') is False
            and config.get('state') in {'running', 'paused'}
            and isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool)
            and math.isfinite(timestamp) and -5 <= time.time() - timestamp <= 90)
