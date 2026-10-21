"""
GRN Router — Goods Receipt Note
Records expected vs received quantity per lot and raises mismatch flags.
"""
import os
import hashlib
from decimal import Decimal
from templates_config import templates
from datetime import datetime
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, Request, HTTPException, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, update

from database import get_db
from models.user import User, UserRole
from models.lot import Lot
from models.master import EXTERNAL_PARTNER_TEST_LOT_PREFIX, EXTERNAL_PARTNER_TEST_GRN_SOURCE
from models.device import Device, DeviceStage, StageMovement, stage_sort_expr
from models.engines import AuditLog
from models.grn_import import GRNImport, GRNLot
from services.invoice_parser import extract_invoice_fields
from services.audit_engine import audit
from utils.master_data import entity_values
from services.location_defaults import ensure_stage_location
from config import UPLOADS_DIR
from auth.dependencies import get_current_user, require_roles, verify_csrf, require_additional_perm

router = APIRouter(prefix="/grn", tags=["grn"], dependencies=[Depends(verify_csrf)])
allowed = require_roles(UserRole.admin, UserRole.inventory_manager)

GRN_UPLOAD_DIR = os.path.join(UPLOADS_DIR, "grn")


async def _next_grn_number(db: AsyncSession) -> str:
    """Auto-generate next GRN number in format GRN-YYYYMMDD-NNNN (legacy per-lot)."""
    today = app_now().strftime("%Y%m%d")
    result = await db.execute(
        select(func.count(Lot.id)).where(
            Lot.grn_system_number.like(f"GRN-{today}-%")
        )
    )
    n = (result.scalar() or 0) + 1
    return f"GRN-{today}-{n:04d}"


async def _next_grn_12(db: AsyncSession) -> str:
    """Unique 12-digit GRN number for an invoice import."""
    base = (await db.execute(select(func.count(GRNImport.id)))).scalar() or 0
    n = base + 1
    for _ in range(10000):
        g = str(n).zfill(12)
        taken = (await db.execute(select(GRNImport.id).where(GRNImport.grn_number == g))).scalar_one_or_none()
        if not taken:
            return g
        n += 1
    return str(n).zfill(12)


async def _stocked_map(db: AsyncSession, grns) -> dict:
    """{grn_number: number of tag numbers wired to it}.

    Counted from Device.grn_number rather than stored on GRNImport so the figure
    can never drift out of step with the tags that were actually mapped.
    Trashed / deactivated tags are excluded — they are no longer stock.
    """
    numbers = [g.grn_number for g in grns if g.grn_number]
    if not numbers:
        return {}
    rows = (await db.execute(
        select(Device.grn_number, func.count(Device.id))
        .where(Device.grn_number.in_(numbers),
               Device.is_active == True, Device.is_trashed == False)
        .group_by(Device.grn_number)
    )).all()
    return {n: c for n, c in rows}


async def _as_is_lot_map(db: AsyncSession, grns) -> dict:
    """{grn_number: sub_lot_number} for GRNs whose mapped Tag Numbers already
    carry a Sub-Lot Number from the "Yes As-Is" modal — every Tag Number
    mapped to a GRN gets the same value in one write (grn_as_is_lot below),
    so the first non-null one found is representative."""
    numbers = [g.grn_number for g in grns if g.grn_number]
    if not numbers:
        return {}
    rows = (await db.execute(
        select(Device.grn_number, Device.sub_lot_number)
        .where(Device.grn_number.in_(numbers), Device.sub_lot_number.isnot(None),
               Device.is_active == True, Device.is_trashed == False)
    )).all()
    result = {}
    for grn_number, sub_lot in rows:
        result.setdefault(grn_number, sub_lot)
    return result


# ── GRN invoice import (the GRN nav page) ──────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def grn_import_list(request: Request, db: AsyncSession = Depends(get_db),
                          current_user: User = Depends(allowed),
                          error: str = "", success: str = ""):
    # GRN with Invoice page → invoice-source GRNs only (legacy NULL treated as
    # invoice). NULL-safe throughout: `source != X` alone evaluates to SQL
    # NULL (not TRUE) for a NULL source, which would silently drop those
    # legacy rows — each exclusion is paired with `.is_(None)` so a NULL
    # source is always treated as "not that value" rather than "unknown".
    rows = (await db.execute(
        select(GRNImport)
        .where(or_(GRNImport.source != "post_iqc", GRNImport.source.is_(None)),
               or_(GRNImport.source != EXTERNAL_PARTNER_TEST_GRN_SOURCE, GRNImport.source.is_(None)),
               GRNImport.is_deleted == False)
        .order_by(GRNImport.created_at.desc())
    )).scalars().all()
    return templates.TemplateResponse("grn/import.html", {
        "request": request, "grns": rows, "current_user": current_user,
        "stocked": await _stocked_map(db, rows),
        "error": error, "success": success,
    })


# ── GRN post IQC (item 14): same import/validate/edit, plus Map-to-Tag ─────────

def _pending_base_filters(device_type: str, pend_entity: str, pend_invoice: str, pend_po: str) -> list:
    """GRN Pending table's filter clauses (Device Type / Entity / Invoice / PO)
    — shared by /grn/post-iqc/data and its Export, so the two never disagree."""
    base_filters = [Device.current_stage == DeviceStage.iqc,
                    or_(Device.grn_number.is_(None), Device.grn_number == ""),
                    Device.is_active == True, Device.is_trashed == False]
    if device_type:
        base_filters.append(Device.device_type == device_type)
    # Entity multi-select (comma-separated; "Unassigned" = no entity) — 2026-10-15.
    ents = [e.strip() for e in (pend_entity or "").split(",") if e.strip()]
    if ents:
        named = [e for e in ents if e != "Unassigned"]
        ent_or = [Device.entity.in_(named)] if named else []
        if "Unassigned" in ents:
            ent_or.append(or_(Device.entity.is_(None), Device.entity == ""))
        base_filters.append(or_(*ent_or))
    # Matches the old client-side rule: with either term present, a row must
    # hit invoice OR po (not both) to survive — replicated exactly rather than
    # simplified to an AND, which would change what the filter selects.
    if pend_invoice or pend_po:
        ors = []
        if pend_invoice:
            ors.append(Device.invoice_number.ilike(f"%{pend_invoice}%"))
        if pend_po:
            ors.append(Device.po_number.ilike(f"%{pend_po}%"))
        base_filters.append(or_(*ors))

    return base_filters


def _pending_search_filters(search: str) -> list:
    """The table's own search box: comma-separated tags = exact tag match,
    otherwise a contains-match on tag / brand / model / device type."""
    search_filters = []
    if search and "," in search:
        # Comma-separated Tag Numbers (scanned or pasted) — exact tag match,
        # so every listed tag shows and gets auto-selected (2026-10-15).
        tags = [t.strip().upper() for t in search.split(",") if t.strip()]
        if tags:
            search_filters.append(func.upper(Device.barcode).in_(tags))
    elif search:
        like = f"%{search}%"
        search_filters.append(or_(
            Device.barcode.ilike(like), Device.brand.ilike(like),
            Device.model.ilike(like), Device.device_type.ilike(like),
        ))
    return search_filters


