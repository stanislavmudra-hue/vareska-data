#!/usr/bin/env python3
"""Export the public rating averages from Supabase to ``docs/ratings.json``.

Contract (app repo ``docs/BACKEND.md`` §3.3)::

    GET {SUPABASE_URL}/rest/v1/recipe_ratings_summary
        ?select=recipe_id,count,avg_taste,avg_difficulty,cook_again_pct&order=recipe_id
    headers: apikey: <anon>, Authorization: Bearer <anon>, Range: 0-999 (paged)

    docs/ratings.json = {"v": 1, "updated": "YYYY-MM-DD",
                         "ratings": {recipe_id: {"n": int, "taste": float,
                                                 "difficulty": float, "cookAgain": float|null}}}

Numbers are rounded to one decimal; ``cookAgain`` is the fraction 0–1 of
answered cook-again votes (``null`` when nobody answered). Rows with
``count`` below ``--min-count`` (default 1) are dropped.

Degrades instead of failing the daily workflow:

* HTTP 404 / PostgREST ``42P01`` (the migration has not been run yet) → an
  **empty** table is written and the exit code is 0;
* network errors, 401/403/5xx → the previous ``docs/ratings.json`` is kept
  (an empty one is written when none exists), exit code 0, warning printed;
* an unexpected payload shape → same as above.

Only the standard library is used (no ``requests``), so the step also runs in
a bare Python. Run as ``python -m pipeline.export_ratings`` or through
``python run_all.py --only ratings``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "ratings.json")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://ygogznerwabvlnwpgikx.supabase.co")
SUPABASE_ANON_KEY = os.environ.get(
    "SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inlnb2d6bmVyd2Fidmxud3BnaWt4Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk1MTk3MTIsImV4cCI6MjEwNTA5NTcxMn0.1PIKN3VJbmsOY05kRrTZtXROLl-bqoEH0eY3jSN_pic",
)
VIEW = "recipe_ratings_summary"
SELECT = "recipe_id,count,avg_taste,avg_difficulty,cook_again_pct"
PAGE_SIZE = 1000
TIMEOUT = 20
USER_AGENT = "Vareska-ratings/1.0 (+https://github.com/stanislavmudra-hue/vareska-data)"
# PostgREST error codes meaning "the view does not exist (yet)".
_MISSING_CODES = {"42P01", "PGRST205", "PGRST202"}


class ExportError(Exception):
    """Fetch failed in a way that should keep the previous file."""


class TableMissing(ExportError):
    """The migration has not been run: 404 / 42P01."""


class HttpResult:
    """Minimal response wrapper so tests can inject a fake ``http_get``."""

    def __init__(self, status: int, body: str, headers: Optional[dict] = None):
        self.status = status
        self.body = body
        self.headers = headers or {}


def _default_http_get(url: str, headers: dict) -> HttpResult:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (fixed https host)
            return HttpResult(resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            pass
        return HttpResult(e.code, body, dict(e.headers or {}))


def _error_code(body: str) -> str:
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        return ""
    return str(data.get("code", "")) if isinstance(data, dict) else ""


def fetch_summary(base_url: str = SUPABASE_URL, anon_key: str = SUPABASE_ANON_KEY,
                  http_get: Optional[Callable[[str, dict], HttpResult]] = None,
                  page_size: int = PAGE_SIZE) -> list[dict]:
    """Return all rows of ``recipe_ratings_summary`` (paged with ``Range``)."""
    http_get = http_get or _default_http_get
    url = f"{base_url.rstrip('/')}/rest/v1/{VIEW}?select={SELECT}&order=recipe_id"
    rows: list[dict] = []
    start = 0
    while True:
        headers = {
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
            "Accept": "application/json",
            "Range-Unit": "items",
            "Range": f"{start}-{start + page_size - 1}",
            "User-Agent": USER_AGENT,
        }
        try:
            res = http_get(url, headers)
        except (urllib.error.URLError, OSError, ValueError) as e:  # DNS, timeout, TLS…
            raise ExportError(f"network error: {e}") from e
        if res.status == 404 or (res.status in (400, 404) and _error_code(res.body) in _MISSING_CODES):
            raise TableMissing(f"HTTP {res.status}: {VIEW} not found (migration not run?)")
        if res.status == 416:  # Range not satisfiable: exactly a multiple of page_size rows
            break
        if res.status not in (200, 206):
            raise ExportError(f"HTTP {res.status}: {(res.body or '')[:200]}")
        try:
            page = json.loads(res.body or "[]")
        except ValueError as e:
            raise ExportError(f"invalid JSON from {VIEW}: {e}") from e
        if not isinstance(page, list):
            raise ExportError(f"unexpected payload from {VIEW}: {type(page).__name__}")
        rows.extend(r for r in page if isinstance(r, dict))
        if len(page) < page_size:
            break
        start += page_size
    return rows


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_ratings(rows: list[dict], min_count: int = 1) -> dict[str, dict]:
    """Map view rows to the ``ratings`` object of ratings.json."""
    out: dict[str, dict] = {}
    for r in rows:
        rid = str(r.get("recipe_id") or "").strip()
        n = r.get("count")
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if not rid or n < max(1, int(min_count)):
            continue
        taste = _num(r.get("avg_taste"))
        difficulty = _num(r.get("avg_difficulty"))
        if taste is None or difficulty is None:
            continue
        cook = _num(r.get("cook_again_pct"))
        out[rid] = {
            "n": n,
            "taste": round(taste, 1),
            "difficulty": round(difficulty, 1),
            "cookAgain": None if cook is None else round(min(max(cook, 0.0), 1.0), 2),
        }
    return dict(sorted(out.items()))


def make_table(ratings: dict[str, dict], today: dt.date) -> dict:
    return {"v": 1, "updated": today.isoformat(), "ratings": ratings}


def write_table(table: dict, path: str = OUT_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(table, f, ensure_ascii=False, indent=1)
        f.write("\n")


def load_existing(path: str = OUT_PATH) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with io.open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("ratings"), dict) else None


def export(today: dt.date, out_path: str = OUT_PATH, min_count: int = 1,
           http_get: Optional[Callable[[str, dict], HttpResult]] = None,
           base_url: str = SUPABASE_URL, anon_key: str = SUPABASE_ANON_KEY) -> dict:
    """Run the export; returns a small status dict (never raises for expected failures)."""
    status: dict[str, Any] = {"ok": True, "written": False, "rows": 0, "exported": 0, "note": ""}
    try:
        rows = fetch_summary(base_url, anon_key, http_get)
    except TableMissing as e:
        status.update(note=f"backend not deployed ({e}); writing empty table")
        write_table(make_table({}, today), out_path)
        status["written"] = True
        return status
    except ExportError as e:
        existing = load_existing(out_path)
        if existing is None:
            write_table(make_table({}, today), out_path)
            status.update(ok=False, written=True, note=f"{e}; no previous file, wrote empty table")
        else:
            status.update(ok=False, note=f"{e}; kept previous file ({len(existing['ratings'])} recipes, updated {existing.get('updated')})")
        return status
    ratings = build_ratings(rows, min_count)
    write_table(make_table(ratings, today), out_path)
    status.update(written=True, rows=len(rows), exported=len(ratings))
    return status


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Export recipe_ratings_summary → docs/ratings.json")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (default: today, Europe/Prague)")
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--min-count", type=int, default=int(os.environ.get("RATINGS_MIN_COUNT", "1")),
                    help="drop recipes with fewer ratings (privacy); default 1 = keep all")
    args = ap.parse_args(argv)
    if args.today:
        today = dt.date.fromisoformat(args.today)
    else:
        try:
            from zoneinfo import ZoneInfo
            today = dt.datetime.now(ZoneInfo("Europe/Prague")).date()
        except Exception:  # pragma: no cover - tzdata missing
            today = dt.date.today()
    st = export(today, args.out, args.min_count)
    rel = os.path.relpath(args.out, ROOT)
    if st["ok"]:
        if st["note"]:
            print(f"[ratings] {st['note']} -> {rel}")
        else:
            print(f"[ratings] {st['rows']} rows from {VIEW}, {st['exported']} exported -> {rel}")
    else:
        print(f"[ratings] WARNING: {st['note']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
