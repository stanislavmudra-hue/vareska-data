#!/usr/bin/env python3
"""Write the run report, append the run history and copy the panel data.

Inputs (all optional except the price table):
* ``out/build_report.json`` - written by ``build_prices.py`` (stale/dropped lists),
* ``out/matched.json``      - mapper output (items, optional ``review``/``unmatched``/``sources``),
* ``out/offers.json``       - fetch output, list of raw offers (or ``{"offers": [...]}``);
                              alternatives ``out/offers/*.json`` per source,
* ``out/fetch_report.json`` - fetch status per source, copied verbatim into health.json
                              (alias ``out/sources.json`` / ``out/fetch.json``),
* ``out/unmatched.json`` / ``out/review.json`` - mapper leftovers when not embedded,
* ``docs/prices.json``.

Outputs:
* ``out/report.json``  - one document with everything above summarised,
* ``out/history.json`` - list, one entry per run date (same-day reruns replace),
* ``docs/data/report.json``, ``history.json``, ``unmatched.json``, ``health.json``,
  ``matched.json`` (trimmed) - what the panel in ``docs/`` reads.
"""
from __future__ import annotations

import argparse
import datetime as dt
import filecmp
import glob
import os
import shutil
import sys
from collections import defaultdict
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_prices import (  # noqa: E402
    BUILD_REPORT_PATH, DOCS_DIR, MATCHED_PATH, OUT_DIR, PRICES_PATH, REPO_ROOT,
    STORES, dump_json, find_catalog, is_promo, iter_items, load_json, parse_iso_date,
    prague_today,
)
from validate import validate_file  # noqa: E402

REPORT_PATH = os.path.join(OUT_DIR, "report.json")
HISTORY_PATH = os.path.join(OUT_DIR, "history.json")
PANEL_DATA_DIR = os.path.join(DOCS_DIR, "data")
OFFERS_CANDIDATES = (
    os.path.join(OUT_DIR, "offers.json"),
    os.path.join(OUT_DIR, "offers_raw.json"),
    os.path.join(OUT_DIR, "raw_offers.json"),
)
FETCH_REPORT_CANDIDATES = (
    os.path.join(OUT_DIR, "fetch_report.json"),
    os.path.join(OUT_DIR, "sources.json"),
    os.path.join(OUT_DIR, "fetch.json"),
)
HISTORY_MAX = 400
PANEL_MATCHED_MAX = 6000
PANEL_UNMATCHED_MAX = 3000
MIN_DEALS_WARN = 20
MIN_INGREDIENTS_WARN = 50


def _first_existing(paths) -> str | None:
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def _source_of(item: dict, fallback: str = "unknown") -> str:
    for k in ("source", "provider", "store"):
        v = item.get(k)
        if isinstance(v, str) and v:
            return v.lower()
    return fallback


def _list(obj: Any, *keys: str) -> list:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in keys:
            if isinstance(obj.get(k), list):
                return obj[k]
    return []


def offers_per_source() -> tuple[dict[str, int], str | None]:
    """Count raw offers per source from the fetch output (best effort)."""
    counts: dict[str, int] = defaultdict(int)
    path = _first_existing(OFFERS_CANDIDATES)
    if path:
        raw = load_json(path, [])
        if isinstance(raw, dict) and not any(isinstance(raw.get(k), list) for k in ("offers", "items", "results")) \
                and all(isinstance(v, list) for v in raw.values()):
            for src, items in raw.items():          # {"globus": [...], "lidl": [...]}
                counts[str(src).lower()] += len(items)
        else:
            for it in _list(raw, "offers", "items", "results"):
                if isinstance(it, dict):
                    counts[_source_of(it)] += 1
        return dict(sorted(counts.items())), os.path.relpath(path, REPO_ROOT)
    per_source_files = sorted(glob.glob(os.path.join(OUT_DIR, "offers", "*.json")))
    for p in per_source_files:
        raw = load_json(p, [])
        counts[os.path.splitext(os.path.basename(p))[0].lower()] += len(_list(raw, "offers", "items", "results"))
    if per_source_files:
        return dict(sorted(counts.items())), "out/offers/"
    return {}, None


