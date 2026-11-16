/* Global Secondary Table (2026-11-14, Pankaj): the light table used inside
   modals and small cards — e.g. Sales Coordinator's invoice popup.

   Markup:  <div class="gtable2-wrap"><table class="gtable2"> … </table></div>
   • Rounded corners, light header, first column frozen (sticky) when the
     table scrolls sideways, header frozen when it scrolls down (app.css).
   • Sorting: click a header (again to reverse). Numbers, dd-mm-yyyy /
     "14 Nov 2026" dates and text each sort naturally. <th data-nosort> opts
     a column out. Rows with .gt2-empty (placeholder) never move.
   • Optional total row: put data-total="sum" (or "count") on the <th> of
     the columns to total; a <tfoot> row "Total" is added and kept current.
     data-total-label on the <table> changes the word.
   Tables filled by script call gt2Refresh(table) after filling, to redo the
   total and the sort arrows. Works without any per-page setup. */
(function () {
  function num(t) {
    var s = String(t == null ? '' : t).replace(/[₹,\s]/g, '');
    return /^-?\d+(\.\d+)?$/.test(s) ? parseFloat(s) : null;
  }
  var MON = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };
  function date(t) {
    var s = String(t || '').trim(), m;
    if ((m = s.match(/^(\d{1,2})[-/](\d{1,2})[-/](\d{4})/))) return new Date(+m[3], +m[2] - 1, +m[1]).getTime();
    if ((m = s.match(/^(\d{4})-(\d{2})-(\d{2})/))) return new Date(+m[1], +m[2] - 1, +m[3]).getTime();
    if ((m = s.match(/^(\d{1,2})[ -]([A-Za-z]{3})[a-z]*[ ,-]+(\d{4})/)) && MON[m[2].toLowerCase()] !== undefined)
      return new Date(+m[3], MON[m[2].toLowerCase()], +m[1]).getTime();
    return null;
  }
  function cellText(td) {
    if (!td) return '';
    var sel = td.querySelector('select');
    if (sel) return sel.value;
    return td.getAttribute('data-order') != null ? td.getAttribute('data-order') : td.textContent.trim();
  }
  function rows(table) {
    var tb = table.tBodies[0];
    return tb ? Array.prototype.filter.call(tb.rows, function (r) { return !r.classList.contains('gt2-empty') && r.cells.length > 1; }) : [];
  }

  function sortBy(table, idx, dir) {
    var list = rows(table), tb = table.tBodies[0];
    if (!list.length) return;
    var vals = list.map(function (r) { return cellText(r.cells[idx]); });
    var kind = vals.every(function (v) { return v === '' || num(v) !== null; }) ? 'num'
             : vals.every(function (v) { return v === '' || date(v) !== null; }) ? 'date' : 'text';
    var key = function (v) { return kind === 'num' ? num(v) : kind === 'date' ? date(v) : v.toLowerCase(); };
    list.map(function (r, i) { return { r: r, k: key(vals[i]), i: i }; })
      .sort(function (a, b) {
        var ae = a.k === null || a.k === '', be = b.k === null || b.k === '';
        if (ae || be) return ae === be ? a.i - b.i : (ae ? 1 : -1);       // blanks last
        var c = kind === 'text' ? String(a.k).localeCompare(String(b.k), undefined, { numeric: true }) : a.k - b.k;
        return c ? c * dir : a.i - b.i;
      })
      .forEach(function (o) { tb.appendChild(o.r); });
  }

  function total(table) {
    var head = table.tHead && table.tHead.rows[0];
    if (!head) return;
    var ths = Array.prototype.slice.call(head.cells);
    var want = ths.map(function (th) { return th.getAttribute('data-total'); });
    var foot = table.tFoot;
    if (!want.some(Boolean)) { if (foot && foot.classList.contains('gt2-auto')) foot.remove(); return; }
    if (!foot) { foot = table.createTFoot(); foot.classList.add('gt2-auto'); }
    var list = rows(table);
    var tr = document.createElement('tr'); tr.className = 'gt2-total';
    ths.forEach(function (th, i) {
      var td = document.createElement('td');
      if (want[i] === 'sum') {
        var s = list.reduce(function (a, r) { var n = num(cellText(r.cells[i])); return a + (n || 0); }, 0);
        td.textContent = (Math.round(s * 100) / 100).toLocaleString('en-IN');
      } else if (want[i] === 'count') {
        td.textContent = (i === 0 ? (table.getAttribute('data-total-label') || 'Total') + ' ' : '') +
                         list.length.toLocaleString('en-IN');
      } else if (i === 0) {
        td.textContent = table.getAttribute('data-total-label') || 'Total';
      }
      tr.appendChild(td);
    });
    foot.innerHTML = ''; foot.appendChild(tr);
  }

  function refresh(table) {
    if (!table) return;
    if (table.dataset.gt2Sort) {
      var p = table.dataset.gt2Sort.split(':'); sortBy(table, +p[0], +p[1]);
    }
    total(table);
  }
  window.gt2Refresh = refresh;

  document.addEventListener('click', function (e) {
    var th = e.target.closest('table.gtable2 > thead th');
    if (!th || th.hasAttribute('data-nosort') || e.target.closest('input,select,button,a')) return;
    var table = th.closest('table'), idx = th.cellIndex;
    var prev = (table.dataset.gt2Sort || '').split(':');
    var dir = (+prev[0] === idx && prev[1] === '1') ? -1 : 1;
    table.dataset.gt2Sort = idx + ':' + dir;
    table.tHead.querySelectorAll('th').forEach(function (h) { h.removeAttribute('aria-sort'); });
    th.setAttribute('aria-sort', dir === 1 ? 'ascending' : 'descending');
    sortBy(table, idx, dir);
  });
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('table.gtable2').forEach(total);
  });
})();
