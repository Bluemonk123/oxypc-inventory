/* Global Filter fields (2026-11-08, Pankaj).
 *
 * 1. Date range: every From / To pair of date boxes in a filter bar
 *    (.filter-bar-row) or a Global Card header becomes ONE box. Pick one day, or a start and an end
 *    day. One day sets From and To to the same date. A date box on its own
    gets the same look in single-day mode. The two original inputs
 *    stay in the form as hidden fields with the same names / ids, so the
 *    server and any page script that reads them (e.g. GRN's fFrom / fTo) need
 *    no change. Uses the vendored flatpickr (static/vendor/flatpickr).
 *
 * 2. Search in dropdowns (both kinds) when the list has 4 or more options,
 *    in filter bars and in Global Card headers:
 *    - Multi-select (.ms-filter): a search box at the top of the menu.
 *    - Single-select (<select> in a filter bar): drawn as the same kind of
 *      menu with a search box; the real <select> stays in the form (hidden),
 *      gets the chosen value and a "change" event, so the server and page
 *      scripts see no difference.
 *    Typing filters the list and highlights the first match. When exactly one
 *    option matches it is selected automatically (single: chosen; multi:
 *    ticked — and unticked again if the typing moves on to another single
 *    match). Enter picks the highlighted option, Up / Down move, Esc closes.
 *    With a search typed, "All" ticks only the options that are showing.
 */
