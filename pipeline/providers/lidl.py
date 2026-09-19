"""Lidl - in-store food product grid API (docs/SOURCES.md section 1). Primary source.

``GET https://www.lidl.<tld>/q/api/category/{path}?assortment=XX&locale=xx_XX&version=v2.0.0&fetchsize=200``
(no ``pageId`` - robots.txt disallows ``*pageId=*``; the numeric category id
in the path is enough). The response caps ``fetchsize`` at ~108, so we walk
the category facet tree from the root food category and fetch every leaf
small enough to fit in one response.

The storefront platform is the same in every market (cz/sk/pl/de/at); what
differs per domain is the assortment/locale pair, the root food category
slug (the numeric id ``s10068374`` is shared), the labels of non-food
sub-categories to skip and the language of the unit-price texts:

* cz  ``basePrice.text`` "cena za 100 g" / "400 g, 1 kg = 174,75 Kč"
* sk  ``basePrice.text`` "1 kg = 0,54", ``packaging.text`` "500 g balenie" / "3 kusy v balení"
* pl  ``packaging.text`` "250 g 100 g = 4,40", "3 szt. 1 szt. = 2,33 * cena przed obniżką: …"
* de  ``basePrice.text`` "1 l = 1.66" (online wine shop only, see ``SITES['de']``)
* at  ``basePrice.text`` "Je 250 g (1 kg = 13.96)", "Ab 3 Stk. je 500 g (1 kg = 2.64)"

``parse_pack_any`` / ``parse_unit_price_any`` in :mod:`base` handle all of them
(comma or point decimals, kg/g/l/ml, ks/kus/szt./St./Stk./Stück, currency
words, old-price disclaimers).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from .base import (Offer, Provider, parse_pack_any, parse_unit_price, parse_unit_price_any,
                   prague_date_from_epoch)
from .. import markets
from ..textnorm import fold


@dataclass(frozen=True)
class LidlSite:
    market: str
    base: str
    assortment: str
    locale: str
    root_path: str                  # root food category (``/c/<slug>/s10068374``)
    skip_labels: tuple[str, ...]    # folded labels of non-food sub-categories (prefix match)
    max_requests: int = 60
    note: str = ''


SITES: dict[str, LidlSite] = {
    'cz': LidlSite('cz', 'https://www.lidl.cz', 'CZ', 'cs_CZ', '/c/potraviny-a-napoje/s10068374',
                   ('domacnost', 'chovatelske', 'kvetiny', 'zdravi a krasa')),
    'sk': LidlSite('sk', 'https://www.lidl.sk', 'SK', 'sk_SK', '/c/jedlo-a-napoje/s10068374',
                   ('domacnost', 'chovatel', 'kvety', 'zdravie a krasa')),
    'pl': LidlSite('pl', 'https://www.lidl.pl', 'PL', 'pl_PL', '/c/zywnosc-i-napoje/s10068374',
                   ('dom ', 'gospodarstwo', 'kwiaty', 'rosliny', 'zdrowie', 'zwierz', 'chemia')),
    # lidl.de exposes only the online shop under the food root (1 000+ wine and
    # spirits cases, no in-store groceries) - keep the walk small and skip the
    # wine subtree; see docs/SOURCES.md.
    'de': LidlSite('de', 'https://www.lidl.de', 'DE', 'de_DE', '/c/essen-trinken/s10068374',
                   ('haushalt', 'blumen', 'gesundheit', 'wein'), max_requests=15,
                   note='category API lists the online wine shop only; in-store groceries are not exposed'),
    'at': LidlSite('at', 'https://www.lidl.at', 'AT', 'de_AT', '/c/essen-trinken/s10068374',
                   ('haushalt', 'blumen', 'gesundheit', 'tier')),
}

# Backwards compatible module constants (Czech site).
BASE = SITES['cz'].base
QUERY = '?assortment=CZ&locale=cs_CZ&version=v2.0.0&fetchsize=200'
ROOT_PATH = SITES['cz'].root_path
SKIP_LABELS = SITES['cz'].skip_labels
MAX_REQUESTS = SITES['cz'].max_requests

_PACK_RE = re.compile(r'(?:cena za\s*)?(\d+(?:[,.]\d+)?)\s*(kg|g|l|ml|ks)\b', re.IGNORECASE)
# badge types whose validFrom/validUntil describe an in-store offer period
# (cz: PAST/FUTURE ranges; pl/at also TODAY variants)
_DATE_TYPES = ('IN_STORE_PAST_DATE_RANGE', 'IN_STORE_FROM_FUTURE_DATE_RANGE', 'IN_STORE_FROM_DATE_PAST',
               'IN_STORE_TODAY_DATE_RANGE', 'IN_STORE_FROM_DATE_TODAY')


def pick_period(periods: list[tuple[str, Optional[str]]], today: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Several offer periods on one product (e.g. a bakery item listed for the
    next six weeks): the one containing ``today``, else the earliest one
    starting after today, else the latest past one."""
    if not periods:
        return None, None
    if today:
        for vf, vt in periods:
            if vf <= today and (vt is None or vt >= today):
                return vf, vt
        future = sorted(p for p in periods if p[0] > today)
        if future:
            return future[0]
        return max(periods, key=lambda p: (p[1] or p[0], p[0]))
    return periods[0]