def matched_summary(matched_path: str) -> dict:
    """Matched / review / unmatched / ignored rows from the mapper output.

    Accepts the embedded form (``review``/``unmatched`` lists next to ``items``),
    the mapper's split form (``out/unmatched.json`` with ``items`` carrying a
    ``status``) and a ``status`` field on the main items themselves.
    """
    raw = load_json(matched_path, None)
    items = list(iter_items(raw)) if raw is not None else []
    rest: list[tuple[str, dict]] = []          # (default status, item)
    if isinstance(raw, dict):
        rest += [("review", it) for it in _list(raw, "review")]
        rest += [("unmatched", it) for it in _list(raw, "unmatched")]
    if not rest:
        rest += [("review", it) for it in _list(load_json(os.path.join(OUT_DIR, "review.json"), []), "items", "review")]
        rest += [("unmatched", it) for it in
                 _list(load_json(os.path.join(OUT_DIR, "unmatched.json"), []), "items", "unmatched")]
    by_status: dict[str, int] = defaultdict(int)
    matched_items: list[dict] = []
    review: list[dict] = []
    unmatched: list[dict] = []
    ignored = 0
    for it in items:
        st = str(it.get("status", "matched")).lower()
        by_status[st] += 1
        if st in ("matched", "ok", "auto", "manual"):
            matched_items.append(it)
        else:
            rest.append((st, it))
    for default, it in rest:
        if not isinstance(it, dict):
            continue
        st = str(it.get("status") or default).lower()
        if st == "review":
            review.append(it)
        elif st in ("ignored", "ignore", "nonfood"):
            ignored += 1
        elif st in ("matched", "ok", "auto", "manual"):
            matched_items.append(it)
        else:
            unmatched.append(it)
    per_source: dict[str, int] = defaultdict(int)
    per_store: dict[str, int] = defaultdict(int)
    promo = 0
    for it in matched_items:
        per_source[_source_of(it)] += 1
        st = str(it.get("store", "")).lower()
        if st in STORES:
            per_store[st] += 1
        if is_promo(it):
            promo += 1
    stats = None
    if isinstance(raw, dict):
        stats = raw.get("counts") or raw.get("stats") or raw.get("sources")
    return {
        "path": os.path.relpath(matched_path, REPO_ROOT) if os.path.exists(matched_path) else None,
        "generated": raw.get("generated") if isinstance(raw, dict) else None,
        "matched": len(matched_items),
        "review": len(review),
        "unmatched": len(unmatched),
        "ignored": ignored,
        "byStatus": dict(sorted(by_status.items())),
        "matchedPerSource": dict(sorted(per_source.items())),
        "matchedPerStore": {s: per_store.get(s, 0) for s in STORES},
        "promoRows": promo,
        "mapperStats": stats if isinstance(stats, dict) else None,
        "_items": matched_items,
        "_review": review,
        "_unmatched": unmatched,
    }


def _trim_item(it: dict) -> dict:
    keep = ("ingredientId", "store", "source", "title", "czkPerKg", "price", "originalPrice",
            "validFrom", "validTo", "confidence", "score", "status", "club", "reason", "candidates",
            "suggestions", "query", "unit", "pack", "url", "count", "key")
    alias = {"ingredient_id": "ingredientId", "price_per_kg": "czkPerKg", "price_czk": "price",
             "original_price_czk": "originalPrice", "valid_from": "validFrom", "valid_to": "validTo",
             "source_url": "url", "pack_text": "pack", "sellUnitSizeText": "pack"}
    out = {k: it[k] for k in keep if k in it and it[k] is not None}
    for src, dst in alias.items():
        if dst not in out and it.get(src) is not None:
            out[dst] = it[src]
    if "pack" not in out and it.get("quantity") and it.get("unit"):
        out["pack"] = f"{it['quantity']:g} {it['unit']}"
    if "title" not in out:
        for k in ("name", "product"):
            if it.get(k):
                out["title"] = it[k]
                break
    out["promo"] = is_promo(it)
    return out


