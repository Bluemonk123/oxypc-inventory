/* Model cards (2026-11-18, Pankaj) — templates/_model_cards.html,
   data from routers/model_cards.py. 6 rows of cards per load; filters,
   search and sort reload from the server. */
(function () {
  var root = document.getElementById('mcd');
  if (!root || root.dataset.ready) return;
  root.dataset.ready = '1';
  var SCOPE = root.dataset.scope, CAN_EXPORT = root.dataset.canExport === '1', CAN_SELL = root.dataset.canSell === '1',
      CAN_BLOCK = root.dataset.canBlock === '1';
  var grid = document.getElementById('mcdGrid'), more = document.getElementById('mcdMore');
  var GRADES = ['A', 'B', 'C', 'D', 'E', 'Scrap', 'NG'];
  // Grades present in a count map: known ones in order, then any other value
  // (so the colour bar always adds up to the total).
  function gkeys(g) {
    var k = GRADES.filter(function (x) { return g[x]; });
    Object.keys(g).sort().forEach(function (x) { if (g[x] && k.indexOf(x) < 0) k.push(x); });
    return k;
  }
  function gcls(k) { return 'mcd-g-' + (GRADES.indexOf(k) >= 0 ? k : 'NG'); }
  var S = { cards: [], total: 0, tags: 0, req: 0, loading: false };
  var modal = { kind: '', idx: -1, bs: null };

  function esc(v) { var d = document.createElement('div'); d.textContent = v == null ? '' : String(v); return d.innerHTML; }
  function fmt(n) { return Number(n || 0).toLocaleString('en-IN'); }
  function val(id) { var e = document.getElementById(id); return e ? e.value : ''; }

  function params() {
    var p = new URLSearchParams();
    p.set('scope', SCOPE);
    if (SCOPE === 'inventory') {   // the Tag table's own page filters
      var u = new URLSearchParams(window.location.search);
      ['q', 'stage', 'lot', 'grade', 'category', 'device_type', 'stock_type', 'pna', 'zone', 'entity',
       'date_from', 'date_to', 'exclude_sold', 'fs', 'employee'].forEach(function (k) { if (u.get(k)) p.set(k, u.get(k)); });
    }
    [['c_entity', 'ms_val_mc_entity'], ['c_device_type', 'ms_val_mc_dtype'], ['c_make', 'ms_val_mc_make'],
     ['c_touch', 'ms_val_mc_touch'], ['c_stock', 'ms_val_mc_stock'], ['c_loc', 'ms_val_mc_loc']].forEach(function (x) {
      var v = val(x[1]); if (v) p.set(x[0], v);
    });
    if (val('mcdAvail')) p.set('c_avail', val('mcdAvail'));
    if (val('mcdGrade')) p.set('c_grade', val('mcdGrade'));
    var q = (document.getElementById('mcdSearch') || {}).value || '';
    if (q.trim()) p.set('c_q', q.trim());
    p.set('sort', val('mcdSort') || 'stock');
    return p;
  }
  function cols() {
    var t = getComputedStyle(grid).gridTemplateColumns || '';
    var n = t && t !== 'none' ? t.split(' ').filter(Boolean).length : 0;
    return Math.max(1, n || Math.floor((grid.clientWidth || 1200) / 262) || 4);
  }

  function load(append) {
    var my = ++S.req, p = params();
    p.set('offset', append ? S.cards.length : 0);
    p.set('limit', cols() * 6);        // 6 rows of cards
    S.loading = true;
    more.disabled = true;
    if (!append) grid.classList.add('mcd-busy');
    fetch('/model-cards/data?' + p.toString(), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (d) {
        if (my !== S.req) return;
        var cards = (d.cards || []).map(function (c) { c.sel = ''; return c; });
        S.cards = append ? S.cards.concat(cards) : cards;
        S.total = d.total_models || 0; S.tags = d.total_tags || 0;
        if (!append) drawTabs(d.grade_tabs || [], d.grade || '');
        draw();
      })
      .catch(function () { if (my === S.req && !append) { S.cards = []; S.total = 0; draw(); } })
      .finally(function () { if (my === S.req) { S.loading = false; more.disabled = false; grid.classList.remove('mcd-busy'); } });
  }

  // Section grade tabs: "All <count>" then one per grade (ms-gtabs look).
  function drawTabs(tabs, cur) {
    var ul = document.getElementById('mcdGTabs');
    ul.innerHTML = tabs.map(function (t) {
      return '<li class="nav-item"><button type="button" class="nav-link py-1 px-3 ms-gtab' + (t.grade === cur ? ' active' : '') +
        '" data-grade="' + esc(t.grade) + '">' + esc(t.label) + ' <span class="badge rounded-pill ms-1">' + fmt(t.n) + '</span></button></li>';
    }).join('');
    ul.classList.toggle('d-none', !tabs.length);
  }
  function gradeBar(g, t) {
    return '<div class="mcd-gb">' + gkeys(g).map(function (k) {
      return '<span class="' + gcls(k) + '" style="width:' + (g[k] / t * 100).toFixed(2) + '%" title="' + k + ' ' + fmt(g[k]) + '"></span>';
    }).join('') + '</div>';
  }
  function cardName(c) { return c.kind === 'lot' ? 'Sub-Lot ' + c.model : c.make + ' ' + c.model; }
  // As-Is lot card: its parent lot numbers ("Lot 4673, 4593 +3").
  function lotsLine(l) {
    l = l || [];
    if (!l.length) return 'Lot —';
    return 'Lot ' + esc(l.slice(0, 2).join(', ')) + (l.length > 2 ? ' +' + (l.length - 2) : '');
  }
  function cardHtml(i, full) {
    var c = S.cards[i], grades = gkeys(c.grades);
    var stack = '<div class="mcd-gs' + (c.sel ? ' picked' : '') + '">' +
      '<div class="mcd-hcell mcd-all' + (c.sel ? '' : ' on') + '" data-i="' + i + '" data-g=""><span>All</span><span>' + fmt(c.total) + '</span></div>' +
      grades.map(function (k) {
        return '<div class="mcd-gc ' + gcls(k) + (c.sel === k ? ' on' : '') + '" data-i="' + i + '" data-g="' + esc(k) + '"><span>' + esc(k) + '</span><span>' + fmt(c.grades[k]) + '</span></div>';
      }).join('') + '</div>';
    var rows = c.variants.map(function (v, j) { return { j: j, label: v.label, n: c.sel ? (v.grades[c.sel] || 0) : v.total }; })
      .filter(function (r) { return r.n > 0; }).sort(function (a, b) { return b.n - a.n; });   // zero hidden
    var sum = rows.reduce(function (s, r) { return s + r.n; }, 0);
    var vis = full ? rows : rows.slice(0, 3), extra = rows.length - vis.length;
    var list = '<div class="mcd-vl"><div class="mcd-hcell mcd-vh"><span>' + (c.sel ? 'Grade ' + c.sel + ' · variants' : 'Variants') + '</span><b>' + fmt(sum) + '</b></div>' +
      vis.map(function (r) {
        return '<div class="mcd-vrow" data-i="' + i + '" data-j="' + r.j + '" title="' + esc(r.label) + ' — open these ' + fmt(r.n) + ' tags"><span>' + esc(r.label) + '</span><b>' + fmt(r.n) + '</b></div>';
      }).join('') +
      (extra > 0 ? '<div class="mcd-vrow mcd-more" data-more="' + i + '">+' + extra + ' more</div>' : '') + '</div>';
    return '<div class="mcd-card" data-card="' + i + '"><div class="mcd-hd"><i class="bi ' + esc(c.icon) + ' mcd-cat" aria-hidden="true"></i>' +
      (c.kind === 'lot'
        ? '<div class="mcd-mk" title="Lot ' + esc((c.lots || []).join(', ')) + '">' + lotsLine(c.lots) + '</div>'
        : '<div class="mcd-mk">' + esc(c.make) + ' · ' + esc(c.category) + '</div><div class="mcd-nm">' + esc(c.model) + '</div>') +
      '<div class="mcd-trow">' + (c.kind === 'lot'
        ? '<div class="mcd-tot mcd-tot-sub" title="Sub-Lot ' + esc(c.model) + ' · ' + fmt(c.total) + ' in stock">' + esc(c.model) + '</div>'
        : '<div class="mcd-tot">' + fmt(c.total) + ' <span>in stock</span></div>') +
      (c.blocked ? '<span class="mcd-blk" title="Blocked tags (Block Tags hold or assigned to a user)">Blocked ' + fmt(c.blocked) + '</span>' : '') + '</div>' +
      '</div><div class="mcd-body">' + stack + list + '</div></div>';
  }
  function draw() {
    grid.innerHTML = S.cards.map(function (c, i) { return cardHtml(i, false); }).join('');
    document.getElementById('mcdEmpty').classList.toggle('d-none', S.cards.length > 0);
    document.getElementById('mcdCount').textContent = S.total ? fmt(S.tags) + ' tags' : '';
    var xb = document.getElementById('mcdExportAll');
    if (xb) { document.getElementById('mcdExportN').textContent = fmt(S.tags); xb.disabled = !S.tags; }
    var left = S.total - S.cards.length;
    more.classList.toggle('d-none', left <= 0);
    more.innerHTML = '<i class="bi bi-chevron-double-down me-1"></i>Load more (' + fmt(Math.min(left, cols() * 6)) + ' of ' + fmt(left) + ')';
  }
  function redrawCard(i) {
    var el = grid.querySelector('[data-card="' + i + '"]');
    if (el) el.outerHTML = cardHtml(i, false);
    if (modal.kind === 'card' && modal.idx === i) document.getElementById('mcdModalBody').innerHTML = cardHtml(i, true);
  }

  function openModal(kind, title, bodyHtml, footHtml, wide) {
    modal.kind = kind;
    document.getElementById('mcdModalTitle').textContent = title;
    document.getElementById('mcdModalBody').innerHTML = bodyHtml;
    var foot = document.getElementById('mcdModalFoot');
    foot.innerHTML = footHtml || '';
    foot.classList.toggle('d-none', !footHtml);
    document.getElementById('mcdModalDialog').className = 'modal-dialog modal-dialog-scrollable' + (wide ? ' modal-lg' : '');
    modal.bs = modal.bs || bootstrap.Modal.getOrCreateInstance(document.getElementById('mcdModal'));
    modal.bs.show();
  }
  // Variant tags pop-up: every grade of the variant is fetched once; grade tabs
  // (All / A / B …, in the grade colours) filter the table and the buttons.
  function openTags(i, j) {
    var c = S.cards[i], v = c.variants[j], p = params();
    p.set('make', c.make); p.set('model', c.model);
    if (c.kind === 'lot') {   // As-Is lot card: variant = Make + Model + spec
      p.set('vmake', v.key[0]); p.set('vmodel', v.key[1]);
      p.set('cpu', v.key[2]); p.set('gen', v.key[3]); p.set('ram', v.key[4]); p.set('storage', v.key[5]);
    } else {
      p.set('cpu', v.key[0]); p.set('gen', v.key[1]); p.set('ram', v.key[2]); p.set('storage', v.key[3]);
    }
    modal.idx = -1; modal.tags = []; modal.capped = false; modal.g = c.sel || val('mcdGrade') || '';
    openModal('tags', cardName(c) + ' · ' + v.label,
      '<div class="p-3 text-muted small">Loading tags…</div>', '', true);
    fetch('/model-cards/tags?' + p.toString(), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (d) { modal.tags = d.tags || []; modal.capped = !!d.capped; drawTags(); })
      .catch(function () { document.getElementById('mcdModalBody').innerHTML = '<div class="p-3 text-danger small">Could not load the tags.</div>'; });
  }
  function drawTags() {
    var all = modal.tags, cnt = {};
    all.forEach(function (t) { cnt[t.grade] = (cnt[t.grade] || 0) + 1; });
    var gs = gkeys(cnt);
    if (modal.g && !cnt[modal.g]) modal.g = '';
    var tags = modal.g ? all.filter(function (t) { return t.grade === modal.g; }) : all;
    var tabs = '<div class="mcd-ttabs"><button type="button" class="mcd-tt mcd-tt-all' + (modal.g ? '' : ' on') + '" data-tg="">All <b>' + fmt(all.length) + '</b></button>' +
      gs.map(function (k) {
        return '<button type="button" class="mcd-tt ' + gcls(k) + (modal.g === k ? ' on' : '') + '" data-tg="' + esc(k) + '">' + esc(k) + ' <b>' + fmt(cnt[k]) + '</b></button>';
      }).join('') + '</div>';
    var open = tags.filter(function (t) { return !t.blocked; }).map(function (t) { return t.barcode; });
    var bar = tags.map(function (t) { return t.barcode; });
    document.getElementById('mcdModalBody').innerHTML = all.length
      ? tabs + '<table class="table table-sm table-hover mb-0 small mcd-tags"><thead><tr><th>Tag Number</th><th>Grade</th><th>Location</th><th>Entity</th><th>Status</th></tr></thead><tbody>' +
        tags.map(function (t) {
          return '<tr><td class="font-monospace"><a href="/devices/' + encodeURIComponent(t.barcode) + '" target="_blank" rel="noopener">' + esc(t.barcode) + '</a></td><td><span class="mcd-gpill ' + gcls(t.grade) + '">' + esc(t.grade) +
            '</span></td><td>' + esc(t.location) + '</td><td>' + esc(t.entity || '—') + '</td><td>' +
            (t.blocked ? '<span class="badge bg-danger">Blocked</span>' : '<span class="badge bg-success">Open</span>') + '</td></tr>';
        }).join('') + '</tbody></table>' + (modal.capped ? '<div class="p-2 small text-muted">Showing the first ' + fmt(all.length) + ' tags.</div>' : '')
      : '<div class="p-3 text-muted small">No tags.</div>';
    var foot = document.getElementById('mcdModalFoot'), btns = '<span class="small text-muted me-auto">' + (modal.g ? 'Grade ' + esc(modal.g) + ' · ' : '') + fmt(tags.length) + ' tags · ' + fmt(open.length) + ' open</span>' +
      '<button type="button" class="btn btn-sm btn-outline-secondary" data-mcd-copy>Copy tag numbers</button>';
    if (CAN_EXPORT && bar.length) btns += '<button type="button" class="btn btn-sm btn-outline-primary" data-mcd-export><i class="bi bi-file-earmark-arrow-down me-1"></i>Export</button>';
    var blk = tags.filter(function (t) { return t.blockable; });
    modal.block = blk.map(function (t) { return { id: t.id, barcode: t.barcode, model: t.model }; });
    if (CAN_BLOCK && blk.length && typeof window.nbOpen === 'function') btns += '<button type="button" class="btn btn-sm btn-warning" data-mcd-block title="Open New Block Tags with these tags selected"><i class="bi bi-lock me-1"></i>Block ' + fmt(blk.length) + '</button>';
    if (CAN_SELL && open.length && typeof window.sellBarcodesList === 'function') btns += '<button type="button" class="btn btn-sm btn-success" data-mcd-sell>Sell ' + fmt(open.length) + ' open</button>';
    foot.innerHTML = btns; foot.classList.toggle('d-none', !all.length);
    foot.dataset.all = bar.join(','); foot.dataset.open = open.join(',');
  }

  root.addEventListener('click', function (e) {
    var t;
    if ((t = e.target.closest('[data-g]')) && root.contains(t)) {
      var c = S.cards[+t.dataset.i]; c.sel = c.sel === t.dataset.g ? '' : t.dataset.g; redrawCard(+t.dataset.i); return;
    }
    if ((t = e.target.closest('[data-more]'))) {
      var i = +t.dataset.more; modal.idx = i;
      openModal('card', cardName(S.cards[i]) + ' · all variants', cardHtml(i, true), '', false); return;
    }
    if ((t = e.target.closest('.mcd-vrow[data-j]'))) { openTags(+t.dataset.i, +t.dataset.j); return; }
    if ((t = e.target.closest('#mcdExportAll'))) {
      var btn = t; btn.disabled = true;
      fetch('/model-cards/barcodes?' + params().toString(), { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (d) {
          if (!(d.barcodes || []).length) return;
          document.getElementById('mcdExportTags').value = d.barcodes.join(',');
          document.getElementById('mcdExportForm').submit();
        })
        .catch(function () { if (window.showTopToast) window.showTopToast('error', 'Could not export the tags.'); })
        .finally(function () { btn.disabled = !S.tags; });
      return;
    }
    if (e.target.closest('#mcdMore')) { if (!S.loading) load(true); return; }
    if (e.target.closest('#mcdReset')) {
      root.querySelectorAll('.mcd-filters .ms-none').forEach(function (b) { b.click(); });
      document.getElementById('mcdAvail').value = ''; document.getElementById('mcdSearch').value = '';
      document.getElementById('mcdGrade').value = '';
      load(false);
    }
  });
  // Pop-up (outside #mcd): expanded card interactions + tag actions.
  document.getElementById('mcdModal').addEventListener('click', function (e) {
    var t, foot = document.getElementById('mcdModalFoot');
    if ((t = e.target.closest('[data-tg]'))) { modal.g = t.dataset.tg; drawTags(); return; }
    if ((t = e.target.closest('[data-g]'))) { var c = S.cards[+t.dataset.i]; c.sel = c.sel === t.dataset.g ? '' : t.dataset.g; redrawCard(+t.dataset.i); return; }
    if ((t = e.target.closest('.mcd-vrow[data-j]'))) { openTags(+t.dataset.i, +t.dataset.j); return; }
    if (e.target.closest('[data-mcd-copy]')) {
      var txt = (foot.dataset.all || '').split(',').join('\n');
      if (navigator.clipboard) navigator.clipboard.writeText(txt);
      e.target.closest('[data-mcd-copy]').textContent = 'Copied'; return;
    }
    if (e.target.closest('[data-mcd-export]')) {
      document.getElementById('mcdExportTags').value = foot.dataset.all || '';
      document.getElementById('mcdExportForm').submit(); return;
    }
    if (e.target.closest('[data-mcd-block]')) {
      var bl = modal.block || [];
      if (modal.bs) modal.bs.hide();
      setTimeout(function () { window.nbOpen(bl); }, 300);   // after the pop-up has closed
      return;
    }
    if (e.target.closest('[data-mcd-sell]')) {
      var tags = (foot.dataset.open || '').split(',').filter(Boolean);
      if (modal.bs) modal.bs.hide();
      window.sellBarcodesList(tags);
    }
  });
  // A block saved from the pop-up: refresh the cards (Blocked counts change).
  document.addEventListener('block:created', function (e) {
    if (window.showTopToast) window.showTopToast('success', 'Block ' + e.detail.block_id + ' created (' + e.detail.count + ' tag' + (e.detail.count === 1 ? '' : 's') + ').');
    load(false);
  });
  document.getElementById('mcdModal').addEventListener('hidden.bs.modal', function () { modal.kind = ''; modal.idx = -1; });

  var timer;
  function soon() { clearTimeout(timer); timer = setTimeout(function () { load(false); }, 300); }
  ['mcdGrade', 'ms_val_mc_entity', 'ms_val_mc_dtype', 'ms_val_mc_make', 'ms_val_mc_touch', 'ms_val_mc_stock', 'ms_val_mc_loc'].forEach(function (id) {
    var el = document.getElementById(id); if (el) el.addEventListener('ms:change', soon);
  });
  // Pages with their own multi-select handler (All Inventories) don't fire
  // ms:change: also follow the ticks / All / Clear directly (after the page
  // handler has written the hidden value).
  var filters = root.querySelector('.mcd-filters');
  filters.addEventListener('change', function (e) { if (e.target.classList.contains('ms-opt')) setTimeout(soon, 0); });
  filters.addEventListener('click', function (e) { if (e.target.closest('.ms-all, .ms-none')) setTimeout(soon, 0); });
  document.getElementById('mcdAvail').addEventListener('change', soon);
  document.getElementById('mcdSort').addEventListener('change', soon);
  document.getElementById('mcdSearch').addEventListener('input', soon);
  load(false);
})();
