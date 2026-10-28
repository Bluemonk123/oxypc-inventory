"""WorkID Status page — PNA (Part Not Available) column + export column.

DB-free: the enrichment is a pure helper (`_pna_fields`); the export is
exercised by stubbing the page handler it delegates to; the template is
rendered directly.
"""
import asyncio
import csv
import io
import types
import uuid
from datetime import datetime

import routers.workid_status as ws
from tests.workid_helpers import export_sheet


def _part(name, by="eng1", at=None):
    return types.SimpleNamespace(part_name=name, marked_by=by,
                                 marked_at=at or datetime(2026, 10, 1, 9, 30))


def _item(barcode, pna_parts=()):
    it = {
        "row_key": f"wo-{barcode}", "work_id": "WO1", "device_id": uuid.uuid4(),
        "barcode": barcode, "lot_number": "L1", "brand": "HP", "model": "840",
        "engineer": "Eng", "stage_label": "L1", "stage_value": "l1",
        "assigned_date": datetime(2026, 9, 1, 10, 0), "completed_at": None,
        "days": 3, "ongoing": True, "notes": "",
        "wo_status": "pending", "pna_history": [], "pna_history_text": "",
    }
    it.update(ws._pna_fields(pna_parts))
    return it


def test_pna_fields_empty():
    f = ws._pna_fields([])
    assert f == {"pna": False, "pna_parts": [], "pna_marked_by": "", "pna_marked_at": ""}


def test_pna_fields_parts():
    f = ws._pna_fields([
        _part("Keyboard", "a", datetime(2026, 10, 1, 9, 30)),
        _part("Battery", "a", datetime(2026, 10, 2, 11, 5)),
        _part("Screen", "b", datetime(2026, 9, 30, 8, 0)),
        _part("Fan", None, None),
    ])
    assert f["pna"] is True
    assert f["pna_parts"] == ["Keyboard", "Battery", "Screen", "Fan"]
    assert f["pna_marked_by"] == "a, b"
    assert f["pna_marked_at"] == "02-10-2026 11:05"


def _export_rows(items, monkeypatch):
    # Export delegates to the page builder (_workid_status_page, row_cap=None)
    # and returns an .xlsx workbook (2026-10-27); read its Tag Summary sheet.
    async def fake_page(**kwargs):
        return types.SimpleNamespace(context={"items": items})
    monkeypatch.setattr(ws, "_workid_status_page", fake_page)
    resp = asyncio.run(ws.workid_status_export(
        request=None, db=None, current_user=None, workid="", tag="", engineer="",
        completed_from="", completed_to="", cosmetic_stage="", exclude_admin="",
        show_reassigned="", scrap_type="none"))
    return [["" if v is None else v for v in r] for r in export_sheet(resp.body)]


def test_export_pna_column(monkeypatch):
    rows = _export_rows([_item("TAG-PNA", [_part("Keyboard")]), _item("TAG-NONE")], monkeypatch)
    assert rows[0][-2:] == ["PNA", "PNA History"]
    assert rows[0][:-2] == ["Tag Number", "Lot Number", "Make", "Model", "Engineer Name",
                            "Stage", "Movement Date", "Assigned Date", "Completed Date", "Status", "Scrap", "QC Result"]
    by_tag = {r[0]: r for r in rows[1:]}
    assert by_tag["TAG-PNA"][-2] == "Yes"
    # inactive parts are filtered out by active_pna_parts, so the tag gets no parts
    assert by_tag["TAG-NONE"][-2] == "No"


def test_page_template_shows_pna_badge():
    from templates_config import templates
    from models.user import UserRole
    tpl = templates.env.get_template("workid_status/list.html")
    user = types.SimpleNamespace(username="admin", role=UserRole.admin, full_name="Admin",
                                 is_active=True, id=1)
    items = [_item("TAG-PNA", [_part("Keyboard", "alice")]), _item("TAG-NONE")]
    html = tpl.render({
        "request": types.SimpleNamespace(url=types.SimpleNamespace(path="/workid-status"),
                                         query_params={}, session={}, cookies={}),
        "current_user": user,
        "items": items, "rows_json": ws._rows_json(items),
        "highlight": "", "is_admin": True, "engineers": [],
        "tile_counts": {"assigned_tags": 0, "completed_tags": 0, "ongoing_tags": 0,
                        "total_cust_return": 0, "total_scrap": 0, "total_pna": 1},
        "stage_tiles": {k: {"assigned": 0, "completed": 0, "scrap": 0} for k, *_ in ws.STAGE_GROUPS},
        "stage_groups": ws.STAGE_GROUPS,
        "cosmetic_stage_choices": [], "main_truncated": False, "main_total": 2, "main_cap": 100,
        "f_workid": "", "f_tag": "", "f_engineer": "", "f_completed_from": "",
        "f_completed_to": "", "f_cosmetic_stage": "", "f_exclude_admin": "",
        "f_show_reassigned": "", "f_scrap_type": "none",
    })
    assert '<th>PNA</th>' in html
    # Rows ship as JSON (2026-10-15); the PNA column's badge is drawn from
    # each row's p (has PNA) and pt (tooltip) fields.
    from tests.workid_helpers import page_rows
    rows = {r["b"]: r for r in page_rows(html)}
    assert rows["TAG-PNA"]["p"] is True
    assert rows["TAG-PNA"]["pt"] == "Parts: Keyboard, Marked by: alice, at: 01-10-2026 09:30"
    # exactly one PNA row (only the PNA tag)
    assert sum(1 for r in rows.values() if r["p"]) == 1
