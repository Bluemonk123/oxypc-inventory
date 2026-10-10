"""
Block Tags
==========
Hold specific tags off the sellable market for a pending deal or customer,
without actually selling them. A blocked tag stays visible on Ready to Sale
(stock levels still read correctly) but its Sell action is refused by
services/control_engine.validate_sale_allowed() until released.

One "New Block Tags" submission creates one BlockRecord (the shared header —
Block ID, Date, Container, Location, Block For, Customer) plus one
BlockRecordItem per tag selected. The "Tags Blocked" table lists one row per
still-blocked BlockRecordItem (so Edit/Release/Bulk Release can act on
individual tags within the same block), with the parent BlockRecord's shared
fields repeated on every sibling row. Releasing a tag cancels the hold and
removes its row from the table (it stays in the DB, status=released, for the
CSV export history — never hard-deleted).
"""
import io
import re
import uuid as _uuid
from datetime import datetime, date as _date
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request, Query, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_

from database import get_db
from templates_config import templates
from auth.dependencies import get_current_user, require_module_perm, verify_csrf
from models.user import User
from models.device import Device, DeviceStage
from models.lot import Lot
from models.location import UnitType, ZoneType, UNIT_TYPE_LABELS, ZONE_LABELS
from models.block_tags import BlockRecord, BlockRecordItem, BlockItemStatus, BLOCK_ITEM_STATUS_LABELS
from routers.inventory_location import _zone_dropdown_options

router = APIRouter(prefix="/block-tags", tags=["block-tags"], dependencies=[Depends(verify_csrf)])


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _next_block_id(db: AsyncSession, block_date: _date, container: UnitType) -> str:
    """<DDMMYYYY>-<CONTAINER>-<4-digit incremental>, e.g. 01102026-SHELF-0001.
    The incremental counter resets per (block_date, container) pair — each
    container type gets its own daily sequence rather than sharing one."""
    prefix = f"{block_date.strftime('%d%m%Y')}-{container.value.upper()}-"
    count = (await db.execute(
        select(func.count()).select_from(BlockRecord)
        .where(BlockRecord.block_date == block_date, BlockRecord.container == container)
    )).scalar() or 0
    return f"{prefix}{count + 1:04d}"


def _block_container_options():
    return [(u.value, UNIT_TYPE_LABELS.get(u, u.value)) for u in UnitType]


async def _block_for_options(db: AsyncSession):
    rows = (await db.execute(
        select(User.id, User.full_name, User.username).where(User.status == True)  # noqa: E712
        .order_by(User.full_name)
    )).all()
    return [{"id": str(r.id), "label": r.full_name or r.username} for r in rows]


async def _block_location_options(db: AsyncSession):
    zones = await _zone_dropdown_options(db)
    return [(z.value, ZONE_LABELS.get(z, z.value)) for z in zones]


def _block_filters(q, block_date, container, block_for, location_zone):
    w = []
    if q:
        like = f"%{q}%"
        w.append(or_(
            BlockRecord.block_id.ilike(like), BlockRecordItem.barcode.ilike(like),
            BlockRecordItem.lot_number.ilike(like), BlockRecord.customer_name.ilike(like),
            BlockRecord.box_number.ilike(like),
            BlockRecordItem.device_id.in_(
                select(Device.id).join(Lot, Device.lot_id == Lot.id).where(Lot.lot_number.ilike(like))),
        ))
    if block_date:
        try:
            w.append(BlockRecord.block_date == datetime.strptime(block_date.strip(), "%Y-%m-%d").date())
        except ValueError:
            pass
    if container:
        try:
            w.append(BlockRecord.container == UnitType(container))
        except ValueError:
            pass
    if block_for:
        w.append(BlockRecord.block_for_user_id == block_for)
    if location_zone:
        try:
            w.append(BlockRecord.location_zone == ZoneType(location_zone))
        except ValueError:
            pass
    return w


