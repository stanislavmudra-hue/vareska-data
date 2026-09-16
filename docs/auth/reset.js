/* Vareska – password-reset landing page (docs/auth/reset.html).
 *
 * The app calls `resetPasswordForEmail(email, redirectTo: <this page>)`; the
 * e-mail link lands here in one of three shapes (docs/BACKEND.md §2.2, §9):
 *   1. implicit flow:  reset.html#access_token=…&refresh_token=…&type=recovery
 *   2. token hash:     reset.html?token_hash=…&type=recovery   (custom e-mail template)
 *   3. PKCE:           reset.html?code=…                        (needs the verifier
 *      stored by the *same* browser that started the flow – usually not the case
 *      for a link opened from a phone's mail app, so we explain and give up)
 * plus the error shape  #error=access_denied&error_code=otp_expired&error_description=…
 *
 * Tokens are read once, removed from the URL, and used only for
 * `auth.updateUser({password})` (or `PUT /auth/v1/user` when the link carries
 * an access token without a refresh token). Nothing is persisted.
 */
(function () {
  'use strict';

  const CFG = window.VareskaConfig || {};
  const TIMEOUT_MS = 10000;
  const MIN_PASSWORD = 8;

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
  /** Merge `#hash` and `?query` parameters of a Supabase auth redirect into one object. */
  function parseAuthParams(hash, search) {
    const out = {};
    for (const raw of [String(search || ''), String(hash || '')]) {
      const s = raw.replace(/^[#?]/, '');
      if (!s) continue;
      for (const [k, v] of new URLSearchParams(s)) out[k] = v;
    }
    return out;
  }

  /** Decide which flow the URL parameters describe. */
  function detectFlow(p) {
    if (p.error || p.error_code || p.error_description) return 'error';
    if (p.access_token && p.refresh_token) return 'session';
    if (p.access_token) return 'token';
    if (p.token_hash) return 'token_hash';
    if (p.code) return 'pkce';
    return 'none';
  }

  /** Client-side password checks → Czech message or null when fine. */
  function validatePassword(a, b) {
    if (!a || a.length < MIN_PASSWORD) return `Heslo musí mít alespoň ${MIN_PASSWORD} znaků.`;
    if (a !== b) return 'Hesla se neshodují.';
    if (a.length > 72) return 'Heslo je příliš dlouhé (max. 72 znaků).';
    return null;
  }

  /** Czech text for an auth error (URL error params or an AuthApiError). */
  function errorText(e) {
    const code = String((e && (e.code || e.error_code)) || '');
    const msg = String((e && (e.message || e.error_description)) || e || '');
    const name = String((e && e.name) || '');
    if (code === 'otp_expired' || /expired|invalid or has expired/i.test(msg)) return 'Odkaz vypršel nebo už byl použit.';
    if (code === 'access_denied' && !msg) return 'Odkaz je neplatný.';
    if (code === 'weak_password' || /weak|at least \d+ characters/i.test(msg)) return 'Heslo je příliš slabé – použijte alespoň 8 znaků, ideálně písmena i číslice.';
    if (code === 'same_password' || /different from the old password/i.test(msg)) return 'Nové heslo musí být jiné než to staré.';
    if (code === 'over_request_rate_limit' || Number(e && e.status) === 429) return 'Příliš mnoho pokusů – zkuste to za chvíli.';
    if (/code verifier|both auth code and code verifier/i.test(msg)) return 'Tento odkaz lze ověřit jen v prohlížeči, ve kterém bylo obnovení hesla vyžádáno. Nechte si v aplikaci poslat nový e-mail.';
    if (/session_not_found|not logged in|missing sub claim|invalid claim|bad_jwt|jwt/i.test(msg) || Number(e && e.status) === 401 || Number(e && e.status) === 403) return 'Odkaz je neplatný nebo vypršel.';
    if (name === 'TimeoutError' || /failed to fetch|networkerror|load failed|timeout/i.test(msg)) return 'Připojení není k dispozici – zkontrolujte internet a zkuste to znovu.';
    return 'Něco se nepovedlo: ' + (msg || 'neznámá chyba');
  }

  // ---------------------------------------------------------------------
  // Page logic
  // ---------------------------------------------------------------------
  const state = { sb: null, accessToken: null, email: '' };

  function client() {
    if (state.sb) return state.sb;
    if (!window.supabase || typeof window.supabase.createClient !== 'function') return null;
    state.sb = window.supabase.createClient(CFG.supabaseUrl, CFG.supabaseAnonKey, {
      auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false, flowType: 'pkce' },
    });
    return state.sb;
  }

  async function restUser(method, accessToken, body) {
    const res = await withTimeout(fetch(`${String(CFG.supabaseUrl).replace(/\/+$/, '')}/auth/v1/user`, {
      method,
      headers: { apikey: CFG.supabaseAnonKey, Authorization: 'Bearer ' + accessToken, 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    }));
    let json = null;
    try { json = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok) {
      const err = new Error((json && (json.msg || json.message || json.error_description)) || `HTTP ${res.status}`);
      err.status = res.status; err.code = json && (json.error_code || json.code);
      throw err;
    }
    return json;
  }

  function showForm(email) {
    state.email = email || '';
    $('who').textContent = email || 'Vareska';
    notice(null);
    $('reset-form').hidden = false;
    $('password').focus();
  }
  function fail(text) {
    notice('err', text);
    $('help').hidden = false;
    $('reset-form').hidden = true;
  }

  async function init() {
    const params = parseAuthParams(location.hash, location.search);
    const flow = detectFlow(params);
    // Never keep the tokens in the address bar / history.
    if (flow !== 'none') { try { history.replaceState(null, '', location.pathname); } catch (e) { /* ignore */ } }

    if (!CFG.supabaseUrl || !CFG.supabaseAnonKey) { fail('Chybí konfigurace (config.js).'); return; }
    if (flow === 'error') { fail(errorText(params)); return; }
    if (flow === 'none') { fail('Tento odkaz neobsahuje údaje pro obnovení hesla. Otevřete odkaz z e-mailu, nebo si v aplikaci nechte poslat nový.'); return; }

    const sb = client();
    try {
      if (flow === 'session' && sb) {
        const data = await q(sb.auth.setSession({ access_token: params.access_token, refresh_token: params.refresh_token }));
        const user = data && data.user ? data.user : (data && data.session ? data.session.user : null);
        showForm(user && user.email);
        return;
      }
      if (flow === 'token' || (flow === 'session' && !sb)) {
        // Access token only (or the SDK failed to load): talk to GoTrue directly.
        state.accessToken = params.access_token;
        const user = await restUser('GET', state.accessToken);
        showForm(user && user.email);
        return;
      }
      if (!sb) { fail('Knihovna supabase-js se nenačetla (offline nebo blokovaný CDN). Zkuste stránku obnovit.'); return; }
      if (flow === 'token_hash') {
        const data = await q(sb.auth.verifyOtp({ token_hash: params.token_hash, type: params.type || 'recovery' }));
        showForm(data && data.user && data.user.email);
        return;
      }
      if (flow === 'pkce') {
        const data = await q(sb.auth.exchangeCodeForSession(params.code));
        showForm(data && data.session && data.session.user && data.session.user.email);
        return;
      }
    } catch (e) {
      console.warn('[reset] verify', e);
      fail(errorText(e));
    }
  }

  async function submit(ev) {
    ev.preventDefault();
    const a = $('password').value, b = $('password2').value;
    const res = $('form-result');
    const v = validatePassword(a, b);
    if (v) { res.textContent = v; return; }
    $('btn-submit').disabled = true;
    res.textContent = 'Ukládám…';
    try {
      if (state.accessToken) await restUser('PUT', state.accessToken, { password: a });
      else await q(state.sb.auth.updateUser({ password: a }));
      $('reset-form').hidden = true;
      $('done').hidden = false;
      notice('ok', 'Hotovo.');
      try { if (state.sb) await withTimeout(state.sb.auth.signOut({ scope: 'local' }), 3000); } catch (e) { /* ignore */ }
      state.accessToken = null;
    } catch (e) {
      console.warn('[reset] update', e);
      res.textContent = errorText(e);
      $('btn-submit').disabled = false;
    }
  }

  window.VareskaReset = { parseAuthParams, detectFlow, validatePassword, errorText };
  document.addEventListener('DOMContentLoaded', () => {
    $('reset-form').addEventListener('submit', submit);
    init();
  });
})();
