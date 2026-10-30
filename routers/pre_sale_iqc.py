"""Pre-Sale IQC (2026-10-29): a last inspection of Ready for Sale tags before
they are sold.

Queue (2026-10-29): Ready for Sale Tags' bulk "Assign to Pre-Sale" puts tags
in the queue — a Pre-Sale IQC WorkID (stage "psiqc", work_id PSQC-…) with
status "pending" and no engineer. Nothing changes on Ready for Sale. The
Pre-Sale IQC page lists only queued tags (any Pre-Sale IQC WorkID pending,
in progress or completed) with the Ready for Sale Tags table's filters and
rows (routers/sales.py ready_feed(psiqc=True)), four tiles (Assigned /
Completed / Part Requested / Part Verified, each filters the table) and its
own Action column, following the L1/L2 flow:

  Start          takes the queued WorkID: in_progress, assigned to the user
                 (a new WorkID when the last one is completed — "Start
                 again"). It shows on WorkID Status as stage "Pre-Sale IQC".
  Request Part   opens the tag's Device Detail, where parts are requested
                 and verified exactly as from L1/L2 (the request attaches to
                 the open PSQC WorkID).
  Mark Complete  same rules as L1/L2's: open PNA parts block it; parts
                 handed over but not verified are verified, parts never
                 handed over are cancelled; the WorkID completes. The tag
                 stays in Ready for Sale.
  Cancel (x)     undoes Start: the WorkID goes back to the queue (pending,
                 no engineer) and its part requests that were never handed
                 over are cancelled.

Parts Requested column: the tag's part requests raised on its Pre-Sale IQC
WorkIDs, as a clickable count + Verified / Pending badge; the modal lists
them.

Module key "pre_sale_iqc" — OFF by default for every role
(models/role_permissions.py DEFAULT_DISABLED_MODULES).
"""
from datetime import datetime
from html import escape

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user, require_module_perm, verify_csrf
from database import get_db
from models.device import Device, DeviceStage
from models.part_request import PartRequest
from models.user import User
from models.work_order import WorkOrder
from services.audit_engine import audit
from utils.timezone import app_now

router = APIRouter(tags=["pre_sale_iqc"])

MODULE = "pre_sale_iqc"
STAGE_CODE = "psiqc"          # WorkOrder.stage (String(5))
WID_PREFIX = "PSQC-"          # WorkOrder.work_id prefix (12 chars total)
LABEL = "Pre-Sale IQC"

view_perm = require_module_perm(MODULE, "enable")
edit_perm = require_module_perm(MODULE, "edit")

# A tag is in the Pre-Sale IQC queue while it has a WorkID in one of these.
QUEUE_STATUSES = ("pending", "in_progress", "completed")
TILES = ("assigned", "completed", "requested", "verified")
VIEWS = ("assigned", "completed")    # the page's two tabs


def queue_device_ids():
    """SQL: ids of the tags in the Pre-Sale IQC queue."""
    return (select(WorkOrder.device_id)
            .where(WorkOrder.stage == STAGE_CODE, WorkOrder.status.in_(QUEUE_STATUSES)))


def part_state(prs) -> str:
    """'' (no parts), 'pending', 'handed_over' or 'verified' — the Parts
    Requested badge."""
    if not prs:
        return ""
    if all(p.status == "received" for p in prs):
        return "verified"
    if all(p.status in ("received", "handed_over") for p in prs):
        return "handed_over"
    return "pending"


async def tile_sets(db, device_ids):
    """For the queued tags in view: (counts, {tile: set(device_id)}).
    Assigned = queued, latest WorkID not completed (the Assigned tab);
    Completed = latest WorkID completed (the Completed tab);
    Part Requested = parts not all verified yet (Pending / Handed Over);
    Part Verified = every part verified — both within the Assigned tab only.
    Parts = the latest WorkID's."""
    state, parts = await psiqc_state_map(db, device_ids)
    sets = {k: set() for k in TILES}
    for did in device_ids:
        wo = state.get(did)
        if wo is None or wo.status not in QUEUE_STATUSES:
            continue
        if wo.status == "completed":
            sets["completed"].add(did)
            continue                 # part tiles count the Assigned tab only (2026-10-29)
        sets["assigned"].add(did)
        st = part_state(parts.get(did))
        if st in ("pending", "handed_over"):
            sets["requested"].add(did)
        elif st == "verified":
            sets["verified"].add(did)
    return {k: len(v) for k, v in sets.items()}, sets


