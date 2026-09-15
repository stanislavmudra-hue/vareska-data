"""CLI: fetch offers from one or all providers.

    python -m pipeline.providers.fetch --source all --out out/offers.json
    python -m pipeline.providers.fetch --source kupi --kupi-priority 1 --out out/offers.json

Writes ``<out>`` (``{"v":1,"fetched":..,"offers":[...]}``) and
``health.json`` next to it (per-provider status, per-host HTTP audit,
robots.txt summaries).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from typing import Any

from .http import Http
from .registry import DEFAULT_ORDER, TIER, get


def run(sources: list[str], http: Http, kupi_kwargs: dict[str, Any] | None = None,
        log=print) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    offers: list[dict[str, Any]] = []
    health: dict[str, Any] = {'v': 1, 'run': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
                              'today': http.today.isoformat(), 'sources': {}, 'hosts': {}, 'robots': []}
    for name in sources:
        cls = get(name)
        kwargs = dict(kupi_kwargs or {}) if name == 'kupi' else {}
        provider = cls(http=http, today=http.today, **kwargs)
        t0 = time.monotonic()
        rows, h = provider.run()
        h['tier'] = TIER.get(name)
        health['sources'][name] = h
        offers.extend(o.to_dict() for o in rows)
        log(f'[{name:8}] ok={h["ok"]} count={h["count"]} stores={h["stores"]} '
            f'requests={h["requests"]} {time.monotonic() - t0:.1f}s'
            + (f' error={h["error"]}' if h['error'] else ''))
        for n in h.get('notes') or []:
            log(f'           {n}')
    health['hosts'] = http.host_report()
    health['robots'] = http.robots_summaries()
    health['requests_made'] = http.requests_made
    health['cache_hits'] = http.cache_hits
    return offers, health


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Fetch supermarket offers.')
    ap.add_argument('--source', default='all',
                    help='all | comma-separated provider names (' + ', '.join(DEFAULT_ORDER) + ')')
    ap.add_argument('--out', default='out/offers.json', help='offers JSON path (health.json goes next to it)')
    ap.add_argument('--cache-dir', default='.cache')
    ap.add_argument('--no-cache', action='store_true', help='ignore the on-disk cache')
    ap.add_argument('--today', help='override the run date (YYYY-MM-DD)')
    ap.add_argument('--kupi-priority', type=int, default=None, help='only kupi terms with priority <= N')
    ap.add_argument('--kupi-limit', type=int, default=None, help='only the first N kupi terms (smoke test)')
    args = ap.parse_args(argv)

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    sources = list(DEFAULT_ORDER) if args.source == 'all' else [s.strip() for s in args.source.split(',') if s.strip()]
    for s in sources:
        get(s)  # validate early
    http = Http(cache_dir=args.cache_dir, use_cache=not args.no_cache, today=today)
    offers, health = run(sources, http, {'max_priority': args.kupi_priority, 'limit': args.kupi_limit})

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, 'w', encoding='utf-8', newline='\n') as f:
        json.dump({'v': 1, 'fetched': health['run'], 'today': today.isoformat(), 'sources': sources,
                   'count': len(offers), 'offers': offers}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, 'health.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(health, f, ensure_ascii=False, indent=1)
    per_store: dict[str, int] = {}
    for o in offers:
        per_store[o['store']] = per_store.get(o['store'], 0) + 1
    print(f'total offers: {len(offers)} {per_store}; requests: {http.requests_made}, cache hits: {http.cache_hits}')
    print(f'wrote {args.out} and {os.path.join(out_dir, "health.json")}')
    return 0 if any(h['ok'] for h in health['sources'].values()) else 1


if __name__ == '__main__':
    sys.exit(main())
