/* Global pill tabs count (2026-11-11, Pankaj): every .pill-tabs button reads
   "<tab name> - <count>", the count being the rows of the table(s) in the pane
   it opens. Global Tables (DataTables) count all their rows, not just the
   page shown; placeholder rows ("Nothing here") count as 0. Buttons that
   already carry a .badge, or whose pane has no table, are left alone. */
(function () {
  function rowsIn(pane) {
    var tables = pane.querySelectorAll('table'), n = 0, found = false;
    tables.forEach(function (t) {
      if (t.closest('.tab-pane') !== pane) return;        // a nested pane's table belongs to that pane
      found = true;
      if (window.jQuery && jQuery.fn.dataTable && jQuery.fn.dataTable.isDataTable(t)) {
        var api = jQuery(t).DataTable(), info = api.page.info();
        n += (info && info.serverSide) ? info.recordsTotal : api.rows().count();
      } else {
        t.querySelectorAll(':scope > tbody > tr').forEach(function (tr) {
          var c = tr.children;
          if (tr.querySelector('.dataTables_empty')) return;
          if (c.length === 1 && c[0].hasAttribute('colspan')) return;   // empty placeholder row
          n += 1;
        });
      }
    });
    return found ? n : null;
  }
  function label(btn) {
    if (btn.dataset.pcBase === undefined) btn.dataset.pcBase = btn.textContent.trim();
    return btn.dataset.pcBase;
  }
  function update() {
    document.querySelectorAll('.pill-tabs [data-bs-toggle="tab"][data-bs-target]').forEach(function (btn) {
      if (btn.querySelector('.badge')) return;
      var pane = document.querySelector(btn.getAttribute('data-bs-target'));
      if (!pane) return;
      var n = rowsIn(pane);
      if (n === null) return;
      var base = label(btn), icon = btn.querySelector('i.bi');
      btn.textContent = '';
      if (icon) btn.appendChild(icon);
      btn.appendChild(document.createTextNode((icon ? ' ' : '') + base + ' - ' + n));
    });
  }
  window.oxyPillCounts = update;
  document.addEventListener('DOMContentLoaded', function () {
    update();
    setTimeout(update, 400);                                  // after Global Tables have initialised
    if (window.jQuery) jQuery(document).on('init.dt xhr.dt', function () { setTimeout(update, 0); });
  });
})();
