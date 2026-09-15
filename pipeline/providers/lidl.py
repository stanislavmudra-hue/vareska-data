"""Lidl - in-store food product grid API (docs/SOURCES.md section 1). Primary source.

``GET https://www.lidl.cz/q/api/category/{path}?assortment=CZ&locale=cs_CZ&version=v2.0.0&fetchsize=200``
(no ``pageId`` - robots.txt disallows ``*pageId=*``; the numeric category id
in the path is enough). The response caps ``fetchsize`` at ~108, so we walk
the category facet tree from the root food category and fetch every leaf
small enough to fit in one response.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .base import Offer, Provider, parse_unit_price, prague_date_from_epoch
from ..textnorm import fold

BASE = 'https://www.lidl.cz'
QUERY = '?assortment=CZ&locale=cs_CZ&version=v2.0.0&fetchsize=200'
ROOT_PATH = '/c/potraviny-a-napoje/s10068374'
# top-level categories that are not groceries (folded labels, prefix match)
SKIP_LABELS = ('domacnost', 'chovatelske', 'kvetiny', 'zdravi a krasa')
MAX_REQUESTS = 60

_PACK_RE = re.compile(r'(?:cena za\s*)?(\d+(?:[,.]\d+)?)\s*(kg|g|l|ml|ks)\b', re.IGNORECASE)
_DATE_TYPES = ('IN_STORE_PAST_DATE_RANGE', 'IN_STORE_FROM_FUTURE_DATE_RANGE', 'IN_STORE_FROM_DATE_PAST')


def parse_pack_text(text: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """``"cena za 100 g"`` -> (100, 'g'); ``"400 g, 1 kg = 174,75 Kč"`` -> (400, 'g');
    ``"cena za 1 kg"`` -> (1, 'kg'); ``"0,75 l, 1 l = ..."`` -> (0.75, 'l')."""
    if not text:
        return None, None
    m = _PACK_RE.search(text.replace(chr(0xA0), ' '))
    if not m:
        return None, None
    return float(m.group(1).replace(',', '.')), m.group(2).lower()


def parse_item(item: dict[str, Any], category: str = '', source_url: str = '') -> Optional[Offer]:
    g = ((item.get('gridbox') or {}).get('data')) or {}
    title = (g.get('fullTitle') or (g.get('keyfacts') or {}).get('title') or '').strip()
    price_info = g.get('price') or {}
    price = price_info.get('price')
    if not title or price is None:
        return None
    base_text = (price_info.get('basePrice') or {}).get('text') or ''
    pack_text = (price_info.get('packaging') or {}).get('text') or ''
    qty, unit = parse_pack_text(base_text)
    if unit is None:
        qty, unit = parse_pack_text(pack_text)
    per_kg = parse_unit_price(base_text, title)
    old = price_info.get('oldPrice') or 0
    discount = (price_info.get('discount') or {}).get('discountText')
    valid_from = valid_to = None
    badge_types: list[str] = []
    for b in (g.get('stockAvailability') or {}).get('badgeInfoV2') or []:
        for bb in b.get('badges') or []:
            badge_types.append(bb.get('type') or '')
        if b.get('validFrom') and any(t in _DATE_TYPES for t in badge_types):
            valid_from = prague_date_from_epoch(b['validFrom'])
            valid_to = prague_date_from_epoch(b.get('validUntil'))
    is_promo = bool(old and old > price) or bool(discount) or valid_to is not None
    return Offer(
        store='lidl', title=title, price_czk=float(price), unit=unit, quantity=qty,
        price_per_kg=per_kg, valid_from=valid_from, valid_to=valid_to,
        source_url=source_url, source='lidl',
        original_price_czk=float(old) if old and old > price else None,
        is_promo=is_promo,
        raw={'code': item.get('code') or g.get('erpNumber'), 'basePrice': base_text,
             'packaging': pack_text, 'discountText': discount, 'badges': badge_types,
             'category': category, 'canonicalPath': g.get('canonicalPath'),
             'wonCategory': (g.get('keyfacts') or {}).get('wonCategoryPrimary')},
    )


def _category_children(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Direct children of the selected node in the ``category`` facet tree."""
    for f in data.get('facets') or []:
        if f.get('code') != 'category':
            continue
        nodes = list(f.get('topvalues') or [])
        while nodes:
            n = nodes.pop(0)
            children = n.get('children') or []
            if n.get('selected') and children and not any(c.get('selected') for c in children):
                return children
            nodes.extend(children)
    return []


class LidlProvider(Provider):
    name = 'lidl'
    stores = ('lidl',)

    def _url(self, path: str) -> str:
        return f'{BASE}/q/api/category{path}{QUERY}'

    def fetch(self) -> list[Offer]:
        offers: list[Offer] = []
        seen_codes: set[str] = set()
        requests_used = 0
        root = self.http.get_json(self._url(ROOT_PATH))
        requests_used += 1
        queue = [(c, c.get('label') or '') for c in _category_children(root)]
        visited: set[str] = set()
        while queue and requests_used < MAX_REQUESTS:
            node, label = queue.pop(0)
            path = (node.get('link') or {}).get('categoryPath')
            if not path or path in visited:
                continue
            visited.add(path)
            folded = fold(label)
            if any(folded.startswith(s) for s in SKIP_LABELS):
                continue
            try:
                data = self.http.get_json(self._url(path))
            except Exception as e:  # noqa: BLE001 - fail soft per category
                self.notes.append(f'{label}: {type(e).__name__}: {e}')
                continue
            requests_used += 1
            items = data.get('items') or []
            num_found = data.get('numFound') or len(items)
            for it in items:
                code = str(it.get('code') or '')
                if code in seen_codes:
                    continue
                o = parse_item(it, label, self._url(path))
                if o is not None:
                    seen_codes.add(code)
                    offers.append(o)
            if num_found > len(items):
                children = _category_children(data)
                if children:
                    queue.extend((c, f'{label} / {c.get("label") or ""}') for c in children)
                else:
                    self.notes.append(f'{label}: {num_found} items, only {len(items)} returned')
        self.notes.append(f'{len(visited)} categories, {len(offers)} products')
        return offers


def fetch(http=None, today=None) -> list[Offer]:
    return LidlProvider(http, today).fetch()


def health(http=None, today=None) -> dict[str, Any]:
    return LidlProvider(http, today).health()