@router.get("/post-iqc/data")
async def grn_post_iqc_data(
    request: Request,
    draw: int = 1, start: int = 0, length: int = 25,
    device_type: str = "", pend_invoice: str = "", pend_po: str = "", pend_entity: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """DataTables server-side feed for the GRN in TRC pending-devices table.

    'pending' is every IQC-stage device with no GRN number yet — effectively
    the whole IQC backlog (~3,900 devices with no filter). Rendering all of
    them into the HTML on every load froze the page; rows now come a page at
    a time. Invoice/PO are still hidden via DataTables' own columnDefs
    (visible:false), not a d-none class, so header and cell hide together.
    """
    from sqlalchemy import desc as _desc, asc as _asc
    from html import escape

    base_filters = _pending_base_filters(device_type, pend_entity, pend_invoice, pend_po)
    count_q = select(func.count()).select_from(Device).where(*base_filters)
    total = (await db.execute(count_q)).scalar() or 0

    search = (request.query_params.get("search[value]") or "").strip()
    search_filters = _pending_search_filters(search)
    # Identical to `total` when no search term is present, and DataTables asks
    # on every draw — skip the duplicate round-trip.
    filtered = total if not search_filters else (
        (await db.execute(count_q.where(*search_filters))).scalar() or 0)

    # 7 = Invoice Number (shown, before Stage — 2026-10-15); 8 = Stage; 9 = PO (hidden).
    col_map = {1: Device.barcode, 2: Device.brand, 3: Device.model,
               4: Device.device_type, 5: Device.ram_gb, 6: Device.storage_gb,
               7: Device.invoice_number, 8: stage_sort_expr(Device.current_stage),
               9: Device.po_number}
    try:
        order_col = int(request.query_params.get("order[0][column]", 0))
    except ValueError:
        order_col = 0
    order_dir = request.query_params.get("order[0][dir]", "desc")
    sort_expr = col_map.get(order_col, Device.created_at)
    order_by = _asc(sort_expr) if order_dir == "asc" else _desc(sort_expr)

    rows = (await db.execute(
        select(Device).where(*base_filters, *search_filters)
        .order_by(order_by, Device.barcode)
        # length -1 = "all rows" (sent while a search is active, so every match
        # can be auto-selected and mapped at once — no cap, 2026-10-15).
        .offset(max(0, start) if length != -1 else 0).limit(None if length == -1 else min(max(1, length), 5000))
    )).scalars().all()

    def esc(v):
        return escape(str(v)) if v is not None else ""

    data = []
    for d in rows:
        data.append([
            f'<input type="checkbox" class="pendChk" name="device_ids" value="{d.id}" data-barcode="{esc(d.barcode)}">',
            # Entity under the Tag Number — same style as All Inventory.
            (f'<a href="/devices/{esc(d.barcode)}" class="font-monospace text-decoration-none">{esc(d.barcode)}</a>'
             f'<div style="color:#999999;font-size:12px;">{esc(d.entity) if d.entity else "—"}</div>'),
            esc(d.brand or "—"), esc(d.model or "—"), esc(d.device_type or "—"),
            f"{d.ram_gb} GB" if d.ram_gb else "—",
            f"{d.storage_gb} GB" if d.storage_gb else "—",
            esc(d.invoice_number or "—"),
            f'<span class="badge bg-{d.stage_color}">{esc(d.stage_label)}</span>',
            esc(d.po_number or ""),
        ])

    return {"draw": draw, "recordsTotal": total, "recordsFiltered": filtered, "data": data}


@router.post("/post-iqc/export")
async def grn_post_iqc_export(
    device_ids: str = Form(""),
    device_type: str = Form(""), pend_invoice: str = Form(""), pend_po: str = Form(""),
    pend_entity: str = Form(""), search: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """GRN Pending table's Export button (2026-10-15): the ticked rows when
    any are ticked (device_ids), otherwise every row matching the table's
    current filters + search — no page or row cap."""
    import csv, io, uuid as _u
    from fastapi.responses import StreamingResponse
    ids = []
    for x in (device_ids or "").split(","):
        try:
            ids.append(_u.UUID(x.strip()))
        except ValueError:
            pass
    q = select(Device)
    if ids:
        q = q.where(Device.id.in_(ids))
    else:
        q = q.where(*_pending_base_filters(device_type, pend_entity, pend_invoice, pend_po),
                    *_pending_search_filters((search or "").strip()))
    rows = (await db.execute(q.order_by(Device.barcode))).scalars().all()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Tag Number", "Entity", "Brand", "Model", "Device Type", "RAM", "Storage",
                "Invoice Number", "PO Number", "Stage"])
    for d in rows:
        w.writerow([d.barcode, d.entity or "", d.brand or "", d.model or "", d.device_type or "",
                    f"{d.ram_gb} GB" if d.ram_gb else "", f"{d.storage_gb} GB" if d.storage_gb else "",
                    d.invoice_number or "", d.po_number or "", d.stage_label])
    fname = f"grn_pending_{app_now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(content=out.getvalue().encode("utf-8-sig"), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={fname}"})


@router.get("/post-iqc", response_class=HTMLResponse)
async def grn_post_iqc(request: Request, db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(allowed),
                       error: str = "", success: str = "", highlight_tag: str = ""):
    grns = (await db.execute(
        select(GRNImport).where(GRNImport.source == "post_iqc",
                                GRNImport.is_deleted == False)
        .order_by(GRNImport.created_at.desc())
    )).scalars().all()
    # Pending = devices currently in IQC stage whose GRN field is still empty.
    # Rows for that table now come from /grn/post-iqc/data (DataTables
    # server-side) — only the count is needed here, for the header badge.
    pending_count = (await db.execute(
        select(func.count()).select_from(Device).where(
            Device.current_stage == DeviceStage.iqc,
            or_(Device.grn_number.is_(None), Device.grn_number == ""),
            Device.is_active == True, Device.is_trashed == False,
        )
    )).scalar() or 0

    # Lot Number column — every Lot mapped to each GRN: grn_lots links (a lot
    # can belong to several GRNs since 2026-10-21) plus the legacy links.
    lots_by_grn = await _lots_by_grn(db, grns)

    # JSON-serializable form for the "N Lots" popup (id + lot_number only —
    # Lot itself isn't JSON-safe to dump straight into a data attribute).
    lots_json = {
        grn_number: [{"id": str(l.id), "lot_number": l.lot_number} for l in lots]
        for grn_number, lots in lots_by_grn.items()
    }

    all_users = (await db.execute(
        select(User).where(User.status == True).order_by(User.full_name)  # noqa: E712
    )).scalars().all()

    # Added By column: GRNImport.created_by is a username — show the user's
    # display name (inactive users included, so old GRNs still resolve).
    # Matched case-insensitively (older GRNs saved the username in another case).
    creators = {g.created_by for g in grns if g.created_by}
    user_names = {}
    if creators:
        by_lower = {}
        for uname, fname in (await db.execute(
            select(User.username, User.full_name)
            .where(func.lower(User.username).in_([c.lower() for c in creators]))
        )).all():
            by_lower[uname.lower()] = fname or uname
        user_names = {c: by_lower[c.lower()] for c in creators if c.lower() in by_lower}

    return templates.TemplateResponse("grn/post_iqc.html", {
        "request": request, "grns": grns, "pending_count": pending_count,
        "user_names": user_names,
        "stocked": await _stocked_map(db, grns),
        "as_is_lot": await _as_is_lot_map(db, grns),
        "all_users": all_users,
        "current_user": current_user, "error": error, "success": success,
        "highlight_tag": highlight_tag,
        "device_type_options": ["Laptop", "Desktop", "AIO", "Workstation", "Mini PC", "Server", "Tablet"],
        "entity_options": await entity_values(db),
        "lots_by_grn": lots_by_grn,
        "lots_json": lots_json,
    })


