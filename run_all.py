#!/usr/bin/env python3
"""Makefile-style runner for the price pipeline.

    python run_all.py                 # fetch -> mapper -> build -> validate -> report
    python run_all.py --skip fetch    # reuse out/offers.json from a previous run
    python run_all.py --only build,validate,report
    python run_all.py --sync-catalog "C:/AI/Jídlo"   # copy ingredients + fallbacks from the app
    python run_all.py --test          # run the unit tests

Steps are separate modules under ``pipeline/`` run as ``python -m pipeline.<name>``
(so package-relative imports work); the fetch and mapper scripts are discovered
by name (first existing file in ``STEP_SCRIPTS``), so the runner keeps working
if their module is renamed - adjust the list if needed.
A missing step script is reported and the run fails, unless the step is
skipped explicitly.
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

STEP_ORDER = ["fetch", "mapper", "build", "validate", "report"]
STEP_SCRIPTS = {
    "fetch": ["fetch.py", "fetch_all.py", "fetch_offers.py", "fetcher.py"],
    "mapper": ["mapper.py", "map_offers.py", "match.py", "matcher.py", "map.py"],
    "build": ["build_prices.py"],
    "validate": ["validate.py"],
    "report": ["report.py"],
}


def find_script(step: str) -> str | None:
    for name in STEP_SCRIPTS[step]:
        p = os.path.join(PIPELINE, name)
        if os.path.exists(p):
            return p
    return None


def run_step(step: str, extra: list[str]) -> int:
    script = find_script(step)
    if script is None:
        print(f"[{step}] no script found (looked for {', '.join(STEP_SCRIPTS[step])} in pipeline/)")
        return 2
    module = "pipeline." + os.path.splitext(os.path.basename(script))[0]
    print(f"[{step}] python -m {module} {' '.join(extra)}".rstrip())
    t0 = time.time()
    env = dict(os.environ, PYTHONUTF8="1")
    rc = subprocess.call([sys.executable, "-m", module, *extra], cwd=ROOT, env=env)
    print(f"[{step}] exit {rc} in {time.time() - t0:.1f}s")
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--only", default=None, help="comma-separated steps to run")
    ap.add_argument("--skip", default="", help="comma-separated steps to skip")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD passed to build/validate/report")
    ap.add_argument("--sync-catalog", metavar="APP_DIR", default=None)
    ap.add_argument("--test", action="store_true", help="run unit tests and exit")
    ap.add_argument("--keep-going", action="store_true", help="do not stop on a failing step")
    args = ap.parse_args(argv)

    if args.sync_catalog:
        return sync_catalog(args.sync_catalog)
    if args.test:
        return subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT)

    steps = args.only.split(",") if args.only else list(STEP_ORDER)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    unknown = [s for s in steps if s not in STEP_ORDER]
    if unknown:
        print("unknown step(s): " + ", ".join(unknown))
        return 2
    failed = 0
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for step in STEP_ORDER:
        if step not in steps or step in skip:
            continue
        extra = ["--today", args.today] if args.today and step in ("build", "validate", "report") else []
        if step == "mapper":
            extra += ["--offers", os.path.join("out", "offers.json"), "--out-dir", "out"]
        if step == "report" and "fetch" in steps and "fetch" not in skip:
            extra += ["--started-at", started_at]
        rc = run_step(step, extra)
        if rc != 0:
            failed = rc
            if not args.keep_going:
                print(f"stopping after failed step '{step}'")
                return rc
    return failed


if __name__ == "__main__":
    sys.exit(main())
