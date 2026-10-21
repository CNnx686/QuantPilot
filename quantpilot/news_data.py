"""Allowlisted official RSS, immutable observation-time news versions, plain text only."""
import hashlib
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import uuid
import xml.etree.ElementTree as ET
import certifi
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

from . import public_store

FEEDS = {
    'coindesk': {'name': 'CoinDesk', 'url': 'https://www.coindesk.com/arc/outboundfeeds/rss', 'host': 'www.coindesk.com', 'domain': 'coindesk.com'},
    'federal_reserve': {'name': 'Federal Reserve', 'url': 'https://www.federalreserve.gov/feeds/press_all.xml', 'host': 'www.federalreserve.gov', 'domain': 'federalreserve.gov'},
    'ecb': {'name': 'European Central Bank', 'url': 'https://www.ecb.europa.eu/rss/press.html', 'host': 'www.ecb.europa.eu', 'domain': 'ecb.europa.eu'},
}
EVENTS = ('monetary_policy', 'regulation', 'security', 'market', 'other')
MAX_BYTES = 512000


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        elif tag in {'p', 'br', 'div', 'li'}:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value, limit=500):
    parser = PlainText()
    parser.feed(html.unescape(value or '')[:20000])
    # Stored content is plain text. Render with textContent/React text, never innerHTML.
    return re.sub(r'\s+', ' ', re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', ''.join(parser.parts))).strip()[:limit]


def safe_link(value, source):
    parsed = urlsplit(value.strip())
    domain = FEEDS[source]['domain']
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    if parsed.hostname != domain and not (parsed.hostname or '').endswith('.' + domain):
        return None
    return urlunsplit(('https', parsed.netloc, parsed.path, parsed.query, ''))[:2000]


def public_addresses(host):
    addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError('feed_address_rejected')
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=12, context=ssl.create_default_context(cafile=certifi.where()))
        self.address = address

    def connect(self):
        # Connect to the address already validated, retaining TLS hostname verification.
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def fetch(source):
    if source not in FEEDS:
        raise ValueError('Unknown news source')
    feed = FEEDS[source]
    connection = PinnedHTTPS(feed['host'], public_addresses(feed['host'])[0])
    deadline = time.monotonic() + 25
    try:
        connection.request('GET', urlsplit(feed['url']).path, headers={'User-Agent': 'QuantPilot-LocalResearch/2', 'Accept': 'application/rss+xml, application/xml, text/xml', 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        # No redirects, arbitrary URL input, proxy credentials or article fetching.
        if response.status != 200:
            raise ValueError('feed_http_failure')
        if response.getheader('Content-Encoding', 'identity') != 'identity':
            raise ValueError('feed_encoding_rejected')
        chunks = bytearray()
        while True:
            if time.monotonic() > deadline:
                raise ValueError('feed_deadline_exceeded')
            chunk = response.read(16384)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_BYTES:
                raise ValueError('feed_size_limit')
        return bytes(chunks)
    finally:
        connection.close()


def publication(value):
    if not value:
        return None
    try:
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def classify(title, summary):
    text = (title + ' ' + summary).lower()
    assets = []
    if re.search(r'\b(bitcoin|btc)\b', text):
        assets.append('BTC')
    if re.search(r'\b(ethereum|ether|eth)\b', text):
        assets.append('ETH')
    event = 'other'
    for candidate, terms in [('security', ('hack', 'cyber', 'breach')), ('monetary_policy', ('interest rate', 'monetary', 'fomc', 'inflation', 'policy rate')), ('regulation', ('regulation', 'enforcement', 'supervision', 'penalty', 'fine')), ('market', ('market', 'liquidity', 'economic', 'financial'))]:
        if any(term in text for term in terms):
            event = candidate
            break
    return assets, event


def parse(source, raw):
    if source not in FEEDS or len(raw) > MAX_BYTES:
        raise ValueError('invalid_feed')
    # UTF-8 only prevents encoded declaration bypass. Reject DTD/entities before parsing.
    text = raw.decode('utf-8-sig')
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise ValueError('unsafe_xml')
    root = ET.fromstring(text)
    if root.tag != 'rss':
        raise ValueError('unsupported_feed_format')
    records = []
    for item in root.findall('./channel/item')[:50]:
        title = plain(item.findtext('title'), 300)
        summary = plain(item.findtext('description'), 500)
        link = safe_link(item.findtext('link') or '', source)
        if not title or not link:
            continue
        assets, event = classify(title, summary)
        records.append({'source': source, 'title': title, 'summary': summary, 'url': link,
            'published_at': publication(item.findtext('pubDate')), 'assets': assets, 'event': event,
            'classification': 'local_keyword_rules', 'relevance': 'direct_asset_mention' if assets else 'macro_context',
            'item_key': hashlib.sha256(link.encode()).hexdigest()})
    return records


def ingest(source, raw):
    records = parse(source, raw)
    added = 0
    with public_store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        public_store.require_lease(conn)
        for record in records:
            content_hash = hashlib.sha256(public_store.dumps(record).encode()).hexdigest()
            latest = conn.execute('SELECT * FROM news WHERE source=? AND item_key=? ORDER BY rowid DESC LIMIT 1', (source, record['item_key'])).fetchone()
            if latest and latest['content_hash'] == content_hash:
                continue
            observed = public_store.now()
            identifier = str(uuid.uuid4())
            first_seen = latest['first_seen_at'] if latest else observed
            available = max(observed, record['published_at'] or observed)
            record.update(id=identifier, content_hash=content_hash, first_seen_at=first_seen,
                revision_seen_at=observed, available_at=available, publisher_timestamp_in_future=(record['published_at'] or observed) > observed,
                previous_version_id=latest['id'] if latest else None,
                availability_note='Known only from this local observation time; publication time is publisher-reported')
            conn.execute('INSERT INTO news VALUES(?,?,?,?,?,?,?)', (identifier, source, record['item_key'], content_hash, first_seen, observed, public_store.dumps(record)))
            added += 1
    return {'source': source, 'items_parsed': len(records), 'new_versions': added}


def refresh(source):
    try:
        result = ingest(source, fetch(source))
    except Exception:
        public_store.save_cache('news:' + source, error='official_feed_request_or_parse_failed')
        raise ValueError('official_feed_request_or_parse_failed') from None
    public_store.save_cache('news:' + source, result)
    return result


def get(identifier):
    with public_store.connect() as conn:
        row = conn.execute('SELECT record FROM news WHERE id=?', (identifier,)).fetchone()
        return json.loads(row['record']) if row else None


def list_items(source=None, asset=None, event=None, limit=50, before=None):
    with public_store.connect() as conn:
        rows = conn.execute('''SELECT n.record FROM news n WHERE NOT EXISTS
          (SELECT 1 FROM news newer WHERE newer.source=n.source AND newer.item_key=n.item_key AND newer.rowid>n.rowid)
          AND (? IS NULL OR n.source=?) AND (? IS NULL OR n.revision_seen_at<?)
          AND (? IS NULL OR EXISTS (SELECT 1 FROM json_each(json_extract(n.record,'$.assets')) WHERE value=?))
          AND (? IS NULL OR json_extract(n.record,'$.event')=?)
          ORDER BY n.revision_seen_at DESC,n.rowid DESC LIMIT ?''', (source,source,before,before,asset,asset,event,event,limit)).fetchall()
    return [json.loads(row['record']) for row in rows]


def sources():
    return [{'id': key, 'name': value['name'], 'url': value['url'], **public_store.cached('news:' + key, 1800)} for key, value in FEEDS.items()]
