#!/usr/bin/env python3
"""Makefile-style runner for the price pipeline.

    python run_all.py                 # every market: fetch -> mapper -> build -> ratings -> validate -> report
    python run_all.py --market cz     # one market (cz | sk | pl | de | at | all, comma-separated allowed)
    python run_all.py --skip fetch    # reuse out/offers.json (out/<market>/offers.json) from a previous run
    python run_all.py --only build,validate,report
    python run_all.py --sync-catalog "C:/AI/Jídlo"   # copy ingredients + fallbacks from the app
    python run_all.py --test          # run the unit tests

Steps are separate modules under ``pipeline/`` run as ``python -m pipeline.<name>``
(so package-relative imports work); the fetch and mapper scripts are discovered
by name (first existing file in ``STEP_SCRIPTS``), so the runner keeps working
if their module is renamed - adjust the list if needed.
A missing step script is reported and the run fails, unless the step is
skipped explicitly.

Markets (``pipeline/markets.py``): every step except ``ratings`` runs once per
selected market (``--market``, default ``all``), step-major so that the
workflow can still call one step at a time. A failing ``fetch`` of a market
other than cz is a warning (its build carries forward / publishes an empty
table); every other failure stops the run unless ``--keep-going``.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PIPELINE = os.path.join(ROOT, "pipeline")
sys.path.insert(0, ROOT)
from pipeline import markets  # noqa: E402

STEP_ORDER = ["fetch", "mapper", "build", "ratings", "validate", "report"]
STEP_SCRIPTS = {
    "fetch": ["fetch.py", "fetch_all.py", "fetch_offers.py", "fetcher.py"],
    "mapper": ["mapper.py", "map_offers.py", "match.py", "matcher.py", "map.py"],
    "build": ["build_prices.py"],
    "ratings": ["export_ratings.py"],   # Supabase recipe_ratings_summary -> docs/ratings.json (never fails the run)
    "validate": ["validate.py"],
    "report": ["report.py"],
}
PER_MARKET_STEPS = ("fetch", "mapper", "build", "validate", "report")


def find_script(step: str) -> str | None:
    for name in STEP_SCRIPTS[step]:
        p = os.path.join(PIPELINE, name)
        if os.path.exists(p):
            return p
    return None


def run_step(step: str, extra: list[str], label: str | None = None) -> int:
    script = find_script(step)
    tag = f"{step}:{label}" if label else step
    if script is None:
        print(f"[{tag}] no script found (looked for {', '.join(STEP_SCRIPTS[step])} in pipeline/)")
        return 2
    module = "pipeline." + os.path.splitext(os.path.basename(script))[0]
    print(f"[{tag}] python -m {module} {' '.join(extra)}".rstrip())
    t0 = time.time()
    env = dict(os.environ, PYTHONUTF8="1")
    rc = subprocess.call([sys.executable, "-m", module, *extra], cwd=ROOT, env=env)
    print(f"[{tag}] exit {rc} in {time.time() - t0:.1f}s")
    return rc


def sync_catalog(app_dir: str) -> int:
    """Copy the ingredient catalog and category fallbacks from the Flutter app."""
    src_ing = os.path.join(app_dir, "assets", "data", "ingredients.json")
    src_prices = os.path.join(app_dir, "assets", "data", "prices.json")
    if not os.path.exists(src_ing):
        print(f"ingredients.json not found at {src_ing}")
        return 1
    os.makedirs(os.path.join(ROOT, "catalog"), exist_ok=True)
    shutil.copyfile(src_ing, os.path.join(ROOT, "catalog", "ingredients.json"))
    print(f"copied {src_ing} -> catalog/ingredients.json")
    if os.path.exists(src_prices):
        with io.open(src_prices, "r", encoding="utf-8-sig") as f:
            fallback = json.load(f).get("categoryFallbackCzkPerKg", {})
        with io.open(os.path.join(ROOT, "catalog", "category_fallback.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            json.dump({"v": 1, "syncedFrom": "assets/data/prices.json",
                       "syncedOn": time.strftime("%Y-%m-%d"),
                       "categoryFallbackCzkPerKg": fallback}, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print("copied categoryFallbackCzkPerKg -> catalog/category_fallback.json")
    return 0


def step_args(step: str, market: str, args: argparse.Namespace, steps: list[str], skip: set[str],
              started_at: str) -> list[str]:
    extra: list[str] = []
    if step in PER_MARKET_STEPS:
        extra += ["--market", market]
    if args.today and step in ("build", "ratings", "validate", "report"):
        extra += ["--today", args.today]
    if step == "mapper":
        mp = markets.paths(market)
        extra += ["--offers", os.path.relpath(mp["offers"], ROOT), "--out-dir", os.path.relpath(mp["out_dir"], ROOT)]
    if step == "report" and "fetch" in steps and "fetch" not in skip:
        extra += ["--started-at", started_at]
    return extra


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--only", default=None, help="comma-separated steps to run")
    ap.add_argument("--skip", default="", help="comma-separated steps to skip")
    ap.add_argument("--market", default="all", help="cz | sk | pl | de | at | all, comma-separated (default all)")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD passed to build/validate/report")
    ap.add_argument("--sync-catalog", metavar="APP_DIR", default=None)
    ap.add_argument("--test", action="store_true", help="run unit tests and exit")
    ap.add_argument("--keep-going", action="store_true", help="do not stop on a failing step")
    args = ap.parse_args(argv)

    if args.sync_catalog:
        return sync_catalog(args.sync_catalog)
    if args.test:
        return subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT)

    try:
        selected = markets.parse_selection(args.market)
    except KeyError as e:
        print(str(e))
        return 2
    steps = args.only.split(",") if args.only else list(STEP_ORDER)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    unknown = [s for s in steps if s not in STEP_ORDER]
    if unknown:
        print("unknown step(s): " + ", ".join(unknown))
        return 2
    failed = 0
    warnings: list[str] = []
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for step in STEP_ORDER:
        if step not in steps or step in skip:
            continue
        runs = selected if step in PER_MARKET_STEPS else [markets.DEFAULT_MARKET]
        for market in runs:
            label = market if step in PER_MARKET_STEPS else None
            rc = run_step(step, step_args(step, market, args, steps, skip, started_at), label)
            if rc == 0:
                continue
            if step == "fetch" and market != markets.DEFAULT_MARKET:
                warnings.append(f"fetch:{market} exit {rc} (carry-forward only for this market)")
                continue
            failed = rc
            if not args.keep_going:
                print(f"stopping after failed step '{step}' (market {market})")
                return rc
    for w in warnings:
        print(" ! " + w)
    return failed


if __name__ == "__main__":
    sys.exit(main())
