/* Node checks for the pure helpers exported by docs/admin/moderation.js
 * (window.VareskaModeration). Run: `node tests/moderation_helpers.test.js`
 * (also invoked by tests/test_moderation_auth.py). No browser, no supabase-js.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const noop = () => {};
global.window = global;
global.document = { addEventListener: noop, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
global.localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
global.location = { hash: '', search: '', pathname: '/x', replace: noop };
global.history = { replaceState: noop };
global.fetch = () => Promise.reject(new Error('no network in tests'));

const ADMIN = path.join(__dirname, '..', 'docs', 'admin');
for (const f of ['config.js', 'moderation.js']) new Function(fs.readFileSync(path.join(ADMIN, f), 'utf8'))();
const M = window.VareskaModeration;

let n = 0;
function t(name, fn) {
  try { fn(); } catch (e) { console.error('FAIL ' + name); throw e; }
  n += 1;
}

t('classifyError: 0001 vs 0002 missing', () => {
  assert.strictEqual(M.classifyError({ code: 'PGRST202' }).kind, 'missing');
  assert.match(M.classifyError({ code: 'PGRST202' }).text, /0001_vareska\.sql/);
  assert.match(M.classifyError({ code: 'PGRST202' }, 'v2').text, /^Migrace 0002 není spuštěna/);
  assert.match(M.classifyError({ code: 'PGRST202' }, 'v2').text, /0002_reports_moderation\.sql/);
  for (const code of ['42883', '42703', '42P01', 'PGRST205']) assert.strictEqual(M.classifyError({ code }, 'v2').kind, 'missing', code);
  assert.strictEqual(M.classifyError({ status: 404, message: 'Not found' }, 'v2').kind, 'missing');
});

t('classifyError: rule codes from rpcs / triggers', () => {
  const r = M.classifyError({ code: 'P0001', message: 'not_moderator' });
  assert.strictEqual(r.kind, 'denied');
  assert.match(r.text, /moderátor/);
  assert.strictEqual(M.classifyError({ message: 'status_not_allowed' }).kind, 'denied');
  for (const code of ['author_banned', 'too_many_reports', 'already_reported', 'cannot_report_self', 'target_invalid',
    'report_not_found', 'not_approved', 'user_not_found', 'cannot_ban_self', 'cannot_ban_moderator']) {
    const c = M.classifyError({ code: 'P0001', message: code });
    assert.strictEqual(c.kind, 'rule', code);
    assert.strictEqual(c.code, code);
    assert.ok(c.text.includes(code) && c.text.includes(M.RULE_TEXT[code]), c.text);
  }
  // a message that merely starts with a similar word is not a code
  assert.strictEqual(M.classifyError({ message: 'not_approvedx' }).kind, 'other');
  assert.strictEqual(M.classifyError({ message: 'not_approved: only approved recipes' }).kind, 'rule');
  assert.strictEqual(M.classifyError({ code: '42501', message: 'permission denied' }).kind, 'denied');
  assert.strictEqual(M.classifyError(new TypeError('Failed to fetch')).kind, 'offline');
});

t('fmtIngredient: catalog ids and custom lines', () => {
  const names = { cibule: 'cibule' };
  const nameOf = (id) => names[id];
  assert.strictEqual(M.fmtIngredient({ ingredientId: 'cibule', grams: 120 }, nameOf), '120 g cibule');
  assert.strictEqual(M.fmtIngredient({ name: 'kuřecí vývar z kostky', grams: 250 }, nameOf), '250 g kuřecí vývar z kostky');
  assert.strictEqual(M.fmtIngredient({ name: 'sójová omáčka', qty: 2, unit: 'lžíce', note: 'světlá' }, nameOf), '2 lžíce sójová omáčka (světlá)');
  assert.strictEqual(M.fmtIngredient({ name: '  ', grams: 5 }, nameOf), '5 g ?');
  assert.strictEqual(M.fmtIngredient({ ingredientId: 'unknown_x', name: 'Neznámá', grams: 10 }, nameOf), '10 g unknown_x');
  assert.strictEqual(M.fmtIngredient({ ingredientId: '', name: 'sůl', optional: true }), 'sůl – volitelné');
  assert.strictEqual(M.fmtIngredient(null), '');
  assert.strictEqual(M.fmtIngredient('x'), '');
});

t('normalizeRecipe: verified flag', () => {
  assert.strictEqual(M.normalizeRecipe({ verified_at: '2026-09-17T10:00:00Z' }).verified, true);
  assert.strictEqual(M.normalizeRecipe({ verified_at: null }).verified, false);
  assert.strictEqual(M.normalizeRecipe({}).title, '(bez názvu)');
});

t('summarizeReasons: counts, order, labels', () => {
  const s = M.summarizeReasons(['spam', 'offensive', 'spam', 'bogus', null]);
  assert.deepStrictEqual(s.map((x) => `${x.reason}:${x.count}`), ['spam:2', 'bogus:1', 'offensive:1', 'other:1']);
  assert.strictEqual(s[0].label, 'spam');
  assert.strictEqual(s[1].label, 'bogus');
  assert.strictEqual(s[2].label, 'urážlivý obsah');
  assert.deepStrictEqual(M.summarizeReasons(null), []);
});

t('normalizeReportGroup: reports_overview row', () => {
  const g = M.normalizeReportGroup({
    target_type: 'recipe', target_id: '11111111-1111-1111-1111-111111111111', report_count: 2,
    first_at: '2026-09-10T00:00:00Z', latest_at: '2026-09-12T00:00:00Z', latest_reason: 'spam', latest_note: 'reklama',
    reasons: ['spam', 'offensive'], report_ids: ['r2', 'r1'], latest_report_id: 'r2',
    target_title: 'Guláš', target_status: 'approved', author_id: 'a1', author_username: 'petr', author_banned: false,
  });
  assert.strictEqual(g.key, 'recipe:11111111-1111-1111-1111-111111111111');
  assert.strictEqual(g.appRecipeId, 'u:11111111-1111-1111-1111-111111111111');
  assert.strictEqual(g.count, 2);
  assert.strictEqual(g.title, 'Guláš');
  assert.strictEqual(g.latestReportId, 'r2');
  assert.deepStrictEqual(g.reportIds, ['r2', 'r1']);
  assert.strictEqual(g.authorUsername, 'petr');
  assert.strictEqual(g.authorBanned, false);
  const u = M.normalizeReportGroup({ target_type: 'user', target_id: 'u1', latest_reason: 'offensive', latest_report_id: 'r9', author_username: 'tomas', author_banned: true });
  assert.strictEqual(u.title, '@tomas');
  assert.strictEqual(u.appRecipeId, null);
  assert.strictEqual(u.count, 1);
  assert.deepStrictEqual(u.reasons, ['offensive']);
  assert.strictEqual(u.authorBanned, true);
  const b = M.normalizeReportGroup({ target_type: 'bundled_recipe', target_id: 'cz_gulas', target_title: 'cz_gulas', report_count: '3' });
  assert.strictEqual(b.appRecipeId, 'cz_gulas');
  assert.strictEqual(b.count, 3);
  assert.strictEqual(b.latestReason, 'other');
  assert.strictEqual(M.normalizeReportGroup(null).count, 0);
});

t('normalizeCounts: json object, string, array, garbage', () => {
  assert.deepStrictEqual(M.normalizeCounts({ pending_recipes: 1, open_reports: 3 }), { pending: 1, open: 3 });
  assert.deepStrictEqual(M.normalizeCounts('{"pending_recipes":"4","open_reports":0}'), { pending: 4, open: 0 });
  assert.deepStrictEqual(M.normalizeCounts([{ pending_recipes: 2, open_reports: 5 }]), { pending: 2, open: 5 });
  assert.deepStrictEqual(M.normalizeCounts(null), { pending: null, open: null });
  assert.deepStrictEqual(M.normalizeCounts('nope'), { pending: null, open: null });
});

t('plural: Czech forms', () => {
  assert.strictEqual(M.plural(1, 'účet', 'účty', 'účtů'), '1 účet');
  assert.strictEqual(M.plural(3, 'účet', 'účty', 'účtů'), '3 účty');
  assert.strictEqual(M.plural(5, 'účet', 'účty', 'účtů'), '5 účtů');
  assert.strictEqual(M.plural(0, 'účet', 'účty', 'účtů'), '0 účtů');
});

t('labels', () => {
  assert.deepStrictEqual(Object.keys(M.REASON_LABEL).sort(), ['copyright', 'dangerous', 'offensive', 'other', 'spam', 'wrong_content']);
  assert.deepStrictEqual(Object.keys(M.TARGET_LABEL).sort(), ['bundled_recipe', 'recipe', 'user']);
});

console.log(`moderation helpers: ${n} checks ok`);