(function () {
  'use strict';

  function zoomFactor() {
    var v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--app-zoom'));
    return v > 0 ? v : 1;
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function iso(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function parseIso(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || '');
    return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
  }
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function fmt(d, withYear) { return pad(d.getDate()) + ' ' + MON[d.getMonth()] + (withYear ? ' ' + d.getFullYear() : ''); }
  // One day: "04 Nov 2026". A range in the current year drops the year
  // ("01 – 08 Nov", "28 Oct – 08 Nov"); any other range uses a 2-digit year
  // (across years: "28.12.25 – 08.01.26"). Short enough for the narrowest filter field;
  // the full dates are in the box's tooltip.
  function yy(d) { return String(d.getFullYear()).slice(-2); }
  function num(d) { return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + yy(d); }
  function label(a, b) {
    if (!a) return '';
    if (!b || iso(a) === iso(b)) return fmt(a, true);
    var thisYear = new Date().getFullYear();
    var tail = (a.getFullYear() === thisYear && b.getFullYear() === thisYear) ? '' : ' ' + yy(b);
    if (a.getFullYear() !== b.getFullYear()) return num(a) + ' – ' + num(b);
    if (a.getMonth() === b.getMonth()) return pad(a.getDate()) + ' – ' + fmt(b, false) + tail;
    return fmt(a, false) + ' – ' + fmt(b, false) + tail;
  }
  function fullLabel(a, b) {
    if (!a) return '';
    return (!b || iso(a) === iso(b)) ? fmt(a, true) : fmt(a, true) + ' to ' + fmt(b, true);
  }

  // A filter-bar cell that holds exactly one date input and nothing else to fill in.
  function dateCell(el) {
    if (!el || el.nodeType !== 1) return null;
    var inputs = el.querySelectorAll('input:not([type=hidden]), select, textarea, button');
    if (inputs.length !== 1) return null;
    var i = inputs[0];
    return (i.tagName === 'INPUT' && i.type === 'date') ? i : null;
  }

  function enhancePair(cellA, from, cellB, to) {
    var title = (from.getAttribute('title') || '').replace(/\s*(—\s*)?From\b.*$/i, '').trim();
    from.type = 'hidden';
    to.type = 'hidden';
    cellA.appendChild(to);
    if (cellB) { cellB.parentNode.removeChild(cellB); cellA.classList.add('filter-bar-daterange'); }

    var wrap = document.createElement('div');
    wrap.className = 'dr-wrap';
    var box = document.createElement('input');
    box.type = 'text';
    box.readOnly = true;
    box.className = 'form-control form-control-sm dr-input';
    // A page can name the range (data-dr-placeholder on the From box, 2026-11-10)
    box.placeholder = from.getAttribute('data-dr-placeholder') || 'Date range';
    var baseTitle = (title || 'Date') + ': pick one day, or a start and an end day';
    box.title = baseTitle;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'dr-icon';
    btn.tabIndex = -1;
    wrap.appendChild(box);
    wrap.appendChild(btn);
    cellA.insertBefore(wrap, cellA.firstChild);

    function fire(el) {
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
    function show() {
      var a = parseIso(from.value), b = parseIso(to.value);
      if (!a && b) a = b;
      box.value = label(a, b);
      box.title = baseTitle + (box.value ? ' — ' + fullLabel(a, b) : '');
      btn.innerHTML = box.value ? '<i class="bi bi-x-lg"></i>' : '<i class="bi bi-calendar3"></i>';
      btn.title = box.value ? 'Clear dates' : '';
    }
    function write(dates) {
      var f = '', t2 = '';
      if (dates.length) {
        var a = dates[0], b = dates[1] || dates[0];
        if (b < a) { var t = a; a = b; b = t; }
        f = iso(a); t2 = iso(b);
      }
      // Card-header filters reload on every change, so fire only on a real change.
      if (f === from.value && t2 === to.value) { show(); return; }
      from.value = f; to.value = t2;
      fire(from); fire(to);
      show();
    }

    var initial = [parseIso(from.value), parseIso(to.value)].filter(Boolean);
    var fp = window.flatpickr(box, {
      mode: 'range',
      defaultDate: initial,
      clickOpens: true,
      disableMobile: true,
      locale: { firstDayOfWeek: 1, rangeSeparator: ' – ' },
      // Same double-zoom fix as multiselect-filter.js: the calendar lives in
      // the zoomed <body>, so place it with position:fixed off the box's rect
      // divided by --app-zoom.
      position: function (self) {
        var r = box.getBoundingClientRect(), z = zoomFactor(), cal = self.calendarContainer;
        cal.style.position = 'fixed';
        cal.style.top = ((r.bottom + 2) / z) + 'px';
        var left = r.left / z, w = cal.offsetWidth || 308;
        var maxLeft = (window.innerWidth / z) - w - 8;
        cal.style.left = Math.max(4, Math.min(left, maxLeft)) + 'px';
        cal.style.right = 'auto';
      },
      onChange: function (dates) { if (dates.length === 2) write(dates); },
      onClose: function (dates) { write(dates); },
    });
    // flatpickr writes its own text into the box; keep ours.
    fp.config.onValueUpdate.push(show);
    btn.addEventListener('click', function () {
      if (box.value) { fp.clear(); write([]); } else { fp.open(); }
    });
    // Pages that clear the hidden inputs from script (GRN's Reset) — re-read after any click in the bar.
    var bar = cellA.closest('.filter-bar') || cellA.parentNode;
    bar.addEventListener('click', function (e) {
      if (wrap.contains(e.target)) return;
      setTimeout(function () {
        var want = [parseIso(from.value), parseIso(to.value)].filter(Boolean);
        if (label(want[0], want[1]) !== box.value) { fp.setDate(want, false); show(); }
      }, 0);
    });
    // Page scripts that set the dates directly (a Reset / clear button) fire no
    // event; follow those too.
    [from, to].forEach(function (el) {
      var d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
      Object.defineProperty(el, 'value', {
        configurable: true,
        get: function () { return d.get.call(this); },
        set: function (v) {
          d.set.call(this, v);
          var want = [parseIso(d.get.call(from)), parseIso(d.get.call(to))].filter(Boolean);
          if (label(want[0], want[1]) !== box.value) { fp.setDate(want, false); show(); }
        },
      });
    });
    show();
  }

  // Card header: a From and a To date box side by side (labels such as "From",
  // "to" between them allowed). Their own labels go; the field name stays.
  function headerPairs(header) {
    var dates = Array.prototype.slice.call(header.querySelectorAll('input[type=date]'));
    for (var i = 0; i < dates.length - 1; i++) {
      var a = dates[i], b = dates[i + 1];
      if (a.parentNode !== b.parentNode) continue;
      var between = [], n = a.nextSibling, ok = true;
      while (n && n !== b) {
        if (n.nodeType === 1 && (n.matches('input,select,textarea,button') || n.querySelector('input,select,textarea,button'))) { ok = false; break; }
        between.push(n); n = n.nextSibling;
      }
      if (!ok || n !== b) continue;
      var cell = document.createElement('div');
      cell.className = 'dr-cell';
      var w = parseFloat(a.style.width || a.style.maxWidth) || 0;
      cell.style.width = Math.max(175, w) + 'px';
      var prev = a.previousElementSibling;
      if (prev && /^\s*from\s*$/i.test(prev.textContent) && !prev.querySelector('input,select')) prev.parentNode.removeChild(prev);
      a.parentNode.insertBefore(cell, a);
      between.forEach(function (x) { x.parentNode.removeChild(x); });
      cell.appendChild(a);
      enhancePair(cell, a, null, b);
      i++;
    }
  }

  // A single date box (not part of a From / To pair) gets the same look: one
  // box, "Select date" placeholder, calendar icon / × to clear, "05 Nov 2026".
  function enhanceSingle(inp) {
    var w = parseFloat(inp.style.width || inp.style.maxWidth) || 0;
    inp.type = 'hidden';
    var wrap = document.createElement('div');
    wrap.className = 'dr-wrap dr-single';
    if (!inp.closest('.filter-bar-row')) { wrap.style.display = 'inline-block'; wrap.style.verticalAlign = 'middle'; wrap.style.width = Math.max(150, w) + 'px'; }
    var box = document.createElement('input');
    box.type = 'text'; box.readOnly = true;
    box.className = 'form-control form-control-sm dr-input';
    box.placeholder = 'Select date';
    box.title = inp.getAttribute('title') || 'Date';
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'dr-icon'; btn.tabIndex = -1;
    inp.parentNode.insertBefore(wrap, inp);
    wrap.appendChild(box); wrap.appendChild(btn); wrap.appendChild(inp);
    var d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
    function show() {
      var a = parseIso(d.get.call(inp));
      box.value = a ? fmt(a, true) : '';
      btn.innerHTML = box.value ? '<i class="bi bi-x-lg"></i>' : '<i class="bi bi-calendar3"></i>';
      btn.title = box.value ? 'Clear date' : '';
    }
    function write(v) {
      if (v === d.get.call(inp)) { show(); return; }
      d.set.call(inp, v);
      inp.dispatchEvent(new Event('input', { bubbles: true }));
      inp.dispatchEvent(new Event('change', { bubbles: true }));
      show();
    }
    var a0 = parseIso(d.get.call(inp));
    var fp = window.flatpickr(box, {
      defaultDate: a0 ? [a0] : [],
      disableMobile: true,
      locale: { firstDayOfWeek: 1 },
      position: function (self) {
        var r = box.getBoundingClientRect(), z = zoomFactor(), cal = self.calendarContainer;
        cal.style.position = 'fixed';
        cal.style.top = ((r.bottom + 2) / z) + 'px';
        var left = r.left / z, cw = cal.offsetWidth || 308;
        cal.style.left = Math.max(4, Math.min(left, (window.innerWidth / z) - cw - 8)) + 'px';
        cal.style.right = 'auto';
      },
      onChange: function (dates) { write(dates.length ? iso(dates[0]) : ''); },
    });
    fp.config.onValueUpdate.push(show);
    btn.addEventListener('click', function () { if (box.value) { fp.clear(); write(''); } else fp.open(); });
    Object.defineProperty(inp, 'value', {
      configurable: true,
      get: function () { return d.get.call(this); },
      set: function (v) { d.set.call(this, v); var a = parseIso(v); fp.setDate(a ? [a] : [], false); show(); },
    });
    show();
  }

  function initDateRanges() {
    if (!window.flatpickr) return;
    document.querySelectorAll('.filter-bar-row').forEach(function (row) {
      var kids = Array.prototype.slice.call(row.children);
      for (var i = 0; i < kids.length - 1; i++) {
        var a = dateCell(kids[i]), b = dateCell(kids[i + 1]);
        if (a && b) { enhancePair(kids[i], a, kids[i + 1], b); i++; }
      }
    });
    document.querySelectorAll('.card-header').forEach(headerPairs);
    document.querySelectorAll('.filter-bar-row input[type=date], .card-header input[type=date]').forEach(function (inp) {
      if (!inp.closest('.modal') && !inp.disabled) enhanceSingle(inp);
    });
  }

  // ── Search inside dropdowns ──────────────────────────────────────────────
  var SEARCH_MIN = 4;

  function makeSearch() {
    var s = document.createElement('input');
    s.type = 'search';
    s.className = 'form-control form-control-sm ms-search mb-1';
    s.placeholder = 'Search…';
    s.autocomplete = 'off';
    return s;
  }
  function matches(el, q) { return !q || el.textContent.toLowerCase().indexOf(q) !== -1; }
  // Highlight helper shared by both kinds: rows = visible candidates.
  function setHi(rows, idx) {
    rows.forEach(function (r, i) { r.classList.toggle('dd-hi', i === idx); });
    if (rows[idx]) rows[idx].scrollIntoView({ block: 'nearest' });
  }

  // Multi-select
  function msRows(menu) { return menu.querySelectorAll('.form-check'); }
  function initMsSearch() {
    document.querySelectorAll('.ms-filter .dropdown-menu').forEach(function (menu) {
      if (menu.querySelector('.ms-search') || menu.querySelectorAll('.ms-opt').length < SEARCH_MIN) return;
      var s = makeSearch();
      menu.insertBefore(s, menu.firstChild);
      var hi = 0, auto = null;   // auto = checkbox we ticked because it was the only match
      function visible() { return Array.prototype.filter.call(msRows(menu), function (r) { return r.style.display !== 'none'; }); }
      function tick(box, on) {
        if (box.checked === on) return;
        box.checked = on;
        box.dispatchEvent(new Event('change', { bubbles: true }));
      }
      function filter() {
        var q = s.value.trim().toLowerCase();
        msRows(menu).forEach(function (row) { row.style.display = matches(row, q) ? '' : 'none'; });
        var rows = visible();
        hi = 0; setHi(q ? rows : [], 0);
        var only = (q && rows.length === 1) ? rows[0].querySelector('.ms-opt') : null;
        if (auto && auto !== only) { tick(auto, false); auto = null; }
        if (only && !only.checked) { tick(only, true); auto = only; }
      }
      var toggle = menu.parentNode.querySelector('[data-bs-toggle="dropdown"]');
      if (toggle) {
        toggle.addEventListener('shown.bs.dropdown', function () { s.focus(); });
        toggle.addEventListener('hidden.bs.dropdown', function () { auto = null; s.value = ''; filter(); });
      }
      s.addEventListener('input', filter);
      // A tick made by hand keeps its state even if the typing moves on.
      menu.addEventListener('click', function (e) { if (e.target.classList && e.target.classList.contains('ms-opt') && e.target === auto) auto = null; });
      s.addEventListener('keydown', function (e) {
        var rows = visible();
        if (e.key === 'Enter') {
          e.preventDefault();          // never submit the filter form from here
          var r = s.value.trim() && rows[hi];
          if (r) { var b = r.querySelector('.ms-opt'); if (b === auto) auto = null; else tick(b, !b.checked); s.select(); }
        } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          hi = Math.max(0, Math.min(rows.length - 1, hi + (e.key === 'ArrowDown' ? 1 : -1)));
          setHi(rows, hi);
        }
      });
    });
  }
  // Capture phase, so it runs before each page's own "All" handler: with a
  // search typed, tick only the showing options and stop the page handler
  // from ticking the hidden ones too.
  document.addEventListener('click', function (e) {
    var all = e.target.closest && e.target.closest('.ms-all');
    if (!all) return;
    var menu = all.closest('.dropdown-menu');
    var s = menu && menu.querySelector('.ms-search');
    if (!s || !s.value.trim()) return;
    e.stopImmediatePropagation();
    var last = null;
    msRows(menu).forEach(function (row) {
      var b = row.querySelector('.ms-opt');
      if (b && row.style.display !== 'none') { b.checked = true; last = b; }
    });
    if (last) last.dispatchEvent(new Event('change', { bubbles: true }));
  }, true);

  // Single-select
  var openSS = null;
  function enhanceSelect(sel) {
    var inHeader = !sel.closest('.filter-bar-row');
    var wrap = document.createElement('div');
    wrap.className = 'ss-filter' + (inHeader ? ' ss-inline' : '');
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'form-select form-select-sm ss-btn';
    btn.title = sel.title || '';
    if (inHeader) {
      // Card header: keep the select's own classes, inline style and width,
      // so the header lays out exactly as before.
      btn.className = sel.className.replace(/\b(d-none|ss-native)\b/g, '') + ' ss-btn';
      if (sel.style.cssText) btn.style.cssText = sel.style.cssText;
      var w = sel.offsetWidth;
      if (w) btn.style.width = w + 'px'; else btn.style.minWidth = '150px';
      if (!/form-select/.test(btn.className)) btn.className += ' form-select form-select-sm';
    }
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(btn);
    wrap.appendChild(sel);
    sel.classList.add('ss-native');
    sel.tabIndex = -1;

    var menu = document.createElement('div');
    menu.className = 'dropdown-menu p-2 ss-menu';
    var s = makeSearch();
    var list = document.createElement('div');
    list.className = 'ss-list';
    menu.appendChild(s);
    menu.appendChild(list);
    var hi = 0;

    function label() {
      var o = sel.options[sel.selectedIndex];
      btn.textContent = o ? o.text : '';
    }
    function build() {
      list.innerHTML = '';
      Array.prototype.forEach.call(sel.options, function (o, i) {
        if (o.disabled && o.value === '') return;
        var it = document.createElement('div');
        it.className = 'ss-item' + (i === sel.selectedIndex ? ' ss-sel' : '');
        it.textContent = o.text;
        it.setAttribute('data-i', i);
        list.appendChild(it);
      });
      label();
    }
    function items() { return Array.prototype.filter.call(list.children, function (r) { return r.style.display !== 'none'; }); }
    function choose(i, close) {
      if (sel.selectedIndex !== i) {
        sel.selectedIndex = i;
        sel.dispatchEvent(new Event('input', { bubbles: true }));
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }
      Array.prototype.forEach.call(list.children, function (r) { r.classList.toggle('ss-sel', +r.getAttribute('data-i') === i); });
      label();
      if (close) hide(true);
    }
    function filter() {
      var q = s.value.trim().toLowerCase();
      Array.prototype.forEach.call(list.children, function (r) { r.style.display = matches(r, q) ? '' : 'none'; });
      var rows = items();
      hi = 0; setHi(rows, q ? 0 : -1);
      if (q && rows.length === 1) choose(+rows[0].getAttribute('data-i'), false);
    }
    function place() {
      var r = btn.getBoundingClientRect(), z = zoomFactor();
      menu.style.position = 'fixed';
      menu.style.margin = '0';
      menu.style.left = (r.left / z) + 'px';
      menu.style.top = ((r.bottom + 2) / z) + 'px';
      menu.style.minWidth = (r.width / z) + 'px';
    }
    function show() {
      if (openSS && openSS !== hide) openSS(false);
      build();
      document.body.appendChild(menu);
      place();
      menu.classList.add('show');
      btn.classList.add('show');
      s.value = ''; filter();
      var cur = list.querySelector('.ss-sel');
      if (cur) cur.scrollIntoView({ block: 'nearest' });
      s.focus();
      openSS = hide;
    }
    function hide(focusBtn) {
      menu.classList.remove('show');
      btn.classList.remove('show');
      if (menu.parentNode) menu.parentNode.removeChild(menu);
      if (openSS === hide) openSS = null;
      if (focusBtn) btn.focus();
    }
    btn.addEventListener('click', function () { if (menu.classList.contains('show')) hide(false); else show(); });
    btn.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ' || (e.key.length === 1 && !e.ctrlKey && !e.metaKey)) {
        e.preventDefault();
        show();
        if (e.key.length === 1 && e.key !== ' ') { s.value = e.key; filter(); }
      }
    });
    s.addEventListener('input', filter);
    s.addEventListener('keydown', function (e) {
      var rows = items();
      if (e.key === 'Enter') {
        e.preventDefault();
        var r = rows[hi] && s.value.trim() ? rows[hi] : list.querySelector('.ss-sel');
        if (r) choose(+r.getAttribute('data-i'), true); else hide(true);
      } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        hi = Math.max(0, Math.min(rows.length - 1, hi + (e.key === 'ArrowDown' ? 1 : -1)));
        setHi(rows, hi);
      } else if (e.key === 'Escape') { e.preventDefault(); hide(true); }
      else if (e.key === 'Tab') hide(false);
    });
    list.addEventListener('mousedown', function (e) { e.preventDefault(); });   // keep focus in the search
    list.addEventListener('click', function (e) {
      var it = e.target.closest('.ss-item');
      if (it) choose(+it.getAttribute('data-i'), true);
    });
    document.addEventListener('mousedown', function (e) {
      if (menu.classList.contains('show') && !menu.contains(e.target) && !wrap.contains(e.target)) hide(false);
    });
    window.addEventListener('resize', function () { if (menu.classList.contains('show')) place(); });
    window.addEventListener('scroll', function () { if (menu.classList.contains('show')) place(); }, true);
    // Page scripts that set the value (Reset) or rebuild the options.
    sel.addEventListener('change', label);
    // Page scripts that set .value / .selectedIndex directly (a Reset button,
    // a restored filter) fire no event; catch those too so the label follows.
    ['value', 'selectedIndex'].forEach(function (prop) {
      var d = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, prop);
      if (!d || !d.set) return;
      Object.defineProperty(sel, prop, {
        configurable: true,
        get: function () { return d.get.call(this); },
        set: function (v) { d.set.call(this, v); label(); },
      });
    });
    new MutationObserver(function () { label(); if (menu.classList.contains('show')) build(); })
      .observe(sel, { childList: true, subtree: true, attributes: true });
    var bar = wrap.closest('.filter-bar') || wrap.parentNode;
    bar.addEventListener('click', function () { setTimeout(label, 0); });
    label();
  }
  function initSsSearch() {
    // Filter bars and Global Card headers (header filters still apply instantly:
    // the page's own change handler runs on the hidden select as before).
    document.querySelectorAll('.filter-bar-row select, .card-header select').forEach(function (sel) {
      if (sel.multiple || sel.disabled || sel.closest('.ss-filter') || sel.classList.contains('d-none')) return;
      if (sel.closest('.dataTables_length') || sel.hasAttribute('data-no-search') || sel.closest('.modal')) return;
      // Counted as the list shows it, "All …" included.
      if (sel.options.length >= SEARCH_MIN) { enhanceSelect(sel); return; }
      // Lists a page fills in later (e.g. Credit Note's lots): enhance once they reach 4.
      var mo = new MutationObserver(function () {
        if (sel.options.length >= SEARCH_MIN && !sel.closest('.ss-filter')) { mo.disconnect(); enhanceSelect(sel); }
      });
      mo.observe(sel, { childList: true });
    });
  }

  // ── Global Card header: filters apply automatically + button colours ────
  // (2026-11-08, Pankaj)
  //  - Filters in a card header apply on change; a "Filter" / "Apply" /
  //    "Search" submit button there is hidden (a text box applies on Enter or
  //    when you leave it).
  //  - A header with filters always has ONE Reset: a grey icon-only button
  //    right after the last filter. An existing Reset / Clear keeps its own
  //    action and is restyled; otherwise one is added.
  //  - Action buttons: see "Colours" below (Add → primary, far right).
  //    Opt out: data-hdr-keep on the card-header (Dashboard's Financial Summary).
  var FIELD_SEL = '.ss-filter, .ms-filter, .dr-wrap, select:not(.ss-native), input.form-control:not([type=hidden]):not([type=file]):not(.dr-input):not(.ms-search)';
  var RESET_RE = /^\s*(reset|clear|reset filters?|clear filters?|reset dates?)\s*$/i;
  var SUBMIT_RE = /^\s*(filter|apply|search|go)\s*$/i;
  function plainHeader(h) {
    var cls = h.className;
    return !/\bbg-(?!transparent|white|light)\w+/.test(cls) && !h.hasAttribute('data-hdr-keep') && !h.closest('.modal');
  }
  function isReset(b) {
    var t = (b.textContent || '').trim();
    if (RESET_RE.test(t)) return true;
    var id = (b.id || '') + ' ' + (b.getAttribute('title') || '');
    return !t && (/reset|clear/i.test(id) || !!b.querySelector('.bi-arrow-counterclockwise, .bi-x-circle'));
  }
  function headerFields(h) {
    return Array.prototype.filter.call(h.querySelectorAll(FIELD_SEL), function (el) {
      // A box parked hidden in the header (d-none / hidden) is not a filter -
      // e.g. Inventory Manager's scan box waiting to move into the table
      // toolbar; counting it added a Reset, twice after a re-tidy (2026-11-09).
      // A POST form in a header is an action (e.g. Quote's Update Status,
      // 2026-11-10), not a filter: no Reset for it.
      return !el.closest('.dropdown-menu') && !(el.matches('select') && el.closest('.ss-filter')) && !el.closest('.dataTables_length') &&
             !el.closest('.d-none, [hidden]') && !el.closest('form[method="post" i]');
    });
  }
  function resetFields(fields) {
    fields.forEach(function (f) {
      if (f.classList.contains('dr-wrap')) {
        f.querySelectorAll('input[type=hidden]').forEach(function (i) { if (i.value) { i.value = ''; i.dispatchEvent(new Event('change', { bubbles: true })); } });
      } else if (f.classList.contains('ss-filter')) {
        var sel = f.querySelector('select');
        if (sel.selectedIndex !== 0) { sel.selectedIndex = 0; sel.dispatchEvent(new Event('change', { bubbles: true })); }
      } else if (f.classList.contains('ms-filter')) {
        var none = f.querySelector('.ms-none') || (f.parentNode && f.parentNode.querySelector('.ms-none'));
        if (none) none.click();
      } else if (f.tagName === 'SELECT') {
        if (f.selectedIndex !== 0) { f.selectedIndex = 0; f.dispatchEvent(new Event('change', { bubbles: true })); }
      } else if (f.value) {
        f.value = '';
        f.dispatchEvent(new Event('input', { bubbles: true }));
        f.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
        f.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
  }
  function makeIconReset(b) {
    b.innerHTML = '<i class="bi bi-arrow-counterclockwise"></i>';
    b.className = b.className.replace(/\bbtn-(outline-)?(primary|secondary|success|danger|warning|info|light|dark|link)\b/g, '').replace(/\btext-\w+\b/g, '') + ' btn-outline-secondary hdr-reset';
    b.title = 'Reset filters';
    b.setAttribute('aria-label', 'Reset filters');
  }
  function autoApply(form) {
    if (form.__autoApply) return;
    form.__autoApply = true;
    var submit = function () { if (form.requestSubmit) form.requestSubmit(); else form.submit(); };
    form.addEventListener('change', function (e) {
      var t = e.target;
      if (t.hasAttribute && t.hasAttribute('onchange')) return;       // already submits itself
      if (t.closest && t.closest('.ms-filter, .dropdown-menu')) return; // multi-select: on close
      // flatpickr fires change on its visible box at every click; the date
      // filter applies when the hidden from/to inputs change (2026-11-09).
      if (t.classList && t.classList.contains('dr-input')) return;
      submit();
    });
    form.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && e.target.matches('input.form-control:not(.dr-input):not(.ms-search)')) { e.preventDefault(); submit(); }
    });
    form.querySelectorAll('.ms-filter [data-bs-toggle="dropdown"]').forEach(function (tg) {
      var hid = form.querySelector('#ms_val_' + (tg.closest('.ms-filter').getAttribute('data-ms') || ''));
      var before = null;
      tg.addEventListener('show.bs.dropdown', function () { before = hid ? hid.value : null; });
      tg.addEventListener('hidden.bs.dropdown', function () { if (hid && hid.value !== before) submit(); });
    });
  }
  function tidyHeader(h) {
    if (h.__tidy || !plainHeader(h)) return;
    h.__tidy = true;
    var fields = headerFields(h);
    // Resets this script added on an earlier pass: never a second one, and
    // none once the header has no filter fields left (2026-11-09).
    var autoResets = Array.prototype.slice.call(h.querySelectorAll('[data-hdr-auto-reset]'));
    if (!fields.length) autoResets.forEach(function (b) { b.remove(); });
    var btns = Array.prototype.filter.call(h.querySelectorAll('.btn'), function (b) {
      // On/off toggle buttons (Split Entity / Split Zone…) keep their own look.
      return b.offsetParent !== null && !b.matches('.ss-btn, .btn-close, label.btn, .btn-check + .btn, [data-bs-toggle="collapse"], [data-bs-toggle="button"], [aria-pressed], [id*="Toggle"], [id*="toggle"]') &&
             !b.closest('.ms-filter, .dropdown-menu, .btn-group-toggle');
    });
    if (fields.length) {
      // Filter / Apply buttons go: header filters apply on change.
      btns = btns.filter(function (b) {
        var form = b.closest('form');
        if (form && (form.method || 'get').toLowerCase() === 'get' && SUBMIT_RE.test(b.textContent || '') &&
            (b.type === 'submit' || b.tagName === 'BUTTON' && !b.getAttribute('type'))) {
          b.style.display = 'none'; b.classList.add('hdr-hidden-submit'); autoApply(form); return false;
        }
        return true;
      });
      // Every GET form holding header filters applies on change, with or
      // without a Filter button to hide.
      fields.forEach(function (f) {
        var fm = f.closest('form');
        // Only a real server filter form: an explicit method="get", named fields, no JS submit handler.
        if (fm && (fm.getAttribute('method') || '').toLowerCase() === 'get' && h.contains(fm) &&
            !fm.hasAttribute('onsubmit') &&
            // A Date range box keeps its dates in hidden inputs, so a form holding
            // only a date range counts too (Lots Estimation, 2026-11-09).
            fm.querySelector('[name]:not([type=hidden]), .dr-cell > input[type=hidden][name], .dr-wrap input[type=hidden][name]')) autoApply(fm);
      });
      var resets = btns.filter(isReset);
      btns = btns.filter(function (b) { return resets.indexOf(b) === -1; });
      var reset = resets[0];
      if (!reset && autoResets.length) reset = autoResets[0];
      autoResets.forEach(function (b) { if (b !== reset) b.remove(); });
      resets.slice(1).forEach(function (b) { b.style.display = 'none'; });
      if (!reset) {
        reset = document.createElement('button');
        reset.type = 'button';
        reset.className = 'btn btn-sm';
        reset.setAttribute('data-hdr-auto-reset', '');
        var form = fields[0].closest('form');
        reset.addEventListener('click', function () {
          if (form && (form.method || 'get').toLowerCase() === 'get' && !form.querySelector('[onsubmit]')) {
            var url = new URL(form.getAttribute('action') || location.pathname, location.href);
            var keep = new URLSearchParams(location.search);
            Array.prototype.forEach.call(form.elements, function (el) { if (el.name) keep.delete(el.name); });
            url.search = keep.toString();
            location.href = url.toString();
          } else {
            resetFields(fields);
          }
        });
      }
      makeIconReset(reset);
      var last = fields[fields.length - 1];
      var anchor = last.closest('.filter-bar-check, .form-check, .dr-cell') || last;
      // Step out of wrappers that hold only this one field, so the Reset sits
      // beside it in the header row, not underneath inside the wrapper.
      var solo = function (el) {
        return Array.prototype.filter.call(el.children, function (c) { return !(c.tagName === 'INPUT' && c.type === 'hidden'); }).length === 1;
      };
      while (anchor.parentNode && anchor.parentNode !== h && solo(anchor.parentNode)) anchor = anchor.parentNode;
      if (anchor.nextSibling !== reset) anchor.parentNode.insertBefore(reset, anchor.nextSibling);
    }
    applyButtonRules(btns);
  }
  // Button colours for a card header or the page-top toolbar (2026-11-08, Pankaj).
  function applyButtonRules(btns) {
    // Colours of the remaining action buttons (2026-11-08, Pankaj):
    //  - Only an Add button is primary (solid blue). With no Add button, a
    //    Backup or Upload button is. Otherwise no button is primary.
    //  - The primary button moves to the far right of the header.
    //  - Every other button is a blue outline on white; red (Delete…) stays
    //    red outline. Export buttons always use the download icon.
    var label = function (b) { return (b.textContent || '').replace(/\s+/g, ' ').trim(); };
    var pick = function (re) {
      var m = btns.filter(function (b) { return re.test(label(b)) && !/\bbtn-(outline-)?danger\b/.test(b.className); });
      return m[m.length - 1] || null;
    };
    // A page can name its main action instead with data-hdr-primary
    // (Inventory Manager's Bulk Move to L1/L2, 2026-11-09).
    var marked = btns.filter(function (b) { return b.hasAttribute('data-hdr-primary'); });
    var primary = marked[marked.length - 1] || pick(/^Add\b/i) || pick(/backup|upload/i);
    if (primary && btns.length) {
      var lastBtn = btns[btns.length - 1];
      if (lastBtn !== primary) lastBtn.parentNode.insertBefore(primary, lastBtn.nextSibling);
    }
    btns.forEach(function (b) {
      var danger = /\bbtn-(outline-)?danger\b/.test(b.className);
      var role = b === primary ? 'primary' : 'other';
      // Any colour class (Bootstrap's and the app's own, e.g. btn-outline-teal) goes.
      b.className = b.className.replace(/\bbtn-(outline-)?(?!sm\b|lg\b|xs\b|close\b|check\b)[a-z]+\b/g, '').replace(/\b(bg-white|text-white|text-dark)\b/g, '').trim();
      if (role === 'primary') b.classList.add('btn-primary');
      else { b.classList.add(danger ? 'btn-outline-danger' : 'btn-outline-primary'); b.classList.add('bg-white'); }
      b.classList.add('hdr-btn-' + role);
      if (/^Export\b/i.test(label(b))) {
        var ic = b.querySelector('i.bi');
        if (ic) ic.className = ic.className.replace(/\bbi-[\w-]+/g, '').trim() + ' bi-download';
        else b.insertAdjacentHTML('afterbegin', '<i class="bi bi-download me-1"></i>');
      }
    });
  }
  // Page-top buttons (2026-11-08, Pankaj): the buttons at the top of a page,
  // before its first card / filter bar / tiles / table / tabs and outside any
  // form with fields, follow the same rules as a card header.
  function tidyPageTop() {
    var main = document.querySelector('main');
    if (!main || main.__pageTop) return;
    var anchor = main.querySelector('.card, .filter-bar, .oxt-row, table, .nav-tabs');
    if (!anchor) return;
    main.__pageTop = true;
    var btns = Array.prototype.filter.call(main.querySelectorAll('.btn'), function (b) {
      if (b.offsetParent === null) return false;
      if (!(anchor.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_PRECEDING)) return false;
      if (anchor.contains(b)) return false;
      if (b.closest('.card, .modal, .dropdown-menu, .filter-bar, .nav, table, .alert, .toast, .oxt-row, .btn-group, [data-hdr-keep]')) return false;
      // Segmented pickers (period / status buttons where one is .active) keep their own look.
      if (b.parentNode && b.parentNode.querySelector(':scope > .btn.active')) return false;
      if (b.matches('.btn-close, label.btn, .btn-check + .btn, [data-bs-toggle="collapse"], [data-bs-toggle="tab"], [data-bs-toggle="button"], [aria-pressed], [id*="Toggle"], [id*="toggle"]')) return false;
      var f = b.closest('form');
      if (f && f.querySelector('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea')) return false;
      return true;
    });
    // Link groups that work as view pickers or page navigation (FY 2024-25 |
    // 2025-26…, 7d | 30d…, stage pages, status filters) keep their own look:
    // a group of links on one row that either stay on this page with other
    // parameters, or are 3+ links with no Add / Upload / Backup among them.
    var groups = new Map();
    btns.forEach(function (b) { var k = b.parentNode; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(b); });
    var skip = [];
    groups.forEach(function (g) {
      if (g.length < 2 || !g.every(function (b) { return b.tagName === 'A'; })) return;
      var samePage = g.some(function (b) {
        var h = b.getAttribute('href') || '';
        if (h.charAt(0) === '?') return true;
        try { return new URL(h, location.href).pathname === location.pathname; } catch (e) { return false; }
      });
      var hasMain = g.some(function (b) { return /^Add\b|backup|upload/i.test((b.textContent || '').trim()); });
      if (samePage || (g.length >= 3 && !hasMain)) skip = skip.concat(hasMain ? g.filter(function (b) { return !/^Add\b|backup|upload/i.test((b.textContent || '').trim()); }) : g);
    });
    btns = btns.filter(function (b) { return skip.indexOf(b) === -1; });
    if (btns.length) applyButtonRules(btns);
  }
  function initHeaders() { document.querySelectorAll('.card-header').forEach(tidyHeader); }

  // Re-apply the header rules after a page reveals or moves header buttons
  // itself (e.g. once its DataTable has loaded) — 2026-11-09.
  window.retidyCardHeader = function (h) {
    if (!h) return;
    h.__tidy = false;
    tidyHeader(h);
  };

  function init() {
    initDateRanges(); initMsSearch(); initSsSearch();
    initHeaders(); tidyPageTop();
    // Buttons some pages add after load (permissions, DataTables): one more pass.
    window.addEventListener('load', function () { setTimeout(function () { initHeaders(); tidyPageTop(); }, 300); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
