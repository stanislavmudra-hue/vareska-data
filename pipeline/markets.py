"""Markets (countries) the price pipeline publishes for.

One market = one country with its own grocery chains, currency, catalogue
language and price table (``docs/prices/<market>.json``, schema v2). The
Czech market additionally keeps the legacy ``docs/prices.json`` (schema v1)
that older app versions read.

The store lists mirror the app's ``Store`` enum per market exactly; the
pipeline never publishes a store the app does not know.

``fx`` converts CZK reference values of the catalogue (``refPriceCzkPerKg``,
``categoryFallbackCzkPerKg``) into the market currency for outlier checks and
the per-market category fallbacks. They are rough, hand-maintained rates -
good enough for "is this price plausible" and for a fallback estimate, not
for accounting.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO_ROOT, "out")
DOCS_DIR = os.path.join(REPO_ROOT, "docs")

DEFAULT_MARKET = "cz"


@dataclass(frozen=True)
class Market:
    code: str                 # cz | sk | pl | de | at
    label: str                # human label (Czech, for the panel)
    currency: str             # ISO 4217
    lang: str                 # catalogue language: cs | sk | pl | de
    stores: tuple[str, ...]   # app Store enum names of this market
    fx: float                 # 1 CZK in the market currency
    accept_language: str      # HTTP Accept-Language for the market's sites
    lidl_domain: str          # Lidl storefront (same platform in every market)

    @property
    def is_default(self) -> bool:
        return self.code == DEFAULT_MARKET


MARKETS: dict[str, Market] = {
    "cz": Market("cz", "Česko", "CZK", "cs",
                 ("albert", "lidl", "kaufland", "tesco", "billa", "penny", "globus"),
                 1.0, "cs-CZ,cs;q=0.9", "https://www.lidl.cz"),
    "sk": Market("sk", "Slovensko", "EUR", "sk",
                 ("tesco", "lidl", "kaufland", "billa", "coopJednota", "terno", "fresh"),
                 0.041, "sk-SK,sk;q=0.9,cs;q=0.7", "https://www.lidl.sk"),
    "pl": Market("pl", "Polsko", "PLN", "pl",
                 ("biedronka", "lidl", "kaufland", "auchan", "carrefour", "netto", "dino", "aldi", "zabka"),
                 0.17, "pl-PL,pl;q=0.9", "https://www.lidl.pl"),
    "de": Market("de", "Německo", "EUR", "de",
                 ("aldiNord", "aldiSued", "lidl", "kaufland", "edeka", "rewe", "penny", "netto", "norma"),
                 0.041, "de-DE,de;q=0.9", "https://www.lidl.de"),
    "at": Market("at", "Rakousko", "EUR", "de",
                 ("billa", "spar", "interspar", "hofer", "lidl", "penny", "mpreis"),
                 0.041, "de-AT,de;q=0.9", "https://www.lidl.at"),
}

CURRENCIES = tuple(sorted({m.currency for m in MARKETS.values()}))
ALL_STORES = tuple(sorted({s for m in MARKETS.values() for s in m.stores}))


def codes() -> list[str]:
    return list(MARKETS)


def get(code: str | None) -> Market:
    code = (code or DEFAULT_MARKET).lower()
    try:
        return MARKETS[code]
    except KeyError:
        raise KeyError(f"unknown market {code!r}; known: {', '.join(MARKETS)}") from None


def parse_selection(value: str | None) -> list[str]:
    """``"all"`` / ``None`` -> every market; ``"cz,sk"`` -> those (validated)."""
    if not value or value.strip().lower() == "all":
        return codes()
    out: list[str] = []
    for part in value.split(","):
        part = part.strip().lower()
        if not part:
            continue
        get(part)
        if part not in out:
            out.append(part)
    return out


def stores_of(code: str | None) -> tuple[str, ...]:
    return get(code).stores


def convert_czk(value: float, code: str | None, ndigits: int = 2) -> float:
    """CZK -> market currency (identity for cz)."""
    m = get(code)
    return round(float(value) * m.fx, ndigits) if not m.is_default else float(value)


def category_fallback(czk_table: dict[str, float], code: str | None) -> dict[str, float]:
    """Per-market category fallback: CZ values x fx, rounded to 2 decimals."""
    return {k: convert_czk(v, code) for k, v in czk_table.items()}


def paths(code: str | None) -> dict[str, str | None]:
    """File layout of one market. The Czech market keeps the original
    paths (``out/offers.json``, ``docs/prices.json`` v1, ...); the other
    markets live under ``out/<market>/``, ``docs/prices/<market>.json`` and
    ``docs/data/<market>/``."""
    m = get(code)
    v2 = os.path.join(DOCS_DIR, "prices", f"{m.code}.json")
    if m.is_default:
        return {
            "offers": os.path.join(OUT_DIR, "offers.json"),
            "health": os.path.join(OUT_DIR, "health.json"),
            "matched": os.path.join(OUT_DIR, "matched.json"),
            "unmatched": os.path.join(OUT_DIR, "unmatched.json"),
            "build_report": os.path.join(OUT_DIR, "build_report.json"),
            "last_seen": os.path.join(OUT_DIR, "last_seen.json"),
            "prices_v1": os.path.join(DOCS_DIR, "prices.json"),
            "prices": v2,
            "report": os.path.join(OUT_DIR, "report.json"),
            "history": os.path.join(OUT_DIR, "history.json"),
            "panel_dir": os.path.join(DOCS_DIR, "data"),
            "out_dir": OUT_DIR,
        }
    sub = os.path.join(OUT_DIR, m.code)
    return {
        "offers": os.path.join(sub, "offers.json"),
        "health": os.path.join(sub, "health.json"),
        "matched": os.path.join(sub, "matched.json"),
        "unmatched": os.path.join(sub, "unmatched.json"),
        "build_report": os.path.join(sub, "build_report.json"),
        "last_seen": os.path.join(OUT_DIR, f"last_seen_{m.code}.json"),
        "prices_v1": None,
        "prices": v2,
        "report": os.path.join(sub, "report.json"),
        "history": os.path.join(sub, "history.json"),
        "panel_dir": os.path.join(DOCS_DIR, "data", m.code),
        "out_dir": sub,
    }
