"""Albert - leaflet list from albert.cz + Publitas page text (docs/SOURCES.md
section 3). Secondary source (text parsing, medium confidence).

1. ``https://www.albert.cz/aktualni-letaky`` embeds ``__NEXT_DATA__`` with
   ``Leaflet`` objects (validity, ``viewUrl`` on letaky.albert.cz).
2. ``{viewUrl}/spreads.json?page=N`` gives the text layer of every page,
   parsed by :mod:`publitas`.

Only ``documentType == LEAFLET`` documents are used (hypermarket first; the
supermarket leaflet adds rows for products not seen in the HM one).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Optional

from .base import Offer, Provider
from .publitas import fetch_spreads, parse_spreads

LIST_URL = 'https://www.albert.cz/aktualni-letaky'
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_DMY_RE = re.compile(r'(\d{1,2})\.(\d{1,2})\.(\d{4})')


def _dmy(s: Optional[str]) -> Optional[str]:
    m = _DMY_RE.search(s or '')
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def parse_leaflet_list(html: str) -> list[dict[str, Any]]:
    """``Leaflet:*`` entries of the Apollo state: id, title, validity, viewUrl, locationType, documentType."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    apollo = (((data.get('props') or {}).get('pageProps') or {}).get('apolloState')) or {}
    out = []
    for key, v in apollo.items():
        if not (isinstance(key, str) and key.startswith('Leaflet:')) or not isinstance(v, dict):
            continue
        out.append({
            'id': v.get('id'), 'title': v.get('title'),
            'valid_from': _dmy(v.get('validityStartDateFormatted')),
            'valid_to': _dmy(v.get('validityEndDateFormatted')),
            'viewUrl': v.get('viewUrl'), 'locationType': v.get('locationType'),
            'documentType': v.get('documentType'),
        })
    return out


def select_leaflets(leaflets: list[dict[str, Any]], today: dt.date) -> list[dict[str, Any]]:
    """Current + next week's LEAFLET documents, hypermarket before supermarket."""
    today_iso = today.isoformat()
    horizon = (today + dt.timedelta(days=8)).isoformat()
    picked = [l for l in leaflets
              if l.get('documentType') == 'LEAFLET' and l.get('viewUrl') and l.get('valid_to')
              and l['valid_to'] >= today_iso and (l.get('valid_from') or '') <= horizon]
    picked.sort(key=lambda l: (l.get('valid_from') or '', 0 if l.get('locationType') == 'HYPERMARKET' else 1))
    return picked


class AlbertProvider(Provider):
    name = 'albert'
    stores = ('albert',)

    def fetch(self) -> list[Offer]:
        html = self.http.get(LIST_URL).text
        leaflets = select_leaflets(parse_leaflet_list(html), self.today)
        if not leaflets:
            self.notes.append('no current leaflet found in __NEXT_DATA__')
            return []
        offers: list[Offer] = []
        seen: set[tuple[str, str, float, bool]] = set()
        for leaf in leaflets:
            url = leaf['viewUrl']
            try:
                spreads = fetch_spreads(self.http, url)
            except Exception as e:  # noqa: BLE001 - fail soft per leaflet
                self.notes.append(f'{url}: {type(e).__name__}: {e}')
                continue
            rows = parse_spreads(spreads, 'albert', leaf['valid_from'], leaf['valid_to'], url, 'albert-text')
            kind = 'hm' if leaf.get('locationType') == 'HYPERMARKET' else 'sm'
            added = 0
            for o in rows:
                key = (o.title.lower(), o.valid_from or '', o.price_czk, o.club)
                if key in seen:
                    continue
                seen.add(key)
                o.raw['leaflet'] = leaf.get('title')
                o.raw['location'] = kind
                offers.append(o)
                added += 1
            self.notes.append(f'{leaf.get("title")} ({leaf["valid_from"]}..{leaf["valid_to"]}): '
                              f'{len(spreads)} spreads, {added} offers')
        return offers


def fetch(http=None, today=None) -> list[Offer]:
    return AlbertProvider(http, today).fetch()


def health(http=None, today=None) -> dict[str, Any]:
    return AlbertProvider(http, today).health()
