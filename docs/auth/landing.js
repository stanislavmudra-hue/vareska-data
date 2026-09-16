/* Vareska – account landing page (Supabase Site URL).
 * Supabase sends the user here when a redirect URL is not on the allow-list
 * or for e-mail confirmations. Recovery links are forwarded to reset.html
 * with their parameters intact; confirmations just show a Czech message.
 */
(function () {
  'use strict';
  function esc(s) { return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  function params(hash, search) {
    const out = {};
    for (const raw of [String(search || ''), String(hash || '')]) {
      const s = raw.replace(/^[#?]/, '');
      if (s) for (const [k, v] of new URLSearchParams(s)) out[k] = v;
    }
    return out;
  }
  /** 'reset' → forward to reset.html; 'confirmed' → show message; 'error'; or null. */
  function classify(p) {
    if (p.type === 'recovery' || p.code || p.token_hash) return 'reset';
    if (p.error || p.error_code) return 'error';
    if (p.access_token || p.type) return 'confirmed';
    return null;
  }
  function show(kind, text) {
    const box = document.getElementById('notice');
    if (box) box.innerHTML = `<div class="notice ${kind}">${esc(text)}</div>`;
  }
  function run() {
    const p = params(location.hash, location.search);
    const kind = classify(p);
    if (!kind) return;
    if (kind === 'reset') { location.replace('reset.html' + location.search + location.hash); return; }
    try { history.replaceState(null, '', location.pathname); } catch (e) { /* ignore */ }
    if (kind === 'error') {
      const code = String(p.error_code || p.error || '');
      show('err', code === 'otp_expired' ? 'Odkaz vypršel nebo už byl použit. Nechte si v aplikaci poslat nový.' : 'Odkaz je neplatný: ' + (p.error_description || code));
      return;
    }
    show('ok', 'E-mail je potvrzený. Vraťte se do aplikace Vareska a přihlaste se.');
  }
  window.VareskaLanding = { params, classify };
  document.addEventListener('DOMContentLoaded', run);
})();