async def _release_items(db: AsyncSession, item_ids: list, current_user: User):
    """Shared by the single-row and bulk Release actions. Cancels the hold:
    flips Device.is_blocked back to False (tag is sellable again) and marks
    the item Released, which drops it out of the Tags Blocked table — see the
    BlockRecordItem.status == blocked filter in block_tags_data()/_ids below.
    Released rows are kept (not hard-deleted) for the CSV export history;
    re-releasing an already-released row is a no-op, not an error, so a bulk
    action over a mixed selection never fails partway through."""
    items = (await db.execute(
        select(BlockRecordItem).where(BlockRecordItem.id.in_(item_ids))
    )).scalars().all()
    if not items:
        return 0
    device_ids = [it.device_id for it in items if it.status == BlockItemStatus.blocked]
    for it in items:
        if it.status != BlockItemStatus.blocked:
            continue
        it.status = BlockItemStatus.released
        it.status_changed_by_user_id = current_user.id
        it.status_changed_at = datetime.utcnow()
    if device_ids:
        devices = (await db.execute(select(Device).where(Device.id.in_(device_ids)))).scalars().all()
        for d in devices:
            d.is_blocked = False
    await db.commit()
    return len(device_ids)


# ── Page ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def block_tags_home(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("block_tags")),
):
    return templates.TemplateResponse("block_tags/list.html", {
        "request": request,
        "current_user": current_user,
        "today": _date.today().isoformat(),
        "container_options": _block_container_options(),
        "block_for_options": await _block_for_options(db),
        "location_options": await _block_location_options(db),
    })


# ── Table data (DataTables server-side) ──────────────────────────────────────