def table_summary(prices_path: str) -> dict:
    table = load_json(prices_path, None) or {}
    prices = table.get("czkPerKg") or {}
    deals = table.get("deals") or []
    per_store = {s: 0 for s in STORES}
    for per in prices.values():
        for s in per:
            if s in per_store:
                per_store[s] += 1
    deals_per_store = {s: 0 for s in STORES}
    for d in deals:
        if d.get("store") in deals_per_store:
            deals_per_store[d["store"]] += 1
    return {
        "path": os.path.relpath(prices_path, REPO_ROOT),
        "updated": table.get("updated"),
        "ingredients": len(prices),
        "pairs": sum(len(p) for p in prices.values()),
        "perStore": per_store,
        "deals": len(deals),
        "dealsPerStore": deals_per_store,
        "sizeBytes": os.path.getsize(prices_path) if os.path.exists(prices_path) else 0,
    }


def _fetch_sources(fetch_status: Any) -> dict[str, dict]:
    """Per-source status from the fetch report (``{"sources": {...}}`` or a list)."""
    out: dict[str, dict] = {}

    def from_list(lst):
        for v in lst:
            if isinstance(v, dict):
                k = v.get("id") or v.get("name") or v.get("source") or v.get("store")
                if k:
                    out[str(k).lower()] = v

    if isinstance(fetch_status, dict):
        src = fetch_status.get("sources") or fetch_status.get("providers") or fetch_status.get("stores")
        if isinstance(src, dict):
            for k, v in src.items():
                out[str(k).lower()] = v if isinstance(v, dict) else {"status": v}
        elif isinstance(src, list):
            from_list(src)
        elif fetch_status and all(isinstance(v, dict) for v in fetch_status.values()):
            out = {str(k).lower(): v for k, v in fetch_status.items()}
    elif isinstance(fetch_status, list):
        from_list(fetch_status)
    return out


def build_report(today: dt.date, matched_path: str = MATCHED_PATH,
                 prices_path: str = PRICES_PATH,
                 build_report_path: str = BUILD_REPORT_PATH,
                 started_at: dt.datetime | None = None) -> tuple[dict, dict]:
    now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    duration = None
    if started_at is not None:
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=dt.timezone.utc)
        duration = max(0, int((now_utc - started_at).total_seconds()))
    offers, offers_path = offers_per_source()
    m = matched_summary(matched_path)
    build = load_json(build_report_path, {}) or {}
    table = table_summary(prices_path)
    validation_errors = validate_file(prices_path, today=today, require_catalog=False)
    fetch_status_path = _first_existing(FETCH_REPORT_CANDIDATES)
    fetch_status = load_json(fetch_status_path, None) if fetch_status_path else None

    warnings: list[str] = []
    if not offers and offers_path is None:
        warnings.append("no fetch output found (out/offers.json) - offers per source unknown")
    for s in build.get("storesMissing") or []:
        warnings.append(f"store '{s}' had no matched rows today (carry-forward only)")
    for src, n in offers.items():
        if n == 0:
            warnings.append(f"source '{src}' returned 0 offers")
    if table["deals"] < MIN_DEALS_WARN:
        warnings.append(f"only {table['deals']} deals in prices.json")
    if table["ingredients"] < MIN_INGREDIENTS_WARN:
        warnings.append(f"only {table['ingredients']} ingredients priced")
    if build.get("droppedCount"):
        warnings.append(f"{build['droppedCount']} stale price(s) dropped (> 21 days)")
    if validation_errors:
        warnings.append(f"validation failed with {len(validation_errors)} error(s)")

    report = {
        "date": today.isoformat(),
        "generatedAt": now_utc.isoformat().replace("+00:00", "Z"),
        "startedAt": started_at.isoformat().replace("+00:00", "Z") if started_at else None,
        "durationSec": duration,
        "status": "error" if validation_errors else ("warning" if warnings else "ok"),
        "ok": not validation_errors,
        "warnings": warnings,
        "validation": {"ok": not validation_errors, "errors": validation_errors[:100]},
        "offers": {"perSource": offers, "total": sum(offers.values()), "path": offers_path},
        "matching": {k: v for k, v in m.items() if not k.startswith("_")},
        "build": build,
        "prices": table,
        "fetch": fetch_status,
        "fetchPath": os.path.relpath(fetch_status_path, REPO_ROOT) if fetch_status_path else None,
    }
    return report, m


