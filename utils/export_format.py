"""Global export format (2026-11-04, Pankaj).

Every CSV / Excel file the app sends as a download goes through
normalize_export() once, just before it leaves the server (main.py
_export_format middleware), so no export route needs its own date/amount code:

  * Dates  -> YYYY-MM-DD, no time. In Excel files they become real date cells
              (number format yyyy-mm-dd) so Excel's filter and sort work.
              Exception: a column whose header says "Time"/"Timestamp" keeps
              its time as YYYY-MM-DD HH:MM (the time is the data there).
  * Amounts -> plain numbers: "₹1,17,900" / "1,17,900.00" -> 117900, so SUM
              adds every row (Excel read "1,17,900" as text and skipped it —
              found on Parts Manager, 13.27M vs 22.59M).
              A cell is an amount when it starts with ₹, or when its column
              header reads like money (Price / Value / Amount / Cost / Total /
              MRP / Paid / Balance / ...) and not like a count or a code.
  * Everything else is left exactly as it was.

The browser-side twin is static/js/date-export.js (same rules) for the few
pages that build their CSV in the page. A route can opt out by sending the
header X-Export-Raw: 1.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTHS["sept"] = 9

_TIME = r"(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(?:\s*([AaPp][Mm]))?)?"
_DATE_RES = [
    (re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})" + _TIME + r"$"), "dmy"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})" + _TIME + r"$"), "dmy"),
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})" + _TIME + r"(?:Z|[+-]\d{2}:?\d{2})?$"), "ymd"),
    (re.compile(r"^(\d{1,2})[ -]([A-Za-z]{3,9})[ ,-]+(\d{4})" + _TIME + r"$"), "dMy"),
]

MONEY_HDR = re.compile(r"price|value|amount|amt\b|cost|total|₹|\bmrp\b|\brate\b|payable|paid|balance|deduct|"
                       r"revenue|profit|margin|buying|selling|discount amt|tax\b|gst amt|charges?\b|fee", re.I)
NOT_MONEY_HDR = re.compile(r"qty|quantit|count|%|percent|pct|date|\bid\b|code|number|\bno\.?\b|days|age\b|"
                           r"rating|\bgb\b|\btb\b|\btags?\b|\bdevices?\b|\bunits\b|\bitems?\b|\brows?\b", re.I)
# Quantity columns: "1,200" -> 1200 too (SUM), but never a free-text column.
QTY_HDR = re.compile(r"qty|quantit|count|\bnos?\.?\b|\bunits\b", re.I)
TIME_HDR = re.compile(r"\btime\b|timestamp", re.I)
_MONEY_RE = re.compile(r"^[−\-]?\s*₹?\s*[−\-]?\s*\d[\d,]*(?:\.\d+)?\s*(?:/-)?$")


def parse_date(text: str):
    """(date_or_datetime, has_time) from a date-looking string, else None."""
    t = re.sub(r"\s+", " ", str(text)).strip()
    if len(t) < 8 or len(t) > 32 or not re.search(r"\d{4}", t):
        return None
    for rx, kind in _DATE_RES:
        m = rx.match(t)
        if not m:
            continue
        try:
            if kind == "ymd":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif kind == "dmy":
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                name = m.group(2).lower()
                mo = MONTHS.get(name if name == "sept" else name[:3])
                d, y = int(m.group(1)), int(m.group(3))
            if not mo or not (1900 <= y <= 2200):
                return None
            has_time = m.group(4) is not None
            h = int(m.group(4)) if has_time else 0
            mi = int(m.group(5)) if has_time else 0
            if m.group(7):
                pm = m.group(7).lower() == "pm"
                h = (12 if pm else 0) if h == 12 else (h + 12 if pm else h)
            return (datetime(y, mo, d, h, mi) if has_time else date(y, mo, d)), has_time
        except ValueError:            # 31-02-2026 and friends
            return None
    return None


def is_money_header(h) -> bool:
    h = str(h or "")
    return bool(MONEY_HDR.search(h)) and not NOT_MONEY_HDR.search(h)


def parse_money(text):
    """Decimal from '₹1,17,900' / '1,17,900.00' / '− ₹800', else None."""
    t = str(text).strip()
    if not t or not _MONEY_RE.match(t):
        return None
    neg = "−" in t or t.lstrip().startswith("-")
    digits = re.sub(r"[^\d.]", "", t)
    try:
        v = Decimal(digits)
    except InvalidOperation:
        return None
    return -v if neg else v


def _num_text(v: Decimal) -> str:
    v = v.normalize()
    s = format(v, "f")
    return s


def normalize_value(value, header, money_col: bool, time_col: bool):
    """New value for one cell (CSV: str; Excel: date/datetime/number/str)."""
    if isinstance(value, datetime):
        return value if time_col else value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return value
    s = value
    if s.startswith("=") or s.startswith("'"):          # ="00123" keeps leading zeros in Excel
        return value
    p = parse_date(s)
    if p:
        dt, has_time = p
        if has_time and time_col:
            return dt
        return dt.date() if isinstance(dt, datetime) else dt
    if money_col or s.lstrip().lstrip("−-").lstrip().startswith("₹"):
        m = parse_money(s)
        if m is not None:
            return m
    return value


def _csv_out(v) -> str:
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, Decimal):
        return _num_text(v)
    return "" if v is None else str(v)


def normalize_csv(data: bytes) -> bytes:
    bom = data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    first = text.split("\n", 1)[0]
    dialect = csv.excel
    if "," not in first and ("\t" in first or ";" in first):      # rare tab / semicolon files
        dialect = csv.excel_tab if "\t" in first else type("semi", (csv.excel,), {"delimiter": ";"})
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return data
    header = rows[0]
    money = [is_money_header(h) or bool(QTY_HDR.search(str(h or ""))) for h in header]
    timec = [bool(TIME_HDR.search(str(h or ""))) for h in header]
    delim = getattr(dialect, "delimiter", ",")

    def cell(v) -> str:
        v = "" if v is None else str(v)
        if re.fullmatch(r'="[^"]*"', v):          # ="00123": Excel text formula, must stay unquoted
            return v
        if any(ch in v for ch in (delim, '"', "\r", "\n")):
            return '"' + v.replace('"', '""') + '"'
        return v
    lines = [delim.join(cell(h) for h in header)]
    for r in rows[1:]:
        lines.append(delim.join(cell(_csv_out(normalize_value(v, header[i] if i < len(header) else "",
                                                             money[i] if i < len(money) else False,
                                                             timec[i] if i < len(timec) else False)))
                                for i, v in enumerate(r)))
    out = io.StringIO()
    out.write("\r\n".join(lines))
    body = out.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + body) if bom else body      # BOM kept exactly as the route sent it


def _header_row(ws, max_scan=6):
    """First row with 2+ text cells — sheets sometimes start with a title row."""
    best = None
    for row in ws.iter_rows(min_row=1, max_row=min(max_scan, ws.max_row)):
        texts = [c for c in row if isinstance(c.value, str) and c.value.strip()]
        if len(texts) >= 2:
            return row[0].row
        if best is None and texts:
            best = row[0].row
    return best or 1


def normalize_xlsx(data: bytes) -> bytes:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data))
    for ws in wb.worksheets:
        if ws.max_row < 2:
            continue
        hr = _header_row(ws)
        headers = {c.column: (c.value if isinstance(c.value, str) else "") for c in ws[hr]}
        money = {k: is_money_header(v) or bool(QTY_HDR.search(v or "")) for k, v in headers.items()}
        timec = {k: bool(TIME_HDR.search(v or "")) for k, v in headers.items()}
        for row in ws.iter_rows(min_row=hr + 1):
            for c in row:
                v = c.value
                if v is None or c.data_type == "f":
                    continue
                nv = normalize_value(v, headers.get(c.column, ""), money.get(c.column, False), timec.get(c.column, False))
                if nv is v:
                    if isinstance(v, datetime) or isinstance(v, date):
                        pass
                    else:
                        continue
                if isinstance(nv, datetime):
                    c.value, c.number_format = nv, "yyyy-mm-dd hh:mm"
                elif isinstance(nv, date):
                    c.value, c.number_format = nv, "yyyy-mm-dd"
                elif isinstance(nv, Decimal):
                    c.value = int(nv) if nv == nv.to_integral_value() else float(nv)
                    if c.number_format and "₹" in c.number_format:
                        c.number_format = "0.##"
                else:
                    c.value = nv
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def export_kind(content_type: str, disposition: str):
    """'csv' | 'xlsx' | None for a response."""
    ct, cd = (content_type or "").lower(), (disposition or "").lower()
    if "attachment" not in cd:
        return None
    m = re.search(r'filename\*?=(?:utf-8\'\')?"?([^";]+)', cd)
    name = m.group(1) if m else ""
    if "text/csv" in ct or name.endswith(".csv"):
        return "csv"
    if "spreadsheetml" in ct or name.endswith(".xlsx"):
        return "xlsx"
    return None


def normalize_export(kind: str, data: bytes) -> bytes:
    return normalize_csv(data) if kind == "csv" else normalize_xlsx(data)
