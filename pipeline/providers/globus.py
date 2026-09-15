"""Globus - GSOA action offers API (docs/SOURCES.md section 7). Primary source.

``GET /api/v1/gsoa/actionOffers/houses/{house}/actionProductsCatalog?page=N&pageSize=100``
returns fully structured promo items for one hypermarket (house 4005 =
Praha-Cakovice). ``baseComparisonPrice`` + ``baseComparisonSaleUnitSizeText``
give the unit price per kg/l/ks directly.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import Offer, Provider, compute_price_per_kg, iso_date, parse_pack

BASE = 'https://www.globus.cz/api/v1/gsoa/actionOffers/houses/{house}/actionProductsCatalog'
DEFAULT_HOUSE = 4005
PAGE_SIZE = 100
MAX_PAGES = 30
PERMANENT_VALID_TO = '9999-12-31'


def _unit_from_id(unit_id: Optional[str]) -> Optional[str]:
    if not unit_id:
        return None
    u = unit_id.lower()
    return u if u in ('kg', 'g', 'l', 'ml', 'ks') else None


def _base_per_kg(price: Optional[float], size_text: Optional[str], title: str) -> Optional[float]:
    """``baseComparisonPrice`` is per ``baseComparisonSaleUnitSizeText`` ("1 kg", "1,00 l", "1,00 ks")."""
    if price is None:
        return None
    qty, unit = parse_pack(size_text)
    if not qty or not unit:
        return None
    return compute_price_per_kg(price, unit, qty, title)


def parse_product(p: dict[str, Any], house: int = DEFAULT_HOUSE, source_url: str = '') -> list[Offer]:
    """One catalogue item -> 1 offer (+1 club offer when ``bonusProgramPrice`` is cheaper)."""
    ih = p.get('productInHouse') or {}
    title = (p.get('name') or p.get('regulatedName') or '').strip()
    price = ih.get('actualPrice')
    if not title or price is None:
        return []
    qty, unit = parse_pack(p.get('sellUnitSizeText'))
    if unit is None:
        unit = _unit_from_id(p.get('unitId'))
        qty = p.get('unitAmount') or (1 if unit else None)
        if unit and qty is not None:
            qty = float(qty)
    valid_to = iso_date(ih.get('priceValidTo'))
    if valid_to == PERMANENT_VALID_TO:
        valid_to = None
    original = ih.get('originalPrice')
    base: dict[str, Any] = dict(
        store='globus', title=title, unit=unit, quantity=qty,
        valid_from=iso_date(ih.get('priceValidFrom')), valid_to=valid_to,
        source_url=source_url or BASE.format(house=house), source='globus',
        original_price_czk=original, is_promo=original is not None,
    )
    raw = {'vanr': p.get('vanr'), 'ean': (p.get('ean') or [None])[0],
           'categories': p.get('productCategories') or [],
           'sellUnitSizeText': p.get('sellUnitSizeText'), 'unitId': p.get('unitId'),
           'unitAmount': p.get('unitAmount'), 'discountPercentage': ih.get('discountPercentage'),
           'comparison': ih.get('baseComparisonSaleUnitSizeText'), 'availability': ih.get('availability'),
           'house': house}
    offers = [Offer(price_czk=float(price),
                    price_per_kg=_base_per_kg(ih.get('baseComparisonPrice'),
                                              ih.get('baseComparisonSaleUnitSizeText'), title),
                    raw=raw, **base)]
    bonus = ih.get('bonusProgramPrice') or {}
    bprice = bonus.get('actualPrice')
    if bprice is not None and bprice < price:
        bvt = iso_date(bonus.get('priceValidTo'))
        offers.append(Offer(price_czk=float(bprice),
                            price_per_kg=_base_per_kg(bonus.get('baseComparisonPrice'),
                                                      bonus.get('baseComparisonSaleUnitSizeText'), title),
                            raw={**raw, 'bonus': True},
                            **{**base, 'club': True, 'is_promo': True,
                               'valid_from': iso_date(bonus.get('priceValidFrom')) or base['valid_from'],
                               'valid_to': None if bvt == PERMANENT_VALID_TO else (bvt or valid_to),
                               'original_price_czk': bonus.get('originalPrice') or price}))
    return offers


class GlobusProvider(Provider):
    name = 'globus'
    stores = ('globus',)

    def __init__(self, http=None, today=None, house: int = DEFAULT_HOUSE):
        super().__init__(http, today)
        self.house = house

    def fetch(self) -> list[Offer]:
        offers: list[Offer] = []
        total = None
        for page in range(MAX_PAGES):
            url = f'{BASE.format(house=self.house)}?page={page}&pageSize={PAGE_SIZE}'
            data = self.http.get_json(url)
            products = data.get('products') or []
            total = data.get('totalCount', total)
            for p in products:
                offers.extend(parse_product(p, self.house, url))
            if not data.get('paginationShowMore') or not products:
                break
        self.notes.append(f'house {self.house}: totalCount={total}, offers={len(offers)}')
        return offers


def fetch(http=None, today=None) -> list[Offer]:
    return GlobusProvider(http, today).fetch()


def health(http=None, today=None) -> dict[str, Any]:
    return GlobusProvider(http, today).health()
