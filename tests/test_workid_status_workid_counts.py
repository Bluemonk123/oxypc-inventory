"""WorkID Status counts WorkIDs (2026-10-28, Pankaj): the tiles and the
export's Stage / Staff Summary count the same rows the table and the Tag
Summary sheet list, so they add up — Assigned = Completed + Ongoing + Scrap,
and each summary's Total = the Tag Summary row count. Ongoing is every open
WorkID (it was "in progress" only). One Cosmetic exit is one Pass / Fail."""
import io
import json
import pathlib
import re
import sys
from datetime import datetime, timedelta

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)


def test_cosmetic_exit_counted_once():
    from routers.workid_status import _cosmetic_exit_owners
    exit_at = datetime(2026, 10, 27, 12, 0)
    keep = _cosmetic_exit_owners([
        ("masking", "D1", exit_at, exit_at - timedelta(minutes=8)),
        ("cos_completed", "D1", exit_at, exit_at - timedelta(seconds=2)),
        ("open", "D1", exit_at, None),
        ("other_tag", "D2", exit_at, exit_at),
    ])
    assert keep == {"cos_completed", "other_tag"}


def test_stage_tiles_count_workids():
    from routers.workid_status import _stage_tile_counts_from_items
    items = [
        {"stage_value": "cleaning", "barcode": "T1", "wo_status": "completed", "qc_result": ""},
        {"stage_value": "putty", "barcode": "T1", "wo_status": "completed", "qc_result": ""},
        {"stage_value": "cosmetic_completed", "barcode": "T1", "wo_status": "completed", "qc_result": "pass"},
        {"stage_value": "l1", "barcode": "T2", "wo_status": "pending", "qc_result": ""},
        {"stage_value": "l3", "barcode": "T3", "wo_status": "completed", "scrap": "Normal Scrap", "qc_result": ""},
    ]
    t = _stage_tile_counts_from_items(items)
    assert t["cosmetic"]["assigned"] == 3 and t["cosmetic"]["completed"] == 3 and t["cosmetic"]["pass"] == 1
    assert t["l1l2"]["assigned"] == 1 and t["l1l2"]["completed"] == 0
    assert t["l3l4"]["scrap"] == 1 and t["l3l4"]["completed"] == 0


def test_tiles_and_export_add_up_to_rows(app_client, make_user):  # noqa: F811
    from openpyxl import load_workbook
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/workid-status").text
    rows = json.loads(re.search(r'id="workidRows">(.*?)</script>', html, re.S).group(1))

    def tile(key):
        return int(re.search(r'data-tile="%s".*?pm-tile-num" data-server="(\d+)"' % key, html, re.S).group(1))

    if "alert-info" not in html:        # not capped: tiles count the loaded rows
        assert tile("assigned") == len(rows)
        open_rows = sum(1 for r in rows if r["st"] not in ("completed", "reassigned"))
        assert tile("ongoing") == open_rows
        assert tile("completed") == sum(1 for r in rows if r["st"] == "completed" and not r["x"])
        scrap_rows = sum(1 for r in rows if r["x"] and r["st"] == "completed")
        reassigned = sum(1 for r in rows if r["st"] == "reassigned")
        assert tile("assigned") == tile("completed") + tile("ongoing") + scrap_rows + reassigned

    r = app_client.get("/workid-status/export")
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    n_tag_rows = wb["Tag Summary"].max_row - 1
    for name, col in (("Stage Summary", "Total WorkIDs"), ("Staff Summary", "Total WorkIDs")):
        ws = wb[name]
        hdr = [c.value for c in ws[1]]
        last = [c.value for c in ws[ws.max_row]]
        assert last[0] == "Total"
        assert last[hdr.index(col)] == n_tag_rows
        body = [[c.value for c in row] for row in ws.iter_rows(min_row=2, max_row=ws.max_row - 1)]
        assert sum(b[hdr.index(col)] for b in body) == n_tag_rows
        # Total = Open + Completed + Scrap + Reassigned
        parts = sum(last[hdr.index(k)] for k in ("Open WorkIDs", "Completed WorkIDs",
                                                 "Scrap WorkIDs", "Reassigned WorkIDs"))
        assert parts == n_tag_rows


def test_default_is_today_and_cleared_dates_mean_all(app_client, make_user):  # noqa: F811
    from utils.timezone import app_now
    username, password = make_user("admin")
    _login(app_client, username, password)
    today = app_now().date().isoformat()
    html = app_client.get("/workid-status").text
    assert f'name="work_from" value="{today}"' in html and f'name="work_to" value="{today}"' in html
    html = app_client.get("/workid-status?work_from=&work_to=").text      # cleared + Search
    assert 'name="work_from" value=""' in html
    html = app_client.get("/workid-status?highlight=NOPE").text              # a link to one WorkID
    assert 'name="work_from" value=""' in html


def test_repeat_tags_count(app_client, make_user):  # noqa: F811
    """Repeat Tags (2026-10-28): Tag Numbers on more than one row."""
    from collections import Counter
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/workid-status?work_from=&work_to=").text
    rep = int(re.search(r"var REPEAT_TAGS = (\d+);", html).group(1))
    assert "Repeat Tags - " in html and "wid-repeat-badge" in html
    if "alert-info" not in html:
        rows = json.loads(re.search(r'id="workidRows">(.*?)</script>', html, re.S).group(1))
        c = Counter(r["b"] for r in rows if r["b"] and r["b"] != "—")
        assert rep == sum(1 for n in c.values() if n > 1)
