/* Global Table behavior — one call layers the standard OxyPC heavy-table UX
 * onto a server-side DataTables instance:
 *
 *   1. Headers stay on a single line; DataTables' own autoWidth then sizes
 *      each column around that (plus clamped body content) instead of
 *      wrapping headers and squashing the grid.
 *   2. Horizontal scroll (scrollX) with the first column frozen via CSS
 *      position:sticky — see .gtable-scroll-wrap in app.css. No FixedColumns
 *      plugin needed: DataTables' own scrollX split (cloned header table +
 *      body table, kept in sync) already gives sticky positioning something
 *      to stick to in both halves. If the first column is a checkbox
 *      (auto-detected), the second column freezes too — a lone frozen
 *      checkbox does nothing to keep the row identifiable while scrolled,
 *      so its neighbor (usually the Tag Number) comes along with it. The
 *      second column's stick offset is measured from the real rendered
 *      width of column 1, not assumed, since that varies per table.
 *   3. Table fills the card at 100% width; scrollX takes over once real
 *      column widths exceed that instead of squashing them.
 *   4. Page length defaults to 12 (matches the site-wide DataTables default
 *      already set in base.html — restated here so this module works even
 *      if that default ever changes).
 *   5. Any cell at or over 32 characters of plain text (no markup — badges, links,
 *      and buttons are left alone) gets wrapped to 2 lines with the full
 *      value in a native title= tooltip — cheap at any row count, unlike a
 *      Bootstrap Tooltip instance per cell. Applies to every column by
 *      default; opts.clampColumns restricts it to specific columns instead.
 *   6. Table-top toolbar, left side: "Page view" (length) dropdown + a
 *      warning-badge filtered-row-count, then (optional, caller-added via a
 *      dom override) entity/category breakdown badges right after the count.
 *      Right side: search/scan box + pagination, then (optional, same
 *      mechanism) any buttons or checkboxes this table needs in its own
 *      toolbar — e.g. Cosmetic Received's admin-only Assign button and its
 *      Failed-from-Final-QC filter checkbox, prepended into .dataTables_filter.
 *      14px default text size (down from the 16px browser default); the
 *      last (Action) column is always centered, and the first column
 *      centers too but ONLY when it's a checkbox (.gtable-checkbox-first,
 *      set below from the rendered first body cell) — a real first column
 *      (a name, a tag number, ...) stays left-aligned. See app.css.
 *   7. Optional scan-to-select: hijacks DataTables' own search box (so
 *      "Scan or Search" is one field, not two) and wires the existing
 *      initScanSelect / initTagScanAutocheck helpers from
 *      tag-scan-autocheck.js onto it.
 *   8. Cross-browser by construction, not by browser-specific CSS: the
 *      frozen-column sticky header (point 2) relies only on standard
 *      position:sticky plus border-collapse:separate (already forced by
 *      DataTables' own bootstrap5 CSS on table.dataTable) — both work
 *      identically in Chrome, Edge, Safari, and Firefox, so no vendor
 *      prefixes or per-browser overrides exist or should be added here.
 *      The one real cross-browser failure mode found in production
 *      (2026-09-03) wasn't a rendering gap at all: this file is cache-
 *      busted via base.html's ?v={{ ASSET_VERSION }} stamp, and that
 *      stamp is computed in templates_config.py's _VERSIONED_ASSETS list
 *      — editing this file without also touching one of the other listed
 *      assets used to leave the stamp unchanged, so a browser that had
 *      already cached an older copy kept running it indefinitely (seen
 *      as the sticky header working in one browser but not another,
 *      both on the same deployed HEAD). global-table.js is now itself
 *      one of the files that stamp is derived from — see
 *      templates_config.py and tests/test_asset_version_cache_busting.py
 *      — so any future edit here always reaches every browser on next
 *      load. Keep it there if this file is ever renamed or split.
 *   9. Button labels never wrap to a 2nd line, icon or no icon — enforced
 *      in app.css, not here: `.gtable tbody td:last-child` (the Action
 *      column) inherits white-space:nowrap down onto every button inside
 *      it, and `.gtable-top .btn, .gtable-top button` covers the table-top
 *      toolbar (Assign, the admin bulk-Assign button, any caller-injected
 *      control prepended into .dataTables_filter) the same way — both
 *      areas are flex-wrap rows a narrow viewport can otherwise shrink a
 *      button below its own label's width. The caller-authored card-header
 *      convention below is a different piece of markup (outside what
 *      initGlobalTable touches) — give its own buttons a `text-nowrap`
 *      class if they're ever long enough to be at risk.
 *  10. Frozen-column sticky header always wins, even over DataTables' own
 *      CSS: its bootstrap5 stylesheet sets `position: relative` on every
 *      sortable header cell (for its sort-arrow icon), specificity 0,2,4
 *      — enough to silently beat the first-frozen-column rule's 0,2,2 and
 *      break the freeze on any table whose column 1 is actually sortable
 *      (every table except a checkbox-first one, where DataTables applies
 *      "sorting_disabled" instead, a class that vendor rule doesn't
 *      target). Found 2026-09-03, reported as "sticky header works with 2
 *      frozen columns but not 1" — never a browser thing, the 2nd-column
 *      rule only survived by coincidence (its extra
 *      [data-freeze-cols="2"] attribute selector happens to out-rank the
 *      same vendor rule). Both frozen-column rules in app.css now carry
 *      `!important` so neither depends on out-specificity-ing a
 *      third-party stylesheet.
 *  11. Pagination: chevron icons (bi-chevron-left/right) replace the
 *      "Previous"/"Next" text (language.paginate), and a custom pager
 *      ($.fn.dataTable.ext.pager.gtable_numbers, pagingType:
 *      'gtable_numbers', registered once at load below) replaces
 *      DataTables' own page-number pattern: only 2 pages show at either
 *      end before the ellipsis (down from DataTables' default of 5), and
 *      away from both ends only the current page shows, alone between two
 *      ellipses — no "current-1/current+1" neighbors. Not a tweak to
 *      DataTables' own numbers_length: that single setting drives the
 *      edge-page count and the middle-window size together, and the only
 *      value giving edge-count 2 (numbers_length=4) has a real gap —
 *      landing exactly on page 3 of a large table falls in DataTables'
 *      own "near start" branch, whose window doesn't include page 3, so
 *      the active page is never highlighted at all. The custom pager
 *      switches to the "current alone" pattern based on where the current
 *      page actually falls relative to the edge window, not a fixed
 *      threshold, so every page (including page 3) always renders
 *      correctly highlighted.
 *  12. Optional header select-all checkbox (opts.selectAll), safe against
 *      scrollX's own DOM surgery: when opts.freeze is on, DataTables
 *      relocates <thead> into a separate .dataTables_scrollHead table so it
 *      can render outside the scrolling body — the header checkbox the user
 *      actually sees and clicks now lives outside tableSelector's own
 *      subtree. A listener delegated from tableSelector itself (the
 *      instinctive thing to write) never sees that click; this module always
 *      delegates from dt.table().container() instead, which wraps both the
 *      relocated head and the body. Found 2026-09-07 on Devices' select-all
 *      (was delegated from '#devicesTable', silently dead the moment this
 *      module's scrollX shipped) — folded in here so no future page can
 *      reintroduce it by hand.
 *
 * Convention (not enforced here — the title text and any buttons/filters are
 * page-specific, so this is markup the caller writes, not something
 * initGlobalTable can inject): wrap the table in a plain Bootstrap card and
 * give it a
 * `card-header bg-transparent d-flex justify-content-between align-items-center`
 * with an icon + title on the left (e.g. "All Tags Inventory") and, only if
 * this table has action buttons and/or filter controls of its own, those on
 * the right — action buttons (e.g. Devices' Delete Selected/Customise/
 * Upload Tags/Export CSV, same header shape as
 * templates/cosmetic/received.html's "Devices in {{ stage_label }}" bar) OR
 * filter controls (e.g. L1/L2's whole Search/CPU/RAM/Hard Drive/Lot/PNA/
 * Failed-from-Final-QC bar, or QC's single Failed-from-Final-QC checkbox —
 * both server-rendered directly rather than JS-injected into
 * .dataTables_filter, moved there 2026-09-03 for exactly this reason) OR
 * both together. Never a plain count badge here — the table-top toolbar's
 * own row-count badge (point 6 above) already covers that; a repeated
 * number in the header adds nothing. Every page adopting the Global Table
 * module should follow this.
 *
 * Every plain DataTables option (ajax, columns, order, drawCallback, ...)
 * still passes straight through via dtOptions — this only supplies shared
 * defaults and never overwrites a caller's own columnDefs/drawCallback.
 *
 *   tableSelector      - e.g. '#devicesTable'
 *   dtOptions          - DataTables options; overrides the defaults below
 *                        (a caller-supplied `dom` or `language` fully/partly
 *                        replaces the corresponding default — see $.extend).
 *   opts.clampColumns  - column indices to restrict the >32-character
 *                        clamp+tooltip to (default: every column).
 *   opts.clampLength   - character threshold before a cell clamps+tooltips
 *                        (default: 32).
 *   opts.freeze        - pass false to opt out of scrollX + the frozen
 *                        first column (default: on).
 *   opts.scan          - { inputId, selection, checkboxSelector, onChange, placeholder }
 *                        if given, wires scan-to-select onto the table's own
 *                        search box. `selection` is the caller's Set of
 *                        selected row values (server-side tables only keep
 *                        the current page in the DOM, so selection state
 *                        has to live outside it) and `onChange` is called
 *                        after the Set changes, to refresh count badges.
 *   opts.selectAll     - { headerSelector, rowSelector, onChange, resetOnDraw, scope }
 *                        if given, wires a header "select all" checkbox onto
 *                        row checkboxes (see point 12 above). headerSelector/
 *                        rowSelector are jQuery selectors (e.g.
 *                        '#cosmeticSelectAll', '.cosmeticRowCheck'); onChange
 *                        fires after any header or row toggle, for count/
 *                        button-state updates; resetOnDraw (default false —
 *                        matches pre-existing per-page behavior) unchecks the
 *                        header on every redraw, for tables where a fresh
 *                        page/sort should never look like "all selected" by
 *                        accident. scope (default 'page') controls how wide
 *                        "all" reaches: 'page' checks only the rows on the
 *                        currently-displayed page (the simple "check what I
 *                        can see" pattern); 'search' checks every row
 *                        matching the current search/filter across every
 *                        page (rows({search:'applied'})) — use this when the
 *                        page's own search box needs to drive selection (a
 *                        plain 'page' scope leaves rows past page 1 of a
 *                        search result looking un-selectable, which reads as
 *                        a bug). Neither scope is a fit for select-all-
 *                        matching-NO-filter-at-all-across-every-page-of-the-
 *                        UNFILTERED-table semantics (e.g. Devices' own
 *                        barcode-fetch select-all, which selects rows never
 *                        even paged into the DOM) — that stays hand-rolled,
 *                        but must still delegate from dt.table().container(),
 *                        never tableSelector, for the same reason as point 12.
 *
 * Returns the DataTables API instance.
 */

