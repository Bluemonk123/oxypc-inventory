/* Crate Matched / Not-Matched (2026-10-31).
 * Live answer under a Crate Number field: is there already a Part Master row
 * for Part Name + Make + Model + Type (New/Harvest) + this crate?
 *   mode 'add'  (Parts GRN line, Add Harvest): Not-Matched = a new crate row
 *               will be created, allowed.
 *   mode 'take' (Handover): Not-Matched blocks, and so does a short crate.
 * crateMatch(getVals, msgEl, mode, qty, onResult) — getVals() returns
 * {name, make, model, source, crate}; onResult(ok, data) is optional. */
(function () {
  var timers = new WeakMap();
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  window.crateMatch = function (getVals, msgEl, mode, qty, onResult) {
    if (!msgEl) return;
    clearTimeout(timers.get(msgEl));
    timers.set(msgEl, setTimeout(function () {
      var v = getVals() || {};
      if (!v.name || !(v.crate || '').trim()) {
        msgEl.innerHTML = '';
        if (onResult) onResult(false, null);
        return;
      }
      var q = new URLSearchParams({ name: v.name || '', make: v.make || '', model: v.model || '',
                                    source: v.source || 'new', crate: v.crate || '' });
      fetch('/spare-parts/crate-match?' + q.toString(), { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var add = parseInt(qty, 10) || 0, ok, html;
          if (d.matched) {
            if (mode === 'take') {
              ok = d.qty >= add;
              html = ok
                ? '<span class="badge bg-success">Matched</span> <span class="small">' + esc(d.crate) + ' has <b>' + d.qty + '</b> available</span>'
                : '<span class="badge bg-warning text-dark">Matched</span> <span class="small text-danger">' + esc(d.crate) + ' has only <b>' + d.qty + '</b> — need ' + add + '</span>';
            } else {
              ok = true;
              html = '<span class="badge bg-success">Matched</span> <span class="small">' + esc(d.crate) + ' has <b>' + d.qty + '</b>' +
                     (add ? ' → will become <b>' + (d.qty + add) + '</b>' : '') + '</span>';
            }
          } else {
            ok = mode !== 'take';
            html = mode === 'take'
              ? '<span class="badge bg-danger">Not-Matched</span> <span class="small text-danger">this part is not in crate ' + esc(v.crate) + '</span>'
              : '<span class="badge bg-secondary">Not-Matched</span> <span class="small">a new row will be created in crate ' + esc(v.crate) + '</span>';
          }
          msgEl.innerHTML = html;
          if (onResult) onResult(ok, d);
        })
        .catch(function () { msgEl.innerHTML = ''; if (onResult) onResult(false, null); });
    }, 250));
  };
})();