@router.post("/map")
async def grn_map(request: Request, grn_id: str = Form(...),
                  device_ids: list[str] = Form(default=[]),
                  db: AsyncSession = Depends(get_db),
                  current_user: User = Depends(allowed)):
    import uuid as _u
    from urllib.parse import quote
    try:
        gid = _u.UUID(grn_id)
    except ValueError:
        raise HTTPException(404)
    g = (await db.execute(select(GRNImport).where(GRNImport.id == gid))).scalar_one_or_none()
    if not g:
        raise HTTPException(404, "GRN not found")
    valid_ids = []
    for d in device_ids:
        try:
            valid_ids.append(_u.UUID(d))
        except (ValueError, AttributeError):
            pass
    if not valid_ids:
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"ok": False, "message": "No tag numbers selected"},
                                status_code=400)
        return RedirectResponse(url="/grn/post-iqc?error=No+tag+numbers+selected", status_code=302)
    devices = (await db.execute(
        select(Device).where(Device.id.in_(valid_ids))
    )).scalars().all()
    now = app_now()
    moved = 0
    for d in devices:
        d.grn_number = g.grn_number
        # Default new/legacy tags with no Entity yet to OxyPC Computers' own
        # stock — but never overwrite one that's already set. A tag re-
        # entering IQC after Entity Movement's "SOLD TO" change (which
        # deliberately set it to a different entity and cleared its GRN
        # number so it must be re-mapped here) would otherwise have that
        # change silently reverted the moment it's mapped to a GRN again.
        if not d.entity:
            d.entity = "OxyPC Computers"
        # After GRN mapping the tag leaves IQC and enters the Stock Inward table
        if d.current_stage == DeviceStage.iqc:
            prev = (await db.execute(
                select(StageMovement).where(
                    StageMovement.device_id == d.id,
                    StageMovement.to_stage == d.current_stage,
                    StageMovement.exited_at.is_(None),
                ).order_by(StageMovement.moved_at.desc())
            )).scalars().first()
            if prev:
                prev.exited_at = now
            db.add(StageMovement(
                device_id=d.id, from_stage=DeviceStage.iqc, to_stage=DeviceStage.stock_in,
                moved_by=current_user.username,
                notes=f"GRN {g.grn_number} mapped — moved to Stock Inward"))
            d.current_stage = DeviceStage.stock_in
            d.updated_at = now
            await ensure_stage_location(db, d, DeviceStage.stock_in, current_user)
            moved += 1
    await audit(db, user=current_user, action="GRN_MAPPED",
                table_name="devices", record_id=str(gid),
                new_value={"grn_number": g.grn_number, "tags": len(valid_ids),
                           "moved_to_stock_in": moved},
                request=request)
    await db.commit()
    message = (f"Mapped GRN {g.grn_number} to {len(valid_ids)} tag(s) "
               f"\u2014 {moved} moved to Stock Inward")
    # Answer JSON when the page posts this over fetch. A redirect reloaded the
    # whole screen and threw away the invoice / PO / device-type filters the
    # operator had set to find those tags in the first place, so mapping a
    # second batch meant setting them all up again.
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True, "message": message,
                             "grn_number": g.grn_number,
                             "mapped": len(valid_ids), "moved": moved})
    return RedirectResponse(
        url=f"/grn/post-iqc?success={quote(message)}", status_code=302)


# ── GRN Records ("GRN Board") — 3 tabs, rows served via /records/data ─────────
# Rendering every matching device straight into the HTML (as this page used to)
# froze the browser once the table reached a few thousand rows — same root
# cause already fixed on /grn/post-iqc. Only tab counts (for the nav badges)
# are computed here now; rows come a page at a time from /records/data below.

def _records_base_filters(date_from: str, date_to: str):
    from utils.date_filter import apply_date_range
    filters = [Device.is_active == True, Device.is_trashed == False]
    apply_date_range(filters, Device.created_at, date_from, date_to)
    return filters