@router.get("/data")
async def block_tags_data(
    request: Request,
    draw: int = Query(default=1), start: int = Query(default=0), length: int = Query(default=25),
    q: str = Query(default=""), block_date: str = Query(default=""), container: str = Query(default=""),
    block_for: str = Query(default=""), location_zone: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("block_tags")),
):
    from html import escape

    def esc(v):
        return escape(str(v)) if v is not None else ""

    # Released tags drop out of this table entirely (per product decision —
    # Release = cancel the hold and remove it from view); they're still kept
    # in the DB and still show up in the CSV exports for history.
    still_blocked = BlockRecordItem.status == BlockItemStatus.blocked
    base = (
        select(BlockRecordItem, BlockRecord)
        .join(BlockRecord, BlockRecordItem.block_record_id == BlockRecord.id)
        .where(still_blocked)
    )
    count_base = select(func.count()).select_from(BlockRecordItem).join(
        BlockRecord, BlockRecordItem.block_record_id == BlockRecord.id).where(still_blocked)

    filters = _block_filters(q, block_date, container, block_for, location_zone)
    total = (await db.execute(count_base)).scalar() or 0
    filtered = (await db.execute(count_base.where(*filters))).scalar() or 0

    rows = (await db.execute(
        base.where(*filters)
        .order_by(BlockRecord.created_at.desc(), BlockRecordItem.created_at.asc())
        .offset(max(0, start)).limit(min(max(1, length), 500))
    )).all()

    block_for_ids = {r.block_for_user_id for _, r in rows if r.block_for_user_id}
    user_map = {}
    if block_for_ids:
        users = (await db.execute(select(User).where(User.id.in_(block_for_ids)))).scalars().all()
        user_map = {u.id: (u.full_name or u.username) for u in users}

    # Lot Number column and the Stock Type badge under the Tag Number are read
    # live from the device (one query for the page): Lot = the device's actual
    # lot; Stock Type follows the rule used on All Inventory / Sales — As-Is
    # when the tag carries a sub-lot, otherwise Ready for Sale.
    dev_ids = {item.device_id for item, _ in rows}
    dev_info = {}
    if dev_ids:
        for dev_id, lot_no, sub_lot in (await db.execute(
            select(Device.id, Lot.lot_number, Device.sub_lot_number)
            .outerjoin(Lot, Device.lot_id == Lot.id).where(Device.id.in_(dev_ids))
        )).all():
            dev_info[dev_id] = (lot_no, (sub_lot or "").strip())

    data = []
    for item, rec in rows:
        lot_no, sub_lot = dev_info.get(item.device_id, (None, ""))
        tag_badges = []
        if sub_lot:
            tag_badges.append(f'<span class="badge bg-warning text-dark" title="As-Is sub-lot {esc(sub_lot)}">As-Is</span>')
        else:
            tag_badges.append('<span class="badge bg-success">Ready for Sale</span>')
        tag_cell = (
            f'<a href="/devices/{esc(item.barcode)}" class="text-decoration-none"><code>{esc(item.barcode)}</code></a>'
            f'<div class="d-flex flex-nowrap text-nowrap gap-1 mt-1">{"".join(tag_badges)}</div>'
        )

        # Block ID with its Box Number (high-contrast pill) and the barcode
        # label download right underneath, so everything about the physical
        # box sits in one place.
        under_block = []
        if rec.box_number:
            under_block.append(
                f'<span class="badge rounded-pill bg-dark" style="font-size:.78rem;letter-spacing:.02em" '
                f'title="Box Number"><i class="bi bi-box-seam me-1"></i>{esc(rec.box_number)}</span>')
        under_block.append(
            f'<a href="#" class="btn btn-primary btn-sm py-0 px-2 fw-semibold block-barcode-btn" '
            f'data-id="{rec.id}" data-blockid="{esc(rec.block_id)}" title="Preview / download barcode label">'
            f'<i class="bi bi-download"></i> <i class="bi bi-upc-scan"></i> barcode</a>')
        block_cell = (f'<span class="fw-semibold text-nowrap">{esc(rec.block_id)}</span>'
                      f'<div class="d-flex flex-nowrap text-nowrap align-items-center gap-1 mt-1">{"".join(under_block)}</div>')

        actions = (
            f'<a href="#" class="btn btn-outline-secondary btn-sm py-0 px-1 block-edit-btn" data-id="{rec.id}" title="Edit"><i class="bi bi-pencil"></i></a>'
            f' <a href="#" class="btn btn-outline-primary btn-sm py-0 px-1 block-release-btn" data-id="{item.id}" title="Release"><i class="bi bi-unlock"></i></a>'
        )
        data.append([
            f'<input type="checkbox" class="row-check" value="{item.id}">',
            block_cell,
            tag_cell,
            f'<span class="text-nowrap">{esc(lot_no or "—")}</span>',
            esc(item.model or "—"),
            f'<span class="text-nowrap">{esc(item.cpu or "—")}</span>',
            f"{item.ram_gb} GB" if item.ram_gb else "—",
            f"{item.storage_gb} GB" if item.storage_gb else "—",
            f'<span class="text-nowrap">{rec.block_date.isoformat() if rec.block_date else ""}</span>',
            esc(user_map.get(rec.block_for_user_id, "—")),
            actions,
        ])

    return JSONResponse({"draw": draw, "recordsTotal": total, "recordsFiltered": filtered, "data": data})


@router.get("/ids")
async def block_tags_ids(
    q: str = Query(default=""), block_date: str = Query(default=""), container: str = Query(default=""),
    block_for: str = Query(default=""), location_zone: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("block_tags")),
):
    """Every (still-blocked) BlockRecordItem id matching the current filters,
    for Select All on the Tags Blocked table — same cross-page fix pattern as
    /sales/ids. Released items never appear in this table, so they're never
    selectable here either."""
    MAX_SELECT_ALL = 5000
    filters = _block_filters(q, block_date, container, block_for, location_zone)
    ids = (await db.execute(
        select(BlockRecordItem.id)
        .join(BlockRecord, BlockRecordItem.block_record_id == BlockRecord.id)
        .where(BlockRecordItem.status == BlockItemStatus.blocked, *filters)
        .order_by(BlockRecord.created_at.desc())
        .limit(MAX_SELECT_ALL)
    )).scalars().all()
    return JSONResponse({"ids": [str(i) for i in ids], "truncated": len(ids) == MAX_SELECT_ALL})


# ── New Block Tags modal: tag search + create ────────────────────────────────

