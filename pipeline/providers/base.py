"""Common types and helpers for deal providers.

An :class:`Offer` is one promotional (or regular) price of one product in one
store. Providers only *collect* offers; matching them to catalogue ingredients
happens downstream (the ``kupi`` provider already knows the ingredient id it
searched for and records it in ``ingredient_id``).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Optional

STORES = ('albert', 'lidl', 'kaufland', 'tesco', 'billa', 'penny', 'globus')
UNITS = ('kg', 'l', 'ks', 'g', 'ml')

# Eggs are sold per piece; the app converts 10 pcs -> 0.55 kg (55 g per egg).
EGG_KG_PER_PIECE = 0.055
_EGG_RE = re.compile(r'\b(vejce|vajec|vajic|vajíčk|vajick|egg)', re.IGNORECASE)


@dataclass
class Offer:
    store: str                       # one of STORES
    title: str                       # product name as printed by the source
    price_czk: float                 # price of one pack / one selling unit
    unit: Optional[str] = None       # 'kg' | 'l' | 'ks' | 'g' | 'ml'
    quantity: Optional[float] = None  # amount of ``unit`` the price refers to
    price_per_kg: Optional[float] = None  # CZK per kg (l treated as kg)
    valid_from: Optional[str] = None  # ISO date YYYY-MM-DD
    valid_to: Optional[str] = None    # ISO date YYYY-MM-DD
    source_url: str = ''
    raw: dict[str, Any] = field(default_factory=dict)
    # optional extras
    source: str = ''                 # provider name (globus, lidl, kupi, ...)
    ingredient_id: Optional[str] = None  # set by term-driven providers (kupi)
    original_price_czk: Optional[float] = None
    club: bool = False               # loyalty-card price
    is_promo: bool = True            # False for regular assortment prices

    def __post_init__(self) -> None:
        if self.store not in STORES:
            raise ValueError(f'unknown store {self.store!r}')
        if self.unit is not None and self.unit not in UNITS:
            raise ValueError(f'unknown unit {self.unit!r}')
        if self.price_per_kg is None:
            self.price_per_kg = compute_price_per_kg(self.price_czk, self.unit, self.quantity, self.title)
        if self.price_per_kg is not None:
            self.price_per_kg = round(self.price_per_kg, 2)

    def to_dict(self) -> dict[str, Any]:
        """Canonical snake_case fields plus the camelCase aliases the mapper
        (``pipeline/mapper.py`` input contract) reads: ``price``, ``oldPrice``,
        ``packAmount``/``packUnit``, ``pack``, ``czkPerKg``, ``validFrom``,
        ``validTo``, ``url``, ``category``, ``ingredientId``."""
        d = dataclasses.asdict(self)
        raw = self.raw or {}
        category = raw.get('categories') or raw.get('wonCategory') or raw.get('category')
        d.update({
            'price': self.price_czk,
            'oldPrice': self.original_price_czk,
            'packAmount': self.quantity,
            'packUnit': self.unit,
            'pack': (f'{self.quantity:g} {self.unit}' if self.quantity is not None and self.unit else None),
            'czkPerKg': self.price_per_kg,
            'validFrom': self.valid_from,
            'validTo': self.valid_to,
            'url': self.source_url,
            'category': category,
            'ingredientId': self.ingredient_id,
        })
        return d


def is_egg_title(title: str) -> bool:
    return bool(_EGG_RE.search(title or ''))


def kg_amount(unit: Optional[str], quantity: Optional[float], title: str = '') -> Optional[float]:
    """Mass in kg of a pack (litres count as kg; ``ks`` only for eggs)."""
    if unit is None or not quantity or quantity <= 0:
        return None
    if unit in ('kg', 'l'):
        return float(quantity)
    if unit in ('g', 'ml'):
        return quantity / 1000.0
    if unit == 'ks' and is_egg_title(title):
        return quantity * EGG_KG_PER_PIECE
    return None


def compute_price_per_kg(price: float, unit: Optional[str], quantity: Optional[float],
                         title: str = '') -> Optional[float]:
    """CZK per kg from a pack price. Litres count as kilograms (water-like
    density; the matcher may refine with the catalogue density). Pieces are
    convertible for eggs only (10 ks -> 0.55 kg); other ``ks`` packs need the
    catalogue's ``unitGrams`` and return ``None`` here."""
    if price is None or unit is None or not quantity or quantity <= 0:
        return None
    if unit == 'kg' or unit == 'l':
        return price / quantity
    if unit == 'g' or unit == 'ml':
        return price / quantity * 1000.0
    if unit == 'ks' and is_egg_title(title):
        return price / (quantity * EGG_KG_PER_PIECE)
    return None


