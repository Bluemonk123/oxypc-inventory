/* Option C dashboards: "View Data" link (2026-11-14, Pankaj).
   Swaps the dashboard (.dash-body) for the page's own content (.dash-data)
   and back. The choice is remembered per page (this browser), and a link with
   ?view=data or ?view=dashboard opens that view. Tables sized while hidden
   are re-measured when they appear. */
(function () {
  'use strict';
  var KEY = 'oxyDashView:' + location.pathname;
  function read() {
    var q = new URLSearchParams(location.search).get('view');
    if (q === 'data' || q === 'dashboard') return q;
    try { return localStorage.getItem(KEY) || 'dashboard'; } catch (e) { return 'dashboard'; }
  }
  function apply(mode) {
    var html = document.documentElement;
    html.classList.toggle('dash-show-data', mode === 'data');
    document.querySelectorAll('[data-dash-toggle] span').forEach(function (s) {
      s.textContent = mode === 'data' ? 'View Dashboard' : 'View Data';
    });
    document.querySelectorAll('[data-dash-toggle] i').forEach(function (i) {
      i.className = (mode === 'data' ? 'bi bi-speedometer2' : 'bi bi-table') + ' me-1';
    });
    if (mode === 'data') {
      setTimeout(function () {
        try { if (window.jQuery && jQuery.fn.dataTable) jQuery.fn.dataTable.tables({ visible: true, api: true }).columns.adjust(); } catch (e) {}
        window.dispatchEvent(new Event('resize'));
      }, 30);
    }
  }
  // Chart hover tooltip: any [data-tip] inside a dashboard (2026-11-14).
  var tip = null;
  function tipEl() {
    if (!tip) { tip = document.createElement('div'); tip.id = 'dashTip'; document.body.appendChild(tip); }
    return tip;
  }
  document.addEventListener('mousemove', function (e) {
    var t = e.target.closest && e.target.closest('.dash [data-tip]');
    if (!t || !t.getAttribute('data-tip')) { if (tip) tip.classList.remove('show'); return; }
    var el = tipEl();
    el.textContent = t.getAttribute('data-tip');
    el.classList.add('show');
    var x = e.clientX + 14, y = e.clientY + 14, r = el.getBoundingClientRect();
    if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - 14;
    if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - 14;
    el.style.left = x + 'px'; el.style.top = y + 'px';
  });
  document.addEventListener('scroll', function () { if (tip) tip.classList.remove('show'); }, true);

  if (!document.querySelector('.dash-body')) return;
  // No Dashboard View permission (server marks <html class="dash-off">): always
  // the data view, never the empty dashboard (2026-11-16, Pankaj).
  if (document.documentElement.classList.contains('dash-off')) { apply('data'); return; }
  apply(read());
  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-dash-toggle]');
    if (!t) return;
    e.preventDefault();
    var next = document.documentElement.classList.contains('dash-show-data') ? 'dashboard' : 'data';
    try { localStorage.setItem(KEY, next); } catch (err) {}
    apply(next);
  });
})();