async def assigned_tag_count(db, lo=None, hi=None) -> int:
    """Tags in the Pre-Sale IQC page's Assigned table right now: Ready for
    Sale, not trashed, not in an As-Is sub-lot, queued and latest WorkID not
    completed — the same rows as ready_feed(psiqc=True). With lo/hi, only
    tags whose current Pre-Sale IQC WorkID was created (moved from Ready for
    Sale) in that window. WorkID Status's Pre-Sale IQC tile shows it as
    "Moved" (2026-10-30)."""
    from sqlalchemy import or_
    from models.lot import Lot
    ids = (await db.execute(
        select(Device.id).join(Lot, Device.lot_id == Lot.id)
        .where(Device.current_stage == DeviceStage.ready_to_sale, Device.is_trashed == False,  # noqa: E712
               or_(Device.sub_lot_number.is_(None), Device.sub_lot_number == ""),
               Device.id.in_(queue_device_ids())))).scalars().all()
    _counts, sets = await tile_sets(db, ids)
    if lo is None and hi is None:
        return len(sets["assigned"])
    state, _parts = await psiqc_state_map(db, list(sets["assigned"]))
    lo, hi = lo or datetime.min, hi or datetime.max
    return sum(1 for did in sets["assigned"]
               if state.get(did) is not None and state[did].created_at
               and lo <= state[did].created_at <= hi)


def _esc(v):
    return escape(str(v)) if v is not None else ""


async def psiqc_state_map(db: AsyncSession, device_ids):
    """({device_id: latest Pre-Sale IQC WorkOrder or None},
        {device_id: [PartRequests on that latest WorkID, not cancelled]}).
    Latest only (2026-10-29): a tag assigned again after a completed
    Pre-Sale IQC starts fresh — the old round's parts stay on the old
    WorkID (and in Device Detail), not in this table."""
    state, parts = {}, {}
    ids = [d for d in device_ids if d]
    if not ids:
        return state, parts
    wos = (await db.execute(
        select(WorkOrder).where(WorkOrder.device_id.in_(ids), WorkOrder.stage == STAGE_CODE)
        .order_by(WorkOrder.created_at.asc())
    )).scalars().all()
    for wo in wos:
        state[wo.device_id] = wo                      # latest wins (ascending order)
    wo_ids = [wo.id for wo in state.values()]
    if wo_ids:
        for pr in (await db.execute(
            select(PartRequest).where(PartRequest.work_order_id.in_(wo_ids),
                                      PartRequest.status != "cancelled")
            .order_by(PartRequest.created_at.asc())
        )).scalars().all():
            parts.setdefault(pr.device_id, []).append(pr)
    return state, parts


def _parts_badge(prs) -> str:
    """Three states (2026-10-29): Verified = every part verified as fitted;
    Handed Over = the rest are all handed over, waiting for Verify (Device
    Detail) or Mark Complete; Pending = something not handed over yet."""
    st = part_state(prs)
    if st == "verified":
        return '<span class="badge bg-success ms-1">Verified</span>'
    if st == "handed_over":
        return ('<span class="badge bg-primary ms-1" title="Handed over — Verify on Device Detail, '
                'or Mark Complete verifies it">Handed Over</span>')
    return '<span class="badge bg-warning text-dark ms-1">Pending</span>'


def psiqc_parts_cell(barcode, prs) -> str:
    """Parts Requested column: clickable count + Pending / Handed Over /
    Verified badge."""
    if not prs:
        return '<span class="text-muted">—</span>'
    n = len(prs)
    badge = _parts_badge(prs)
    return (f'<span class="text-nowrap"><button type="button" class="btn btn-link btn-sm p-0 fw-bold psiqc-parts-btn" '
            f'data-barcode="{_esc(barcode)}" title="List the parts requested">{n}</button>{badge}</span>')