# ---- pack text parsing --------------------------------------------------
NBSP = chr(0xA0)
_NUM = r'(\d+(?:[,.]\d+)?)'
_PACK_RE = re.compile(
    _NUM + r'\s*(kg|g|l|ml|ks|kus[a-z]*|kusů|cl|dl|x)\b', re.IGNORECASE)
_MULTI_RE = re.compile(r'(\d+)\s*[x×]\s*' + _NUM + r'\s*(kg|g|l|ml|ks)\b', re.IGNORECASE)
_UNIT_ALIASES = {'kus': 'ks', 'kusu': 'ks', 'kusů': 'ks', 'kusy': 'ks', 'ks': 'ks', 'kg': 'kg', 'g': 'g',
                 'l': 'l', 'ml': 'ml', 'cl': 'ml', 'dl': 'ml'}


def _num(s: str) -> float:
    return float(s.replace(',', '.').replace(NBSP, '').replace(' ', ''))


def parse_pack(text: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """``"250 g"`` -> (250, 'g'); ``"0,5 l"`` -> (0.5, 'l'); ``"4 x 125 g"`` ->
    (500, 'g'); ``"10 ks"`` -> (10, 'ks'); unknown -> (None, None)."""
    if not text:
        return None, None
    t = text.replace(NBSP, ' ')
    m = _MULTI_RE.search(t)
    if m:
        n, q, u = int(m.group(1)), _num(m.group(2)), m.group(3).lower()
        return n * q, u
    m = _PACK_RE.search(t)
    if not m:
        return None, None
    q = _num(m.group(1))
    u = m.group(2).lower()
    if u == 'x':
        return None, None
    if u.startswith('kus'):
        u = 'ks'
    u = _UNIT_ALIASES.get(u, u)
    if u == 'ml' and m.group(2).lower() in ('cl', 'dl'):
        q *= 10 if m.group(2).lower() == 'cl' else 100
    return q, u


_UNIT_PRICE_RE = re.compile(
    r'(\d+(?:[,.]\d+)?)\s*Kč\s*/\s*(\d+(?:[,.]\d+)?)?\s*(kg|g|l|ml|ks|kus)\b', re.IGNORECASE)
_UNIT_PRICE_RE2 = re.compile(
    r'(\d+(?:[,.]\d+)?)\s*(kg|g|l|ml|ks|kus)\s*=\s*(?:od\s*)?(\d+(?:[,.]\d+)?)\s*Kč', re.IGNORECASE)


def parse_unit_price(text: Optional[str], title: str = '') -> Optional[float]:
    """``"15,96 Kč / 100 g"`` or ``"1 kg = 174,75 Kč"`` -> CZK per kg.
    Litres are treated as kilograms, ``ks`` is converted for eggs only."""
    if not text:
        return None
    t = text.replace(NBSP, ' ')
    m = _UNIT_PRICE_RE.search(t)
    if m:
        price, qty, unit = _num(m.group(1)), _num(m.group(2)) if m.group(2) else 1.0, m.group(3).lower()
    else:
        m = _UNIT_PRICE_RE2.search(t)
        if not m:
            return None
        qty, unit, price = _num(m.group(1)), m.group(2).lower(), _num(m.group(3))
    if unit.startswith('kus'):
        unit = 'ks'
    return compute_price_per_kg(price, unit, qty, title)


# ---- dates ------------------------------------------------------------------
def _prague_offset(utc: dt.datetime) -> dt.timedelta:
    """CET/CEST offset without tzdata (last Sunday of March/October, 01:00 UTC)."""
    y = utc.year

    def last_sunday(month: int) -> dt.datetime:
        d = dt.datetime(y, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.datetime(y, 12, 31)
        return (d - dt.timedelta(days=(d.weekday() + 1) % 7)).replace(hour=1)

    if last_sunday(3) <= utc < last_sunday(10):
        return dt.timedelta(hours=2)
    return dt.timedelta(hours=1)


def prague_date_from_epoch(ts: Optional[float]) -> Optional[str]:
    """Epoch seconds -> ISO date in Europe/Prague."""
    if ts is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.fromtimestamp(float(ts), ZoneInfo('Europe/Prague')).date().isoformat()
    except Exception:  # noqa: BLE001 - no tzdata on this machine
        utc = dt.datetime.utcfromtimestamp(float(ts))
        return (utc + _prague_offset(utc)).date().isoformat()


def iso_date(value: Optional[str]) -> Optional[str]:
    """``2026-09-08T00:00:00.000+02:00`` / ``2026-09-08`` -> ``2026-09-08``."""
    if not value:
        return None
    m = re.match(r'(\d{4}-\d{2}-\d{2})', value)
    return m.group(1) if m else None


def dmy_to_iso(day: int, month: int, year: int) -> str:
    return dt.date(year, month, day).isoformat()


def nearest_year(day: int, month: int, today: dt.date) -> str:
    """Year for a ``d. m.`` date without year: the candidate closest to today."""
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = dt.date(y, month, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best.isoformat() if best else today.isoformat()


# ---- provider base ---------------------------------------------------------
class Provider:
    """Base class: subclasses implement :meth:`fetch`."""

    name: str = ''
    stores: tuple[str, ...] = ()

    def __init__(self, http=None, today: Optional[dt.date] = None):
        from .http import Http
        self.http = http or Http(today=today)
        self.today = today or self.http.today
        self.notes: list[str] = []

    def fetch(self) -> list[Offer]:
        raise NotImplementedError

    def health(self) -> dict[str, Any]:
        """Runs :meth:`fetch` and reports ``{source, ok, count, error, ...}``."""
        return self.run()[1]

    def run(self) -> tuple[list[Offer], dict[str, Any]]:
        """Fetches once and returns ``(offers, health)``; never raises."""
        import time as _t
        from .http import Blocked, HttpError, RobotsDisallowed
        t0 = _t.monotonic()
        res: dict[str, Any] = {'source': self.name, 'ok': False, 'count': 0, 'error': None,
                               'stores': {}, 'requests': 0, 'elapsed_s': 0.0, 'notes': []}
        before = self.http.requests_made
        offers: list[Offer] = []
        try:
            offers = self.fetch()
            res['ok'] = len(offers) > 0
            res['count'] = len(offers)
            per: dict[str, int] = {}
            for o in offers:
                per[o.store] = per.get(o.store, 0) + 1
            res['stores'] = per
            if not offers:
                res['error'] = 'no offers parsed'
        except Blocked as e:
            res['error'] = f'blocked: {e}'
        except RobotsDisallowed as e:
            res['error'] = f'robots: {e}'
        except HttpError as e:
            res['error'] = f'http: {e}'
        except Exception as e:  # noqa: BLE001 - report, never crash the run
            res['error'] = f'{type(e).__name__}: {e}'
        res['requests'] = self.http.requests_made - before
        res['elapsed_s'] = round(_t.monotonic() - t0, 1)
        res['notes'] = list(self.notes)
        return offers, res
