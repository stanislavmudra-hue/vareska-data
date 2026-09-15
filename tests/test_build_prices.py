"""Unit tests for pipeline/build_prices.py (run: python -m unittest discover -s tests)."""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "pipeline"))

import build_prices as bp  # noqa: E402

TODAY = dt.date(2026, 9, 15)

CATALOG = {
    "v": 1,
    "ingredients": [
        {"id": "maslo", "nameCs": "máslo", "category": "dairy", "refPriceCzkPerKg": 200},
        {"id": "mleko_15", "nameCs": "mléko polotučné", "category": "dairy", "refPriceCzkPerKg": 25},
        {"id": "kureci_prsa", "nameCs": "kuřecí prsa", "category": "poultry", "refPriceCzkPerKg": 180},
        {"id": "vejce", "nameCs": "vejce", "category": "egg", "refPriceCzkPerKg": 90},
    ],
}


def item(ing, store, czk, promo=None, vf=None, vt=None, source=None, title=None, **extra):
    d = {"ingredientId": ing, "store": store, "czkPerKg": czk, "title": title or f"{ing} {store}"}
    if promo is not None:
        d["promo"] = promo
    if vf:
        d["validFrom"] = vf
    if vt:
        d["validTo"] = vt
    if source:
        d["source"] = source
    d.update(extra)
    return d


class BuildCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.catalog = os.path.join(self.root, "ingredients.json")
        bp.dump_json(self.catalog, CATALOG)
        self.fallback = os.path.join(self.root, "category_fallback.json")
        bp.dump_json(self.fallback, {"categoryFallbackCzkPerKg": {"dairy": 111}})
        self.matched = os.path.join(self.root, "matched.json")
        self.prices = os.path.join(self.root, "prices.json")
        self.last_seen = os.path.join(self.root, "last_seen.json")

    def tearDown(self):
        self.tmp.cleanup()

    def run_build(self, items, today=TODAY, previous=None):
        bp.dump_json(self.matched, items)
        if previous is not None:
            bp.dump_json(self.prices, previous)
        return bp.build(self.matched, self.prices, self.last_seen, self.catalog, self.fallback,
                        build_report_path=None, today=today)

    # -- czkPerKg -----------------------------------------------------------
    def test_median_of_regular_prices(self):
        table, rep = self.run_build([
            item("maslo", "lidl", 160, promo=False),
            item("maslo", "lidl", 180, promo=False),
            item("maslo", "lidl", 200, promo=False),
            item("maslo", "lidl", 120, promo=True, vt="2026-09-20"),   # promo ignored for regular
        ])
        self.assertEqual(table["czkPerKg"]["maslo"]["lidl"], 180)
        self.assertEqual(rep["pairsFromPromoOnly"], 0)
        self.assertEqual(table["updated"], "2026-09-15")
        self.assertEqual(table["v"], 1)

    def test_even_count_median(self):
        table, _ = self.run_build([item("maslo", "albert", 100, promo=False),
                                   item("maslo", "albert", 150, promo=False)])
        self.assertEqual(table["czkPerKg"]["maslo"]["albert"], 125)

    def test_deal_price_when_only_deal_exists(self):
        table, rep = self.run_build([item("maslo", "globus", 139.8, promo=True,
                                          vf="2026-09-09", vt="2026-09-15")])
        self.assertEqual(table["czkPerKg"]["maslo"]["globus"], 139.8)
        self.assertEqual(rep["pairsFromPromoOnly"], 1)

    def test_future_or_expired_rows_do_not_price(self):
        table, _ = self.run_build([
            item("maslo", "penny", 100, promo=True, vf="2026-09-16", vt="2026-09-22"),  # future
            item("maslo", "billa", 100, promo=True, vf="2026-09-01", vt="2026-09-14"),  # expired
        ])
        self.assertNotIn("maslo", table["czkPerKg"])
        # future deal is still published, expired one is not
        self.assertEqual([d["store"] for d in table["deals"]], ["penny"])

    def test_kupi_rows_are_promotions(self):
        table, _ = self.run_build([item("vejce", "tesco", 80, source="kupi", vt="2026-09-22")])
        self.assertEqual(len(table["deals"]), 1)
        self.assertEqual(table["deals"][0]["validFrom"], "2026-09-15")  # defaults to today

    def test_promo_from_original_price(self):
        self.assertTrue(bp.is_promo({"price": 39.9, "originalPrice": 49.9}))
        self.assertTrue(bp.is_promo({"price_czk": 39.9, "original_price_czk": 49.9}))
        self.assertFalse(bp.is_promo({"price": 39.9, "originalPrice": None}))
        self.assertFalse(bp.is_promo({"price": 39.9}))
        self.assertTrue(bp.is_promo({"discount": "akce"}))
        self.assertFalse(bp.is_promo({"is_promo": False, "source": "kupi"}))

    # -- deals --------------------------------------------------------------
    def test_deal_requires_valid_to_and_is_deduped_and_capped(self):
        rows = [item("maslo", "lidl", 150, promo=True)]  # no validTo -> not a deal
        rows += [item("maslo", "lidl", 150, promo=True, vt="2026-09-20")] * 2  # duplicate
        rows += [item("maslo", "lidl", p, promo=True, vt="2026-09-20", title=f"t{p}")
                 for p in (170, 160, 180, 190)]
        table, _ = self.run_build(rows)
        deals = [d for d in table["deals"] if d["store"] == "lidl"]
        self.assertEqual([d["czkPerKg"] for d in deals], [150, 160, 170])  # cheapest 3
        for d in deals:
            self.assertEqual(set(d), {"ingredientId", "store", "czkPerKg", "validFrom", "validTo", "title"})

    def test_deal_title_trimmed(self):
        table, _ = self.run_build([item("maslo", "lidl", 150, promo=True, vt="2026-09-20",
                                        title="  Máslo   " + "x" * 200)])
        self.assertLessEqual(len(table["deals"][0]["title"]), bp.TITLE_MAX_LEN)
        self.assertTrue(table["deals"][0]["title"].startswith("Máslo x"))

    # -- input tolerance -----------------------------------------------------
    def test_snake_case_aliases_and_wrapper(self):
        bp.dump_json(self.matched, {"items": [{
            "ingredient_id": "mleko_15", "store": "lidl", "price_per_kg": 24.9, "price_czk": 24.9,
            "is_promo": False, "valid_from": None, "valid_to": None, "title": "Mléko 1,5 %",
        }]})
        table, rep = bp.build(self.matched, self.prices, self.last_seen, self.catalog, self.fallback,
                              build_report_path=None, today=TODAY)
        self.assertEqual(table["czkPerKg"]["mleko_15"]["lidl"], 24.9)
        self.assertEqual(rep["rows"], 1)

    def test_bad_rows_are_skipped_and_counted(self):
        _, rep = self.run_build([
            item("neznama", "lidl", 100),
            item("maslo", "kosik", 100),
            item("maslo", "lidl", -5),
            item("maslo", "lidl", 9000),                 # outlier vs refPrice 200
            item("maslo", "lidl", 100, status="review"),
            {"store": "lidl", "czkPerKg": 100},
        ])
        self.assertEqual(rep["skipped"], {"unknown_ingredient": 1, "bad_store": 1, "bad_price": 1,
                                          "outlier": 1, "status:review": 1, "no_ingredient": 1})
        self.assertEqual(rep["rows"], 0)

    def test_missing_matched_file(self):
        table, rep = bp.build(os.path.join(self.root, "nope.json"), self.prices, self.last_seen,
                              self.catalog, self.fallback, build_report_path=None, today=TODAY)
        self.assertEqual(table["czkPerKg"], {})
        self.assertIn("warning", rep)

    # -- carry-forward -----------------------------------------------------------
    def test_carry_forward_within_21_days_then_drop(self):
        previous = {"v": 1, "updated": "2026-09-01", "czkPerKg": {
            "maslo": {"lidl": 170, "albert": 190},
            "vejce": {"penny": 80},
        }, "deals": [], "categoryFallbackCzkPerKg": {}}
        bp.dump_json(self.last_seen, {"maslo": {"lidl": "2026-09-14", "albert": "2026-08-01"}})
        table, rep = self.run_build([item("maslo", "lidl", 160, promo=False),
                                     item("vejce", "lidl", 85, promo=False)], previous=previous)
        self.assertEqual(table["czkPerKg"]["maslo"]["lidl"], 160)     # fresh
        self.assertNotIn("albert", table["czkPerKg"]["maslo"])         # 45 days old -> dropped
        self.assertEqual(table["czkPerKg"]["vejce"]["penny"], 80)      # unknown last seen -> previous.updated (14 d)
        self.assertEqual(rep["staleCount"], 1)
        self.assertEqual(rep["droppedCount"], 1)
        self.assertEqual(rep["stale"][0]["reason"], "store_missing")
        seen = bp.load_json(self.last_seen)
        self.assertEqual(seen["maslo"]["lidl"], "2026-09-15")
        self.assertEqual(seen["vejce"]["penny"], "2026-09-01")
        self.assertNotIn("albert", seen["maslo"])

    def test_carry_forward_boundary_exactly_21_days(self):
        previous = {"v": 1, "updated": "2026-08-25", "czkPerKg": {"maslo": {"billa": 200}}, "deals": []}
        table, rep = self.run_build([], previous=previous)
        self.assertEqual(table["czkPerKg"]["maslo"]["billa"], 200)
        os.remove(self.last_seen)
        previous["updated"] = "2026-08-24"
        table, rep = self.run_build([], previous=previous)
        self.assertNotIn("maslo", table["czkPerKg"])

    def test_previous_deals_kept_only_for_missing_stores(self):
        previous = {"v": 1, "updated": "2026-09-14", "czkPerKg": {}, "deals": [
            {"ingredientId": "maslo", "store": "kaufland", "czkPerKg": 150, "validFrom": "2026-09-10",
             "validTo": "2026-09-16", "title": "old kaufland"},
            {"ingredientId": "maslo", "store": "kaufland", "czkPerKg": 150, "validFrom": "2026-09-01",
             "validTo": "2026-09-14", "title": "expired"},
            {"ingredientId": "maslo", "store": "lidl", "czkPerKg": 150, "validFrom": "2026-09-10",
             "validTo": "2026-09-16", "title": "old lidl"},
        ]}
        table, rep = self.run_build([item("maslo", "lidl", 160, promo=False)], previous=previous)
        titles = [d["title"] for d in table["deals"]]
        self.assertEqual(titles, ["old kaufland"])
        self.assertEqual(rep["dealsCarried"], 1)

    # -- fallbacks ------------------------------------------------------------
    def test_category_fallback_merged_with_defaults(self):
        table, _ = self.run_build([])
        fb = table["categoryFallbackCzkPerKg"]
        self.assertEqual(fb["dairy"], 111)
        self.assertEqual(fb["meat"], 240)
        self.assertEqual(set(fb), set(bp.CATEGORIES))

    def test_output_file_is_utf8_without_bom(self):
        self.run_build([item("maslo", "lidl", 150, promo=True, vt="2026-09-20", title="Máslo čerstvé")])
        with open(self.prices, "rb") as f:
            raw = f.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("Máslo čerstvé".encode("utf-8"), raw)
        self.assertNotIn(b"\r\n", raw)


class HelperCase(unittest.TestCase):
    def test_parse_iso_date(self):
        self.assertEqual(bp.parse_iso_date("2026-09-15"), dt.date(2026, 9, 15))
        self.assertEqual(bp.parse_iso_date("2026-09-15T23:59:59+02:00"), dt.date(2026, 9, 15))
        self.assertIsNone(bp.parse_iso_date("15.9.2026"))
        self.assertIsNone(bp.parse_iso_date(None))

    def test_default_fallback_matches_app(self):
        self.assertEqual(len(bp.DEFAULT_CATEGORY_FALLBACK), 21)
        self.assertEqual(bp.DEFAULT_CATEGORY_FALLBACK["spice"], 900)


if __name__ == "__main__":
    unittest.main()