def psiqc_action_cell(barcode, wo, can_edit) -> str:
    """Action column: Start, or Mark Complete / Request Part / Cancel while
    started, or the Completed badge."""
    bc = _esc(barcode)
    if wo is not None and wo.status == "completed":
        when = wo.completed_at.strftime("%d-%m-%Y") if wo.completed_at else ""
        out = f'<span class="badge bg-success">Completed {when}</span>'
        if can_edit:
            out += (f'<button type="button" class="btn btn-sm btn-outline-primary py-0 ms-1 psiqc-start-btn" '
                    f'data-barcode="{bc}" title="Start a new Pre-Sale IQC on this tag">Start again</button>')
        return f'<div class="d-flex gap-1 flex-nowrap text-nowrap align-items-center">{out}</div>'
    started = wo is not None and wo.status == "in_progress"      # pending = queued, not started
    if not can_edit:
        return ('<span class="badge bg-info text-dark">Started</span>' if started
                else '<span class="text-muted">—</span>')
    if not started:
        return (f'<button type="button" class="btn btn-sm btn-primary psiqc-start-btn" data-barcode="{bc}">'
                '<i class="bi bi-play-fill"></i> Start</button>')
    who = _esc(wo.assigned_name or wo.assigned_username or "")
    return (
        '<div class="d-flex gap-1 flex-nowrap text-nowrap align-items-center">'
        f'<button type="button" class="btn btn-sm btn-success psiqc-complete-btn" data-barcode="{bc}" '
        f'title="Started by {who}">Mark Complete</button>'
        f'<a href="/devices/{bc}#parts" class="btn btn-sm" style="background-color:#ffc107;color:#000;border-color:#ffc107;" '
        'title="Request or verify parts on the tag\'s detail page"><i class="bi bi-bag-plus"></i> Request Part</a>'
        f'<button type="button" class="btn btn-sm btn-outline-danger psiqc-cancel-btn" data-barcode="{bc}" '
        'title="Cancel this Pre-Sale IQC (back to Start; part requests not yet handed over are cancelled)">'
        '<i class="bi bi-x-lg"></i></button>'
        '</div>')


async def _device(db, barcode) -> Device:
    d = (await db.execute(select(Device).where(Device.barcode == (barcode or "").strip()))).scalar_one_or_none()
    if d is None:
        raise HTTPException(404, "Tag not found")
    return d


async def _open_wo(db, device_id, statuses=("in_progress",)):
    """The tag's newest Pre-Sale IQC WorkID in `statuses` (default: started)."""
    return (await db.execute(
        select(WorkOrder).where(WorkOrder.device_id == device_id, WorkOrder.stage == STAGE_CODE,
                                WorkOrder.status.in_(statuses))
        .order_by(WorkOrder.created_at.desc())
    )).scalars().first()


# ── Page + feed ─────────────────────────────────────────────────────────────
@router.get("/pre-sale-iqc", response_class=HTMLResponse)
async def pre_sale_iqc_page(request: Request, db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(view_perm)):
    from routers.sales import _ready_page
    return await _ready_page(request, db, current_user, "sales/ready_list.html",
                             extra={"psiqc": True, "pm": MODULE, "psiqc_tiles": TILES})


@router.get("/pre-sale-iqc/data")
async def pre_sale_iqc_data(request: Request, draw: int = 1, start: int = 0, length: int = 25,
                            entity: str = "", touch: str = "", device_type: str = "",
                            availability: str = "", grade: str = "", tile: str = "", view: str = "",
                            db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(view_perm)):
    """Queued tags only; `view` = the tab (assigned / completed), `tile`
    (requested / verified …) narrows it further."""
    from routers.sales import ready_feed
    return await ready_feed(request, draw, start, length, entity, touch, device_type, availability,
                            grade, db, current_user, psiqc=True,
                            psiqc_tile=tile if tile in TILES else "",
                            psiqc_view=view if view in VIEWS else "")


