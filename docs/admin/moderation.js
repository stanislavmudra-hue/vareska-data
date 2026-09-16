/* Vareska admin panel – "Moderace" tab (community recipes + ratings).
 * Talks to Supabase through supabase-js (UMD build from jsDelivr, loaded in
 * index.html before this file) with the public anon key from config.js.
 * Row-level security decides what the signed-in account may do: the queue
 * and the Schválit / Zamítnout buttons only work for accounts listed in the
 * `moderators` table (docs/BACKEND.md §6). Everything degrades gracefully
 * when the library, the network or the migration is missing.
 */
(function () {
  'use strict';

  const CFG = window.VareskaConfig || {};
  const BUCKET = CFG.photoBucket || 'recipe-photos';
  const PAGE = 20;
  const RATINGS_LIMIT = 200;
  const TIMEOUT_MS = 10000;

  const COURSE_LABEL = {
    breakfast: 'snídaně', soup: 'polévka', main: 'hlavní chod', side: 'příloha', salad: 'salát',
    sauce: 'omáčka', dessert: 'dezert', bread: 'pečivo', drink: 'nápoj', snack: 'svačina',
  };
  const STATUS_LABEL = { draft: 'rozepsaný', pending: 'čeká na schválení', approved: 'schválený', rejected: 'zamítnutý' };
  const STATUS_BADGE = { draft: '', pending: 'warn', approved: 'ok', rejected: 'err' };
  const UNIT_LABEL = { portion: 'porce', piece: 'kusy', slice: 'plátky', glass: 'sklenice' };

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
   * kind: missing (migration not run) | offline | denied | auth | other
   */
  function classifyError(e) {
    const code = String((e && (e.code || e.error_code)) || '');
    const status = Number(e && e.status) || 0;
    const msg = String((e && e.message) || e || '');
    const name = String((e && e.name) || '');
    if (code === '42P01' || code === 'PGRST205' || code === 'PGRST202' || (status === 404 && !/auth/i.test(msg))) {
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
    if (code === '42501' || status === 401 || status === 403 || /permission denied|row-level security|jwt/i.test(msg)) {
      return { kind: 'denied', text: 'Nemáte oprávnění – účet není v seznamu moderátorů.' };
    }
    if (/^(status_not_allowed|moderator_may_not_edit_content|photo_path_not_owned)/.test(msg)) {
      return { kind: 'denied', text: 'Server změnu odmítl (' + msg.split(/\s/)[0] + ').' };
    }
    return { kind: 'other', text: 'Chyba: ' + (msg || 'neznámá') };
  }

  /** "120 g cibule (na kostičky)" from a `user_recipes.ingredients` line (BACKEND.md §4.1). */
  function fmtIngredient(ing, nameOf) {
    if (!ing || typeof ing !== 'object') return '';
    const id = ing.ingredientId || ing.id || '';
    const name = (nameOf && nameOf(id)) || id || '?';
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

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------
  const state = {
    sb: null, session: null, isModerator: null, username: '',
    status: 'pending', rows: [], shown: PAGE, loading: false,
    ratings: [], titles: new Map(), backend: 'unknown', started: false,
  };

  function catalogName(id) {
    const A = window.VareskaAdmin;
    const c = A && A.state && A.state.catalogById ? A.state.catalogById.get(id) : null;
    return c ? c.nameCs : null;
  }

  function notice(kind, html) {
    const box = $('mod-notice');
    if (!box) return;
    box.innerHTML = kind ? `<div class="notice ${kind}">${html}</div>` : '';
  }
  function reportError(e, where) {
    const c = classifyError(e);
    if (c.kind === 'missing') { state.backend = 'missing'; notice('warn', esc(c.text)); }
    else if (c.kind === 'offline') notice('warn', esc(c.text));
    else notice('err', esc((where ? where + ': ' : '') + c.text));
    console.warn('[moderation]', where, e);
    return c;
  }

  // ---------------------------------------------------------------------
  // Supabase client / session
  // ---------------------------------------------------------------------
  function ensureClient() {
    if (state.sb) return state.sb;
    if (!window.supabase || typeof window.supabase.createClient !== 'function') {
      notice('warn', 'Knihovna supabase-js se nenačetla (offline nebo blokovaný CDN jsdelivr.net). Moderace je bez ní nedostupná.');
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
    renderSession();
    if (!session) { renderList(); return; }
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
    if (state.isModerator) await loadQueue();
    else renderList();
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
    state.rows = []; state.session = null; state.isModerator = null; state.username = '';
    renderSession(); renderList();
    toast('Odhlášeno.');
  }

  // ---------------------------------------------------------------------
  // Queue
  // ---------------------------------------------------------------------
  const SELECT_EMBED = '*, author:profiles_public(username, display_name)';

  async function fetchRecipes(status) {
    const sb = state.sb;
    const build = (select) => {
      let b = sb.from('user_recipes').select(select);
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

  async function loadPendingCount() {
    try {
      const r = await withTimeout(state.sb.from('user_recipes').select('id', { count: 'exact', head: true }).eq('status', 'pending'));
      if (r.error) throw r.error;
      $('mod-count').textContent = String(r.count ?? '–');
    } catch (e) { $('mod-count').textContent = '–'; }
  }

  async function loadQueue() {
    if (!state.sb || !state.session || state.loading) return;
    state.loading = true;
    $('mod-list').innerHTML = '<p class="muted">Načítám…</p>';
    try {
      state.rows = await fetchRecipes(state.status);
      state.shown = PAGE;
      state.backend = 'ok';
      notice(null);
      for (const r of state.rows) state.titles.set('u:' + r.id, r.title);
      renderList();
      loadPendingCount();
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

  function renderRecipe(r) {
    const photo = photoUrl(r.photo_path, r.updated_at);
    const meta = el('div', { class: 'queue-meta' },
      el('span', null, `${esc(COURSE_LABEL[r.course] || r.course || '–')} · ${esc(String(r.cuisine || '').toUpperCase())}`),
      el('span', null, `${fmtNum(r.servings, 0)} ${UNIT_LABEL[r.serving_unit] || r.serving_unit || 'porce'}`),
      el('span', null, `autor: ${r.authorName ? '@' + r.authorName : (r.author_id || '?').slice(0, 8) + '…'}`),
      el('span', null, `upraveno ${fmtDate(r.updated_at)}`),
      r.published_at ? el('span', null, `publikováno ${fmtDate(r.published_at)}`) : null,
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
      if (r.status !== 'approved') actions.append(el('button', { class: 'btn ok', type: 'button', onclick: () => decide(r, 'approved', note.value, result) }, '✓ Schválit'));
      if (r.status !== 'rejected') actions.append(el('button', { class: 'btn danger', type: 'button', onclick: () => decide(r, 'rejected', note.value, result) }, '✕ Zamítnout'));
    } else {
      actions.append(el('span', { class: 'muted small', text: 'Rozepsaný recept – autor ho zatím neodeslal ke schválení.' }));
    }
    actions.append(result);
    const item = el('div', { class: 'queue-item mod-item', 'data-id': r.id },
      el('div', { class: 'queue-head' },
        el('span', { class: 'title', text: `${r.emoji || ''} ${r.title}`.trim() }),
        r.title_native ? el('span', { class: 'muted', text: r.title_native }) : null,
        el('span', { class: 'badge ' + (STATUS_BADGE[r.status] || ''), text: STATUS_LABEL[r.status] || r.status }),
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

  async function decide(r, status, noteText, resultEl) {
    const note = String(noteText || '').trim();
    if (status === 'rejected' && note.length < 3) { resultEl.textContent = 'Napište autorovi důvod zamítnutí.'; return; }
    const verb = status === 'approved' ? 'Schválit' : 'Zamítnout';
    if (!confirm(`${verb} recept „${r.title}“?`)) return;
    const patch = status === 'approved' ? { status: 'approved' } : { status: 'rejected', moderation_note: note };
    if (status === 'approved' && note) patch.moderation_note = note;
    resultEl.textContent = 'Ukládám…';
    try {
      const rows = await q(state.sb.from('user_recipes').update(patch).eq('id', r.id).select());
      if (!rows || !rows.length) throw Object.assign(new Error('Řádek se nezměnil – chybí oprávnění moderátora?'), { code: '42501' });
      const updated = normalizeRecipe(Object.assign({}, r, rows[0]));
      toast(status === 'approved' ? `Recept „${r.title}“ schválen a publikován.` : `Recept „${r.title}“ zamítnut.`);
      if (state.status !== 'all' && updated.status !== state.status) state.rows = state.rows.filter((x) => x.id !== r.id);
      else state.rows = state.rows.map((x) => (x.id === r.id ? updated : x));
      renderList();
      loadPendingCount();
    } catch (e) {
      resultEl.textContent = classifyError(e).text;
      console.warn('[moderation] decide', e);
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

  function bind() {
    if (!$('panel-moderation')) return;
    $('mod-login-form').addEventListener('submit', login);
    $('btn-mod-logout').addEventListener('click', logout);
    $('mod-status').addEventListener('change', () => { state.status = $('mod-status').value; loadQueue(); });
    $('btn-mod-refresh').addEventListener('click', () => { if (!state.session) start(); else loadQueue(); });
    $('mod-more').addEventListener('click', () => { state.shown += PAGE; renderList(); });
    $('btn-mod-ratings').addEventListener('click', loadRatings);
    $('mod-ratings-min3').addEventListener('change', renderRatings);
    const tab = document.querySelector('.tab[data-tab="moderation"]');
    if (tab) tab.addEventListener('click', start);
    window.addEventListener('hashchange', () => { if (location.hash === '#moderation') start(); });
    if (location.hash === '#moderation') start();
  }

  window.VareskaModeration = { photoUrl, classifyError, fmtIngredient, normalizeRecipe, rankRatings, state };
  document.addEventListener('DOMContentLoaded', bind);
})();
