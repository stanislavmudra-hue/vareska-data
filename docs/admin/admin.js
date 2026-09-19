/* Vareska admin panel – vanilla JS, no build step.
 * Reads the pipeline outputs published on GitHub Pages (docs/) and writes
 * mapping rules / triggers workflow runs through the GitHub REST API.
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------------
  // Constants
  // ---------------------------------------------------------------------
  const STORES = ['albert', 'lidl', 'kaufland', 'tesco', 'billa', 'penny', 'globus'];
  const STORE_LABEL = {
    albert: 'Albert', lidl: 'Lidl', kaufland: 'Kaufland', tesco: 'Tesco',
    billa: 'Billa', penny: 'Penny', globus: 'Globus',
  };
  const SOURCE_LABEL = {
    globus: 'Globus API', lidl: 'Lidl API', penny: 'Penny API', albert: 'Albert (Publitas)',
    billa: 'Billa (Publitas)', kaufland: 'Kaufland (Schwarz)', tesco: 'Tesco', kupi: 'kupi.cz',
    akcniceny: 'akcniceny.cz',
  };
  // Markets (pipeline/markets.py): docs/data/markets.json has one row per market;
  // the panel itself shows the Czech market, the other markets publish
  // docs/prices/<market>.json (schema v2) and docs/data/<market>/…
  const MARKET_LABEL = { cz: 'Česko', sk: 'Slovensko', pl: 'Polsko', de: 'Německo', at: 'Rakousko' };
  const DATA = {
    prices: '../prices.json',
    markets: '../data/markets.json',
    health: '../data/health.json',
    history: '../data/history.json',
    unmatched: '../data/unmatched.json',
    mappings: ['../data/mappings.json'],
    catalog: ['../data/catalog/ingredients.json', '../catalog/ingredients.json', '../data/ingredients.json'],
    qaSources: '../data/qa/sources_report.md',
    qaStats: '../data/qa/content_stats.md',
  };
  const LS = {
    settings: 'vareska.admin.settings',
    pending: 'vareska.admin.pending',
    handled: 'vareska.admin.handled',
  };
  const DEFAULT_SETTINGS = {
    token: '', owner: 'stanislavmudra-hue', repo: 'vareska-data', branch: 'main',
    workflow: 'update.yml', mappingsPath: 'data/mappings.json',
  };
  const API = 'https://api.github.com';

  // ---------------------------------------------------------------------
  // Text normaliser – mirrors lib/logic/text_normalizer.dart (SPEC §7.2)
  // ---------------------------------------------------------------------
  const FOLD_MAP = {
    'á':'a','ä':'a','â':'a','à':'a','ã':'a','å':'a','ā':'a','ă':'a','ą':'a','ǎ':'a','ả':'a','ạ':'a',
    'ắ':'a','ằ':'a','ẳ':'a','ẵ':'a','ặ':'a','ấ':'a','ầ':'a','ẩ':'a','ẫ':'a','ậ':'a','æ':'a',
    'č':'c','ç':'c','ć':'c','ĉ':'c','ċ':'c','ď':'d','đ':'d','ð':'d',
    'é':'e','ě':'e','ë':'e','è':'e','ê':'e','ē':'e','ę':'e','ė':'e','ĕ':'e','ẻ':'e','ẽ':'e','ẹ':'e',
    'ế':'e','ề':'e','ể':'e','ễ':'e','ệ':'e','ğ':'g','ģ':'g','ġ':'g','ħ':'h',
    'í':'i','ï':'i','ì':'i','î':'i','ī':'i','ı':'i','į':'i','ǐ':'i','ỉ':'i','ĩ':'i','ị':'i',
    'ķ':'k','ľ':'l','ĺ':'l','ł':'l','ļ':'l','ň':'n','ñ':'n','ń':'n','ņ':'n',
    'ó':'o','ö':'o','ò':'o','ô':'o','õ':'o','ø':'o','ō':'o','ő':'o','ơ':'o','ǒ':'o','ỏ':'o','ọ':'o',
    'ố':'o','ồ':'o','ổ':'o','ỗ':'o','ộ':'o','ớ':'o','ờ':'o','ở':'o','ỡ':'o','ợ':'o','œ':'oe',
    'ř':'r','ŕ':'r','š':'s','ś':'s','ş':'s','ș':'s','ŝ':'s','ť':'t','þ':'t','ţ':'t','ț':'t',
    'ú':'u','ů':'u','ü':'u','ù':'u','û':'u','ū':'u','ű':'u','ư':'u','ų':'u','ǔ':'u','ǚ':'u','ủ':'u','ũ':'u','ụ':'u',
    'ứ':'u','ừ':'u','ử':'u','ữ':'u','ự':'u','ý':'y','ÿ':'y','ỳ':'y','ỷ':'y','ỹ':'y','ỵ':'y',
    'ž':'z','ż':'z','ź':'z','ß':'ss',
  };
  const SUFFIXES = ['ami','emi','ich','ech','ach','ata','ete','um','am','ou','em','im','ym','e','i','u','y','a','o'];

  function fold(s) {
    const lower = String(s || '').toLowerCase();
    let out = '';
    let lastSpace = true;
    for (const ch of lower) {
      const c = ch.codePointAt(0);
      if ((c >= 0x61 && c <= 0x7a) || (c >= 0x30 && c <= 0x39)) { out += ch; lastSpace = false; continue; }
      const mapped = c >= 0x80 ? FOLD_MAP[ch] : undefined;
      if (mapped !== undefined) { out += mapped; lastSpace = false; }
      else { if (!lastSpace) out += ' '; lastSpace = true; }
    }
    return out.endsWith(' ') ? out.slice(0, -1) : out;
  }
  function tokens(s) { return s.split(' ').filter((t) => t.length >= 2); }
  function stem(t) {
    if (t.length < 5) return t;
    for (const suf of SUFFIXES) {
      if (t.endsWith(suf) && t.length - suf.length >= 4) return t.slice(0, t.length - suf.length);
    }
    return t;
  }
  function normalizeTokens(s) { return tokens(fold(s)).map(stem); }
  function tokenMatches(q, idx) {
    if (!q || !idx) return false;
    const shorter = q.length <= idx.length ? q : idx;
    if (shorter.length < 3) return q === idx;
    return idx.startsWith(q) || q.startsWith(idx);
  }

  // ---------------------------------------------------------------------
  // Small helpers
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
  function esc(s) { return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c])); }
  function fmtNum(n, digits) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return '–';
    return Number(n).toLocaleString('cs-CZ', { maximumFractionDigits: digits ?? 1, minimumFractionDigits: 0 });
  }
  function fmtPrice(n) { return n === null || n === undefined ? '–' : fmtNum(n, 1) + ' Kč'; }
  function fmtDate(s) {
    if (!s) return '–';
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return String(s);
    if (/^\d{4}-\d{2}-\d{2}$/.test(String(s))) return d.toLocaleDateString('cs-CZ');
    return d.toLocaleString('cs-CZ', { dateStyle: 'short', timeStyle: 'short' });
  }
  function fmtDuration(sec) {
    if (sec === null || sec === undefined || Number.isNaN(Number(sec))) return '–';
    sec = Math.round(Number(sec));
    if (sec < 60) return sec + ' s';
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return `${m} min ${s} s`;
    return `${Math.floor(m / 60)} h ${m % 60} min`;
  }
  function todayIso() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }
  function storeLabel(s) { return STORE_LABEL[s] || SOURCE_LABEL[s] || s || '–'; }
  let toastTimer = null;
  function toast(msg, ms) {
    const t = $('toast'); t.textContent = msg; t.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), ms || 3000);
  }
  function lsGet(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch (e) { return fallback; } }
  function lsSet(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) { /* ignore */ } }
  function b64encodeUtf8(str) { return btoa(String.fromCharCode(...new TextEncoder().encode(str))); }
  function b64decodeUtf8(b64) { return new TextDecoder().decode(Uint8Array.from(atob(b64.replace(/\n/g, '')), (c) => c.charCodeAt(0))); }

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------
  const HANDLED_TTL_MS = 7 * 24 * 3600 * 1000;
  function pruneHandled(h) {
    const now = Date.now();
    const out = {};
    for (const [k, v] of Object.entries(h || {})) if (v && v.ts && now - v.ts < HANDLED_TTL_MS) out[k] = v;
    return out;
  }
  const state = {
    settings: Object.assign({}, DEFAULT_SETTINGS, lsGet(LS.settings, {})),
    prices: null, health: null, history: [], unmatched: [], catalog: [], catalogById: new Map(),
    mappings: null, markets: [], diag: [],
    pending: lsGet(LS.pending, []),       // [{key, title, store, source, action, ingredientId, ts}]
    handled: pruneHandled(lsGet(LS.handled, {})), // key -> {action, ingredientId, ts}; hidden locally until the next run picks the rule up
    queueShown: 50, priceShown: 100, dealsShown: 100,
    priceStores: new Set(STORES),
  };

  // ---------------------------------------------------------------------
  // Data loading (tolerant of missing files and shape variants)
  // ---------------------------------------------------------------------
  async function fetchJson(url) {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }
  async function fetchText(url) {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.text();
  }
  async function tryLoad(name, urls, loader) {
    const list = Array.isArray(urls) ? urls : [urls];
    let lastErr = null;
    for (const u of list) {
      try {
        const v = await loader(u);
        state.diag.push({ name, url: u, ok: true });
        return v;
      } catch (e) { lastErr = e; }
    }
    state.diag.push({ name, url: list[list.length - 1], ok: false, error: lastErr ? lastErr.message : '?' });
    return null;
  }

  function normalizeHealth(h) {
    if (!h || typeof h !== 'object') return null;
    const run = h.run || h.lastRun || h.last_run || h;
    const out = {
      date: run.finishedAt || run.finished_at || run.date || run.updated || run.startedAt || run.started_at || h.updated || h.generatedAt || h.generated_at || null,
      startedAt: run.startedAt || run.started_at || null,
      durationSec: run.durationSec ?? run.duration_sec ?? run.durationSeconds ?? run.duration ?? null,
      offers: run.offers ?? run.items ?? run.offersTotal ?? h.offers ?? null,
      deals: run.deals ?? run.dealsCount ?? h.deals ?? null,
      priced: run.priced ?? run.pricedIngredients ?? run.priced_ingredients ?? h.priced ?? null,
      unmatched: run.unmatched ?? h.unmatched ?? null,
      ok: run.ok ?? h.ok ?? null,
      sources: [],
    };
    let src = h.sources || h.stores || h.status || null;
    if (Array.isArray(src)) {
      for (const s of src) out.sources.push(normalizeSource(s.id || s.name || s.source || s.store || '?', s));
    } else if (src && typeof src === 'object') {
      for (const [id, s] of Object.entries(src)) out.sources.push(normalizeSource(id, typeof s === 'object' ? s : { ok: s === 'ok' || s === true }));
    }
    return out;
  }
  function normalizeSource(id, s) {
    s = s || {};
    let ok = s.ok;
    if (ok === undefined && typeof s.status === 'string') ok = /^(ok|success|healthy)$/i.test(s.status);
    if (ok === undefined && typeof s.status === 'boolean') ok = s.status;
    if (ok === undefined && s.error) ok = false;
    const skipped = s.skipped === true || /^skip/i.test(String(s.status || ''));
    return {
      id, label: s.label || s.name || SOURCE_LABEL[id] || STORE_LABEL[id] || id,
      ok: ok === undefined ? null : !!ok, skipped,
      items: s.items ?? s.offers ?? s.count ?? s.rows ?? null,
      deals: s.deals ?? null,
      matched: s.matched ?? null,
      unmatched: s.unmatched ?? null,
      requests: s.requests ?? null,
      durationSec: s.durationSec ?? s.duration_sec ?? s.duration ?? null,
      error: s.error || s.message || s.note || null,
      robots: s.robots || null,
      fetchedAt: s.fetchedAt || s.fetched_at || s.at || null,
    };
  }
  function normalizeMarkets(doc) {
    const rows = doc && typeof doc === 'object' ? (doc.markets || doc) : null;
    if (!rows || typeof rows !== 'object' || Array.isArray(rows)) return [];
    return Object.entries(rows).map(([code, m]) => {
      m = m || {};
      return {
        code, label: m.label || MARKET_LABEL[code] || code.toUpperCase(),
        currency: m.currency || null, ok: m.ok === undefined ? null : m.ok, status: m.status || null,
        date: m.pricesUpdated || m.date || null, priced: m.priced ?? null, deals: m.deals ?? null,
        offers: m.offers ?? null, review: m.review ?? null, unmatched: m.unmatched ?? null,
        sourcesOk: m.sourcesOk ?? null, sourcesError: m.sourcesError ?? null,
        notFetched: Array.isArray(m.notFetched) ? m.notFetched : [],
        prices: m.prices || ('prices/' + code + '.json'),
      };
    });
  }
  function normalizeHistory(h) {
    let runs = Array.isArray(h) ? h : (h && (h.runs || h.history || h.items)) || [];
    return runs.map((r) => ({
      date: r.date || r.updated || r.finishedAt || r.startedAt || r.ts || '',
      durationSec: r.durationSec ?? r.duration_sec ?? r.duration ?? null,
      offers: r.offers ?? r.items ?? r.offersTotal ?? null,
      deals: r.deals ?? r.dealsCount ?? null,
      priced: r.priced ?? r.pricedIngredients ?? r.priced_ingredients ?? null,
      unmatched: r.unmatched ?? null,
      perSource: r.perSource || r.per_source || r.sources || r.byStore || r.perStore || null,
      ok: r.ok,
    })).filter((r) => r.date).sort((a, b) => String(a.date).localeCompare(String(b.date)));
  }
  function normalizeUnmatched(u) {
    let items = [];
    if (Array.isArray(u)) items = u;
    else if (u && typeof u === 'object') {
      if (Array.isArray(u.items)) items = u.items;
      else {
        if (Array.isArray(u.unmatched)) items = items.concat(u.unmatched.map((x) => Object.assign({ status: 'unmatched' }, x)));
        if (Array.isArray(u.review)) items = items.concat(u.review.map((x) => Object.assign({ status: 'review' }, x)));
      }
    }
    const seen = new Set();
    const out = [];
    for (const it of items) {
      const title = it.title || it.name || it.product || it.text || '';
      const store = it.store || it.shop || it.chain || '';
      const key = it.key || it.id || `${fold(title)}|${store}`;
      if (seen.has(key)) continue;
      seen.add(key);
      let status = String(it.status || it.state || 'unmatched').toLowerCase();
      if (!/review|check|kontrola/.test(status)) status = 'unmatched'; else status = 'review';
      let suggestions = it.suggestions || it.candidates || it.top || [];
      suggestions = suggestions.map((s) => (typeof s === 'string' ? { id: s } : { id: s.id || s.ingredientId, score: s.score ?? s.confidence }))
        .filter((s) => s.id);
      if (it.ingredientId && !suggestions.some((s) => s.id === it.ingredientId)) {
        suggestions.unshift({ id: it.ingredientId, score: it.confidence ?? it.score });
      }
      out.push({
        key, title, store, status, suggestions,
        source: it.source || it.provider || '',
        price: it.price ?? it.priceCzk ?? null,
        czkPerKg: it.czkPerKg ?? it.unitPrice ?? it.pricePerKg ?? null,
        unit: it.unit || it.pack || it.packText || it.sellUnitSizeText || '',
        url: it.url || it.link || it.href || '',
        validTo: it.validTo || it.valid_to || '',
        count: it.count ?? it.seen ?? null,
        reason: it.reason || it.note || (Array.isArray(it.reasons) ? it.reasons.filter((r) => !/^rule:|^no-candidates$/.test(r)).join(', ') : ''),
        promo: it.promo === true,
        originalPrice: it.originalPrice ?? null,
        proposed: it.ingredientId || null,
      });
    }
    return out;
  }
  function normalizeCatalog(c) {
    const list = Array.isArray(c) ? c : (c && (c.ingredients || c.items)) || [];
    return list.filter((x) => x && x.id).map((x) => {
      const names = [x.nameCs, x.namePluralCs, x.nameEn].filter(Boolean);
      const aliases = Array.isArray(x.aliases) ? x.aliases : [];
      const idx = new Set();
      for (const n of [...names, ...aliases, x.id.replace(/_/g, ' ')]) for (const t of normalizeTokens(n)) idx.add(t);
      return {
        id: x.id, nameCs: x.nameCs || x.id, namePluralCs: x.namePluralCs || '', nameEn: x.nameEn || '',
        category: x.category || '', refPriceCzkPerKg: x.refPriceCzkPerKg ?? null,
        folded: fold([x.nameCs, x.namePluralCs, ...aliases].filter(Boolean).join(' ')),
        idx: [...idx],
      };
    });
  }

  function searchCatalog(query, limit) {
    const q = normalizeTokens(query);
    if (!q.length) return [];
    const fq = fold(query);
    const scored = [];
    for (const ing of state.catalog) {
      let score = 0;
      let matchedAll = true;
      for (const qt of q) {
        let best = 0;
        for (const it of ing.idx) {
          if (it === qt) { best = Math.max(best, 3); break; }
          if (tokenMatches(qt, it)) best = Math.max(best, 1.5);
        }
        if (!best) matchedAll = false;
        score += best;
      }
      if (score === 0) continue;
      if (matchedAll) score += 2;
      if (ing.folded === fq || fold(ing.nameCs) === fq) score += 5;
      if (fold(ing.nameCs).startsWith(fq)) score += 1;
      score -= ing.idx.length * 0.02; // shorter names first on ties
      scored.push({ ing, score });
    }
    scored.sort((a, b) => b.score - a.score || a.ing.nameCs.localeCompare(b.ing.nameCs, 'cs'));
    return scored.slice(0, limit || 10);
  }

  async function loadAll() {
    state.diag = [];
    const [prices, health, history, unmatched, catalog, mappings, marketsDoc] = await Promise.all([
      tryLoad('prices.json', DATA.prices, fetchJson),
      tryLoad('health.json', DATA.health, fetchJson),
      tryLoad('history.json', DATA.history, fetchJson),
      tryLoad('unmatched.json', DATA.unmatched, fetchJson),
      tryLoad('catalog/ingredients.json', DATA.catalog, fetchJson),
      tryLoad('mappings.json (kopie)', DATA.mappings, fetchJson),
      tryLoad('markets.json', DATA.markets, fetchJson),
    ]);
    state.prices = prices;
    state.markets = normalizeMarkets(marketsDoc);
    state.health = normalizeHealth(health);
    state.history = normalizeHistory(history);
    state.unmatched = normalizeUnmatched(unmatched);
    state.catalog = normalizeCatalog(catalog);
    state.catalogById = new Map(state.catalog.map((c) => [c.id, c]));
    state.mappings = mappings;
    renderAll();
    loadQa();
    if (state.settings.token) checkLatestRun().catch(() => {});
  }

  // ---------------------------------------------------------------------
  // GitHub API
  // ---------------------------------------------------------------------
  function gh(path, opts) {
    const s = state.settings;
    const headers = Object.assign({ Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' }, (opts && opts.headers) || {});
    if (s.token) headers.Authorization = 'Bearer ' + s.token;
    return fetch(API + path, Object.assign({}, opts, { headers })).then(async (r) => {
      if (r.status === 204) return null;
      const text = await r.text();
      let body = null;
      try { body = text ? JSON.parse(text) : null; } catch (e) { body = text; }
      if (!r.ok) {
        const msg = (body && body.message) || r.statusText || ('HTTP ' + r.status);
        const err = new Error(`${r.status}: ${msg}`);
        err.status = r.status; err.body = body;
        throw err;
      }
      return body;
    });
  }
  function repoPath() { return `/repos/${encodeURIComponent(state.settings.owner)}/${encodeURIComponent(state.settings.repo)}`; }
  function actionsUrl() { return `https://github.com/${state.settings.owner}/${state.settings.repo}/actions`; }

  async function testConnection() {
    const out = [];
    const repo = await gh(repoPath());
    out.push({ ok: true, text: `Repozitář ${repo.full_name} (výchozí větev ${repo.default_branch})` });
    if (repo.default_branch && !state.settings.branch) { state.settings.branch = repo.default_branch; }
    const perm = repo.permissions || {};
    out.push({ ok: !!perm.push, text: perm.push ? 'Zápis do obsahu: OK' : 'Zápis do obsahu: token nemá právo push (contents: write)' });
    try {
      await gh(`${repoPath()}/contents/${state.settings.mappingsPath}?ref=${encodeURIComponent(state.settings.branch)}`);
      out.push({ ok: true, text: `${state.settings.mappingsPath} existuje` });
    } catch (e) {
      out.push({ ok: e.status === 404, text: e.status === 404 ? `${state.settings.mappingsPath} zatím neexistuje – bude vytvořen při prvním uložení` : `${state.settings.mappingsPath}: ${e.message}` });
    }
    try {
      const wf = await gh(`${repoPath()}/actions/workflows/${encodeURIComponent(state.settings.workflow)}`);
      out.push({ ok: true, text: `Workflow „${wf.name}“ (${wf.path}) nalezen, stav ${wf.state}` });
    } catch (e) {
      out.push({ ok: false, text: `Workflow ${state.settings.workflow}: ${e.message}` });
    }
    return out;
  }

  async function dispatchWorkflow() {
    await gh(`${repoPath()}/actions/workflows/${encodeURIComponent(state.settings.workflow)}/dispatches`, {
      method: 'POST', body: JSON.stringify({ ref: state.settings.branch || 'main' }),
    });
  }

  async function checkLatestRun() {
    const r = await gh(`${repoPath()}/actions/workflows/${encodeURIComponent(state.settings.workflow)}/runs?per_page=1`);
    const run = r && r.workflow_runs && r.workflow_runs[0];
    const badge = $('run-status');
    if (!run) { badge.textContent = 'žádný běh'; badge.className = 'badge'; return null; }
    const st = run.status === 'completed' ? run.conclusion : run.status;
    badge.textContent = `${translateRunStatus(st)} · ${fmtDate(run.updated_at)}`;
    badge.className = 'badge ' + (st === 'success' ? 'ok' : (st === 'failure' || st === 'cancelled' ? 'err' : 'warn'));
    badge.title = run.html_url;
    $('link-actions').href = run.html_url;
    return run;
  }
  function translateRunStatus(s) {
    return { success: 'úspěch', failure: 'selhal', cancelled: 'zrušen', queued: 've frontě', in_progress: 'běží', waiting: 'čeká', requested: 'požadován', skipped: 'přeskočen', timed_out: 'vypršel' }[s] || s || '–';
  }

  /** Reads data/mappings.json from the repo; returns {sha, json}. */
  async function readMappings() {
    try {
      const r = await gh(`${repoPath()}/contents/${state.settings.mappingsPath}?ref=${encodeURIComponent(state.settings.branch)}`);
      const text = b64decodeUtf8(r.content || '');
      let json = null;
      try { json = text.trim() ? JSON.parse(text) : null; } catch (e) { throw new Error('mappings.json není platný JSON – opravte ho ručně v repozitáři'); }
      return { sha: r.sha, json };
    } catch (e) {
      if (e.status === 404) return { sha: null, json: null };
      throw e;
    }
  }

  /** Merges pending changes into the mappings document.
   * Rule contract of pipeline/mapper.py (load_rules): {pattern, store: "<store>"|"*", ingredientId: "<id>"|"ignore", note}.
   * `pattern` is folded and must occur as a whole-word substring of the folded offer title, so the
   * full folded title is used – it matches exactly this product (and its later re-appearances).
   * Rules with the same pattern+store are replaced; everything else in the file is kept. */
  function applyPending(doc, pending) {
    if (!doc || typeof doc !== 'object' || Array.isArray(doc)) doc = { v: 1, rules: Array.isArray(doc) ? doc : [] };
    if (!Array.isArray(doc.rules)) doc.rules = [];
    if (!doc.v) doc.v = 1;
    const today = new Date().toISOString().slice(0, 10);
    for (const p of pending) {
      const store = p.store || '*';
      const rule = {
        pattern: p.match, store,
        ingredientId: p.action === 'map' ? p.ingredientId : 'ignore',
        note: `panel ${today}` + (p.source && p.source !== p.store ? ` · ${p.source}` : ''),
        title: p.title,
      };
      const i = doc.rules.findIndex((r) => r && fold(r.pattern || '') === rule.pattern && (r.store || '*') === store);
      if (i >= 0) doc.rules[i] = Object.assign({}, doc.rules[i], rule); else doc.rules.push(rule);
    }
    doc.updated = today;
    return doc;
  }

  async function savePending() {
    const pending = state.pending.slice();
    if (!pending.length) return;
    if (!state.settings.token) throw new Error('Chybí GitHub token – nastavte ho v záložce Nastavení.');
    let attempt = 0;
    for (;;) {
      attempt++;
      const { sha, json } = await readMappings();
      const doc = applyPending(json, pending);
      const content = JSON.stringify(doc, null, 2) + '\n';
      const titles = pending.map((p) => p.title).filter(Boolean);
      const message = pending.length === 1
        ? `panel: mapping ${titles[0] || pending[0].match}`
        : `panel: mapping ${titles.slice(0, 3).join(', ')}${pending.length > 3 ? ` +${pending.length - 3}` : ''} (${pending.length} pravidel)`;
      const body = { message, content: b64encodeUtf8(content), branch: state.settings.branch || 'main' };
      if (sha) body.sha = sha;
      try {
        const r = await gh(`${repoPath()}/contents/${state.settings.mappingsPath}`, { method: 'PUT', body: JSON.stringify(body) });
        for (const p of pending) state.handled[p.key] = { action: p.action, ingredientId: p.ingredientId || null, ts: Date.now() };
        state.pending = state.pending.filter((p) => !pending.includes(p));
        lsSet(LS.pending, state.pending); lsSet(LS.handled, state.handled);
        return r;
      } catch (e) {
        if ((e.status === 409 || e.status === 422) && attempt < 3) continue; // sha conflict → re-read and retry
        throw e;
      }
    }
  }

  // ---------------------------------------------------------------------
  // Rendering – overview
  // ---------------------------------------------------------------------
  function renderAll() {
    renderOverview();
    renderHealth();
    renderMarkets();
    renderPerSource();
    renderHistory();
    renderQueue();
    renderPrices();
    renderDeals();
    renderSettings();
    renderDiag();
    updateConnBadge();
  }

  function renderOverview() {
    const h = state.health || {};
    const p = state.prices || {};
    const deals = Array.isArray(p.deals) ? p.deals : [];
    const priced = p.czkPerKg ? Object.keys(p.czkPerKg).length : null;
    const offers = h.offers ?? (h.sources ? h.sources.reduce((a, s) => a + (Number(s.items) || 0), 0) : null);
    $('ov-date').textContent = fmtDate(h.date || p.updated);
    $('ov-date').title = h.date || p.updated || '';
    $('ov-duration').textContent = fmtDuration(h.durationSec);
    $('ov-offers').textContent = offers === null || offers === undefined ? '–' : fmtNum(offers, 0);
    $('ov-deals').textContent = fmtNum(h.deals ?? deals.length, 0);
    $('ov-priced').textContent = fmtNum(h.priced ?? priced, 0);
    const queueOpen = state.unmatched.filter((u) => !state.handled[u.key]).length;
    $('ov-unmatched').textContent = fmtNum(queueOpen, 0);
    $('queue-count').textContent = String(queueOpen);
    $('run-workflow-name').textContent = state.settings.workflow;
    $('link-actions').href = actionsUrl();
    const gn = $('global-notice');
    gn.innerHTML = '';
    if (!state.prices && !state.health) {
      gn.append(el('div', { class: 'notice warn' }, 'Zatím nejsou k dispozici žádná data pipeline (prices.json, data/health.json). Po prvním běhu workflow se sem načtou automaticky.'));
    }
  }

  function renderHealth() {
    const ul = $('health-list');
    ul.innerHTML = '';
    const h = state.health;
    $('health-updated').textContent = h && h.date ? 'k ' + fmtDate(h.date) : '';
    if (!h || !h.sources.length) { ul.append(el('li', { class: 'muted', text: 'health.json není k dispozici.' })); return; }
    for (const s of h.sources) {
      const cls = s.skipped ? 'warn' : (s.ok === null ? '' : (s.ok ? 'ok' : 'err'));
      const detail = [];
      if (s.items !== null) detail.push(`${fmtNum(s.items, 0)} nabídek`);
      if (s.deals !== null) detail.push(`${fmtNum(s.deals, 0)} akcí`);
      if (s.matched !== null) detail.push(`${fmtNum(s.matched, 0)} přiřazeno`);
      if (s.requests !== null) detail.push(`${fmtNum(s.requests, 0)} požadavků`);
      if (s.durationSec !== null) detail.push(fmtDuration(s.durationSec));
      if (s.error) detail.push(String(s.error));
      ul.append(el('li', null,
        el('span', { class: 'dot ' + cls }),
        el('span', { class: 'name', text: s.label }),
        el('span', { class: 'badge ' + cls, text: s.skipped ? 'přeskočen' : (s.ok === null ? '?' : (s.ok ? 'ok' : 'chyba')) }),
        el('span', { class: 'detail', text: detail.join(' · ') }),
      ));
    }
  }

  function renderMarkets() {
    const ul = $('markets-list');
    if (!ul) return;
    ul.innerHTML = '';
    const rows = state.markets || [];
    if (!rows.length) { ul.append(el('li', { class: 'muted', text: 'data/markets.json není k dispozici (jen český trh).' })); return; }
    for (const m of rows) {
      const missing = m.status === 'missing' || m.ok === null;
      const cls = missing ? '' : (m.status === 'error' || m.ok === false ? 'err' : (m.status === 'warning' ? 'warn' : 'ok'));
      const detail = [];
      if (m.date) detail.push('k ' + fmtDate(m.date));
      if (m.priced !== null) detail.push(`${fmtNum(m.priced, 0)} surovin`);
      if (m.deals !== null) detail.push(`${fmtNum(m.deals, 0)} akcí`);
      if (m.offers !== null) detail.push(`${fmtNum(m.offers, 0)} nabídek`);
      if (m.review !== null || m.unmatched !== null) detail.push(`fronta ${fmtNum((m.review || 0) + (m.unmatched || 0), 0)}`);
      if (m.sourcesOk !== null) detail.push(`zdroje ${fmtNum(m.sourcesOk, 0)} ok` + (m.sourcesError ? ` / ${fmtNum(m.sourcesError, 0)} chyba` : ''));
      if (m.notFetched.length) detail.push('bez zdroje: ' + m.notFetched.join(', '));
      ul.append(el('li', null,
        el('span', { class: 'dot ' + cls }),
        el('span', { class: 'name', text: `${m.label} (${m.currency || '?'})` }),
        el('span', { class: 'badge ' + cls, text: missing ? 'bez dat' : (cls === 'err' ? 'chyba' : (cls === 'warn' ? 'varování' : 'ok')) }),
        el('span', { class: 'detail', text: detail.join(' · ') }),
        el('a', { class: 'small mono', href: '../' + m.prices, target: '_blank', rel: 'noopener', text: m.prices }),
      ));
    }
  }

  function renderPerSource() {
    const tbody = $('per-source-table').querySelector('tbody');
    tbody.innerHTML = '';
    const h = state.health;
    const rows = [];
    if (h && h.sources.length) for (const s of h.sources) rows.push([s.label, s.items, s.deals, s.matched, s.unmatched]);
    // Deals per store from prices.json as a second view.
    const p = state.prices;
    const perStoreDeals = {};
    if (p && Array.isArray(p.deals)) for (const d of p.deals) perStoreDeals[d.store] = (perStoreDeals[d.store] || 0) + 1;
    const perStorePrices = {};
    if (p && p.czkPerKg) for (const m of Object.values(p.czkPerKg)) for (const st of Object.keys(m)) perStorePrices[st] = (perStorePrices[st] || 0) + 1;
    if (!rows.length && !Object.keys(perStoreDeals).length && !Object.keys(perStorePrices).length) {
      tbody.append(el('tr', null, el('td', { class: 'muted', text: 'Žádná data.' }))); return;
    }
    const table = $('per-source-table');
    let thead = table.querySelector('thead');
    if (!thead) { thead = el('thead'); table.prepend(thead); }
    thead.innerHTML = '';
    if (rows.length) {
      thead.append(el('tr', null, el('th', { text: 'Zdroj' }), el('th', { class: 'num', text: 'Nabídek' }), el('th', { class: 'num', text: 'Akcí' }), el('th', { class: 'num', text: 'Přiřazeno' }), el('th', { class: 'num', text: 'Nepřiřazeno' })));
      for (const r of rows) tbody.append(el('tr', null, el('td', { text: r[0] }), ...r.slice(1).map((v) => el('td', { class: 'num', text: v === null || v === undefined ? '–' : fmtNum(v, 0) }))));
      tbody.append(el('tr', null, el('td', { colspan: 5, class: 'muted small', text: 'Podle obchodů v prices.json:' })));
    } else {
      thead.append(el('tr', null, el('th', { text: 'Obchod' }), el('th', { class: 'num', text: 'Oceněných surovin' }), el('th', { class: 'num', text: 'Akcí' })));
    }
    for (const st of STORES) {
      if (!perStorePrices[st] && !perStoreDeals[st]) continue;
      const tr = el('tr', null, el('td', { text: STORE_LABEL[st] }), el('td', { class: 'num', text: fmtNum(perStorePrices[st] || 0, 0) + (rows.length ? ' cen' : '') }), el('td', { class: 'num', text: fmtNum(perStoreDeals[st] || 0, 0) + (rows.length ? ' akcí' : '') }));
      if (rows.length) tr.append(el('td'), el('td'));
      tbody.append(tr);
    }
  }

  function renderHistory() {
    const box = $('history-chart');
    box.innerHTML = '';
    const metric = $('hist-metric').value;
    let runs = state.history.filter((r) => r[metric] !== null && r[metric] !== undefined);
    if (!runs.length) { box.append(el('p', { class: 'muted', text: state.history.length ? 'Pro tuto metriku nejsou v historii hodnoty.' : 'history.json není k dispozici – graf se objeví po několika bězích.' })); return; }
    runs = runs.slice(-60);
    const W = 900, H = 220, padL = 44, padR = 10, padT = 16, padB = 34;
    const n = runs.length;
    const max = Math.max(1, ...runs.map((r) => Number(r[metric]) || 0));
    const bw = (W - padL - padR) / n;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('class', 'chart');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Historie běhů');
    const ns = (tag, attrs, text) => {
      const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
      if (text !== undefined) e.textContent = text;
      return e;
    };
    // grid lines
    for (let i = 0; i <= 4; i++) {
      const y = padT + (H - padT - padB) * (1 - i / 4);
      svg.append(ns('line', { x1: padL, x2: W - padR, y1: y, y2: y, class: 'axis' }));
      svg.append(ns('text', { x: padL - 6, y: y + 3, 'text-anchor': 'end', class: 'lbl' }, fmtNum(max * i / 4, 0)));
    }
    runs.forEach((r, i) => {
      const v = Number(r[metric]) || 0;
      const bh = (H - padT - padB) * v / max;
      const x = padL + i * bw + bw * 0.15;
      const y = H - padB - bh;
      const rect = ns('rect', { x, y, width: Math.max(1, bw * 0.7), height: bh, rx: 2, class: 'bar' });
      if (r.ok === false) rect.style.fill = 'var(--err)';
      rect.append(ns('title', {}, `${fmtDate(r.date)}: ${fmtNum(v, 0)}`));
      svg.append(rect);
      if (n <= 20 || i % Math.ceil(n / 12) === 0) {
        const d = String(r.date).slice(5, 10).replace(/(\d{2})-(\d{2})/, '$2.$1.');
        svg.append(ns('text', { x: x + bw * 0.35, y: H - padB + 14, 'text-anchor': 'middle', class: 'lbl' }, d));
      }
      if (n <= 30) svg.append(ns('text', { x: x + bw * 0.35, y: Math.max(padT - 4, y - 3), 'text-anchor': 'middle', class: 'val' }, fmtNum(v, 0)));
    });
    box.append(svg);
    const last = runs[runs.length - 1];
    box.append(el('p', { class: 'muted small', text: `${n} běhů, poslední ${fmtDate(last.date)}${last.durationSec != null ? ', trvání ' + fmtDuration(last.durationSec) : ''}.` }));
  }

  // ---------------------------------------------------------------------
  // Rendering – queue
  // ---------------------------------------------------------------------
  function queueFiltered() {
    const q = fold($('queue-filter').value);
    const st = $('queue-status').value;
    const store = $('queue-store').value;
    const showDone = $('queue-show-done').checked;
    return state.unmatched.filter((u) => {
      if (!showDone && state.handled[u.key]) return false;
      if (st !== 'all' && u.status !== st) return false;
      if (store && u.store !== store) return false;
      if (q && !(fold(u.title).includes(q) || fold(storeLabel(u.store)).includes(q) || fold(u.source).includes(q))) return false;
      return true;
    });
  }

  function renderQueue() {
    const list = $('queue-list');
    list.innerHTML = '';
    // store filter options
    const sel = $('queue-store');
    const cur = sel.value;
    sel.innerHTML = '<option value="">Všechny obchody</option>';
    const stores = [...new Set(state.unmatched.map((u) => u.store).filter(Boolean))].sort();
    for (const s of stores) sel.append(el('option', { value: s, text: storeLabel(s) }));
    sel.value = stores.includes(cur) ? cur : '';

    if (!state.unmatched.length) {
      list.append(el('div', { class: 'empty', text: state.diag.some((d) => d.name === 'unmatched.json' && d.ok) ? 'Fronta je prázdná – všechno je přiřazené.' : 'unmatched.json není k dispozici (zatím neproběhl žádný běh).' }));
      $('queue-info').textContent = '';
      $('queue-more').hidden = true;
      renderPendingBar();
      return;
    }
    const items = queueFiltered();
    const handledCount = state.unmatched.filter((u) => state.handled[u.key]).length;
    $('queue-info').textContent = `${items.length} položek` + (handledCount ? ` · ${handledCount} vyřízených skryto` : '');
    if (!items.length) list.append(el('div', { class: 'empty', text: 'Nic neodpovídá filtru.' }));
    items.slice(0, state.queueShown).forEach((u) => list.append(renderQueueItem(u)));
    $('queue-more').hidden = items.length <= state.queueShown;
    renderPendingBar();
  }

  function pendingFor(key) { return state.pending.find((p) => p.key === key); }

  function renderQueueItem(u) {
    const pend = pendingFor(u.key);
    const done = state.handled[u.key];
    const box = el('div', { class: 'queue-item' + (done ? ' done' : ''), 'data-key': u.key });
    const head = el('div', { class: 'queue-head' },
      el('span', { class: 'title', text: u.title || '(bez názvu)' }),
      el('span', { class: 'badge ' + (u.status === 'review' ? 'warn' : ''), text: u.status === 'review' ? 'ke kontrole' : 'nepřiřazeno' }),
      u.store ? el('span', { class: 'badge primary', text: storeLabel(u.store) }) : null,
      u.source && u.source !== u.store ? el('span', { class: 'badge', text: 'zdroj: ' + (SOURCE_LABEL[u.source] || u.source) }) : null,
      u.promo ? el('span', { class: 'badge warn', text: 'akce' }) : null,
    );
    box.append(head);
    const meta = el('div', { class: 'queue-meta' });
    if (u.price !== null) meta.append(el('span', { text: 'cena ' + fmtPrice(u.price) + (u.unit ? ' / ' + u.unit : '') + (u.originalPrice ? ` (původně ${fmtPrice(u.originalPrice)})` : '') }));
    else if (u.unit) meta.append(el('span', { text: u.unit }));
    if (u.czkPerKg !== null) meta.append(el('span', { text: fmtNum(u.czkPerKg, 1) + ' Kč/kg' }));
    if (u.validTo) meta.append(el('span', { text: 'do ' + fmtDate(u.validTo) }));
    if (u.count !== null) meta.append(el('span', { text: `${u.count}× viděno` }));
    if (u.reason) meta.append(el('span', { text: u.reason }));
    if (u.url) meta.append(el('a', { href: u.url, target: '_blank', rel: 'noopener', text: 'odkaz ↗' }));
    box.append(meta);

    if (done) {
      box.append(el('div', { class: 'small' }, el('span', { class: 'badge ok', text: done.action === 'ignore' ? 'ignorováno' : 'přiřazeno: ' + (catalogName(done.ingredientId)) }), ' ', el('span', { class: 'muted', text: 'uloženo, projeví se v příštím běhu' })));
      return box;
    }

    // Suggestions: from the pipeline, or computed from the catalog.
    let sugg = u.suggestions.slice(0, 3);
    if (sugg.length < 3 && state.catalog.length) {
      for (const r of searchCatalog(u.title, 6)) {
        if (sugg.length >= 3) break;
        if (r.score < 3) break; // only exact-token or all-tokens matches, not loose prefix noise
        if (!sugg.some((s) => s.id === r.ing.id)) sugg.push({ id: r.ing.id, computed: true });
      }
    }
    const chosen = { id: pend && pend.action === 'map' ? pend.ingredientId : (u.status === 'review' && u.proposed && state.catalogById.has(u.proposed) ? u.proposed : null) };
    const suggRow = el('div', { class: 'suggest' });
    const searchInput = el('input', { class: 'input', type: 'search', placeholder: 'Hledat v katalogu surovin…', autocomplete: 'off' });
    const results = el('div', { class: 'search-results', hidden: '' });
    const pickLabel = el('span', { class: 'small muted' });
    const btnAssign = el('button', { class: 'btn small ok', text: 'Přiřadit' });
    const btnIgnore = el('button', { class: 'btn small danger', text: 'Ignorovat' });
    const btnUndo = el('button', { class: 'btn small text', text: 'Zrušit změnu', hidden: '' });

    function setChosen(id) {
      chosen.id = id;
      for (const b of suggRow.querySelectorAll('button')) b.classList.toggle('chosen', b.dataset.id === id);
      pickLabel.textContent = id ? `→ ${catalogName(id)} (${id})` : '';
      btnAssign.disabled = !id;
      if (id) searchInput.value = '';
    }
    function refreshPendingUi() {
      const p = pendingFor(u.key);
      btnUndo.hidden = !p;
      box.style.borderColor = p ? 'var(--primary)' : '';
      if (p) pickLabel.textContent = p.action === 'ignore' ? '→ bude ignorováno (neuloženo)' : `→ ${catalogName(p.ingredientId)} (${p.ingredientId}) – neuloženo`;
    }
    for (const s of sugg) {
      const name = catalogName(s.id);
      const b = el('button', { class: 'btn small', 'data-id': s.id, title: s.id + (s.score != null ? ` · skóre ${fmtNum(s.score, 2)}` : '') + (s.computed ? ' · návrh panelu' : ' · návrh pipeline'), text: name + (s.score != null ? ` (${Math.round(Number(s.score) * (Number(s.score) <= 1 ? 100 : 1))}${Number(s.score) <= 1 ? ' %' : ''})` : '') });
      b.addEventListener('click', () => setChosen(chosen.id === s.id ? null : s.id));
      suggRow.append(b);
    }
    if (!sugg.length) suggRow.append(el('span', { class: 'muted small', text: state.catalog.length ? 'Žádný návrh.' : 'Katalog surovin není načten – vyhledávání není dostupné.' }));
    box.append(suggRow);

    // catalog search
    let hl = -1, lastResults = [];
    function showResults() {
      const q = searchInput.value.trim();
      results.innerHTML = '';
      lastResults = q.length >= 2 ? searchCatalog(q, 12) : [];
      hl = -1;
      if (!lastResults.length) { results.hidden = true; return; }
      lastResults.forEach((r) => {
        const d = el('div', null, el('span', { text: r.ing.nameCs }), el('span', { class: 'id', text: r.ing.id }), r.ing.category ? el('span', { class: 'badge', style: 'margin-left:6px', text: r.ing.category }) : null);
        d.addEventListener('mousedown', (ev) => { ev.preventDefault(); setChosen(r.ing.id); results.hidden = true; });
        results.append(d);
      });
      results.hidden = false;
    }
    searchInput.addEventListener('input', showResults);
    searchInput.addEventListener('focus', showResults);
    searchInput.addEventListener('blur', () => setTimeout(() => { results.hidden = true; }, 150));
    searchInput.addEventListener('keydown', (ev) => {
      if (results.hidden) return;
      const rows = results.children;
      if (ev.key === 'ArrowDown') { hl = Math.min(rows.length - 1, hl + 1); ev.preventDefault(); }
      else if (ev.key === 'ArrowUp') { hl = Math.max(0, hl - 1); ev.preventDefault(); }
      else if (ev.key === 'Enter') { if (hl >= 0 && lastResults[hl]) { setChosen(lastResults[hl].ing.id); results.hidden = true; } ev.preventDefault(); return; }
      else if (ev.key === 'Escape') { results.hidden = true; return; }
      else return;
      [...rows].forEach((r, i) => r.classList.toggle('hl', i === hl));
    });
    box.append(el('div', { class: 'search-box' }, searchInput, results));

    btnAssign.addEventListener('click', () => {
      if (!chosen.id) return;
      addPending(u, 'map', chosen.id);
      refreshPendingUi();
    });
    btnIgnore.addEventListener('click', () => { addPending(u, 'ignore', null); setChosen(null); refreshPendingUi(); });
    btnUndo.addEventListener('click', () => { removePending(u.key); setChosen(null); refreshPendingUi(); renderPendingBar(); });
    box.append(el('div', { class: 'queue-actions' }, btnAssign, btnIgnore, btnUndo, pickLabel));
    setChosen(chosen.id);
    refreshPendingUi();
    return box;
  }

  function catalogName(id) { const c = state.catalogById.get(id); return c ? c.nameCs : (id || '?'); }

  function addPending(u, action, ingredientId) {
    removePending(u.key, true);
    state.pending.push({ key: u.key, match: fold(u.title), title: u.title, store: u.store || null, source: u.source || null, action, ingredientId, ts: Date.now() });
    lsSet(LS.pending, state.pending);
    renderPendingBar();
    toast(action === 'ignore' ? `Ignorovat: ${u.title}` : `${u.title} → ${catalogName(ingredientId)}`, 1800);
  }
  function removePending(key, silent) {
    state.pending = state.pending.filter((p) => p.key !== key);
    lsSet(LS.pending, state.pending);
    if (!silent) renderPendingBar();
  }
  function renderPendingBar() {
    const n = state.pending.length;
    const bar = $('pending-bar');
    bar.hidden = n === 0;
    $('pending-count').textContent = n === 1 ? '1 změna' : (n >= 2 && n <= 4 ? `${n} změny` : `${n} změn`);
    const maps = state.pending.filter((p) => p.action === 'map').length;
    $('pending-detail').textContent = `${maps} přiřazení, ${n - maps} ignorování → ${state.settings.mappingsPath}` + (state.settings.token ? '' : ' (chybí token!)');
    $('btn-pending-save').textContent = n === 1 ? 'Uložit 1 změnu' : (n >= 2 && n <= 4 ? `Uložit ${n} změny` : `Uložit ${n} změn`);
    $('queue-count').textContent = String(state.unmatched.filter((u) => !state.handled[u.key]).length) + (n ? ` · ${n}✎` : '');
  }

  // ---------------------------------------------------------------------
  // Rendering – prices & deals
  // ---------------------------------------------------------------------
  function ingredientMatches(id, q) {
    if (!q) return true;
    const c = state.catalogById.get(id);
    if (fold(id.replace(/_/g, ' ')).includes(q)) return true;
    return !!(c && c.folded.includes(q));
  }
  function activeDealsByIngredient() {
    const t = todayIso();
    const out = new Map();
    const deals = (state.prices && state.prices.deals) || [];
    for (const d of deals) {
      if (!(d.validFrom <= t && d.validTo >= t)) continue;
      if (!out.has(d.ingredientId)) out.set(d.ingredientId, {});
      const m = out.get(d.ingredientId);
      if (!m[d.store] || d.czkPerKg < m[d.store].czkPerKg) m[d.store] = d;
    }
    return out;
  }

  function renderPriceStoreChips() {
    const box = $('price-stores');
    box.innerHTML = '';
    for (const s of STORES) {
      const cb = el('input', { type: 'checkbox' });
      cb.checked = state.priceStores.has(s);
      const chip = el('label', { class: 'chip' + (cb.checked ? ' on' : '') }, cb, STORE_LABEL[s]);
      cb.addEventListener('change', () => { if (cb.checked) state.priceStores.add(s); else state.priceStores.delete(s); chip.classList.toggle('on', cb.checked); state.priceShown = 100; renderPrices(); });
      box.append(chip);
    }
  }

  function renderPrices() {
    const p = state.prices;
    const table = $('price-table');
    const thead = table.querySelector('thead'), tbody = table.querySelector('tbody');
    thead.innerHTML = ''; tbody.innerHTML = '';
    if (!$('price-stores').children.length) renderPriceStoreChips();
    if (!p || !p.czkPerKg) {
      $('prices-meta').textContent = '';
      tbody.append(el('tr', null, el('td', { class: 'muted', text: 'prices.json není k dispozici.' })));
      $('price-more').hidden = true; $('price-info').textContent = '';
      return;
    }
    $('prices-meta').textContent = `· aktualizováno ${fmtDate(p.updated)} · ${Object.keys(p.czkPerKg).length} surovin · v${p.v || 1}`;
    const q = fold($('price-filter').value);
    const onlyDeals = $('price-only-deals').checked;
    const stores = STORES.filter((s) => state.priceStores.has(s));
    const active = activeDealsByIngredient();
    thead.append(el('tr', null, el('th', { text: 'Surovina' }), ...stores.map((s) => el('th', { class: 'num', text: STORE_LABEL[s] })), el('th', { class: 'num', text: 'Medián' }), el('th', { class: 'num', text: 'Ref.' })));
    const ids = Object.keys(p.czkPerKg).sort((a, b) => catalogName(a).localeCompare(catalogName(b), 'cs'));
    const rows = [];
    for (const id of ids) {
      if (!ingredientMatches(id, q)) continue;
      const reg = p.czkPerKg[id] || {};
      const act = active.get(id) || {};
      if (onlyDeals && !stores.some((s) => act[s])) continue;
      const eff = {};
      for (const s of stores) {
        const d = act[s];
        if (d) eff[s] = { v: d.czkPerKg, deal: d };
        else if (reg[s] !== undefined) eff[s] = { v: reg[s], deal: null };
      }
      const vals = Object.values(eff).map((e) => e.v);
      if (!vals.length && !onlyDeals && q === '') { /* still show the row */ }
      rows.push({ id, eff, vals });
    }
    $('price-info').textContent = `${rows.length} surovin`;
    for (const r of rows.slice(0, state.priceShown)) {
      const min = r.vals.length ? Math.min(...r.vals) : null;
      const sorted = r.vals.slice().sort((a, b) => a - b);
      const med = sorted.length ? (sorted.length % 2 ? sorted[(sorted.length - 1) / 2] : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2) : null;
      const c = state.catalogById.get(r.id);
      const tr = el('tr', null, el('td', null, el('span', { text: catalogName(r.id) }), ' ', el('span', { class: 'muted small mono', text: r.id })));
      for (const s of stores) {
        const e = r.eff[s];
        const td = el('td', { class: 'num' + (e && e.v === min && r.vals.length > 1 ? ' best' : '') });
        if (!e) td.textContent = '–';
        else {
          td.textContent = fmtNum(e.v, 1);
          if (e.deal) { td.append(el('span', { class: 'deal-mark', text: '▲ akce', title: `${e.deal.title} · do ${fmtDate(e.deal.validTo)}` })); }
        }
        tr.append(td);
      }
      tr.append(el('td', { class: 'num', text: med === null ? '–' : fmtNum(med, 1) }));
      tr.append(el('td', { class: 'num muted', text: c && c.refPriceCzkPerKg ? fmtNum(c.refPriceCzkPerKg, 0) : '–' }));
      tbody.append(tr);
    }
    if (!rows.length) tbody.append(el('tr', null, el('td', { colspan: stores.length + 3, class: 'muted', text: 'Nic neodpovídá filtru.' })));
    $('price-more').hidden = rows.length <= state.priceShown;
  }

  function renderDeals() {
    const p = state.prices;
    const tbody = $('deals-table').querySelector('tbody');
    tbody.innerHTML = '';
    const deals = (p && Array.isArray(p.deals)) ? p.deals : [];
    $('deals-meta').textContent = deals.length ? `· ${deals.length} celkem` : '';
    if (!deals.length) { tbody.append(el('tr', null, el('td', { colspan: 6, class: 'muted', text: 'Žádné akce.' }))); $('deals-more').hidden = true; $('deals-info').textContent = ''; return; }
    const t = todayIso();
    const q = fold($('price-filter').value);
    const onlyActive = $('deals-only-active').checked;
    const list = deals.filter((d) => state.priceStores.has(d.store) && ingredientMatches(d.ingredientId, q) && (!onlyActive || (d.validFrom <= t && d.validTo >= t)))
      .sort((a, b) => a.validTo.localeCompare(b.validTo) || catalogName(a.ingredientId).localeCompare(catalogName(b.ingredientId), 'cs'));
    $('deals-info').textContent = `${list.length} akcí`;
    for (const d of list.slice(0, state.dealsShown)) {
      const activeNow = d.validFrom <= t && d.validTo >= t;
      const future = d.validFrom > t;
      tbody.append(el('tr', null,
        el('td', null, el('span', { text: catalogName(d.ingredientId) }), ' ', el('span', { class: 'muted small mono', text: d.ingredientId })),
        el('td', { text: storeLabel(d.store) }),
        el('td', { class: 'num', text: fmtNum(d.czkPerKg, 1) }),
        el('td', null, el('span', { text: `${fmtDate(d.validFrom)} – ${fmtDate(d.validTo)}` }), ' ', el('span', { class: 'badge ' + (activeNow ? 'ok' : (future ? 'primary' : '')), text: activeNow ? 'platí' : (future ? 'budoucí' : 'skončila') })),
        el('td', { text: d.title || '' }),
        el('td', null, d.url ? el('a', { href: d.url, target: '_blank', rel: 'noopener', text: '↗' }) : ''),
      ));
    }
    if (!list.length) tbody.append(el('tr', null, el('td', { colspan: 6, class: 'muted', text: 'Nic neodpovídá filtru.' })));
    $('deals-more').hidden = list.length <= state.dealsShown;
  }

  // ---------------------------------------------------------------------
  // Rendering – QA (markdown)
  // ---------------------------------------------------------------------
  async function loadQa() {
    const src = await tryLoad('qa/sources_report.md', DATA.qaSources, fetchText);
    const stats = await tryLoad('qa/content_stats.md', DATA.qaStats, fetchText);
    $('qa-sources').innerHTML = src ? renderMarkdown(src) : '<div class="empty">Zpráva o zdrojích receptů zatím není publikována.<br><span class="small">CI aplikace ji může uložit do <code>docs/data/qa/sources_report.md</code>.</span></div>';
    $('qa-stats').innerHTML = stats ? renderMarkdown(stats) : '<div class="empty">Statistika obsahu zatím není publikována.<br><span class="small">Očekává se v <code>docs/data/qa/content_stats.md</code>.</span></div>';
    renderDiag();
  }

  /** Minimal, safe Markdown renderer (headings, lists, tables, code, emphasis, links). */
  function renderMarkdown(md) {
    const lines = md.replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0;
    const inline = (s) => esc(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    while (i < lines.length) {
      const l = lines[i];
      if (/^```/.test(l)) {
        const buf = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++; out.push('<pre><code>' + esc(buf.join('\n')) + '</code></pre>'); continue;
      }
      const h = /^(#{1,6})\s+(.*)$/.exec(l);
      if (h) { const n = Math.min(3, h[1].length); out.push(`<h${n}>${inline(h[2])}</h${n}>`); i++; continue; }
      if (/^\|/.test(l) && i + 1 < lines.length && /^\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const cells = (row) => row.replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
        let html = '<div class="table-wrap"><table><thead><tr>' + cells(l).map((c) => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>';
        i += 2;
        while (i < lines.length && /^\|/.test(lines[i])) { html += '<tr>' + cells(lines[i]).map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>'; i++; }
        out.push(html + '</tbody></table></div>'); continue;
      }
      if (/^\s*([-*]|\d+\.)\s+/.test(l)) {
        const ordered = /^\s*\d+\./.test(l);
        let html = ordered ? '<ol>' : '<ul>';
        while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) { html += '<li>' + inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, '')) + '</li>'; i++; }
        out.push(html + (ordered ? '</ol>' : '</ul>')); continue;
      }
      if (/^\s*$/.test(l)) { i++; continue; }
      if (/^---+$/.test(l)) { out.push('<hr>'); i++; continue; }
      const buf = [];
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^(#{1,6}\s|```|\||\s*([-*]|\d+\.)\s)/.test(lines[i])) buf.push(lines[i++]);
      out.push('<p>' + inline(buf.join(' ')) + '</p>');
    }
    return out.join('\n');
  }

  // ---------------------------------------------------------------------
  // Rendering – settings & diagnostics
  // ---------------------------------------------------------------------
  function renderSettings() {
    const s = state.settings;
    $('set-token').value = s.token || '';
    $('set-owner').value = s.owner || '';
    $('set-repo').value = s.repo || '';
    $('set-branch').value = s.branch || '';
    $('set-workflow').value = s.workflow || '';
    $('set-mappings').value = s.mappingsPath || '';
  }
  function readSettingsForm() {
    return {
      token: $('set-token').value.trim(),
      owner: $('set-owner').value.trim() || DEFAULT_SETTINGS.owner,
      repo: $('set-repo').value.trim() || DEFAULT_SETTINGS.repo,
      branch: $('set-branch').value.trim() || DEFAULT_SETTINGS.branch,
      workflow: $('set-workflow').value.trim() || DEFAULT_SETTINGS.workflow,
      mappingsPath: $('set-mappings').value.trim().replace(/^\/+/, '') || DEFAULT_SETTINGS.mappingsPath,
    };
  }
  function saveSettings() {
    state.settings = readSettingsForm();
    lsSet(LS.settings, state.settings);
    updateConnBadge();
    renderOverview();
    renderPendingBar();
    toast('Nastavení uloženo (jen v tomto prohlížeči).');
  }
  function updateConnBadge() {
    const b = $('conn-status');
    const s = state.settings;
    b.textContent = s.token ? `GitHub: ${s.owner}/${s.repo}` : 'GitHub: bez tokenu';
    b.className = 'badge ' + (s.token ? 'primary' : 'warn');
  }
  function renderDiag() {
    const ul = $('data-diag');
    ul.innerHTML = '';
    for (const d of state.diag) {
      ul.append(el('li', null, el('span', { class: 'dot ' + (d.ok ? 'ok' : 'err') }), el('span', { class: 'name', text: d.name }), el('span', { class: 'detail mono', text: d.url + (d.ok ? '' : ' – ' + d.error) })));
    }
    if (state.mappings) {
      const n = Array.isArray(state.mappings.rules) ? state.mappings.rules.length : (Array.isArray(state.mappings) ? state.mappings.length : 0);
      ul.append(el('li', null, el('span', { class: 'dot ok' }), el('span', { class: 'name', text: 'pravidla' }), el('span', { class: 'detail', text: `${n} pravidel v publikované kopii mappings.json` })));
    }
  }

  // ---------------------------------------------------------------------
  // Tabs & events
  // ---------------------------------------------------------------------
  function selectTab(name) {
    for (const t of document.querySelectorAll('.tab')) t.setAttribute('aria-selected', String(t.dataset.tab === name));
    for (const p of document.querySelectorAll('.panel')) p.classList.toggle('active', p.id === 'panel-' + name);
    try { history.replaceState(null, '', '#' + name); } catch (e) { /* ignore */ }
  }
  function bind() {
    for (const t of document.querySelectorAll('.tab')) t.addEventListener('click', () => selectTab(t.dataset.tab));
    const initial = (location.hash || '#overview').slice(1);
    selectTab(document.querySelector(`.tab[data-tab="${initial}"]`) ? initial : 'overview');
    window.addEventListener('hashchange', () => {
      const name = location.hash.slice(1);
      if (document.querySelector(`.tab[data-tab="${name}"]`)) selectTab(name);
    });

    $('btn-reload').addEventListener('click', () => { loadAll().then(() => toast('Data obnovena.')); });
    $('hist-metric').addEventListener('change', renderHistory);

    // Run now
    $('btn-run').addEventListener('click', async () => {
      const res = $('run-result');
      if (!state.settings.token) { res.innerHTML = '<span class="badge err">Chybí token</span> Nastavte GitHub token v záložce Nastavení.'; selectTab('settings'); return; }
      if (!confirm(`Spustit workflow ${state.settings.workflow} ve větvi ${state.settings.branch}?`)) return;
      $('btn-run').disabled = true;
      res.textContent = 'Odesílám…';
      try {
        await dispatchWorkflow();
        res.innerHTML = `<span class="badge ok">Spuštěno</span> Běh se objeví za pár sekund v <a href="${esc(actionsUrl())}" target="_blank" rel="noopener">Actions</a>. Výsledky se do panelu načtou po dokončení a publikaci Pages (obnovte stránku).`;
        toast('Workflow spuštěn.');
        setTimeout(() => checkLatestRun().catch(() => {}), 4000);
        setTimeout(() => checkLatestRun().catch(() => {}), 20000);
      } catch (e) {
        res.innerHTML = `<span class="badge err">Chyba</span> ${esc(e.message)}${e.status === 404 ? ' – zkontrolujte název workflow souboru a že má <code>on: workflow_dispatch</code>.' : ''}${e.status === 403 || e.status === 401 ? ' – token potřebuje oprávnění <em>actions: write</em>.' : ''}`;
      } finally { $('btn-run').disabled = false; }
    });

    // Queue
    for (const id of ['queue-filter', 'queue-status', 'queue-store', 'queue-show-done']) {
      $(id).addEventListener('input', () => { state.queueShown = 50; renderQueue(); });
      $(id).addEventListener('change', () => { state.queueShown = 50; renderQueue(); });
    }
    $('queue-more').addEventListener('click', () => { state.queueShown += 50; renderQueue(); });
    $('btn-pending-clear').addEventListener('click', () => { if (confirm('Zahodit všechny neuložené změny?')) { state.pending = []; lsSet(LS.pending, []); renderQueue(); } });
    $('btn-pending-save').addEventListener('click', async () => {
      const btn = $('btn-pending-save');
      btn.disabled = true;
      const n = state.pending.length;
      try {
        const r = await savePending();
        const url = r && r.commit && r.commit.html_url;
        toast(`Uloženo ${n} pravidel do ${state.settings.mappingsPath}. Projeví se v příštím běhu.`, 5000);
        renderQueue(); renderOverview();
        if (url) $('queue-info').append(' · ', el('a', { href: url, target: '_blank', rel: 'noopener', text: 'commit ↗' }));
      } catch (e) {
        alert('Uložení selhalo: ' + e.message + (e.status === 403 || e.status === 401 ? '\nToken potřebuje oprávnění contents: write na tento repozitář.' : ''));
      } finally { btn.disabled = false; }
    });

    // Prices
    $('price-filter').addEventListener('input', () => { state.priceShown = 100; state.dealsShown = 100; renderPrices(); renderDeals(); });
    $('price-only-deals').addEventListener('change', () => { state.priceShown = 100; renderPrices(); });
    $('deals-only-active').addEventListener('change', () => { state.dealsShown = 100; renderDeals(); });
    $('price-more').addEventListener('click', () => { state.priceShown += 100; renderPrices(); });
    $('deals-more').addEventListener('click', () => { state.dealsShown += 100; renderDeals(); });

    // Settings
    $('btn-settings-save').addEventListener('click', saveSettings);
    $('btn-token-show').addEventListener('click', () => { const i = $('set-token'); i.type = i.type === 'password' ? 'text' : 'password'; });
    $('btn-token-forget').addEventListener('click', () => { $('set-token').value = ''; saveSettings(); toast('Token odstraněn z prohlížeče.'); });
    $('btn-settings-test').addEventListener('click', async () => {
      saveSettings();
      const box = $('settings-result');
      box.innerHTML = '<p class="muted">Testuji…</p>';
      if (!state.settings.token) { box.innerHTML = '<div class="notice warn">Zadejte token.</div>'; return; }
      try {
        const res = await testConnection();
        box.innerHTML = '';
        const ul = el('ul', { class: 'health-list' });
        for (const r of res) ul.append(el('li', null, el('span', { class: 'dot ' + (r.ok ? 'ok' : 'err') }), el('span', { class: 'detail', text: r.text })));
        box.append(ul);
        renderSettings(); lsSet(LS.settings, state.settings);
        checkLatestRun().catch(() => {});
      } catch (e) {
        box.innerHTML = `<div class="notice err">Připojení selhalo: ${esc(e.message)}${e.status === 401 ? ' – token je neplatný nebo expirovaný.' : ''}${e.status === 404 ? ' – repozitář nenalezen nebo token k němu nemá přístup.' : ''}</div>`;
      }
    });
  }

  // Expose a few internals for tests / console debugging.
  window.VareskaAdmin = { fold, stem, normalizeTokens, tokenMatches, searchCatalog, renderMarkdown, applyPending, normalizeHealth, normalizeUnmatched, normalizeHistory, normalizeMarkets, state };

  document.addEventListener('DOMContentLoaded', () => { bind(); loadAll(); });
})();