// Custom page-number pattern (2026-09-03), registered once at load: near
// either end shows only the first/last 2 pages before the ellipsis (down
// from DataTables' own default of 5), and away from both ends shows just
// the current page between two ellipses — no "current-1 / current+1"
// neighbors. Not achievable by tuning DataTables' built-in
// $.fn.dataTable.ext.pager.numbers_length alone: that single value drives
// both the edge-page count AND the middle window size together, and the
// only setting that gives edge-count 2 (numbers_length=4) also has a real
// gap — landing exactly on page 3 of a large table falls in DataTables'
// own "near start" branch, whose window ([1,2]) doesn't include page 3, so
// the active page is never highlighted at all. This custom pager avoids
// that by switching to the "current page alone" pattern as soon as the
// current page falls outside the 2-page edge window, instead of at a
// fixed page-index threshold.
$.fn.dataTable.ext.pager.gtable_numbers = function (page, pages) {
  var LEADING = 2;
  var nums;
  if (pages <= LEADING + 2) {
    // Small enough that ellipsis wouldn't save anything — show every page.
    nums = [];
    for (var i = 0; i < pages; i++) nums.push(i);
  } else if (page < LEADING) {
    nums = [];
    for (var j = 0; j < LEADING; j++) nums.push(j);
    nums.push('ellipsis');
    nums.push(pages - 1);
  } else if (page >= pages - LEADING) {
    nums = [0, 'ellipsis'];
    for (var k = pages - LEADING; k < pages; k++) nums.push(k);
  } else {
    nums = [0, 'ellipsis', page, 'ellipsis', pages - 1];
  }
  nums.DT_el = 'span';
  return ['previous', nums, 'next'];
};