# ── Assign to Pre-Sale (Ready for Sale Tags bulk action, 2026-10-29) ────────
@router.post("/pre-sale-iqc/assign", dependencies=[Depends(verify_csrf)])
async def pre_sale_iqc_assign(request: Request, barcodes: str = Form(""),
                              db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(get_current_user)):
    """Open to everyone who can see Ready for Sale (2026-10-30 — that page
    is itself any signed-in user), not only Pre-Sale IQC roles.
    Queue the ticked Ready for Sale tags for Pre-Sale IQC: one pending,
    unassigned PSQC- WorkID each. Tags already queued (pending / in
    progress) are skipped; a tag whose last Pre-Sale IQC is completed gets
    a new WorkID — a fresh start (back in the Assigned tab, no old parts).
    Nothing else about the tag changes."""
    from routers.repair import _create_work_order_with_unique_id
    bcs = _barcode_list(barcodes)
    devs = {}
    for i in range(0, len(bcs), 5000):
        for d in (await db.execute(select(Device).where(Device.barcode.in_(bcs[i:i + 5000])))).scalars().all():
            devs[d.barcode] = d
    queued = set()
    ids = [d.id for d in devs.values()]
    for i in range(0, len(ids), 5000):
        queued |= set((await db.execute(select(WorkOrder.device_id).where(
            WorkOrder.device_id.in_(ids[i:i + 5000]), WorkOrder.stage == STAGE_CODE,
            WorkOrder.status.in_(["pending", "in_progress"])))).scalars().all())
    now = app_now()
    out = {"assigned": [], "already": [], "not_ready": [], "not_found": []}
    for bc in bcs:
        d = devs.get(bc)
        if d is None:
            out["not_found"].append(bc)
        elif d.current_stage != DeviceStage.ready_to_sale:
            out["not_ready"].append(bc)
        elif d.id in queued:
            out["already"].append(bc)
        else:
            await _create_work_order_with_unique_id(
                db, WID_PREFIX, device_id=d.id, barcode=d.barcode, stage=STAGE_CODE,
                status="pending", assigned_at=now,
                requested_by_name=current_user.full_name or current_user.username,
                created_by=current_user.username)
            queued.add(d.id)
            out["assigned"].append(bc)
    if out["assigned"]:
        await audit(db, user=current_user, action="PRE_SALE_IQC_ASSIGNED", table_name="work_orders",
                    record_id=f"{len(out['assigned'])} tags",
                    new_value={"tags": out["assigned"][:500], "count": len(out["assigned"])}, request=request)
    await db.commit()
    return JSONResponse({"ok": True, **{k: len(v) for k, v in out.items()},
                         "not_ready_tags": out["not_ready"][:50], "not_found_tags": out["not_found"][:50]})


@router.get("/pre-sale-iqc/parts")
async def pre_sale_iqc_parts(barcode: str = Query(...), db: AsyncSession = Depends(get_db),
                             current_user: User = Depends(view_perm)):
    """Parts Requested modal: the tag's requests on its Pre-Sale IQC WorkIDs."""
    d = await _device(db, barcode)
    _state, parts = await psiqc_state_map(db, [d.id])
    status_label = {"requested": "Requested", "handed_over": "Handed Over", "received": "Verified",
                    "not_in_stock": "Not In Stock", "procure": "Sent for Sourcing"}
    return JSONResponse({"barcode": d.barcode, "parts": [{
        "part_name": p.part_name, "make": p.part_make or "", "model": p.part_model or "",
        "type": {"replace": "Replace", "new": "New", "faulty": "Faulty"}.get(p.request_type, p.request_type or ""),
        "qty": p.qty_handed_over or p.qty_requested or 1,
        "status": status_label.get(p.status, p.status), "verified": p.status == "received",
        "requested_by": p.engineer_name or p.requested_by or "",
        "requested_at": p.created_at.strftime("%d-%m-%Y %H:%M") if p.created_at else "",
    } for p in parts.get(d.id, [])]})


