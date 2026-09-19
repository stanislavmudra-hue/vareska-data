#!/usr/bin/env python3
"""Validate a price table against its JSON Schema and the rules the Flutter
app enforces at load time (lib/models/price.dart):

* ``docs/prices.json`` (v1, ``docs/prices.schema.json``) - Czech market,
* ``docs/prices/<market>.json`` (v2, ``docs/prices.schema.v2.json``) - one per
  market; ``market``/``currency`` must agree with ``pipeline/markets.py`` and
  only the market's own stores may appear.

Rules: stores are ``Store`` enum names of the market, every price is a
positive number, dates are ``YYYY-MM-DD`` and ``validFrom <= validTo``,
ingredient ids exist in the catalog, at most 5 000 deals, ``updated`` not in
the future. ``--market cz`` validates both the v1 and the v2 file; ``--market
all`` every market. Exit code 1 on the first failing rule set (all errors are
printed).

The schema check uses ``jsonschema`` when it is installed and otherwise a
small built-in validator covering the subset of JSON Schema the file uses
(type, required, properties, additionalProperties, propertyNames, enum,
const, pattern, min/max, exclusiveMinimum, minLength/maxLength,
minProperties, items, maxItems, $ref to ``#/$defs``).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_prices import (  # noqa: E402
    CATEGORIES, DOCS_DIR, MAX_DEALS_TOTAL, PRICES_PATH, STORES, load_catalog,
    load_json, parse_iso_date, prague_today,
)
from pipeline import markets  # noqa: E402

SCHEMA_PATH = os.path.join(DOCS_DIR, "prices.schema.json")
SCHEMA_V2_PATH = os.path.join(DOCS_DIR, "prices.schema.v2.json")


# ---------------------------------------------------------------------------
# Minimal JSON Schema subset validator (fallback when jsonschema is missing)
# ---------------------------------------------------------------------------

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "null": type(None),
}


def _is_type(value, t: str) -> bool:
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t])


def _resolve(schema: dict, ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref {ref}")
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _mini_validate(value, schema, root: dict, path: str, errors: list[str]) -> None:
    if schema is True:
        return
    if schema is False:
        errors.append(f"{path}: not allowed")
        return
    if "$ref" in schema:
        _mini_validate(value, _resolve(schema, schema["$ref"], root), root, path, errors)
        schema = {k: v for k, v in schema.items() if k != "$ref"}
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(value, x) for x in types):
            errors.append(f"{path}: expected {t}, got {type(value).__name__}")
            return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {value} <= exclusiveMinimum {schema['exclusiveMinimum']}")
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required '{req}'")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            errors.append(f"{path}: fewer than {schema['minProperties']} properties")
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties", True)
        for k, v in value.items():
            if "propertyNames" in schema:
                _mini_validate(k, schema["propertyNames"], root, f"{path}.{k} (name)", errors)
            if k in props:
                _mini_validate(v, props[k], root, f"{path}.{k}", errors)
            elif addl is False:
                errors.append(f"{path}: unexpected property '{k}'")
            elif isinstance(addl, dict):
                _mini_validate(v, addl, root, f"{path}.{k}", errors)
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                _mini_validate(item, schema["items"], root, f"{path}[{i}]", errors)


def schema_errors(table, schema: dict) -> list[str]:
    try:
        import jsonschema  # type: ignore

        validator = jsonschema.Draft202012Validator(schema)
        return [f"{'/'.join(str(p) for p in e.absolute_path) or '$'}: {e.message}"
                for e in sorted(validator.iter_errors(table), key=lambda e: list(e.absolute_path))]
    except ImportError:
        errors: list[str] = []
        _mini_validate(table, schema, schema, "$", errors)
        return errors


# ---------------------------------------------------------------------------
# App rules
# ---------------------------------------------------------------------------


def table_version(table) -> int:
    """1 for the legacy Czech table, 2 for a per-market table (by ``v``)."""
    return 2 if isinstance(table, dict) and table.get("v") == 2 else 1


def rule_errors(table, catalog_ids: set[str] | None, today: dt.date | None = None,
                market: str | None = None) -> list[str]:
    """Rules mirrored from PriceTable.fromJson / Deal.fromJson plus sanity
    checks. v1 tables use ``czkPerKg``/``deals[].czkPerKg`` and the Czech
    stores; v2 tables ``perKg``/``deals[].perKg`` plus ``market``/``currency``
    (checked against ``market`` when given, else against the file's own)."""
    errors: list[str] = []
    today = today or prague_today()
    if not isinstance(table, dict):
        return ["root: expected object"]
    v = table_version(table)
    price_key = "perKg" if v == 2 else "czkPerKg"
    if v == 2:
        code = str(table.get("market") or "")
        if code not in markets.MARKETS:
            errors.append(f"market: unknown market {code!r}")
            code = market or markets.DEFAULT_MARKET
        if market and markets.get(market).code != code:
            errors.append(f"market: expected {markets.get(market).code!r}, got {code!r}")
        m = markets.get(code)
        if table.get("currency") != m.currency:
            errors.append(f"currency: expected {m.currency!r} for market {m.code}, got {table.get('currency')!r}")
        stores = m.stores
    else:
        if table.get("v") != 1:
            errors.append(f"v: expected 1, got {table.get('v')!r}")
        if market and markets.get(market).code != markets.DEFAULT_MARKET:
            errors.append(f"v: schema v1 is Czech only, cannot hold market {market!r}")
        stores = STORES
    upd = parse_iso_date(table.get("updated"))
    if upd is None:
        errors.append(f"updated: not an ISO date: {table.get('updated')!r}")
    elif upd > today:
        errors.append(f"updated: {upd} is in the future (today {today})")

    prices = table.get(price_key)
    if not isinstance(prices, dict):
        errors.append(f"{price_key}: expected object")
        prices = {}
    for ing, per_store in prices.items():
        if catalog_ids is not None and ing not in catalog_ids:
            errors.append(f"{price_key}.{ing}: unknown ingredient id")
        if not isinstance(per_store, dict) or not per_store:
            errors.append(f"{price_key}.{ing}: expected non-empty object")
            continue
        for store, price in per_store.items():
            if store not in stores:
                errors.append(f"{price_key}.{ing}.{store}: unknown store")
            if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0 \
                    or price != price or price == float("inf"):
                errors.append(f"{price_key}.{ing}.{store}: price must be a positive number, got {price!r}")

    deals = table.get("deals")
    if not isinstance(deals, list):
        errors.append("deals: expected array")
        deals = []
    if len(deals) > MAX_DEALS_TOTAL:
        errors.append(f"deals: {len(deals)} > {MAX_DEALS_TOTAL}")
    for i, d in enumerate(deals):
        ctx = f"deals[{i}]"
        if not isinstance(d, dict):
            errors.append(f"{ctx}: expected object")
            continue
        for key in ("ingredientId", "store", price_key, "validFrom", "validTo", "title"):
            if key not in d:
                errors.append(f"{ctx}: missing {key}")
        ing = d.get("ingredientId")
        if catalog_ids is not None and isinstance(ing, str) and ing not in catalog_ids:
            errors.append(f"{ctx}: unknown ingredient id {ing!r}")
        if d.get("store") not in stores:
            errors.append(f"{ctx}: unknown store {d.get('store')!r}")
        p = d.get(price_key)
        if not isinstance(p, (int, float)) or isinstance(p, bool) or p <= 0:
            errors.append(f"{ctx}: {price_key} must be a positive number")
        vf, vt = parse_iso_date(d.get("validFrom")), parse_iso_date(d.get("validTo"))
        if vf is None or vt is None:
            errors.append(f"{ctx}: validFrom/validTo must be YYYY-MM-DD")
        else:
            if vf > vt:
                errors.append(f"{ctx}: validFrom {vf} after validTo {vt}")
            if vt < today:
                errors.append(f"{ctx}: validTo {vt} already expired (today {today})")
        if not isinstance(d.get("title"), str) or not d.get("title", "").strip():
            errors.append(f"{ctx}: title must be a non-empty string")

    fb_key = "categoryFallbackPerKg" if v == 2 else "categoryFallbackCzkPerKg"
    fb = table.get(fb_key)
    if not isinstance(fb, dict):
        errors.append(f"{fb_key}: expected object")
    else:
        for cat, price in fb.items():
            if cat not in CATEGORIES:
                errors.append(f"{fb_key}.{cat}: unknown category")
            if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
                errors.append(f"{fb_key}.{cat}: price must be positive")
    return errors


def validate_file(prices_path: str = PRICES_PATH, schema_path: str | None = None,
                  catalog_path: str | None = None, today: dt.date | None = None,
                  require_catalog: bool = True, market: str | None = None) -> list[str]:
    """Errors of one file. The schema defaults to the one matching the file's
    ``v`` (``prices.schema.json`` / ``prices.schema.v2.json``)."""
    table = load_json(prices_path, None)
    if table is None:
        return [f"{prices_path}: file not found"]
    if schema_path is None:
        schema_path = SCHEMA_V2_PATH if table_version(table) == 2 else SCHEMA_PATH
    schema = load_json(schema_path, None)
    errors: list[str] = []
    if schema is None:
        errors.append(f"{schema_path}: schema not found")
    else:
        errors += schema_errors(table, schema)
    catalog = load_catalog(catalog_path)
    if not catalog:
        if require_catalog:
            errors.append("catalog: ingredients.json not found (see catalog/ingredients.json)")
        ids = None
    else:
        ids = set(catalog)
    errors += rule_errors(table, ids, today, market)
    # de-duplicate while keeping order
    seen: set[str] = set()
    return [e for e in errors if not (e in seen or seen.add(e))]


def market_files(code: str) -> list[str]:
    """Files to validate for a market: v1 + v2 for cz, v2 otherwise."""
    mp = markets.paths(code)
    return [p for p in (mp["prices_v1"], mp["prices"]) if p]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate price tables (exit 1 on failure)")
    ap.add_argument("path", nargs="?", default=None, help="one file (default: the market's files)")
    ap.add_argument("--market", default=None, help="cz | sk | pl | de | at | all (default: cz, or the file's own)")
    ap.add_argument("--schema", default=None, help="default: by the file's v")
    ap.add_argument("--catalog", default=None)
    ap.add_argument("--today", default=None)
    ap.add_argument("--no-catalog", action="store_true", help="skip ingredient id check")
    ap.add_argument("--max-print", type=int, default=50)
    args = ap.parse_args(argv)
    today = parse_iso_date(args.today) if args.today else None
    if args.path:
        targets = [(args.path, args.market if args.market and args.market != "all" else None)]
    else:
        codes = markets.parse_selection(args.market or markets.DEFAULT_MARKET)
        targets = [(f, c) for c in codes for f in market_files(c)]
    rc = 0
    for path, code in targets:
        errors = validate_file(path, args.schema, args.catalog, today,
                               require_catalog=not args.no_catalog, market=code)
        if errors:
            print(f"INVALID: {len(errors)} error(s) in {path}")
            for e in errors[:args.max_print]:
                print(" - " + e)
            if len(errors) > args.max_print:
                print(f" ... and {len(errors) - args.max_print} more")
            rc = 1
            continue
        table = load_json(path)
        key = "perKg" if table_version(table) == 2 else "czkPerKg"
        print(f"OK: {os.path.relpath(path)} - v{table_version(table)} {table.get('market', 'cz')} "
              f"{table.get('currency', 'CZK')}: {len(table.get(key, {}))} ingredients, "
              f"{len(table.get('deals', []))} deals, updated {table.get('updated')}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
