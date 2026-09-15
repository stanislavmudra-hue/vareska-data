"""Static checks for docs/admin (the admin panel).

Runs with plain `python tests/test_admin_panel.py` or under pytest. Node.js is
used when available (JS syntax + behavioural checks of the normaliser and the
mapping merge); without node those checks are skipped.
"""
import html.parser
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = os.path.join(ROOT, "docs", "admin")
NODE = shutil.which("node")

# Node harness: stubs the browser globals admin.js touches at load time and
# exposes window.VareskaAdmin for the assertions below.
NODE_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const noop = () => {};
global.window = global;
global.document = { addEventListener: noop, getElementById: () => null, querySelectorAll: () => [], querySelector: () => null,
  createElement: () => ({ style: {}, classList: { add: noop, remove: noop, toggle: noop }, append: noop, setAttribute: noop, addEventListener: noop, querySelectorAll: () => [], querySelector: () => null }),
  createElementNS: () => ({ setAttribute: noop, append: noop, style: {} }) };
global.localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
global.location = { hash: '' };
global.history = { replaceState: noop };
global.fetch = () => Promise.reject(new Error('no network in tests'));
new Function(src)();
const A = window.VareskaAdmin;
const out = {};
out.fold = ['Máslo České 250 g', 'Kuřecí prsní řízky', '  Ďábelské  ražniči--extra ', 'ŠŤÁVA', 'straße', 'abc'].map(A.fold);
out.stem = ['brambory', 'rajčata', 'maslo', 'kure', 'mrkev', 'jablka', 'cibulemi', 'olej', 'kurecimi'].map((t) => A.stem(A.fold(t)));
out.tokens = A.normalizeTokens('Kuřecí prsní řízky 1 kg');
out.tokenMatches = [A.tokenMatches('kur', 'kureci'), A.tokenMatches('ku', 'kureci'), A.tokenMatches('ku', 'ku'), A.tokenMatches('kureci', 'kur')];
out.merged = A.applyPending({ v: 1, rules: [{ pattern: 're:^x', store: '*', ingredientId: 'ignore' }, { pattern: 'Máslo', store: 'lidl', ingredientId: 'wrong', note: 'old' }] },
  [{ key: 'lidl|maslo', match: 'maslo', store: 'lidl', action: 'map', ingredientId: 'maslo', title: 'Máslo' },
   { key: 'penny|pivo', match: 'pivo', store: 'penny', action: 'ignore', title: 'Pivo' }]);