# ── Actions ─────────────────────────────────────────────────────────────────
@router.post("/pre-sale-iqc/start", dependencies=[Depends(verify_csrf)])
async def pre_sale_iqc_start(request: Request, barcode: str = Form(...),
                             db: AsyncSession = Depends(get_db),
                             current_user: User = Depends(edit_perm)):
    d = await _device(db, barcode)
    if d.current_stage != DeviceStage.ready_to_sale:
        return JSONResponse({"ok": False, "error": f"{d.barcode} is not in Ready for Sale."}, status_code=400)
    wo = await _open_wo(db, d.id, ("in_progress", "pending"))
    if wo is not None and wo.status == "pending":
        # Take the queued WorkID (2026-10-29).
        now = app_now()
        wo.status, wo.started_at, wo.assigned_at = "in_progress", now, now
        wo.assigned_user_id, wo.assigned_username = current_user.id, current_user.username
        wo.assigned_name = current_user.full_name
        wo.assigned_role = getattr(current_user.role, "value", current_user.role)
        await audit(db, user=current_user, action="PRE_SALE_IQC_STARTED", table_name="work_orders",
                    record_id=str(d.id), new_value={"barcode": d.barcode, "work_id": wo.work_id},
                    request=request)
        await db.commit()
    elif wo is None:
        from routers.repair import _create_work_order_with_unique_id
        now = app_now()
        wo = await _create_work_order_with_unique_id(
            db, WID_PREFIX, device_id=d.id, barcode=d.barcode, stage=STAGE_CODE,
            assigned_role=getattr(current_user.role, "value", current_user.role),
            assigned_user_id=current_user.id, assigned_username=current_user.username,
            assigned_name=current_user.full_name, status="in_progress",
            assigned_at=now, started_at=now,
            requested_by_name=current_user.full_name or current_user.username,
            created_by=current_user.username)
        await audit(db, user=current_user, action="PRE_SALE_IQC_STARTED", table_name="work_orders",
                    record_id=str(d.id), new_value={"barcode": d.barcode, "work_id": wo.work_id},
                    request=request)
        await db.commit()
    return JSONResponse({"ok": True, "work_id": wo.work_id})


@router.get("/pre-sale-iqc/complete-precheck")
async def pre_sale_iqc_precheck(barcode: str = Query(...), db: AsyncSession = Depends(get_db),
                                current_user: User = Depends(view_perm)):
    """Mark Complete's warning step — same lists as L1/L2's
    /repair/l1l2-complete-precheck, plus the open PNA parts that block it."""
    from models.pna_part import DevicePNAPart
    from routers.repair import _mark_complete_open_parts
    d = await _device(db, barcode)
    to_verify, to_cancel = await _mark_complete_open_parts(db, d.id)
    pna = (await db.execute(select(DevicePNAPart.part_name).where(
        DevicePNAPart.device_id == d.id, DevicePNAPart.is_active.is_(True)))).scalars().all()
    return JSONResponse({
        "pna": sorted(pna),
        "to_verify": [{"part_name": p.part_name, "qty": p.qty_handed_over} for p in to_verify],
        "to_cancel": [{"part_name": p.part_name} for p in to_cancel],
    })


