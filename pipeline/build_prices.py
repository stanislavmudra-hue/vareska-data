#!/usr/bin/env python3
"""Build the price tables from the mapper output.

* ``docs/prices.json``            schema **v1**, Czech market only (unchanged shape -
                                  older app versions read it),
* ``docs/prices/<market>.json``   schema **v2** for every market (cz, sk, pl, de, at):
                                  ``{v: 2, market, currency, updated, perKg, deals[{…, perKg}],
                                  categoryFallbackPerKg}``, prices in the market currency.

``--market cz`` (default) reads ``out/matched.json`` and writes both files;
``--market sk`` reads ``out/sk/matched.json`` and writes ``docs/prices/sk.json``
(paths from ``pipeline/markets.py``). The rules below (medians, deals,
carry-forward with ``out/last_seen[_<market>].json``, outlier band against
``refPriceCzkPerKg`` × ``fx``, category fallback = CZ values × ``fx``) are the
same for every market; an empty ``perKg``/``deals`` is a valid result for a
market whose sources delivered nothing.

Input contract (``out/matched.json``, written by the mapper step)
----------------------------------------------------------------
Either a JSON list of items or an object with an ``items`` (alias ``matched``
or ``offers``) list. Optional top-level keys ``review`` and ``unmatched`` (lists
of rows that were not matched confidently) and ``sources`` / ``stats`` are read
by ``report.py`` only. One item::

    {
      "ingredientId": "maslo",            # catalog id (alias ingredient_id / id)
      "store": "lidl",                    # albert|lidl|kaufland|tesco|billa|penny|globus
      "source": "lidl",                   # globus|lidl|penny|albert-text|billa-text|kupi|...
      "title": "Máslo Jihočeské 250 g",   # alias name / product
      "czkPerKg": 159.6,                  # alias czk_per_kg / price_per_kg / pricePerKg; > 0
      "price": 39.9,                      # pack price, optional (alias price_czk)
      "originalPrice": 49.9,              # alias oldPrice / crossed / original_price_czk, optional
      "promo": true,                      # alias isPromo / is_promo / isDeal; optional, see is_promo()
      "validFrom": "2026-09-14",          # ISO date or null (alias valid_from)
      "validTo": "2026-09-20",            # ISO date or null (alias valid_to)
      "status": "matched",               # matched|review|unmatched (rows != matched are skipped)
      "club": false                       # loyalty price, optional (kept, only reported)
    }

Output rules
------------
* ``czkPerKg[ingredient][store]`` = median of the *current* regular (non-promo)
  rows; when only promo rows exist, the median of the current promo rows.
  A row is current when ``validFrom <= today <= validTo`` (missing bounds are
  open).
* ``deals[]`` = promo rows with an explicit ``validTo >= today``; ``validFrom``
  defaults to today. At most ``MAX_DEALS_PER_PAIR`` cheapest deals per
  (ingredient, store); exact duplicates are dropped.
* Carry-forward: a (ingredient, store) pair that has no row today keeps its
  previous value from the previous ``docs/prices.json`` for at most
  ``STALE_MAX_DAYS`` days (last-seen dates live in ``out/last_seen.json``),
  then it is dropped. Stores with no rows at all today (source failure) also
  keep their still-valid previous deals. Every carried value is listed in
  ``out/report.json`` under ``build.stale``.
* ``categoryFallbackCzkPerKg`` is copied from ``catalog/category_fallback.json``
  (a snapshot of the app's ``assets/data/prices.json``, see ``run_all.py
  --sync-catalog``).
* ``updated`` = today's date in Europe/Prague.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import statistics
import sys
from collections import defaultdict
from typing import Any, Iterable

PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(PIPELINE_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from pipeline import markets  # noqa: E402
OUT_DIR = os.path.join(REPO_ROOT, "out")
DOCS_DIR = os.path.join(REPO_ROOT, "docs")
CATALOG_DIR = os.path.join(REPO_ROOT, "catalog")

MATCHED_PATH = os.path.join(OUT_DIR, "matched.json")
PRICES_PATH = os.path.join(DOCS_DIR, "prices.json")
LAST_SEEN_PATH = os.path.join(OUT_DIR, "last_seen.json")
BUILD_REPORT_PATH = os.path.join(OUT_DIR, "build_report.json")
CATEGORY_FALLBACK_PATH = os.path.join(CATALOG_DIR, "category_fallback.json")

STORES = markets.stores_of(markets.DEFAULT_MARKET)   # Czech market; per market: markets.stores_of()
CATEGORIES = (
    "vegetable", "fruit", "herb", "spice", "meat", "poultry", "fish", "seafood",
    "dairy", "egg", "grain", "pasta", "bakery", "legume", "nut", "oil",
    "sweetener", "condiment", "alcohol", "beverage", "other",
)
# Mirror of PriceTable.defaultCategoryFallback (lib/models/price.dart, SPEC §5.4).
DEFAULT_CATEGORY_FALLBACK = {
    "vegetable": 45, "fruit": 60, "herb": 400, "spice": 900, "meat": 240,
    "poultry": 180, "fish": 350, "seafood": 450, "dairy": 120, "egg": 90,
    "grain": 40, "pasta": 60, "bakery": 80, "legume": 70, "nut": 350, "oil": 90,
    "sweetener": 50, "condiment": 150, "alcohol": 250, "beverage": 30, "other": 100,
}

STALE_MAX_DAYS = 21
MAX_DEALS_PER_PAIR = 3
MAX_DEALS_TOTAL = 5000
MAX_CZK_PER_KG = 50000.0
# Rows whose czkPerKg is outside refPrice * [OUTLIER_LOW, OUTLIER_HIGH] are skipped.
OUTLIER_LOW = 0.1
OUTLIER_HIGH = 10.0
TITLE_MAX_LEN = 80

# Catalog search order (first existing wins); the last entry is the local app
# checkout used during development only.
CATALOG_CANDIDATES = (
    os.path.join(CATALOG_DIR, "ingredients.json"),
    os.path.join(REPO_ROOT, "data", "ingredients.json"),
    os.path.join(PIPELINE_DIR, "ingredients.json"),
    os.path.join(DOCS_DIR, "data", "ingredients.json"),
    "C:/AI/Jídlo/assets/data/ingredients.json",
)

# ---------------------------------------------------------------------------
# Small shared helpers (validate.py / report.py import these)
# ---------------------------------------------------------------------------


def prague_today() -> dt.date:
    """Today's date in Europe/Prague (UTC+2 fallback when tzdata is missing)."""
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo("Europe/Prague")).date()
    except Exception:  # pragma: no cover - only without tzdata
        return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).date()