// Default "no data" illustration — inline SVG (no static asset to go
// missing, no extra HTTP request) at max-height:200px. Used automatically by
// every initGlobalTable table's empty state (see GTABLE_EMPTY_HTML below),
// so no page has to hand-author its own "no rows" placeholder <tr> ever
// again. A hand-authored placeholder row is exactly what caused the
// "DataTables warning: table id=... - Incorrect column count" class of bug
// (found 2026-09-23 on Ready to Sale's As-Is Lot table): a single <td
// colspan> row sitting in the table's initial HTML looks fine to the eye,
// but DataTables' own columnDefs (targets: [7], [8,9,10,11], ...) index into
// it as if it were a full data row and crash the moment the table actually
// has zero rows. DataTables' *own* built-in empty-table rendering (this
// file's language.emptyTable, inserted after init, never part of the
// columnDefs-indexed initial parse) has no such failure mode — so the fix
// isn't just this one table, it's never hand-authoring that row again.
// An empty, open crate (nothing to show) + a magnifying glass looking it
// over, with a couple of floating sparkles for a little life — same spirit
// as a typical "no results" illustration, built as flat shapes so it stays
// legible at 200px and themes cleanly (every fill/stroke is a CSS var with a
// light-mode-safe fallback, so it doesn't go invisible in dark mode).
var GTABLE_EMPTY_SVG =
  '<svg viewBox="0 0 200 160" width="180" height="144" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
    '<ellipse cx="100" cy="122" rx="72" ry="14" fill="var(--bs-tertiary-bg, #f1f3f5)"/>' +
    // Sparkles
    '<path d="M36 34 L39 42 L47 45 L39 48 L36 56 L33 48 L25 45 L33 42 Z" fill="var(--bs-warning, #ffc107)" opacity="0.8"/>' +
    '<path d="M164 78 L166 84 L172 86 L166 88 L164 94 L162 88 L156 86 L162 84 Z" fill="var(--bs-warning, #ffc107)" opacity="0.6"/>' +
    '<circle cx="150" cy="36" r="4" fill="var(--bs-primary, #0d6efd)" opacity="0.35"/>' +
    // Open crate: back rim, then left/right faces for a simple 3D look
    '<path d="M58 66 L100 50 L142 66 L100 82 Z" fill="var(--bs-tertiary-bg, #e9ecef)" stroke="var(--bs-border-color, #ced4da)" stroke-width="2" stroke-linejoin="round"/>' +
    '<path d="M58 66 L58 106 L100 122 L100 82 Z" fill="var(--bs-secondary-bg, #e9ecef)" stroke="var(--bs-border-color, #ced4da)" stroke-width="2" stroke-linejoin="round"/>' +
    '<path d="M142 66 L142 106 L100 122 L100 82 Z" fill="var(--bs-body-bg, #f8f9fa)" stroke="var(--bs-border-color, #ced4da)" stroke-width="2" stroke-linejoin="round"/>' +
    '<path d="M70 84 L92 92 M70 94 L92 102" stroke="var(--bs-border-color, #ced4da)" stroke-width="2" stroke-linecap="round" opacity="0.6"/>' +
    // Magnifying glass, tilted over the crate's open top
    '<circle cx="132" cy="46" r="19" fill="var(--bs-body-bg, #fff)" stroke="var(--bs-secondary-color, #6c757d)" stroke-width="5"/>' +
    '<line x1="145" y1="59" x2="160" y2="74" stroke="var(--bs-secondary-color, #6c757d)" stroke-width="6" stroke-linecap="round"/>' +
  '</svg>';

