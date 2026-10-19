/* Reusable scan/type multi-tag input: auto-checks matching rows in a table
 * and keeps a live selected-count element in sync (both from scanning AND
 * from manual row-checkbox clicks). Used on IQC List, GRN Post-IQC, and
 * Inventory Manager's Inventory Stock table. */
function initTagScanAutocheck({ inputId, tableSelector, rowTagAttr = 'data-tag', countSelector, rowCheckboxSelector = 'input[type="checkbox"]' }) {
  var input = document.getElementById(inputId);
  var table = document.querySelector(tableSelector);
  if (!input || !table) return;

  function updateCount() {
    if (!countSelector) return;
    var countEl = document.querySelector(countSelector);
    if (!countEl) return;
    countEl.textContent = table.querySelectorAll(rowCheckboxSelector + ':checked').length;
  }

  function applyTags() {
    var tags = input.value.split(',').map(function (t) { return t.trim().toUpperCase(); }).filter(Boolean);
    var tagSet = {};
    tags.forEach(function (t) { tagSet[t] = true; });
    table.querySelectorAll('tr[' + rowTagAttr + ']').forEach(function (row) {
      var tag = (row.getAttribute(rowTagAttr) || '').toUpperCase();
      var cb = row.querySelector(rowCheckboxSelector);
      if (cb && tagSet[tag] && !cb.checked) {
        cb.checked = true;
        // Pages often bind their own selected-count logic via a delegated
        // 'change' listener (e.g. jQuery `.on('change', '.rowChk', ...)`) —
        // setting .checked directly doesn't fire that, so dispatch it.
        cb.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
    updateCount();
  }

  input.addEventListener('input', applyTags);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); applyTags(); }
  });
  // Keep the count in sync with manual row-checkbox clicks and "select all" too.
  table.addEventListener('change', function (e) {
    if (e.target.matches(rowCheckboxSelector)) updateCount();
  });
}

/* Scan-to-select for SERVER-SIDE DataTables. One scan narrows the table via
 * the server-side search, ticks the row it resolves to, records it in the
 * caller's selection Set, and re-highlights the input so the scanner's next
 * value overwrites this one with no keyboard in between. Because the Set
 * outlives the redraw, scanning ten tags in a row leaves all ten selected
 * even though only the last one's row is on screen.
 *
 * Distinct from initTagScanAutocheck above, which matches a comma-separated
 * list against rows already rendered on the current page. The two coexist on
 * the same input: comma-bearing values are left to that one.
 *
 *   dt                 - the DataTables API instance (required)
 *   selection          - Set of barcodes the page uses as its source of truth
 *   onChange           - called after the Set changes, to refresh count badges
 */
function initScanSelect({ inputId, dt, tableSelector, checkboxSelector, selection, onChange, debounceMs = 250 }) {
  var input = document.getElementById(inputId);
  if (!input || !dt) return;

  var pendingTag = null;        // value awaiting auto-select on the next draw
  var pendingReselect = false;  // re-highlight input once that draw lands
  var timer = null;
  // Set once a scan has matched a Tag Number exactly (2026-10-19): the next
  // scan / keystroke then replaces the box instead of being appended, so the
  // operator never has to select or clear the old value by hand — even when
  // the highlight was lost (table redraw, focus moved) or the scanner sends
  // no Enter. Partial typing never sets it (exact matches only).
  var scanDone = false;

  function rearm() {
    scanDone = true;
    try { input.focus({ preventScroll: true }); } catch (e) { input.focus(); }
    input.select();
  }

  // Bound once, not per scan: rapid gun fire would otherwise stack a one-shot
  // handler per keystroke and re-tick rows that are already selected.
  dt.on('draw.dt', function () {
    if (pendingTag === null) return;
    var val = pendingTag;
    pendingTag = null;

    var boxes = document.querySelectorAll(tableSelector + ' tbody ' + checkboxSelector);
    var chk = null, exact = false;
    var up = val.toUpperCase();
    for (var i = 0; i < boxes.length; i++) {
      if ((boxes[i].value || '').toUpperCase() === up || (boxes[i].getAttribute('data-serial') || '').toUpperCase() === up) {
        chk = boxes[i]; exact = true; break;
      }
    }
    // Serial numbers aren't carried on every page's checkbox, so fall back to
    // "the search resolved to exactly one row" — that row is the scan.
    if (!chk && boxes.length === 1) chk = boxes[0];

    if (chk) {
      if (!chk.checked) {
        chk.checked = true;
        if (selection) selection.add(chk.value);
        if (onChange) onChange();
      }
      var tr = chk.closest('tr');
      if (tr) tr.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    if (exact || pendingReselect) rearm();
    pendingReselect = false;
  });

  function run(val, reselect) {
    // Clearing the box must reset the search WITHOUT arming auto-select —
    // an empty value matches everything, and a table that happens to redraw
    // to a single row would otherwise have that row silently ticked.
    pendingTag = val ? val : null;
    pendingReselect = !!(val && reselect);
    dt.search(val).draw();
  }

  // dt.search().draw() bypasses the table's own searchDelay, so debounce here
  // or a scanner typing 12 characters fires 12 ajax round-trips.
  // After a completed scan, the first inserted character (typed, scanned or
  // pasted) replaces the old value: select-all just before the browser
  // inserts it. Deleting/editing keys clear the flag and behave normally.
  input.addEventListener('beforeinput', function (e) {
    if (!scanDone) return;
    scanDone = false;
    if (e.inputType && e.inputType.indexOf('insert') === 0) input.select();
  });
  input.addEventListener('input', function () {
    var val = this.value.trim();
    if (val.indexOf(',') !== -1) return;   // comma lists belong to initTagScanAutocheck
    clearTimeout(timer);
    timer = setTimeout(function () { run(val, false); }, debounceMs);
  });
  // Scanners emit Enter as the terminator — act at once and re-highlight.
  input.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    var val = this.value.trim();
    if (!val || val.indexOf(',') !== -1) return;
    clearTimeout(timer);
    run(val, true);
  });
}
