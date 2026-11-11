/* Quick Add — top-bar "+" menu (2026-11-08). Markup: base.html #quickAddWrap;
 * items: services/quick_add.py.
 *
 * Menu names / sections / order follow the sidebar (buildMenu below).
 * A link "…#add=<button text>" goes to a list page whose Add opens a
 *    modal: once the page has loaded, the visible Add button with exactly that
 *    text is clicked, and the hash is removed. Same page → works too
 *    (hashchange).
 */
(function () {
  /* Build the menu from the sidebar (2026-11-08): each item's label, icon and
     section come from its sidebar link (data-side), in sidebar order — so a
     renamed sidebar item is renamed here too. Then whole sections are laid
     out top-to-bottom, up to ROWS (16) rows per column, then the next column (at
     most MAX_COLS; more rows per column after that). The first column sits
     on the right, under the "+" button, and further columns open to the left
     (flex row-reverse in app.css). */
  var ROWS = 16, MAX_COLS = 4;   // 16 rows per column (2026-11-09, Pankaj)
  function buildMenu() {
    var wrap = document.getElementById('quickAddWrap');
    var box = wrap && wrap.querySelector('.qa-groups');
    if (!box) return;
    // Sidebar in page order: each link with the section header above it
    // (works for links inside collapsible sub-menus too).
    var sideLinks = [], sectionOf = new Map(), cur = '';
    document.querySelectorAll('#sidebar .nav-section, #sidebar a.nav-link[href]').forEach(function (el) {
      if (el.classList.contains('nav-section')) { cur = el.textContent.trim(); return; }
      sideLinks.push(el); sectionOf.set(el, cur);
    });
    var items = [];
    box.querySelectorAll('.qa-group').forEach(function (g, gi) {
      var fallbackGroup = g.querySelector('.qa-group-title').textContent.trim();
      g.querySelectorAll('.qa-item').forEach(function (a, ii) {
        var side = a.getAttribute('data-side');
        var link = side ? sideLinks.filter(function (l) { return l.getAttribute('href') === side; })[0] : null;
        var it = { el: a, group: fallbackGroup, order: 100000 + gi * 100 + ii };
        if (link) {
          if (sectionOf.get(link)) it.group = sectionOf.get(link);
          it.order = sideLinks.indexOf(link) * 10 + ii;
          if (!a.hasAttribute('data-keep')) {
            var c = link.cloneNode(true);
            c.querySelectorAll('.badge, .visually-hidden').forEach(function (b) { b.remove(); });
            var label = c.textContent.replace(/\s+/g, ' ').trim();
            if (label) a.querySelector('span').textContent = label;
            var li2 = link.querySelector('i.bi');
            if (li2) a.querySelector('i').className = li2.className.replace(/\bme-\d\b/g, '') + ' me-2';
          }
        }
        items.push(it);
      });
    });
    items.sort(function (x, y) { return x.order - y.order; });
    // Sections in sidebar order; each section = its header + its items.
    var bySection = {}, order = [];
    items.forEach(function (it) {
      if (!bySection[it.group]) { bySection[it.group] = []; order.push(it.group); }
      bySection[it.group].push(it);
    });
    // Whole sections go into a column until the next one would pass `per`
    // rows (header counts as a row) — then the whole section moves to the
    // next column. A section longer than `per` gets a column of its own.
    function layout(per) {
      var out = document.createElement('div');
      out.className = 'qa-cols';
      var col = null, n = 0;
      order.forEach(function (g) {
        var size = bySection[g].length + 1;
        if (!col || (n > 0 && n + size > per)) {
          col = document.createElement('div'); col.className = 'qa-col'; out.appendChild(col); n = 0;
        }
        var sec = document.createElement('div'); sec.className = 'qa-section';
        var h = document.createElement('div'); h.className = 'qa-group-title'; h.textContent = g; sec.appendChild(h);
        bySection[g].forEach(function (it) { sec.appendChild(it.el); });
        col.appendChild(sec);
        n += size;
      });
      return out;
    }
    var total = items.length + order.length;
    var per = Math.max(ROWS, Math.ceil(total / MAX_COLS)), out = layout(per);
    while (out.children.length > MAX_COLS) out = layout(++per);
    box.replaceWith(out);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', buildMenu);
  else buildMenu();

  function addFromHash() {
    var m = (window.location.hash || '').match(/^#add=(.+)$/);
    if (!m) return;
    var want = decodeURIComponent(m[1]).trim().toLowerCase();
    var tries = 0;
    (function look() {
      var btn = Array.prototype.find.call(
        document.querySelectorAll('button, a.btn'),
        function (b) {
          return b.offsetParent !== null && !b.disabled && !b.closest('#quickAddWrap, .modal') &&
                 b.textContent.replace(/\s+/g, ' ').trim().toLowerCase() === want;
        });
      if (btn) {
        history.replaceState(null, '', window.location.pathname + window.location.search);
        btn.click();
      } else if (++tries < 20) {
        setTimeout(look, 150);       // page scripts may still be building it
      }
    })();
  }
  window.addEventListener('hashchange', addFromHash);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { setTimeout(addFromHash, 200); });
  else setTimeout(addFromHash, 200);
})();
