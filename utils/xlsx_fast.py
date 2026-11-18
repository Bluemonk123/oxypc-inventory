"""Fast single-sheet .xlsx writer for big exports (2026-11-18).

openpyxl spends most of a 37k-row All Inventories export building one Python
object per cell (about 75% of the time). This writes the sheet XML directly
as text instead: same file features the export uses (bold coloured header,
freeze panes, column widths, auto-filter, real dates/numbers), several times
faster. Strings are written inline (t="inlineStr"), so there is no shared
strings table to build.

build_xlsx(sheet_name, header, header_styles, widths, rows, freeze="B2")
  header        list of column names
  header_styles list of (fill_hex, font_hex) per column
  widths        list of column widths
  rows          iterable of row lists (same length as header); values may be
                None/"" (empty cell), str, int, float, Decimal, bool,
                datetime.date or datetime.datetime
Returns the file as bytes.
"""
import datetime as _dt
import io
import re
import zipfile
from decimal import Decimal
from xml.sax.saxutils import escape as _esc

_EPOCH = _dt.datetime(1899, 12, 30)
# Characters XML 1.0 can't hold (openpyxl raises IllegalCharacterError on them).
_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def col_letter(i: int) -> str:
    """0-based column index → Excel letters (0 → A, 26 → AA)."""
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _text(v: str) -> str:
    return _esc(_BAD.sub("", v))


def build_xlsx(sheet_name, header, header_styles, widths, rows, freeze="B2") -> bytes:
    n = len(header)
    rows = rows if isinstance(rows, list) else list(rows)
    letters = [col_letter(i) for i in range(n)]

    # ── styles: 0 default, 1 datetime, 2 date, 3.. one per header colour pair ──
    pairs = []
    for p in header_styles:
        if p not in pairs:
            pairs.append(p)
    fills = "".join(
        f'<fill><patternFill patternType="solid"><fgColor rgb="FF{f}"/><bgColor indexed="64"/></patternFill></fill>'
        for f, _c in pairs)
    fonts = "".join(f'<font><b/><sz val="11"/><color rgb="FF{c}"/><name val="Calibri"/><family val="2"/></font>'
                    for _f, c in pairs)
    hdr_xfs = "".join(
        f'<xf numFmtId="0" fontId="{k + 1}" fillId="{k + 2}" borderId="0" xfId="0" applyFont="1" applyFill="1" '
        f'applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>' for k in range(len(pairs)))
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm"/>'
        '<numFmt numFmtId="165" formatCode="yyyy-mm-dd"/></numFmts>'
        f'<fonts count="{len(pairs) + 1}"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>{fonts}</fonts>'
        f'<fills count="{len(pairs) + 2}"><fill><patternFill patternType="none"/></fill>'
        f'<fill><patternFill patternType="gray125"/></fill>{fills}</fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        f'<cellXfs count="{len(pairs) + 3}">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        f'{hdr_xfs}</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>')
    hdr_style = [3 + pairs.index(p) for p in header_styles]

    # ── sheet ──
    out = io.StringIO()
    w = out.write
    w('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">')
    # Used range — readers (openpyxl read-only, Excel) size each row from it, so
    # rows ending in empty cells still read back at full width.
    w(f'<dimension ref="A1:{letters[-1]}{len(rows) + 1}"/>')
    w('<sheetViews><sheetView workbookViewId="0">')
    if freeze:
        m = re.match(r"([A-Z]+)(\d+)", freeze)
        xs = sum((ord(ch) - 64) * 26 ** k for k, ch in enumerate(reversed(m.group(1)))) - 1
        ys = int(m.group(2)) - 1
        attrs = (f' xSplit="{xs}"' if xs else "") + (f' ySplit="{ys}"' if ys else "")
        pane = "bottomRight" if xs and ys else ("bottomLeft" if ys else "topRight")
        w(f'<pane{attrs} topLeftCell="{freeze}" activePane="{pane}" state="frozen"/>'
          f'<selection pane="{pane}" activeCell="{freeze}" sqref="{freeze}"/>')
    w('</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/><cols>')
    for i, wd in enumerate(widths):
        w(f'<col min="{i + 1}" max="{i + 1}" width="{wd}" customWidth="1"/>')
    w('</cols><sheetData>')
    w('<row r="1" ht="30" customHeight="1">')
    for i, name in enumerate(header):
        w(f'<c r="{letters[i]}1" s="{hdr_style[i]}" t="inlineStr"><is><t>{_text(str(name))}</t></is></c>')
    w('</row>')

    r = 1
    for row in rows:
        r += 1
        rs = str(r)
        parts = [f'<row r="{rs}">']
        ap = parts.append
        for i, v in enumerate(row):
            if v is None or v == "":
                continue
            t = type(v)
            if t is str:
                ap(f'<c r="{letters[i]}{rs}" t="inlineStr"><is><t xml:space="preserve">{_text(v)}</t></is></c>')
            elif t is bool:
                ap(f'<c r="{letters[i]}{rs}" t="b"><v>{int(v)}</v></c>')
            elif t is int or t is float or t is Decimal:
                ap(f'<c r="{letters[i]}{rs}"><v>{float(v) if t is Decimal else v}</v></c>')
            elif t is _dt.datetime or isinstance(v, _dt.datetime):
                d = v.replace(tzinfo=None) - _EPOCH
                ap(f'<c r="{letters[i]}{rs}" s="1"><v>{d.days + (d.seconds + d.microseconds / 1e6) / 86400!r}</v></c>')
            elif isinstance(v, _dt.date):
                ap(f'<c r="{letters[i]}{rs}" s="2"><v>{(v - _EPOCH.date()).days}</v></c>')
            else:
                ap(f'<c r="{letters[i]}{rs}" t="inlineStr"><is><t xml:space="preserve">{_text(str(v))}</t></is></c>')
        ap('</row>')
        w("".join(parts))
    w('</sheetData>')
    w(f'<autoFilter ref="A1:{letters[-1]}{r}"/>')
    w('<pageMargins left="0.75" right="0.75" top="1" bottom="1" header="0.5" footer="0.5"/></worksheet>')

    sheet_xml = out.getvalue()
    sn = _esc(sheet_name, {'"': "&quot;"})
    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{sn}" sheetId="1" r:id="rId1"/></sheets>'
            '<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
            f"'{sn}'!$A$1:${letters[-1]}${r}</definedName></definedNames>"
            '</workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        "xl/styles.xml": styles,
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()
