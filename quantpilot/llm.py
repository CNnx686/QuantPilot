"""Optional bounded OpenAI-compatible configuration generator; never executes output."""
import asyncio
import json
import os
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from . import settings
from .research import ResearchSpec

NAMES = ("QUANTPILOT_LLM_BASE_URL", "QUANTPILOT_LLM_MODEL", "QUANTPILOT_LLM_API_KEY")
MAX_RESPONSE_BYTES = 65536
REQUEST_DEADLINE = 20.0


class ProviderUnavailable(ValueError):
    pass


class ProviderError(ValueError):
    pass


class InvalidSuggestion(ValueError):
    pass


def configuration():
    values = {}
    path = settings.HOME / '.env'
    if path.is_file():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            key, separator, value = line.strip().partition('=')
            if separator and key in NAMES:
                values[key] = value.strip().strip('"').strip("'")
    return {name: os.environ.get(name) or values.get(name, '') for name in NAMES}


def metadata():
    values = configuration()
    configured = all(values.values())
    return {"configured": configured, "model": values[NAMES[1]] if configured else None,
            "reason": "OpenAI-compatible provider configured; requests occur only when explicitly selected" if configured else "Set QUANTPILOT_LLM_BASE_URL, QUANTPILOT_LLM_MODEL and QUANTPILOT_LLM_API_KEY locally"}


def client_factory():
    return httpx.AsyncClient(timeout=20.0, follow_redirects=False)


async def request_configuration(url, key, payload):
    # Unlike a socket read timeout, this deadline includes slow headers and trickles.
    async with asyncio.timeout(REQUEST_DEADLINE):
        async with client_factory() as client:
            async with client.stream('POST', url, headers={'Authorization': 'Bearer ' + key}, json=payload) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ProviderError("Provider response exceeded the size limit")
                    raw.extend(chunk)
                return raw


def suggest(prompt, dataset_id):
    values = configuration()
    if not all(values.values()):
        raise ProviderUnavailable("Online LLM is not configured")
    base, model, key = (values[name] for name in NAMES)
    parsed = urlparse(base)
    if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'})) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderUnavailable("Provider URL must be HTTPS or local HTTP, without credentials, query or fragment")
    payload = {"model": model, "max_tokens": 1800, "temperature": 0,
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content":
                   "Return only one JSON research configuration matching this schema. No code, shell, factors or unsupported strategies. "
                   "Supported: SmaTrend or FreqaiTrend, BTC/USDT and ETH/USDT, 1h, development/validation only within [2025-04-01,2025-10-01). "
                   "Default start 2025-04-01 end 2025-07-01, wallet 10000, stake 1000, fee .001. End is exclusive. "
                   "If the user's requirements are unsupported return {\"unsupported\":\"reason\"}, do not silently replace them. "
                   "dataset_id must be " + json.dumps(dataset_id) + ". Schema: " + json.dumps(ResearchSpec.model_json_schema())},
                   {"role": "user", "content": prompt}]}
    try:
        # FastAPI invokes this sync endpoint in its worker thread, outside an event loop.
        raw = asyncio.run(request_configuration(base.rstrip('/') + '/chat/completions', key, payload))
        envelope = json.loads(raw)
        content = envelope['choices'][0]['message']['content']
    except (TimeoutError, httpx.HTTPError, OSError, ValueError, KeyError, IndexError, TypeError):
        # Remote bodies and exceptions can contain credentials. Return no upstream text.
        raise ProviderError("Online provider request failed or returned an invalid response") from None
    try:
        spec = ResearchSpec.model_validate_json(content)
        if spec.dataset_id != dataset_id:
            raise ValueError("Dataset mismatch")
    except (ValidationError, ValueError, TypeError):
        raise InvalidSuggestion("Provider output is unsupported or failed strict ResearchSpec validation; edit manually or refine the request") from None
    return spec, {"provider": "llm", "model": model}