async def _complete_one(db, request, current_user, d, wo, to_verify, to_cancel, now):
    """Close one tag's Pre-Sale IQC (no commit): verify handed-over parts,
    cancel never-handed-over requests, complete the WorkID, audit."""
    for pr in to_verify:
        pr.status, pr.actioned_at, pr.actioned_by = "received", now, current_user.username
    for pr in to_cancel:
        pr.status, pr.actioned_at, pr.actioned_by = "cancelled", now, current_user.username
    if to_verify:
        await audit(db, user=current_user, action="PART_AUTO_VERIFIED_ON_COMPLETE", table_name="part_requests",
                    record_id=str(d.id), new_value={"barcode": d.barcode, "from": "pre_sale_iqc",
                                                    "parts": [p.part_name for p in to_verify]}, request=request)
        # Same as Verify on Device Detail for a tag past Final QC: no Final QC
        # pass is coming, so verified RAM / Hard Drive changes are written onto
        # the tag's spec now (services/parts_changed.py) and show green.
        from services.parts_changed import apply_spec_changes_if_verified_late
        await db.flush()
        await apply_spec_changes_if_verified_late(db, d, current_user, request)
    if to_cancel:
        await audit(db, user=current_user, action="PART_AUTO_CANCELLED_ON_COMPLETE", table_name="part_requests",
                    record_id=str(d.id), new_value={"barcode": d.barcode, "from": "pre_sale_iqc",
                                                    "parts": [p.part_name for p in to_cancel]}, request=request)
    wo.status, wo.completed_at = "completed", now
    await audit(db, user=current_user, action="PRE_SALE_IQC_COMPLETED", table_name="work_orders",
                record_id=str(d.id), new_value={"barcode": d.barcode, "work_id": wo.work_id,
                                                "verified": len(to_verify), "cancelled": len(to_cancel)},
                request=request)


@router.post("/pre-sale-iqc/complete", dependencies=[Depends(verify_csrf)])
async def pre_sale_iqc_complete(request: Request, barcode: str = Form(...),
                                db: AsyncSession = Depends(get_db),
                                current_user: User = Depends(edit_perm)):
    """Same rules as L1/L2 Mark Complete (routers/repair.py
    l1l2_complete_to_stress): open PNA parts block it; handed-over parts are
    verified, never-handed-over requests cancelled. The tag stays in Ready
    for Sale; only the Pre-Sale IQC WorkID completes."""
    from models.pna_part import DevicePNAPart
    from routers.repair import _mark_complete_open_parts
    d = await _device(db, barcode)
    wo = await _open_wo(db, d.id)
    if wo is None:
        return JSONResponse({"ok": False, "error": "Start Pre-Sale IQC on this tag first."}, status_code=400)
    pna = (await db.execute(select(DevicePNAPart.part_name).where(
        DevicePNAPart.device_id == d.id, DevicePNAPart.is_active.is_(True)))).scalars().all()
    if pna:
        return JSONResponse({"ok": False, "error": (
            f"{d.barcode} has open PNA parts ({', '.join(sorted(pna))}) — remove them on the tag's "
            "detail page before Mark Complete.")}, status_code=400)
    to_verify, to_cancel = await _mark_complete_open_parts(db, d.id)
    await _complete_one(db, request, current_user, d, wo, to_verify, to_cancel, app_now())
    await db.commit()
    return JSONResponse({"ok": True, "verified": len(to_verify), "cancelled": len(to_cancel)})


# ── Bulk Complete (2026-10-29): the ticked tags, same rules and prompt ─────
def _barcode_list(raw) -> list:
    return list(dict.fromkeys(b.strip() for b in (raw or "").replace("\n", ",").split(",") if b.strip()))


async def _bulk_plan(db, barcodes):
    """Classify the ticked tags for Complete, in batches:
    ready = [(device, wo, to_verify, to_cancel)], plus the skipped groups."""
    from models.pna_part import DevicePNAPart
    devs = {}
    for i in range(0, len(barcodes), 5000):
        for d in (await db.execute(select(Device).where(Device.barcode.in_(barcodes[i:i + 5000])))).scalars().all():
            devs[d.barcode] = d
    ids = [d.id for d in devs.values()]
    wo_by, pna_by, prs_by = {}, {}, {}
    for i in range(0, len(ids), 5000):
        chunk = ids[i:i + 5000]
        for wo in (await db.execute(
            select(WorkOrder).where(WorkOrder.device_id.in_(chunk), WorkOrder.stage == STAGE_CODE,
                                    WorkOrder.status == "in_progress")
            .order_by(WorkOrder.created_at.asc()))).scalars().all():
            wo_by[wo.device_id] = wo                       # newest open wins
        for did, name in (await db.execute(select(DevicePNAPart.device_id, DevicePNAPart.part_name).where(
                DevicePNAPart.device_id.in_(chunk), DevicePNAPart.is_active.is_(True)))).all():
            pna_by.setdefault(did, []).append(name)
        for pr in (await db.execute(select(PartRequest).where(
                PartRequest.device_id.in_(chunk), PartRequest.status.in_(["handed_over", "requested"])))).scalars().all():
            prs_by.setdefault(pr.device_id, []).append(pr)
    plan = {"ready": [], "not_started": [], "pna": [], "not_found": []}
    for bc in barcodes:
        d = devs.get(bc)
        if d is None:
            plan["not_found"].append(bc)
        elif d.id not in wo_by:
            plan["not_started"].append(bc)
        elif pna_by.get(d.id):
            plan["pna"].append((bc, sorted(pna_by[d.id])))
        else:
            prs = prs_by.get(d.id, [])
            plan["ready"].append((d, wo_by[d.id], [p for p in prs if p.status == "handed_over"],
                                  [p for p in prs if p.status == "requested"]))
    return plan


