/* Model Summary card (2026-11-17, Pankaj) — shared by All Inventories,
   Ready for Sale - Tags and Ready for Sale - Lots.
   1. Grade tabs (.ms-gtabs): a tab writes its grade into the hidden input
      named by data-hidden and fires "ms:change" on it — the page's existing
      refresh listener reloads the table.
   2. Card-header search (.ms-hdr-search[data-table]): types into the table's
      own DataTables search; the table's built-in box is hidden. Call
      msCardReapply(tableId) after a table is rebuilt to keep the term. */
(function () {
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('.ms-gtab');
    if (!b) return;
    var ul = b.closest('.ms-gtabs'), hid = ul && document.getElementById(ul.getAttribute('data-hidden'));
    if (!hid) return;
    ul.querySelectorAll('.ms-gtab').forEach(function (x) { x.classList.toggle('active', x === b); });
    hid.value = b.getAttribute('data-grade') || '';
    hid.dispatchEvent(new CustomEvent('ms:change', { bubbles: true, detail: { value: hid.value } }));
  });
  function dt(id) {
    return (window.jQuery && $.fn.dataTable && $.fn.dataTable.isDataTable('#' + id)) ? $('#' + id).DataTable() : null;
  }
  function hideBuiltIn(id) {
    var w = document.getElementById(id + '_wrapper');
    if (w) w.querySelectorAll('.dataTables_filter input[type="search"]').forEach(function (f) {
      var box = f.closest('label') || f.closest('.input-group') || f.parentNode;
      (box && box.classList && !box.classList.contains('dataTables_filter') ? box : f).style.display = 'none';
    });
  }
  var timer;
  document.addEventListener('input', function (e) {
    var i = e.target;
    if (!i.classList || !i.classList.contains('ms-hdr-search')) return;
    clearTimeout(timer);
    timer = setTimeout(function () { var t = dt(i.getAttribute('data-table')); if (t) t.search(i.value).draw(); }, 200);
  });
  window.msCardReapply = function (id) {
    hideBuiltIn(id);
    var i = document.querySelector('.ms-hdr-search[data-table="' + id + '"]'), t = dt(id);
    if (i && t && i.value && t.search() !== i.value) t.search(i.value).draw();
  };
  window.msCardSwapTabs = function (doc, tabsId) {   // after a partial refresh: new counts
    var a = doc.getElementById(tabsId), b = document.getElementById(tabsId);
    if (a && b) b.innerHTML = a.innerHTML;
  };
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.ms-hdr-search[data-table]').forEach(function (i) { hideBuiltIn(i.getAttribute('data-table')); });
  });
})();
