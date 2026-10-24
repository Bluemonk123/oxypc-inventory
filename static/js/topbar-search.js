/* Top-bar Search / Scan Tag Number (2026-10-24) — every page, see base.html.
 *
 * Type part of a Tag Number or Serial Number: a one-line-per-device dropdown
 * (Tag | Lot | CPU | RAM | Storage | Grade | Stage) opens centred under the
 * box. Click a row or press Enter to open /devices/{tag}. Arrow keys move the
 * highlight, Esc / click outside closes.
 *
 * Scanner: types the tag and Enter faster than the dropdown loads, so Enter
 * with nothing highlighted looks the tag up at once — exact match or a single
 * result opens it, anything else shows the dropdown. The box text is selected
 * on Enter and on focus so the next scan overwrites it; the opened tag is kept
 * in sessionStorage and shown, selected, in the box on its device page.
 *
 * Data: GET /devices/api/quick-search?q= (routers/devices.py).
 */
(function () {
  var input = document.getElementById('tbSearch');
  var wrap = document.getElementById('tbSearchWrap');
  if (!input || !wrap) return;

  var KEY = 'tbSearchLastTag';
  var menu = document.createElement('div');
  menu.id = 'tbSearchMenu';
  menu.setAttribute('role', 'listbox');
  menu.hidden = true;
  wrap.appendChild(menu);

  var results = [];
  var active = -1;
  var seq = 0;          // request counter — a slower, older response is ignored
  var timer = null;

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function store(tag) {
    try { sessionStorage.setItem(KEY, tag); } catch (e) { /* storage blocked: fine */ }
  }
  function stored() {
    try { return sessionStorage.getItem(KEY) || ''; } catch (e) { return ''; }
  }
  function open(tag) {
    if (!tag) return;
    store(tag);
    close();
    window.location.href = '/devices/' + encodeURIComponent(tag);
  }
  function close() {
    menu.hidden = true;
    active = -1;
  }
  function highlight(i) {
    var rows = menu.querySelectorAll('.tbs-row');
    rows.forEach(function (r, n) { r.classList.toggle('active', n === i); });
    active = i;
    if (rows[i] && rows[i].scrollIntoView) rows[i].scrollIntoView({ block: 'nearest' });
  }
  function render() {
    if (!results.length) {
      menu.innerHTML = '<div class="tbs-empty">No tag found</div>';
    } else {
      menu.innerHTML = results.map(function (r, i) {
        return '<div class="tbs-row" role="option" data-i="' + i + '">' +
          '<span class="tbs-tag">' + esc(r.barcode) + '</span>' +
          '<span>' + esc(r.lot_number) + '</span>' +
          '<span class="tbs-cpu" title="' + esc(r.cpu) + '">' + esc(r.cpu) + '</span>' +
          '<span>' + esc(r.ram) + '</span>' +
          '<span>' + esc(r.storage) + '</span>' +
          '<span>' + esc(r.grade) + '</span>' +
          '<span class="tbs-stage"><span class="badge bg-' + esc(r.stage_color || 'secondary') + '">' + esc(r.stage) + '</span></span>' +
          '</div>';
      }).join('');
    }
    active = -1;
    menu.hidden = false;
  }
  function lookup(q) {
    var my = ++seq;
    return fetch('/devices/api/quick-search?q=' + encodeURIComponent(q), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : { results: [] }; })
      .then(function (d) { return my === seq ? (d.results || []) : null; })
      .catch(function () { return my === seq ? [] : null; });
  }

  input.addEventListener('input', function () {
    clearTimeout(timer);
    var q = input.value.trim();
    if (q.length < 2) { seq++; results = []; close(); return; }
    timer = setTimeout(function () {
      lookup(q).then(function (res) {
        if (res === null) return;       // stale
        results = res;
        render();
      });
    }, 200);
  });

  input.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (menu.hidden || !results.length) return;
      e.preventDefault();
      var n = results.length;
      highlight(e.key === 'ArrowDown' ? (active + 1) % n : (active - 1 + n) % n);
    } else if (e.key === 'Escape') {
      close();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      input.select();                    // next scan overwrites this one
      if (!menu.hidden && active >= 0 && results[active]) { open(results[active].barcode); return; }
      var q = input.value.trim();
      if (q.length < 2) return;
      clearTimeout(timer);
      lookup(q).then(function (res) {
        if (res === null) return;
        results = res;
        var exact = res.filter(function (r) { return r.exact; });
        if (exact.length) { open(exact[0].barcode); return; }
        if (res.length === 1) { open(res[0].barcode); return; }
        render();
      });
    }
  });

  input.addEventListener('focus', function () {
    input.select();
    if (results.length && input.value.trim().length >= 2) menu.hidden = false;
  });
  // A click that focuses the box keeps the focus selection (its mouseup would
  // otherwise clear it); later clicks place the caret normally.
  var clickFocus = false;
  input.addEventListener('mousedown', function () { clickFocus = document.activeElement !== input; });
  input.addEventListener('mouseup', function (e) { if (clickFocus) { e.preventDefault(); clickFocus = false; } });

  menu.addEventListener('mousedown', function (e) { e.preventDefault(); });   // keep focus in the box
  menu.addEventListener('click', function (e) {
    var row = e.target.closest('.tbs-row');
    if (row) open(results[+row.getAttribute('data-i')].barcode);
  });
  menu.addEventListener('mousemove', function (e) {
    var row = e.target.closest('.tbs-row');
    if (row) highlight(+row.getAttribute('data-i'));
  });
  document.addEventListener('mousedown', function (e) {
    if (!wrap.contains(e.target)) close();
  });

  // On the device page just opened from here: show its tag, ready for the next scan.
  var m = window.location.pathname.match(/^\/devices\/([^/]+)\/?$/);
  var last = stored();
  if (m && last) {
    var onPage = '';
    try { onPage = decodeURIComponent(m[1]); } catch (e) { onPage = m[1]; }
    if (onPage.toUpperCase() === last.toUpperCase()) {
      input.value = last;
      input.focus();
      input.select();
    }
  }
})();