@router.get("/search-tags")
async def search_tags(
    q: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("block_tags")),
):
    """Candidates for the New Block Tags modal's search+select box — Ready to
    Sale devices that are not already blocked."""
    term = q.strip()
    if not term:
        return JSONResponse({"data": []})
    like = f"%{term}%"
    exact = func.lower(Device.barcode) == term.lower()
    rows = (await db.execute(
        select(Device).where(
            Device.current_stage == DeviceStage.ready_to_sale,
            Device.is_trashed == False,
            Device.is_blocked == False,
            or_(Device.barcode.ilike(like), Device.model.ilike(like), Device.brand.ilike(like)),
        )
        # Exact barcode first, so a scanned tag is always in the result set
        # even when 50+ other tags merely contain the same digits — the modal
        # auto-selects on an exact match.
        .order_by(exact.desc(), Device.barcode)
        .limit(50)
    )).scalars().all()

    # Scanner feedback: when the exact tag exists but can't be blocked, say
    # why instead of silently showing nothing.
    exact_unavailable = None
    if not any((d.barcode or "").lower() == term.lower() for d in rows):
        dev = (await db.execute(select(Device).where(exact).limit(1))).scalar_one_or_none()
        if dev is not None:
            if dev.is_trashed:
                exact_unavailable = "is in trash"
            elif dev.is_blocked:
                exact_unavailable = "is already blocked"
            elif dev.current_stage != DeviceStage.ready_to_sale:
                stage = dev.current_stage.value if dev.current_stage else "unknown"
                exact_unavailable = f"is not in Ready to Sale (stage: {stage.replace('_', ' ')})"

    return JSONResponse({"data": [{
        "id": str(d.id), "barcode": d.barcode, "model": f"{d.brand or ''} {d.model or ''}".strip(),
        "lot_number": None,
    } for d in rows], "exact_unavailable": exact_unavailable})


# ── Upload a file of Block IDs / Tag Numbers to select table rows ──────────────

_BLOCK_ID_RE = re.compile(r"^\d{8}-[A-Z_]+-\d{4}$", re.IGNORECASE)
_BLOCK_HEADERS = {"blockid", "block", "blockno", "blocknumber"}
_TAG_HEADERS = {"tagnumber", "tag", "tagno", "tagid", "barcode"}
_MAX_UPLOAD_ROWS = 5000


def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)  # Excel stores numeric tag numbers as 12345.0
    return str(v).strip()


def _read_upload_rows(filename: str, content: bytes) -> list:
    """Raw rows (lists of strings) from a .csv/.txt or .xlsx upload."""
    import csv
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = [[_cell_text(c) for c in r] for r in ws.iter_rows(values_only=True)]
        wb.close()
        return rows
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [[_cell_text(c) for c in r] for r in csv.reader(io.StringIO(text), dialect)]


def _parse_block_upload(rows: list) -> list:
    """(block_id, tag_number) pairs, either may be "". Uses the header row when
    it names the columns (Block ID / Tag Number, case and spacing ignored);
    without a header, each value is classified by shape — Block IDs always look
    like 01102026-SHELF-0001, anything else is treated as a tag number."""
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return []
    norm = [re.sub(r"[^a-z0-9]", "", c.lower()) for c in rows[0]]
    b_col = next((i for i, h in enumerate(norm) if h in _BLOCK_HEADERS), None)
    t_col = next((i for i, h in enumerate(norm) if h in _TAG_HEADERS), None)

    pairs = []
    if b_col is not None or t_col is not None:
        for r in rows[1:]:
            b = r[b_col] if b_col is not None and b_col < len(r) else ""
            t = r[t_col] if t_col is not None and t_col < len(r) else ""
            if b or t:
                pairs.append((b, t))
    else:
        for r in rows:
            vals = [c for c in r if c]
            b = next((c for c in vals if _BLOCK_ID_RE.match(c)), "")
            t = next((c for c in vals if not _BLOCK_ID_RE.match(c)), "")
            if b or t:
                pairs.append((b, t))
    return pairs[:_MAX_UPLOAD_ROWS]


