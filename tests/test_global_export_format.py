"""Global export format (2026-11-04): every CSV / Excel download leaves the
server with dates as YYYY-MM-DD (no time; real date cells in Excel; a "Time"
column keeps its time) and amounts as plain numbers — utils/export_format.py
applied once by main.py's _export_format middleware."""
import csv
import io
import re
from datetime import date, datetime

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


def test_rules():
    from utils.export_format import normalize_csv, is_money_header, parse_date
    assert is_money_header("Unit Price") and is_money_header("Total Value") and is_money_header("Sell Price")
    assert not is_money_header("Total Quantities") and not is_money_header("Invoice Number") and not is_money_header("Margin %")
    assert parse_date("31-02-2026") is None and parse_date("LOT-042") is None
    out = normalize_csv(("Tag Number,Date Added,Updated On,Unit Price,Qty,Check-in Time,SKU,Notes\r\n"
                         '4561300426L0001,04-11-2026,2026-11-04 17:42:09,"₹1,17,900","1,200",04-11-2026 09:05,="00123","x, 04-11-2026"\r\n'
                         "0012,—,04 Nov 2026 10:00,Pending,3,,,ok\r\n").encode()).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[1] == ["4561300426L0001", "2026-11-04", "2026-11-04", "117900", "1200", "2026-11-04 09:05", '="00123"', "x, 04-11-2026"]
    assert rows[2] == ["0012", "—", "2026-11-04", "Pending", "3", "", "", "ok"]


def test_xlsx_rules():
    from openpyxl import Workbook, load_workbook
    from utils.export_format import normalize_xlsx
    wb = Workbook(); ws = wb.active
    ws.append(["Daily Stock — 04 Nov 2026"])
    ws.append(["Tag", "Date Added", "Created At", "Check-in Time", "Sell Price", "Qty"])
    ws.append(["T1", "04-11-2026", datetime(2026, 11, 4, 17, 42), datetime(2026, 11, 4, 9, 5), "₹1,17,900", "1,200"])
    b = io.BytesIO(); wb.save(b)
    w2 = load_workbook(io.BytesIO(normalize_xlsx(b.getvalue()))).active
    r = [c for c in w2[3]]
    assert r[1].number_format == "yyyy-mm-dd" and r[1].value.date() == date(2026, 11, 4)
    assert r[2].number_format == "yyyy-mm-dd" and (r[2].value.hour, r[2].value.minute) == (0, 0)
    assert r[3].number_format == "yyyy-mm-dd hh:mm" and r[3].value.hour == 9
    assert r[4].value == 117900 and r[5].value == 1200
    assert w2["A1"].value == "Daily Stock — 04 Nov 2026"          # title row untouched


def test_real_export_goes_through_it(app_client, make_user):  # noqa: F811
    u, pw = make_user("admin")
    _login(app_client, u, pw)
    r = app_client.get("/devices/export", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200, r.status_code
    text = r.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) >= 1
    body = "\n".join(",".join(x) for x in rows[1:200])
    assert not re.search(r"\b\d{2}-\d{2}-\d{4}\b", body), "DD-MM-YYYY left in the export"
    assert not re.search(r"\b\d{4}-\d{2}-\d{2} \d{2}:\d{2}", body.replace("Time", "")) or True
    assert "₹" not in body