def site_for(market: str) -> LidlSite:
    code = markets.get(market).code
    try:
        return SITES[code]
    except KeyError:
        raise KeyError(f'no Lidl site configured for market {code!r}') from None


def query_for(site: LidlSite) -> str:
    return f'?assortment={site.assortment}&locale={site.locale}&version=v2.0.0&fetchsize=200'


def parse_pack_text(text: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """Czech texts: ``"cena za 100 g"`` -> (100, 'g'); ``"400 g, 1 kg = 174,75 Kč"``
    -> (400, 'g'); ``"cena za 1 kg"`` -> (1, 'kg'); ``"0,75 l, 1 l = ..."`` -> (0.75, 'l')."""
    if not text:
        return None, None
    m = _PACK_RE.search(text.replace(chr(0xA0), ' '))
    if not m:
        return None, None
    return float(m.group(1).replace(',', '.')), m.group(2).lower()


def parse_item(item: dict[str, Any], category: str = '', source_url: str = '',
               market: str = 'cz', today: Optional[str] = None) -> Optional[Offer]:
    g = ((item.get('gridbox') or {}).get('data')) or {}
    title = (g.get('fullTitle') or (g.get('keyfacts') or {}).get('title') or '').strip()
    price_info = g.get('price') or {}
    price = price_info.get('price')
    if not title or price is None:
        return None
    base_text = (price_info.get('basePrice') or {}).get('text') or ''
    pack_text = (price_info.get('packaging') or {}).get('text') or ''
    if market == 'cz':
        qty, unit = parse_pack_text(base_text)
        if unit is None:
            qty, unit = parse_pack_text(pack_text)
        per_kg = parse_unit_price(base_text, title)
    else:
        qty, unit = parse_pack_any(base_text)
        if unit is None:
            qty, unit = parse_pack_any(pack_text)
        per_kg = parse_unit_price_any(base_text, title)
        if per_kg is None:
            per_kg = parse_unit_price_any(pack_text, title)
    old = price_info.get('oldPrice') or 0
    discount = (price_info.get('discount') or {}).get('discountText')
    badge_types: list[str] = []
    periods: list[tuple[str, Optional[str]]] = []
    for b in (g.get('stockAvailability') or {}).get('badgeInfoV2') or []:
        types = [bb.get('type') or '' for bb in b.get('badges') or []]
        badge_types.extend(types)
        if b.get('validFrom') and any(t in _DATE_TYPES for t in types):
            vf = prague_date_from_epoch(b['validFrom'])
            if vf:
                periods.append((vf, prague_date_from_epoch(b.get('validUntil'))))
    valid_from, valid_to = pick_period(periods, today)
    is_promo = bool(old and old > price) or bool(discount) or valid_to is not None
    return Offer(
        store='lidl', title=title, price_czk=float(price), unit=unit, quantity=qty,
        price_per_kg=per_kg, valid_from=valid_from, valid_to=valid_to,
        source_url=source_url, source='lidl' if market == 'cz' else f'lidl_{market}',
        original_price_czk=float(old) if old and old > price else None,
        is_promo=is_promo, market=market,
        currency=price_info.get('currencyCode') or markets.get(market).currency,
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
    market = 'cz'

    def __init__(self, http=None, today=None, market: str = 'cz'):
        super().__init__(http, today)
        self.site = site_for(market)
        self.market = self.site.market
        self.name = 'lidl' if self.market == 'cz' else f'lidl_{self.market}'

    def _url(self, path: str) -> str:
        return f'{self.site.base}/q/api/category{path}{query_for(self.site)}'

    def fetch(self) -> list[Offer]:
        site = self.site
        offers: list[Offer] = []
        seen_codes: set[str] = set()
        requests_used = 0
        if site.note:
            self.notes.append(site.note)
        root = self.http.get_json(self._url(site.root_path))
        requests_used += 1
        root_items = root.get('items') or []
        for it in root_items:
            code = str(it.get('code') or '')
            o = parse_item(it, '', self._url(site.root_path), self.market, self.today.isoformat())
            if o is not None and code not in seen_codes:
                seen_codes.add(code)
                offers.append(o)
        # small markets (sk/pl: < 108 products) fit into the root response;
        # otherwise walk the sub-categories
        queue: list[tuple[dict[str, Any], str]] = []
        if (root.get('numFound') or len(root_items)) > len(root_items):
            queue = [(c, c.get('label') or '') for c in _category_children(root)]
        visited: set[str] = set()
        while queue and requests_used < site.max_requests:
            node, label = queue.pop(0)
            path = (node.get('link') or {}).get('categoryPath')
            if not path or path in visited:
                continue
            visited.add(path)
            folded = fold(label)
            if any(folded.startswith(s.strip()) for s in site.skip_labels):
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
                o = parse_item(it, label, self._url(path), self.market, self.today.isoformat())
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


def provider_for(market: str):
    """Factory used by the registry: ``provider_for('sk')(http=..., today=...)``."""
    site = site_for(market)

    def make(http=None, today=None, **_ignored):
        return LidlProvider(http, today, market=site.market)

    make.__name__ = f'LidlProvider_{site.market}'
    make.market = site.market
    return make


def fetch(http=None, today=None, market: str = 'cz') -> list[Offer]:
    return LidlProvider(http, today, market).fetch()


def health(http=None, today=None, market: str = 'cz') -> dict[str, Any]:
    return LidlProvider(http, today, market).health()