out.mergedFromNull = A.applyPending(null, [{ key: 'k', match: 'm', store: null, action: 'ignore', title: 't' }]);
out.md = A.renderMarkdown('# H\n\n| a | b |\n|---|---|\n| 1 | <b>x</b> |\n\n- li **b**\n\n```\ncode\n```\n');
out.health = A.normalizeHealth({ run: { durationSec: 5, offers: 10 }, sources: { globus: { ok: true, items: 3 }, kaufland: { status: 'skipped' }, albert: { error: 'boom' } } });
out.unmatched = A.normalizeUnmatched({ unmatched: [{ title: 'A', store: 'lidl' }], review: [{ title: 'B', store: 'penny', ingredientId: 'maslo', confidence: 0.5, suggestions: ['x'] }] });
out.history = A.normalizeHistory([{ date: '2026-09-02', offers: 2 }, { date: '2026-09-01', items: 1 }]);
process.stdout.write(JSON.stringify(out));
"""


class TagBalance(html.parser.HTMLParser):
    VOID = {"meta", "link", "input", "br", "hr", "img", "source"}

    def __init__(self):
        super().__init__()
        self.stack, self.errors, self.ids = [], [], []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "id":
                self.ids.append(v)
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append((tag, self.getpos()))
        else:
            self.stack.pop()


class AdminPanelTests(unittest.TestCase):
    def test_files_exist_and_are_utf8_without_bom(self):
        for name in ("index.html", "admin.css", "admin.js", "README.md"):
            path = os.path.join(ADMIN, name)
            self.assertTrue(os.path.exists(path), name)
            with open(path, "rb") as f:
                head = f.read(3)
            self.assertNotEqual(head, b"\xef\xbb\xbf", f"{name} has a BOM")
            with open(path, encoding="utf-8") as f:
                f.read()  # must decode

    def test_html_is_balanced_and_ids_unique(self):
        with open(os.path.join(ADMIN, "index.html"), encoding="utf-8") as f:
            src = f.read()
        p = TagBalance()
        p.feed(src)
        self.assertEqual(p.errors, [])
        self.assertEqual(p.stack, [])
        self.assertEqual(len(p.ids), len(set(p.ids)), "duplicate ids")
        self.assertIn('<html lang="cs">', src)
        self.assertIn('<script src="admin.js">', src)
        self.assertIn('href="admin.css"', src)

    def test_js_references_only_existing_dom_ids(self):
        with open(os.path.join(ADMIN, "index.html"), encoding="utf-8") as f:
            ids = set(re.findall(r'\bid="([^"]+)"', f.read()))
        with open(os.path.join(ADMIN, "admin.js"), encoding="utf-8") as f:
            used = set(re.findall(r"\$\('([^']+)'\)", f.read()))
        self.assertEqual(used - ids, set(), "admin.js uses ids missing from index.html")

    def test_data_urls_are_relative(self):
        with open(os.path.join(ADMIN, "admin.js"), encoding="utf-8") as f:
            src = f.read()
        for key in ("prices", "health", "history", "unmatched", "qaSources", "qaStats"):
            m = re.search(key + r": '([^']+)'", src)
            self.assertIsNotNone(m, key)
            self.assertTrue(m.group(1).startswith("../"), m.group(1))
        insecure = set(re.findall(r"http://[\w.]+", src)) - {"http://www.w3.org", "http://localhost"}
        self.assertEqual(insecure, set(), "plain-http URLs in admin.js")
        self.assertIn("https://api.github.com", src)

    @unittest.skipUnless(NODE, "node not available")
    def test_js_syntax(self):
        subprocess.run([NODE, "--check", os.path.join(ADMIN, "admin.js")], check=True)

    @unittest.skipUnless(NODE, "node not available")
    def test_js_behaviour(self):
        harness = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_admin_harness.js")
        with open(harness, "w", encoding="utf-8") as f:
            f.write(NODE_HARNESS)
        try:
            r = subprocess.run([NODE, harness, os.path.join(ADMIN, "admin.js")], capture_output=True, text=True, encoding="utf-8", check=True)
        finally:
            os.remove(harness)
        out = json.loads(r.stdout)
        # fold(): mirrors text_normalizer.dart
        self.assertEqual(out["fold"], ["maslo ceske 250 g", "kureci prsni rizky", "dabelske raznici extra", "stava", "strasse", "abc"])
        # stem(): longest suffix first, never below 4 chars, tokens < 5 untouched
        self.assertEqual(out["stem"], ["brambor", "rajc", "masl", "kure", "mrkev", "jablk", "cibul", "olej", "kurecim"])
        self.assertEqual(out["tokens"], ["kurec", "prsn", "rizk", "kg"])
        self.assertEqual(out["tokenMatches"], [True, False, True, True])
        # applyPending(): pipeline/mapper.py contract {pattern, store|"*", ingredientId|"ignore", note};
        # same folded pattern + store is replaced, other rules are kept verbatim
        rules = out["merged"]["rules"]
        self.assertEqual([r["pattern"] for r in rules], ["re:^x", "maslo", "pivo"])
        self.assertEqual(rules[0], {"pattern": "re:^x", "store": "*", "ingredientId": "ignore"})
        self.assertEqual(rules[1]["ingredientId"], "maslo")
        self.assertEqual(rules[1]["store"], "lidl")
        self.assertEqual(rules[1]["title"], "Máslo")
        self.assertTrue(rules[1]["note"].startswith("panel 20"))
        self.assertEqual(rules[2]["ingredientId"], "ignore")
        self.assertEqual(rules[2]["store"], "penny")
        self.assertEqual(out["mergedFromNull"]["v"], 1)
        self.assertEqual(out["mergedFromNull"]["rules"][0]["store"], "*")
        self.assertEqual(out["mergedFromNull"]["rules"][0]["ingredientId"], "ignore")
        # renderMarkdown(): escapes HTML, renders table/list/code
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", out["md"])
        self.assertNotIn("<b>x</b>", out["md"])
        self.assertIn("<table>", out["md"])
        self.assertIn("<li>li <strong>b</strong></li>", out["md"])
        self.assertIn("<pre><code>code</code></pre>", out["md"])
        # normalisers
        h = out["health"]
        self.assertEqual(h["durationSec"], 5)
        by_id = {s["id"]: s for s in h["sources"]}
        self.assertTrue(by_id["globus"]["ok"])
        self.assertTrue(by_id["kaufland"]["skipped"])
        self.assertFalse(by_id["albert"]["ok"])
        u = out["unmatched"]
        self.assertEqual([x["status"] for x in u], ["unmatched", "review"])
        self.assertEqual(u[1]["suggestions"][0]["id"], "maslo")
        self.assertEqual(u[0]["key"], "a|lidl")
        self.assertEqual([r["date"] for r in out["history"]], ["2026-09-01", "2026-09-02"])
        self.assertEqual(out["history"][0]["offers"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