def parse_iso_date(value: Any) -> dt.date | None:
    """``YYYY-MM-DD`` (or a datetime string starting with it) -> date, else None."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def load_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    with io.open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def dump_json(path: str, data: Any, indent: int | None = 1) -> None:
    """UTF-8 without BOM, LF newlines, trailing newline."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.write("\n")


def find_catalog(explicit: str | None = None) -> str | None:
    for p in ([explicit] if explicit else []) + list(CATALOG_CANDIDATES):
        if p and os.path.exists(p):
            return p
    return None


def load_catalog(path: str | None = None) -> dict[str, dict]:
    """Ingredient id -> catalog entry. Empty dict when no catalog is available."""
    found = find_catalog(path)
    if not found:
        return {}
    raw = load_json(found, {})
    items = raw.get("ingredients", raw) if isinstance(raw, dict) else raw
    out: dict[str, dict] = {}
    for it in items or []:
        if isinstance(it, dict) and isinstance(it.get("id"), str):
            out[it["id"]] = it
    return out


def load_category_fallback(path: str = CATEGORY_FALLBACK_PATH) -> dict[str, float]:
    raw = load_json(path, {})
    table = raw.get("categoryFallbackCzkPerKg", raw) if isinstance(raw, dict) else {}
    out = dict(DEFAULT_CATEGORY_FALLBACK)
    for k, v in (table or {}).items():
        if k in CATEGORIES and isinstance(v, (int, float)) and v > 0:
            out[k] = v
    return out


def _first(item: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in item and item[k] is not None:
            return item[k]
    return default


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ".").replace("\xa0", "").replace(" ", ""))
        except ValueError:
            return None
    return None


