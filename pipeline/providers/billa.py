"""Billa - leaflet slugs from billa.cz + Publitas page text (docs/SOURCES.md
section 4). Secondary source (text parsing, medium confidence).

``https://www.billa.cz/letaky-billa?tab=letaky-billa/velky-letak`` embeds the
Publitas viewer links of the current and next "Velký leták" / "Malý leták"
(``view.publitas.com/billa-cz/velky-letak-16-9-22-9-2026/page/1``); the
validity is encoded in the slug. ``spreads.json`` is parsed by :mod:`publitas`.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .base import Offer, Provider
from .publitas import fetch_spreads, parse_spreads

TAB_URL = 'https://www.billa.cz/letaky-billa?tab=letaky-billa/velky-letak'
PUBLITAS_BASE = 'https://view.publitas.com/billa-cz/'
_SLUG_RE = re.compile(r'billa-cz/((velky|maly)-letak-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{4}))')


def parse_slugs(html: str) -> list[dict[str, Any]]:
    """Distinct leaflet slugs with validity decoded from the slug."""
    text = html.replace('\\u002F', '/')
    out: dict[str, dict[str, Any]] = {}
    for m in _SLUG_RE.finditer(text):
        slug, kind = m.group(1), m.group(2)
        d1, m1, d2, m2, y = (int(m.group(i)) for i in range(3, 8))
        try:
            vf = dt.date(y, m1, d1)
            vt = dt.date(y, m2, d2)
            if vt < vf:  # year rollover (velky-letak-30-12-5-1-2027)
                vf = dt.date(y - 1, m1, d1)
        except ValueError:
            continue
        out.setdefault(slug, {'slug': slug, 'kind': kind, 'valid_from': vf.isoformat(),
                              'valid_to': vt.isoformat(), 'url': PUBLITAS_BASE + slug})
    return list(out.values())


def select_leaflets(leaflets: list[dict[str, Any]], today: dt.date) -> list[dict[str, Any]]:
    today_iso = today.isoformat()
    horizon = (today + dt.timedelta(days=8)).isoformat()
    picked = [l for l in leaflets if l['valid_to'] >= today_iso and l['valid_from'] <= horizon]
    picked.sort(key=lambda l: (l['valid_from'], 0 if l['kind'] == 'velky' else 1))
    return picked


class BillaProvider(Provider):
    name = 'billa'
    stores = ('billa',)

    def fetch(self) -> list[Offer]:
        html = self.http.get(TAB_URL).text
        leaflets = select_leaflets(parse_slugs(html), self.today)
        if not leaflets:
            self.notes.append('no current leaflet slug found on billa.cz')
            return []
        offers: list[Offer] = []
        seen: set[tuple[str, str, float, bool]] = set()
        for leaf in leaflets:
            try:
                spreads = fetch_spreads(self.http, leaf['url'])
            except Exception as e:  # noqa: BLE001 - fail soft per leaflet
                self.notes.append(f'{leaf["slug"]}: {type(e).__name__}: {e}')
                continue
            rows = parse_spreads(spreads, 'billa', leaf['valid_from'], leaf['valid_to'], leaf['url'], 'billa-text')
            added = 0
            for o in rows:
                key = (o.title.lower(), o.valid_from or '', o.price_czk, o.club)
                if key in seen:
                    continue
                seen.add(key)
                o.raw['leaflet'] = leaf['slug']
                offers.append(o)
                added += 1
            self.notes.append(f'{leaf["slug"]}: {len(spreads)} spreads, {added} offers')
        return offers


def fetch(http=None, today=None) -> list[Offer]:
    return BillaProvider(http, today).fetch()


def health(http=None, today=None) -> dict[str, Any]:
    return BillaProvider(http, today).health()