function _gtableEmptyHtml(imageUrl, captionText) {
  var img = imageUrl
    ? '<img src="' + imageUrl + '" alt="" class="gtable-empty-img">'
    : '<span class="gtable-empty-img d-inline-block">' + GTABLE_EMPTY_SVG + '</span>';
  var caption = captionText || 'No matching records found.';
  return '<div class="gtable-empty">' + img +
         '<div class="gtable-empty-text">' + caption + '</div></div>';
}

function initGlobalTable(tableSelector, dtOptions, opts) {
  opts = opts || {};
  dtOptions = dtOptions || {};
  var $table = $(tableSelector);
  $table.addClass('gtable');
  // Tag the table's card so its header gets the shared Global Table header
  // colour (.gtable-card > .card-header in app.css).
  $table.closest('.card').addClass('gtable-card');
  // Global Table inside a tab (.tab-pane): the tab already names the table,
  // so its card header is hidden (unless it carries buttons/inputs the table
  // needs), and the table wrapper gets the same padding as a card body
  // (.gtable-in-tab in app.css).
  var $pane = $table.closest('.tab-pane');
  if ($pane.length) {
    var $tabCard = $table.closest('.card');
    if ($tabCard.length && $pane.has($tabCard).length) {
      $tabCard.addClass('gtable-in-tab');
      var $hdr = $tabCard.children('.card-header');
      if ($hdr.length && !$hdr.find('button, a.btn, input, select').length) {
        $hdr.addClass('gtable-tab-hidden-header');
      }
    }
  }

  // Caller may still pass language.emptyTable (plain text, e.g. "No L3/L4
  // work orders assigned.") — captured before the deep-merge below so it
  // becomes the caption under the shared image instead of replacing it.
  // opts.emptyImage overrides the default SVG with a page-specific image
  // (e.g. a branded "no results" illustration); still capped at 200px via
  // .gtable-empty-img in app.css regardless of the image's native size.
  //
  // Two distinct DataTables language keys need this treatment, not just one:
  // emptyTable fires when the table's underlying data source has zero rows
  // (what the As-Is Lot table bug was about); zeroRecords fires separately
  // when a search/filter narrows an otherwise non-empty table down to zero
  // matches — DataTables never falls back from one to the other, so leaving
  // zeroRecords unset meant every "search filtered to zero rows" case fell
  // straight through to DataTables' own plain-text default and never showed
  // the illustration at all. Both are wired to the same image; only the
  // caption text differs, and zeroRecords keeps DataTables' own familiar
  // wording as the default so a filtered-to-zero result doesn't read as if
  // the table has no data at all.
  var emptyTableCaption = (dtOptions.language && dtOptions.language.emptyTable) || null;
  var zeroRecordsCaption = (dtOptions.language && dtOptions.language.zeroRecords) || 'No matching records found.';
  var emptyTableHtml = _gtableEmptyHtml(opts.emptyImage, emptyTableCaption);
  var zeroRecordsHtml = _gtableEmptyHtml(opts.emptyImage, zeroRecordsCaption);

  var clampLength = opts.clampLength || 32;
  var columnDefs = (dtOptions.columnDefs || []).slice();
  // Action column never sorts (2026-10-18): when the last header reads
  // "Action"/"Actions" (or is blank, i.e. a buttons-only column), it is made
  // non-orderable. Pushed after the caller's own columnDefs, so a page that
  // explicitly sets orderable on that column still wins.
  var lastHead = $(tableSelector).find('thead tr').first().children('th').last();
  if (lastHead.length && /^(actions?)?$/i.test($.trim(lastHead.text()))) {
    columnDefs.push({ targets: -1, orderable: false });
  }
  columnDefs.push({
    // No clampColumns given → every column. HTML-bearing cells (badges,
    // links, buttons — the norm for id/status/action columns) are always
    // left untouched regardless of length; only cells DataTables would
    // otherwise render as bare text are candidates for clamping.
    targets: (opts.clampColumns && opts.clampColumns.length) ? opts.clampColumns : '_all',
    render: function (data, type) {
      if (type !== 'display' || data === null || data === undefined) return data;
      var text = String(data);
      // < not <= : a value of exactly clampLength characters (e.g. a CPU
      // string landing right at 32) still clamps — "at or over" the
      // threshold, not strictly "over" it. Found 2026-09-02: real CPU values
      // cluster right around this boundary ("Intel Core i7-10810U @ 1.61 GHz"
      // is exactly 32 chars) and were silently slipping through untouched.
      if (text.length < clampLength || text.indexOf('<') !== -1) return data;
      var escaped = $('<div>').text(text).html().replace(/"/g, '&quot;');
      return '<span class="gtable-clamp2" title="' + escaped + '">' + text + '</span>';
    },
  });

  var freeze = opts.freeze !== false;
  var base = {
    pageLength: 12,
    pagingType: 'gtable_numbers',
    dom: '<"gtable-top d-flex justify-content-between align-items-center flex-wrap gap-2 mb-2"' +
           '<"d-flex align-items-center flex-wrap gap-2"li>' +
           '<"d-flex align-items-center flex-wrap gap-2"fp>' +
         '>rt',
    language: {
      search: '',
      lengthMenu: 'Page view: _MENU_',
      info: '<span class="badge text-bg-warning gtable-count-badge">_TOTAL_</span>',
      infoEmpty: '<span class="badge text-bg-warning gtable-count-badge">0</span>',
      infoFiltered: '',
      // Chevron icons instead of "Previous"/"Next" text — the page-number
      // links (1, 2, ..., last) are untouched, this only swaps the two
      // end buttons.
      paginate: {
        previous: '<i class="bi bi-chevron-left"></i>',
        next: '<i class="bi bi-chevron-right"></i>',
      },
    },
  };
  if (freeze) base.scrollX = true;

  var merged = $.extend(true, {}, base, dtOptions);
  merged.columnDefs = columnDefs; // set post-merge — array-of-objects deep-extend would merge by index, not append
  merged.language.emptyTable = emptyTableHtml; // set post-merge — always the image+caption wrapper, never a caller's bare string
  merged.language.zeroRecords = zeroRecordsHtml; // same wrapper for the "search filtered to zero" case — see note above

  var dt = $table.DataTable(merged);

  // Column 1 centers (app.css .gtable-checkbox-first) only when it's
  // actually a checkbox — every other table's first column is real content
  // (a name, a tag number, ...) and stays left-aligned like any other
  // column. Detected from the rendered cell, not assumed, so this works
  // regardless of whether the caller mentions checkboxes at all. Runs
  // independently of opts.freeze so the alignment rule doesn't depend on
  // scrollX being enabled.
  var markCheckboxFirstColumn = function () {
    var $firstBodyCell = $table.find('tbody tr:first-child td:first-child');
    var isCheckbox = $firstBodyCell.find('input[type="checkbox"]').length > 0;
    $table.toggleClass('gtable-checkbox-first', isCheckbox);
    // With scrollX the visible header lives in a separate table
    // (.dataTables_scrollHead); give it the same class so the select-all
    // checkbox centres over the row checkboxes (app.css rule above).
    $(dt.table().header()).closest('table').toggleClass('gtable-checkbox-first', isCheckbox);
    return isCheckbox;
  };
  dt.on('draw.dt', markCheckboxFirstColumn);
  markCheckboxFirstColumn();

  // App-wide table look (2026-10-17): navy rounded header, plain white rows,
  // ticked-row highlight, soft shadow — see ".gtable-cards" in app.css.
  // Added on DataTables' own wrapper so every Global Table gets it; a page
  // can opt out with opts.plainStyle = true.
  if (!(opts && opts.plainStyle)) {
    $table.closest('.dataTables_wrapper').addClass('gtable-cards');
  }

  if (freeze) {
    var $scrollWrap = $table.closest('.dataTables_scroll').addClass('gtable-scroll-wrap');
    // Freeze 2 columns when the first is a checkbox (a lone frozen checkbox
    // can't identify the row on its own), otherwise just the 1st. Reuses
    // the same checkbox check above rather than re-detecting it.
    var applyFreezeWidth = function () {
      var twoCols = $table.hasClass('gtable-checkbox-first');
      $scrollWrap.attr('data-freeze-cols', twoCols ? 2 : 1);
      if (twoCols) {
        var $firstBodyCell = $table.find('tbody tr:first-child td:first-child');
        var width = $firstBodyCell.outerWidth() || 0;
        $scrollWrap.get(0).style.setProperty('--gtable-col2-left', width + 'px');
      }
    };
    dt.on('draw.dt', applyFreezeWidth);
    applyFreezeWidth();
  }

  if (opts.scan && opts.scan.inputId) {
    var checkboxSelector = opts.scan.checkboxSelector || '.rowChk';
    $(dt.table().container()).find('input[type="search"]').first()
      .attr({ id: opts.scan.inputId, placeholder: opts.scan.placeholder || 'Scan Tag Number, or search…', autocomplete: 'off' })
      .addClass('font-monospace')
      .off();
    initScanSelect({
      inputId: opts.scan.inputId, dt: dt,
      tableSelector: tableSelector, checkboxSelector: checkboxSelector,
      selection: opts.scan.selection, onChange: opts.scan.onChange,
    });
    initTagScanAutocheck({
      inputId: opts.scan.inputId, tableSelector: tableSelector,
      rowCheckboxSelector: checkboxSelector,
    });
  }

  if (opts.selectAll && opts.selectAll.headerSelector) {
    var saHeader = opts.selectAll.headerSelector;
    var saRow = opts.selectAll.rowSelector || '.rowChk';
    var saOnChange = opts.selectAll.onChange || function () {};
    // Delegated from dt.table().container(), not tableSelector — see point
    // 12 in the file doc comment above. Default scope is rows({page:'current'})
    // rather than a bare selector so a deferRender/scrollCollapse table
    // can't accidentally reach into off-page rows that aren't really on
    // screen; for a plain table this matches a bare selector exactly, since
    // DataTables detaches off-page rows from the DOM entirely.
    // opts.selectAll.scope:'search' widens this to every row matching the
    // CURRENT search/filter across every page (rows({search:'applied'})) —
    // for a table whose "select all" should mean "select everything the
    // search box just found", not just the 12 rows currently on screen.
    var saScope = opts.selectAll.scope === 'search' ? { search: 'applied' } : { page: 'current' };
    $(dt.table().container()).on('change', saHeader, function () {
      var checked = $(this).prop('checked');
      $(dt.rows(saScope).nodes()).find(saRow).prop('checked', checked);
      saOnChange();
    });
    // Row checkboxes live in tbody, never relocated by scrollX, so a plain
    // document-level delegation (survives redraws inserting fresh rows) is
    // safe here — no container-scoping needed the way the header above does.
    $(document).on('change', saRow, function () {
      if (!$(this).prop('checked')) $(saHeader).prop('checked', false);
      saOnChange();
    });
    if (opts.selectAll.resetOnDraw) {
      dt.on('draw.dt', function () {
        $(saHeader).prop('checked', false);
        saOnChange();
      });
    }
  }

  // Card-body spacing: a table sitting in a .card-body already gets 1rem of
  // padding (app.css .card-body:has(table)). A table in a tab, or in a card
  // without a .card-body, gets the same 1rem on its wrapper instead, so every
  // Global Table has the same breathing room.
  var $wrap = $(dt.table().container());
  if ($pane.length || ($wrap.closest('.card').length && !$wrap.closest('.card-body').length)) {
    $wrap.addClass('gtable-tab-wrap');
    // Widths were measured before the padding existed — re-measure.
    dt.columns.adjust();
  }
  return dt;
}
