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

from .. import markets

# stores of the default (Czech) market; other markets: ``markets.stores_of(code)``
STORES = markets.stores_of(markets.DEFAULT_MARKET)
UNITS = ('kg', 'l', 'ks', 'g', 'ml')

# Eggs are sold per piece; the app converts 10 pcs -> 0.55 kg (55 g per egg).
# cs vejce/vajíčka, sk vajcia/vajíčka, pl jaja/jajka, de Eier.
EGG_KG_PER_PIECE = 0.055
_EGG_RE = re.compile(r'(?<![a-z])(vejce|vajec|vajic|vajíčk|vajick|vajcia|vajíčok|vajc|jaja|jajk|jajec|eier|egg)',
                     re.IGNORECASE)


@dataclass
class Offer:
    store: str                       # a store of ``market`` (markets.stores_of)
    title: str                       # product name as printed by the source
    price_czk: float                 # price of one pack / one selling unit in ``currency``
                                     # (field name kept for the cz pipeline; see ``currency``)
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
    market: str = markets.DEFAULT_MARKET  # cz | sk | pl | de | at
    currency: str = ''               # ISO 4217; defaults to the market currency

    def __post_init__(self) -> None:
        m = markets.get(self.market)
        self.market = m.code
        if not self.currency:
            self.currency = m.currency
        if self.store not in m.stores:
            raise ValueError(f'unknown store {self.store!r} for market {m.code}')
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
            'perKg': self.price_per_kg,
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


# ---- multilingual pack / unit-price texts (Lidl sk/pl/de/at, ...) -----------
# Piece words: cs ks/kus, sk kus/kusy/kusov, pl szt./sztuk, de St./Stk./Stück.
_PIECE = r'ks|kus[a-zů]*|szt\.?|sztuk[a-z]*|stk\.?|st\.|stück|stueck|stuck'
_ANY_UNIT = r'(kg|g|l|ml|cl|dl|' + _PIECE + r')'
# Currency words that may follow a price: Kč, €, zł, EUR, PLN, CZK.
_CURRENCY = r'(?:kč|kc|czk|€|eur|zł|zl|pln)?'
# "1 kg = 174,75 Kč", "100 g = 0,16", "1 L = 13,32", "1 Stk. = 0.22", "1 szt. = 2,33"
_UNIT_EQ_PRICE_RE = re.compile(
    r'(\d+(?:[,.]\d+)?)\s*' + _ANY_UNIT + r'\s*=\s*(?:od\s*|ab\s*)?(\d+(?:[,.]\d+)?)(?![\d,.])\s*' + _CURRENCY
    + r'(?!\s*(?:kg|g|l|ml|cl|dl)(?![a-z]))',   # "5 x 50 g = 250 g" is a pack, not a price
    re.IGNORECASE)
# "125/150 g" (two pack sizes) -> the first one
_ALT_PACK_RE = re.compile(r'(\d+(?:[,.]\d+)?)/(\d+(?:[,.]\d+)?)(?:/\d+(?:[,.]\d+)?)*\s*(?=' + _ANY_UNIT + r'(?![a-z]))',
                          re.IGNORECASE)
# "15,96 Kč / 100 g", "2,49/100 g", "5,49/szt.", "0,33 €/l"
# The price needs decimals or a currency word so that "125/150 g" (two pack
# sizes) is not read as 125 per 150 g.
_PRICE_PER_UNIT_RE = re.compile(
    r'(?:(\d+[,.]\d+)\s*' + _CURRENCY + r'|(\d+)\s*' + _CURRENCY.rstrip('?') + r')'
    r'\s*/\s*(\d+(?:[,.]\d+)?)?\s*' + _ANY_UNIT + r'(?![a-z])',
    re.IGNORECASE)