@router.post("/match-upload")
async def match_upload(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("block_tags")),
):
    """Reads an uploaded list of Block IDs and/or Tag Numbers and returns the
    matching still-blocked item ids, so the page can tick those rows for bulk
    actions. Read-only — nothing is changed here. Per row: Block ID only ->
    every tag in that block; Tag Number only -> that tag; both -> that tag,
    only if it belongs to that block."""
    content = await file.read()
    if not content:
        return JSONResponse({"ok": False, "error": "The file is empty."})
    if len(content) > 5 * 1024 * 1024:
        return JSONResponse({"ok": False, "error": "File too large (max 5 MB)."})
    try:
        pairs = _parse_block_upload(_read_upload_rows(file.filename, content))
    except Exception:  # noqa: BLE001 — corrupt/unsupported file: tell the user, don't 500
        return JSONResponse({"ok": False, "error": "Could not read the file. Upload a .csv or .xlsx file."})
    if not pairs:
        return JSONResponse({"ok": False, "error": "No Block ID or Tag Number values found in the file."})

    block_keys = {b.upper() for b, _ in pairs if b}
    tag_keys = {t.upper() for _, t in pairs if t}
    conds = []
    if block_keys:
        conds.append(func.upper(BlockRecord.block_id).in_(block_keys))
    if tag_keys:
        conds.append(func.upper(BlockRecordItem.barcode).in_(tag_keys))
    found = (await db.execute(
        select(BlockRecordItem.id, BlockRecordItem.barcode, BlockRecord.block_id)
        .join(BlockRecord, BlockRecordItem.block_record_id == BlockRecord.id)
        .where(BlockRecordItem.status == BlockItemStatus.blocked, or_(*conds))
    )).all()

    by_block, by_tag = {}, {}
    for item_id, barcode, block_id in found:
        by_block.setdefault(block_id.upper(), []).append((str(item_id), barcode.upper()))
        by_tag.setdefault(barcode.upper(), []).append((str(item_id), block_id.upper()))

    ids, unmatched = [], []
    for b, t in pairs:
        bu, tu = b.upper(), t.upper()
        if b and t:
            hit = [i for i, bc in by_block.get(bu, []) if bc == tu]
        elif b:
            hit = [i for i, _ in by_block.get(bu, [])]
        else:
            hit = [i for i, _ in by_tag.get(tu, [])]
        if hit:
            ids.extend(hit)
        else:
            unmatched.append(" / ".join(x for x in (b, t) if x))

    unique_ids = list(dict.fromkeys(ids))
    return JSONResponse({
        "ok": True, "ids": unique_ids, "rows": len(pairs),
        "matched_rows": len(pairs) - len(unmatched),
        "unmatched_count": len(unmatched), "unmatched": unmatched[:50],
    })


@router.post("/create")
async def create_block(
    block_date: str = Form(...),
    container: str = Form(...),
    box_number: str = Form(""),
    location_zone: str = Form(""),
    block_for_user_id: str = Form(""),
    customer_name: str = Form(""),
    device_ids: str = Form(...),  # comma-separated Device UUIDs
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("block_tags", "add")),
):
    ids = [i.strip() for i in device_ids.split(",") if i.strip()]
    if not ids:
        return JSONResponse({"ok": False, "error": "Select at least one tag."})
    try:
        b_date = datetime.strptime(block_date.strip(), "%Y-%m-%d").date()
        container_val = UnitType(container)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid date or container."})

    devices = (await db.execute(select(Device).where(Device.id.in_(ids)))).scalars().all()
    already_blocked = [d.barcode for d in devices if d.is_blocked]
    if already_blocked:
        return JSONResponse({"ok": False, "error": f"Already blocked: {', '.join(already_blocked)}"})
    if len(devices) != len(ids):
        return JSONResponse({"ok": False, "error": "One or more tags could not be found."})

    zone_val = None
    if location_zone.strip():
        try:
            zone_val = ZoneType(location_zone.strip())
        except ValueError:
            pass

    record = BlockRecord(
        block_id=await _next_block_id(db, b_date, container_val),
        block_date=b_date,
        container=container_val,
        box_number=box_number.strip()[:50] or None,
        location_zone=zone_val,
        block_for_user_id=block_for_user_id.strip() or None,
        customer_name=customer_name.strip() or None,
        created_by_user_id=current_user.id,
    )
    db.add(record)
    await db.flush()

    block_for_id = block_for_user_id.strip() or None
    for d in devices:
        db.add(BlockRecordItem(
            block_record_id=record.id, device_id=d.id,
            barcode=d.barcode, lot_number=d.sub_lot_number or None, model=f"{d.brand or ''} {d.model or ''}".strip(),
            cpu=d.cpu, ram_gb=d.ram_gb, storage_gb=d.storage_gb,
        ))
        d.is_blocked = True
        # Ready to Sale's Assigned column reads Device.assigned_to_user_id —
        # Block Tags sets it to the Block For user so that column shows who
        # the tag is held for, with the Open/Blocked badge driven separately
        # by is_blocked above.
        if block_for_id:
            d.assigned_to_user_id = block_for_id

    await db.commit()
    return JSONResponse({"ok": True, "block_id": record.block_id, "count": len(devices)})


