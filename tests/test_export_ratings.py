"""Unit tests for pipeline/export_ratings.py (no network: fake http_get)."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pipeline import export_ratings as er  # noqa: E402

TODAY = dt.date(2026, 9, 16)
ROWS = [
    {"recipe_id": "cz_hovezi_gulas", "count": 23, "avg_taste": "4.57", "avg_difficulty": "2.26", "cook_again_pct": "0.87"},
    {"recipe_id": "u:6f1c0a0e-1111-4bbb-8ccc-000000000001", "count": 4, "avg_taste": 4.0, "avg_difficulty": 3.5, "cook_again_pct": None},
    {"recipe_id": "it_tiramisu", "count": 1, "avg_taste": 5, "avg_difficulty": 1, "cook_again_pct": 1},
]


class FakeHttp:
    """Scripted responses; records the requests made."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return er.HttpResult(status, body if isinstance(body, str) else json.dumps(body))


class BuildRatingsTests(unittest.TestCase):
    def test_shape_and_rounding(self):
        r = er.build_ratings(ROWS)
        self.assertEqual(sorted(r), sorted([x["recipe_id"] for x in ROWS]))
        self.assertEqual(r["cz_hovezi_gulas"], {"n": 23, "taste": 4.6, "difficulty": 2.3, "cookAgain": 0.87})
        self.assertEqual(r["u:6f1c0a0e-1111-4bbb-8ccc-000000000001"], {"n": 4, "taste": 4.0, "difficulty": 3.5, "cookAgain": None})
        self.assertEqual(r["it_tiramisu"]["cookAgain"], 1.0)

    def test_min_count_and_bad_rows(self):
        rows = ROWS + [{"recipe_id": "", "count": 9, "avg_taste": 1, "avg_difficulty": 1},
                       {"recipe_id": "x", "count": "abc", "avg_taste": 1, "avg_difficulty": 1},
                       {"recipe_id": "y", "count": 2, "avg_taste": None, "avg_difficulty": 1}]
        self.assertEqual(sorted(er.build_ratings(rows, min_count=3)), ["cz_hovezi_gulas", "u:6f1c0a0e-1111-4bbb-8ccc-000000000001"])
        self.assertEqual(sorted(er.build_ratings(rows, min_count=5)), ["cz_hovezi_gulas"])
        self.assertEqual(sorted(er.build_ratings(rows, min_count=1)), sorted([x["recipe_id"] for x in ROWS]))

    def test_table_contract(self):
        t = er.make_table(er.build_ratings(ROWS), TODAY)
        self.assertEqual(t["v"], 1)
        self.assertEqual(t["updated"], "2026-09-16")
        self.assertIsInstance(t["ratings"], dict)
        json.dumps(t)  # serialisable


