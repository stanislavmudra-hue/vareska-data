"""Unit tests for pipeline/validate.py and pipeline/report.py."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "pipeline"))

import build_prices as bp  # noqa: E402
import report  # noqa: E402
import validate  # noqa: E402

TODAY = dt.date(2026, 9, 15)
SCHEMA = os.path.join(REPO, "docs", "prices.schema.json")

GOOD = {
    "v": 1,
    "updated": "2026-09-15",
    "czkPerKg": {"maslo": {"lidl": 159.6, "globus": 139.8}, "vejce": {"penny": 80}},
    "deals": [
        {"ingredientId": "maslo", "store": "globus", "czkPerKg": 139.8,
         "validFrom": "2026-09-09", "validTo": "2026-09-15", "title": "Máslo 250 g"},
    ],
    "categoryFallbackCzkPerKg": dict(bp.DEFAULT_CATEGORY_FALLBACK),
}
IDS = {"maslo", "vejce"}


class SchemaCase(unittest.TestCase):
    def setUp(self):
        self.schema = bp.load_json(SCHEMA)
        self.assertIsNotNone(self.schema)

    def errs(self, table):
        return validate.schema_errors(table, self.schema)

    def mini_errs(self, table):
        out: list[str] = []
        validate._mini_validate(table, self.schema, self.schema, "$", out)
        return out

    def test_good_table_passes(self):
        self.assertEqual(self.errs(GOOD), [])
        self.assertEqual(self.mini_errs(GOOD), [])

    def test_schema_catches_bad_store_price_date(self):
        bad = copy.deepcopy(GOOD)
        bad["czkPerKg"]["maslo"]["kosik"] = 10
        bad["czkPerKg"]["vejce"]["penny"] = 0
        bad["deals"][0]["validTo"] = "15.9.2026"
        bad["deals"][0]["extra"] = 1
        bad["v"] = 2
        for errs in ("\n".join(self.errs(bad)), "\n".join(self.mini_errs(bad))):
            self.assertIn("kosik", errs)
            self.assertRegex(errs, r"penny.*\b0\b")
            self.assertIn("15.9.2026", errs)
            self.assertIn("extra", errs)
            self.assertRegex(errs, r"(?m)^(\$\.)?v: ")

    def test_mini_validator_agrees_on_required(self):
        bad = {"v": 1, "updated": "2026-09-15"}
        for errs in (self.errs(bad), self.mini_errs(bad)):
            self.assertTrue(any("czkPerKg" in e for e in errs))
            self.assertTrue(any("deals" in e for e in errs))


class RulesCase(unittest.TestCase):
    def test_good(self):
        self.assertEqual(validate.rule_errors(GOOD, IDS, TODAY), [])

    def test_unknown_ingredient_and_store(self):
        bad = copy.deepcopy(GOOD)
        bad["czkPerKg"]["cosi"] = {"lidl": 10}
        bad["deals"][0]["store"] = "kosik"
        errs = validate.rule_errors(bad, IDS, TODAY)
        self.assertTrue(any("unknown ingredient id" in e for e in errs))
        self.assertTrue(any("unknown store" in e for e in errs))

    def test_dates(self):
        bad = copy.deepcopy(GOOD)
        bad["deals"][0]["validTo"] = "2026-09-14"                      # expired
        bad["deals"].append(dict(GOOD["deals"][0], validFrom="2026-09-20", validTo="2026-09-19"))
        bad["updated"] = "2026-09-16"                                   # future
        errs = validate.rule_errors(bad, IDS, TODAY)
        self.assertTrue(any("expired" in e for e in errs))
        self.assertTrue(any("after validTo" in e for e in errs))
        self.assertTrue(any("future" in e for e in errs))

    def test_too_many_deals(self):
        bad = copy.deepcopy(GOOD)
        bad["deals"] = [GOOD["deals"][0]] * 5001
        errs = validate.rule_errors(bad, IDS, TODAY)
        self.assertTrue(any("> 5000" in e for e in errs))

    def test_positive_prices(self):
        bad = copy.deepcopy(GOOD)
        bad["czkPerKg"]["maslo"]["lidl"] = -1
        bad["czkPerKg"]["vejce"]["penny"] = "80"
        bad["categoryFallbackCzkPerKg"]["dairy"] = 0
        errs = validate.rule_errors(bad, IDS, TODAY)
        self.assertEqual(sum("positive" in e for e in errs), 3)


class CliCase(unittest.TestCase):
    def test_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = os.path.join(tmp, "ingredients.json")
            bp.dump_json(catalog, {"ingredients": [{"id": i} for i in IDS]})
            good = os.path.join(tmp, "good.json")
            bp.dump_json(good, GOOD)
            self.assertEqual(validate.main([good, "--catalog", catalog, "--today", "2026-09-15"]), 0)
            bad = os.path.join(tmp, "bad.json")
            bp.dump_json(bad, dict(GOOD, deals=[dict(GOOD["deals"][0], czkPerKg=-1)]))
            self.assertEqual(validate.main([bad, "--catalog", catalog, "--today", "2026-09-15"]), 1)
            self.assertEqual(validate.main([os.path.join(tmp, "missing.json"), "--catalog", catalog]), 1)


class ReportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.matched = os.path.join(self.root, "matched.json")
        self.prices = os.path.join(self.root, "prices.json")
        self.report = os.path.join(self.root, "report.json")
        self.history = os.path.join(self.root, "history.json")
        self.panel = os.path.join(self.root, "data")
        self.build_report = os.path.join(self.root, "build_report.json")
        bp.dump_json(self.prices, GOOD)
        bp.dump_json(self.build_report, {"staleCount": 2, "stale": [], "droppedCount": 0,
                                         "storesMissing": ["tesco"], "rowsPerStore": {"lidl": 3}})
        bp.dump_json(self.matched, {
            "items": [
                {"ingredientId": "maslo", "store": "lidl", "source": "lidl", "czkPerKg": 159.6, "title": "Máslo"},
                {"ingredientId": "maslo", "store": "globus", "source": "globus", "czkPerKg": 139.8,
                 "originalPrice": 60, "price": 35, "validTo": "2026-09-15", "title": "Máslo 250 g"},
                {"store": "billa", "source": "billa-text", "title": "Cosi", "status": "unmatched"},
            ],
            "review": [{"store": "lidl", "source": "lidl", "title": "Sýr?", "candidates": ["eidam"]}],
            "unmatched": [{"store": "penny", "source": "penny", "title": "Tyčinka"}],
        })

    def tearDown(self):
        self.tmp.cleanup()

    def run_report(self, today=TODAY):
        return report.write_all(today, self.matched, self.prices, self.report, self.history,
                                self.panel, self.build_report, publish_catalog_copy=False)

    def test_report_and_panel_files(self):
        rep = self.run_report()
        self.assertEqual(rep["matching"]["matched"], 2)
        self.assertEqual(rep["matching"]["review"], 1)
        self.assertEqual(rep["matching"]["unmatched"], 2)
        self.assertEqual(rep["matching"]["promoRows"], 1)
        self.assertEqual(rep["prices"]["ingredients"], 2)
        self.assertEqual(rep["prices"]["deals"], 1)
        self.assertTrue(rep["validation"]["ok"])
        self.assertIn("store 'tesco' had no matched rows today (carry-forward only)", rep["warnings"])
        for name in ("report.json", "history.json", "unmatched.json", "health.json", "matched.json"):
            self.assertTrue(os.path.exists(os.path.join(self.panel, name)), name)
        health = bp.load_json(os.path.join(self.panel, "health.json"))
        self.assertEqual(health["run"]["priced"], 2)
        self.assertEqual(health["sources"]["globus"]["matched"], 1)
        self.assertEqual(health["perStore"]["tesco"]["missingToday"], True)
        unmatched = bp.load_json(os.path.join(self.panel, "unmatched.json"))
        self.assertEqual(len(unmatched["review"]), 1)
        self.assertEqual(len(unmatched["unmatched"]), 2)
        self.assertEqual(unmatched["review"][0]["candidates"], ["eidam"])
        matched = bp.load_json(os.path.join(self.panel, "matched.json"))
        self.assertEqual(matched["total"], 2)
        self.assertTrue(matched["items"][1]["promo"])

    def test_history_replaces_same_day_and_appends(self):
        self.run_report()
        self.run_report()
        hist = bp.load_json(self.history)
        self.assertEqual(len(hist), 1)
        self.run_report(today=dt.date(2026, 9, 16))
        hist = bp.load_json(self.history)
        self.assertEqual([h["date"] for h in hist], ["2026-09-15", "2026-09-16"])
        self.assertEqual(hist[0]["priced"], 2)
        self.assertEqual(hist[0]["deals"], 1)
        self.assertIn("perSource", hist[0])

    def test_validation_failure_is_reported_not_raised(self):
        bad = copy.deepcopy(GOOD)
        bad["deals"][0]["czkPerKg"] = -1
        bp.dump_json(self.prices, bad)
        rep = self.run_report()
        self.assertEqual(rep["status"], "error")
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["validation"]["errors"])


if __name__ == "__main__":
    unittest.main()