@router.get("/{record_id}")
async def get_block(record_id: str, db: AsyncSession = Depends(get_db),
                     current_user: User = Depends(require_module_perm("block_tags"))):
    """Current values for the Edit Block modal — without this, the modal
    would open blank and silently overwrite Container/Location/etc. with
    whatever the form's own defaults happen to be the moment you hit Save
    without touching every field."""
    record = (await db.execute(select(BlockRecord).where(BlockRecord.id == record_id))).scalar_one_or_none()
    if not record:
        return JSONResponse({"ok": False, "error": "Block record not found."}, status_code=404)
    return JSONResponse({"ok": True, "record": {
        "block_date": record.block_date.isoformat() if record.block_date else "",
        "container": record.container.value if record.container else "",
        "box_number": record.box_number or "",
        "location_zone": record.location_zone.value if record.location_zone else "",
        "block_for_user_id": str(record.block_for_user_id) if record.block_for_user_id else "",
        "customer_name": record.customer_name or "",
    }})


@router.post("/{record_id}/edit")
async def edit_block(
    record_id: str,
    block_date: str = Form(...),
    container: str = Form(...),
    box_number: str = Form(""),
    location_zone: str = Form(""),
    block_for_user_id: str = Form(""),
    customer_name: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("block_tags", "edit")),
):
    record = (await db.execute(select(BlockRecord).where(BlockRecord.id == record_id))).scalar_one_or_none()
    if not record:
        return JSONResponse({"ok": False, "error": "Block record not found."})
    try:
        record.block_date = datetime.strptime(block_date.strip(), "%Y-%m-%d").date()
        record.container = UnitType(container)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid date or container."})
    record.box_number = box_number.strip()[:50] or None
    record.location_zone = ZoneType(location_zone.strip()) if location_zone.strip() else None
    block_for_id = block_for_user_id.strip() or None
    record.block_for_user_id = block_for_id
    record.customer_name = customer_name.strip() or None

    if block_for_id:
        items = (await db.execute(
            select(BlockRecordItem).where(BlockRecordItem.block_record_id == record.id)
        )).scalars().all()
        device_ids = [it.device_id for it in items]
        if device_ids:
            devices = (await db.execute(select(Device).where(Device.id.in_(device_ids)))).scalars().all()
            for d in devices:
                d.assigned_to_user_id = block_for_id

    await db.commit()
    return JSONResponse({"ok": True})


# ── Release (cancel the hold) ────────────────────────────────────────────────

@router.post("/{item_id}/release")
async def release_item(item_id: str, db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(require_module_perm("block_tags", "edit"))):
    await _release_items(db, [item_id], current_user)
    return JSONResponse({"ok": True})


@router.post("/bulk-release")
async def bulk_release(item_ids: str = Form(...), db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(require_module_perm("block_tags", "edit"))):
    ids = [i.strip() for i in item_ids.split(",") if i.strip()]
    n = await _release_items(db, ids, current_user)
    return JSONResponse({"ok": True, "count": n})