def is_promo(item: dict) -> bool:
    """Promotion flag with the fallbacks agreed in docs/SOURCES.md."""
    flag = _first(item, "promo", "isPromo", "isDeal", "deal", "is_promo", "is_deal")
    if isinstance(flag, bool):
        return flag
    source = str(_first(item, "source", default="")).lower()
    if source.startswith("kupi"):
        return True
    price = _as_float(_first(item, "price", "price_czk", "priceCzk"))
    original = _as_float(_first(item, "originalPrice", "oldPrice", "crossed", "regularPrice",
                                "original_price", "original_price_czk"))
    if original and price and original > price:
        return True
    if original and not price:
        return True
    disc = _first(item, "discount", "discountText", "discountPercentage", "discount_percentage")
    if isinstance(disc, (int, float)) and not isinstance(disc, bool) and disc != 0:
        return True
    if isinstance(disc, str) and ("akce" in disc.lower() or "%" in disc):
        return True
    return False


def iter_items(matched: Any) -> Iterable[dict]:
    if isinstance(matched, list):
        items = matched
    elif isinstance(matched, dict):
        items = matched.get("items") or matched.get("matched") or matched.get("offers") or []
    else:
        items = []
    for it in items:
        if isinstance(it, dict):
            yield it


class Row:
    __slots__ = ("ingredient", "store", "source", "title", "czk_per_kg", "promo",
                 "valid_from", "valid_to", "club")

    def __init__(self, ingredient: str, store: str, source: str, title: str,
                 czk_per_kg: float, promo: bool, valid_from: dt.date | None,
                 valid_to: dt.date | None, club: bool) -> None:
        self.ingredient = ingredient
        self.store = store
        self.source = source
        self.title = title
        self.czk_per_kg = czk_per_kg
        self.promo = promo
        self.valid_from = valid_from
        self.valid_to = valid_to
        self.club = club

    def is_current(self, today: dt.date) -> bool:
        if self.valid_from and self.valid_from > today:
            return False
        if self.valid_to and self.valid_to < today:
            return False
        return True


def normalise_rows(matched: Any, catalog: dict[str, dict], report: dict,
                   stores: tuple[str, ...] = STORES, fx: float = 1.0) -> list[Row]:
    """Matched items -> Row list; skipped rows are counted in ``report``.
    ``stores`` = the market's store enum, ``fx`` converts the catalogue's CZK
    reference prices into the market currency for the outlier band."""
    skipped = report.setdefault("skipped", defaultdict(int))
    rows: list[Row] = []
    max_per_kg = MAX_CZK_PER_KG * fx
    store_ids = {s.lower(): s for s in stores}
    for it in iter_items(matched):
        status = str(_first(it, "status", default="matched")).lower()
        if status not in ("matched", "ok", "auto", "manual"):
            skipped["status:" + status] += 1
            continue
        ing = _first(it, "ingredientId", "ingredient_id", "ingredient", "id")
        if isinstance(ing, dict):  # {"id": ..., "score": ...}
            ing = ing.get("id") or ing.get("ingredientId")
        store = str(_first(it, "store", default="")).strip()
        if store not in stores:             # case-insensitive fallback ("Lidl", "coopjednota")
            store = store_ids.get(store.lower(), store)
        czk = _as_float(_first(it, "czkPerKg", "perKg", "czk_per_kg", "pricePerKg", "price_per_kg",
                                "unitPrice", "unit_price"))
        if not isinstance(ing, str) or not ing:
            skipped["no_ingredient"] += 1
            continue
        if store not in stores:
            skipped["bad_store"] += 1
            continue
        if catalog and ing not in catalog:
            skipped["unknown_ingredient"] += 1
            continue
        if czk is None or czk <= 0 or czk > max_per_kg:
            skipped["bad_price"] += 1
            continue
        ref = _as_float((catalog.get(ing) or {}).get("refPriceCzkPerKg"))
        if ref:
            ref = ref * fx
        if ref and ref > 0 and not (ref * OUTLIER_LOW <= czk <= ref * OUTLIER_HIGH):
            skipped["outlier"] += 1
            continue
        title = str(_first(it, "title", "name", "product", default="") or "").strip()
        if not title:
            title = (catalog.get(ing) or {}).get("nameCs") or ing
        rows.append(Row(
            ingredient=ing,
            store=store,
            source=str(_first(it, "source", default=store)),
            title=" ".join(title.split())[:TITLE_MAX_LEN],
            czk_per_kg=round(czk, 2),
            promo=is_promo(it),
            valid_from=parse_iso_date(_first(it, "validFrom", "valid_from")),
            valid_to=parse_iso_date(_first(it, "validTo", "valid_to")),
            club=bool(_first(it, "club", "loyalty", "is_club", default=False)),
        ))
    report["skipped"] = dict(skipped)
    return rows