def append_history(report: dict, history_path: str = HISTORY_PATH) -> list[dict]:
    history = load_json(history_path, []) or []
    if not isinstance(history, list):
        history = []
    entry = {
        "date": report["date"],
        "generatedAt": report["generatedAt"],
        "durationSec": report.get("durationSec"),
        "status": report["status"],
        "ok": report["ok"],
        "offers": report["offers"]["total"] if report["offers"]["perSource"] else None,
        "perSource": report["offers"]["perSource"],
        "matched": report["matching"]["matched"],
        "review": report["matching"]["review"],
        "unmatched": report["matching"]["unmatched"],
        "deals": report["prices"]["deals"],
        "priced": report["prices"]["ingredients"],
        "pairsPriced": report["prices"]["pairs"],
        "perStore": report["prices"]["perStore"],
        "stale": report["build"].get("staleCount", 0),
        "dropped": report["build"].get("droppedCount", 0),
        "warnings": len(report["warnings"]),
    }
    history = [h for h in history if isinstance(h, dict) and h.get("date") != entry["date"]]
    history.append(entry)
    history.sort(key=lambda h: h.get("date", ""))
    history = history[-HISTORY_MAX:]
    dump_json(history_path, history)
    return history


def health(report: dict, history: list[dict]) -> dict:
    """Panel health document (docs/admin reads ``run`` + ``sources``)."""
    per_store = report["prices"]["perStore"]
    last_ok = None
    for h in reversed(history):
        if h.get("ok"):
            last_ok = h.get("date")
            break
    fetch_sources = _fetch_sources(report.get("fetch"))
    ids = set(report["offers"]["perSource"]) | set(report["matching"]["matchedPerSource"]) | set(fetch_sources)
    sources: dict[str, dict] = {}
    for src in sorted(ids):
        f = dict(fetch_sources.get(src) or {})
        offers = report["offers"]["perSource"].get(src)
        if offers is None:
            offers = f.get("items", f.get("offers", f.get("count")))
        entry = {
            "label": f.get("label") or f.get("name") or src,
            "ok": f["ok"] if isinstance(f.get("ok"), bool) else (
                None if offers is None else offers > 0),
            "skipped": bool(f.get("skipped")) or str(f.get("status", "")).lower().startswith("skip"),
            "items": offers,
            "matched": report["matching"]["matchedPerSource"].get(src),
            "requests": f.get("requests"),
            "durationSec": f.get("durationSec", f.get("duration_sec", f.get("duration"))),
            "error": f.get("error") or f.get("message") or None,
            "robots": f.get("robots"),
            "fetchedAt": f.get("fetchedAt", f.get("fetched_at", f.get("at"))),
        }
        if f.get("status") is not None:
            entry["status"] = f["status"]
        sources[src] = entry
    return {
        "date": report["date"],
        "generatedAt": report["generatedAt"],
        "status": report["status"],
        "ok": report["ok"],
        "run": {
            "date": report["date"],
            "startedAt": report.get("startedAt"),
            "finishedAt": report["generatedAt"],
            "durationSec": report.get("durationSec"),
            "offers": report["offers"]["total"] if report["offers"]["perSource"] else None,
            "deals": report["prices"]["deals"],
            "priced": report["prices"]["ingredients"],
            "matched": report["matching"]["matched"],
            "review": report["matching"]["review"],
            "unmatched": report["matching"]["unmatched"],
            "ok": report["ok"],
        },
        "lastSuccessfulRun": last_ok,
        "pricesUpdated": report["prices"]["updated"],
        "sources": sources,
        "perStore": {s: {"priced": per_store.get(s, 0),
                         "deals": report["prices"]["dealsPerStore"].get(s, 0),
                         "rowsToday": (report["build"].get("rowsPerStore") or {}).get(s, 0),
                         "missingToday": s in (report["build"].get("storesMissing") or [])}
                     for s in STORES},
        "stale": report["build"].get("staleCount", 0),
        "dropped": report["build"].get("droppedCount", 0),
        "warnings": report["warnings"],
        "validation": report["validation"],
        "recentRuns": history[-14:],
    }


