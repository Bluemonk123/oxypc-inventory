/* Global toast messages (2026-10-31) — loaded on every page by base.html.
 *
 * window.showTopToast(type, message, title)
 *   A toast in the top-right stack (#notifToastContainer, shared with the live
 *   notification pop-ups). type = success | danger | warning | primary.
 *   Text only (no HTML). Closes itself after 6 s; ✕ closes it sooner.
 *   Pages call it for actions that don't reload the page.
 *
 * window.flashAsToast()
 *   Turns the page's green ?success= banner into that toast. Runs on EVERY
 *   page automatically (DOMContentLoaded) — the text is the page's own result
 *   message, unchanged; only where it shows moves. Also removes a page's own
 *   copy of the same banner (several pages printed one too, so it showed
 *   twice) and drops ?success= from the address bar so a refresh doesn't
 *   repeat it. Errors and warnings stay as banners.
 */
(function () {
  function norm(s) { return String(s == null ? '' : s).replace(/\+/g, ' ').replace(/\s+/g, ' ').trim(); }

  window.showTopToast = function (type, message, title) {
    var box = document.getElementById('notifToastContainer');
    if (!box || !message) return;
    var el = document.createElement('div');
    el.className = 'toast align-items-center text-bg-' + (type || 'primary') + ' border-0 show shadow mb-2';
    el.setAttribute('role', 'alert');
    var wrap = document.createElement('div'); wrap.className = 'd-flex';
    var body = document.createElement('div'); body.className = 'toast-body';
    if (title) { var t = document.createElement('div'); t.className = 'fw-semibold small'; t.textContent = title; body.appendChild(t); }
    var m = document.createElement('div'); m.className = 'small';
    var ic = document.createElement('i');
    ic.className = 'bi ' + ({ success: 'bi-check-circle', danger: 'bi-exclamation-circle', warning: 'bi-exclamation-triangle' }[type] || 'bi-info-circle') + ' me-2';
    m.appendChild(ic); m.appendChild(document.createTextNode(message)); body.appendChild(m);
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'btn-close btn-close-white me-2 m-auto'; btn.setAttribute('aria-label', 'Close');
    btn.addEventListener('click', function () { el.remove(); });
    wrap.appendChild(body); wrap.appendChild(btn); el.appendChild(wrap);
    box.appendChild(el);
    setTimeout(function () { if (el.parentNode) el.remove(); }, 6000);
  };

  var done = false;
  window.flashAsToast = function () {
    if (done) return;
    var flash = document.getElementById('flashMessages');
    var param = null;
    try { param = new URLSearchParams(location.search).get('success'); } catch (e) {}
    var shown = false;
    if (flash) {
      flash.querySelectorAll('.alert-success').forEach(function (a) {
        var text = norm(a.textContent);
        a.remove();
        if (text) { window.showTopToast('success', text); shown = true; }
      });
    }
    if (param) {
      var p = norm(param);
      // A page's own copy of the same message.
      document.querySelectorAll('.alert-success').forEach(function (a) {
        if (norm(a.textContent) === p) a.remove();
      });
      if (!shown && p) { window.showTopToast('success', p); }
      try {
        var u = new URL(location.href); u.searchParams.delete('success');
        history.replaceState(history.state, '', u.pathname + (u.searchParams.toString() ? '?' + u.searchParams : '') + u.hash);
      } catch (e) {}
    }
    done = true;
  };

  document.addEventListener('DOMContentLoaded', function () { window.flashAsToast(); });
})();