class FetchTests(unittest.TestCase):
    def test_headers_and_single_page(self):
        http = FakeHttp([(200, ROWS)])
        rows = er.fetch_summary("https://x.supabase.co/", "KEY", http)
        self.assertEqual(len(rows), 3)
        url, headers = http.calls[0]
        self.assertTrue(url.startswith("https://x.supabase.co/rest/v1/recipe_ratings_summary?select="))
        self.assertIn("order=recipe_id", url)
        self.assertEqual(headers["apikey"], "KEY")
        self.assertEqual(headers["Authorization"], "Bearer KEY")
        self.assertEqual(headers["Range"], "0-999")

    def test_pagination(self):
        page1 = [{"recipe_id": f"cz_r{i}", "count": 1, "avg_taste": 3, "avg_difficulty": 3} for i in range(2)]
        page2 = [{"recipe_id": "cz_last", "count": 1, "avg_taste": 3, "avg_difficulty": 3}]
        http = FakeHttp([(206, page1), (200, page2)])
        rows = er.fetch_summary("https://x.supabase.co", "K", http, page_size=2)
        self.assertEqual([r["recipe_id"] for r in rows], ["cz_r0", "cz_r1", "cz_last"])
        self.assertEqual(http.calls[1][1]["Range"], "2-3")

    def test_404_is_table_missing(self):
        http = FakeHttp([(404, {"code": "PGRST205", "message": "Could not find the table"})])
        with self.assertRaises(er.TableMissing):
            er.fetch_summary("https://x.supabase.co", "K", http)

    def test_other_errors(self):
        with self.assertRaises(er.ExportError):
            er.fetch_summary("https://x.supabase.co", "K", FakeHttp([(401, {"message": "Invalid API key"})]))
        with self.assertRaises(er.ExportError):
            er.fetch_summary("https://x.supabase.co", "K", FakeHttp([OSError("dns")]))
        with self.assertRaises(er.ExportError):
            er.fetch_summary("https://x.supabase.co", "K", FakeHttp([(200, "not json")]))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "docs", "ratings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        with open(self.out, encoding="utf-8") as f:
            raw = f.read()
        self.assertFalse(raw.startswith("﻿"))
        self.assertTrue(raw.endswith("\n"))
        return json.loads(raw)

    def test_happy_path(self):
        st = er.export(TODAY, self.out, 1, FakeHttp([(200, ROWS)]))
        self.assertTrue(st["ok"] and st["written"])
        self.assertEqual(st["exported"], 3)
        t = self.read()
        self.assertEqual(t["updated"], "2026-09-16")
        self.assertEqual(t["ratings"]["cz_hovezi_gulas"]["n"], 23)

    def test_404_writes_empty_table(self):
        st = er.export(TODAY, self.out, 1, FakeHttp([(404, {"code": "PGRST205"})]))
        self.assertTrue(st["ok"] and st["written"])
        self.assertEqual(self.read(), {"v": 1, "updated": "2026-09-16", "ratings": {}})

    def test_network_error_keeps_previous_file(self):
        er.export(TODAY, self.out, 1, FakeHttp([(200, ROWS)]))
        st = er.export(dt.date(2026, 9, 17), self.out, 1, FakeHttp([OSError("offline")]))
        self.assertFalse(st["ok"])
        self.assertFalse(st["written"])
        t = self.read()
        self.assertEqual(t["updated"], "2026-09-16")
        self.assertEqual(len(t["ratings"]), 3)

    def test_network_error_without_previous_writes_empty(self):
        st = er.export(TODAY, self.out, 1, FakeHttp([(503, "down")]))
        self.assertFalse(st["ok"])
        self.assertTrue(st["written"])
        self.assertEqual(self.read()["ratings"], {})

    def test_main_exit_code_is_zero_on_failure(self):
        real = er._default_http_get
        er._default_http_get = FakeHttp([OSError("offline"), (404, {"code": "PGRST205"})])
        try:
            self.assertEqual(er.main(["--today", "2026-09-16", "--out", self.out]), 0)
            self.assertEqual(self.read()["ratings"], {})  # no previous file → empty table
            self.assertEqual(er.main(["--today", "2026-09-17", "--out", self.out]), 0)
            self.assertEqual(self.read()["updated"], "2026-09-17")
        finally:
            er._default_http_get = real


class RunAllTests(unittest.TestCase):
    def test_step_registered_after_build(self):
        sys.path.insert(0, REPO)
        import run_all
        self.assertIn("ratings", run_all.STEP_ORDER)
        self.assertEqual(run_all.STEP_ORDER.index("ratings"), run_all.STEP_ORDER.index("build") + 1)
        self.assertTrue(run_all.find_script("ratings").endswith("export_ratings.py"))

    def test_workflow_has_step(self):
        with open(os.path.join(REPO, ".github", "workflows", "update.yml"), encoding="utf-8") as f:
            wf = f.read()
        self.assertIn("run_all.py --only ratings", wf)
        self.assertLess(wf.index("--only build"), wf.index("--only ratings"))
        self.assertLess(wf.index("--only ratings"), wf.index("--only validate"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