def publish_catalog(panel_dir: str) -> str | None:
    """Copy catalog/ingredients.json to docs/data/catalog/ for the panel (when changed)."""
    src = find_catalog()
    if not src:
        return None
    dst = os.path.join(panel_dir, "catalog", "ingredients.json")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not (os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False)):
        shutil.copyfile(src, dst)
    return dst


PANEL_LIST_MAX = 500


def _panel_report(report: dict) -> dict:
    """Panel copy of the report: long stale/dropped lists trimmed to PANEL_LIST_MAX."""
    out = dict(report)
    build = dict(report.get("build") or {})
    for key in ("stale", "dropped"):
        lst = build.get(key)
        if isinstance(lst, list) and len(lst) > PANEL_LIST_MAX:
            build[key] = lst[:PANEL_LIST_MAX]
            build[key + "Truncated"] = len(lst) - PANEL_LIST_MAX
    out["build"] = build
    return out


def write_all(today: dt.date | None = None, matched_path: str = MATCHED_PATH,
              prices_path: str = PRICES_PATH, report_path: str = REPORT_PATH,
              history_path: str = HISTORY_PATH, panel_dir: str = PANEL_DATA_DIR,
              build_report_path: str = BUILD_REPORT_PATH,
              started_at: dt.datetime | None = None, publish_catalog_copy: bool = True) -> dict:
    today = today or prague_today()
    report, m = build_report(today, matched_path, prices_path, build_report_path, started_at)
    dump_json(report_path, report)
    history = append_history(report, history_path)
    dump_json(os.path.join(panel_dir, "report.json"), _panel_report(report))
    dump_json(os.path.join(panel_dir, "history.json"), history)
    dump_json(os.path.join(panel_dir, "health.json"), health(report, history))
    dump_json(os.path.join(panel_dir, "unmatched.json"), {
        "date": today.isoformat(),
        "review": [_trim_item(i) for i in m["_review"] if isinstance(i, dict)][:PANEL_UNMATCHED_MAX],
        "unmatched": [_trim_item(i) for i in m["_unmatched"] if isinstance(i, dict)][:PANEL_UNMATCHED_MAX],
        "reviewTotal": len(m["_review"]),
        "unmatchedTotal": len(m["_unmatched"]),
    })
    dump_json(os.path.join(panel_dir, "matched.json"), {
        "date": today.isoformat(),
        "total": len(m["_items"]),
        "items": [_trim_item(i) for i in m["_items"]][:PANEL_MATCHED_MAX],
    }, indent=None)
    if publish_catalog_copy:
        publish_catalog(panel_dir)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write out/report.json, out/history.json and docs/data/*")
    ap.add_argument("--today", default=None)
    ap.add_argument("--matched", default=MATCHED_PATH)
    ap.add_argument("--prices", default=PRICES_PATH)
    ap.add_argument("--started-at", default=None, help="ISO datetime (UTC) when the run started")
    args = ap.parse_args(argv)
    started = None
    if args.started_at:
        try:
            started = dt.datetime.fromisoformat(args.started_at.replace("Z", "+00:00"))
        except ValueError:
            started = None
    report = write_all(parse_iso_date(args.today) if args.today else None, args.matched,
                       args.prices, started_at=started)
    print(f"report: status={report['status']} offers={report['offers']['total']} "
          f"matched={report['matching']['matched']} review={report['matching']['review']} "
          f"unmatched={report['matching']['unmatched']} ingredients={report['prices']['ingredients']} "
          f"deals={report['prices']['deals']}")
    for w in report["warnings"]:
        print(" ! " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
