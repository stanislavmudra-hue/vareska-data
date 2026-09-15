"""kupi.cz - leaflet aggregator search, one request per ingredient term
(docs/SOURCES.md section 8). Fallback for every chain and the only source for
Kaufland and Tesco.

``GET https://www.kupi.cz/hledej?f={term}`` renders the first 18 matching
products with every store's offers in one HTML page. Terms come from
``pipeline/terms.json`` (``[{"id", "q", "neg", "priority"}]``); the product
title must contain every stemmed token of ``q`` and none of the negative
stems, mirroring the app's ``text_normalizer.dart`` rules.

Policy: robots.txt allows ``/hledej?f=`` (pagination and sorting are not
used), 1 request/s, results cached per day, stop on the first block.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from typing import Any, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from .base import Offer, Provider, nearest_year, parse_pack, parse_unit_price
from .http import Blocked, RobotsDisallowed
from ..textnorm import all_tokens_match, normalize_tokens

BASE = 'https://www.kupi.cz'
SEARCH = BASE + '/hledej?f={q}'
TERMS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'terms.json')

# kupi data-shop ids -> app store enum (others are ignored)
SHOP_IDS = {'1': 'tesco', '3': 'albert', '4': 'kaufland', '5': 'billa', '6': 'lidl',
            '7': 'penny', '27': 'globus'}
# leaflet period end weekday for "aktuální" (Mon=0): Wed-Tue chains end Tuesday, Lidl Sunday
PERIOD_END_WEEKDAY = {'lidl': 6}
DEFAULT_PERIOD_END_WEEKDAY = 1

_DATE_RE = re.compile(r'(\d{1,2})\.\s*(\d{1,2})\.')
_NUM_RE = re.compile(r'(\d+(?:[,.]\d+)?)')


def load_terms(path: str = TERMS_PATH, max_priority: Optional[int] = None) -> list[dict[str, Any]]:
    with open(path, encoding='utf-8') as f:
        terms = json.load(f)
    if max_priority is not None:
        terms = [t for t in terms if int(t.get('priority', 1)) <= max_priority]
    return terms


def title_matches(title: str, query: str, negatives: list[str]) -> bool:
    """All stemmed query tokens occur in the title and no negative stem does."""
    ttoks = normalize_tokens(title)
    if not ttoks:
        return False
    if not all_tokens_match(normalize_tokens(query), ttoks):
        return False
    for neg in negatives:
        n = neg.lower()
        if any(t.startswith(n) or (len(t) >= 4 and n.startswith(t)) for t in ttoks):
            return False
    return True


def period_end(store: str, today: dt.date) -> dt.date:
    wd = PERIOD_END_WEEKDAY.get(store, DEFAULT_PERIOD_END_WEEKDAY)
    return today + dt.timedelta(days=(wd - today.weekday()) % 7)


def parse_validity(text: str, store: str, today: dt.date) -> tuple[Optional[str], Optional[str]]:
    """Map kupi validity text to (valid_from, valid_to) ISO dates."""
    t = ' '.join((text or '').split()).lower()
    if not t:
        return None, None
    if 'dnes' in t:
        return today.isoformat(), today.isoformat()
    if 'zítra' in t or 'zitra' in t:
        return today.isoformat(), (today + dt.timedelta(days=1)).isoformat()
    hits = _DATE_RE.findall(t)
    if not hits:
        if 'aktu' in t:
            return today.isoformat(), period_end(store, today).isoformat()
        return None, None
    dates = [nearest_year(int(d), int(m), today) for d, m in hits[:2]]
    if len(dates) == 1:
        return today.isoformat(), dates[0]
    return dates[0], dates[1]


def _text(el) -> str:
    return ' '.join(el.get_text().split()) if el is not None else ''


def _num(s: str) -> Optional[float]:
    m = _NUM_RE.search(s.replace(chr(0xA0), ''))
    return float(m.group(1).replace(',', '.')) if m else None


def parse_search_page(html: str, term: dict[str, Any], today: dt.date,
                      source_url: str = '') -> list[Offer]:
    """Parse one ``/hledej`` page into offers of the 7 known chains whose
    product title matches ``term``."""
    soup = BeautifulSoup(html, 'lxml')
    offers: list[Offer] = []
    query, negatives = term.get('q') or '', term.get('neg') or []
    for group in soup.select('div.group_discounts'):
        name_el = group.select_one('div.product_name h2 strong')
        title = _text(name_el)
        if not title or not title_matches(title, query, negatives):
            continue
        pack_el = group.select_one('div.product_name h2 span.nowrap span')
        pack_qty, pack_unit = parse_pack(_text(pack_el))
        wrap = group.select_one('div.product--wrap')
        pid = wrap.get('data-product-id') if wrap else None
        avg = _num(_text(group.select_one('div.avg_price span')))
        seen: set[str] = set()
        for row in group.select('div.discount_row'):
            did = row.get('data-discount') or ''
            if did in seen:
                continue
            seen.add(did)
            store = SHOP_IDS.get(row.get('data-shop') or '')
            if store is None:
                continue
            add = row.select_one('a.btn_list_add')
            price = None
            if add is not None and add.get('data-price'):
                try:
                    price = float(add['data-price'])
                except ValueError:
                    price = None
            if price is None:
                price = _num(_text(row.select_one('strong.discount_price_value')))
            if price is None:
                continue
            qty, unit = parse_pack(_text(row.select_one('div.discount_amount')))
            if unit is None:
                qty, unit = pack_qty, pack_unit
            per_kg = parse_unit_price(_text(row.select_one('span.price_per_unit')), title)
            valid_el = row.select_one('div.discounts_validity')
            validity = _text(valid_el)
            valid_from, valid_to = parse_validity(validity, store, today)
            shop_name = _text(row.select_one('.discounts_shop_name'))
            leaflet = row.select_one('a.btn_link_leaflet')
            pct = _text(row.select_one('div.discount_percentage'))
            offers.append(Offer(
                store=store, title=title, price_czk=price, unit=unit, quantity=qty,
                price_per_kg=per_kg, valid_from=valid_from, valid_to=valid_to,
                source_url=source_url, source='kupi', ingredient_id=term.get('id'),
                club=row.select_one('div.discounts_club') is not None,
                original_price_czk=avg,
                raw={'discount_id': did, 'product_id': pid, 'shop': shop_name,
                     'shop_id': row.get('data-shop'), 'pack': _text(pack_el), 'validity': validity,
                     'currently_valid': 'valid_discount' in (valid_el.get('class') or []) if valid_el else None,
                     'percentage': pct or None, 'note': _text(row.select_one('div.discount_note')) or None,
                     'leaflet': leaflet.get('href') if leaflet else None, 'term': query},
            ))
    return offers


class KupiProvider(Provider):
    name = 'kupi'
    stores = tuple(SHOP_IDS.values())

    def __init__(self, http=None, today=None, terms: Optional[list[dict[str, Any]]] = None,
                 max_priority: Optional[int] = None, limit: Optional[int] = None):
        super().__init__(http, today)
        self.terms = terms if terms is not None else load_terms(max_priority=max_priority)
        if limit:
            self.terms = self.terms[:limit]

    def fetch(self) -> list[Offer]:
        offers: list[Offer] = []
        seen: set[tuple[str, str]] = set()
        failed = 0
        for term in self.terms:
            url = SEARCH.format(q=quote(term['q']))
            try:
                r = self.http.get(url)
            except (Blocked, RobotsDisallowed):
                self.notes.append(f'stopped at term {term["id"]}: blocked/robots')
                raise
            except Exception as e:  # noqa: BLE001 - one bad term must not kill the run
                failed += 1
                self.notes.append(f'{term["id"]}: {type(e).__name__}: {e}')
                continue
            for o in parse_search_page(r.text, term, self.today, url):
                key = (o.raw.get('discount_id') or '', o.ingredient_id or '')
                if key in seen:
                    continue
                seen.add(key)
                offers.append(o)
        self.notes.append(f'{len(self.terms)} terms, {failed} failed, {len(offers)} offers')
        return offers


def fetch(http=None, today=None, **kw) -> list[Offer]:
    return KupiProvider(http, today, **kw).fetch()


def health(http=None, today=None, **kw) -> dict[str, Any]:
    return KupiProvider(http, today, **kw).health()
