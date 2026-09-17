/* Vareska – account-deletion page (docs/auth/delete.html).
 *
 * Google Play "Data safety" requires a public URL where a user can request
 * account deletion. The page signs the user in (e-mail + password, or Google
 * OAuth via `signInWithOAuth` – works once the Google provider is enabled in
 * the Supabase dashboard and this page is on the Redirect URL allow-list),
 * shows what will be deleted, then mirrors the app (docs/BACKEND.md §2.3):
 *   1. remove own photos from bucket `recipe-photos/<uid>/` (best effort)
 *   2. rpc('delete_my_account')  – deletes the auth user; profile, ratings,
 *      user_recipes and moderators cascade
 *   3. local sign-out
 * The session lives in sessionStorage only (it must survive the OAuth
 * redirect) and is cleared afterwards.
 */
(function () {
  'use strict';

  const CFG = window.VareskaConfig || {};
  const TIMEOUT_MS = 12000;
  const BUCKET = CFG.photoBucket || 'recipe-photos';

  const $ = (id) => document.getElementById(id);
  function esc(s) { return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  function notice(kind, text) {
    const box = $('notice');
    if (box) box.innerHTML = kind ? `<div class="notice ${kind}">${esc(text)}</div>` : '';
  }
  function withTimeout(promise, ms) {
    let timer;
    const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(Object.assign(new Error('timeout'), { name: 'TimeoutError' })), ms || TIMEOUT_MS); });
    return Promise.race([Promise.resolve(promise), timeout]).finally(() => clearTimeout(timer));
  }
  async function q(builder) {
    const r = await withTimeout(builder);
    if (r && r.error) throw r.error;
    return r ? r.data : null;
  }

  // ---------------------------------------------------------------------
  // Pure helpers (exposed for tests)
  // ---------------------------------------------------------------------
  function parseAuthParams(hash, search) {
    const out = {};
    for (const raw of [String(search || ''), String(hash || '')]) {
      const s = raw.replace(/^[#?]/, '');
      if (!s) continue;
      for (const [k, v] of new URLSearchParams(s)) out[k] = v;
    }
    return out;
  }

  /** True when the Supabase error says the OAuth provider is switched off. */
  function isProviderDisabled(e) {
    const msg = String((e && (e.message || e.error_description)) || e || '');
    return /provider is not enabled|unsupported provider|provider.*disabled/i.test(msg);
  }

  /** Czech text for an auth / RPC error. */
  function errorText(e) {
    const code = String((e && (e.code || e.error_code)) || '');
    const msg = String((e && (e.message || e.error_description)) || e || '');
    const name = String((e && e.name) || '');
    const status = Number(e && e.status);
    if (isProviderDisabled(e)) return 'Přihlášení přes Google zatím není zapnuté. Použijte e‑mail a heslo, nebo nám napište (viz níže).';
    if (code === 'invalid_credentials' || /invalid login credentials/i.test(msg)) return 'Nesprávný e‑mail nebo heslo.';
    if (code === 'email_not_confirmed' || /email not confirmed/i.test(msg)) return 'E‑mail účtu ještě není potvrzený. Potvrďte ho z uvítacího e‑mailu, nebo nám napište.';
    if (code === 'otp_expired' || /expired/i.test(msg)) return 'Odkaz vypršel nebo už byl použit. Zkuste přihlášení znovu.';
    if (code === 'over_request_rate_limit' || status === 429) return 'Příliš mnoho pokusů – zkuste to za chvíli.';
    if (/code verifier|both auth code and code verifier/i.test(msg)) return 'Přihlášení přes Google se nepodařilo dokončit v tomto prohlížeči. Zkuste to znovu ve stejném okně, nebo použijte e‑mail a heslo.';
    if (/not_authenticated/i.test(msg) || status === 401 || status === 403 || /jwt|session_not_found/i.test(msg)) return 'Přihlášení vypršelo. Přihlaste se prosím znovu.';
    if (name === 'TimeoutError' || /failed to fetch|networkerror|load failed|timeout/i.test(msg)) return 'Připojení není k dispozici – zkontrolujte internet a zkuste to znovu.';
    return 'Něco se nepovedlo: ' + (msg || 'neznámá chyba');
  }

  // ---------------------------------------------------------------------
  // Page logic
  // ---------------------------------------------------------------------
  const state = { sb: null, user: null };

  /** sessionStorage-backed adapter so the session never outlives the tab. */
  function tabStorage() {
    const mem = {};
    let ss = null;
    try { ss = window.sessionStorage; } catch (e) { ss = null; }
    return {
      getItem: (k) => { try { return ss ? ss.getItem(k) : (k in mem ? mem[k] : null); } catch (e) { return k in mem ? mem[k] : null; } },
      setItem: (k, v) => { try { if (ss) ss.setItem(k, v); else mem[k] = v; } catch (e) { mem[k] = v; } },
      removeItem: (k) => { try { if (ss) ss.removeItem(k); } catch (e) { /* ignore */ } delete mem[k]; },
    };
  }

  function client() {
    if (state.sb) return state.sb;
    if (!window.supabase || typeof window.supabase.createClient !== 'function') return null;
    state.sb = window.supabase.createClient(CFG.supabaseUrl, CFG.supabaseAnonKey, {
      auth: { persistSession: true, autoRefreshToken: false, detectSessionInUrl: true, flowType: 'pkce', storage: tabStorage() },
    });
    return state.sb;
  }

  function pageUrl() { return location.origin + location.pathname; }

  function show(id) {
    for (const s of ['login-form', 'confirm', 'confirm2', 'done']) $(s).hidden = s !== id;
  }
  function showConfirm(user) {
    state.user = user;
    const label = (user && user.email) || 'účet Vareska';
    $('who').textContent = label;
    $('who2').textContent = label;
    $('delete-result').textContent = '';
    show('confirm');
  }
  function showLogin() {
    state.user = null;
    $('btn-login').disabled = false;
    $('btn-google').disabled = false;
    $('login-result').textContent = '';
    show('login-form');
  }
  function stripUrl() { try { history.replaceState(null, '', location.pathname); } catch (e) { /* ignore */ } }

  async function init() {
    const params = parseAuthParams(location.hash, location.search);
    const hasAuthParams = !!(params.code || params.access_token || params.error || params.error_code);
    if (!CFG.supabaseUrl || !CFG.supabaseAnonKey) { notice('err', 'Chybí konfigurace (config.js).'); return; }
    const sb = client();
    if (!sb) { notice('err', 'Knihovna supabase-js se nenačetla (offline nebo blokovaný CDN). Zkuste stránku obnovit.'); return; }

    if (params.error || params.error_code) {
      // e.g. #error=…&error_code=validation_failed&error_description=Unsupported provider
      stripUrl();
      notice('err', errorText(params));
      showLogin();
      return;
    }
    try {
      // detectSessionInUrl exchanges `?code=` / `#access_token` on creation.
      const data = await q(sb.auth.getSession());
      if (hasAuthParams) stripUrl();
      const session = data && data.session;
      if (session && session.user) { showConfirm(session.user); return; }
    } catch (e) {
      console.warn('[delete] session', e);
      if (hasAuthParams) stripUrl();
      notice('err', errorText(e));
    }
    showLogin();
  }

  async function login(ev) {
    ev.preventDefault();
    const res = $('login-result');
    res.textContent = 'Přihlašuji…';
    $('btn-login').disabled = true;
    try {
      const data = await q(state.sb.auth.signInWithPassword({ email: $('email').value.trim(), password: $('password').value }));
      $('password').value = '';
      notice(null);
      showConfirm(data && data.user);
    } catch (e) {
      console.warn('[delete] login', e);
      res.textContent = errorText(e);
      $('btn-login').disabled = false;
    }
  }

  /** GoTrue answers `/authorize` for a disabled provider with a bare JSON 400
   *  instead of redirecting back, so ask `/auth/v1/settings` first. Returns
   *  true/false, or null when the check itself failed (then just try). */
  async function providerEnabled(name) {
    try {
      const res = await withTimeout(fetch(`${String(CFG.supabaseUrl).replace(/\/+$/, '')}/auth/v1/settings`, { headers: { apikey: CFG.supabaseAnonKey } }), 6000);
      if (!res.ok) return null;
      const json = await res.json();
      const ext = json && json.external;
      return ext && typeof ext[name] === 'boolean' ? ext[name] : null;
    } catch (e) { return null; }
  }

  async function loginGoogle() {
    const res = $('login-result');
    res.textContent = 'Přesměrovávám na Google…';
    $('btn-google').disabled = true;
    try {
      if ((await providerEnabled('google')) === false) throw new Error('Unsupported provider: provider is not enabled');
      await q(state.sb.auth.signInWithOAuth({ provider: 'google', options: { redirectTo: pageUrl() } }));
      // The browser is navigating away; nothing more to do here.
    } catch (e) {
      console.warn('[delete] google', e);
      res.textContent = errorText(e);
      $('btn-google').disabled = false;
    }
  }

  async function removeOwnPhotos(sb, uid) {
    try {
      const files = await q(sb.storage.from(BUCKET).list(uid, { limit: 1000 }));
      const names = (files || []).map((f) => `${uid}/${f.name}`);
      if (names.length) await q(sb.storage.from(BUCKET).remove(names));
    } catch (e) {
      // The RPC removes the storage rows as a safety net.
      console.warn('[delete] photos', e);
    }
  }

  async function deleteFinal() {
    const res = $('delete-result');
    const btn = $('btn-delete-final');
    btn.disabled = true; $('btn-cancel').disabled = true;
    res.textContent = 'Mažu účet…';
    const sb = state.sb;
    let ok = false;
    try {
      const uid = state.user && state.user.id;
      if (uid) await removeOwnPhotos(sb, uid);
      await q(sb.rpc('delete_my_account'));
      ok = true;
    } catch (e) {
      console.warn('[delete] rpc', e);
      res.textContent = errorText(e);
      btn.disabled = false; $('btn-cancel').disabled = false;
      return;
    } finally {
      if (ok) { try { await withTimeout(sb.auth.signOut({ scope: 'local' }), 3000); } catch (e) { /* ignore */ } }
    }
    state.user = null;
    show('done');
    notice('ok', 'Účet byl smazán.');
  }

  async function logout() {
    try { await withTimeout(state.sb.auth.signOut({ scope: 'local' }), 3000); } catch (e) { /* ignore */ }
    notice('info', 'Odhlášeno. Účet zůstává beze změny.');
    showLogin();
  }

  window.VareskaDelete = { parseAuthParams, errorText, isProviderDisabled };
  document.addEventListener('DOMContentLoaded', () => {
    $('login-form').addEventListener('submit', login);
    $('btn-google').addEventListener('click', loginGoogle);
    $('btn-delete').addEventListener('click', () => show('confirm2'));
    $('btn-cancel').addEventListener('click', () => show('confirm'));
    $('btn-delete-final').addEventListener('click', deleteFinal);
    $('btn-logout').addEventListener('click', logout);
    init();
  });
})();