def _median(values: list[float]) -> float:
    return round(float(statistics.median(values)), 1)


def previous_as_v1(previous: Any) -> dict | None:
    """A previous table in either schema -> the v1 field names used here
    (``czkPerKg`` / ``deals[].czkPerKg``); None when unusable."""
    if not isinstance(previous, dict):
        return None
    if "perKg" in previous and "czkPerKg" not in previous:
        deals = []
        for d in previous.get("deals") or []:
            if isinstance(d, dict):
                d = dict(d)
                if "czkPerKg" not in d and "perKg" in d:
                    d["czkPerKg"] = d.pop("perKg")
                deals.append(d)
        return {**previous, "czkPerKg": previous.get("perKg") or {}, "deals": deals}
    return previous


def build_table(rows: list[Row], today: dt.date, previous: dict | None,
                last_seen: dict, category_fallback: dict[str, float],
                report: dict, stores: tuple[str, ...] = STORES) -> tuple[dict, dict]:
    """Return (v1-shaped table, new last_seen map). ``stores`` = the market's
    store enum (carry-forward ignores previous values of unknown stores)."""
    previous = previous_as_v1(previous)
    STORES_ = stores
    today_iso = today.isoformat()
    current: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: {"regular": [], "promo": []})
    stores_seen: dict[str, int] = defaultdict(int)
    for r in rows:
        stores_seen[r.store] += 1
        if r.is_current(today):
            current[(r.ingredient, r.store)]["promo" if r.promo else "regular"].append(r.czk_per_kg)

    prices: dict[str, dict[str, float]] = defaultdict(dict)
    new_last_seen: dict[str, dict[str, str]] = defaultdict(dict)
    price_source: dict[tuple[str, str], str] = {}
    for (ing, store), buckets in current.items():
        if buckets["regular"]:
            prices[ing][store] = _median(buckets["regular"])
            price_source[(ing, store)] = "regular"
        elif buckets["promo"]:
            prices[ing][store] = _median(buckets["promo"])
            price_source[(ing, store)] = "promo"
        new_last_seen[ing][store] = today_iso

    # Carry-forward of previous values --------------------------------------
    stale: list[dict] = []
    dropped: list[dict] = []
    prev_prices = (previous or {}).get("czkPerKg") or {}
    prev_updated = parse_iso_date((previous or {}).get("updated")) or today
    for ing, per_store in prev_prices.items():
        if not isinstance(per_store, dict):
            continue
        for store, value in per_store.items():
            if store not in STORES_ or not isinstance(value, (int, float)) or value <= 0:
                continue
            if store in prices.get(ing, {}):
                continue
            seen = parse_iso_date((last_seen.get(ing) or {}).get(store)) or prev_updated
            age = (today - seen).days
            entry = {"ingredientId": ing, "store": store, "czkPerKg": value,
                     "lastSeen": seen.isoformat(), "ageDays": age}
            if age <= STALE_MAX_DAYS:
                prices[ing][store] = value
                new_last_seen[ing][store] = seen.isoformat()
                entry["reason"] = "store_missing" if stores_seen.get(store, 0) == 0 else "not_matched_today"
                stale.append(entry)
            else:
                dropped.append(entry)

    # Deals -----------------------------------------------------------------
    deals_by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if not r.promo or r.valid_to is None or r.valid_to < today:
            continue
        deals_by_pair[(r.ingredient, r.store)].append({
            "ingredientId": r.ingredient,
            "store": r.store,
            "czkPerKg": round(r.czk_per_kg, 1),
            "validFrom": (r.valid_from or today).isoformat(),
            "validTo": r.valid_to.isoformat(),
            "title": r.title,
        })
    carried_deals = 0
    for d in (previous or {}).get("deals") or []:
        if not isinstance(d, dict):
            continue
        store = d.get("store")
        vt = parse_iso_date(d.get("validTo"))
        if store in STORES_ and stores_seen.get(store, 0) == 0 and vt and vt >= today \
                and isinstance(d.get("ingredientId"), str) and isinstance(d.get("czkPerKg"), (int, float)):
            deals_by_pair[(d["ingredientId"], store)].append({
                "ingredientId": d["ingredientId"],
                "store": store,
                "czkPerKg": round(float(d["czkPerKg"]), 1),
                "validFrom": (parse_iso_date(d.get("validFrom")) or today).isoformat(),
                "validTo": vt.isoformat(),
                "title": str(d.get("title") or "")[:TITLE_MAX_LEN],
            })
            carried_deals += 1

    deals: list[dict] = []
    for key in sorted(deals_by_pair):
        seen_keys: set[tuple] = set()
        uniq: list[dict] = []
        for d in sorted(deals_by_pair[key], key=lambda x: (x["czkPerKg"], x["validTo"], x["title"])):
            # Same product (title) valid to the same day from two sources/prices is one deal:
            # keep the cheapest (rows are sorted by price asc), regardless of validFrom.
            k = (d["title"].strip().casefold(), d["validTo"])
            if k in seen_keys:
                continue
            seen_keys.add(k)
            uniq.append(d)
        deals.extend(uniq[:MAX_DEALS_PER_PAIR])
    if len(deals) > MAX_DEALS_TOTAL:
        report["dealsTruncated"] = len(deals) - MAX_DEALS_TOTAL
        deals = deals[:MAX_DEALS_TOTAL]

    table = {
        "v": 1,
        "updated": today_iso,
        "czkPerKg": {ing: dict(sorted(prices[ing].items())) for ing in sorted(prices) if prices[ing]},
        "deals": deals,
        "categoryFallbackCzkPerKg": {c: category_fallback[c] for c in CATEGORIES},
    }

    fresh_pairs = sum(1 for k in price_source)
    report.update({
        "today": today_iso,
        "rows": len(rows),
        "rowsPerStore": dict(sorted(stores_seen.items())),
        "rowsPerSource": _count(rows, "source"),
        "promoRows": sum(1 for r in rows if r.promo),
        "clubRows": sum(1 for r in rows if r.club),
        "ingredientsPriced": len(table["czkPerKg"]),
        "pairsPriced": sum(len(v) for v in table["czkPerKg"].values()),
        "pairsFresh": fresh_pairs,
        "pairsFromPromoOnly": sum(1 for v in price_source.values() if v == "promo"),
        "storesPriced": _stores_priced(table["czkPerKg"], STORES_),
        "deals": len(deals),
        "dealsCarried": carried_deals,
        "stale": stale,
        "staleCount": len(stale),
        "dropped": dropped,
        "droppedCount": len(dropped),
        "storesMissing": [s for s in STORES_ if stores_seen.get(s, 0) == 0],
    })
    return table, {k: dict(sorted(v.items())) for k, v in sorted(new_last_seen.items())}


