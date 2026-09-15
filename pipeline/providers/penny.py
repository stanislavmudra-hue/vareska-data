"""Penny - commercetools product-discovery API (docs/SOURCES.md section 5).

``GET https://www.penny.cz/api/product-discovery/categories/vsechny-akce-99000000/products?page=N&pageSize=100``
lists the web-featured promotions of this and next week (~60 items). Prices
are integers in halere (1290 = 12,90 Kc); ``perStandardizedQuantity`` is per
``basePriceFactor`` x ``baseUnitShort``.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import Offer, Provider, compute_price_per_kg, iso_date

BASE = 'https://www.penny.cz/api/product-discovery/categories/vsechny-akce-99000000/products'
PAGE_SIZE = 100
MAX_PAGES = 10
_UNITS = {'g': 'g', 'kg': 'kg', 'ml': 'ml', 'l': 'l', 'ks': 'ks', 'kus': 'ks'}


def _czk(v: Optional[int]) -> Optional[float]:
    return None if v is None else round(v / 100.0, 2)


def _per_kg(block: Optional[dict[str, Any]], price: dict[str, Any], title: str) -> Optional[float]:
    """``perStandardizedQuantity`` (halere) per ``basePriceFactor`` ``baseUnitShort``."""
    if not block or block.get('perStandardizedQuantity') is None:
        return None
    unit = _UNITS.get((price.get('baseUnitShort') or '').lower())
    try:
        factor = float(price.get('basePriceFactor') or 1)
    except ValueError:
        factor = 1.0
    return compute_price_per_kg(block['perStandardizedQuantity'] / 100.0, unit, factor, title)


def parse_product(p: dict[str, Any], source_url: str = BASE) -> list[Offer]:
    title = (p.get('name') or '').strip()
    price = p.get('price') or {}
    regular = price.get('regular') or {}
    value = regular.get('value')
    if not title or value is None:
        return []
    unit = _UNITS.get((p.get('volumeLabelShort') or '').lower())
    qty = None
    if unit and p.get('amount'):
        try:
            qty = float(str(p['amount']).replace(',', '.'))
        except ValueError:
            qty = None
    crossed = _czk(price.get('crossed'))
    valid_from, valid_to = iso_date(price.get('validityStart')), iso_date(price.get('validityEnd'))
    cats = [c.get('name') for grp in (p.get('parentCategories') or []) for c in grp if c.get('name')]
    common = dict(store='penny', title=title, unit=unit, quantity=qty, valid_from=valid_from,
                  valid_to=valid_to, source_url=source_url, source='penny')
    raw = {'sku': p.get('sku'), 'category': p.get('category'), 'categories': cats,
           'discountPercentage': price.get('discountPercentage'),
           'promotionType': regular.get('promotionType'), 'tags': regular.get('tags')}
    offers = [Offer(price_czk=_czk(value), price_per_kg=_per_kg(regular, price, title),
                    original_price_czk=crossed, is_promo=True, raw=raw, **common)]
    loyalty = price.get('loyalty') or {}
    lv = loyalty.get('value')
    if lv is not None and lv < value:
        offers.append(Offer(price_czk=_czk(lv), price_per_kg=_per_kg(loyalty, price, title),
                            original_price_czk=crossed or _czk(value), is_promo=True, club=True,
                            raw={**raw, 'loyalty': True}, **common))
    return offers


class PennyProvider(Provider):
    name = 'penny'
    stores = ('penny',)

    def fetch(self) -> list[Offer]:
        offers: list[Offer] = []
        seen: set[str] = set()
        total = None
        for page in range(MAX_PAGES):
            url = f'{BASE}?page={page}&pageSize={PAGE_SIZE}'
            data = self.http.get_json(url)
            results = data.get('results') or []
            total = data.get('total', total)
            for p in results:
                key = f"{p.get('sku')}|{(p.get('price') or {}).get('validityStart')}"
                if key in seen:
                    continue
                seen.add(key)
                offers.extend(parse_product(p, url))
            if len(results) < PAGE_SIZE or (total is not None and (page + 1) * PAGE_SIZE >= total):
                break
        self.notes.append(f'total={total}, offers={len(offers)}')
        return offers


def fetch(http=None, today=None) -> list[Offer]:
    return PennyProvider(http, today).fetch()


def health(http=None, today=None) -> dict[str, Any]:
    return PennyProvider(http, today).health()