@router.get("/records", response_class=HTMLResponse)
async def grn_records(request: Request, db: AsyncSession = Depends(get_db),
                      current_user: User = Depends(allowed),
                      date_from: str = Query(default=""),
                      date_to: str = Query(default="")):
    filters = _records_base_filters(date_from, date_to)

    async def _count(*extra):
        q = select(func.count()).select_from(Device).where(*filters, *extra)
        return (await db.execute(q)).scalar() or 0

    assigned_count = await _count(Device.grn_number.isnot(None), Device.grn_number != "")
    not_mapped_count = await _count(or_(Device.grn_number.is_(None), Device.grn_number == ""))
    # Pending for TRC — Deshwal-entity tags still sitting at Stage IQC
    pending_trc_count = await _count(Device.entity == "Deshwal", Device.current_stage == DeviceStage.iqc)

    return templates.TemplateResponse("grn/records.html", {
        "request": request,
        "assigned_count": assigned_count, "not_mapped_count": not_mapped_count,
        "pending_trc_count": pending_trc_count,
        "current_user": current_user,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.get("/records/data")
async def grn_records_data(
    request: Request,
    tab: str = Query(...),  # assigned | not_mapped | pending_trc
    draw: int = 1, start: int = 0, length: int = 25,
    date_from: str = Query(default=""), date_to: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """DataTables server-side feed for the GRN Board's 3 tabs."""
    from html import escape

    filters = _records_base_filters(date_from, date_to)
    show_grn = True
    if tab == "assigned":
        filters += [Device.grn_number.isnot(None), Device.grn_number != ""]
    elif tab == "pending_trc":
        filters += [Device.entity == "Deshwal", Device.current_stage == DeviceStage.iqc]
    else:
        tab = "not_mapped"
        filters += [or_(Device.grn_number.is_(None), Device.grn_number == "")]
        show_grn = False

    count_q = select(func.count()).select_from(Device).where(*filters)
    total = (await db.execute(count_q)).scalar() or 0

    search = (request.query_params.get("search[value]") or "").strip()
    search_filters = []
    if search:
        like = f"%{search}%"
        search_filters.append(or_(
            Device.barcode.ilike(like), Device.brand.ilike(like), Device.model.ilike(like),
        ))
    filtered = (await db.execute(count_q.where(*search_filters))).scalar() or 0

    rows = (await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id, isouter=True)
        .where(*filters, *search_filters)
        .order_by(Device.updated_at.desc())
        .offset(max(0, start)).limit(min(max(1, length), 5000))
    )).all()

    def esc(v):
        return escape(str(v)) if v is not None else ""

    def grade_class(gv):
        return ('success' if gv == 'A' else 'warning text-dark' if gv == 'B'
                else 'danger' if gv in ('C', 'D', 'scrap') else 'secondary')

    data = []
    for d, lot_number in rows:
        gv = d.grade.value if d.grade else ""
        row = []
        if tab == "pending_trc":
            row.append(f'<input type="checkbox" name="device_ids" value="{d.id}" '
                        f'class="form-check-input pending-trc-cb" data-barcode="{esc(d.barcode)}">')
        row += [
            f'<a href="/devices/{esc(d.barcode)}" class="font-monospace text-decoration-none">{esc(d.barcode)}</a>',
            f'<span class="badge bg-info text-dark">{esc(lot_number)}</span>' if lot_number else '—',
            esc(d.brand or '—'), esc(d.model or '—'),
            f'<span class="badge bg-{grade_class(gv)}">{esc(gv or "—")}</span>',
            f'<span class="badge bg-{d.stage_color}">{esc(d.stage_label)}</span>',
        ]
        if show_grn:
            row.append(esc(d.grn_number))
        row.append(d.updated_at.strftime('%d %b %Y') if d.updated_at else '—')
        data.append(row)

    return {"draw": draw, "recordsTotal": total, "recordsFiltered": filtered, "data": data}


@router.post("/records/bulk-move-to-trc")
async def grn_bulk_move_to_trc(
    request: Request,
    device_ids: list[str] = Form(default=[]),
    dest: str = Form(...),  # "selling" | "sold_oxypc"
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Pending for TRC tab — bulk-move selected tag numbers per the chosen
    destination:
      - selling: back to Stage IQC with GRN cleared (re-enters the "GRN in
        TRC" / post-IQC pending queue, awaiting a fresh GRN before sale).
      - sold_oxypc: into Stock Inward (Inventory Manager), GRN value left as-is.
    """
    import uuid as _u
    from services.control_engine import validate_transition
    if dest not in ("selling", "sold_oxypc"):
        return RedirectResponse(url="/grn/records?error=Choose+a+destination", status_code=302)

    valid_ids = []
    for d in device_ids:
        try:
            valid_ids.append(_u.UUID(d))
        except (ValueError, AttributeError):
            pass
    if not valid_ids:
        return RedirectResponse(url="/grn/records?error=No+tag+numbers+selected", status_code=302)

    target_stage = DeviceStage.iqc if dest == "selling" else DeviceStage.stock_in
    label = "Sending for Selling" if dest == "selling" else "Sold to OxyPC Computers"

    devices = (await db.execute(select(Device).where(Device.id.in_(valid_ids)))).scalars().all()
    is_admin = current_user.role.value == "admin"
    now = app_now()
    moved = 0
    skipped = []
    for d in devices:
        try:
            await validate_transition(d, target_stage, db, override_admin=is_admin)
        except HTTPException:
            skipped.append(d.barcode)
            continue
        prev = d.current_stage
        d.current_stage = target_stage
        d.updated_at = now
        if dest == "selling":
            d.grn_number = None
        db.add(StageMovement(device_id=d.id, from_stage=prev, to_stage=target_stage,
                              moved_by=current_user.username,
                              notes=f"GRN Records — Pending for TRC bulk move ({label})"))
        moved += 1

    await audit(db, user=current_user, action="GRN_BULK_MOVE_TO_TRC",
                table_name="devices", record_id=None,
                new_value={"moved": moved, "skipped": len(skipped), "dest": dest}, request=request)
    await db.commit()
    msg = f"{moved}+tag(s)+moved+%E2%80%94+{label.replace(' ', '+')}"
    if skipped:
        import urllib.parse
        msg += f"&error={urllib.parse.quote(f'{len(skipped)} tag(s) could not move (not an allowed transition): ' + ', '.join(skipped[:10]))}"
    return RedirectResponse(url=f"/grn/records?success={msg}", status_code=302)


@router.post("/upload")
async def grn_upload(
    request: Request,
    invoice: UploadFile = File(...),
    source: str = Form("invoice"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    base = "/grn/post-iqc" if source == "post_iqc" else "/grn"
    data = await invoice.read()
    if not data:
        return RedirectResponse(url=f"{base}?error=Empty+file", status_code=302)
    # file_hash is still computed and stored on GRNImport for audit/traceability,
    # but no longer used to block the upload — the same invoice file can be
    # uploaded any number of times to create separate GRNs (e.g. a multi-lot
    # invoice split across several GRN entries).
    file_hash = hashlib.sha256(data).hexdigest()
    from urllib.parse import quote

    # Persist file
    os.makedirs(GRN_UPLOAD_DIR, exist_ok=True)
    grn_number = await _next_grn_12(db)
    safe_name = (invoice.filename or "invoice.pdf").replace("/", "_").replace("\\", "_")
    stored = f"{grn_number}_{safe_name}"
    path = os.path.join(GRN_UPLOAD_DIR, stored)
    with open(path, "wb") as f:
        f.write(data)

    # Best-effort extract
    fields = extract_invoice_fields(path)

    # Duplicate by invoice number (the real business key) — only when one was
    # extracted. Anchored on a non-empty invoice number so a sparse/partial parse
    # (e.g. only a vendor name) never false-positives as a duplicate.
    inv_no = (fields.get("invoice_number") or "").strip()
    if inv_no:
        same_grn = (await db.execute(
            select(GRNImport.grn_number).where(GRNImport.invoice_number == inv_no,
                                               GRNImport.is_deleted == False)
        )).scalar_one_or_none()
        if same_grn:
            try:
                os.remove(path)
            except OSError:
                pass
            return RedirectResponse(
                url=f"{base}?error=" + quote(
                    f"Invoice number {inv_no} already exists as GRN {same_grn}."),
                status_code=302)

    db.add(GRNImport(
        grn_number=grn_number,
        lot_number=fields.get("lot_number"),
        invoice_number=fields["invoice_number"], invoice_date=fields["invoice_date"],
        sender_name=fields["sender_name"], quantity=fields["quantity"], amount=fields["amount"],
        file_name=safe_name, file_path=path, file_hash=file_hash,
        source=("post_iqc" if source == "post_iqc" else "invoice"),
        created_by=current_user.username,
    ))
    await db.commit()
    return RedirectResponse(url=f"{base}?success=GRN+{grn_number}+created", status_code=302)


@router.get("/download/{grn_id}")
async def grn_download(grn_id: str, db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(allowed)):
    try:
        import uuid as _u
        gid = _u.UUID(grn_id)
    except ValueError:
        raise HTTPException(404)
    g = (await db.execute(select(GRNImport).where(GRNImport.id == gid))).scalar_one_or_none()
    if not g or not g.file_path:
        raise HTTPException(404, "File not found")
    # file_path was stored as an ABSOLUTE path at upload time. That path is
    # only valid on the server that actually wrote the bytes — records synced
    # in from another environment (e.g. the pre-cutover Railway deployment,
    # whose container root was "/app") carry a path that never existed on
    # this box. Re-resolve by basename under this server's own GRN upload
    # dir before giving up, so any file whose bytes genuinely are present
    # here (including ones this server itself wrote) still downloads.
    resolved = g.file_path
    if not os.path.exists(resolved):
        candidate = os.path.join(GRN_UPLOAD_DIR, os.path.basename(g.file_path))
        if os.path.exists(candidate):
            resolved = candidate
        else:
            raise HTTPException(
                404,
                "File not found on this server — it may have been uploaded to a "
                "previous deployment whose files were never migrated here.")
    return FileResponse(resolved, filename=g.file_name or "invoice.pdf",
                        media_type="application/pdf")


@router.post("/{grn_id}/validate")
async def grn_validate(
    grn_id: str, request: Request,
    received_qty: str = Form(""), grn_number: str = Form(""), notes: str = Form(""),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed),
):
    """Validate GRN (from the GRN Import page) — marks the GRN as validated."""
    try:
        import uuid as _u
        gid = _u.UUID(grn_id)
    except ValueError:
        raise HTTPException(404)
    g = (await db.execute(select(GRNImport).where(GRNImport.id == gid))).scalar_one_or_none()
    if not g:
        raise HTTPException(404, "GRN not found")
    g.validated = True
    # Persist what the operator entered — previously accepted then discarded,
    # which meant discrepancy notes at the Plant GRN stage were silently lost.
    try:
        g.received_qty = int(received_qty) if str(received_qty).strip() else None
    except (ValueError, TypeError):
        g.received_qty = None
    g.validation_ref = (grn_number or "").strip()[:100] or None
    g.validation_notes = (notes or "").strip()[:500] or None

    # Auto-map: validating a Plant GRN now maps the linked lot's IQC-stage
    # devices to Stock Inward automatically — previously the operator had to
    # go to GRN post IQC and run "Map this GRN" by hand for every lot.
    auto_mapped = 0
    if g.lot_number:
        lot = (await db.execute(
            select(Lot).where(Lot.lot_number == g.lot_number)
        )).scalar_one_or_none()
        if lot:
            devices = (await db.execute(
                select(Device).where(Device.lot_id == lot.id,
                                     Device.current_stage == DeviceStage.iqc,
                                     Device.is_active == True)
            )).scalars().all()
            now = app_now()
            for d in devices:
                d.grn_number = g.grn_number
                prev = (await db.execute(
                    select(StageMovement).where(
                        StageMovement.device_id == d.id,
                        StageMovement.to_stage == d.current_stage,
                        StageMovement.exited_at.is_(None),
                    ).order_by(StageMovement.moved_at.desc())
                )).scalars().first()
                if prev:
                    prev.exited_at = now
                db.add(StageMovement(
                    device_id=d.id, from_stage=DeviceStage.iqc,
                    to_stage=DeviceStage.stock_in, moved_by=current_user.username,
                    notes=f"GRN {g.grn_number} validated — auto-mapped to Stock Inward"))
                d.current_stage = DeviceStage.stock_in
                d.updated_at = now
                await ensure_stage_location(db, d, DeviceStage.stock_in, current_user)
                auto_mapped += 1
    await db.commit()
    return JSONResponse({"ok": True, "grn_number": g.grn_number, "auto_mapped": auto_mapped})


@router.post("/{grn_id}/delete")
async def grn_delete(
    grn_id: str, request: Request,
    source: str = Form("invoice"),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed),
):
    """Soft-delete a GRN (hidden from tables, file + row kept for audit/compliance)."""
    try:
        import uuid as _u
        gid = _u.UUID(grn_id)
    except ValueError:
        raise HTTPException(404)
    g = (await db.execute(select(GRNImport).where(GRNImport.id == gid))).scalar_one_or_none()
    if not g:
        raise HTTPException(404, "GRN not found")
    # Redirect target comes from the GRN's OWN stored source, not the form's
    # hidden field — robust regardless of what the calling template passes,
    # and correctly routes a test GRN back to Manage Lots even though its
    # delete form (like every other page's) just carries a generic hidden
    # source input.
    base = ("/trade-partner/manage-lots" if g.source == EXTERNAL_PARTNER_TEST_GRN_SOURCE
            else "/grn/post-iqc" if source == "post_iqc" else "/grn")
    g.is_deleted = True
    g.deleted_at = app_now()
    await audit(db, user=current_user, action="GRN_DELETED",
                table_name="grn_imports", record_id=str(gid),
                old_value={"grn_number": g.grn_number, "invoice_number": g.invoice_number},
                request=request)
    await db.commit()
    return RedirectResponse(url=f"{base}?success=GRN+{g.grn_number}+deleted", status_code=302)


def _split_lot_numbers(raw) -> list[str]:
    """'A, B ,A' -> ['A', 'B'] — comma/newline separated, trimmed, de-duplicated
    case-insensitively, first spelling kept."""
    parts = raw if isinstance(raw, list) else [raw or ""]
    out, seen = [], set()
    for chunk in parts:
        for n in str(chunk or "").replace("\n", ",").split(","):
            n = n.strip()
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
    return out


async def _map_lots_to_grn(db: AsyncSession, g: GRNImport, numbers: list[str],
                           username: str) -> tuple[list[str], list[str], list[str]]:
    """Flow 1 (2026-10-21): map each Lot Number onto GRN `g` via grn_lots.

    Existing lot -> just linked (a lot can belong to many GRNs — its tags can
    be split across invoices — so its fields are NOT copied over this GRN's).
    Missing lot -> created from this GRN's details, then linked.
    A trashed lot with that number is refused (its number is still taken).
    Returns (linked_existing, created, refused). Caller commits."""
    linked, created, refused = [], [], []
    have = set((await db.execute(
        select(GRNLot.lot_id).where(GRNLot.grn_id == g.id))).scalars().all())
    for number in numbers:
        lot = (await db.execute(
            select(Lot).where(func.lower(Lot.lot_number) == number.lower())
        )).scalars().first()
        if lot is not None and lot.is_trashed:
            refused.append(number)
            continue
        if lot is None:
            lot = Lot(
                lot_number=number,
                supplier_name=g.sender_name or "",
                vendor_name=g.sender_name,
                invoice_no=g.invoice_number,
                invoice_value=g.amount,
                buying_price=g.amount or Decimal("0"),
                qty=g.quantity or 1,
                purchase_date=(datetime.combine(g.purchase_date, datetime.min.time())
                               if g.purchase_date else app_now()),
                grn_date=(datetime.combine(g.grn_date, datetime.min.time()) if g.grn_date else None),
                po_number=g.po_number,
                vehicle_number=g.vehicle_number,
                e_way_bill=g.e_way_bill,
                notes=g.notes,
                # Older readers (Product IQC Lot tab, Edit Lot) still read this.
                grn_system_number=g.grn_number,
                is_restricted=number.upper().startswith(EXTERNAL_PARTNER_TEST_LOT_PREFIX.upper()),
                created_by=username,
            )
            db.add(lot)
            await db.flush()
            created.append(lot.lot_number)
        else:
            if not lot.grn_system_number:
                lot.grn_system_number = g.grn_number
            if number.upper().startswith(EXTERNAL_PARTNER_TEST_LOT_PREFIX.upper()):
                lot.is_restricted = True
            linked.append(lot.lot_number)
        if lot.id not in have:
            db.add(GRNLot(grn_id=g.id, lot_id=lot.id, created_by=username))
            have.add(lot.id)
        # Legacy single-lot columns: keep pointing at the first mapped lot.
        if g.lot_id is None:
            g.lot_id = lot.id
            g.lot_number = lot.lot_number[:60]
    return linked, created, refused


async def _lots_by_grn(db: AsyncSession, grns) -> dict:
    """{grn_number: [Lot, ...]} — every lot mapped to each GRN: grn_lots links
    (source of truth since 2026-10-21) plus the two legacy links (a lot's own
    grn_system_number, and grn_imports.lot_id) so older GRNs still show."""
    by_grn: dict[str, list] = {}
    seen: dict[str, set] = {}

    def _add(grn_number, lot):
        if lot.id in seen.setdefault(grn_number, set()):
            return
        seen[grn_number].add(lot.id)
        by_grn.setdefault(grn_number, []).append(lot)

    grn_by_id = {g.id: g.grn_number for g in grns if g.grn_number}
    if grn_by_id:
        for grn_id, lot in (await db.execute(
            select(GRNLot.grn_id, Lot).join(Lot, Lot.id == GRNLot.lot_id)
            .where(GRNLot.grn_id.in_(list(grn_by_id)))
        )).all():
            _add(grn_by_id[grn_id], lot)
        for lot in (await db.execute(
            select(Lot).where(Lot.grn_system_number.in_(list(grn_by_id.values())))
        )).scalars().all():
            _add(lot.grn_system_number, lot)
        fk = {g.lot_id for g in grns if g.lot_id}
        if fk:
            fk_lots = {l.id: l for l in (await db.execute(
                select(Lot).where(Lot.id.in_(fk)))).scalars().all()}
            for g in grns:
                if g.lot_id in fk_lots and g.grn_number:
                    _add(g.grn_number, fk_lots[g.lot_id])
    for lots in by_grn.values():
        lots.sort(key=lambda l: l.lot_number or "")
    return by_grn


@router.post("/create-manual")
async def grn_create_manual(
    request: Request,
    sender_name: str = Form(""), invoice_number: str = Form(""),
    invoice_date: str = Form(""), purchase_date: str = Form(""),
    grn_date: str = Form(""), amount: str = Form(""), quantity: str = Form(""),
    po_number: str = Form(""), vehicle_number: str = Form(""),
    e_way_bill: str = Form(""), notes: str = Form(""),
    source: str = Form("post_iqc"),
    # Flow 1 (2026-10-21): the entity these goods belong to, and the Lot
    # Number(s) — single or comma-separated — mapped onto this GRN on save.
    entity: str = Form(""), lot_numbers: str = Form(""),
    # Optional, unlike the upload path's File(...). An empty file input still
    # posts an UploadFile with a blank filename, so presence is decided by
    # reading the bytes, not by the object being non-None.
    invoice: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed),
    _perm: User = Depends(require_additional_perm("add_grn")),
):
    """Create a GRN by hand, with an optional invoice PDF.

    Covers goods that arrive without an invoice, or whose invoice follows
    later — the upload path cannot express that, since it requires a file.
    When a PDF is attached here it is stored and served like an uploaded one,
    but deliberately not parsed: the operator has already typed the fields, and
    letting a best-effort parse overwrite them is how the upload path produces
    wrong vendors and amounts.
    The GRN number is generated server-side by the same allocator the upload
    path uses, and the lot is left unmapped: mapping happens afterwards, on the
    tag list below, exactly as it does for an uploaded GRN.
    """
    from datetime import date as _date
    from urllib.parse import quote

    base = ("/trade-partner/manage-lots" if source == EXTERNAL_PARTNER_TEST_GRN_SOURCE
            else "/grn/post-iqc" if source == "post_iqc" else "/grn")

    # Same duplicate rule as the upload path: the invoice number is the real
    # business key, so refuse a second GRN for one that already exists rather
    # than letting the same goods be received twice.
    inv_no = (invoice_number or "").strip()
    if inv_no:
        same_grn = (await db.execute(
            select(GRNImport.grn_number).where(GRNImport.invoice_number == inv_no,
                                               GRNImport.is_deleted == False)
        )).scalar_one_or_none()
        if same_grn:
            return RedirectResponse(
                url=f"{base}?error=" + quote(
                    f"Invoice number {inv_no} already exists as GRN {same_grn}."),
                status_code=302)

    entity = (entity or "").strip()
    if entity and entity not in await entity_values(db):
        return RedirectResponse(url=f"{base}?error=" + quote(f"Unknown entity '{entity}'."),
                                status_code=302)
    if source == "post_iqc" and not entity:
        return RedirectResponse(url=f"{base}?error=" + quote("Select the GRN's Entity."),
                                status_code=302)

    def _int(v):
        try:
            return int(v) if v else None
        except ValueError:
            return None

    def _dec(v):
        try:
            return Decimal(v.replace(",", "")) if v else None
        except Exception:
            return None

    def _date_or_none(v):
        try:
            return _date.fromisoformat(v) if v else None
        except ValueError:
            return None

    grn_number = await _next_grn_12(db)

    # Store the attachment, if any, under the same directory and naming scheme
    # the upload path uses so the existing download route resolves it unchanged.
    file_name = file_path = file_hash = None
    if invoice is not None:
        data = await invoice.read()
        if data:
            os.makedirs(GRN_UPLOAD_DIR, exist_ok=True)
            file_hash = hashlib.sha256(data).hexdigest()
            file_name = (invoice.filename or "invoice.pdf").replace("/", "_").replace("\\", "_")
            file_path = os.path.join(GRN_UPLOAD_DIR, f"{grn_number}_{file_name}")
            with open(file_path, "wb") as f:
                f.write(data)

    g = GRNImport(
        grn_number=grn_number,
        entity=entity or None,
        file_name=file_name, file_path=file_path, file_hash=file_hash,
        invoice_number=inv_no or None,
        invoice_date=invoice_date or None,
        sender_name=sender_name or None,
        quantity=_int(quantity), amount=_dec(amount),
        purchase_date=_date_or_none(purchase_date),
        grn_date=_date_or_none(grn_date),
        po_number=po_number or None,
        vehicle_number=vehicle_number or None,
        e_way_bill=e_way_bill or None,
        notes=notes or None,
        source=(source if source in ("post_iqc", EXTERNAL_PARTNER_TEST_GRN_SOURCE) else "invoice"),
        created_by=current_user.username,
    )
    db.add(g)
    await db.flush()
    linked, created, refused = await _map_lots_to_grn(
        db, g, _split_lot_numbers(lot_numbers), current_user.username)
    await audit(db, user=current_user, action="GRN_CREATED_MANUAL",
                table_name="grn_imports", record_id=grn_number,
                new_value={"grn_number": grn_number, "invoice_number": inv_no or None,
                           "vendor": sender_name or None, "entity": entity or None,
                           "lots_linked": linked, "lots_created": created},
                request=request)
    await db.commit()
    msg = f"GRN {grn_number} created"
    if linked or created:
        msg += f" — lots mapped: {', '.join(linked + created)}"
        if created:
            msg += f" (new: {', '.join(created)})"
    if refused:
        msg += f". Not mapped (lot is in Trash): {', '.join(refused)}"
    return RedirectResponse(url=f"{base}?success=" + quote(msg), status_code=302)


@router.post("/{grn_id}/edit")
async def grn_edit(
    grn_id: str, request: Request,
    invoice_number: str = Form(""), invoice_date: str = Form(""),
    sender_name: str = Form(""), quantity: str = Form(""), amount: str = Form(""),
    # None (not "") when the form omits the field. The Edit GRN modal no longer
    # carries a Lot Number input, and a "" default would blank the stored lot
    # number on every save.
    lot_number: str | None = Form(None),
    purchase_date: str = Form(""), grn_date: str = Form(""),
    po_number: str = Form(""), vehicle_number: str = Form(""),
    e_way_bill: str = Form(""), notes: str = Form(""),
    # None when the form has no Entity field (older callers) — keeps the value.
    entity: str | None = Form(None),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed),
    _perm: User = Depends(require_additional_perm("edit_grn")),
):
    from datetime import date as _date
    try:
        import uuid as _u
        gid = _u.UUID(grn_id)
    except ValueError:
        raise HTTPException(404)
    g = (await db.execute(select(GRNImport).where(GRNImport.id == gid))).scalar_one_or_none()
    if not g:
        raise HTTPException(404, "GRN not found")
    g.invoice_number = invoice_number or None
    g.invoice_date = invoice_date or None
    if lot_number is not None:
        g.lot_number = lot_number or None
    g.sender_name = sender_name or None
    try:
        g.quantity = int(quantity) if quantity else None
    except ValueError:
        pass
    try:
        g.amount = Decimal(amount.replace(",", "")) if amount else None
    except Exception:
        pass
    g.purchase_date = _date.fromisoformat(purchase_date) if purchase_date else None
    g.grn_date = _date.fromisoformat(grn_date) if grn_date else None
    g.po_number = po_number or None
    g.vehicle_number = vehicle_number or None
    g.e_way_bill = e_way_bill or None
    g.notes = notes or None
    if entity is not None:
        ent = entity.strip()
        if ent and ent not in await entity_values(db):
            raise HTTPException(400, f"Unknown entity '{ent}'.")
        g.entity = ent or None

    # Mirror the shared header fields onto every Lot mapped to this GRN. A GRN
    # can now carry several Lots via the Add/Edit Lot modal, so this can no
    # longer assume a single g.lot_id — it mirrors onto the full set.
    mapped_lots = (await db.execute(
        select(Lot).where(Lot.grn_system_number == g.grn_number)
    )).scalars().all()
    if g.lot_id and not any(lot.id == g.lot_id for lot in mapped_lots):
        legacy_lot = (await db.execute(select(Lot).where(Lot.id == g.lot_id))).scalar_one_or_none()
        if legacy_lot:
            mapped_lots.append(legacy_lot)
    for lot in mapped_lots:
        lot.vendor_name = g.sender_name
        lot.supplier_name = g.sender_name or lot.supplier_name
        lot.invoice_no = g.invoice_number
        try:
            lot.invoice_date = datetime.fromisoformat(invoice_date) if invoice_date else lot.invoice_date
        except ValueError:
            pass
        lot.invoice_value = g.amount
        lot.qty = g.quantity or lot.qty
        lot.purchase_date = (datetime.combine(g.purchase_date, datetime.min.time())
                              if g.purchase_date else lot.purchase_date)
        lot.grn_date = (datetime.combine(g.grn_date, datetime.min.time())
                         if g.grn_date else lot.grn_date)
        lot.po_number = g.po_number
        lot.vehicle_number = g.vehicle_number
        lot.e_way_bill = g.e_way_bill
        lot.notes = g.notes

    await db.commit()
    # Test GRNs redirect back to Manage Lots — the Edit GRN modal there posts
    # to this same endpoint, so it must not land the operator on the live
    # GRN Records page.
    base = ("/trade-partner/manage-lots" if g.source == EXTERNAL_PARTNER_TEST_GRN_SOURCE
            else "/grn/post-iqc")
    return RedirectResponse(url=f"{base}?success=GRN+{g.grn_number}+updated", status_code=302)


@router.get("/lots/check")
async def grn_lot_check(lot_number: str, db: AsyncSession = Depends(get_db),
                         current_user: User = Depends(allowed)):
    """Add Lot modal — checks whether the typed Lot Number already exists,
    so the UI can show the 'add tags in this Lot' confirmation checkbox."""
    exists = (await db.execute(
        select(Lot.id).where(Lot.lot_number == lot_number)
    )).scalar_one_or_none() is not None
    return JSONResponse({"exists": exists})


@router.post("/{grn_id}/add-lot")
async def grn_add_lot(grn_id: str, lot_number: list[str] = Form(...),
                       confirm_merge: list[str] = Form(default=[]),
                       db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed)):
    """GRN in Plant 'Add/Edit Lot' action — maps this GRN to one or more Lot
    Numbers, entered as multiple rows in the same modal. Each row runs the
    same per-lot create-or-merge logic the single-lot flow always used: if a
    typed Lot Number already exists, its row's confirm_merge checkbox must be
    ticked (checked with a first pass below, before anything is written) and
    the existing Lot's header fields (all but Quantity) win onto this GRN; otherwise a brand-new
    Lot is created, seeded from this GRN's current fields — just repeated
    once per row so a GRN can now carry several Lots instead of only one."""
    try:
        import uuid as _u
        gid = _u.UUID(grn_id)
    except ValueError:
        raise HTTPException(404)
    g = (await db.execute(select(GRNImport).where(GRNImport.id == gid))).scalar_one_or_none()
    if not g:
        raise HTTPException(404, "GRN not found")

    numbers = [n.strip() for n in lot_number if n and n.strip()]
    if not numbers:
        raise HTTPException(400, "Enter at least one Lot Number.")
    confirms = list(confirm_merge) + [""] * (len(numbers) - len(confirm_merge))

    # First pass: look up every typed Lot Number and block on any unconfirmed
    # existing one before writing anything, so a batch never applies halfway.
    existing_by_number = {}
    conflicts = []
    for number, confirmed in zip(numbers, confirms):
        existing = (await db.execute(select(Lot).where(Lot.lot_number == number))).scalar_one_or_none()
        existing_by_number[number] = existing
        if existing and not confirmed:
            conflicts.append(number)
    if conflicts:
        return JSONResponse({"ok": False, "conflicts": conflicts})

    # Flow 1 rules (2026-10-21): an existing lot is linked as-is — it no longer
    # overwrites this GRN's invoice/vendor fields, since one lot can now sit
    # under several GRNs (its tags split across invoices); a missing lot is
    # created from this GRN's details. Links are recorded in grn_lots.
    linked, created, refused = await _map_lots_to_grn(db, g, numbers, current_user.username)
    if refused:
        await db.rollback()
        return JSONResponse({"ok": False, "error":
                             "These Lot Numbers are in Trash and can't be mapped: " + ", ".join(refused)})
    await db.commit()
    return JSONResponse({"ok": True, "linked": linked, "created": created})


@router.post("/{grn_id}/as-is-lot")
async def grn_as_is_lot(request: Request, grn_id: str, sub_lot_number: str = Form(...),
                        assigned_to_user_id: str = Form(""),
                        db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed)):
    """GRN Post-IQC's "Yes As-Is" modal — bulk-writes one Sub-Lot Number (and,
    optionally, an Assigned To employee) across every Tag Number currently
    mapped to this GRN (Device.grn_number), same "stocked" scope
    _stocked_map counts."""
    try:
        import uuid as _u
        gid = _u.UUID(grn_id)
    except ValueError:
        raise HTTPException(404)
    g = (await db.execute(select(GRNImport).where(GRNImport.id == gid))).scalar_one_or_none()
    if not g:
        raise HTTPException(404, "GRN not found")
    value = sub_lot_number.strip()
    if not value:
        raise HTTPException(400, "Enter a Sub-Lot Number.")
    if not g.grn_number:
        raise HTTPException(400, "This GRN has no GRN Number to match Tag Numbers against.")

    assign_uid = None
    if assigned_to_user_id.strip():
        try:
            import uuid as _u2
            assign_uid = _u2.UUID(assigned_to_user_id.strip())
        except ValueError:
            assign_uid = None

    values = {"sub_lot_number": value}
    if assign_uid:
        values["assigned_to_user_id"] = assign_uid
    result = await db.execute(
        update(Device).where(Device.grn_number == g.grn_number, Device.is_active == True,
                              Device.is_trashed == False)
        .values(**values)
    )
    await audit(db, user=current_user, action="GRN_AS_IS_LOT_SET",
                table_name="grn_imports", record_id=str(g.id),
                new_value={"grn_number": g.grn_number, "sub_lot_number": value,
                           "assigned_to_user_id": str(assign_uid) if assign_uid else None,
                           "tags_updated": result.rowcount},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True, "sub_lot_number": value, "tags_updated": result.rowcount})


# ── Legacy per-lot GRN status view (kept; not in nav) ─────────────────────────

@router.get("/lots-status", response_class=HTMLResponse)
async def grn_list(request: Request, db: AsyncSession = Depends(get_db),
                   current_user: User = Depends(allowed)):
    from models.device import Device
    from sqlalchemy import func

    result = await db.execute(select(Lot).order_by(Lot.created_at.desc()))
    lots = result.scalars().all()
    lot_ids = [lot.id for lot in lots]

    dev_counts = {}
    if lot_ids:
        dev_rows = await db.execute(
            select(Device.lot_id, func.count(Device.id))
            .where(Device.lot_id.in_(lot_ids))
            .group_by(Device.lot_id)
        )
        dev_counts = dict(dev_rows.fetchall())

    lot_data = [
        {
            "lot": lot,
            "actual_devices": dev_counts.get(lot.id, 0),
            "grn_received": lot.qty or 0,
            "mismatch": dev_counts.get(lot.id, 0) != (lot.qty or 0),
        }
        for lot in lots
    ]

    return templates.TemplateResponse("grn/index.html", {
        "request": request, "lot_data": lot_data, "current_user": current_user,
    })


@router.get("/new", response_class=HTMLResponse)
async def grn_new_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    lot_id: str = Query(default=""),
):
    lots_result = await db.execute(select(Lot).order_by(Lot.lot_number))
    lots = lots_result.scalars().all()
    next_grn = await _next_grn_number(db)
    return templates.TemplateResponse("grn/form.html", {
        "request": request, "lots": lots, "current_user": current_user,
        "error": None, "next_grn": next_grn, "preselect_lot_id": lot_id,
    })


@router.post("/submit")
async def submit_grn(
    request: Request,
    lot_id: str = Form(...),
    expected_qty: int = Form(...),
    received_qty: int = Form(...),
    grn_number: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    lot_result = await db.execute(select(Lot).where(Lot.id == lot_id))
    lot = lot_result.scalar_one_or_none()
    if not lot:
        raise HTTPException(404, "Lot not found")

    mismatch = received_qty != expected_qty

    # Auto-generate GRN number if not provided
    if not grn_number:
        grn_number = await _next_grn_number(db)

    # Store GRN info on lot record
    lot.qty = received_qty   # update with actual received qty
    lot.grn_system_number = grn_number
    lot.grn_date = app_now()

    # Audit
    db.add(AuditLog(
        username=current_user.username,
        action="GRN_SUBMITTED",
        table_name="lots",
        record_id=str(lot.id),
        new_value=(
            f'{{"lot": "{lot.lot_number}", "expected": {expected_qty}, '
            f'"received": {received_qty}, "mismatch": {str(mismatch).lower()}}}'
        ),
        notes=f"Mismatch: {mismatch}" if mismatch else "OK",
    ))

    await db.commit()

    import urllib.parse
    success_msg = urllib.parse.quote(f"GRN recorded for {lot.lot_number}")
    redirect = f"/lots/{lot_id}?success={success_msg}"
    if mismatch:
        warn_msg = urllib.parse.quote(
            f"QTY MISMATCH — Expected {expected_qty}, Received {received_qty}. Check lot before proceeding."
        )
        redirect += f"&warning={warn_msg}"
    return RedirectResponse(url=redirect, status_code=302)
