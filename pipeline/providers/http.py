"""Polite HTTP client shared by all providers.

* custom User-Agent, ``timeout`` 20 s
* at most 1 request per second per host
* retries with exponential backoff on connection errors, 429 and 5xx
* on-disk cache in ``.cache/<date>/<host>/<sha1(url)>.json`` (one entry per
  URL and day; only 2xx/404 responses are cached)
* robots.txt honoured for our User-Agent (fetched once per host, cached)
* a Cloudflare/Akamai block (403/503 challenge) raises :class:`Blocked`;
  callers must stop the provider and never retry with a browser UA.

Every request is appended to ``Http.log`` with the response headers that
matter for the geo/bot-protection audit (status, server, cf-mitigated).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

import requests

USER_AGENT = 'Vareska-deals/1.0 (+https://github.com/stanislavmudra-hue/vareska-data)'
DEFAULT_TIMEOUT = 20
DEFAULT_CACHE_DIR = '.cache'
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_BLOCK_MARKERS = ('challenge-platform', 'cf-chl', 'Vyžadováno ověření', 'Access Denied',
                  'Just a moment', '_cf_chl_opt')


class HttpError(Exception):
    """Base class of the client's own errors."""


class Blocked(HttpError):
    """Bot protection refused the request (403/503 challenge). Do not retry."""


class RobotsDisallowed(HttpError):
    """robots.txt forbids the URL for our User-Agent."""


class HttpStatusError(HttpError):
    """Non-retryable, non-block HTTP error (e.g. 404 where a body was required)."""

    def __init__(self, status: int, url: str):
        super().__init__(f'HTTP {status} for {url}')
        self.status = status
        self.url = url


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str]
    text: str
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.text)


class RobotsRules:
    """Minimal robots.txt matcher with ``*`` and ``$`` wildcards (Google
    semantics: the longest matching rule wins, Allow wins a tie). The stdlib
    ``urllib.robotparser`` ignores wildcards, which would silently *allow*
    e.g. ``Disallow: *pageId=*`` - so we do not use it."""

    def __init__(self, text: str, user_agent: str = USER_AGENT):
        token = user_agent.split('/')[0].strip().lower()
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        in_rules = False
        self.crawl_delay: Optional[float] = None
        for raw in text.splitlines():
            line = raw.split('#', 1)[0].strip()
            if not line or ':' not in line:
                continue
            key, _, val = line.partition(':')
            key, val = key.strip().lower(), val.strip()
            if key == 'user-agent':
                if in_rules:
                    groups.append((agents, rules))
                    agents, rules, in_rules = [], [], False
                agents.append(val.lower())
            elif key in ('allow', 'disallow'):
                in_rules = True
                rules.append((key == 'allow', val))
            elif key == 'crawl-delay':
                in_rules = True
                try:
                    self.crawl_delay = float(val)
                except ValueError:
                    pass
        if agents:
            groups.append((agents, rules))
        specific = [r for a, r in groups if any(x != '*' and x in token for x in a)]
        generic = [r for a, r in groups if '*' in a]
        chosen = specific if specific else generic
        self.rules: list[tuple[bool, str, re.Pattern[str]]] = []
        for rs in chosen:
            for allow, path in rs:
                if not path:
                    continue
                self.rules.append((allow, path, self._compile(path)))

    @staticmethod
    def _compile(path: str) -> re.Pattern[str]:
        pat = ''.join('.*' if ch == '*' else re.escape(ch) for ch in path)
        if pat.endswith(re.escape('$')):
            pat = pat[:-len(re.escape('$'))] + '$'
        if not path.startswith('*'):
            pat = '^' + pat
        return re.compile(pat)

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        target = parts.path or '/'
        if parts.query:
            target += '?' + parts.query
        best: Optional[tuple[int, bool]] = None
        for allow, path, rx in self.rules:
            if rx.search(target):
                score = (len(path), allow)
                if best is None or score > best:
                    best = score
        return True if best is None else best[1]


@dataclass
class RobotsInfo:
    host: str
    status: Optional[int]
    text: str = ''
    error: Optional[str] = None
    rules: Optional[RobotsRules] = field(default=None, repr=False)

    def allowed(self, url: str, ua: str = USER_AGENT) -> bool:
        if self.rules is None:
            return True  # no robots.txt (or unreadable) -> allowed
        return self.rules.allowed(url)

    def summary(self) -> dict[str, Any]:
        lines = [l.strip() for l in self.text.splitlines() if l.strip() and not l.startswith('#')]
        return {'host': self.host, 'status': self.status, 'error': self.error,
                'rules': len(lines), 'disallow': [l for l in lines if l.lower().startswith('disallow')][:40],
                'crawl_delay': next((l for l in lines if l.lower().startswith('crawl-delay')), None)}


