/* Shared date + export format (2026-11-04, Pankaj).
 *
 * 1. Dates on screen: gtParseDate / gtFormatDate — Global Tables show
 *    "04 Nov 2026 17:42" (or "04 Nov 2026" for a date-only value); used by
 *    static/js/global-table.js.
 * 2. Exports built in the page: any CSV a page creates (new Blob([...csv],
 *    {type: 'text/csv'})) is normalised the same way the server normalises its
 *    downloads (utils/export_format.py): dates YYYY-MM-DD with no time (a
 *    "Time"/"Timestamp" column keeps YYYY-MM-DD HH:MM), amounts as plain
 *    numbers ("₹1,17,900" -> 117900). Done by wrapping window.Blob for CSV
 *    text only, so every page export — and any future one — gets it without
 *    its own code. Cells written as ="…" (keeps leading zeros) are untouched.
 */
(function () {
var GT_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var GT_MON_IDX = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9, oct: 10, nov: 11, dec: 12 };
  var GT_TIME = '(?:[ T,]+(\\d{1,2}):(\\d{2})(?::(\\d{2})(?:\\.\\d+)?)?(?:\\s*([AaPp][Mm]))?)?';
  var GT_DATE_RES = [
    [new RegExp('^(\\d{1,2})-(\\d{1,2})-(\\d{4})' + GT_TIME + '$'), 'dmy'],
    [new RegExp('^(\\d{1,2})/(\\d{1,2})/(\\d{4})' + GT_TIME + '$'), 'dmy'],
    [new RegExp('^(\\d{4})-(\\d{2})-(\\d{2})' + GT_TIME + '(?:Z|[+-]\\d{2}:?\\d{2})?$'), 'ymd'],
    [new RegExp('^(\\d{1,2})[ -]([A-Za-z]{3,9})[ ,-]+(\\d{4})' + GT_TIME + '$'), 'dMy'],
  ];
  function gtParseDate(text) {
    var t = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
    if (t.length < 8 || t.length > 32 || !/\d{4}/.test(t)) return null;
    for (var i = 0; i < GT_DATE_RES.length; i++) {
      var m = GT_DATE_RES[i][0].exec(t);
      if (!m) continue;
      var kind = GT_DATE_RES[i][1], d, mo, y;
      if (kind === 'ymd') { y = +m[1]; mo = +m[2]; d = +m[3]; }
      else if (kind === 'dmy') { d = +m[1]; mo = +m[2]; y = +m[3]; }
      else { d = +m[1]; mo = GT_MON_IDX[m[2].toLowerCase().slice(0, m[2].toLowerCase() === 'sept' ? 4 : 3)]; y = +m[3]; }
      if (!mo || mo > 12 || d < 1 || d > new Date(y, mo, 0).getDate() || y < 1900 || y > 2200) return null;
      var hasTime = m[4] !== undefined, h = hasTime ? +m[4] : 0, mi = hasTime ? +m[5] : 0, sec = m[6] ? +m[6] : 0;
      if (m[7]) { var pm = /p/i.test(m[7]); if (h === 12) h = pm ? 12 : 0; else if (pm) h += 12; }
      if (h > 23 || mi > 59) return null;
      return { y: y, m: mo, d: d, h: h, mi: mi, s: sec, hasTime: hasTime };
    }
    return null;
  }
  function _gtPad(n) { return (n < 10 ? '0' : '') + n; }
  function gtFormatDate(p) {
    if (!p) return '';
    return _gtPad(p.d) + ' ' + GT_MONTHS[p.m - 1] + ' ' + p.y + (p.hasTime ? ' ' + _gtPad(p.h) + ':' + _gtPad(p.mi) : '');
  }
  function gtIsoDate(textOrParsed) {   // 'YYYY-MM-DD' (no time) or '' — for filters and exports
    var p = (textOrParsed && textOrParsed.y) ? textOrParsed : gtParseDate(textOrParsed);
    return p ? p.y + '-' + _gtPad(p.m) + '-' + _gtPad(p.d) : '';
  }
  function gtSortKey(p) { return '' + p.y + _gtPad(p.m) + _gtPad(p.d) + _gtPad(p.h) + _gtPad(p.mi) + _gtPad(p.s); }
  window.gtParseDate = gtParseDate; window.gtFormatDate = gtFormatDate; window.gtIsoDate = gtIsoDate; window.gtSortKey = gtSortKey;

  // ── Export normalisation (same rules as utils/export_format.py) ─────────────
  var MONEY_HDR = /price|value|amount|amt\b|cost|total|₹|\bmrp\b|\brate\b|payable|paid|balance|deduct|revenue|profit|margin|buying|selling|discount amt|tax\b|gst amt|charges?\b|fee/i;
  var NOT_MONEY_HDR = /qty|quantit|count|%|percent|pct|date|\bid\b|code|number|\bno\.?\b|days|age\b|rating|\bgb\b|\btb\b|\btags?\b|\bdevices?\b|\bunits\b|\bitems?\b|\brows?\b/i;
  var QTY_HDR = /qty|quantit|count|\bnos?\.?\b|\bunits\b/i;
  var TIME_HDR = /\btime\b|timestamp/i;
  var MONEY_RE = /^[−\-]?\s*₹?\s*[−\-]?\s*\d[\d,]*(?:\.\d+)?\s*(?:\/-)?$/;

  function numCol(h) { h = String(h || ''); return (MONEY_HDR.test(h) && !NOT_MONEY_HDR.test(h)) || QTY_HDR.test(h); }
  function money(t) {
    t = String(t).trim();
    if (!t || !MONEY_RE.test(t)) return null;
    var neg = t.indexOf('−') >= 0 || /^\s*-/.test(t);
    var n = t.replace(/[^\d.]/g, '');
    if (!n || isNaN(Number(n))) return null;
    n = String(Number(n));
    return neg ? '-' + n : n;
  }
  function cellOut(v, numeric, timeCol) {
    if (v == null) return v;
    var s = String(v);
    if (!s.trim() || s.charAt(0) === '=' || s.charAt(0) === "'") return v;
    var p = gtParseDate(s);
    if (p) return (p.hasTime && timeCol) ? gtIsoDate(p) + ' ' + _gtPad(p.h) + ':' + _gtPad(p.mi) : gtIsoDate(p);
    if (numeric || /^\s*[−\-]?\s*₹/.test(s)) { var m = money(s); if (m !== null) return m; }
    return v;
  }
  function parseCsv(text) {
    var rows = [], row = [], cell = '', q = false, i = 0, c;
    for (; i < text.length; i++) {
      c = text.charAt(i);
      if (q) {
        if (c === '"') { if (text.charAt(i + 1) === '"') { cell += '"'; i++; } else q = false; }
        else cell += c;
      } else if (c === '"' && cell === '') { q = true; }
      else if (c === ',') { row.push(cell); cell = ''; }
      else if (c === '\n' || c === '\r') {
        if (c === '\r' && text.charAt(i + 1) === '\n') i++;
        row.push(cell); rows.push(row); row = []; cell = '';
      } else cell += c;
    }
    if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
    return rows;
  }
  function csvCell(v) {
    v = v == null ? '' : String(v);
    if (/^=".*"$/.test(v)) return v;                       // ="00123" stays a formula
    return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  }
  function gtNormalizeCsv(text) {
    var bom = text.charCodeAt(0) === 0xFEFF, body = bom ? text.slice(1) : text;
    var rows = parseCsv(body);
    if (rows.length < 2) return text;
    var head = rows[0], numeric = head.map(numCol), timec = head.map(function (h) { return TIME_HDR.test(String(h || '')); });
    var out = [head.map(csvCell).join(',')];
    for (var r = 1; r < rows.length; r++) {
      out.push(rows[r].map(function (v, i) { return csvCell(cellOut(v, numeric[i], timec[i])); }).join(','));
    }
    return (bom ? '\uFEFF' : '') + out.join('\r\n');   // BOM kept exactly as the page wrote it
  }
  window.gtNormalizeCsv = gtNormalizeCsv;

  // Every CSV a page builds as text goes through gtNormalizeCsv.
  var NativeBlob = window.Blob;
  if (NativeBlob && !NativeBlob.__gtWrapped) {
    var Wrapped = function (parts, options) {
      var type = (options && options.type || '').toLowerCase();
      if (/csv/.test(type) && Array.isArray(parts) && parts.length && parts.every(function (x) { return typeof x === 'string'; }) && !(options && options.gtRaw)) {
        try { parts = [gtNormalizeCsv(parts.join(''))]; } catch (e) { /* keep the page's own text */ }
      }
      return new NativeBlob(parts, options);
    };
    Wrapped.prototype = NativeBlob.prototype;
    Wrapped.__gtWrapped = true;
    window.Blob = Wrapped;
  }
})();
