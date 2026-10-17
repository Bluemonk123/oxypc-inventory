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
from models.block_tags import BlockRecord, BlockRecordItem, BlockItemStatus, BLOCK_ITEM_STATUS_LABELS, SOLD_NOTE_PREFIX
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

def _matching_blocks(q, block_date, container, block_for, location_zone):
    """Block ids (subquery) that still hold at least one blocked tag and match
    the filter bar. Filters can hit either the block header (Block ID, Box,
    Customer, Date, ...) or any of its tags (Tag Number, Lot), and a match on
    one tag brings in the whole block — the table shows one row per block."""
    filters = _block_filters(q, block_date, container, block_for, location_zone)
    return (
        select(BlockRecordItem.block_record_id)
        .join(BlockRecord, BlockRecordItem.block_record_id == BlockRecord.id)
        .where(BlockRecordItem.status == BlockItemStatus.blocked, *filters)
        .distinct()
    )


@router.get("/data")
async def block_tags_data(
    request: Request,
    draw: int = Query(default=1), start: int = Query(default=0), length: int = Query(default=25),
    q: str = Query(default=""), block_date: str = Query(default=""), container: str = Query(default=""),
    block_for: str = Query(default=""), location_zone: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("block_tags")),
):
    """One row per block. Released tags drop out entirely (per product
    decision — Release = cancel the hold and remove it from view); a block
    with no tags left blocked disappears too. Both stay in the DB and in the
    CSV exports for history. The Tags and Models cells open modals fed by
    /block-tags/{id}/items."""
    from html import escape

    def esc(v):
        return escape(str(v)) if v is not None else ""

    total = (await db.execute(
        select(func.count()).select_from(_matching_blocks("", "", "", "", "").subquery())
    )).scalar() or 0
    matching = _matching_blocks(q, block_date, container, block_for, location_zone)
    filtered = (await db.execute(select(func.count()).select_from(matching.subquery()))).scalar() or 0

    # Column sorting (2026-10-18): 1 Block ID, 2 Box/Bundle, 3 Customer,
    # 4 Tags (still-blocked count), 5 Models (distinct count), 6 Block Date,
    # 7 Block For (user name). No order -> newest block first, as before.
    _blocked = (BlockRecordItem.block_record_id == BlockRecord.id) & (BlockRecordItem.status == BlockItemStatus.blocked)
    sort_map = {
        1: BlockRecord.block_id,
        2: BlockRecord.box_number,
        3: func.lower(BlockRecord.customer_name),
        4: select(func.count(BlockRecordItem.id)).where(_blocked).correlate(BlockRecord).scalar_subquery(),
        5: select(func.count(func.distinct(func.nullif(func.trim(BlockRecordItem.model), ""))))
             .where(_blocked).correlate(BlockRecord).scalar_subquery(),
        6: BlockRecord.block_date,
        7: select(func.lower(func.coalesce(User.full_name, User.username)))
             .where(User.id == BlockRecord.block_for_user_id).correlate(BlockRecord).scalar_subquery(),
    }
    try:
        order_col = int(request.query_params.get("order[0][column]", -1))
    except ValueError:
        order_col = -1
    sort_expr = sort_map.get(order_col)
    if sort_expr is not None:
        asc = request.query_params.get("order[0][dir]", "asc") == "asc"
        order_by = [(sort_expr.asc() if asc else sort_expr.desc()).nulls_last(), BlockRecord.created_at.desc()]
    else:
        order_by = [BlockRecord.created_at.desc()]

    records = (await db.execute(
        select(BlockRecord).where(BlockRecord.id.in_(matching))
        .order_by(*order_by)
        .offset(max(0, start)).limit(min(max(1, length), 500))
    )).scalars().all()

    # Per-block stats for this page in one query: still-blocked tag count,
    # distinct model count, and the item ids (row checkbox selects these).
    stats = {}
    rec_ids = [r.id for r in records]
    if rec_ids:
        for rid, n_tags, n_models, item_ids in (await db.execute(
            select(BlockRecordItem.block_record_id, func.count(BlockRecordItem.id),
                   func.count(func.distinct(func.nullif(func.trim(BlockRecordItem.model), ""))),
                   func.array_agg(BlockRecordItem.id))
            .where(BlockRecordItem.block_record_id.in_(rec_ids),
                   BlockRecordItem.status == BlockItemStatus.blocked)
            .group_by(BlockRecordItem.block_record_id)
        )).all():
            stats[rid] = (n_tags, n_models, [str(i) for i in item_ids])

    block_for_ids = {r.block_for_user_id for r in records if r.block_for_user_id}
    user_map = {}
    if block_for_ids:
        users = (await db.execute(select(User).where(User.id.in_(block_for_ids)))).scalars().all()
        user_map = {u.id: (u.full_name or u.username) for u in users}

    # Sell: only for a role that can record a sale (the New Sale page and its
    # POST enforce the same check server-side).
    from models.role_permissions import has_perm as _has_perm
    role_val = getattr(current_user.role, "value", None) or str(current_user.role)
    can_sell = _has_perm(role_val, "sales", "add")

    data = []
    for rec in records:
        n_tags, n_models, item_ids = stats.get(rec.id, (0, 0, []))

        # Block ID and the barcode label download on one line.
        barcode_btn = (
            f'<a href="#" class="btn btn-primary btn-sm py-0 px-2 fw-semibold block-barcode-btn" '
            f'data-id="{rec.id}" data-blockid="{esc(rec.block_id)}" title="Preview / download barcode label">'
            f'<i class="bi bi-download"></i> <i class="bi bi-upc-scan"></i> barcode</a>')
        block_cell = (f'<div class="d-flex flex-nowrap text-nowrap align-items-center gap-2">'
                      f'<span class="fw-semibold">{esc(rec.block_id)}</span>{barcode_btn}</div>')
        # Box Number in its own column, as a high-contrast pill.
        box_cell = (f'<span class="badge rounded-pill bg-dark text-nowrap" style="font-size:.78rem;letter-spacing:.02em">'
                    f'<i class="bi bi-box-seam me-1"></i>{esc(rec.box_number)}</span>' if rec.box_number else "—")

        tags_cell = (f'<a href="#" class="block-tags-link fw-semibold text-decoration-none text-nowrap" '
                     f'data-id="{rec.id}" data-blockid="{esc(rec.block_id)}" title="View tags in this block">'
                     f'<i class="bi bi-tags me-1"></i>{n_tags} tag{"s" if n_tags != 1 else ""}</a>')
        models_cell = (f'<a href="#" class="block-models-link fw-semibold text-decoration-none text-nowrap" '
                       f'data-id="{rec.id}" data-blockid="{esc(rec.block_id)}" title="View model summary">'
                       f'<i class="bi bi-laptop me-1"></i>{n_models} model{"s" if n_models != 1 else ""}</a>')

        ids_attr = ",".join(item_ids)
        actions = (
            f'<a href="#" class="btn btn-outline-secondary btn-sm py-0 px-1 block-edit-btn" data-id="{rec.id}" title="Edit"><i class="bi bi-pencil"></i></a>'
            f' <a href="#" class="btn btn-outline-primary btn-sm py-0 px-1 block-release-btn" data-items="{ids_attr}" '
            f'data-blockid="{esc(rec.block_id)}" data-count="{n_tags}" title="Release all tags in this block"><i class="bi bi-unlock"></i></a>'
        )
        if can_sell and n_tags:
            # Opens New Sale with every still-blocked tag of this block (no cap);
            # the sale releases the hold on exactly the tags it sells.
            actions += (
                f' <a href="#" class="btn btn-outline-success btn-sm py-0 px-2 block-sell-btn" data-id="{rec.id}" '
                f'data-blockid="{esc(rec.block_id)}" title="Sell all {n_tags} tag(s) in this block">Sell</a>'
            )
        data.append([
            f'<input type="checkbox" class="row-check" value="{rec.id}" data-items="{ids_attr}">',
            block_cell,
            box_cell,
            esc(rec.customer_name or "—"),
            tags_cell,
            models_cell,
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
    """Every still-blocked tag (BlockRecordItem id) in every block matching the
    current filters, for Select All — same cross-page fix pattern as
    /sales/ids. Selection stays at tag level so bulk actions and Upload to
    Select act on exactly the tags chosen, even within a block."""
    MAX_SELECT_ALL = 5000
    matching = _matching_blocks(q, block_date, container, block_for, location_zone)
    ids = (await db.execute(
        select(BlockRecordItem.id)
        .join(BlockRecord, BlockRecordItem.block_record_id == BlockRecord.id)
        .where(BlockRecordItem.status == BlockItemStatus.blocked, BlockRecordItem.block_record_id.in_(matching))
        .order_by(BlockRecord.created_at.desc())
        .limit(MAX_SELECT_ALL)
    )).scalars().all()
    return JSONResponse({"ids": [str(i) for i in ids], "truncated": len(ids) == MAX_SELECT_ALL})


@router.get("/{record_id}/items")
async def block_items(record_id: str, db: AsyncSession = Depends(get_db),
                      current_user: User = Depends(require_module_perm("block_tags"))):
    """Still-blocked tags in one block (Tags modal) plus the per-configuration
    summary (Models modal). Lot Number and Stock Type are read live from the
    device: Lot = the device's actual lot; Stock Type follows the rule used
    on All Inventory / Sales — As-Is when the tag carries a sub-lot,
    otherwise Ready for Sale."""
    try:
        rid = _uuid.UUID(record_id)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Block not found."}, status_code=404)
    record = (await db.execute(select(BlockRecord).where(BlockRecord.id == rid))).scalar_one_or_none()
    if not record:
        return JSONResponse({"ok": False, "error": "Block not found."}, status_code=404)

    rows = (await db.execute(
        select(BlockRecordItem, Lot.lot_number, Device.sub_lot_number)
        .outerjoin(Device, BlockRecordItem.device_id == Device.id)
        .outerjoin(Lot, Device.lot_id == Lot.id)
        .where(BlockRecordItem.block_record_id == rid, BlockRecordItem.status == BlockItemStatus.blocked)
        .order_by(BlockRecordItem.barcode)
    )).all()

    items, summary = [], {}
    for it, lot_no, sub_lot in rows:
        as_is = bool((sub_lot or "").strip())
        items.append({
            "id": str(it.id), "tag": it.barcode, "lot": lot_no or "",
            "model": it.model or "", "cpu": it.cpu or "",
            "ram": f"{it.ram_gb} GB" if it.ram_gb else "", "storage": f"{it.storage_gb} GB" if it.storage_gb else "",
            "stock_type": "As-Is" if as_is else "Ready for Sale", "sub_lot": (sub_lot or "").strip(),
        })
        key = (it.model or "", it.cpu or "", it.ram_gb or 0, it.storage_gb or 0)
        summary[key] = summary.get(key, 0) + 1

    models = [{"model": m, "cpu": c, "ram": f"{r} GB" if r else "", "storage": f"{st} GB" if st else "", "qty": n}
              for (m, c, r, st), n in sorted(summary.items(), key=lambda kv: (kv[0][0].lower(), -kv[1]))]
    return JSONResponse({"ok": True, "block_id": record.block_id, "box_number": record.box_number or "",
                         "items": items, "models": models})


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
        item.ram_gb or "", item.storage_gb or "",
        # A tag sold through the block's Sell button is closed as released with
        # the sale number in its notes — export it as "Sold", not "Released".
        ("Sold" if (item.notes or "").startswith(SOLD_NOTE_PREFIX)
         else BLOCK_ITEM_STATUS_LABELS.get(item.status, item.status)),
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
    """One .xlsx with two sheets for the selected tags:
    Summary      — one row per block, like the on-screen table (counts are of
                   the tags selected in that block).
    Tags Details — one row per selected tag, with Make/Model/Grade/Stock Type
                   read live from the device and its lot."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    ids = []
    for raw in item_ids.split(","):
        try:
            ids.append(_uuid.UUID(raw.strip()))
        except ValueError:
            continue
    if not ids:
        return JSONResponse({"ok": False, "error": "No rows selected."})

    rows = (await db.execute(
        select(BlockRecordItem, BlockRecord, Device, Lot.lot_number)
        .join(BlockRecord, BlockRecordItem.block_record_id == BlockRecord.id)
        .outerjoin(Device, BlockRecordItem.device_id == Device.id)
        .outerjoin(Lot, Device.lot_id == Lot.id)
        .where(BlockRecordItem.id.in_(ids))
        .order_by(BlockRecord.created_at.desc(), BlockRecordItem.barcode)
    )).all()
    if not rows:
        return JSONResponse({"ok": False, "error": "Selected tags were not found."})

    user_ids = {rec.block_for_user_id for _, rec, _, _ in rows if rec.block_for_user_id}
    user_map = {}
    if user_ids:
        users = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        user_map = {u.id: (u.full_name or u.username) for u in users}

    def make_model(item, dev):
        if dev is not None and (dev.brand or dev.model):
            return (dev.brand or ""), (dev.model or "")
        return "", (item.model or "")

    details, summary = [], {}
    for item, rec, dev, lot_no in rows:
        make, model = make_model(item, dev)
        sub_lot = (dev.sub_lot_number or "").strip() if dev is not None else ""
        grade = dev.grade.value if dev is not None and dev.grade else ""
        details.append([
            rec.block_id, rec.box_number or "", rec.block_date, user_map.get(rec.block_for_user_id, ""),
            rec.customer_name or "", item.barcode, lot_no or "", make, model, item.cpu or "",
            item.ram_gb, item.storage_gb, grade.capitalize() if grade == "scrap" else grade,
            "As-Is" if sub_lot else "Ready for Sale",
        ])
        s_row = summary.setdefault(rec.id, {"rec": rec, "tags": 0, "models": set()})
        s_row["tags"] += 1
        label = f"{make} {model}".strip()
        if label:
            s_row["models"].add(label.lower())

    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="212529")

    def write_sheet(ws, header, data, date_cols=(), num_cols=()):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill, c.alignment = head_font, head_fill, Alignment(vertical="center")
        for r in data:
            ws.append(r)
        for idx in date_cols:
            for cell in ws.iter_cols(min_col=idx, max_col=idx, min_row=2):
                for c in cell:
                    c.number_format = "dd-mm-yyyy"
        for idx in num_cols:
            for cell in ws.iter_cols(min_col=idx, max_col=idx, min_row=2):
                for c in cell:
                    c.alignment = Alignment(horizontal="right")
        for i, h in enumerate(header, start=1):
            longest = max([len(h)] + [len(str(r[i - 1])) for r in data if r[i - 1] is not None])
            ws.column_dimensions[get_column_letter(i)].width = min(max(10, longest + 2), 45)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    ws = wb.active
    ws.title = "Summary"
    write_sheet(ws, ["Block ID", "Box Number", "Customer", "Tags", "Models", "Block Date", "Block For"],
                [[v["rec"].block_id, v["rec"].box_number or "", v["rec"].customer_name or "", v["tags"],
                  len(v["models"]), v["rec"].block_date, user_map.get(v["rec"].block_for_user_id, "")]
                 for v in summary.values()],
                date_cols=(6,), num_cols=(4, 5))
    write_sheet(wb.create_sheet("Tags Details"),
                ["Block ID", "Box Number", "Block Date", "Block For", "Customer Name", "Tag Number",
                 "Lot Number", "Make", "Model", "CPU", "RAM (GB)", "Storage (GB)", "Grade", "Stock Type"],
                details, date_cols=(3,), num_cols=(11, 12))

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    from utils.timezone import app_now
    stamp = app_now().strftime("%Y%m%d_%H%M")   # IST, matches the rest of the app
    return StreamingResponse(
        out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="block_tags_{stamp}.xlsx"'},
    )