def _count(rows: list[Row], attr: str) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for r in rows:
        c[getattr(r, attr)] += 1
    return dict(sorted(c.items()))


def _stores_priced(prices: dict[str, dict[str, float]], stores: tuple[str, ...] = STORES) -> dict[str, int]:
    c: dict[str, int] = {s: 0 for s in stores}
    for per_store in prices.values():
        for s in per_store:
            c[s] += 1
    return c


def to_v2(table: dict, market: str) -> dict:
    """v1-shaped table -> schema v2 document of ``market``."""
    m = markets.get(market)
    deals = []
    for d in table.get("deals") or []:
        deals.append({
            "ingredientId": d["ingredientId"], "store": d["store"], "perKg": d["czkPerKg"],
            "validFrom": d["validFrom"], "validTo": d["validTo"], "title": d["title"],
        })
    return {
        "v": 2,
        "market": m.code,
        "currency": m.currency,
        "updated": table["updated"],
        "perKg": table.get("czkPerKg") or {},
        "deals": deals,
        "categoryFallbackPerKg": dict(table.get("categoryFallbackCzkPerKg") or {}),
    }


def build(matched_path: str | None = None, prices_path: str | None = None,
          last_seen_path: str | None = None, catalog_path: str | None = None,
          category_fallback_path: str = CATEGORY_FALLBACK_PATH,
          build_report_path: str | None = "",
          today: dt.date | None = None, previous_path: str | None = None,
          market: str = markets.DEFAULT_MARKET, prices_v2_path: str | None = None) -> tuple[dict, dict]:
    """Run the whole build for one market; returns (table, build_report).

    ``prices_path`` is the v1 file (cz only; ignored for other markets),
    ``prices_v2_path`` the v2 file. Defaults come from ``markets.paths``.
    The returned table is v1-shaped for cz (what ``docs/prices.json`` holds)
    and v2 for the other markets."""
    m = markets.get(market)
    mp = markets.paths(m.code)
    matched_path = matched_path or mp["matched"]
    last_seen_path = last_seen_path or mp["last_seen"]
    if build_report_path == "":
        build_report_path = mp["build_report"]
    v1_path = (prices_path or mp["prices_v1"]) if m.is_default else None
    if prices_v2_path:
        v2_path = prices_v2_path
    elif prices_path:   # explicit v1 target (tests, manual runs): v2 goes next to it
        v2_path = os.path.join(os.path.dirname(os.path.abspath(prices_path)), "prices", f"{m.code}.json")
    else:
        v2_path = mp["prices"]
    today = today or prague_today()
    report: dict[str, Any] = {"market": m.code, "currency": m.currency,
                              "matchedPath": os.path.relpath(matched_path, REPO_ROOT)}
    matched = load_json(matched_path, None)
    if matched is None:
        report["warning"] = "matched.json missing - only carry-forward applied"
        matched = []
    catalog = load_catalog(catalog_path)
    report["catalogPath"] = os.path.relpath(find_catalog(catalog_path) or "", REPO_ROOT) or None
    report["catalogSize"] = len(catalog)
    rows = normalise_rows(matched, catalog, report, m.stores, m.fx)
    previous = load_json(previous_path or v1_path or v2_path, None)
    if isinstance(previous, dict):
        report["previousUpdated"] = previous.get("updated")
    else:
        previous = None
    last_seen = load_json(last_seen_path, {}) or {}
    fallback = markets.category_fallback(load_category_fallback(category_fallback_path), m.code)
    table, new_last_seen = build_table(rows, today, previous, last_seen, fallback, report, m.stores)
    v2 = to_v2(table, m.code)
    if v1_path:
        dump_json(v1_path, table)
    dump_json(v2_path, v2)
    dump_json(last_seen_path, new_last_seen)
    report["pricesPath"] = os.path.relpath(v1_path or v2_path, REPO_ROOT)
    report["pricesV2Path"] = os.path.relpath(v2_path, REPO_ROOT)
    if build_report_path:
        dump_json(build_report_path, report)
    return (table if v1_path else v2), report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--market", default=markets.DEFAULT_MARKET, help="cz | sk | pl | de | at")
    ap.add_argument("--matched", default=None, help="default: the market's out/…/matched.json")
    ap.add_argument("--out", default=None, help="v1 docs/prices.json (cz only); default from markets.paths")
    ap.add_argument("--out-v2", default=None, help="v2 docs/prices/<market>.json; default from markets.paths")
    ap.add_argument("--previous", default=None, help="previous table (default: the output file)")
    ap.add_argument("--last-seen", default=None)
    ap.add_argument("--catalog", default=None)
    ap.add_argument("--category-fallback", default=CATEGORY_FALLBACK_PATH)
    ap.add_argument("--today", default=None, help="YYYY-MM-DD override (tests)")
    args = ap.parse_args(argv)
    today = parse_iso_date(args.today) if args.today else None
    table, report = build(args.matched, args.out, args.last_seen, args.catalog,
                          args.category_fallback, today=today, previous_path=args.previous,
                          market=args.market, prices_v2_path=args.out_v2)
    print(f"[{report['market']}] {report['pricesPath']}: {report['ingredientsPriced']} ingredients, {report['pairsPriced']} pairs "
          f"({report['pairsFresh']} fresh, {report['staleCount']} stale, {report['droppedCount']} dropped), "
          f"{report['deals']} deals, rows {report['rows']}, skipped {report.get('skipped')}")
    if report.get("storesMissing"):
        print("stores without rows today: " + ", ".join(report["storesMissing"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
