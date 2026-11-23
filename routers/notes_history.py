"""Notes History (2026-11-23, Pankaj): every note, never overwritten.

  GET /notes-history/api      one record's notes (Device Detail and other pages' Notes History button)
  GET /notes-history          admin page: all notes, filtered by type / note / date / search
  GET /notes-history/export   the same rows as Excel
Rows come from notes_history (services/notes_history.py writes them on every save).
"""
import io
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user, require_roles
from database import get_db
from models.device import Device, STAGE_LABELS, DeviceStage
from models.notes_history import NotesHistory
from models.user import User, UserRole
from services.notes_history import RECORD_LABELS, RULES
from templates_config import templates

router = APIRouter(tags=["notes_history"])
admin_only = require_roles(UserRole.admin)
_PAGE_LIMIT = 2000


def _stage_label(v):
    try:
        return STAGE_LABELS.get(DeviceStage(v), v) if v else ""
    except ValueError:
        return v or ""


def _note_types():
    types = {"Final QC Fail Notes", "Pre-Sale QC Fail Notes"}
    for rule in RULES.values():
        types.update(rule[3].values())
    return sorted(types)


def _row(n: NotesHistory, barcode=None):
    return {"record_type": n.record_type, "record_label": RECORD_LABELS.get(n.record_type, n.record_type),
            "record_id": n.record_id, "barcode": barcode or "", "note_type": n.note_type,
            "note": n.note_text, "category": n.category or "", "stage": _stage_label(n.stage),
            "by": n.written_by or "", "at": n.written_at.strftime("%d-%m-%Y %H:%M") if n.written_at else "",
            "source": n.source or "", "backfilled": bool(n.backfilled)}


@router.get("/notes-history/api")
async def notes_history_api(record_type: str = "", record_id: str = "", barcode: str = "",
                            db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(get_current_user)):
    """A tag (barcode=, or record_type=device) gets every note about it: its
    own TRC notes plus returns / telecalling rows that name it."""
    q = select(NotesHistory)
    if barcode or record_type == "device":
        did = None
        if barcode:
            did = (await db.execute(select(Device.id).where(func.upper(Device.barcode) == barcode.strip().upper()))).scalar()
        else:
            did = record_id
        if not did:
            return JSONResponse({"ok": False, "error": "Tag not found"}, status_code=404)
        q = q.where(or_(NotesHistory.device_id == did,
                        (NotesHistory.record_type == "device") & (NotesHistory.record_id == str(did))))
    elif record_type and record_id:
        q = q.where(NotesHistory.record_type == record_type, NotesHistory.record_id == str(record_id))
    else:
        return JSONResponse({"ok": False, "error": "Say which record"}, status_code=400)
    rows = (await db.execute(q.order_by(NotesHistory.written_at.desc()).limit(_PAGE_LIMIT))).scalars().all()
    return JSONResponse({"ok": True, "rows": [_row(n) for n in rows]})


def _filtered(record_type, note_type, date_from, date_to, search):
    q = (select(NotesHistory, Device.barcode)
         .outerjoin(Device, Device.id == NotesHistory.device_id))
    if record_type:
        q = q.where(NotesHistory.record_type == record_type)
    if note_type:
        q = q.where(NotesHistory.note_type == note_type)
    if date_from:
        q = q.where(NotesHistory.written_at >= datetime.strptime(date_from, "%Y-%m-%d"))
    if date_to:
        q = q.where(NotesHistory.written_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))
    if search.strip():
        s = f"%{search.strip()}%"
        q = q.where(or_(NotesHistory.note_text.ilike(s), NotesHistory.source.ilike(s),
                        NotesHistory.written_by.ilike(s), Device.barcode.ilike(s), NotesHistory.category.ilike(s)))
    return q.order_by(NotesHistory.written_at.desc())


@router.get("/notes-history", response_class=HTMLResponse)
async def notes_history_page(request: Request, record_type: str = "", note_type: str = "",
                             date_from: str = "", date_to: str = "", search: str = "",
                             db: AsyncSession = Depends(get_db), current_user: User = Depends(admin_only)):
    q = _filtered(record_type, note_type, date_from, date_to, search)
    total = (await db.execute(select(func.count()).select_from(q.order_by(None).subquery()))).scalar() or 0
    rows = [_row(n, bc) for n, bc in (await db.execute(q.limit(_PAGE_LIMIT))).all()]
    return templates.TemplateResponse("admin/notes_history.html", {
        "request": request, "current_user": current_user, "rows": rows, "total": total,
        "limit": _PAGE_LIMIT, "record_types": RECORD_LABELS, "note_types": _note_types(),
        "f": {"record_type": record_type, "note_type": note_type, "date_from": date_from,
              "date_to": date_to, "search": search}})


@router.get("/notes-history/export")
async def notes_history_export(record_type: str = "", note_type: str = "", date_from: str = "",
                               date_to: str = "", search: str = "",
                               db: AsyncSession = Depends(get_db), current_user: User = Depends(admin_only)):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Notes History"
    head = ["Written At", "Record", "Tag Number", "Note Type", "Note", "Category", "Stage",
            "Written By", "Source", "Existing (copied)"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    res = await db.stream(_filtered(record_type, note_type, date_from, date_to, search))
    async for n, bc in res:
        r = _row(n, bc)
        ws.append([r["at"], r["record_label"], r["barcode"], r["note_type"], r["note"], r["category"],
                   r["stage"], r["by"], r["source"], "Yes" if r["backfilled"] else ""])
    for col, w in zip("ABCDEFGHIJ", (17, 18, 16, 30, 60, 14, 18, 20, 36, 10)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": 'attachment; filename="notes_history.xlsx"'})
