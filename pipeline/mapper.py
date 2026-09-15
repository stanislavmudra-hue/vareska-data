"""Offer -> ingredient mapper for the Vareska price pipeline.

Reads the raw offers produced by the fetchers (``out/offers.json``), maps every
offer title onto an ingredient id of the app catalog (``catalog/ingredients.json``,
a verbatim copy of the app's ``assets/data/ingredients.json`` - see
``catalog/README.md``), converts the pack price into CZK per kilogram and writes

* ``out/matched.json``   - offers accepted automatically (confidence >= 0.85) or by a
                           learned rule from ``data/mappings.json``;
* ``out/unmatched.json`` - offers that need a human (``review``, 0.60-0.85) or that
                           nothing matched (``unmatched``, < 0.60), each with the top-3
                           suggestions, plus offers ignored by rule / as non-food.

Text normalisation (``fold``, ``tokens``, ``stem``, ``normalize_tokens``,
``token_matches``) mirrors ``lib/logic/text_normalizer.dart`` of the Flutter app
one-to-one so both sides agree on what "the same word" is.

Input contract (``offers.json``)
--------------------------------
Either a JSON list or an object with an ``offers`` list (what
``pipeline/providers/fetch.py`` writes). Each offer is an object; both the
camelCase names below and the snake_case names of ``providers.base.Offer``
(``price_czk``, ``unit`` + ``quantity``, ``price_per_kg``, ``valid_from``,
``valid_to``, ``source_url``, ``original_price_czk``, ``is_promo``, ``club``,
``ingredient_id``, ``raw``) are accepted; the first present alias wins:

``store``        albert | lidl | kaufland | tesco | billa | penny | globus
                 (``shop`` accepted; "Penny Market", "Albert hypermarket" are normalised)
``title``        product title as printed (``name`` accepted)
``price``        promo price of the pack in CZK, number or "39,90 Kč" (``price_czk``)
``originalPrice`` optional (``oldPrice``, ``original_price_czk``)
``pack``         optional pack text: "250 g", "0,5 l", "10 ks", "2x100 g", "1 kg"
                 (``packText``; or ``quantity`` + ``unit`` as number + unit code)
``unitPriceText`` optional: "15,96 Kč / 100 g", "1 kg = 43 Kč", "cena za 100 g"
                 (Lidl ``raw.basePrice`` is read too)
``unitPrice``    optional structured: {"czk": 139.8, "per": "kg"|"l"|"ks"|"100g"|"100ml"}
``price_per_kg`` / ``czkPerKg``  optional CZK/kg computed by the fetcher - a *fallback*
                 only (litres ~ kg there): the mapper recomputes from the pack + catalog
                 density / ``unitGrams`` (eggs 55 g) whenever it can
``validFrom`` / ``validTo``  ISO dates (passed through)
``url``          provenance link (``source_url``)
``source``       provider name, e.g. "globus", "kupi" (passed through)
``promo`` / ``club``  booleans (``is_promo``), passed through
``category``     optional category hint from the source (Globus ``productCategories``,
                 Lidl ``wonCategoryPrimary``, Penny ``category``; string or list;
                 ``raw.categories`` / ``raw.category`` / ``raw.wonCategory`` are read too)
``ingredient_id`` optional prior from term-driven providers (kupi searched for this
                 id); accepted when the title scorer agrees (>= REVIEW_MIN), else the
                 scorer's own verdict stands and ``term-mismatch`` is flagged

Output (``matched.json`` -> ``items[]``, consumed by ``pipeline/build_prices.py``)
--------------------------------------------------------------------------------
``ingredientId, store, source, title, czkPerKg, price, originalPrice, promo, validFrom,
validTo, status, club, url, pack, confidence, method, priceMethod, flags, reasons,
suggestions[{ingredientId, score, nameCs}]`` - ``raw`` is dropped.

CLI
---
    python -m pipeline.mapper --offers out/offers.json [--out-dir out]
    python -m pipeline.mapper --explain "Madeta Jihočeské máslo 82% 250 g"
    python -m pipeline.mapper --sync-catalog [path/to/app/assets/data/ingredients.json]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_PATH = os.path.join(REPO_ROOT, "catalog", "ingredients.json")
MAPPINGS_PATH = os.path.join(REPO_ROOT, "data", "mappings.json")
KUPI_TERMS_PATH = os.path.join(REPO_ROOT, "docs", "kupi_terms.json")
DEFAULT_APP_CATALOG = os.path.join("C:", os.sep, "AI", "Jídlo", "assets", "data", "ingredients.json")

STORES = ("albert", "lidl", "kaufland", "tesco", "billa", "penny", "globus")

AUTO_ACCEPT = 0.85
REVIEW_MIN = 0.60
AMBIGUITY_MARGIN = 0.03   # two different ids closer than this at the top -> ×0.85
HEAD_FACTOR = 0.93        # variant that does not cover the first title word
GENERIC_CAP = 0.80        # brand + a category word only ("Leerdammer ... sýr")
TERM_MARGIN = 0.10        # kupi term prior wins unless another id beats it by more than this
PRICE_RATIO = (0.2, 4.0)  # czkPerKg / refPriceCzkPerKg outside this band -> review
SOLID_CATEGORIES = frozenset("vegetable fruit meat poultry fish seafood grain pasta bakery legume nut spice herb egg".split())

# --------------------------------------------------------------------------- #
# Text normalisation - mirror of lib/logic/text_normalizer.dart
# --------------------------------------------------------------------------- #

FOLD_MAP: dict[str, str] = {
    # a
    "á": "a", "ä": "a", "â": "a", "à": "a", "ã": "a", "å": "a", "ā": "a",
    "ă": "a", "ą": "a", "ǎ": "a", "ả": "a", "ạ": "a",
    "ắ": "a", "ằ": "a", "ẳ": "a", "ẵ": "a", "ặ": "a",
    "ấ": "a", "ầ": "a", "ẩ": "a", "ẫ": "a", "ậ": "a",
    "æ": "a",
    # c
    "č": "c", "ç": "c", "ć": "c", "ĉ": "c", "ċ": "c",
    # d
    "ď": "d", "đ": "d", "ð": "d",
    # e
    "é": "e", "ě": "e", "ë": "e", "è": "e", "ê": "e", "ē": "e", "ę": "e",
    "ė": "e", "ĕ": "e", "ẻ": "e", "ẽ": "e", "ẹ": "e",
    "ế": "e", "ề": "e", "ể": "e", "ễ": "e", "ệ": "e",
    # g
    "ğ": "g", "ģ": "g", "ġ": "g",
    # h
    "ħ": "h",
    # i
    "í": "i", "ï": "i", "ì": "i", "î": "i", "ī": "i", "ı": "i", "į": "i",
    "ǐ": "i", "ỉ": "i", "ĩ": "i", "ị": "i",
    # k, l
    "ķ": "k",
    "ľ": "l", "ĺ": "l", "ł": "l", "ļ": "l",
    # n
    "ň": "n", "ñ": "n", "ń": "n", "ņ": "n",
    # o
    "ó": "o", "ö": "o", "ò": "o", "ô": "o", "õ": "o", "ø": "o", "ō": "o",
    "ő": "o", "ơ": "o", "ǒ": "o", "ỏ": "o", "ọ": "o",
    "ố": "o", "ồ": "o", "ổ": "o", "ỗ": "o", "ộ": "o",
    "ớ": "o", "ờ": "o", "ở": "o", "ỡ": "o", "ợ": "o",
    "œ": "oe",
    # r
    "ř": "r", "ŕ": "r",
    # s
    "š": "s", "ś": "s", "ş": "s", "ș": "s", "ŝ": "s",
    # t
    "ť": "t", "þ": "t", "ţ": "t", "ț": "t",
    # u
    "ú": "u", "ů": "u", "ü": "u", "ù": "u", "û": "u", "ū": "u", "ű": "u",
    "ư": "u", "ų": "u", "ǔ": "u", "ǚ": "u", "ủ": "u", "ũ": "u", "ụ": "u",
    "ứ": "u", "ừ": "u", "ử": "u", "ữ": "u", "ự": "u",
    # y
    "ý": "y", "ÿ": "y", "ỳ": "y", "ỷ": "y", "ỹ": "y", "ỵ": "y",
    # z
    "ž": "z", "ż": "z", "ź": "z",
    "ß": "ss",
}

SUFFIXES: tuple[str, ...] = (
    "ami", "emi", "ich", "ech", "ach", "ata", "ete",
    "um", "am", "ou", "em", "im", "ym",
    "e", "i", "u", "y", "a", "o",
)


def fold(s: str) -> str:
    """Lowercase, strip diacritics via the explicit map, turn every other
    non-[a-z0-9] character into a space, collapse runs, trim (Dart ``fold``)."""
    lower = s.lower()
    out: list[str] = []
    last_space = True
    for ch in lower:
        o = ord(ch)
        if (0x61 <= o <= 0x7A) or (0x30 <= o <= 0x39):
            out.append(ch)
            last_space = False
            continue
        mapped = FOLD_MAP.get(ch) if o >= 0x80 else None
        if mapped is not None:
            out.append(mapped)
            last_space = False
        else:
            if not last_space:
                out.append(" ")
            last_space = True
    r = "".join(out)
    return r[:-1] if r.endswith(" ") else r


def tokens(s: str) -> list[str]:
    """Split a folded string on spaces and drop tokens shorter than 2."""
    return [t for t in s.split(" ") if len(t) >= 2]


def stem(t: str) -> str:
    """Czech suffix stemmer (folded input). Tokens of 5+ chars lose the first
    matching suffix (longest first) but never go below 4 characters."""
    if len(t) < 5:
        return t
    for suf in SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: len(t) - len(suf)]
    return t


def normalize_tokens(s: str) -> list[str]:
    """fold -> tokens -> stem."""
    return [stem(t) for t in tokens(fold(s))]


def token_matches(query: str, indexed: str) -> bool:
    """A query token matches an index token when either is a prefix of the
    other and the shorter one has at least 3 characters."""
    if not query or not indexed:
        return False
    shorter = query if len(query) <= len(indexed) else indexed
    if len(shorter) < 3:
        return query == indexed
    return indexed.startswith(query) or query.startswith(indexed)


# --------------------------------------------------------------------------- #
# Noise: brands, filler words, phrases
# --------------------------------------------------------------------------- #

# Brand / producer / private-label names seen in Czech leaflets. Folded; matched
# as whole words (multi-word brands as phrases) before tokenisation.
BRANDS: tuple[str, ...] = (
    # chains and private labels
    "globus", "albert", "albert excellent", "albert quality", "nature s promise", "lidl",
    "kaufland", "k classic", "k bio", "k favourites", "k purland", "k take it veggie",
    "tesco", "tesco finest", "tesco value", "billa", "clever", "ja", "chef menu",
    "penny", "penny market", "karlova koruna", "boni", "reznikuv talir", "reznikuv",
    "pilos", "milbona", "chef select", "vemondo", "dulano", "baker street", "deluxe",
    "freshona", "solevita", "combino", "italiamo", "bio organic", "crownfield", "cien",
    "argus", "kania", "belbake", "grand duca", "duc de coeur", "parkside", "silvercrest",
    "premium", "select",
    # dairy
    "madeta", "olma", "kunin", "meggle", "laktos", "agro la", "jaromericka mlekarna",
    "jaromericke", "hollandia", "danone", "zott", "muller", "mullermilch", "milkpol",
    "bohemilk", "tatra", "tami", "zvolensky", "alpro", "lucina", "pribina", "apetito",
    "sedlcansky", "kral syru", "milko", "moravia", "choceňska", "chocenska", "krajanka",
    "bohusovicka", "polabske", "rajo", "mlekarna kunin", "philadelphia", "leerdammer",
    "president", "almette", "gervais", "activia", "actimel", "jogobella", "mila",
    "florian", "opavia", "pilos tradicni", "tradicni",     # meat
    "rabbit", "vodnanske", "vodnanska", "stepanovske", "drubezarsky zavod klatovy",
    "kmotr", "chodura", "le co", "krahulik", "kostelecke uzeniny", "kostelecke",
    "schneider", "vaclav", "steinhauser", "kunert", "hodonin", "masokombinat",
    "sedlacka", "sedlacke", "pikok", "purland", "vocilka",     # grocery
    "barilla", "panzani", "de cecco", "vitana", "hame", "znojmia", "bonduelle", "mutti",
    "emco", "penam", "odkolek", "kraslice", "hellmann s", "hellmanns", "spak", "heinz",
    "calvo", "rio mare", "giana", "franz josef", "kaiser franz josef", "lagris",
    "bask", "babiccina volba", "babiccina", "penam", "orion", "nestle",
    "kotanyi", "hranacek", "brolio", "fabio", "lukana", "vegetol",
    "hera", "rama", "flora", "perla", "stella", "seliko", "bon menu", "bonduelle bon menu",
    "nesquik", "jacobs", "douwe egberts", "tchibo", "nescafe", "dolce gusto",
    "coca cola", "kofola", "mattoni", "magnesia", "rajec", "bonaqua", "ondrasovka",
    "pilsner urquell", "gambrinus", "radegast", "kozel", "budweiser", "staropramen",
    "bernard", "krusovice", "birell", "allini", "frost food", "amylon", "nowaco",
    "iglo", "dr oetker", "lotus", "milka", "sedita", "tuc", "maretti",
    "flipz", "pringles", "znojmia moravanka", "znojmia", "hamanek", "sunar",
    "top q", "ta zenska", "kbo", "billa bio", "ja bio", "biocentrum", "s budget",
    "s budget", "koh i noor", "svatava", "moravanka", "mistr", "mistr reznik",
    "srdce domova", "tuzemske", "tuzemsky", "tuzemska", "cesky", "ceska", "ceske", "ceskych", "jihoceske",
    "jihocesky", "jihoceska", "moravske", "moravsky", "moravska", "domaci", "farmarske", "farmarska", "farmarsky", "polske", "spanelske",
    "italske", "recke", "nemecke", "rakouske", "francouzske", "holandske",
    "jihoamericky", "jihoamericka", "jihoamericke",
    # private labels / brands seen in the 2026-09 live run
    "jeden tag", "vas vyber", "alnatura", "mistrovska", "mistrovsky", "mistrovske", "chocensky",
    "chocenska", "chocenske", "pribinacek", "lipanek", "bobik", "termix", "smetanito", "hochland",
    "kiri", "babybel", "galbani", "giotti", "cielo", "lipton", "starbucks", "oreo", "nowaco",
    "seliko", "giana", "semix", "racio", "snack day", "kinder", "royal crown", "corona",
    "san fabio", "jihoceska", "lucasova", "nautic", "mecom", "gombasecka", "kania", "lagris",
    "ristorante", "platan", "dr oetker", "hame", "hamanek", "vitana", "maggi", "knorr", "podravka",
    "bonduelle", "giana", "franz josef", "kaiser", "sedlcansky", "sedlcanska", "sedlcanske",
    "tatra", "olma", "olomoucky", "olomoucke", "kunin", "kuninska", "valasska", "valassky",
    "z valasska", "madeta", "jihoceske", "krajanka", "milkpol", "laktos", "meggle",
)

# Filler words (folded, whole words). Symmetric: stripped from titles AND from
# catalog names so "čerstvý špenát" still matches "špenát".
NOISE_WORDS: frozenset[str] = frozenset(
    """
    akce sleva slevy cena ceny kc czk ks kus kusy kusu bal baleni balene balena baleny
    balen volne volny volna volnych vazene vazeny vazena vazenych chlazene chlazeny
    chlazena chlazenych chlaz mrazene mrazeny mrazena mrazenych mraz cerstve cerstvy
    cerstva cerstvych cerstveho trvanlive trvanlivy trvanliva bio eko organic premium
    original originalni classic klasik klasicke klasicky klasicka xxl xl xxxl mix ruzne
    druhy vybrane vybranych dle vyberu nabidka nabidce pouze jen nyni vice druhu zdarma
    top jemne jemny jemna jemneho vyberove vyberova vyberovy standard standardni konzumni
    pozdni rane prane cistene varny typ velikost vel velke velky velka male maly mala
    stredni jednotlive jednotlivy zivy ziva zive
    kg g ml cl dkg gr grams lt ltr
    a i s se z ze v ve na za od do pro nebo ci bez k ke o u po pri
    sklo plech plechovka plechovce lahev lahvi pet karton krabice sacek sacku kelimek
    kelimku vanicka vanicce tuba tuby
    cca min max approx
    nejvyssi jakosti jakost kvalita kvality extra fine super hyper mega maxi mini family
    rodinne rodinny multipack twinpack duopack pack
    jedly jedla jedle hluboce zmrazene zmrazeny zmrazena
    svetly svetla svetle
    """.split()
)

# Form / cut words stripped from titles only (never from catalog names, where
# "gouda plátky" or "strouhaný chléb" carry meaning).
TITLE_NOISE_WORDS: frozenset[str] = frozenset(
    """
    porce porcovany porcovana porcovane platky platek platkovy platkova platkove
    platkoveho blok bloky
    """.split()
)

# Word-boundary phrases removed from the folded title before tokenising.
NOISE_PHRASES: tuple[str, ...] = (
    "bez kuze a kosti", "bez kosti a kuze", "bez kosti", "s kosti", "bez kuze", "s kuzi",
    "s kozi", "bez kozi", "v olivovem oleji", "ve slunecnicovem oleji", "v rostlinnem oleji",
    "v oleji", "ve vlastni stave", "ve vlastni stavě", "v nalevu", "ve slanem nalevu",
    "ve sladkokyselem nalevu", "v rajcatove omacce", "v rajcatech", "v zele", "v laku",
    "z podestylky", "podestylka", "podestylkova", "podestylkove", "z volneho vybehu",
    "volny vybeh", "volneho vybehu", "klecovy chov", "z klecoveho chovu",
    "cena za", "za kus", "za kg", "za 1 kg", "za 100 g", "za balení", "za baleni",
    "v akci", "1 1 zdarma", "2 1 zdarma", "vyhodne baleni", "rodinne baleni",
    "ruzne druhy", "vybrane druhy", "vice druhu", "dle vyberu", "v nabidce",
    "nejvyssi jakosti", "nejvyssi jakost", "1 jakost", "i jakost", "tridy i", "trida i",
    "z kamenne pece", "ve varnych saccich", "v varnych saccich", "varne sacky", "ve varnych sackach",
    "s aplikaci", "bez aplikace", "s klubem", "bez klubu",
    "s kartou", "bez karty", "s penny kartou", "s clubcard", "clubcard cena",
    "dostupne pouze ve vybranych prodejnach", "pouze ve vybranych prodejnach",
)

# --------------------------------------------------------------------------- #
# Pack / unit parsing
# --------------------------------------------------------------------------- #

_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT = r"(kg|g|dkg|dag|mg|ml|cl|dl|l|ks|kus|kusy|kusu|kusů|pcs|x)"

# "2x100 g", "2 x 100g", "6x65 g", "44 x 85g"
PACK_MULTI_RE = re.compile(
    r"(?<![\w,.])(\d+)\s*[x×]\s*" + _NUM + r"\s*(kg|g|dkg|ml|cl|dl|l)(?![a-zA-Z])",
    re.IGNORECASE,
)
# "250 g", "0,5 l", "1kg", "10 ks", "10ks", "30Ks"
PACK_RE = re.compile(
    r"(?<![\w,])(?<!\d[.,])" + _NUM + r"\s*(kg|g|dkg|dag|ml|cl|dl|l|ks|kus|kusy|kusů|kusu)(?![a-zA-Zá-ž])",
    re.IGNORECASE,
)
# "82%", "1,5 %", "3.5%"
PERCENT_RE = re.compile(r"(?<![\w,.])" + _NUM + r"\s*%")
# unit price texts: "15,96 Kč / 100 g", "1 kg = 43 Kč", "100 g = 7,93 Kč", "1 kus = 2,50 Kč"
UNIT_PRICE_A = re.compile(  # PRICE Kč / AMOUNT UNIT
    _NUM + r"\s*(?:kc|kč|czk)?\s*/\s*" + _NUM + r"?\s*(kg|g|ml|l|ks|kus)\b", re.IGNORECASE
)
UNIT_PRICE_B = re.compile(  # AMOUNT UNIT = PRICE Kč
    _NUM + r"\s*(kg|g|ml|l|ks|kus)\s*=\s*(?:od\s*)?" + _NUM + r"\s*(?:kc|kč|czk)?", re.IGNORECASE
)
PRICE_PER_100 = re.compile(r"cena\s+za\s+" + _NUM + r"\s*(kg|g|ml|l|ks|kus)", re.IGNORECASE)
PRICE_RE = re.compile(_NUM)


def _num(s: str | float | int | None) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = PRICE_RE.search(str(s).replace(" ", " "))
    return float(m.group(1).replace(",", ".")) if m else None


def _unit_norm(u: str) -> str:
    u = u.lower()
    if u in ("kus", "kusy", "kusu", "kusů", "pcs"):
        return "ks"
    if u == "dag":
        return "dkg"
    return u


def to_base(amount: float, unit: str) -> tuple[float, str] | None:
    """Convert (amount, unit) to (kg, 'kg') | (l, 'l') | (n, 'ks')."""
    unit = _unit_norm(unit)
    if unit == "kg":
        return amount, "kg"
    if unit == "g":
        return amount / 1000.0, "kg"
    if unit == "dkg":
        return amount / 100.0, "kg"
    if unit == "mg":
        return amount / 1e6, "kg"
    if unit == "l":
        return amount, "l"
    if unit == "ml":
        return amount / 1000.0, "l"
    if unit == "cl":
        return amount / 100.0, "l"
    if unit == "dl":
        return amount / 10.0, "l"
    if unit == "ks":
        return amount, "ks"
    return None


@dataclass
class Pack:
    amount: float          # in base unit
    unit: str              # kg | l | ks
    text: str              # what was parsed
    count: int = 1         # multipack count ("2x100 g" -> 2)

    def as_dict(self) -> dict[str, Any]:
        return {"amount": round(self.amount, 6), "unit": self.unit, "text": self.text, "count": self.count}


def parse_pack(text: str | None) -> Pack | None:
    """Parse "250 g" / "2x100 g" / "0,5 l" / "10 ks" -> Pack in base units.
    Multipacks are summed ("2x100 g" -> 0.2 kg)."""
    if not text:
        return None
    t = str(text).replace(" ", " ")
    m = PACK_MULTI_RE.search(t)
    if m:
        n = int(m.group(1))
        amt = float(m.group(2).replace(",", "."))
        base = to_base(amt, m.group(3))
        if base:
            return Pack(base[0] * n, base[1], m.group(0).strip(), n)
    m = PACK_RE.search(t)
    if m:
        amt = float(m.group(1).replace(",", "."))
        base = to_base(amt, m.group(2))
        if base and amt > 0:
            return Pack(base[0], base[1], m.group(0).strip())
    return None


def parse_unit_price(text: str | None) -> tuple[float, str] | None:
    """Parse a unit-price text -> (czk per base unit, base unit).

    "15,96 Kč / 100 g" -> (159.6, 'kg'); "1 kg = 43 Kč" -> (43, 'kg');
    "1 kus = 2,50 Kč" -> (2.5, 'ks'); "1 l = 99,70 Kč" -> (99.7, 'l')."""
    if not text:
        return None
    t = str(text).replace(" ", " ")
    m = UNIT_PRICE_B.search(t)
    if m:
        amt = float(m.group(1).replace(",", "."))
        price = float(m.group(3).replace(",", "."))
        base = to_base(amt, m.group(2))
        if base and base[0] > 0:
            return price / base[0], base[1]
    m = UNIT_PRICE_A.search(t)
    if m:
        price = float(m.group(1).replace(",", "."))
        amt = float((m.group(2) or "1").replace(",", "."))
        base = to_base(amt, m.group(3))
        if base and base[0] > 0:
            return price / base[0], base[1]
    return None


def parse_price_basis(text: str | None) -> Pack | None:
    """"cena za 100 g" -> the pack the price refers to."""
    if not text:
        return None
    m = PRICE_PER_100.search(str(text))
    if not m:
        return None
    amt = float(m.group(1).replace(",", "."))
    base = to_base(amt, m.group(2))
    return Pack(base[0], base[1], m.group(0)) if base else None


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

@dataclass
class Ingredient:
    id: str
    name_cs: str
    category: str
    unit_grams: dict[str, float]
    density: float | None
    ref_price: float | None
    variants: list[tuple[str, tuple[str, ...], float]] = field(default_factory=list)  # (source text, stems, weight)
    negatives: set[str] = field(default_factory=set)

    @property
    def ks_grams(self) -> float | None:
        v = self.unit_grams.get("ks")
        return float(v) if v else None


def _clean_stems(text: str) -> tuple[tuple[str, ...], bool]:
    """fold -> remove noise phrases/words -> tokens -> stems. Returns the stems
    and whether a noise *phrase* was removed (such variants are slightly
    down-weighted so "vepřová kotleta" beats "vepřová kotleta bez kosti").
    Falls back to the raw stems when everything was noise (e.g. "extra")."""
    f = fold(text)
    reduced = False
    for ph in NOISE_PHRASES:
        f2 = re.sub(r"(?<![a-z0-9])" + re.escape(ph) + r"(?![a-z0-9])", " ", f)
        if f2 != f:
            reduced = True
            f = f2
    toks = [t for t in f.split(" ") if len(t) >= 2 and t not in NOISE_WORDS and not t.isdigit()]
    if not toks:
        toks = tokens(f)
    return tuple(stem(t) for t in toks), reduced


# Extra per-ingredient negative stems on top of docs/kupi_terms.json.
BUILTIN_NEGATIVES: dict[str, tuple[str, ...]] = {
    "maslo": ("arasid", "pomazank", "dyn", "kakaov", "orisk", "mandl", "burak", "prepusten", "ghi", "ghee", "cesnekov", "bylink"),
    "smetana_33": ("rostlinn", "zakysan", "zmrzlin", "sojov", "ovesn"),
    "smetana_12": ("rostlinn", "zakysan", "zmrzlin", "sojov", "ovesn"),
    "smetana_zakysana": ("rostlinn", "zmrzlin"),
    "jogurt_bily": ("reck", "ovocn", "jahod", "borůvk", "boruvk", "vanilk", "cokolad", "sojov", "kokos", "pit"),
    "tvaroh": ("dezert", "tycink", "sojov", "kolac", "pomazank"),
    "vejce": ("cokolad", "kinder", "prepelic", "tekut", "susen"),
    "kureci_prsa": ("salat", "sunk", "salam", "uzen", "parky", "parek", "polevk", "nugget", "smazen"),
    "kure_cele": ("prs", "stehn", "kridl", "jatr", "mlet", "salat", "polevk", "sunk", "salam", "parky", "grilovan"),
    "kachna_cela": ("prs", "stehn", "jatr", "sadl"),
    "sunka": ("salat", "pomazank"),
    "losos": ("pomazank", "salat", "uzen"),
    "brambory": ("kase", "salat", "knedl", "lupink", "chips", "hranolk", "krokety", "skrob", "sladk", "bramburk"),
    "cibule": ("smazen", "susen"),
    "cesnek": ("susen", "medved", "granul", "prasek"),
    "petrzel_koren": ("nat",),
    "celer_bulva": ("rapik",),
    "rajcata": ("cherry", "susen", "sterilov", "loupan", "krajen", "drcen", "protlak", "passat", "pyre", "koktejlov", "kecup", "omack", "polevk", "stav"),
    "okurka_salatova": ("kysel", "sterilov", "naklad", "znojemsk"),
    "paprika_cervena": ("mlet", "plnen", "sterilov", "uzen", "paliv", "sladk", "koren"),
    "zeli_bile": ("kysan", "kysel", "cerven", "pekingsk", "cinsk"),
    "zazvor": ("mlet", "susen", "kandov"),
    "jablko": ("susen", "mus", "stav", "dzus", "krizal"),
    "svestky": ("susen", "povidl", "knedl"),
    "broskve": ("kompot",),
    "merunky": ("susen", "dzem", "kompot"),
    "ananas": ("kompot", "dzus", "stav"),
    "med": ("medov", "medovnik"),
    "chleb": ("toustov", "strouhan"),
    "arasidy": ("masl", "pomazank"),
    "cukr": ("mouck", "vanilk", "trtinov", "hned", "perlov", "palmov", "kokosov"),
    "spagety": ("omack", "sugo"),
    "kecup": ("chips", "prichut"),
    "tunak_konzerva": ("salat", "pomazank", "steak", "cerstv"),
    "tunak_cerstvy": ("salat", "pomazank", "konzerv", "olej", "nalev", "stav"),
    "ryze_dlouhozrnna": ("mlecn", "nakyp", "chips", "burisk", "vlocky", "mouk", "nudl", "papir", "hork", "lezak", "piv", "svetl", "tmav"),
    "voda": ("ustn", "toalet", "micelar", "pletov", "kolinsk", "destilov"),
    "mleko_15": ("kokos", "kondenz", "susen", "sojov", "ovesn", "mandlov", "ryzov", "kefir", "acidof", "cokolad", "kakaov", "prasek", "prasku", "telov", "pletov", "kosmet", "cistic"),
    "mleko_35": ("kokos", "kondenz", "susen", "sojov", "ovesn", "mandlov", "ryzov", "kefir", "acidof", "cokolad", "kakaov", "prasek", "prasku", "telov", "pletov", "kosmet", "cistic"),
    "olej_olivovy": ("oliv v", "sardin", "tunak", "ancov"),
    "olej_slunecnicovy": ("tunak", "sardin", "kys"),
    "salat_hlavkovy": ("ovocn", "zeleninov", "salatov", "mix", "tunak", "sunk", "kureci", "tesovin", "bramborov"),
    "kokos_strouhany": ("mlek", "olej", "vod", "tuk"),
}

# Species words that rule an id out ("jelení kýta" is not "vepřová kýta").
SPECIES_NEGATIVES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("veprov", ("jelen", "srnc", "kanc", "zverin", "hovez", "kurec", "krut", "telec", "jehnec", "kachn", "husi")),
    ("hovez", ("veprov", "kurec", "krut", "telec", "jelen", "jehnec", "kanc")),
    ("kure", ("krut", "veprov", "hovez", "kachn", "husi", "krali")),
    ("krut", ("kurec", "veprov", "hovez", "kachn")),
    ("kachn", ("kurec", "krut", "husi", "veprov")),
    ("teleci", ("veprov", "hovez", "kurec", "jehnec")),
    ("jehnec", ("veprov", "hovez", "kurec", "telec")),
)


def _species_negatives(ingredient_id: str) -> set[str]:
    out: set[str] = set()
    for key, negs in SPECIES_NEGATIVES:
        if key in ingredient_id:
            out.update(negs)
    return out


# Category resolution from source hints and from strong title words. Values are
# allowed app categories (the union of every matching rule); NONFOOD marks
# offers to skip entirely. None = no opinion.
NONFOOD = "nonfood"
HINT_RULES: tuple[tuple[str, Any], ...] = (
    (r"pet_|_pet|pets|for_cats|for_dogs|cat_|dog_|mazlic|krmiv|pro psy|pro kocky|drogeri|cleaning|cleaner|cisti|čisti|praci|prací|hygien|garden|zahrad|hnojiv|electro|elektro|toys|hracky|hračky|hobby|nadobi|nádobí|textil|obuv|odev|oděv|kancel|kvetin|květin|rostlin|substrat|charcoal|uhli|briket|barbecue|television|audio|appliance|robot|mixer|kettle|microwave|elektro_grill|electric_grill|myck|pleny|kosmet|fuel|firelighter|fertilizer|insect|rodent|barkmulch|toalet|maker", NONFOOD),
    (r"cheese|syr|sýr|mlec|mléč|dairy|milk|yogurt|jogurt|butter|maslo|máslo|smetan|cream|quark|tvaroh", {"dairy", "egg", "other", "beverage", "condiment", "oil"}),
    (r"egg|vejce|vajic", {"egg"}),
    (r"poultry|drubez|drůbež|chicken|kure|kuř", {"poultry", "beverage"}),
    (r"meat|maso|pork|beef|veal|veprov|hovez|telec|jehnec|lamb|salami|salam|ham|sunk|šunk|sausage|uzenin|klobas|park|frankfurter|bacon|slanin", {"meat", "poultry", "oil"}),
    (r"fish|ryb|seafood|morsk|mořsk|tuna|salmon|losos|canned_fish", {"fish", "seafood"}),
    (r"vegetable|zelenin|salad|salat", {"vegetable", "herb", "legume", "condiment"}),
    (r"fruit|ovoce|citrus|stone_fruit|berries", {"fruit"}),
    (r"bread|pastry|peciv|pekar|pekař|bakery|croissant|bagel", {"bakery"}),
    (r"pasta|testovin|těstovin|couscous|rice|ryz|rýž|grain|cereal|musli|flour|mouk|obilov", {"pasta", "grain"}),
    (r"oil|olej|tuk|fat", {"oil", "condiment"}),
    (r"spice|koren|kořen|seasoning|flavors|sauce|omack|omáčk|ketchup|mustard|condiment|dressing|pesto|dochuc", {"spice", "condiment", "herb", "oil"}),
    (r"beer|pivo|wine|vino|víno|spirits|lihov|liker|likér|alkohol|alcohol|vodka|rum|whisky|prosecco", {"alcohol"}),
    (r"drink|napoj|nápoj|beverage|water|voda|juice|dzus|džus|stav|šťáv|mineral|lemonade|limonad|cola|energy", {"beverage", "alcohol"}),
    (r"coffee|kav|káv|tea|caj|čaj|cocoa|kakao", {"other", "beverage"}),
    (r"sweet|sladk|candy|chocolate|cokolad|čokolád|biscuit|susenk|sušenk|wafer|snack|chips|crisps|salty_delic|dessert|dezert|ice_cream|icecream|zmrzlin|spread|pomazank", {"other", "sweetener", "bakery", "dairy", "nut", "condiment", "fruit"}),
    (r"canned|konzerv|sterilov|legume|lustenin|luštěnin|fazol|cizrn|cock|čočk", {"legume", "vegetable", "condiment", "fruit"}),
    (r"nut|orech|ořech|seed|semin|semínk|dried_fruit|susene", {"nut", "fruit", "spice"}),
    (r"frozen|mraz|mraž", None),
    (r"ready|hotov|instant|soup|polevk|polévk|dumpling|knedl", {"other", "grain", "bakery", "pasta"}),
)

# Strong title stems -> categories the target ingredient must be in (penalty ×0.5
# otherwise). Keys are compared with prefix matching against title stems.
TITLE_CATEGORY_HINTS: tuple[tuple[str, frozenset[str]], ...] = tuple(
    (k, frozenset(v)) for k, v in {
        "smetan": {"dairy", "other"},
        "jogurt": {"dairy"},
        "tvaroh": {"dairy", "other"},
        "kefir": {"dairy"},
        "syr": {"dairy", "other"},
        "kurec": {"poultry", "meat", "beverage"},
        "kure": {"poultry", "meat", "beverage"},
        "krut": {"poultry", "meat"},
        "kachn": {"poultry", "meat", "oil"},
        "veprov": {"meat", "oil"},
        "hovez": {"meat", "beverage"},
        "telec": {"meat"},
        "jehnec": {"meat"},
        "losos": {"fish"},
        "tresk": {"fish"},
        "tunak": {"fish"},
        "pstruh": {"fish"},
        "kapr": {"fish"},
        "krevet": {"seafood", "condiment"},
        "vejc": {"egg", "pasta"},
        "vajick": {"egg"},
        "mouk": {"grain"},
        "ryze": {"grain", "pasta", "condiment", "alcohol", "other"},
        "ryzov": {"grain", "pasta", "condiment", "alcohol", "other", "beverage"},
        "olej": {"oil"},
        "pivo": {"alcohol", "beverage"},
        "piva": {"alcohol", "beverage"},
    }.items()
)


def _title_hint_hit(key: str, ts: str) -> bool:
    return ts == key or (len(key) >= 5 and ts.startswith(key))


def category_from_hint(hint: Any) -> set[str] | str | None:
    """Map a source category hint to allowed app categories, NONFOOD, or None."""
    if hint is None:
        return None
    if isinstance(hint, (list, tuple)):
        text = " ".join(str(h) for h in hint)
    else:
        text = str(hint)
    if not text.strip():
        return None
    low = text.lower()
    folded = fold(text)
    allowed: set[str] = set()
    for pat, cats in HINT_RULES:
        if re.search(pat, low) or re.search(pat, folded):
            if cats == NONFOOD:
                return NONFOOD
            if cats:
                allowed |= set(cats)
    return allowed or None


# Sibling groups: ids that share a name and are told apart by a numeric hint
# (fat %), by keywords, or by a default preference.
@dataclass
class SiblingGroup:
    ids: tuple[str, ...]
    pct_split: tuple[float, str, str] | None = None   # (threshold, id_if_below, id_if_at_or_above)
    keywords: dict[str, tuple[str, ...]] = field(default_factory=dict)  # id -> stems that select it
    default: str | None = None


SIBLING_GROUPS: tuple[SiblingGroup, ...] = (
    SiblingGroup(("mleko_15", "mleko_35"), pct_split=(2.5, "mleko_15", "mleko_35"),
                 keywords={"mleko_15": ("polotucn",), "mleko_35": ("plnotucn", "tucn", "selsk")}, default="mleko_15"),
    SiblingGroup(("smetana_12", "smetana_33"), pct_split=(20.0, "smetana_12", "smetana_33"),
                 keywords={"smetana_12": ("varen",), "smetana_33": ("slehan",)}, default="smetana_33"),
    SiblingGroup(("fazole_cervene_sterilovane", "fazole_cervene_susene"),
                 keywords={"fazole_cervene_sterilovane": ("steriliz", "sterilov", "konzerv", "plechov", "nalev"),
                           "fazole_cervene_susene": ("susen", "such")}, default="fazole_cervene_sterilovane"),
    SiblingGroup(("fazole_bile_sterilovane", "fazole_bile_susene"),
                 keywords={"fazole_bile_sterilovane": ("steriliz", "sterilov", "konzerv", "plechov", "nalev"),
                           "fazole_bile_susene": ("susen", "such")}, default="fazole_bile_sterilovane"),
    SiblingGroup(("fazole_cerne_sterilovane", "fazole_cerne_susene"),
                 keywords={"fazole_cerne_sterilovane": ("steriliz", "sterilov", "konzerv", "plechov", "nalev"),
                           "fazole_cerne_susene": ("susen", "such")}, default="fazole_cerne_sterilovane"),
    SiblingGroup(("cizrna_sterilovana", "cizrna_susena"),
                 keywords={"cizrna_sterilovana": ("steriliz", "sterilov", "konzerv", "plechov", "nalev"),
                           "cizrna_susena": ("susen", "such")}, default="cizrna_sterilovana"),
    SiblingGroup(("cocka_cervena", "cocka_hneda"), keywords={"cocka_cervena": ("cerven",), "cocka_hneda": ("hned", "zelen")},
                 default="cocka_cervena"),
    SiblingGroup(("vino_bile", "vino_cervene"), keywords={"vino_bile": ("bil",), "vino_cervene": ("cerven",)}),
    SiblingGroup(("pivo_lezak", "pivo_tmave"), keywords={"pivo_tmave": ("tmav", "cern")}, default="pivo_lezak"),
    SiblingGroup(("drozdi_cerstve", "drozdi_susene"), keywords={"drozdi_susene": ("susen", "instant")}, default="drozdi_cerstve"),
    SiblingGroup(("jogurt_bily", "jogurt_recky"), keywords={"jogurt_recky": ("reck",)}, default="jogurt_bily"),
)


class Catalog:
    def __init__(self, ingredients: Iterable[dict[str, Any]], negatives_extra: dict[str, Iterable[str]] | None = None):
        self.items: dict[str, Ingredient] = {}
        self.index: dict[str, set[str]] = {}  # first 3 chars of a stem -> ingredient ids
        for raw in ingredients:
            ing = Ingredient(
                id=raw["id"],
                name_cs=raw.get("nameCs") or raw["id"],
                category=raw.get("category") or "other",
                unit_grams={k: float(v) for k, v in (raw.get("unitGrams") or {}).items() if v},
                density=raw.get("densityGPerMl"),
                ref_price=raw.get("refPriceCzkPerKg"),
            )
            names: list[str] = []
            for key in ("nameCs", "namePluralCs"):
                if raw.get(key):
                    names.append(raw[key])
            names.extend(raw.get("aliases") or [])
            seen: set[tuple[str, ...]] = set()
            for n in names:
                stems, reduced = _clean_stems(n)
                if not stems or stems in seen:
                    continue
                seen.add(stems)
                ing.variants.append((n, stems, 0.95 if reduced else 1.0))
                for s in stems:
                    self.index.setdefault(s[:3], set()).add(ing.id)
            neg = set(BUILTIN_NEGATIVES.get(ing.id, ())) | _species_negatives(ing.id)
            if negatives_extra and ing.id in negatives_extra:
                neg.update(negatives_extra[ing.id])
            ing.negatives = {fold(n) for n in neg if fold(n)}
            self.items[ing.id] = ing
        self.sibling_of: dict[str, SiblingGroup] = {}
        for g in SIBLING_GROUPS:
            for i in g.ids:
                if i in self.items:
                    self.sibling_of[i] = g

    @classmethod
    def load(cls, path: str = CATALOG_PATH, kupi_terms_path: str | None = KUPI_TERMS_PATH) -> "Catalog":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = data["ingredients"] if isinstance(data, dict) else data
        negatives: dict[str, list[str]] = {}
        if kupi_terms_path and os.path.exists(kupi_terms_path):
            try:
                with open(kupi_terms_path, encoding="utf-8") as f:
                    for term in json.load(f):
                        if term.get("id") and term.get("neg"):
                            negatives.setdefault(term["id"], []).extend(term["neg"])
            except (OSError, ValueError):
                pass
        return cls(items, negatives)

    def candidates_for(self, stems: Iterable[str]) -> set[str]:
        out: set[str] = set()
        for s in stems:
            if len(s) >= 3:
                out |= self.index.get(s[:3], set())
            else:
                # tokens < 3 chars only match exactly: scan every bucket lazily
                for key, ids in self.index.items():
                    if key == s:
                        out |= ids
        return out


# --------------------------------------------------------------------------- #
# Learned rules (data/mappings.json)
# --------------------------------------------------------------------------- #

@dataclass
class Rule:
    pattern: str
    store: str
    ingredient_id: str          # catalog id or "ignore"
    note: str = ""
    _regex: re.Pattern | None = None
    _sub: str = ""

    def __post_init__(self) -> None:
        p = self.pattern.strip()
        if p.startswith("re:"):
            self._regex = re.compile(p[3:], re.IGNORECASE)
        elif p.startswith("sub:"):
            self._regex = re.compile(re.escape(fold(p[4:])))
        elif len(p) > 2 and p.startswith("/") and p.endswith("/"):
            self._regex = re.compile(p[1:-1], re.IGNORECASE)
        else:
            self._sub = fold(p)

    def matches(self, folded_title: str, store: str) -> bool:
        if self.store not in ("*", "", None) and self.store != store:
            return False
        if self._regex is not None:
            return bool(self._regex.search(folded_title))
        return bool(self._sub) and re.search(r"(?<![a-z0-9])" + re.escape(self._sub) + r"(?![a-z0-9])", folded_title) is not None


def load_rules(path: str = MAPPINGS_PATH) -> list[Rule]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rules: list[Rule] = []
    for r in data.get("rules", []):
        if not r.get("pattern") or not r.get("ingredientId"):
            continue
        rules.append(Rule(r["pattern"], r.get("store") or "*", r["ingredientId"], r.get("note", "")))
    return rules


# --------------------------------------------------------------------------- #
# Title analysis
# --------------------------------------------------------------------------- #

@dataclass
class TitleInfo:
    raw: str
    folded: str
    stems: tuple[str, ...]          # content stems after noise removal
    pack: Pack | None
    percents: tuple[float, ...]
    price_basis: Pack | None        # "cena za 100 g"
    n_raw_tokens: int = 0           # tokens before brand / noise removal
    fragment: bool = False          # looks like a leaflet text fragment ("s vepřovým masem", "pupek")


FRAGMENT_START = frozenset("s se z ze v ve na k ke o do pro bez od a i nebo".split())

# Single-stem aliases that name a whole category ("sýr", "salám"): matching
# only such a word after brand/noise words were dropped is not a confident hit.
GENERIC_STEMS = frozenset("syr salam testovin olej mas houb nudl kase omack koren dzem sirup mlek pecivo".split())


_BRAND_RES: list[re.Pattern] = []


def _brand_regexes() -> list[re.Pattern]:
    global _BRAND_RES
    if not _BRAND_RES:
        brands = sorted({fold(b) for b in BRANDS if fold(b)}, key=len, reverse=True)
        _BRAND_RES = [re.compile(r"(?<![a-z0-9])" + re.escape(b) + r"(?![a-z0-9])") for b in brands]
    return _BRAND_RES


def analyze_title(title: str, extra_pack: str | None = None) -> TitleInfo:
    raw = (title or "").replace(" ", " ")
    percents = tuple(float(m.group(1).replace(",", ".")) for m in PERCENT_RE.finditer(raw))
    price_basis = parse_price_basis(raw)
    pack = parse_pack(raw)
    # strip pack / percent fragments so their digits do not become tokens
    t = PERCENT_RE.sub(" ", raw)
    t = PACK_MULTI_RE.sub(" ", t)
    t = PACK_RE.sub(" ", t)
    f = fold(t)
    for ph in NOISE_PHRASES:
        f = re.sub(r"(?<![a-z0-9])" + re.escape(ph) + r"(?![a-z0-9])", " ", f)
    # words that carry product identity (brands and cut/form words included)
    n_raw = len([x for x in f.split(" ") if len(x) >= 2 and x not in NOISE_WORDS and not x.isdigit()])
    for rx in _brand_regexes():
        f = rx.sub(" ", f)
    toks = [x for x in f.split(" ")
            if len(x) >= 2 and x not in NOISE_WORDS and x not in TITLE_NOISE_WORDS and not x.isdigit()]
    stems = tuple(stem(x) for x in toks)
    if extra_pack and not pack:
        pack = parse_pack(extra_pack)
    folded = fold(raw)
    stripped = raw.strip()
    first = folded.split(" ")[0] if folded else ""
    fragment = bool(stripped) and (stripped[0].islower() or first in FRAGMENT_START)
    return TitleInfo(raw=raw, folded=folded, stems=stems, pack=pack, percents=percents, price_basis=price_basis,
                     n_raw_tokens=n_raw, fragment=fragment)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

@dataclass
class Suggestion:
    ingredient_id: str
    score: float
    variant: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self, catalog: Catalog | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {"ingredientId": self.ingredient_id, "score": round(self.score, 3), "via": self.variant}
        if catalog and self.ingredient_id in catalog.items:
            d["nameCs"] = catalog.items[self.ingredient_id].name_cs
        if self.reasons:
            d["reasons"] = self.reasons
        return d


def _match_variant(title_stems: tuple[str, ...], name_stems: tuple[str, ...]) -> tuple[float, int, bool]:
    """Return (name score 0..1, number of title stems consumed, first title
    stem consumed). Every name stem must find a title stem: exact = 1.0,
    prefix = 0.8."""
    used: set[int] = set()
    total = 0.0
    for ns in name_stems:
        best = 0.0
        best_i = -1
        for i, ts in enumerate(title_stems):
            if i in used:
                continue
            if ts == ns:
                best, best_i = 1.0, i
                break
            if best < 0.8 and token_matches(ns, ts):
                best, best_i = 0.8, i
        if best_i < 0:
            return 0.0, 0, False
        used.add(best_i)
        total += best
    return total / len(name_stems), len(used), 0 in used


def score_title(info: TitleInfo, catalog: Catalog, hint: Any = None) -> list[Suggestion]:
    """Score every plausible ingredient for a title. Sorted best first."""
    stems = info.stems
    if not stems:
        return []
    allowed = category_from_hint(hint)
    title_cats: set[str] | None = None
    for key, cats in TITLE_CATEGORY_HINTS:
        if any(_title_hint_hit(key, ts) for ts in stems):
            title_cats = set(cats) if title_cats is None else (title_cats | set(cats))
    n_title = len(stems)
    results: dict[str, Suggestion] = {}
    for iid in catalog.candidates_for(stems):
        ing = catalog.items[iid]
        best: Suggestion | None = None
        for text, name_stems, weight in ing.variants:
            name_score, consumed, head = _match_variant(stems, name_stems)
            if name_score <= 0:
                continue
            # coverage of the title: 1/1 -> 1.0, 2/3 -> 0.90, 1/2 -> 0.84 (review), 1/3 -> 0.77
            title_cov = consumed / n_title
            score = weight * name_score * (0.45 + 0.55 * math.sqrt(title_cov))
            # Czech leaflet titles lead with the head noun ("Gouda se zeleným
            # pestem"); a variant that skips the first word is less likely the head.
            if not head and consumed < n_title:
                score *= HEAD_FACTOR
            if (len(name_stems) == 1 and name_stems[0] in GENERIC_STEMS and consumed == n_title
                    and info.n_raw_tokens > n_title):
                score = min(score, GENERIC_CAP)   # "Leerdammer plátkový sýr" -> not simply "sýr"
            if best is None or score > best.score:
                best = Suggestion(iid, score, text)
        if best is None:
            continue
        # negatives
        neg_hit = [n for n in ing.negatives if any(ts.startswith(n) or (len(ts) >= 4 and n.startswith(ts)) for ts in stems)]
        if neg_hit:
            best.score *= 0.25
            best.reasons.append("negative:" + ",".join(sorted(neg_hit)[:3]))
        # category sanity from the source hint
        if allowed == NONFOOD:
            best.score *= 0.1
            best.reasons.append("hint:nonfood")
        elif isinstance(allowed, set) and ing.category not in allowed:
            best.score *= 0.5
            best.reasons.append(f"hint-category:{ing.category}")
        # category sanity from strong title words
        if title_cats is not None and ing.category not in title_cats:
            best.score *= 0.5
            best.reasons.append(f"title-category:{ing.category}")
        results[iid] = best
    # sibling resolution (fat %, keywords, default)
    for g in SIBLING_GROUPS:
        present = [i for i in g.ids if i in results]
        if len(present) < 1:
            continue
        chosen: str | None = None
        if g.pct_split and info.percents:
            thr, lo, hi = g.pct_split
            pct = min(info.percents)  # "82 %" butter is not in a group; milk 1,5 % / 3,5 %
            chosen = lo if pct < thr else hi
        if chosen is None:
            for iid, kws in g.keywords.items():
                if any(any(ts.startswith(k) for ts in stems) for k in kws):
                    chosen = iid
                    break
        if chosen is None:
            chosen = g.default
        if chosen is None or chosen not in catalog.items:
            continue
        top = max(results[i].score for i in present)
        if chosen not in results:
            src = results[present[0]]
            results[chosen] = Suggestion(chosen, top, src.variant, list(src.reasons))
        results[chosen].score = max(results[chosen].score, top)
        results[chosen].reasons.append("sibling:chosen")
        for i in present:
            if i != chosen:
                results[i].score *= 0.6
                results[i].reasons.append("sibling:other")
    ranked = sorted(results.values(), key=lambda s: (-s.score, s.ingredient_id))
    # ambiguity: two different ids nearly tied at the top
    if len(ranked) >= 2 and ranked[0].score >= REVIEW_MIN and ranked[0].score - ranked[1].score < AMBIGUITY_MARGIN:
        ranked[0].score *= 0.85
        ranked[0].reasons.append("ambiguous:" + ranked[1].ingredient_id)
        ranked.sort(key=lambda s: (-s.score, s.ingredient_id))
    for s in ranked:
        s.score = max(0.0, min(1.0, round(s.score, 4)))
    return ranked


# --------------------------------------------------------------------------- #
# Price per kg
# --------------------------------------------------------------------------- #

@dataclass
class PriceResult:
    czk_per_kg: float | None
    method: str
    detail: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


def per_base_to_kg(value: float, unit: str, ing: Ingredient | None) -> tuple[float | None, str | None]:
    """Convert CZK per (kg|l|ks) to CZK per kg using the catalog."""
    if unit == "kg":
        return value, None
    if unit == "l":
        density = (ing.density if ing and ing.density else None) or 1.0
        return value / density, None if (ing and ing.density) else "density-assumed-1.0"
    if unit == "ks":
        grams = ing.ks_grams if ing else None
        if not grams:
            return None, "no-unit-grams"
        return value / (grams / 1000.0), None
    return None, "unknown-unit"


def compute_price(offer: dict[str, Any], info: TitleInfo, ing: Ingredient | None) -> PriceResult:
    """Derive CZK/kg from the most reliable source available."""
    flags: list[str] = []
    # 1. structured unit price
    up = offer.get("unitPrice")
    if isinstance(up, dict) and _num(up.get("czk") if "czk" in up else up.get("value")):
        czk = _num(up.get("czk") if "czk" in up else up.get("value"))
        per = str(up.get("per") or up.get("unit") or "kg")
        pk = parse_pack(per) or (Pack(1.0, _unit_norm(per), per) if _unit_norm(per) in ("kg", "l", "ks") else None)
        if pk and pk.amount > 0 and czk:
            per_unit = czk / pk.amount
            kg, flag = per_base_to_kg(per_unit, pk.unit, ing)
            if flag:
                flags.append(flag)
            if kg:
                return PriceResult(round(kg, 2), "offer.unitPrice", {"perUnit": round(per_unit, 4), "unit": pk.unit}, flags)
    # 2. unit price text
    for key in ("unitPriceText", "pricePerUnit", "basePrice"):
        parsed = parse_unit_price(offer.get(key))
        if parsed:
            kg, flag = per_base_to_kg(parsed[0], parsed[1], ing)
            if flag:
                flags.append(flag)
            if kg:
                return PriceResult(round(kg, 2), f"offer.{key}", {"perUnit": round(parsed[0], 4), "unit": parsed[1]}, flags)
    parsed = parse_unit_price(info.raw)
    if parsed:
        kg, flag = per_base_to_kg(parsed[0], parsed[1], ing)
        if flag:
            flags.append(flag)
        if kg:
            return PriceResult(round(kg, 2), "title.unitPrice", {"perUnit": round(parsed[0], 4), "unit": parsed[1]}, flags)
    # 3. pack price / pack size (4. fetcher's price_per_kg as the last resort)
    price = _num(offer.get("price"))
    if price is None or price <= 0:
        hint_kg = _num(offer.get("pricePerKgHint"))
        if hint_kg and hint_kg > 0:
            return PriceResult(round(hint_kg, 2), "offer.price_per_kg", {}, flags)
        return PriceResult(None, "no-price", {}, flags + ["no-price"])
    pack: Pack | None = None
    pa, pu = _num(offer.get("packAmount")), offer.get("packUnit")
    if pa and pu:
        base = to_base(pa, str(pu))
        if base:
            pack = Pack(base[0], base[1], f"{pa} {pu}")
    # "cena za 100 g" wins over a pack size: the price refers to that basis
    basis = (parse_price_basis(offer.get("unitPriceText")) or parse_price_basis(offer.get("pack"))
             or info.price_basis)
    if pack is None and basis is not None:
        pack = basis
    for key in ("pack", "packText", "amount", "discountAmount", "unitPriceText", "basePrice"):
        if pack is None and offer.get(key):
            pack = parse_pack(offer.get(key))
    if pack is None:
        pack = info.pack
    if pack is None:
        for key in ("pack", "unit", "unitPriceText"):
            pt = fold(str(offer.get(key) or ""))
            if pt in ("kg", "1 kg", "za kg", "cena za kg"):
                pack = Pack(1.0, "kg", pt)
            elif pt in ("l", "1 l", "za l", "za litr"):
                pack = Pack(1.0, "l", pt)
            elif pt in ("ks", "kus", "1 ks", "1 kus", "za kus", "cena za kus"):
                pack = Pack(1.0, "ks", pt)
            if pack:
                break
    hint_kg = _num(offer.get("pricePerKgHint"))
    if pack is None or pack.amount <= 0:
        if hint_kg and hint_kg > 0:
            return PriceResult(round(hint_kg, 2), "offer.price_per_kg", {"price": price}, flags)
        return PriceResult(None, "no-pack", {"price": price}, flags + ["no-pack"])
    per_unit = price / pack.amount
    kg, flag = per_base_to_kg(per_unit, pack.unit, ing)
    if flag:
        flags.append(flag)
    if kg is None:
        if hint_kg and hint_kg > 0:
            return PriceResult(round(hint_kg, 2), "offer.price_per_kg", {"price": price, "pack": pack.as_dict()}, flags)
        return PriceResult(None, "pack", {"price": price, "pack": pack.as_dict()}, flags)
    return PriceResult(round(kg, 2), "pack", {"price": price, "pack": pack.as_dict()}, flags)


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #

STORE_ALIASES = {
    "penny market": "penny", "penny-market": "penny", "pennymarket": "penny",
    "albert hypermarket": "albert", "albert supermarket": "albert",
    "globus hypermarket": "globus", "tesco extra": "tesco", "tesco expres": "tesco",
    "billa plus": "billa", "kaufland hypermarket": "kaufland",
}


def normalize_store(value: Any) -> str | None:
    if not value:
        return None
    f = fold(str(value))
    if f in STORES:
        return f
    if f in STORE_ALIASES:
        return STORE_ALIASES[f]
    for s in STORES:
        if f.startswith(s):
            return s
    return None


def _first(offer: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in offer and offer[k] not in (None, ""):
            return offer[k]
    return None


def normalise_offer(offer: dict[str, Any]) -> dict[str, Any]:
    """Canonical camelCase view of an offer (accepts the fetch layer's
    snake_case ``Offer.to_dict()`` as well). ``raw`` is not copied."""
    raw = offer.get("raw") if isinstance(offer.get("raw"), dict) else {}
    o: dict[str, Any] = {k: v for k, v in offer.items() if k != "raw"}
    o["title"] = str(_first(offer, "title", "name", "product") or "").strip()
    o["store"] = normalize_store(_first(offer, "store", "shop"))
    o["price"] = _num(_first(offer, "price", "price_czk", "priceCzk"))
    orig = _num(_first(offer, "originalPrice", "oldPrice", "original_price_czk", "crossed", "regularPrice"))
    o["originalPrice"] = orig if orig and orig > 0 else None
    o["validFrom"] = _first(offer, "validFrom", "valid_from")
    o["validTo"] = _first(offer, "validTo", "valid_to")
    o["url"] = _first(offer, "url", "source_url", "sourceUrl") or ""
    o["source"] = _first(offer, "source", "provider") or ""
    promo = _first(offer, "promo", "isPromo", "is_promo", "isDeal")
    if isinstance(promo, bool):
        o["promo"] = promo
    elif o["originalPrice"] and o["price"] and o["originalPrice"] > o["price"]:
        o["promo"] = True
    elif str(o["source"]).lower().startswith("kupi"):
        o["promo"] = True
    else:
        o["promo"] = False
    o["club"] = bool(_first(offer, "club", "isClub", "loyalty") or False)
    pack = _first(offer, "pack", "packText", "pack_text", "amount", "discountAmount")
    if not pack:
        pack = raw.get("pack") or raw.get("pack_text") or raw.get("sellUnitSizeText") or raw.get("packaging")
    qty, unit = _first(offer, "quantity", "packAmount"), _first(offer, "unit", "packUnit")
    if qty and unit and not pack:
        pack = f"{float(qty):g} {unit}"
    o["pack"] = pack if isinstance(pack, str) else None
    if qty and unit:
        o["packAmount"], o["packUnit"] = float(qty), str(unit)
    o["unitPriceText"] = (_first(offer, "unitPriceText", "pricePerUnit", "basePrice", "unit_price_text")
                          or raw.get("basePrice") or raw.get("price_per_unit") or raw.get("unitPriceText"))
    o["unitPrice"] = offer.get("unitPrice") if isinstance(offer.get("unitPrice"), dict) else None
    # a per-kg value from the fetcher is a fallback only: litres ~ kg there,
    # the mapper recomputes with the catalog density / unitGrams when it can
    o["pricePerKgHint"] = _num(_first(offer, "price_per_kg", "pricePerKg", "czkPerKg", "czk_per_kg"))
    o["czkPerKg"] = None
    o["termId"] = _first(offer, "ingredient_id", "termId", "queryIngredientId", "ingredientId")
    o["sourceConfidence"] = _first(offer, "sourceConfidence", "parseConfidence") or raw.get("confidence")
    o["category"] = (_first(offer, "category", "categoryHint", "categories")
                     or raw.get("categories") or raw.get("wonCategory") or raw.get("category")
                     or raw.get("parentCategories"))
    for k in ("price_czk", "priceCzk", "original_price_czk", "oldPrice", "valid_from", "valid_to",
              "source_url", "sourceUrl", "is_promo", "isPromo", "ingredient_id", "price_per_kg",
              "quantity", "unit", "shop", "name"):
        o.pop(k, None)
    return o


class Mapper:
    def __init__(self, catalog: Catalog, rules: list[Rule] | None = None):
        self.catalog = catalog
        self.rules = rules or []

    def map_offer(self, offer: dict[str, Any]) -> dict[str, Any]:
        base = normalise_offer(offer)
        title, store = base["title"], base["store"]
        info = analyze_title(title, extra_pack=base.get("pack"))
        base["fragmentTitle"] = info.fragment
        hint = base.get("category")
        reasons: list[str] = []
        if store is None:
            reasons.append("unknown-store")

        # 1. learned rules first
        for rule in self.rules:
            if rule.matches(info.folded, store or ""):
                if rule.ingredient_id == "ignore":
                    return {**base, "status": "ignored", "ingredientId": None, "confidence": 1.0, "method": "rule",
                            "rule": rule.pattern, "reasons": reasons + ["rule:ignore"], "suggestions": []}
                if rule.ingredient_id in self.catalog.items:
                    ing = self.catalog.items[rule.ingredient_id]
                    price = compute_price(base, info, ing)
                    return self._finish(base, ing, 1.0, "rule", price, reasons + [f"rule:{rule.pattern}"],
                                        [Suggestion(ing.id, 1.0, f"rule:{rule.pattern}")])
                reasons.append(f"rule-unknown-id:{rule.ingredient_id}")

        # 2. non-food by hint
        allowed = category_from_hint(hint)
        if allowed == NONFOOD:
            return {**base, "status": "ignored", "ingredientId": None, "confidence": 0.0, "method": "hint",
                    "reasons": reasons + ["nonfood"], "suggestions": []}

        # 3. score
        ranked = score_title(info, self.catalog, hint)
        top3 = ranked[:3]
        if not ranked:
            return {**base, "status": "unmatched", "ingredientId": None, "confidence": 0.0, "method": "score",
                    "reasons": reasons + ["no-candidates"], "suggestions": [], "tokens": list(info.stems)}
        best = ranked[0]
        method = "score"
        # 4. term prior (kupi searched for this id): accept when the scorer agrees
        term = base.get("termId")
        if term and term in self.catalog.items:
            hit = next((s for s in ranked if s.ingredient_id == term), None)
            if (hit is not None and hit.score >= REVIEW_MIN and hit.score >= best.score - TERM_MARGIN
                    and not any(r.startswith("negative") for r in hit.reasons)):
                if hit is not best:
                    reasons.append(f"term-prior:{term}")
                hit.score = max(hit.score, 0.9)
                best, method = hit, "term+score"
                top3 = [hit] + [s for s in ranked if s is not hit][:2]
            else:
                reasons.append(f"term-mismatch:{term}")
        ing = self.catalog.items[best.ingredient_id]
        price = compute_price(base, info, ing)
        return self._finish(base, ing, best.score, method, price, reasons + best.reasons, top3, tokens=info.stems)

    def _finish(self, base: dict[str, Any], ing: Ingredient, confidence: float, method: str,
                price: PriceResult, reasons: list[str], suggestions: list[Suggestion],
                tokens: tuple[str, ...] = ()) -> dict[str, Any]:
        conf = confidence
        flags = list(price.flags)
        if price.czk_per_kg is None:
            flags.append("no-unit-price")
            conf = min(conf, AUTO_ACCEPT - 0.01)
        elif ing.ref_price:
            ratio = price.czk_per_kg / float(ing.ref_price)
            if ratio < PRICE_RATIO[0] or ratio > PRICE_RATIO[1]:
                flags.append(f"price-suspicious:{price.czk_per_kg}/{ing.ref_price}")
                conf = min(conf, AUTO_ACCEPT - 0.01)
        if "density-assumed-1.0" in flags and ing.category in SOLID_CATEGORIES and method != "rule":
            flags.append("liquid-pack-for-solid")   # "Moravská Švestka 0,5 l" is not a plum
            conf = min(conf, AUTO_ACCEPT - 0.01)
        if base.get("store") is None:
            conf = min(conf, AUTO_ACCEPT - 0.01)
        # a human rule overrides the heuristics below (otherwise low-confidence
        # leaflet-text rows could never leave the review queue)
        if base.get("fragmentTitle") and method != "rule":
            flags.append("fragment-title")
            conf = min(conf, AUTO_ACCEPT - 0.01)
        if str(base.get("sourceConfidence") or "").lower() == "low" and method != "rule":
            flags.append("source-confidence:low")
            conf = min(conf, AUTO_ACCEPT - 0.01)
        if conf >= AUTO_ACCEPT:
            status = "matched"
        elif conf >= REVIEW_MIN:
            status = "review"
        else:
            status = "unmatched"
        out = {
            **base,
            "status": status,
            "ingredientId": ing.id if status != "unmatched" else None,
            "ingredientNameCs": ing.name_cs if status != "unmatched" else None,
            "ingredientCategory": ing.category,
            "confidence": round(conf, 3),
            "method": method,
            "czkPerKg": price.czk_per_kg,
            "priceMethod": price.method,
            "priceDetail": price.detail,
            "flags": flags,
            "reasons": reasons,
            "suggestions": [s.as_dict(self.catalog) for s in suggestions],
        }
        if tokens:
            out["tokens"] = list(tokens)
        return out

    def map_offers(self, offers: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        matched: list[dict[str, Any]] = []
        rest: list[dict[str, Any]] = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            r = self.map_offer(offer)
            (matched if r["status"] == "matched" else rest).append(r)
        return matched, rest


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #

def load_offers(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for key in ("offers", "items", "rows", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        raise ValueError(f"{path}: expected a list or an object with an 'offers' list")
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list")
    return data


MATCHED_KEYS = ("ingredientId", "ingredientNameCs", "ingredientCategory", "store", "source", "title", "czkPerKg",
                "price", "originalPrice", "promo", "club", "validFrom", "validTo", "status", "url", "pack",
                "unitPriceText", "confidence", "method", "priceMethod", "priceDetail", "flags", "reasons",
                "suggestions", "category", "termId", "sourceConfidence", "id", "key")
UNMATCHED_KEYS = ("status", "title", "store", "source", "price", "originalPrice", "promo", "club", "pack",
                  "unitPriceText", "url", "validFrom", "validTo", "category", "czkPerKg", "ingredientId",
                  "confidence", "reasons", "flags", "suggestions", "tokens", "rule", "termId", "sourceConfidence",
                  "id", "key")


def _entry(r: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: r[k] for k in keys if k in r and r[k] not in (None, [], {}, "")}


def write_outputs(matched: list[dict[str, Any]], rest: list[dict[str, Any]], out_dir: str,
                  today: str | None = None) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    today = today or date.today().isoformat()
    counts = {
        "matched": len(matched),
        "review": sum(1 for r in rest if r["status"] == "review"),
        "unmatched": sum(1 for r in rest if r["status"] == "unmatched"),
        "ignored": sum(1 for r in rest if r["status"] == "ignored"),
    }
    per_store: dict[str, int] = {}
    for r in matched:
        per_store[r.get("store") or "?"] = per_store.get(r.get("store") or "?", 0) + 1
    matched_path = os.path.join(out_dir, "matched.json")
    unmatched_path = os.path.join(out_dir, "unmatched.json")
    with open(matched_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"v": 1, "generated": today, "counts": counts, "matchedPerStore": per_store,
                   "items": [_entry(r, MATCHED_KEYS) for r in matched]}, f, ensure_ascii=False, indent=1)
        f.write("\n")
    order = {"review": 0, "unmatched": 1, "ignored": 2}
    items = sorted((_entry(r, UNMATCHED_KEYS) for r in rest),
                   key=lambda r: (order.get(r["status"], 9), -(r.get("confidence") or 0), r.get("title", "")))
    with open(unmatched_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"v": 1, "generated": today, "counts": counts, "items": items}, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return matched_path, unmatched_path


def sync_catalog(app_catalog: str, dest: str = CATALOG_PATH) -> str:
    """Copy the app's ingredients.json verbatim into catalog/ (the app repo is
    the source of truth - see catalog/README.md)."""
    with open(app_catalog, encoding="utf-8") as f:
        data = json.load(f)
    items = data["ingredients"] if isinstance(data, dict) else data
    if not items or "id" not in items[0]:
        raise ValueError(f"{app_catalog}: not an ingredient catalog")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(app_catalog, dest)
    return f"synced {len(items)} ingredients from {app_catalog} -> {dest}"


def explain(title: str, catalog: Catalog, store: str | None = None, hint: Any = None) -> str:
    info = analyze_title(title)
    lines = [f"title : {title}", f"folded: {info.folded}", f"stems : {' '.join(info.stems)}",
             f"pack  : {info.pack.as_dict() if info.pack else None}   percents: {info.percents}"]
    for s in score_title(info, catalog, hint)[:8]:
        ing = catalog.items[s.ingredient_id]
        lines.append(f"  {s.score:5.3f}  {s.ingredient_id:32s} ({ing.category}) via '{s.variant}' {' '.join(s.reasons)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.mapper", description=__doc__.split("\n\n")[0])
    ap.add_argument("--offers", help="input offers.json (list or {offers:[...]})")
    ap.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "out"))
    ap.add_argument("--catalog", default=CATALOG_PATH)
    ap.add_argument("--mappings", default=MAPPINGS_PATH)
    ap.add_argument("--explain", metavar="TITLE", help="print the scoring of one title and exit")
    ap.add_argument("--store", help="store for --explain")
    ap.add_argument("--hint", help="category hint for --explain")
    ap.add_argument("--sync-catalog", nargs="?", const=DEFAULT_APP_CATALOG, metavar="APP_INGREDIENTS_JSON",
                    help="copy the app's ingredients.json into catalog/ and exit")
    args = ap.parse_args(argv)

    if args.sync_catalog:
        print(sync_catalog(args.sync_catalog, args.catalog))
        return 0
    catalog = Catalog.load(args.catalog)
    if args.explain:
        print(explain(args.explain, catalog, args.store, args.hint))
        return 0
    if not args.offers:
        ap.error("--offers is required (or use --explain / --sync-catalog)")
    offers = load_offers(args.offers)
    mapper = Mapper(catalog, load_rules(args.mappings))
    matched, rest = mapper.map_offers(offers)
    mp, up = write_outputs(matched, rest, args.out_dir)
    counts = {"matched": len(matched)}
    for r in rest:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"offers: {len(offers)}  " + "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    print(f"wrote {mp} and {up}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