class Http:
    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR, min_interval: float = 1.0,
                 timeout: float = DEFAULT_TIMEOUT, user_agent: str = USER_AGENT,
                 use_cache: bool = True, retries: int = 3, today: Optional[dt.date] = None,
                 check_robots: bool = True, sleep=time.sleep, accept_language: str = 'cs-CZ,cs;q=0.9'):
        self.cache_dir = cache_dir
        self.min_interval = min_interval
        self.timeout = timeout
        self.user_agent = user_agent
        self.use_cache = use_cache
        self.retries = retries
        self.today = today or dt.date.today()
        self.check_robots = check_robots
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotsInfo] = {}
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': user_agent, 'Accept-Language': accept_language})
        self.log: list[dict[str, Any]] = []
        self.requests_made = 0
        self.cache_hits = 0

    # ---- cache -----------------------------------------------------------
    def _cache_path(self, url: str, headers: Optional[dict[str, str]]) -> str:
        host = urlsplit(url).netloc
        key = url + '|' + json.dumps(headers or {}, sort_keys=True)
        h = hashlib.sha1(key.encode('utf-8')).hexdigest()
        return os.path.join(self.cache_dir, self.today.isoformat(), host, h + '.json')

    def _cache_get(self, path: str) -> Optional[Response]:
        try:
            with open(path, encoding='utf-8') as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None
        return Response(d['url'], d['status'], d['headers'], d['text'], from_cache=True)

    def _cache_put(self, path: str, r: Response) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'url': r.url, 'status': r.status, 'headers': r.headers, 'text': r.text}, f,
                          ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass

    # ---- robots ----------------------------------------------------------
    def robots(self, url: str) -> RobotsInfo:
        parts = urlsplit(url)
        host = parts.netloc
        info = self._robots.get(host)
        if info is not None:
            return info
        robots_url = f'{parts.scheme}://{host}/robots.txt'
        info = RobotsInfo(host=host, status=None)
        self._robots[host] = info  # set early so the robots fetch itself is not checked
        try:
            r = self._get_raw(robots_url, None, cache=True)
            info.status = r.status
            if r.status == 200:
                info.text = r.text
                info.rules = RobotsRules(r.text, self.user_agent)
        except Blocked as e:
            info.status = 403
            info.error = f'blocked: {e}'
        except Exception as e:  # noqa: BLE001 - robots failure is not fatal
            info.error = str(e)
        return info

    def robots_summaries(self) -> list[dict[str, Any]]:
        return [i.summary() for i in self._robots.values()]

    # ---- requests --------------------------------------------------------
    def _throttle(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last[host] = time.monotonic()

    @staticmethod
    def _is_block(status: int, headers: dict[str, str], text: str) -> bool:
        if headers.get('cf-mitigated', '').lower() == 'challenge':
            return True
        if status in (401, 403):
            return True
        if status == 503 and any(m in text for m in _BLOCK_MARKERS):
            return True
        return False

    def _get_raw(self, url: str, headers: Optional[dict[str, str]], cache: bool) -> Response:
        host = urlsplit(url).netloc
        path = self._cache_path(url, headers)
        if cache and self.use_cache:
            cached = self._cache_get(path)
            if cached is not None:
                self.cache_hits += 1
                return cached
        attempt = 0
        while True:
            attempt += 1
            self._throttle(host)
            entry: dict[str, Any] = {'url': url, 'host': host, 'attempt': attempt}
            try:
                self.requests_made += 1
                resp = self.session.get(url, headers=headers or {}, timeout=self.timeout)
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                text = resp.text
                r = Response(url, resp.status_code, hdrs, text)
                entry.update(status=r.status, server=hdrs.get('server'), cf=hdrs.get('cf-mitigated'),
                             bytes=len(resp.content))
                self.log.append(entry)
            except (requests.ConnectionError, requests.Timeout) as e:
                entry.update(status=None, error=type(e).__name__)
                self.log.append(entry)
                if attempt > self.retries:
                    raise HttpError(f'{type(e).__name__} for {url}') from e
                self._sleep(2.0 ** attempt)
                continue
            if self._is_block(r.status, hdrs, text):
                raise Blocked(f'HTTP {r.status} bot protection ({hdrs.get("server", "?")}) for {url}')
            if r.status in _RETRY_STATUSES and attempt <= self.retries:
                ra = hdrs.get('retry-after')
                delay = float(ra) if ra and re.fullmatch(r'\d+', ra) else 2.0 ** attempt
                self._sleep(min(delay, 30))
                continue
            if cache and self.use_cache and (r.ok or r.status == 404):
                self._cache_put(path, r)
            return r

    def get(self, url: str, headers: Optional[dict[str, str]] = None, cache: bool = True,
            require_ok: bool = True) -> Response:
        """GET ``url``; raises :class:`RobotsDisallowed`, :class:`Blocked`,
        :class:`HttpStatusError` (when ``require_ok``) or :class:`HttpError`."""
        if self.check_robots:
            info = self.robots(url)
            if not info.allowed(url, self.user_agent):
                raise RobotsDisallowed(f'robots.txt of {info.host} disallows {url}')
        r = self._get_raw(url, headers, cache)
        if require_ok and not r.ok:
            raise HttpStatusError(r.status, url)
        return r

    def get_json(self, url: str, headers: Optional[dict[str, str]] = None, cache: bool = True) -> Any:
        h = {'Accept': 'application/json, */*;q=0.5'}
        if headers:
            h.update(headers)
        r = self.get(url, headers=h, cache=cache)
        try:
            return r.json()
        except ValueError as e:
            raise HttpError(f'invalid JSON from {url}: {e}') from e

    def host_report(self) -> dict[str, dict[str, Any]]:
        """Per-host status/server/cf summary of the requests actually made."""
        out: dict[str, dict[str, Any]] = {}
        for e in self.log:
            h = out.setdefault(e['host'], {'requests': 0, 'statuses': {}, 'server': None, 'cf_mitigated': None})
            h['requests'] += 1
            s = str(e.get('status'))
            h['statuses'][s] = h['statuses'].get(s, 0) + 1
            h['server'] = e.get('server') or h['server']
            h['cf_mitigated'] = e.get('cf') or h['cf_mitigated']
        return out
