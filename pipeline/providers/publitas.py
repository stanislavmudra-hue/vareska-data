"""Best-effort parser for Publitas leaflet page text (Albert, Billa).

``spreads.json`` gives the raw text layer of every page: product titles,
pack bullets, unit prices and the promo price split into separate tokens,
in an order that only loosely follows the layout. We therefore:

1. classify every line (title / pack / unit price / price / old price /
   discount % / noise),
2. open a product at each title, and
3. assign the k-th pack / unit-price / price line on a page to the k-th open
   product that still lacks that attribute (leaflets print rows of products
   attribute by attribute, so this round-robin is usually right),
4. cross-check ``price ~ unit_price x pack`` and keep the unit price when the
   assigned price disagrees.

Every offer carries ``raw['confidence']`` = ``high`` (cross-checked),
``medium`` (from the unit-price line only) or ``low`` (assigned price token
without a check). Downstream code should prefer structured sources.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .base import Offer, compute_price_per_kg, kg_amount, parse_pack

_UNIT_WORD = r'(kg|g|l|ml|ks|kus|kusů|kusy)'
_PRICE_TOKEN = r'(\d{1,4}(?:[,.]\d\d)?)'
UNIT_PRICE_RE = re.compile(
    r'(\d+(?:[,.]\d+)?)\s*' + _UNIT_WORD + r'\s*=\s*(?:od\s*)?' + _PRICE_TOKEN + r'\s*Kč'
    r'(?:\s*(bez Aplikace|s Klubem)\s*/?\s*(?:od\s*)?' + _PRICE_TOKEN + r'\s*Kč\s*(?:Aplikace|bez Klubu)?)?',
    re.IGNORECASE)
PACK_RE = re.compile(
    r'^(?:•\s*)?(?:balení,?\s*|cena za\s*|od\s*)?(\d+(?:[,.]\d+)?(?:\s*[–-]\s*\d+(?:[,.]\d+)?)?)\s*' + _UNIT_WORD +
    r'\b\.?$', re.IGNORECASE)
MULTI_PACK_RE = re.compile(r'^(?:•\s*)?(\d+)\s*[x×]\s*(\d+(?:[,.]\d+)?)\s*' + _UNIT_WORD + r'\b', re.IGNORECASE)
PRICE_RE = re.compile(r'^(\d{1,4}),(\d\d)$')          # 129,90
PRICE_DASH_RE = re.compile(r'^(\d{1,4}),-$')          # 159,-
OLD_PRICE_RE = re.compile(r'^(\d{1,4})(?:,(\d\d)|,-)?/$')  # 199,90/  159,-/
BULLET_PRICE_RE = re.compile(r'^•\s*(\d{1,4}),(\d\d)\s*Kč$')  # • 9,90 Kč (Albert app price)
COMPACT_RE = re.compile(r'^(\d{3,5})$')               # 3490 -> 34,90
SPLIT_HI_RE = re.compile(r'^(\d{1,4})$')
SPLIT_LO_RE = re.compile(r'^(\d\d)$')
DISCOUNT_RE = re.compile(r'^[–-]\s*(\d{1,2})\s*%$')
NOISE_PREFIXES = ('běžná cena', 'nepora', 'nelze', 'více informací', '▼', 'cena za 1 ks', 'platí',
                  'při koupi', 'záloha', 'původ', 'pouze', 'obsah', 'nejnižší', 'nabídka', 'ilustrační',
                  'chcete', 'do vyprodání', 'více druhů', 'vybrané druhy', 'vhodné', 'ideální', 'tip')
NOISE_WORDS = {'neporazitelné', 'novinka', 'naše', 'cena', 'ceny', 'super', 'zralé', 'billa', 'albert',
               'akce', 'sleva', 'tip', 'bio', 'nové', 'nový', 'nová', 'masa', 'zdarma', 'plus', 'klub'}
MAX_TITLE_LINES = 3
PRICE_TOLERANCE = 0.06


@dataclass
class Product:
    title_lines: list[str] = field(default_factory=list)
    pack: Optional[tuple[float, str]] = None
    pack_text: Optional[str] = None
    per_kg: Optional[float] = None
    per_kg_club: Optional[float] = None
    unit_text: Optional[str] = None
    price: Optional[float] = None
    price_club: Optional[float] = None
    old_price: Optional[float] = None
    discount_pct: Optional[int] = None
    club_first: bool = False  # Billa: "s Klubem/ ... bez Klubu" -> first price is the club price

    @property
    def title(self) -> str:
        return ' '.join(' '.join(self.title_lines).split())


def _num(s: str) -> float:
    return float(s.replace(',', '.'))


def _is_title_line(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 3:
        return False
    if any(c.isdigit() for c in line) and ('Kč' in line or '%' in line or re.search(r'\d,(\d\d|-)', line)):
        return False
    low = line.lower()
    if any(low.startswith(p) for p in NOISE_PREFIXES):
        return False
    if line.startswith('•'):
        return False
    words = [w for w in re.split(r'\s+', low) if w]
    if all(w in NOISE_WORDS for w in words):
        return False
    upper = sum(1 for c in letters if c.isupper())
    if upper / len(letters) > 0.8 and len(letters) > 4:
        return False  # decorative ALL-CAPS slogans (also letter-spaced fragments)
    if len(letters) / max(len(line), 1) < 0.5:
        return False
    return True


def _join_continuations(lines: list[str]) -> list[str]:
    """Re-attach fragments Publitas splits off: lines starting with '=',
    the 'bez Aplikace / 13,17 Kč Aplikace' tail, 's Klubem/ 44,92 Kč bez Klubu'."""
    out: list[str] = []
    for ln in lines:
        low = ln.lower()
        cont = (ln.startswith('=') or low.startswith(('bez aplikace', 'aplikace', 's klubem', 'bez klubu'))
                or re.match(r'^(od\s*)?\d+,\d\d\s*Kč\s*(aplikace|bez klubu)', low)
                or (out and out[-1].rstrip().endswith(('=', '/', 'bez', 'Kč bez', 'od')) and re.match(r'^\d', ln)))
        if cont and out:
            out[-1] = out[-1] + ' ' + ln
        else:
            out.append(ln)
    return out


def _split_bullets(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        if '•' in ln[1:]:
            parts = [p.strip() for p in ln.split('•')]
            out.extend(('• ' + p) if i > 0 else p for i, p in enumerate(parts) if p)
        else:
            out.append(ln)
    return out


def _assign(products: list[Product], attr: str, value: Any) -> bool:
    for p in products:
        if getattr(p, attr) is None:
            setattr(p, attr, value)
            return True
    return False


def _bad_title(title: str) -> bool:
    words = title.split()
    if not words or title[0].isdigit():
        return True
    if max(len(w) for w in words) < 4:
        return True
    singles = sum(1 for w in words if len(w) == 1)
    if singles >= 3 and singles > len(words) / 2:
        return True  # letter-spaced decorative text ("V ý ra z m a")
    low = title.lower()
    return low in ('více druhů', 'druhy', 'vybrané druhy') or low.startswith(('více druh', 'od ', 'z pultu'))


def parse_page_text(text: str, chain: str) -> list[Product]:
    """Group the page's lines into products (see module doc).

    Consecutive title lines form a *row group*; attribute lines that follow
    are assigned round-robin inside that group only. A title appearing after
    attribute lines closes the group and opens a new one."""
    raw_lines = [' '.join(l.replace(chr(0xA0), ' ').split()) for l in text.splitlines()]
    lines = _split_bullets(_join_continuations([l for l in raw_lines if l]))
    products: list[Product] = []
    group: list[Product] = []
    current: Optional[Product] = None
    last_was_title = False
    pending_hi: Optional[str] = None
    for ln in lines:
        m = UNIT_PRICE_RE.search(ln)
        if m:
            qty, unit, price = _num(m.group(1)), m.group(2).lower(), _num(m.group(3))
            if unit.startswith('kus'):
                unit = 'ks'
            target = next((p for p in group if p.per_kg is None and p.per_kg_club is None), None)
            if target is not None:
                per_kg = compute_price_per_kg(price, unit, qty, target.title)
                tag, second = (m.group(4) or '').lower(), m.group(5)
                per_kg2 = compute_price_per_kg(_num(second), unit, qty, target.title) if second else None
                target.unit_text = m.group(0)
                if tag.startswith('s klubem'):
                    target.per_kg_club, target.per_kg, target.club_first = per_kg, per_kg2, True
                else:
                    target.per_kg, target.per_kg_club = per_kg, per_kg2
            last_was_title = False
            pending_hi = None
            continue
        mm = MULTI_PACK_RE.match(ln)
        if mm:
            qty = int(mm.group(1)) * _num(mm.group(2))
            _assign_pack(group, (qty, mm.group(3).lower()), ln)
            last_was_title = False
            continue
        mp = PACK_RE.match(ln)
        if mp:
            q, u = parse_pack(mp.group(1).split('–')[0].split('-')[0] + ' ' + mp.group(2))
            if q is not None:
                _assign_pack(group, (q, u), ln)
            last_was_title = False
            pending_hi = None
            continue
        md = DISCOUNT_RE.match(ln)
        if md:
            _assign(group, 'discount_pct', int(md.group(1)))
            last_was_title = False
            pending_hi = None
            continue
        mo = OLD_PRICE_RE.match(ln)
        if mo:
            _assign(group, 'old_price', _num(mo.group(1) + ',' + (mo.group(2) or '00')))
            last_was_title = False
            pending_hi = None
            continue
        mb = BULLET_PRICE_RE.match(ln)
        if mb:
            # Albert: "• 9,90 Kč" inside the details block = price with the app
            _assign(group, 'price_club', _num(mb.group(1) + '.' + mb.group(2)))
            last_was_title = False
            continue
        mpr = PRICE_RE.match(ln) or PRICE_DASH_RE.match(ln)
        if mpr:
            cents = mpr.group(2) if mpr.lastindex and mpr.lastindex >= 2 else '00'
            _assign(group, 'price', _num(mpr.group(1) + ',' + cents))
            last_was_title = False
            pending_hi = None
            continue
        if pending_hi is not None and SPLIT_LO_RE.match(ln):
            _assign(group, 'price', _num(pending_hi + '.' + ln))
            pending_hi = None
            last_was_title = False
            continue
        if SPLIT_HI_RE.match(ln):
            if COMPACT_RE.match(ln):
                # "3490" -> 34,90 (Albert prints the non-app price without separator)
                _assign(group, 'price', _num(ln[:-2] + '.' + ln[-2:]))
                pending_hi = None
            else:
                pending_hi = ln
            last_was_title = False
            continue
        pending_hi = None
        if _is_title_line(ln):
            starts_upper = ln[0].isupper()
            if current is not None and last_was_title and len(current.title_lines) < MAX_TITLE_LINES and (
                    not starts_upper or (len(current.title_lines) == 1 and ' ' not in current.title_lines[0])):
                current.title_lines.append(ln)
            else:
                current = Product(title_lines=[ln])
                products.append(current)
                if last_was_title:
                    group.append(current)
                else:
                    group = [current]
            last_was_title = True
            continue
        last_was_title = False
    return [p for p in products if not _bad_title(p.title)]


def _assign_pack(products: list[Product], pack: tuple[float, str], text: str) -> None:
    for p in products:
        if p.pack is None:
            p.pack, p.pack_text = pack, text
            return


def _close(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and a > 0 and abs(a - b) / a <= PRICE_TOLERANCE


def products_to_offers(products: list[Product], store: str, valid_from: Optional[str], valid_to: Optional[str],
                       source_url: str, page: int, source: str) -> list[Offer]:
    offers: list[Offer] = []
    for p in products:
        title = p.title
        if len(title) < 4 or (p.pack is None and p.per_kg is None and p.per_kg_club is None):
            continue
        qty, unit = p.pack if p.pack else (None, None)
        kg = kg_amount(unit, qty, title)
        per_kg_pack = p.price / kg if (p.price is not None and kg) else None
        confidence = 'low'
        price = p.price
        per_kg = p.per_kg
        if per_kg is not None:
            if per_kg_pack is not None and _close(per_kg, per_kg_pack):
                confidence = 'high'
            elif per_kg_pack is not None and p.per_kg_club is not None and _close(p.per_kg_club, per_kg_pack):
                # the assigned price is the club price; the plain one follows from the unit price
                p.price_club, price, confidence = p.price, None, 'medium'
            else:
                confidence = 'medium'
            if price is None or (per_kg_pack is not None and not _close(per_kg, per_kg_pack)):
                price = round(per_kg * kg, 2) if kg else None
        else:
            per_kg = per_kg_pack
            if per_kg is None or price is None:
                if p.per_kg_club is None:
                    continue
            elif p.old_price and p.discount_pct and _close(p.old_price * (1 - p.discount_pct / 100.0), price):
                confidence = 'medium'
        if per_kg is not None and not (5 <= per_kg <= 5000):
            continue
        if p.pack is None:
            confidence = 'low'
        raw = {'page': page, 'confidence': confidence, 'pack_text': p.pack_text, 'unit_text': p.unit_text,
               'discount_pct': p.discount_pct, 'assigned_price': p.price}
        base = dict(store=store, title=title, valid_from=valid_from, valid_to=valid_to,
                    source_url=source_url, source=source, original_price_czk=p.old_price)
        if per_kg is not None:
            if price is not None:
                offers.append(Offer(price_czk=price, unit=unit, quantity=qty, price_per_kg=per_kg, raw=raw, **base))
            else:  # per-kg quote without a known pack mass (e.g. "1 ks")
                offers.append(Offer(price_czk=round(per_kg, 2), unit='kg', quantity=1.0, price_per_kg=per_kg,
                                    raw={**raw, 'pack_text': p.pack_text, 'per_kg_quote': True}, **base))
        club_per_kg = p.per_kg_club
        club_price = p.price_club
        if club_per_kg is None and club_price is not None and kg:
            club_per_kg = club_price / kg
        if club_per_kg is not None and (per_kg is None or club_per_kg < per_kg) and 5 <= club_per_kg <= 5000:
            if club_price is None and kg:
                club_price = round(club_per_kg * kg, 2)
            if club_price is not None:
                offers.append(Offer(price_czk=club_price, unit=unit, quantity=qty, price_per_kg=club_per_kg,
                                    club=True, raw={**raw, 'club': True}, **base))
            else:
                offers.append(Offer(price_czk=round(club_per_kg, 2), unit='kg', quantity=1.0, price_per_kg=club_per_kg,
                                    club=True, raw={**raw, 'club': True, 'per_kg_quote': True}, **base))
    return offers


def parse_spreads(spreads: list[dict[str, Any]], store: str, valid_from: Optional[str], valid_to: Optional[str],
                  source_url: str, source: str) -> list[Offer]:
    offers: list[Offer] = []
    for spread in spreads:
        for page in spread.get('pages') or []:
            text = page.get('text') or ''
            if not text.strip():
                continue
            products = parse_page_text(text, store)
            offers.extend(products_to_offers(products, store, valid_from, valid_to, source_url,
                                             page.get('number') or 0, source))
    return offers


def fetch_spreads(http, base_url: str) -> list[dict[str, Any]]:
    """All spreads of one Publitas publication (follows ``X-Next-Page``)."""
    spreads: list[dict[str, Any]] = []
    page = 1
    while page and page < 20:
        r = http.get(f'{base_url.rstrip("/")}/spreads.json?page={page}')
        data = r.json()
        if not isinstance(data, list) or not data:
            break
        spreads.extend(data)
        nxt = r.headers.get('x-next-page')
        page = int(nxt) if nxt and nxt.isdigit() else 0
    return spreads
