/* Vareska admin panel – "Moderace" + "Nahlášení" tabs (community recipes,
 * ratings, reports, bans, verified badge).
 * Talks to Supabase through supabase-js (UMD build from jsDelivr, loaded in
 * index.html before this file) with the public anon key from config.js.
 * Row-level security decides what the signed-in account may do: the queue
 * and the Schválit / Zamítnout buttons only work for accounts listed in the
 * `moderators` table (docs/BACKEND.md §6). Reports, bans and the Ověřeno
 * toggle need migration 0002 (BACKEND.md §4.7–§4.10); without it the panel
 * shows "Migrace 0002 není spuštěna" and keeps the 0001 features working.
 * Everything degrades gracefully when the library, the network or the
 * migration is missing.
 */
(function () {
  'use strict';

  const CFG = window.VareskaConfig || {};
  const BUCKET = CFG.photoBucket || 'recipe-photos';
  const PAGE = 20;
  const RATINGS_LIMIT = 200;
  const TIMEOUT_MS = 10000;
  const MIGRATION_0002 = 'supabase/migrations/0002_reports_moderation.sql';

  const COURSE_LABEL = {
    breakfast: 'snídaně', soup: 'polévka', main: 'hlavní chod', side: 'příloha', salad: 'salát',
    sauce: 'omáčka', dessert: 'dezert', bread: 'pečivo', drink: 'nápoj', snack: 'svačina',
  };
  const STATUS_LABEL = { draft: 'rozepsaný', pending: 'čeká na schválení', approved: 'schválený', rejected: 'zamítnutý' };
  const STATUS_BADGE = { draft: '', pending: 'warn', approved: 'ok', rejected: 'err' };
  const UNIT_LABEL = { portion: 'porce', piece: 'kusy', slice: 'plátky', glass: 'sklenice' };
  /** reports.reason (BACKEND.md §4.7) → Czech label. */
  const REASON_LABEL = {
    spam: 'spam', offensive: 'urážlivý obsah', wrong_content: 'nesprávný obsah', copyright: 'autorská práva',
    dangerous: 'nebezpečný postup', other: 'jiné',
  };
  /** reports.target_type → Czech label. */
  const TARGET_LABEL = { recipe: 'komunitní recept', user: 'uživatel', bundled_recipe: 'vestavěný recept' };
  const REPORT_STATUS_LABEL = { open: 'otevřené', resolved: 'vyřešené', dismissed: 'zamítnuté' };
  /** Rule codes raised by the 0001/0002 triggers and rpcs (BACKEND.md §7, §4.10). */
  const RULE_TEXT = {
    status_not_allowed: 'Tento přechod stavu server nepovoluje.',
    moderator_may_not_edit_content: 'Moderátor smí měnit jen stav a poznámku.',
    photo_path_not_owned: 'Cesta k fotce nepatří autorovi.',
    not_moderator: 'Nemáte oprávnění – účet není v seznamu moderátorů.',
    not_authenticated: 'Nejste přihlášeni.',
    author_banned: 'Účet autora je omezen.',
    too_many_reports: 'Příliš mnoho otevřených nahlášení.',
    already_reported: 'Tento obsah už byl nahlášen.',
    cannot_report_self: 'Vlastní obsah nahlásit nelze.',
    target_invalid: 'Tento obsah nelze nahlásit.',
    report_not_found: 'Nahlášení nebylo nalezeno (možná už bylo vyřízeno).',
    recipe_not_found: 'Recept nebyl nalezen.',
    not_approved: 'Ověřit lze jen schválený recept.',
    user_not_found: 'Uživatel nebyl nalezen.',
    cannot_ban_self: 'Sám sebe omezit nemůžete.',
    cannot_ban_moderator: 'Moderátora omezit nelze.',
  };
  const RULE_RE = new RegExp('^(' + Object.keys(RULE_TEXT).join('|') + ')\\b');

  // ---------------------------------------------------------------------
  // Small helpers (kept local so this file works without admin.js)
  // ---------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);
  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k === 'html') e.innerHTML = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) e.setAttribute(k, v);
    }
    for (const c of children) if (c !== null && c !== undefined) e.append(c);
    return e;
  }
  function esc(s) { return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  function fmtNum(n, digits) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return '–';
    return Number(n).toLocaleString('cs-CZ', { maximumFractionDigits: digits ?? 1, minimumFractionDigits: 0 });
  }
  function fmtDate(s) {
    if (!s) return '–';
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return String(s);
    return d.toLocaleString('cs-CZ', { dateStyle: 'short', timeStyle: 'short' });
  }
  function toast(msg, ms) {
    const t = $('toast');
    if (!t) return;
    t.textContent = msg; t.classList.add('show');
    clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove('show'), ms || 3000);
  }
  function withTimeout(promise, ms) {
    let timer;
    const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(Object.assign(new Error('timeout'), { name: 'TimeoutError' })), ms || TIMEOUT_MS); });
    return Promise.race([Promise.resolve(promise), timeout]).finally(() => clearTimeout(timer));
  }
  /** supabase-js resolves `{data, error}`; turn `error` into a rejection. */
  async function q(builder) {
    const r = await withTimeout(builder);
    if (r && r.error) throw r.error;
    return r ? r.data : null;
  }
  /** Czech plural: plural(3, 'recept', 'recepty', 'receptů') → "3 recepty". */
  function plural(n, one, few, many) {
    const k = Math.abs(Number(n) || 0);
    return `${fmtNum(k, 0)} ${k === 1 ? one : (k >= 2 && k <= 4 ? few : many)}`;
  }

  // ---------------------------------------------------------------------
  // Pure helpers (exposed for tests)
  // ---------------------------------------------------------------------
  /** Public URL of a photo in the `recipe-photos` bucket (BACKEND.md §5), cache-busted by updated_at. */
  function photoUrl(path, updatedAt) {
    if (!path) return null;
    const base = String(CFG.supabaseUrl || '').replace(/\/+$/, '');
    const clean = String(path).replace(/^\/+/, '').split('/').map(encodeURIComponent).join('/');
    let url = `${base}/storage/v1/object/public/${BUCKET}/${clean}`;
    if (updatedAt) {
      const t = Date.parse(updatedAt);
      if (!Number.isNaN(t)) url += '?v=' + Math.floor(t / 1000);
    }
    return url;
  }

  /**
   * Classify an error from supabase-js / fetch into a Czech message.
   * kind: missing (migration not run) | offline | denied | auth | rule | other
   * `feature` = 'v2' when the call needs migration 0002 (reports, bans, verified,
   * moderation_counts): a missing table/function then names that file instead of 0001.
   */
  function classifyError(e, feature) {
    const code = String((e && (e.code || e.error_code)) || '');
    const status = Number(e && e.status) || 0;
    const msg = String((e && e.message) || e || '');
    const name = String((e && e.name) || '');
    if (code === '42P01' || code === '42883' || code === '42703' || code === 'PGRST205' || code === 'PGRST202' || (status === 404 && !/auth/i.test(msg))) {
      if (feature === 'v2') return { kind: 'missing', text: 'Migrace 0002 není spuštěna – v Supabase SQL editoru spusťte ' + MIGRATION_0002 + '.' };
      return { kind: 'missing', text: 'Backend zatím není nasazen – v Supabase SQL editoru spusťte supabase/migrations/0001_vareska.sql.' };
    }
    if (name === 'TimeoutError' || name === 'AbortError' || /failed to fetch|networkerror|load failed|network request failed|timeout/i.test(msg)) {
      return { kind: 'offline', text: 'Připojení není k dispozici.' };
    }
    if (code === 'invalid_credentials' || /invalid login credentials/i.test(msg)) {
      return { kind: 'auth', text: 'Nesprávný e-mail nebo heslo.' };
    }
    if (code === 'email_not_confirmed' || /email not confirmed/i.test(msg)) {
      return { kind: 'auth', text: 'E-mail zatím není potvrzený – dokončete registraci z e-mailu.' };
    }
    if (code === 'over_email_send_rate_limit' || code === 'over_request_rate_limit' || status === 429) {
      return { kind: 'auth', text: 'Příliš mnoho pokusů – zkuste to za chvíli.' };
    }
    const rule = RULE_RE.exec(msg);
    if (rule) {
      const kind = /^(status_not_allowed|moderator_may_not_edit_content|photo_path_not_owned|not_moderator)$/.test(rule[1]) ? 'denied' : 'rule';
      return { kind, code: rule[1], text: 'Server změnu odmítl (' + rule[1] + '): ' + RULE_TEXT[rule[1]] };
    }
    if (code === '42501' || status === 401 || status === 403 || /permission denied|row-level security|jwt/i.test(msg)) {
      return { kind: 'denied', text: 'Nemáte oprávnění – účet není v seznamu moderátorů.' };
    }
    return { kind: 'other', text: 'Chyba: ' + (msg || 'neznámá') };
  }

  /**
   * "120 g cibule (na kostičky)" from a `user_recipes.ingredients` line (BACKEND.md §4.1).
   * Custom lines (no ingredientId) print their own `name`.
   */
  function fmtIngredient(ing, nameOf) {
    if (!ing || typeof ing !== 'object') return '';
    const id = ing.ingredientId || ing.id || '';
    const custom = String(ing.name || ing.customName || ing.title || '').trim();
    const name = (id && nameOf && nameOf(id)) || (!id && custom) || id || custom || '?';
    let qty = '';
    if (ing.qty !== undefined && ing.qty !== null && ing.unit) qty = `${fmtNum(ing.qty, 2)} ${ing.unit}`;
    else if (ing.grams !== undefined && ing.grams !== null) qty = `${fmtNum(ing.grams, 0)} g`;
    let out = (qty ? qty + ' ' : '') + name;
    if (ing.note) out += ` (${ing.note})`;
    if (ing.optional) out += ' – volitelné';
    return out;
  }

  /** Normalise a `user_recipes` row (jsonb columns may arrive as strings from older clients). */
  function normalizeRecipe(row) {
    const r = Object.assign({}, row || {});
    for (const k of ['ingredients', 'steps']) {
      let v = r[k];
      if (typeof v === 'string') { try { v = JSON.parse(v); } catch (e) { v = []; } }
      r[k] = Array.isArray(v) ? v : [];
    }
    for (const k of ['tags', 'colors']) r[k] = Array.isArray(r[k]) ? r[k] : [];
    r.status = String(r.status || 'draft');
    r.title = String(r.title || '').trim() || '(bez názvu)';
    r.verified = !!r.verified_at;
    const a = r.author && typeof r.author === 'object' ? (Array.isArray(r.author) ? r.author[0] : r.author) : null;
    r.authorName = a ? (a.username || a.display_name || '') : (r.authorName || '');
    return r;
  }

  /** Top-rated list from recipe_ratings_summary rows: min count, best taste first. */
  function rankRatings(rows, minCount) {
    const min = Math.max(1, Number(minCount) || 1);
    return (Array.isArray(rows) ? rows : [])
      .map((r) => ({
        recipeId: String(r.recipe_id || ''), count: Number(r.count) || 0,
        taste: r.avg_taste === null || r.avg_taste === undefined ? null : Number(r.avg_taste),
        difficulty: r.avg_difficulty === null || r.avg_difficulty === undefined ? null : Number(r.avg_difficulty),
        cookAgain: r.cook_again_pct === null || r.cook_again_pct === undefined ? null : Number(r.cook_again_pct),
        updatedAt: r.updated_at || null,
      }))
      .filter((r) => r.recipeId && r.count >= min)
      .sort((a, b) => (b.taste ?? 0) - (a.taste ?? 0) || b.count - a.count || a.recipeId.localeCompare(b.recipeId));
  }

  /** [{reason, label, count}] – reasons of one target, most frequent first. */
  function summarizeReasons(reasons) {
    const counts = new Map();
    for (const r of Array.isArray(reasons) ? reasons : []) {
      const k = String(r || 'other');
      counts.set(k, (counts.get(k) || 0) + 1);
    }
    return Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([k, n]) => ({ reason: k, label: REASON_LABEL[k] || k, count: n }));
  }

  /** Normalise one `reports_overview()` row (BACKEND.md §4.10). */
  function normalizeReportGroup(row) {
    const r = row && typeof row === 'object' ? row : {};
    const type = String(r.target_type || 'recipe');
    const targetId = String(r.target_id || '');
    const reasons = Array.isArray(r.reasons) ? r.reasons.map(String) : (r.latest_reason ? [String(r.latest_reason)] : []);
    const ids = Array.isArray(r.report_ids) ? r.report_ids.map(String) : (r.latest_report_id ? [String(r.latest_report_id)] : []);
    return {
      key: type + ':' + targetId,
      type, targetId,
      count: Number(r.report_count) || ids.length || reasons.length || 0,
      firstAt: r.first_at || null, latestAt: r.latest_at || null,
      latestReason: String(r.latest_reason || reasons[0] || 'other'),
      latestNote: r.latest_note ? String(r.latest_note) : '',
      reasons, reportIds: ids,
      latestReportId: String(r.latest_report_id || ids[0] || ''),
      title: r.target_title ? String(r.target_title) : (type === 'user' ? (r.author_username ? '@' + r.author_username : targetId) : targetId),
      targetStatus: r.target_status ? String(r.target_status) : null,
      authorId: r.author_id ? String(r.author_id) : null,
      authorUsername: r.author_username ? String(r.author_username) : '',
      authorBanned: !!r.author_banned,
      appRecipeId: type === 'recipe' ? 'u:' + targetId : (type === 'bundled_recipe' ? targetId : null),
    };
  }

  /** Normalise a `moderation_counts()` payload (json object or string). */
  function normalizeCounts(data) {
    let d = data;
    if (typeof d === 'string') { try { d = JSON.parse(d); } catch (e) { d = null; } }
    if (Array.isArray(d)) d = d[0];
    d = d && typeof d === 'object' ? d : {};
    const n = (v) => (v === null || v === undefined || Number.isNaN(Number(v)) ? null : Number(v));
    return { pending: n(d.pending_recipes), open: n(d.open_reports) };
  }

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------
  const state = {
    sb: null, session: null, isModerator: null, username: '',
    status: 'pending', rows: [], shown: PAGE, loading: false,
    ratings: [], titles: new Map(), backend: 'unknown', started: false,
    // migration 0002 (reports / bans / verified): unknown | ok | missing
    v2: 'unknown', profiles: new Map(),
    reportStatus: 'open', reports: [], reportsLoading: false, reportsLoaded: false, users: [],
  };

  function catalogName(id) {
    const A = window.VareskaAdmin;
    const c = A && A.state && A.state.catalogById ? A.state.catalogById.get(id) : null;
    return c ? c.nameCs : null;
  }

  function notice(kind, html, boxId) {
    const box = $(boxId || 'mod-notice');
    if (!box) return;
    box.innerHTML = kind ? `<div class="notice ${kind}">${html}</div>` : '';
  }
  function reportError(e, where, boxId) {
    const c = classifyError(e);
    if (c.kind === 'missing') { state.backend = 'missing'; notice('warn', esc(c.text), boxId); }
    else if (c.kind === 'offline') notice('warn', esc(c.text), boxId);
    else notice('err', esc((where ? where + ': ' : '') + c.text), boxId);
    console.warn('[moderation]', where, e);
    return c;
  }
  /** Error of a call that needs migration 0002: remembers "missing" and hides the v2 controls. */
  function reportV2Error(e, where, boxId) {
    const c = classifyError(e, 'v2');
    if (c.kind === 'missing') { markV2Missing(); notice('warn', esc(c.text), boxId); }
    else if (c.kind === 'offline') notice('warn', esc(c.text), boxId);
    else notice('err', esc((where ? where + ': ' : '') + c.text), boxId);
    console.warn('[moderation]', where, e);
    return c;
  }
  function markV2Missing() {
    if (state.v2 === 'missing') return;
    state.v2 = 'missing';
    const rc = $('rep-count');
    if (rc) rc.textContent = '–';
    const html = 'Migrace 0002 není spuštěna – nahlášení, zákazy a značka Ověřeno zatím nefungují. V Supabase SQL editoru spusťte <code>' + esc(MIGRATION_0002) + '</code>.';
    notice('warn', html, 'rep-notice');
    notice('warn', html, 'mod-notice');
    renderList();
    renderReports();
  }
  const v2Enabled = () => state.v2 !== 'missing';

  // ---------------------------------------------------------------------
  // Supabase client / session
  // ---------------------------------------------------------------------
  function ensureClient() {
    if (state.sb) return state.sb;
    if (!window.supabase || typeof window.supabase.createClient !== 'function') {
      notice('warn', 'Knihovna supabase-js se nenačetla (offline nebo blokovaný CDN jsdelivr.net). Moderace je bez ní nedostupná.');
      notice('warn', 'Knihovna supabase-js se nenačetla (offline nebo blokovaný CDN jsdelivr.net).', 'rep-notice');
      return null;
    }
    if (!CFG.supabaseUrl || !CFG.supabaseAnonKey) {
      notice('err', 'Chybí <code>config.js</code> s adresou Supabase a anon klíčem.');
      return null;
    }
    state.sb = window.supabase.createClient(CFG.supabaseUrl, CFG.supabaseAnonKey, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: false, storageKey: 'vareska.admin.auth' },
    });
    state.sb.auth.onAuthStateChange((_event, session) => { applySession(session).catch((e) => reportError(e, 'Přihlášení')); });
    return state.sb;
  }

  async function applySession(session) {
    state.session = session || null;
    state.isModerator = null;
    state.username = '';
    state.profiles = new Map();
    state.reportsLoaded = false;
    renderSession();
    if (!session) { renderList(); renderReports(); renderUsers(); return; }
    try {
      const [isMod, me] = await Promise.all([
        q(state.sb.rpc('is_moderator')),
        q(state.sb.from('profiles').select('username, display_name').eq('id', session.user.id).maybeSingle()),
      ]);
      state.isModerator = isMod === true;
      state.username = me ? (me.username || me.display_name || '') : '';
      state.backend = 'ok';
      notice(null);
    } catch (e) {
      const c = reportError(e, 'Ověření účtu');
      state.isModerator = c.kind === 'missing' ? null : false;
    }
    renderSession();
    if (state.isModerator) {
      await Promise.all([loadQueue(), loadCounts()]);
      if (location.hash === '#reports') loadReports();
    } else { renderList(); renderReports(); }
  }

  function renderSession() {
    const badge = $('mod-session');
    const form = $('mod-login-form');
    const s = state.session;
    if (!s) {
      badge.textContent = 'nepřihlášen'; badge.className = 'badge';
      $('btn-mod-login').hidden = false; $('btn-mod-logout').hidden = true;
      for (const id of ['mod-email', 'mod-password']) $(id).disabled = false;
      $('mod-count').textContent = '–';
      $('rep-count').textContent = '–';
      return;
    }
    const who = state.username ? `${state.username}` : (s.user && s.user.email) || 'přihlášen';
    if (state.isModerator === true) { badge.textContent = `${who} · moderátor`; badge.className = 'badge ok'; }
    else if (state.isModerator === false) { badge.textContent = `${who} · bez práv moderátora`; badge.className = 'badge warn'; }
    else { badge.textContent = who; badge.className = 'badge primary'; }
    $('btn-mod-login').hidden = true; $('btn-mod-logout').hidden = false;
    for (const id of ['mod-email', 'mod-password']) $(id).disabled = true;
    form.reset();
  }

  async function login(ev) {
    ev.preventDefault();
    const sb = ensureClient();
    if (!sb) return;
    const email = $('mod-email').value.trim();
    const password = $('mod-password').value;
    const res = $('mod-login-result');
    if (!email || !password) { res.textContent = 'Zadejte e-mail a heslo.'; return; }
    $('btn-mod-login').disabled = true;
    res.textContent = 'Přihlašuji…';
    try {
      const data = await q(sb.auth.signInWithPassword({ email, password }));
      res.textContent = '';
      notice(null);
      if (data && data.session) await applySession(data.session);
      toast('Přihlášeno.');
    } catch (e) {
      res.textContent = classifyError(e).text;
      console.warn('[moderation] login', e);
    } finally { $('btn-mod-login').disabled = false; }
  }

  async function logout() {
    if (!state.sb) return;
    try { await withTimeout(state.sb.auth.signOut({ scope: 'local' })); } catch (e) { /* ignore */ }
    state.rows = []; state.reports = []; state.reportsLoaded = false; state.users = []; state.session = null; state.isModerator = null; state.username = '';
    renderSession(); renderList(); renderReports(); renderUsers();
    toast('Odhlášeno.');
  }

  // ---------------------------------------------------------------------
  // Counts (badges on both tabs; rpc moderation_counts – 0002, fallback = head count)
  // ---------------------------------------------------------------------
  async function loadCounts() {
    if (!state.sb || !state.isModerator) return;
    if (v2Enabled()) {
      try {
        const c = normalizeCounts(await q(state.sb.rpc('moderation_counts')));
        state.v2 = 'ok';
        $('mod-count').textContent = c.pending === null ? '–' : String(c.pending);
        $('rep-count').textContent = c.open === null ? '–' : String(c.open);
        return;
      } catch (e) {
        const c = classifyError(e, 'v2');
        if (c.kind === 'missing') markV2Missing();
        else console.warn('[moderation] counts', e);
      }
    }
    await loadPendingCount();
  }

  async function loadPendingCount() {
    try {
      const r = await withTimeout(state.sb.from('user_recipes').select('id', { count: 'exact', head: true }).eq('status', 'pending'));
      if (r.error) throw r.error;
      $('mod-count').textContent = String(r.count ?? '–');
    } catch (e) { $('mod-count').textContent = '–'; }
  }

  // ---------------------------------------------------------------------
  // Profiles (ban state of authors; moderator-only rpc search_profiles – 0002)
  // ---------------------------------------------------------------------
  function normalizeProfile(p) {
    return {
      id: String(p.id || ''), username: String(p.username || ''), displayName: String(p.display_name || ''),
      createdAt: p.created_at || null, bannedAt: p.banned_at || null, banReason: p.ban_reason ? String(p.ban_reason) : '',
      approved: Number(p.recipes_approved) || 0, pending: Number(p.recipes_pending) || 0,
      banned: !!p.banned_at,
    };
  }

  /** Fetch (and cache) the ban state of the given author ids; silent when 0002 is missing. */
  async function loadProfiles(ids) {
    if (!state.sb || !state.isModerator || !v2Enabled()) return;
    const todo = Array.from(new Set(ids.filter(Boolean))).filter((id) => !state.profiles.has(id));
    if (!todo.length) return;
    for (const id of todo) state.profiles.set(id, undefined); // in flight: skip duplicate lookups
    let changed = false;
    for (let i = 0; i < todo.length; i += 5) {
      await Promise.all(todo.slice(i, i + 5).map(async (id) => {
        try {
          const rows = await q(state.sb.rpc('search_profiles', { p_query: id }));
          const row = (rows || []).find((p) => p.id === id) || null;
          state.profiles.set(id, row ? normalizeProfile(row) : null);
          state.v2 = 'ok';
          changed = true;
        } catch (e) {
          state.profiles.delete(id);
          const c = classifyError(e, 'v2');
          if (c.kind === 'missing') markV2Missing();
          else console.warn('[moderation] profile', id, e);
        }
      }));
      if (!v2Enabled()) return;
    }
    if (changed) { renderList(); renderReports(); }
  }

  function profileOf(id) { return (id && state.profiles.get(id)) || null; }

  /** Ban / unban a user (rpc ban_user / unban_user); `done(classified|null)` reports the outcome. */
  async function setBan(userId, label, banned, done) {
    if (!state.sb || !userId) return;
    let reason = '';
    if (banned) {
      reason = prompt(`Důvod omezení účtu ${label} (uvidí ho v aplikaci, max. 500 znaků):`, '');
      if (reason === null) return;
      reason = reason.trim().slice(0, 500);
      if (reason.length < 3) { toast('Napište důvod (alespoň 3 znaky).'); return; }
    } else if (!confirm(`Zrušit omezení účtu ${label}?`)) return;
    try {
      if (banned) await q(state.sb.rpc('ban_user', { p_user: userId, p_reason: reason }));
      else await q(state.sb.rpc('unban_user', { p_user: userId }));
      state.v2 = 'ok';
      const now = new Date().toISOString();
      const p = state.profiles.get(userId);
      if (p) state.profiles.set(userId, Object.assign({}, p, { banned, bannedAt: banned ? now : null, banReason: banned ? reason : '' }));
      else state.profiles.delete(userId);
      for (const u of state.users) if (u.id === userId) { u.banned = banned; u.bannedAt = banned ? now : null; u.banReason = banned ? reason : ''; }
      for (const g of state.reports) if (g.authorId === userId) g.authorBanned = banned;
      toast(banned ? `Účet ${label} omezen.` : `Omezení účtu ${label} zrušeno.`);
      if (banned) { loadQueue(); loadCounts(); } // ban_user also rejects the author's pending recipes
      renderList(); renderReports(); renderUsers();
      if (done) done(null);
    } catch (e) {
      const c = reportV2Error(e, banned ? 'Omezení účtu' : 'Zrušení omezení');
      if (done) done(c);
    }
  }

  // ---------------------------------------------------------------------
  // Queue
  // ---------------------------------------------------------------------
  const SELECT_EMBED = '*, author:profiles_public(username, display_name)';

  async function fetchRecipes(status, id) {
    const sb = state.sb;
    const build = (select) => {
      let b = sb.from('user_recipes').select(select);
      if (id) return b.eq('id', id).limit(1);
      if (status && status !== 'all') b = b.eq('status', status);
      return b.order('updated_at', { ascending: status === 'pending' }).limit(200);
    };
    let rows;
    try {
      rows = await q(build(SELECT_EMBED));
    } catch (e) {
      // PGRST200: the FK → view embed is not resolvable; fall back to two queries (BACKEND.md §4.3).
      if (String(e && e.code) !== 'PGRST200') throw e;
      rows = await q(build('*'));
      const ids = Array.from(new Set(rows.map((r) => r.author_id).filter(Boolean)));
      if (ids.length) {
        const names = await q(sb.from('profiles_public').select('id, username, display_name').in('id', ids));
        const byId = new Map((names || []).map((n) => [n.id, n]));
        for (const r of rows) r.author = byId.get(r.author_id) || null;
      }
    }
    return (rows || []).map(normalizeRecipe);
  }

  async function loadQueue() {
    if (!state.sb || !state.session || state.loading) return;
    state.loading = true;
    $('mod-list').innerHTML = '<p class="muted">Načítám…</p>';
    try {
      state.rows = await fetchRecipes(state.status);
      state.shown = PAGE;
      state.backend = 'ok';
      if (v2Enabled()) notice(null);
      for (const r of state.rows) state.titles.set('u:' + r.id, r.title);
      renderList();
      loadProfiles(state.rows.slice(0, state.shown).map((r) => r.author_id));
    } catch (e) {
      reportError(e, 'Načtení receptů');
      state.rows = [];
      renderList();
    } finally { state.loading = false; }
  }

  function renderList() {
    const box = $('mod-list');
    const info = $('mod-queue-info');
    const more = $('mod-more');
    if (!box) return;
    box.innerHTML = '';
    more.hidden = true;
    if (!state.session) { box.innerHTML = '<p class="muted">Přihlaste se jako moderátor.</p>'; info.textContent = ''; return; }
    if (state.isModerator === false) { box.innerHTML = '<div class="notice warn">Tento účet není moderátor – frontu vidí jen účty ze seznamu <code>moderators</code>.</div>'; info.textContent = ''; return; }
    if (state.loading) { box.innerHTML = '<p class="muted">Načítám…</p>'; return; }
    const rows = state.rows;
    info.textContent = rows.length ? `${rows.length} receptů` : '';
    if (!rows.length) { box.innerHTML = `<div class="empty">Žádné recepty (${esc(STATUS_LABEL[state.status] || 'vše')}).</div>`; return; }
    for (const r of rows.slice(0, state.shown)) box.append(renderRecipe(r));
    if (rows.length > state.shown) more.hidden = false;
  }

  /** Author label + "účet omezen" badge shared by the queue card and the report card. */
  function authorBits(authorId, authorName) {
    const p = profileOf(authorId);
    const label = authorName ? '@' + authorName : (authorId || '?').slice(0, 8) + '…';
    const bits = [el('span', null, `autor: ${label}`)];
    if (p && p.banned) bits.push(el('span', { class: 'badge err', title: p.banReason || '', text: 'účet omezen' }));
    return { p, label, bits };
  }
  function banButton(authorId, label, p, resultEl) {
    if (!v2Enabled() || !authorId || !p) return null; // p === null: ban state unknown (profile not loaded yet)
    const banned = !!p.banned;
    return el('button', {
      class: 'btn small ' + (banned ? '' : 'danger'), type: 'button',
      title: banned ? 'Zrušit omezení účtu autora' : 'Omezit účet autora (ban): nemůže odesílat recepty, jeho schválené recepty se skryjí',
      onclick: () => setBan(authorId, label, !banned, (c) => { if (resultEl && c) resultEl.textContent = c.text; }),
    }, banned ? '🔓 Odblokovat autora' : '⛔ Zablokovat autora');
  }

  function renderRecipe(r, opts) {
    const o = opts || {};
    const photo = photoUrl(r.photo_path, r.updated_at);
    const author = authorBits(r.author_id, r.authorName);
    const meta = el('div', { class: 'queue-meta' },
      el('span', null, `${esc(COURSE_LABEL[r.course] || r.course || '–')} · ${esc(String(r.cuisine || '').toUpperCase())}`),
      el('span', null, `${fmtNum(r.servings, 0)} ${UNIT_LABEL[r.serving_unit] || r.serving_unit || 'porce'}`),
      ...author.bits,
      el('span', null, `upraveno ${fmtDate(r.updated_at)}`),
      r.published_at ? el('span', null, `publikováno ${fmtDate(r.published_at)}`) : null,
      r.verified_at ? el('span', null, `ověřeno ${fmtDate(r.verified_at)}`) : null,
    );
    const ings = el('ul', { class: 'mod-ings' });
    for (const ing of r.ingredients) ings.append(el('li', { text: fmtIngredient(ing, catalogName) }));
    const steps = el('ol', { class: 'mod-steps' });
    for (const s of r.steps) steps.append(el('li', { text: (s && s.text ? String(s.text) : '') + (s && s.minutes ? ` (${fmtNum(s.minutes, 0)} min)` : '') }));
    const body = el('div', { class: 'mod-body' },
      photo ? el('a', { href: photo, target: '_blank', rel: 'noopener', class: 'mod-photo' }, el('img', { src: photo, alt: 'Fotka receptu', loading: 'lazy' }))
        : el('div', { class: 'mod-photo mod-photo-empty', text: r.emoji || '🍽️' }),
      el('div', { class: 'mod-cols' },
        el('div', null, el('h3', { text: `Suroviny (${r.ingredients.length})` }), ings),
        el('div', null, el('h3', { text: `Postup (${r.steps.length})` }), steps),
      ),
    );
    const note = el('textarea', { class: 'input', rows: '2', placeholder: 'Poznámka pro autora (při zamítnutí povinná, max. 500 znaků)', maxlength: '500' });
    if (r.moderation_note) note.value = r.moderation_note;
    const result = el('span', { class: 'muted small' });
    const actions = el('div', { class: 'row mod-actions' });
    if (r.status !== 'draft') {
      if (r.status !== 'approved') actions.append(el('button', { class: 'btn ok', type: 'button', onclick: () => decide(r, 'approved', note.value, result, o.onChange) }, '✓ Schválit'));
      if (r.status !== 'rejected') actions.append(el('button', { class: 'btn danger', type: 'button', onclick: () => decide(r, 'rejected', note.value, result, o.onChange) }, '✕ Zamítnout'));
      if (r.status === 'approved' && v2Enabled()) {
        const cb = el('input', { type: 'checkbox' });
        cb.checked = !!r.verified_at;
        cb.addEventListener('change', () => setVerified(r, cb.checked, result, cb, o.onChange));
        actions.append(el('label', { class: 'chip' + (r.verified_at ? ' on' : ''), title: 'Značka „Ověřeno“ (štít) u receptu v aplikaci' }, cb, ' 🛡 Ověřeno'));
      }
    } else {
      actions.append(el('span', { class: 'muted small', text: 'Rozepsaný recept – autor ho zatím neodeslal ke schválení.' }));
    }
    const ban = banButton(r.author_id, author.label, author.p, result);
    if (ban) actions.append(ban);
    actions.append(result);
    const item = el('div', { class: 'queue-item mod-item', 'data-id': r.id },
      el('div', { class: 'queue-head' },
        el('span', { class: 'title', text: `${r.emoji || ''} ${r.title}`.trim() }),
        r.title_native ? el('span', { class: 'muted', text: r.title_native }) : null,
        el('span', { class: 'badge ' + (STATUS_BADGE[r.status] || ''), text: STATUS_LABEL[r.status] || r.status }),
        r.verified_at ? el('span', { class: 'badge primary', text: '🛡 Ověřeno' }) : null,
        el('span', { class: 'muted small mono', text: 'u:' + r.id }),
      ),
      meta,
      r.description ? el('p', { class: 'mod-desc', text: r.description }) : null,
      r.tags.length ? el('div', { class: 'checks' }, ...r.tags.map((t) => el('span', { class: 'chip', text: t }))) : null,
      body,
      r.status !== 'draft' ? el('div', { class: 'field', style: 'margin-top:10px' }, note) : null,
      actions,
    );
    return item;
  }

  /** Put an updated row back into the queue (or drop it when it left the current filter). */
  function replaceRow(updated) {
    if (state.status !== 'all' && updated.status !== state.status) state.rows = state.rows.filter((x) => x.id !== updated.id);
    else if (state.rows.some((x) => x.id === updated.id)) state.rows = state.rows.map((x) => (x.id === updated.id ? updated : x));
    renderList();
  }

  async function decide(r, status, noteText, resultEl, onChange) {
    const note = String(noteText || '').trim();
    if (status === 'rejected' && note.length < 3) { resultEl.textContent = 'Napište autorovi důvod zamítnutí.'; return; }
    const verb = status === 'approved' ? 'Schválit' : 'Zamítnout';
    if (!confirm(`${verb} recept „${r.title}“?`)) return;
    resultEl.textContent = 'Ukládám…';
    try {
      const updated = await updateStatus(r, status, note);
      toast(status === 'approved' ? `Recept „${r.title}“ schválen a publikován.` : `Recept „${r.title}“ zamítnut.`);
      replaceRow(updated);
      loadCounts();
      if (onChange) onChange(updated);
    } catch (e) {
      resultEl.textContent = classifyError(e).text;
      console.warn('[moderation] decide', e);
    }
  }

  /** update user_recipes.status (+ moderation_note); returns the normalised row. */
  async function updateStatus(r, status, note) {
    const patch = status === 'approved' ? { status: 'approved' } : { status: 'rejected', moderation_note: note };
    if (status === 'approved' && note) patch.moderation_note = note;
    const rows = await q(state.sb.from('user_recipes').update(patch).eq('id', r.id).select());
    if (!rows || !rows.length) throw Object.assign(new Error('Řádek se nezměnil – chybí oprávnění moderátora?'), { code: '42501' });
    return normalizeRecipe(Object.assign({}, r, rows[0]));
  }

  async function setVerified(r, verified, resultEl, cb, onChange) {
    resultEl.textContent = 'Ukládám…';
    if (cb) cb.disabled = true;
    try {
      const row = await q(state.sb.rpc('set_recipe_verified', { p_id: r.id, p_verified: !!verified }));
      state.v2 = 'ok';
      const data = Array.isArray(row) ? row[0] : row;
      const updated = normalizeRecipe(Object.assign({}, r, data || { verified_at: verified ? new Date().toISOString() : null }));
      toast(verified ? `Recept „${r.title}“ označen jako ověřený.` : `Značka Ověřeno u receptu „${r.title}“ zrušena.`);
      replaceRow(updated);
      if (onChange) onChange(updated);
    } catch (e) {
      const c = classifyError(e, 'v2');
      resultEl.textContent = c.text;
      if (c.kind === 'missing') markV2Missing();
      if (cb) { cb.checked = !!r.verified_at; cb.disabled = false; }
      console.warn('[moderation] verified', e);
    }
  }

  // ---------------------------------------------------------------------
  // Reports (tab Nahlášení; rpc reports_overview / resolve_report – 0002)
  // ---------------------------------------------------------------------
  async function loadReports() {
    if (!state.sb || !state.session || !state.isModerator || state.reportsLoading || !v2Enabled()) { renderReports(); return; }
    state.reportsLoading = true;
    renderReports();
    try {
      const rows = await q(state.sb.rpc('reports_overview', { p_status: state.reportStatus }));
      state.reports = (rows || []).map(normalizeReportGroup);
      state.reportsLoaded = true;
      state.v2 = 'ok';
      notice(null, '', 'rep-notice');
      state.reportsLoading = false;
      renderReports();
      loadProfiles(state.reports.map((g) => g.authorId));
      loadCounts();
    } catch (e) {
      state.reports = [];
      state.reportsLoading = false;
      reportV2Error(e, 'Načtení nahlášení', 'rep-notice');
      renderReports();
    }
  }

  function renderReports() {
    const box = $('rep-list');
    const info = $('rep-info');
    if (!box) return;
    box.innerHTML = '';
    if (!state.session) { box.innerHTML = '<p class="muted">Přihlaste se jako moderátor v záložce <a href="#moderation">Moderace</a>.</p>'; info.textContent = ''; return; }
    if (state.isModerator === false) { box.innerHTML = '<div class="notice warn">Tento účet není moderátor.</div>'; info.textContent = ''; return; }
    if (!v2Enabled()) { box.innerHTML = '<p class="muted">Nahlášení vyžadují migraci 0002.</p>'; info.textContent = ''; return; }
    if (state.reportsLoading) { box.innerHTML = '<p class="muted">Načítám…</p>'; return; }
    const list = state.reports;
    const total = list.reduce((n, g) => n + g.count, 0);
    info.textContent = list.length ? `${plural(list.length, 'nahlášený cíl', 'nahlášené cíle', 'nahlášených cílů')} · ${fmtNum(total, 0)} nahlášení` : '';
    if (!list.length) { box.innerHTML = `<div class="empty">Žádná ${esc(REPORT_STATUS_LABEL[state.reportStatus] || '')} nahlášení.</div>`; return; }
    for (const g of list) box.append(renderReportGroup(g));
  }

  function renderReportGroup(g) {
    const author = authorBits(g.authorId, g.authorUsername);
    const banned = g.authorBanned || !!(author.p && author.p.banned);
    const head = el('div', { class: 'queue-head' },
      el('span', { class: 'badge primary', text: TARGET_LABEL[g.type] || g.type }),
      el('span', { class: 'title', text: g.title }),
      g.targetStatus ? el('span', { class: 'badge ' + (STATUS_BADGE[g.targetStatus] || ''), text: STATUS_LABEL[g.targetStatus] || g.targetStatus }) : null,
      banned ? el('span', { class: 'badge err', text: 'účet omezen' }) : null,
      el('span', { class: 'badge warn', text: `${fmtNum(g.count, 0)}× nahlášeno` }),
      el('span', { class: 'muted small mono', text: g.appRecipeId || g.targetId }),
    );
    const meta = el('div', { class: 'queue-meta' },
      el('span', null, `${g.type === 'user' ? 'nahlášený účet' : 'autor'}: ${author.label}`),
      el('span', null, plural(g.count, 'nahlašující', 'nahlašující', 'nahlašujících')),
      el('span', null, `poprvé ${fmtDate(g.firstAt)}`),
      el('span', null, `naposledy ${fmtDate(g.latestAt)}`),
    );
    const reasons = el('div', { class: 'checks' },
      ...summarizeReasons(g.reasons).map((x) => el('span', { class: 'chip' + (x.reason === g.latestReason ? ' on' : ''), text: `${x.label} ×${x.count}` })));
    const latest = g.latestNote ? el('p', { class: 'mod-desc' }, el('span', { class: 'muted', text: 'poslední poznámka: ' }), el('span', { text: g.latestNote })) : null;
    const note = el('textarea', { class: 'input', rows: '2', placeholder: 'Poznámka k vyřízení (u „Skrýt recept“ ji uvidí autor; max. 500 znaků)', maxlength: '500' });
    const result = el('span', { class: 'muted small' });
    const detail = el('div', { class: 'rep-detail' });
    const actions = el('div', { class: 'row mod-actions' });
    if (g.type === 'recipe') {
      actions.append(el('button', { class: 'btn small', type: 'button', onclick: (ev) => toggleRecipeDetail(g, detail, ev.currentTarget) }, '📖 Zobrazit recept'));
    }
    if (state.reportStatus === 'open') {
      actions.append(el('button', { class: 'btn', type: 'button', title: 'Nahlášení je neoprávněné – obsah zůstává', onclick: () => resolveGroup(g, 'dismissed', note.value, result) }, '✕ Zamítnout'));
      actions.append(el('button', { class: 'btn ok', type: 'button', title: 'Nahlášení bylo vyřízeno', onclick: () => resolveGroup(g, 'resolved', note.value, result) }, '✓ Vyřešit'));
      if (g.type === 'recipe' && g.targetStatus !== 'rejected') {
        actions.append(el('button', { class: 'btn danger', type: 'button', title: 'Zamítne recept (autor uvidí poznámku) a nahlášení označí jako vyřešená', onclick: () => hideRecipe(g, note.value, result) }, '🚫 Skrýt recept'));
      }
      if (g.authorId && !banned) {
        actions.append(el('button', { class: 'btn danger', type: 'button', title: 'Omezí účet (ban) a nahlášení označí jako vyřešená', onclick: () => banFromReport(g, result) }, g.type === 'user' ? '⛔ Zablokovat uživatele' : '⛔ Zablokovat autora'));
      } else if (g.authorId && banned) {
        actions.append(el('button', { class: 'btn small', type: 'button', onclick: () => setBan(g.authorId, author.label, false, (c) => { if (c) result.textContent = c.text; }) }, '🔓 Odblokovat'));
      }
    } else {
      actions.append(el('button', { class: 'btn', type: 'button', onclick: () => resolveGroup(g, 'open', note.value, result) }, '↩ Znovu otevřít'));
    }
    actions.append(result);
    return el('div', { class: 'queue-item rep-item', 'data-key': g.key },
      head, meta, reasons, latest, detail,
      el('div', { class: 'field', style: 'margin-top:10px' }, note),
      actions,
    );
  }

  /** Load the reported recipe and show its full moderation card inside the report item. */
  async function toggleRecipeDetail(g, box, btn) {
    if (box.childElementCount) { box.innerHTML = ''; btn.textContent = '📖 Zobrazit recept'; return; }
    box.innerHTML = '<p class="muted">Načítám recept…</p>';
    btn.textContent = '▲ Skrýt náhled';
    try {
      const rows = await fetchRecipes(null, g.targetId);
      box.innerHTML = '';
      if (!rows.length) { box.innerHTML = '<p class="muted">Recept není k dispozici (smazaný, nebo ho RLS nezobrazí).</p>'; return; }
      box.append(renderRecipe(rows[0], { onChange: (updated) => { g.targetStatus = updated.status; } }));
      loadProfiles([rows[0].author_id]);
    } catch (e) {
      box.innerHTML = `<p class="muted">${esc(classifyError(e).text)}</p>`;
    }
  }

  function removeGroup(g) {
    state.reports = state.reports.filter((x) => x.key !== g.key);
    renderReports();
    loadCounts();
  }

  async function resolveGroup(g, status, noteText, resultEl) {
    if (!g.latestReportId) { resultEl.textContent = 'Chybí id nahlášení.'; return; }
    const note = String(noteText || '').trim();
    const verb = { dismissed: 'Zamítnout', resolved: 'Označit jako vyřešené', open: 'Znovu otevřít' }[status] || status;
    if (!confirm(`${verb} ${fmtNum(g.count, 0)} nahlášení cíle „${g.title}“?`)) return;
    resultEl.textContent = 'Ukládám…';
    try {
      const n = await q(state.sb.rpc('resolve_report', { p_id: g.latestReportId, p_status: status, p_note: note || null, p_whole_target: true }));
      state.v2 = 'ok';
      toast(`${fmtNum(Number(n) || g.count, 0)} nahlášení: ${REPORT_STATUS_LABEL[status] || status}.`);
      removeGroup(g);
    } catch (e) {
      resultEl.textContent = reportV2Error(e, 'Vyřízení nahlášení', 'rep-notice').text;
    }
  }

  async function hideRecipe(g, noteText, resultEl) {
    const note = String(noteText || '').trim();
    if (note.length < 3) { resultEl.textContent = 'Napište autorovi důvod skrytí (poznámka nahoře).'; return; }
    if (!confirm(`Skrýt (zamítnout) recept „${g.title}“ a nahlášení označit jako vyřešená?`)) return;
    resultEl.textContent = 'Ukládám…';
    try {
      const updated = await updateStatus({ id: g.targetId, title: g.title }, 'rejected', note);
      replaceRow(updated);
      await q(state.sb.rpc('resolve_report', { p_id: g.latestReportId, p_status: 'resolved', p_note: 'Recept skryt: ' + note, p_whole_target: true }));
      toast(`Recept „${g.title}“ skryt, nahlášení vyřešena.`);
      removeGroup(g);
    } catch (e) {
      resultEl.textContent = reportV2Error(e, 'Skrytí receptu', 'rep-notice').text;
    }
  }

  function banFromReport(g, resultEl) {
    const label = g.authorUsername ? '@' + g.authorUsername : (g.authorId || '').slice(0, 8) + '…';
    setBan(g.authorId, label, true, async (c) => {
      if (c) { resultEl.textContent = c.text; return; }
      try {
        await q(state.sb.rpc('resolve_report', { p_id: g.latestReportId, p_status: 'resolved', p_note: 'Účet ' + label + ' omezen.', p_whole_target: true }));
        removeGroup(g);
      } catch (e) { resultEl.textContent = reportV2Error(e, 'Vyřízení nahlášení', 'rep-notice').text; }
    });
  }

  // ---------------------------------------------------------------------
  // Users (rpc search_profiles / ban_user / unban_user – 0002)
  // ---------------------------------------------------------------------
  async function searchUsers() {
    const info = $('rep-users-info');
    if (!state.sb || !state.isModerator) { info.textContent = 'Přihlaste se jako moderátor.'; return; }
    if (!v2Enabled()) { info.textContent = 'Vyžaduje migraci 0002.'; return; }
    info.textContent = 'Hledám…';
    try {
      const rows = await q(state.sb.rpc('search_profiles', { p_query: $('rep-users-query').value.trim() }));
      state.v2 = 'ok';
      state.users = (rows || []).map(normalizeProfile);
      for (const u of state.users) state.profiles.set(u.id, u);
      info.textContent = state.users.length ? `${plural(state.users.length, 'účet', 'účty', 'účtů')}${state.users.length >= 50 ? ' (prvních 50)' : ''}` : 'Nic nenalezeno.';
      renderUsers();
    } catch (e) {
      info.textContent = reportV2Error(e, 'Hledání účtů', 'rep-notice').text;
    }
  }

  function renderUsers() {
    const table = $('rep-users-table');
    if (!table) return;
    const tbody = table.querySelector('tbody');
    tbody.innerHTML = '';
    if (!state.session || !state.users.length) { tbody.innerHTML = '<tr><td class="muted" colspan="6">Zadejte uživatelské jméno, e-mail nebo id; prázdné hledání vypíše nejnovější účty.</td></tr>'; return; }
    for (const u of state.users) {
      const label = u.username ? '@' + u.username : u.id.slice(0, 8) + '…';
      const res = el('span', { class: 'muted small' });
      tbody.append(el('tr', null,
        el('td', null, el('strong', { text: label }), u.displayName ? el('span', { class: 'muted', text: ' ' + u.displayName }) : null, el('div', { class: 'mono muted small', text: u.id })),
        el('td', { text: fmtDate(u.createdAt) }),
        el('td', { class: 'num', text: `${fmtNum(u.approved, 0)} / ${fmtNum(u.pending, 0)}` }),
        el('td', null, u.banned ? el('span', { class: 'badge err', text: 'omezen ' + fmtDate(u.bannedAt) }) : el('span', { class: 'badge ok', text: 'aktivní' })),
        el('td', { class: 'small', text: u.banReason || '' }),
        el('td', null, el('button', { class: 'btn small' + (u.banned ? '' : ' danger'), type: 'button', onclick: () => setBan(u.id, label, !u.banned, (c) => { res.textContent = c ? c.text : ''; }) }, u.banned ? '🔓 Odblokovat' : '⛔ Zablokovat'), ' ', res),
      ));
    }
  }

  // ---------------------------------------------------------------------
  // Ratings (public view, no login needed)
  // ---------------------------------------------------------------------
  async function loadRatings() {
    const sb = ensureClient();
    if (!sb) return;
    const info = $('mod-ratings-info');
    const tbody = $('mod-ratings-table').querySelector('tbody');
    info.textContent = 'Načítám…';
    try {
      const rows = await q(sb.from('recipe_ratings_summary')
        .select('recipe_id, count, avg_taste, avg_difficulty, cook_again_pct, updated_at')
        .order('avg_taste', { ascending: false }).order('count', { ascending: false }).limit(RATINGS_LIMIT));
      state.ratings = rows || [];
      state.backend = 'ok';
      if (state.backend === 'ok' && !state.session) notice(null);
      info.textContent = `${state.ratings.length} hodnocených receptů` + (state.ratings.length >= RATINGS_LIMIT ? ` (zobrazeno prvních ${RATINGS_LIMIT})` : '');
      await resolveCommunityTitles(state.ratings.map((r) => r.recipe_id));
      renderRatings();
    } catch (e) {
      const c = reportError(e, 'Hodnocení');
      info.textContent = c.text;
      tbody.innerHTML = `<tr><td class="muted" colspan="7">${esc(c.text)}</td></tr>`;
    }
  }

  async function resolveCommunityTitles(ids) {
    const uuids = ids.filter((id) => /^u:/.test(id)).map((id) => id.slice(2)).filter((id) => !state.titles.has('u:' + id));
    if (!uuids.length || !state.sb) return;
    try {
      const rows = await q(state.sb.from('user_recipes').select('id, title').in('id', uuids.slice(0, 200)));
      for (const r of rows || []) state.titles.set('u:' + r.id, r.title);
    } catch (e) { /* titles are decoration only */ }
  }

  function renderRatings() {
    const tbody = $('mod-ratings-table').querySelector('tbody');
    const min = $('mod-ratings-min3').checked ? 3 : 1;
    const list = rankRatings(state.ratings, min);
    tbody.innerHTML = '';
    if (!list.length) { tbody.innerHTML = `<tr><td class="muted" colspan="7">Zatím žádná hodnocení${min > 1 ? ' se třemi a více hlasy' : ''}.</td></tr>`; return; }
    list.forEach((r, i) => {
      const title = r.recipeId.startsWith('u:') ? (state.titles.get(r.recipeId) || '(komunitní recept)') : '';
      tbody.append(el('tr', null,
        el('td', { class: 'num', text: String(i + 1) }),
        el('td', null, title ? el('span', { text: title + ' ' }) : null, el('span', { class: 'mono muted small', text: r.recipeId })),
        el('td', { class: 'num', text: fmtNum(r.count, 0) }),
        el('td', { class: 'num', text: r.taste === null ? '–' : fmtNum(r.taste, 1) + ' ★' }),
        el('td', { class: 'num', text: r.difficulty === null ? '–' : fmtNum(r.difficulty, 1) }),
        el('td', { class: 'num', text: r.cookAgain === null ? '–' : fmtNum(r.cookAgain * 100, 0) + ' %' }),
        el('td', { text: fmtDate(r.updatedAt) }),
      ));
    });
  }

  // ---------------------------------------------------------------------
  // Wiring
  // ---------------------------------------------------------------------
  async function start() {
    if (state.started) return;
    state.started = true;
    const sb = ensureClient();
    if (!sb) return;
    try {
      const data = await withTimeout(sb.auth.getSession());
      await applySession(data && data.data ? data.data.session : null);
    } catch (e) { reportError(e, 'Obnovení přihlášení'); }
    loadRatings();
  }

  /** The Nahlášení tab shares the session with Moderace; opening it starts the client and loads reports once. */
  async function startReports() {
    if (!state.started) { await start(); }
    if (state.isModerator && !state.reportsLoaded && !state.reportsLoading) loadReports();
    else renderReports();
  }

  function bind() {
    if (!$('panel-moderation')) return;
    $('mod-login-form').addEventListener('submit', login);
    $('btn-mod-logout').addEventListener('click', logout);
    $('mod-status').addEventListener('change', () => { state.status = $('mod-status').value; loadQueue(); });
    $('btn-mod-refresh').addEventListener('click', () => { if (!state.session) start(); else { loadQueue(); loadCounts(); } });
    $('mod-more').addEventListener('click', () => { state.shown += PAGE; renderList(); loadProfiles(state.rows.slice(0, state.shown).map((r) => r.author_id)); });
    $('btn-mod-ratings').addEventListener('click', loadRatings);
    $('mod-ratings-min3').addEventListener('change', renderRatings);
    const tab = document.querySelector('.tab[data-tab="moderation"]');
    if (tab) tab.addEventListener('click', start);
    if ($('panel-reports')) {
      $('rep-status').addEventListener('change', () => { state.reportStatus = $('rep-status').value; loadReports(); });
      $('btn-rep-refresh').addEventListener('click', () => { if (!state.session) startReports(); else loadReports(); });
      $('rep-users-form').addEventListener('submit', (ev) => { ev.preventDefault(); searchUsers(); });
      const rtab = document.querySelector('.tab[data-tab="reports"]');
      if (rtab) rtab.addEventListener('click', startReports);
    }
    window.addEventListener('hashchange', () => {
      if (location.hash === '#moderation') start();
      if (location.hash === '#reports') startReports();
    });
    if (location.hash === '#moderation') start();
    if (location.hash === '#reports') startReports();
  }

  window.VareskaModeration = {
    photoUrl, classifyError, fmtIngredient, normalizeRecipe, rankRatings,
    summarizeReasons, normalizeReportGroup, normalizeCounts, plural, REASON_LABEL, TARGET_LABEL, RULE_TEXT, state,
  };
  document.addEventListener('DOMContentLoaded', bind);
})();