# ── Barcode label (2x4" Code128 of the Block ID) ─────────────────────────────

@router.get("/{record_id}/barcode")
async def download_barcode(record_id: str, inline: bool = Query(default=False),
                            db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(require_module_perm("block_tags"))):
    """Same PDF either way — inline=1 (used by the preview modal's <iframe>)
    asks the browser to render it in place instead of triggering a save
    dialog; the Download button in that modal links here without the param
    for the normal forced-download behaviour."""
    from utils.block_barcode import render_block_id_barcode_pdf

    record = (await db.execute(select(BlockRecord).where(BlockRecord.id == record_id))).scalar_one_or_none()
    if not record:
        return JSONResponse({"ok": False, "error": "Block record not found."}, status_code=404)
    counts = dict((await db.execute(
        select(BlockRecordItem.status, func.count())
        .where(BlockRecordItem.block_record_id == record.id)
        .group_by(BlockRecordItem.status)
    )).all())
    pdf_bytes = render_block_id_barcode_pdf(
        record.block_id,
        quantity=counts.get(BlockItemStatus.blocked, 0),
        released=counts.get(BlockItemStatus.released, 0),
        box_number=record.box_number,
    )
    disposition = "inline" if inline else "attachment"
    return StreamingResponse(
        io.BytesIO(pdf_bytes), media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="block_{record.block_id}.pdf"'},
    )


# ── Exports ────────────────────────────────────────────────────────────────

def _export_header():
    return ["Block ID", "Box Number", "Block Date", "Container", "Block For", "Location", "Customer Name",
            "Tag Number", "Lot Number", "Model", "CPU", "RAM (GB)", "Storage (GB)", "Status"]


def _export_row(item: BlockRecordItem, rec: BlockRecord, user_map: dict):
    return [
        rec.block_id, rec.box_number or "", rec.block_date.isoformat() if rec.block_date else "",
        UNIT_TYPE_LABELS.get(rec.container, rec.container),
        user_map.get(rec.block_for_user_id, ""),
        ZONE_LABELS.get(rec.location_zone, rec.location_zone) if rec.location_zone else "",
        rec.customer_name or "",
        item.barcode, item.lot_number or "", item.model or "", item.cpu or "",
        item.ram_gb or "", item.storage_gb or "", BLOCK_ITEM_STATUS_LABELS.get(item.status, item.status),
    ]


@router.get("/export/all")
async def export_all(db: AsyncSession = Depends(get_db),
                      current_user: User = Depends(require_module_perm("block_tags"))):
    import csv
    rows = (await db.execute(
        select(BlockRecordItem, BlockRecord).join(BlockRecord, BlockRecordItem.block_record_id == BlockRecord.id)
        .order_by(BlockRecord.created_at.desc())
    )).all()
    user_ids = {r.block_for_user_id for _, r in rows if r.block_for_user_id}
    user_map = {}
    if user_ids:
        users = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        user_map = {u.id: (u.full_name or u.username) for u in users}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(_export_header())
    for item, rec in rows:
        writer.writerow(_export_row(item, rec, user_map))
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=block_tags_all.csv"},
    )


@router.post("/export/selected")
async def export_selected(item_ids: str = Form(""), db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(require_module_perm("block_tags"))):
    import csv
    ids = [i.strip() for i in item_ids.split(",") if i.strip()]
    if not ids:
        return JSONResponse({"ok": False, "error": "No rows selected."})
    rows = (await db.execute(
        select(BlockRecordItem, BlockRecord).join(BlockRecord, BlockRecordItem.block_record_id == BlockRecord.id)
        .where(BlockRecordItem.id.in_(ids)).order_by(BlockRecord.created_at.desc())
    )).all()
    user_ids = {r.block_for_user_id for _, r in rows if r.block_for_user_id}
    user_map = {}
    if user_ids:
        users = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        user_map = {u.id: (u.full_name or u.username) for u in users}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(_export_header())
    for item, rec in rows:
        writer.writerow(_export_row(item, rec, user_map))
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=block_tags_selected.csv"},
    )
