"""Static + behavioural checks for the Supabase-backed web pages:
docs/admin (Moderace tab: moderation.js, config.js) and docs/auth (reset, landing).

Runs with plain `python tests/test_moderation_auth.py` or under pytest. Node.js
(when available) checks JS syntax and exercises the pure helpers exposed on
window.VareskaModeration / window.VareskaReset / window.VareskaLanding.
"""
import html.parser
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
ADMIN = os.path.join(DOCS, "admin")
AUTH = os.path.join(DOCS, "auth")
NODE = shutil.which("node")
SUPABASE_URL = "https://ygogznerwabvlnwpgikx.supabase.co"
CDN = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"

# Node harness: minimal browser globals, no supabase library (the modules must
# load without it), then call the exported pure helpers.
NODE_HARNESS = r"""
const fs = require('fs');
const noop = () => {};
global.window = global;
global.document = { addEventListener: noop, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
global.localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
global.location = { hash: '', search: '', pathname: '/x', replace: noop };
global.history = { replaceState: noop };
global.fetch = () => Promise.reject(new Error('no network in tests'));
for (const f of process.argv.slice(2)) new Function(fs.readFileSync(f, 'utf8'))();
const M = window.VareskaModeration, R = window.VareskaReset, L = window.VareskaLanding;
const out = { config: window.VareskaConfig };
out.photo = [M.photoUrl('abc/def.jpg', '2026-09-16T10:00:00Z'), M.photoUrl(null), M.photoUrl('a b/c.jpg')];
out.errors = [
  { code: 'PGRST205', message: 'Could not find the table' }, { code: '42P01' }, { status: 404, message: 'Not found' },
  new TypeError('Failed to fetch'), Object.assign(new Error('timeout'), { name: 'TimeoutError' }),
  { code: 'invalid_credentials', message: 'Invalid login credentials' }, { code: '42501', message: 'permission denied' },
  { status: 401 }, { message: 'status_not_allowed' }, { message: 'weird' },
].map((e) => M.classifyError(e).kind);
out.errorTexts = [M.classifyError({ code: 'PGRST205' }).text, M.classifyError(new TypeError('Failed to fetch')).text];
const names = { cibule: 'cibule', maslo: 'máslo' };
out.ings = [
  M.fmtIngredient({ ingredientId: 'cibule', grams: 120 }, (id) => names[id]),
  M.fmtIngredient({ ingredientId: 'maslo', grams: 50, qty: 2, unit: 'lžíce', note: 'změklé', optional: true }, (id) => names[id]),
  M.fmtIngredient({ ingredientId: 'unknown_x', grams: 10 }, (id) => names[id]),
  M.fmtIngredient(null),
];
out.recipe = M.normalizeRecipe({ id: '1', title: '  Guláš ', status: 'pending', ingredients: '[{"ingredientId":"cibule","grams":1}]', steps: null, tags: null, author: { username: 'pepa' } });
out.recipeArr = M.normalizeRecipe({ author: [{ username: 'a' }], title: '' }).authorName;
out.rank = M.rankRatings([
  { recipe_id: 'a', count: 2, avg_taste: '4.90', avg_difficulty: '2', cook_again_pct: null },
  { recipe_id: 'b', count: 5, avg_taste: '4.50', avg_difficulty: '3', cook_again_pct: '0.8' },
  { recipe_id: 'c', count: 7, avg_taste: '4.50', avg_difficulty: '3', cook_again_pct: '0.5' },
  { recipe_id: '', count: 9, avg_taste: '5' },
], 3).map((r) => r.recipeId + ':' + r.count + ':' + r.cookAgain);
out.rankAll = M.rankRatings([{ recipe_id: 'a', count: 1, avg_taste: 1 }], 1).length;
// reset.js
out.params = R.parseAuthParams('#access_token=AT&refresh_token=RT&type=recovery', '?x=1');
out.flows = [
  R.detectFlow({ access_token: 'a', refresh_token: 'b' }), R.detectFlow({ access_token: 'a' }), R.detectFlow({ token_hash: 't', type: 'recovery' }),
  R.detectFlow({ code: 'c' }), R.detectFlow({ error: 'access_denied', error_code: 'otp_expired' }), R.detectFlow({}),
  R.detectFlow({ code: 'c', error_code: 'otp_expired' }),
];
out.pw = [R.validatePassword('short', 'short'), R.validatePassword('longenough1', 'different1'), R.validatePassword('longenough1', 'longenough1')];
out.resetErr = [
  R.errorText({ error_code: 'otp_expired', error_description: 'Email link is invalid or has expired' }),
  R.errorText({ code: 'weak_password', message: 'Password should be at least 6 characters' }),
  R.errorText({ code: 'same_password', message: 'New password should be different from the old password.' }),
  R.errorText(new Error('invalid request: both auth code and code verifier should be non-empty')),
  R.errorText(Object.assign(new Error('x'), { name: 'TimeoutError' })),
];
// landing.js
out.landing = [
  L.classify(L.params('#access_token=a&type=recovery', '')), L.classify(L.params('', '?code=abc')), L.classify(L.params('', '?token_hash=t&type=recovery')),
  L.classify(L.params('#error=access_denied&error_code=otp_expired', '')), L.classify(L.params('#access_token=a&type=signup', '')), L.classify(L.params('', '')),
];
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


def read(path):
    with open(path, "rb") as f:
        raw = f.read()
    assert not raw.startswith(b"\xef\xbb\xbf"), path + " has a BOM"
    return raw.decode("utf-8")


def check_html(tc, path):
    src = read(path)
    p = TagBalance()
    p.feed(src)
    tc.assertEqual(p.errors, [], path)
    tc.assertEqual(p.stack, [], path)
    tc.assertEqual(len(p.ids), len(set(p.ids)), "duplicate ids in " + path)
    tc.assertIn('<html lang="cs">', src)
    tc.assertIn('<meta charset="utf-8">', src)
    return src, set(p.ids)


def ids_used(js_src):
    return set(re.findall(r"\$\('([^']+)'\)", js_src))


class ConfigTests(unittest.TestCase):
    def test_config_files_identical_and_public(self):
        a = read(os.path.join(ADMIN, "config.js"))
        b = read(os.path.join(AUTH, "config.js"))
        self.assertEqual(a, b, "docs/admin/config.js and docs/auth/config.js must stay identical")
        self.assertIn("supabaseUrl: '" + SUPABASE_URL + "'", a)
        self.assertRegex(a, r"supabaseAnonKey: 'eyJ[A-Za-z0-9._-]+'")
        # the anon key is a JWT with role=anon – never ship a service key here
        key = re.search(r"supabaseAnonKey: '([^']+)'", a).group(1)
        import base64
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        self.assertEqual(claims.get("role"), "anon")


class ModerationTabTests(unittest.TestCase):
    def test_index_html_has_tab_scripts_and_ids(self):
        src, ids = check_html(self, os.path.join(ADMIN, "index.html"))
        self.assertIn('data-tab="moderation"', src)
        self.assertIn('id="panel-moderation"', src)
        self.assertIn('data-tab="reports"', src)
        self.assertIn('id="panel-reports"', src)
        self.assertIn('id="rep-count"', src)
        self.assertIn('<script src="' + CDN + '"', src)
        self.assertIn('<script src="config.js">', src)
        self.assertIn('<script src="moderation.js">', src)
        # load order: library → config → admin → moderation
        self.assertLess(src.index(CDN), src.index('src="config.js"'))
        self.assertLess(src.index('src="config.js"'), src.index('src="admin.js"'))
        self.assertLess(src.index('src="admin.js"'), src.index('src="moderation.js"'))
        self.assertIn("Schválit", read(os.path.join(ADMIN, "moderation.js")))
        self.assertIn("Zamítnout", read(os.path.join(ADMIN, "moderation.js")))
        used = ids_used(read(os.path.join(ADMIN, "moderation.js")))
        self.assertEqual(used - ids, set(), "moderation.js uses ids missing from index.html")
        self.assertIn(".mod-photo", read(os.path.join(ADMIN, "admin.css")))

    def test_moderation_js_contract(self):
        src = read(os.path.join(ADMIN, "moderation.js"))
        for needle in ("from('user_recipes')", "rpc('is_moderator')", "from('recipe_ratings_summary')",
                       "profiles_public", "status: 'approved'", "status: 'rejected'", "moderation_note",
                       "signInWithPassword", "Připojení není k dispozici",
                       # migration 0002 (BACKEND.md §4.10): reports, bans, verified badge, counts
                       "rpc('reports_overview'", "rpc('resolve_report'", "rpc('moderation_counts'",
                       "rpc('ban_user'", "rpc('unban_user'", "rpc('set_recipe_verified'", "rpc('search_profiles'",
                       "Migrace 0002 není spuštěna", "0002_reports_moderation.sql"):
            self.assertIn(needle, src, needle)
        self.assertNotIn("service_role", src)
        insecure = set(re.findall(r"http://[\w.]+", src))
        self.assertEqual(insecure, set(), "plain-http URLs in moderation.js")

    def test_readme_mentions_moderation(self):
        md = read(os.path.join(ADMIN, "README.md"))
        self.assertIn("Moderace", md)
        self.assertIn("config.js", md)


class AuthPagesTests(unittest.TestCase):
    def test_reset_page(self):
        src, ids = check_html(self, os.path.join(AUTH, "reset.html"))
        self.assertIn("<title>Vareska – nové heslo</title>", src)
        self.assertIn('<script src="' + CDN + '"', src)
        self.assertIn('<script src="config.js">', src)
        self.assertIn('<script src="reset.js">', src)
        self.assertIn('href="auth.css"', src)
        self.assertIn("Nové heslo", src)
        self.assertIn('id="password2"', src)
        self.assertIn('autocomplete="new-password"', src)
        js = read(os.path.join(AUTH, "reset.js"))
        self.assertEqual(ids_used(js) - ids, set(), "reset.js uses ids missing from reset.html")
        for needle in ("updateUser", "setSession", "verifyOtp", "exchangeCodeForSession", "/auth/v1/user", "replaceState"):
            self.assertIn(needle, js, needle)

    def test_landing_page(self):
        src, ids = check_html(self, os.path.join(AUTH, "index.html"))
        self.assertIn("<title>Vareska – účet</title>", src)
        self.assertIn('href="reset.html"', src)
        self.assertIn('<script src="landing.js">', src)
        js = read(os.path.join(AUTH, "landing.js"))
        self.assertIn("reset.html", js)
        for name in ("auth.css", "config.js", "landing.js", "reset.js", "reset.html", "index.html"):
            self.assertTrue(os.path.exists(os.path.join(AUTH, name)), name)

    def test_no_plain_http(self):
        for name in ("reset.js", "landing.js", "reset.html", "index.html"):
            src = read(os.path.join(AUTH, name))
            self.assertEqual(set(re.findall(r"http://[\w.]+", src)) - {"http://www.w3.org"}, set(), name)


@unittest.skipUnless(NODE, "node not available")
class NodeTests(unittest.TestCase):
    FILES = [os.path.join(ADMIN, "config.js"), os.path.join(ADMIN, "moderation.js"),
             os.path.join(AUTH, "config.js"), os.path.join(AUTH, "reset.js"), os.path.join(AUTH, "landing.js")]

    def test_js_syntax(self):
        for f in self.FILES:
            subprocess.run([NODE, "--check", f], check=True)

    def test_js_helpers_script(self):
        script = os.path.join(ROOT, "tests", "moderation_helpers.test.js")
        r = subprocess.run([NODE, script], capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertIn("checks ok", r.stdout)

    def test_js_behaviour(self):
        harness = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_moderation_harness.js")
        with open(harness, "w", encoding="utf-8") as f:
            f.write(NODE_HARNESS)
        try:
            r = subprocess.run([NODE, harness, *self.FILES], capture_output=True, text=True, encoding="utf-8", check=True)
        finally:
            os.remove(harness)
        out = json.loads(r.stdout)
        self.assertEqual(out["config"]["supabaseUrl"], SUPABASE_URL)
        # photoUrl: public bucket URL, cache-busted, encoded, null-safe
        self.assertEqual(out["photo"][0], SUPABASE_URL + "/storage/v1/object/public/recipe-photos/abc/def.jpg?v=1789552800")
        self.assertIsNone(out["photo"][1])
        self.assertEqual(out["photo"][2], SUPABASE_URL + "/storage/v1/object/public/recipe-photos/a%20b/c.jpg")
        # classifyError kinds (BACKEND.md §7)
        self.assertEqual(out["errors"], ["missing", "missing", "missing", "offline", "offline", "auth", "denied", "denied", "denied", "other"])
        self.assertIn("migrations/0001_vareska.sql", out["errorTexts"][0])
        self.assertEqual(out["errorTexts"][1], "Připojení není k dispozici.")
        # ingredient lines
        self.assertEqual(out["ings"][0], "120 g cibule")
        self.assertEqual(out["ings"][1], "2 lžíce máslo (změklé) – volitelné")
        self.assertEqual(out["ings"][2], "10 g unknown_x")
        self.assertEqual(out["ings"][3], "")
        # normalizeRecipe
        rec = out["recipe"]
        self.assertEqual(rec["title"], "Guláš")
        self.assertEqual(rec["ingredients"], [{"ingredientId": "cibule", "grams": 1}])
        self.assertEqual(rec["steps"], [])
        self.assertEqual(rec["tags"], [])
        self.assertEqual(rec["authorName"], "pepa")
        self.assertEqual(out["recipeArr"], "a")
        # rankRatings: min count, taste desc, count desc; empty ids dropped
        self.assertEqual(out["rank"], ["c:7:0.5", "b:5:0.8"])
        self.assertEqual(out["rankAll"], 1)
        # reset.js helpers
        self.assertEqual(out["params"], {"x": "1", "access_token": "AT", "refresh_token": "RT", "type": "recovery"})
        self.assertEqual(out["flows"], ["session", "token", "token_hash", "pkce", "error", "none", "error"])
        self.assertEqual(out["pw"][0], "Heslo musí mít alespoň 8 znaků.")
        self.assertEqual(out["pw"][1], "Hesla se neshodují.")
        self.assertIsNone(out["pw"][2])
        self.assertEqual(out["resetErr"][0], "Odkaz vypršel nebo už byl použit.")
        self.assertIn("slabé", out["resetErr"][1])
        self.assertIn("jiné", out["resetErr"][2])
        self.assertIn("v prohlížeči, ve kterém", out["resetErr"][3])
        self.assertIn("Připojení", out["resetErr"][4])
        # landing.js
        self.assertEqual(out["landing"], ["reset", "reset", "reset", "error", "confirmed", None])


if __name__ == "__main__":
    unittest.main(verbosity=2)