@router.post("/pre-sale-iqc/bulk-complete-precheck", dependencies=[Depends(verify_csrf)])
async def pre_sale_iqc_bulk_precheck(barcodes: str = Form(""), db: AsyncSession = Depends(get_db),
                                     current_user: User = Depends(edit_perm)):
    plan = await _bulk_plan(db, _barcode_list(barcodes))
    return JSONResponse({
        "ready": [{"barcode": d.barcode, "to_verify": [f"{p.part_name} ×{p.qty_handed_over}" for p in v],
                   "to_cancel": [p.part_name for p in c]} for d, _wo, v, c in plan["ready"]],
        "not_started": plan["not_started"],
        "pna": [{"barcode": bc, "parts": parts} for bc, parts in plan["pna"]],
        "not_found": plan["not_found"],
    })


@router.post("/pre-sale-iqc/bulk-complete", dependencies=[Depends(verify_csrf)])
async def pre_sale_iqc_bulk_complete(request: Request, barcodes: str = Form(""),
                                     db: AsyncSession = Depends(get_db),
                                     current_user: User = Depends(edit_perm)):
    """Complete every ticked tag that is started and has no open PNA part —
    the same rules as the row's Mark Complete. Others are skipped and
    listed."""
    plan = await _bulk_plan(db, _barcode_list(barcodes))
    now = app_now()
    verified = cancelled = 0
    for d, wo, to_verify, to_cancel in plan["ready"]:
        await _complete_one(db, request, current_user, d, wo, to_verify, to_cancel, now)
        verified += len(to_verify)
        cancelled += len(to_cancel)
    await db.commit()
    return JSONResponse({"ok": True, "completed": len(plan["ready"]), "verified": verified,
                         "cancelled": cancelled, "not_started": len(plan["not_started"]),
                         "pna": len(plan["pna"]), "not_found": len(plan["not_found"])})


@router.post("/pre-sale-iqc/cancel", dependencies=[Depends(verify_csrf)])
async def pre_sale_iqc_cancel(request: Request, barcode: str = Form(...),
                              db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(edit_perm)):
    """Undo Start: the WorkID goes back to the queue (pending, no engineer)
    and its part requests never handed over are cancelled. Parts already
    handed over stay as they are (a physical part is out)."""
    d = await _device(db, barcode)
    wo = await _open_wo(db, d.id)
    if wo is None:
        return JSONResponse({"ok": True})
    now = app_now()
    res = await db.execute(
        update(PartRequest).where(PartRequest.work_order_id == wo.id, PartRequest.status == "requested")
        .values(status="cancelled", actioned_at=now, actioned_by=current_user.username))
    wo.status, wo.started_at = "pending", None
    wo.assigned_user_id = wo.assigned_username = wo.assigned_name = None
    await audit(db, user=current_user, action="PRE_SALE_IQC_CANCELLED", table_name="work_orders",
                record_id=str(d.id), new_value={"barcode": d.barcode, "work_id": wo.work_id,
                                                "requests_cancelled": res.rowcount or 0},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True, "cancelled": res.rowcount or 0})