_MULTI_ANY_RE = re.compile(r'(\d+)\s*[x×]\s*(\d+(?:[,.]\d+)?)\s*(kg|g|l|ml|cl|dl)(?![a-z])', re.IGNORECASE)
_PACK_ANY_RE = re.compile(r'(?<![\d.,])(\d+(?:[,.]\d+)?)\s*' + _ANY_UNIT + r'(?![a-z])', re.IGNORECASE)
# Everything after these markers describes a *previous* price / a limit / a
# deposit, not the current pack: "* cena przed obniżką: 1 kg = 14,99",
# "+ kaucja 1,00 zł", "Limit: 5 kg", "statt 2,49", "Najniższa cena z 30 dni".
# "je Stk." / "je kus" / "za kus" -> the price is per piece; "je kg" / "cena za
# kg" / "za 1 kg" -> per kilogram; a bare "kus" / "kg" means the same.
_PER_PIECE_RE = re.compile(r'(?:\b(?:je|pro|za|per|cena za)\s+|^\s*)(?:1\s*)?(?:' + _PIECE + r')\s*$', re.IGNORECASE)
_PER_UNIT_RE = re.compile(r'(?:\b(?:je|pro|za|per|cena za)\s+|^\s*)(?:1\s*)?(kg|l)\s*$', re.IGNORECASE)
_DISCLAIMER_RE = re.compile(
    r'(\*|\bcena przed\b|\bnajni[żz]sza cena\b|\blimit:|\+\s*kaucja|\bstatt\b|\bpůvodn[íi]\b|\bp[ôo]vodn[áa]\b'
    r'|\bcena p[řr]ed\b|\bcena pred\b|\bUVP\b|\bbisher\b)',
    re.IGNORECASE)


def strip_disclaimers(text: Optional[str]) -> str:
    """Cut a unit-price / packaging text at the first old-price disclaimer."""
    if not text:
        return ''
    t = text.replace(NBSP, ' ')
    m = _DISCLAIMER_RE.search(t)
    return t[:m.start()] if m else t


def normalise_unit(u: str) -> str:
    u = u.lower().rstrip('.')
    if u in ('kg', 'g', 'l', 'ml', 'cl', 'dl'):
        return u
    return 'ks'   # ks, kus*, szt*, stk, st, stück


def parse_unit_price_any(text: Optional[str], title: str = '') -> Optional[float]:
    """Unit price per kg from a text in any supported locale; decimal comma
    or point, optional currency word: ``"1 kg = 174,75 Kč"``, ``"100 g = 0,16"``,
    ``"1 L = 13,32"``, ``"Je 250 g (1 kg = 13.96)"``, ``"2,49/100 g"``,
    ``"1 Stk. = 0.22"`` (eggs only for pieces). The first unit-price expression
    before any old-price disclaimer wins."""
    t = strip_disclaimers(text)
    if not t:
        return None
    m = _UNIT_EQ_PRICE_RE.search(t)
    if m:
        qty, unit, price = _num(m.group(1)), normalise_unit(m.group(2)), _num(m.group(3))
    else:
        m = _PRICE_PER_UNIT_RE.search(t)
        if not m:
            return None
        price = _num(m.group(1) or m.group(2))
        qty, unit = _num(m.group(3)) if m.group(3) else 1.0, normalise_unit(m.group(4))
    if unit == 'cl':
        qty, unit = qty * 10, 'ml'
    elif unit == 'dl':
        qty, unit = qty * 100, 'ml'
    return compute_price_per_kg(price, unit, qty, title)


def parse_pack_any(text: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """Pack size from a packaging / unit-price text in any supported locale.
    Unit-price expressions (``1 kg = 2,64``) are removed first, then the first
    multipack (``2 x 1 L``, ``5 x 50 g``), else the first weight/volume
    (``Je 250 g``, ``500 g balenie``, ``750 ml``), else the first piece count
    (``3 szt.``, ``12 Stück``, ``3 kusy``). Returns (quantity, unit) with unit in
    kg/g/l/ml/ks."""
    t = strip_disclaimers(text)
    if not t:
        return None, None
    t = _UNIT_EQ_PRICE_RE.sub(' ', t)
    t = _PRICE_PER_UNIT_RE.sub(' ', t)
    t = _ALT_PACK_RE.sub(r'\1 ', t)
    m = _MULTI_ANY_RE.search(t)
    if m:
        n, q, u = int(m.group(1)), _num(m.group(2)), m.group(3).lower()
        if u == 'cl':
            q, u = q * 10, 'ml'
        elif u == 'dl':
            q, u = q * 100, 'ml'
        return n * q, u
    m = _PER_UNIT_RE.search(t)
    if m:
        return 1.0, m.group(1).lower()          # "Je kg", "cena za kg", "kg"
    if _PER_PIECE_RE.search(t):
        return 1.0, 'ks'                         # "Bei 3 Stk. je Stück", "kus", "za kus"
    piece: tuple[Optional[float], Optional[str]] = (None, None)
    for m in _PACK_ANY_RE.finditer(t):
        q, u = _num(m.group(1)), normalise_unit(m.group(2))
        if u in ('kg', 'g', 'l', 'ml'):
            return q, u
        if u == 'cl':
            return q * 10, 'ml'
        if u == 'dl':
            return q * 100, 'ml'
        if piece == (None, None):
            piece = (q, 'ks')
    return piece


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
