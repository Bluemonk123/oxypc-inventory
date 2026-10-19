"""
Spare Parts Router — double-entry ledger + negative stock guard + audit
"""
import csv
import io
import secrets
from templates_config import templates
from datetime import datetime
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, File, UploadFile, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from utils.csv_decode import decode_csv_bytes
from models.user import User, UserRole
from models.device import Device
from models.lot import Lot
from models.spare_parts import SparePart, SparePartPurchase, SparePartConsumption, RAMTracking
from models.parts_grn import PartsGRN, PartsGRNLineItem
from utils.master_data import master_values
from models.repair import RepairJob, RepairStatus
from models.engines import SparePartsLedger
from models.part_sales import PartSaleRequest
from models.part_request import PartRequest, PartSourcingRequest
from models.part_estimate import PartEstimate
from auth.dependencies import get_current_user, require_roles, verify_csrf, require_module_perm
from services.audit_engine import audit

router = APIRouter(tags=["spare_parts"], dependencies=[Depends(verify_csrf)])
allowed = require_roles(UserRole.admin, UserRole.spare_parts_manager,
                         UserRole.qc_inspector, UserRole.sales_manager)

BULK_CSV_HEADERS = ["part_name", "category", "part_brand", "part_model", "crate_number",
                    "physical_qty", "price", "invoice_number", "po_number", "vendor_name",
                    "grn_number"]
BULK_CSV_EXAMPLE = ["DDR4 8GB RAM", "RAM", "Samsung", "M471A1K43", "CR-001", "10",
                    "500", "Internal", "Internal", "Internal", "Internal"]


def _new_bulk_part_id() -> str:
    return f"{secrets.randbelow(90_000_000) + 10_000_000:08d}"


async def _next_part_code(db: AsyncSession) -> str:
    result = await db.execute(select(func.count(SparePart.id)))
    count  = (result.scalar() or 0) + 1
    return f"PART-{count:04d}"


async def _computed_stock(part_id, db: AsyncSession) -> int:
    """Derive stock from ledger rather than stored column."""
    in_result  = await db.execute(
        select(func.sum(SparePartsLedger.qty))
        .where(SparePartsLedger.part_id == part_id, SparePartsLedger.entry_type == "IN")
    )
    out_result = await db.execute(
        select(func.sum(SparePartsLedger.qty))
        .where(SparePartsLedger.part_id == part_id, SparePartsLedger.entry_type == "OUT")
    )
    stock_in   = in_result.scalar() or 0
    stock_out  = out_result.scalar() or 0
    return stock_in - stock_out


@router.get("/spare-parts", response_class=HTMLResponse)
async def parts_list(request: Request, db: AsyncSession = Depends(get_db),
                     current_user: User = Depends(allowed),
                     added_from: str = "", added_to: str = "",
                     category: str = "", part_name: str = "", engineer: str = ""):
    from datetime import date

    # Part master — `all_parts` (unfiltered) feeds Qty Available cross-lookups
    # below (group_stock/part_stock/stock_by_name), which must see every part
    # regardless of the current filter — a Part Request for "Battery" still
    # needs a real answer even when the filter bar says Category=RAM. `parts`
    # is the filtered view the Part Master table and its tiles actually use.
    result = await db.execute(
        select(SparePart).where(SparePart.is_trashed == False)
        .order_by(SparePart.category, SparePart.name)
    )
    all_parts = result.scalars().all()

    def _in_filter(cat, name, created_at):
        if category and (cat or "").strip().lower() != category.strip().lower():
            return False
        if part_name and (name or "").strip().lower() != part_name.strip().lower():
            return False
        if added_from:
            try:
                f = date.fromisoformat(added_from)
                if not created_at or created_at.date() < f:
                    return False
            except ValueError:
                pass
        if added_to:
            try:
                t = date.fromisoformat(added_to)
                if not created_at or created_at.date() > t:
                    return False
            except ValueError:
                pass
        return True

    parts = [p for p in all_parts if _in_filter(p.category, p.name, p.created_at)]

    # Grouping key for "the same physical part" — name+make+model, so a
    # request for one laptop model's RAM doesn't get pooled in with an
    # unrelated model's RAM just because Part Master happens to label both
    # rows "RAM". Two blank make/model rows still match each other (neither
    # field is mandatory on Part Master), which is deliberate: an
    # undifferentiated part still pools with its own undifferentiated
    # duplicates, it just never crosses into a differentiated one.
    def _group_key(p):
        return ((p.name or "").strip().lower(),
                (p.make or "").strip().lower(),
                (p.model or "").strip().lower())

    # "Consumed" per part — the same Qty Handover figure the Part Requests and
    # Faulty Request tables below print, summed per name+make+model group
    # rather than per part_id. Part Master accumulates several rows sharing
    # one name across repeated harvest/bulk uploads (e.g. four separate "RAM"
    # rows, each its own part_code), and a request is only ever tied to
    # whichever specific row happened to be picked at handover time — so
    # grouping by part_id showed each duplicate row only its own slice instead
    # of the true total for that part. Pooling by name ALONE over-corrected
    # this: a "RAM" row for one Make/Model was picking up handovers booked
    # against a completely different Make/Model's "RAM" row. Joining each
    # request's resolved part_id back to its SparePart (routers/part_requests.py
    # resolves a real part_id at request-creation time — see "BUG 3" there)
    # gives the make/model actually consumed against; requests whose part_id
    # never resolved to a live SparePart (deleted since, or predate that fix)
    # fall back to a blank-make/model bucket of their own rather than being
    # dropped or wrongly merged into a named group.
    #
    # Deliberately NOT filtered on status == "handed_over". Once an engineer
    # confirms receipt the row moves to "received" while still showing its
    # handover quantity, so the old filter dropped those from Consumed the
    # moment they were acknowledged — 9 units on live today — and silently
    # inflated In Stock by the same amount. Both request types are included
    # because faulty requests are the same table under request_type="faulty".
    consumed_rows = (await db.execute(
        select(PartRequest.part_name, SparePart.make, SparePart.model,
               func.sum(PartRequest.qty_handed_over))
        .outerjoin(SparePart, PartRequest.part_id == SparePart.id)
        .group_by(PartRequest.part_name, SparePart.make, SparePart.model)
    )).all()
    consumed_by_group: dict = {}
    for name, make, model, total in consumed_rows:
        key = ((name or "").strip().lower(),
               (make or "").strip().lower(),
               (model or "").strip().lower())
        consumed_by_group[key] = consumed_by_group.get(key, 0) + int(total or 0)
    consumed_by_part = {
        str(p.id): consumed_by_group.get(_group_key(p), 0) for p in all_parts
    }
    # Sum of every distinct name+make+model group's consumed total among the
    # FILTERED parts — not sum(consumed_by_part.values()) (would count a
    # group's total once per duplicate Part Master row sharing it), and not
    # every group in consumed_by_group (would ignore the current filter).
    _filtered_groups = {_group_key(p) for p in parts}
    total_consumed = sum(consumed_by_group.get(g, 0) for g in _filtered_groups)

    # "Sold" per part — Requested Quantity on every APPROVED row of Parts Sale
    # Request. Approval is what commits the stock, so it reserves the quantity
    # straight away rather than waiting for the sale to be raised against it.
    # This replaces spare_parts.sold_qty, which only moved once a sale was
    # actually completed and so left approved-but-unsold stock looking available.
    sold_rows = (await db.execute(
        select(PartSaleRequest.part_id, func.sum(PartSaleRequest.qty_requested))
        .where(PartSaleRequest.status == "approved",
               PartSaleRequest.part_id.isnot(None))
        .group_by(PartSaleRequest.part_id)
    )).all()
    sold_by_part = {str(pid): int(total or 0) for pid, total in sold_rows}

    # Live availability for one part — the single definition this page uses for
    # its tiles, its group/name stock rollups and the request tables' QTY
    # Available column, so none of them can disagree with the In Stock column
    # in the table, which computes the same subtraction in the template.
    #
    # Deducts BOTH consumed and sold: stock handed to an engineer and stock
    # committed by an approved sale request have equally left the shelf, and a
    # tile that counts either as available is telling Stores it can promise
    # something twice.
    def _live(p):
        return max(0, int(p.qty_in_stock or 0)
                   - consumed_by_part.get(str(p.id), 0)
                   - sold_by_part.get(str(p.id), 0))

    # Summary stats
    total_part_types = len(parts)
    below_min_count  = sum(1 for p in parts if p.qty_in_stock <= p.min_stock_alert)
    out_of_stock_count = sum(1 for p in parts if _live(p) <= 0)
    total_stock_value = sum(float(p.unit_price or 0) * int(p.qty_in_stock or 0) for p in parts)

    total_qty = sum(_live(p) for p in parts)
    total_new = sum(1 for p in parts if (p.source or "new") != "harvest")
    total_harvest = sum(1 for p in parts if p.source == "harvest")

    # part_requested_count is computed further down, once part_reqs/faulty_reqs
    # (themselves filtered the same way as parts) are built — see below.
    part_sourced_count = (await db.execute(
        select(func.count(PartSourcingRequest.id)).where(PartSourcingRequest.status == "open")
    )).scalar() or 0

    # Last 100 purchases (with part name + code)
    purchases_result = await db.execute(
        select(SparePartPurchase, SparePart.name, SparePart.part_code)
        .join(SparePart, SparePartPurchase.part_id == SparePart.id)
        .order_by(SparePartPurchase.purchase_date.desc())
        .limit(100)
    )
    purchases = purchases_result.all()

    # Last 100 consumptions (with part name + code + device barcode)
    consumptions_result = await db.execute(
        select(SparePartConsumption, SparePart.name, SparePart.part_code, Device.barcode)
        .join(SparePart, SparePartConsumption.part_id == SparePart.id)
        .outerjoin(Device, SparePartConsumption.device_id == Device.id)
        .order_by(SparePartConsumption.used_at.desc())
        .limit(100)
    )
    consumptions = consumptions_result.all()

    # ── Parts Consumption tab (per tag number x part name): every PartRequest
    # row whose Action flipped to "Part Changed" (status == "received"), same
    # definition routers/devices.py uses for the per-device Parts Consumed
    # table on Device Detail — rolled up here by (device, part) so a tag that
    # had e.g. Keyboard changed once and RAM changed twice shows as two rows,
    # one per part name, each with its own quantity and price (2026-09-18;
    # previously rolled up by device alone into one row with device-wide
    # totals, which couldn't show which part cost what).
    #
    # "Date Added" (2026-09-18) = PartRequest.actioned_at, the moment the
    # engineer hit Verify on Device Detail's Parts Consumed table and status
    # flipped to "received" (see routers/part_requests.py::validate_receiving)
    # — the same global Added From/To filter now applies here too, against
    # that per-row timestamp (the MAX one contributing to a rolled-up row,
    # when several handovers of the same part landed on different dates).
    changed_rows = (await db.execute(
        select(PartRequest, Device.barcode, Lot.lot_number, Device.brand, Device.model)
        .join(Device, PartRequest.device_id == Device.id)
        .outerjoin(Lot, Device.lot_id == Lot.id)
        .where(PartRequest.status == "received")
    )).all()
    changed_part_ids = {r.part_id for r, _, _, _, _ in changed_rows if r.part_id}
    changed_sp_by_id = {}
    if changed_part_ids:
        changed_sp_by_id = {
            sp.id: sp for sp in (await db.execute(
                select(SparePart).where(SparePart.id.in_(changed_part_ids))
            )).scalars().all()
        }

    def _pc_in_date_range(actioned_at):
        if added_from:
            try:
                f = date.fromisoformat(added_from)
                if not actioned_at or actioned_at.date() < f:
                    return False
            except ValueError:
                pass
        if added_to:
            try:
                t = date.fromisoformat(added_to)
                if not actioned_at or actioned_at.date() > t:
                    return False
            except ValueError:
                pass
        return True

    tag_consumption = {}
    for r, barcode, lot_number, tag_make, tag_model in changed_rows:
        if not _pc_in_date_range(r.actioned_at):
            continue
        sp = changed_sp_by_id.get(r.part_id)
        # Category / Part Name / Engineer apply here too (2026-10-19), so the
        # global filter bar covers every tab. Date stays the Verify date above.
        if category and ((sp.category if sp else r.part_category) or "").strip().lower() != category.strip().lower():
            continue
        if part_name and ((sp.name if sp else r.part_name) or "").strip().lower() != part_name.strip().lower():
            continue
        if engineer and (r.engineer_name or r.requested_by or "").strip().lower() != engineer.strip().lower():
            continue
        unit_price = float(sp.unit_price) if sp else 0.0
        # NOT `part_name` — that's this route's own Part Name FILTER query
        # param (see the function signature), and reusing the name here
        # silently clobbered it with whichever row happened to be last in
        # `changed_rows`, which is what the Part Name dropdown was echoing
        # back as "selected" regardless of what was actually picked (or on
        # a totally filter-less page load).
        consumed_part_name = (sp.name if sp else r.part_name) or "—"
        qty = r.qty_handed_over or 0
        key = (barcode, r.part_id or consumed_part_name)
        row = tag_consumption.setdefault(key, {
            "tag_number": barcode, "lot_number": lot_number,
            "tag_make": tag_make or "—", "tag_model": tag_model or "—",
            "part_name": consumed_part_name,
            "unit_price": unit_price, "total_qty": 0, "total_price": 0.0, "date_added": None,
        })
        row["total_qty"] += qty
        row["total_price"] += unit_price * qty
        if r.actioned_at and (row["date_added"] is None or r.actioned_at > row["date_added"]):
            row["date_added"] = r.actioned_at
    tag_consumption_rows = sorted(tag_consumption.values(),
                                  key=lambda r: (r["tag_number"] or "", r["part_name"] or ""))
    tag_consumption_tag_count = len({r["tag_number"] for r in tag_consumption_rows})

    # Parts consumed this month (count)
    today = date.today()
    consumed_this_month = sum(
        1 for c, *_ in consumptions
        if c.used_at and c.used_at.year == today.year and c.used_at.month == today.month
    )

    # ── Part requests raised by engineers (#11/#14) ──────────────────────────
    # "Faulty" button on Device Detail raises request_type='faulty' — those
    # land in their own Faulty Request tab instead of the normal Part Requests
    # tab (New/Replace requests only).
    all_part_reqs_unfiltered = (await db.execute(
        select(PartRequest).order_by(PartRequest.created_at.desc())
    )).scalars().all()
    # Every distinct engineer who has ever raised a request — sourced from
    # the fully unfiltered set so the dropdown's own option list never
    # shrinks just because another filter (category/date/engineer itself) is
    # already applied.
    engineer_options = sorted({
        (r.engineer_name or r.requested_by or "").strip()
        for r in all_part_reqs_unfiltered
        if (r.engineer_name or r.requested_by or "").strip()
    })
    # Filtered the same way as parts (category/part name/added-date), so the
    # global filter bar's stated scope — "Part Master, Part Requests and
    # Faulty Request" — actually holds for these two tables too.
    all_part_reqs = [
        r for r in all_part_reqs_unfiltered
        if _in_filter(r.part_category, r.part_name, r.created_at)
    ]
    # Engineer is its own filter, applied only here — Part Master rows have
    # no engineer to match against, so this deliberately isn't folded into
    # _in_filter (which parts/all_part_reqs share above). Without this,
    # "who does dilip_kumar actually still need parts for" was unanswerable
    # on this page: the tab showed every request ever raised, any engineer,
    # any status, any device, forever (see the 2026-09-14 investigation into
    # his 33-tag queue vs. 45 unrelated historical rows).
    if engineer:
        all_part_reqs = [
            r for r in all_part_reqs
            if (r.engineer_name or r.requested_by or "").strip().lower() == engineer.strip().lower()
        ]
    part_reqs = [r for r in all_part_reqs if r.request_type != "faulty"]
    faulty_reqs = [r for r in all_part_reqs if r.request_type == "faulty"]
    part_requested_count = sum(1 for r in all_part_reqs if r.status == "requested")

    # Qty Available on the Part Requests / Faulty Requests tables sums the New
    # and Harvest rows for the same physical part. Part Master keeps them as
    # separate rows (different `source`), but an engineer asking for a Keyboard
    # can be given either, so showing only the matched row's stock understated
    # what is actually on the shelf. Grouped on name+make+model so it stays a
    # like-for-like total rather than lumping every "Panel" together.
    #
    # Built from all_parts (not the filtered `parts`) — Qty Available has to
    # answer correctly for every request regardless of what the Category/Part
    # Name filter currently shows, or a filtered-out part's own request rows
    # would read as having zero stock. (_group_key defined earlier, above the
    # Consumed calculation, which needs the same name+make+model grouping.)
    group_stock: dict = {}
    for p in all_parts:
        group_stock[_group_key(p)] = group_stock.get(_group_key(p), 0) + _live(p)

    part_stock = {str(p.id): group_stock.get(_group_key(p), 0) for p in all_parts}

    # Qty Available is resolved by part NAME, not by the request's stored
    # part_id. 88% of live part_requests carry a part_id that is NULL or points
    # at a since-trashed Part Master row, so keying on the id showed 0 for
    # almost every request. Summing every active row with the same name also
    # gives the New + Harvest total the page is meant to show.
    stock_by_name: dict = {}
    for p in all_parts:
        k = (p.name or "").strip().lower()
        if k:
            stock_by_name[k] = stock_by_name.get(k, 0) + _live(p)
    part_meta = {
        str(p.id): {"crate": p.crate_number, "make": p.make, "model": p.model}
        for p in all_parts
    }

    # ── Pending part-sourcing requests, mirrored read-only from CRM (#15) ────
    sourcing = (await db.execute(
        select(PartSourcingRequest).order_by(PartSourcingRequest.created_at.desc())
    )).scalars().all()
    # Global filter bar applies here too (2026-10-19): Added date = request
    # created, Part Name = the request's part, Category = its Part Master row.
    # Engineer has nothing to match on a sourcing request, so it is ignored.
    if category or part_name or added_from or added_to:
        _cat_by_part = {p.id: p.category for p in all_parts}
        sourcing = [s for s in sourcing
                    if _in_filter(_cat_by_part.get(s.part_id), s.part_name, s.created_at)]

    # ── Every estimate file from the same Generate click as each production
    # request — Part Estimate can write several files in one click (one per
    # Grade selected), all sharing the exact created_at stamp and lot_id, but
    # only ONE PartSourcingRequest is raised per click. Group by that shared
    # timestamp so the Part Name column can list every file from the batch
    # instead of the single one the request happens to be linked to.
    lot_ids = {s.lot_id for s in sourcing if s.source == "production" and s.lot_id}
    estimates_by_batch = {}
    if lot_ids:
        est_rows = (await db.execute(
            select(PartEstimate).where(
                PartEstimate.lot_id.in_(lot_ids), PartEstimate.file_name.isnot(None))
        )).scalars().all()
        by_lot_and_time = {}
        for e in est_rows:
            by_lot_and_time.setdefault((e.lot_id, e.created_at), []).append(e)
        for s in sourcing:
            if s.source == "production" and s.lot_id:
                estimates_by_batch[str(s.id)] = by_lot_and_time.get((s.lot_id, s.created_at), [])

    # map source_deal_id (UUID string) -> CRMSourcingDeal for download links + display
    from models.crm import CRMSourcingDeal
    deal_map = {}
    valid_ids = []
    for s in sourcing:
        if s.source_deal_id:
            try:
                valid_ids.append(str(__import__("uuid").UUID(s.source_deal_id)))
            except (ValueError, AttributeError, TypeError):
                continue
    if valid_ids:
        dm_r = await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id.in_(valid_ids)))
        for d in dm_r.scalars().all():
            deal_map[str(d.id)] = d

    # ── GRN mapping for Part Master (#new): PartsGRNLineItem.part_id stores the
    # SparePart.part_code (see routers/parts_grn.py _new_part_id upsert). Map
    # each part_code to its most recent GRN line item + parent GRN number.
    part_codes = [p.part_code for p in parts if p.part_code]
    grn_by_part_code = {}
    if part_codes:
        li_rows = (await db.execute(
            select(PartsGRNLineItem, PartsGRN.grn_number)
            .join(PartsGRN, PartsGRNLineItem.grn_id == PartsGRN.id)
            .where(PartsGRNLineItem.part_id.in_(part_codes))
            .order_by(PartsGRN.date_received.desc().nullslast())
        )).all()
        for li, grn_number in li_rows:
            if li.part_id not in grn_by_part_code:
                grn_by_part_code[li.part_id] = {
                    "grn_id": str(li.grn_id), "grn_number": grn_number,
                    "is_harvest": li.is_harvest,
                }

    return templates.TemplateResponse("spare_parts/list.html", {
        "request": request, "parts": parts, "current_user": current_user,
        "purchases": purchases, "consumptions": consumptions,
        "stock_by_name": stock_by_name,
        "total_part_types": total_part_types,
        "total_qty": total_qty,
        "total_new": total_new,
        "total_harvest": total_harvest,
        "below_min_count": below_min_count,
        "out_of_stock_count": out_of_stock_count,
        "part_requested_count": part_requested_count,
        "part_sourced_count": part_sourced_count,
        "total_stock_value": total_stock_value,
        "consumed_this_month": consumed_this_month,
        "total_consumed": total_consumed,
        "tag_consumption_rows": tag_consumption_rows,
        "tag_consumption_tag_count": tag_consumption_tag_count,
        "consumed_by_part": consumed_by_part,
        "sold_by_part": sold_by_part,
        "part_reqs": part_reqs, "faulty_reqs": faulty_reqs, "part_stock": part_stock,
        "part_meta": part_meta, "sourcing": sourcing,
        "deal_map": deal_map, "estimates_by_batch": estimates_by_batch,
        "grn_docs": {},
        "grn_by_part_code": grn_by_part_code,
        "harvest_categories": await master_values(db, "iqc_part_category"),
        # Spare Part Names / Spare Part Brands for the Add Harvest Part modal.
        "part_names": await master_values(db, "part_category"),
        "part_brands": await master_values(db, "spare_part_brand"),
        # Echoed back into the global filter bar's own fields so they show
        # what's actually applied after the Filter button reloads the page.
        "added_from": added_from, "added_to": added_to,
        "filter_category": category, "filter_part_name": part_name,
        "filter_engineer": engineer, "engineer_options": engineer_options,
    })


@router.get("/spare-parts/new", response_class=HTMLResponse)
async def new_part_form(request: Request, db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(allowed)):
    next_code = await _next_part_code(db)
    return templates.TemplateResponse("spare_parts/part_form.html", {
        "request": request, "next_code": next_code, "categories": await master_values(db, "iqc_part_category"),
        "current_user": current_user, "error": None,
    })


@router.post("/spare-parts/new")
async def create_part(
    request: Request,
    part_code: str = Form(...),
    name: str = Form(...),
    category: str = Form(...),
    unit_price: str = Form("0"),
    min_stock_alert: int = Form(0),
    supplier: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
    _perm: User = Depends(require_module_perm("spare_parts", "add")),
):
    part = SparePart(part_code=part_code, name=name, category=category,
                     unit_price=float(unit_price), min_stock_alert=min_stock_alert,
                     supplier=supplier or None, notes=notes or None, source='new')
    db.add(part)
    await db.commit()
    return RedirectResponse(url="/spare-parts?success=Part+added", status_code=302)


@router.get("/spare-parts/{part_id}/edit", response_class=HTMLResponse)
async def edit_part_form(part_id: str, request: Request,
                         db: AsyncSession = Depends(get_db),
                         current_user: User = Depends(allowed)):
    result = await db.execute(select(SparePart).where(SparePart.id == part_id))
    part = result.scalar_one_or_none()
    if not part:
        raise HTTPException(404, "Part not found")
    return templates.TemplateResponse("spare_parts/edit_form.html", {
        "request": request, "part": part, "categories": await master_values(db, "iqc_part_category"),
        # Spare Part Names / Spare Part Brands master dropdowns, wired to Part
        # Name and Part Make. part.name / part.make are injected as a fallback
        # option in the template when not present in these lists, so an older
        # row's value is never silently blanked by a select that doesn't
        # contain it.
        "part_names": await master_values(db, "part_category"),
        "part_brands": await master_values(db, "spare_part_brand"),
        "current_user": current_user, "error": None,
    })


@router.post("/spare-parts/{part_id}/edit")
async def update_part(
    part_id: str,
    name: str = Form(...),
    category: str = Form(...),
    unit_price: str = Form("0"),
    qty_in_stock: int = Form(None),
    min_stock_alert: int = Form(0),
    supplier: str = Form(""),
    notes: str = Form(""),
    part_lot: str = Form(""),
    crate_number: str = Form(""),
    make: str = Form(""),
    model: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    result = await db.execute(select(SparePart).where(SparePart.id == part_id))
    part = result.scalar_one_or_none()
    if not part:
        raise HTTPException(404, "Part not found")
    part.name = name; part.category = category; part.unit_price = float(unit_price)
    part.min_stock_alert = min_stock_alert; part.supplier = supplier or None; part.notes = notes or None
    # Lot/make/model are optional and blank-means-clear, so an operator can
    # correct a wrong make by emptying the box rather than typing a placeholder.
    part.part_lot = part_lot.strip() or None
    part.crate_number = crate_number.strip() or None
    part.make = make.strip() or None
    part.model = model.strip() or None
    if qty_in_stock is not None:
        new_qty = max(0, int(qty_in_stock))
        old_qty = int(part.qty_in_stock or 0)
        delta = new_qty - old_qty
        if delta != 0:
            # Write a compensating ledger entry so _computed_stock() (ledger-based,
            # used by the negative-stock guard in record_consumption) stays in sync
            # with qty_in_stock (column-based, used by Part Master + Part Request
            # display). Previously this edit path overwrote qty_in_stock directly
            # with no ledger entry, letting the two diverge over time.
            db.add(SparePartsLedger(
                part_id=part.id,
                entry_type="IN" if delta > 0 else "OUT",
                qty=abs(delta),
                cost_per_unit=float(part.unit_price),
                total_cost=abs(delta) * float(part.unit_price),
                reference_type="adjustment",
                reference_id=None,
                created_by=current_user.username,
                notes=f"Part Master manual stock adjustment: {old_qty} -> {new_qty}",
            ))
            await audit(db, action="PART_STOCK_ADJUSTED", user=current_user,
                        table_name="spare_parts_ledger", record_id=str(part.id),
                        notes=f"Adjust {old_qty} -> {new_qty} ({'+' if delta>0 else ''}{delta})")
        part.qty_in_stock = new_qty
    await db.commit()
    return RedirectResponse(url="/spare-parts?success=Part+updated", status_code=302)


@router.post("/spare-parts/{part_id}/set-type")
async def set_part_type(part_id: str, request: Request,
                        part_type: str = Form(""),
                        db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(allowed)):
    """Inline Type select on Part Master (New/Replace/Upgrade/Downgrade). Called via
    fetch() from the table row, so it returns JSON rather than redirecting."""
    result = await db.execute(select(SparePart).where(SparePart.id == part_id))
    part = result.scalar_one_or_none()
    if not part:
        return JSONResponse({"ok": False, "error": "Part not found"}, status_code=404)
    valid = {"New", "Replace", "Upgrade", "Downgrade", ""}
    if part_type not in valid:
        return JSONResponse({"ok": False, "error": "Invalid type"}, status_code=400)
    part.part_type = part_type or None
    await audit(db, action="PART_TYPE_SET", user=current_user,
                table_name="spare_parts", record_id=str(part.id),
                notes=f"Type -> {part_type or '(cleared)'}")
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/spare-parts/{part_id}/delete")
async def delete_part(part_id: str, request: Request,
                      db: AsyncSession = Depends(get_db),
                      current_user: User = Depends(allowed)):
    result = await db.execute(select(SparePart).where(SparePart.id == part_id))
    part = result.scalar_one_or_none()
    if not part:
        raise HTTPException(404, "Part not found")
    part.is_trashed = True
    part.trashed_at = app_now()
    await audit(db, action="PART_MASTER_DELETE", user=current_user,
                table_name="spare_parts", record_id=str(part.id))
    await db.commit()
    return RedirectResponse(url="/spare-parts?success=Part+deleted", status_code=302)


@router.post("/spare-parts/bulk-delete")
async def bulk_delete_parts(request: Request, ids: list[str] = Form([]),
                            db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(allowed)):
    deleted = 0
    for part_id in ids:
        result = await db.execute(select(SparePart).where(SparePart.id == part_id))
        part = result.scalar_one_or_none()
        if not part:
            continue
        part.is_trashed = True
        part.trashed_at = app_now()
        deleted += 1
    if deleted:
        await audit(db, action="PART_MASTER_BULK_DELETE", user=current_user,
                    table_name="spare_parts",
                    notes=f"Bulk soft-deleted {deleted} part(s): {', '.join(ids)}")
        await db.commit()
    return {"deleted": deleted}


@router.get("/spare-parts/bulk-template")
async def download_bulk_template(current_user: User = Depends(get_current_user)):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(BULK_CSV_HEADERS)
    writer.writerow(BULK_CSV_EXAMPLE)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=part_master_bulk_template.csv"},
    )


@router.post("/spare-parts/bulk-upload")
async def bulk_upload_parts(
    request: Request,
    file: UploadFile = File(...),
    source: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Bulk-add Part Master rows from CSV via Download Sample / Upload New /
    Upload Harvest on the Parts Dashboard. Mirrors the manual Part GRN 'Add
    Item' flow: creates a PartsGRN header for the batch, a PartsGRNLineItem
    per row, and upserts the row into SparePart with the given source
    ('new' or 'harvest')."""
    if source not in ("new", "harvest"):
        raise HTTPException(400, "Invalid source")

    content = await file.read()
    text = decode_csv_bytes(content)

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return RedirectResponse(url="/spare-parts?error=No+rows+found+in+file", status_code=302)

    grn_number = f"BULK-{secrets.randbelow(900_000) + 100_000}"
    grn = PartsGRN(
        grn_number=grn_number,
        po_number="Internal" if source == "harvest" else None,
        vendor_name="Internal" if source == "harvest" else None,
        invoice_number="Internal" if source == "harvest" else None,
        created_by=current_user.username,
    )
    db.add(grn)
    await db.flush()

    inserted, errors = 0, []
    for i, row in enumerate(rows, start=2):
        try:
            part_name = (row.get("part_name") or "").strip()
            if not part_name:
                errors.append(f"Row {i}: part_name is required")
                continue
            part_id = _new_bulk_part_id()
            qty = int((row.get("physical_qty") or "0").strip() or 0)
            price_raw = (row.get("price") or "").strip()
            price = float(price_raw) if price_raw else 0.0
            category = (row.get("category") or "Other").strip() or "Other"
            invoice_number = (row.get("invoice_number") or "Internal").strip() or "Internal"
            po_number = (row.get("po_number") or "Internal").strip() or "Internal"
            vendor_name = (row.get("vendor_name") or "Internal").strip() or "Internal"
            row_grn_number = (row.get("grn_number") or "Internal").strip() or "Internal"
            make = (row.get("part_brand") or "").strip() or None
            model = (row.get("part_model") or "").strip() or None
            crate_number = (row.get("crate_number") or "").strip() or None

            db.add(PartsGRNLineItem(
                grn_id=grn.id,
                part_id=part_id,
                grn_number=row_grn_number,
                po_number=po_number,
                vendor_name=vendor_name,
                invoice_number=invoice_number,
                part_name=part_name,
                part_brand=make,
                part_model=model,
                invoice_qty=qty,
                physical_qty=qty,
                price=price,
                category=category,
                is_harvest=(source == "harvest"),
            ))

            match_q = select(SparePart).where(
                func.lower(SparePart.name) == part_name.lower()
            )
            if make:
                match_q = match_q.where(func.lower(SparePart.make) == make.lower())
            else:
                match_q = match_q.where(SparePart.make.is_(None))
            if model:
                match_q = match_q.where(func.lower(SparePart.model) == model.lower())
            else:
                match_q = match_q.where(SparePart.model.is_(None))
            existing_part = (await db.execute(match_q)).scalars().first()
            if existing_part:
                # Part Master bulk upload is a master-data load, not a goods
                # receipt: In Stock is set to exactly what the file says. It
                # previously accumulated (+= qty), so re-uploading the same
                # file multiplied stock by the number of uploads.
                existing_part.qty_in_stock = qty
                if crate_number:
                    existing_part.crate_number = crate_number
                if price_raw:
                    existing_part.unit_price = price
                existing_part.is_trashed = False
                existing_part.trashed_at = None
            else:
                db.add(SparePart(
                    part_code=part_id, name=part_name, category=category,
                    unit_price=price, qty_in_stock=qty, min_stock_alert=0,
                    supplier=vendor_name, source=source,
                    make=make, model=model, crate_number=crate_number,
                ))
            inserted += 1
        except Exception as e:
            errors.append(f"Row {i}: {str(e)}")

    await audit(db, action="PART_MASTER_BULK_UPLOAD", user=current_user,
                table_name="spare_parts", record_id=str(grn.id),
                new_value={"source": source, "inserted": inserted, "errors": len(errors)})
    await db.commit()
    return templates.TemplateResponse("bulk_upload/result.html", {
        "request": request, "current_user": current_user,
        "upload_type": f"Part Master ({'Harvest' if source == 'harvest' else 'New'})",
        "inserted": inserted, "errors": errors,
        "back_url": "/spare-parts",
    })


@router.get("/spare-parts/purchase", response_class=HTMLResponse)
async def purchase_log(request: Request, db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(allowed)):
    result = await db.execute(
        select(SparePartPurchase, SparePart.name, SparePart.part_code)
        .join(SparePart, SparePartPurchase.part_id == SparePart.id)
        .order_by(SparePartPurchase.purchase_date.desc())
    )
    purchases   = result.all()
    parts_result= await db.execute(select(SparePart).order_by(SparePart.name))
    parts       = parts_result.scalars().all()
    return templates.TemplateResponse("spare_parts/purchase.html", {
        "request": request, "purchases": purchases, "parts": parts, "current_user": current_user,
    })


@router.post("/spare-parts/purchase")
async def record_purchase(
    request: Request,
    part_id: str = Form(...),
    qty: int = Form(...),
    unit_price: str = Form(...),
    supplier: str = Form(""),
    invoice_no: str = Form(""),
    purchase_date: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    total = float(unit_price) * qty
    purchase = SparePartPurchase(
        part_id=part_id, qty=qty, unit_price=float(unit_price), total_price=total,
        supplier=supplier or None, invoice_no=invoice_no or None,
        purchase_date=datetime.strptime(purchase_date, "%Y-%m-%d"),
        purchased_by=current_user.username,
    )
    db.add(purchase)

    # ── Ledger entry: IN ──────────────────────────────────────────────────
    db.add(SparePartsLedger(
        part_id=part_id, entry_type="IN", qty=qty,
        cost_per_unit=float(unit_price), total_cost=total,
        reference_type="purchase", reference_id=None,
        created_by=current_user.username,
    ))

    # Keep qty_in_stock in sync (for read performance)
    result = await db.execute(select(SparePart).where(SparePart.id == part_id))
    part = result.scalar_one_or_none()
    if part:
        part.qty_in_stock += qty
        part.unit_price   = float(unit_price)

    log = await audit(db, action="PARTS_PURCHASED", user=current_user,
                      table_name="spare_parts_ledger",
                      notes=f"IN {qty}x part {part_id} @ {unit_price}")
    db.add(log)

    await db.commit()
    return RedirectResponse(url="/spare-parts/purchase?success=Purchase+recorded", status_code=302)


@router.get("/spare-parts/consume", response_class=HTMLResponse)
async def consume_log(request: Request, db: AsyncSession = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    result = await db.execute(
        select(SparePartConsumption, SparePart.name, Device.barcode)
        .join(SparePart, SparePartConsumption.part_id == SparePart.id)
        .outerjoin(Device, SparePartConsumption.device_id == Device.id)
        .order_by(SparePartConsumption.used_at.desc())
    )
    consumptions = result.all()
    parts_result = await db.execute(
        select(SparePart).where(SparePart.qty_in_stock > 0).order_by(SparePart.name)
    )
    parts      = parts_result.scalars().all()
    lots_result= await db.execute(select(Lot).order_by(Lot.lot_number))
    lots       = lots_result.scalars().all()
    return templates.TemplateResponse("spare_parts/consume.html", {
        "request": request, "consumptions": consumptions, "parts": parts,
        "lots": lots, "current_user": current_user,
    })


@router.post("/spare-parts/consume")
async def record_consumption(
    request: Request,
    part_id: str = Form(...),
    qty_used: int = Form(...),
    device_barcode: str = Form(""),
    lot_id: str = Form(""),
    stage: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    part_result = await db.execute(select(SparePart).where(SparePart.id == part_id))
    part = part_result.scalar_one_or_none()
    if not part:
        raise HTTPException(404, "Part not found")

    # ── Negative stock guard (BR-05 / BR-16) ─────────────────────────────
    current_stock = await _computed_stock(part_id, db)
    if current_stock < qty_used:
        raise HTTPException(
            409,
            f"INVENTORY ENGINE: Insufficient stock — available {current_stock}, "
            f"requested {qty_used}. Consumption blocked."
        )

    device_id = None
    repair_job_id = None
    if device_barcode:
        dev_result = await db.execute(select(Device).where(Device.barcode == device_barcode))
        dev = dev_result.scalar_one_or_none()
        if dev:
            device_id = dev.id
            # Auto-link to the open repair job for this device (if any)
            job_result = await db.execute(
                select(RepairJob)
                .where(
                    RepairJob.device_id == dev.id,
                    RepairJob.status == RepairStatus.in_progress,
                )
                .order_by(RepairJob.started_at.desc())
                .limit(1)
            )
            open_job = job_result.scalars().first()
            if open_job:
                repair_job_id = open_job.id

    total = float(part.unit_price) * qty_used
    consumption = SparePartConsumption(
        part_id=part_id, qty_used=qty_used,
        unit_cost=float(part.unit_price), total_cost=total,
        device_id=device_id,
        repair_job_id=repair_job_id,
        lot_id=lot_id or None, stage=stage or None,
        used_by=current_user.username, notes=notes or None,
    )
    db.add(consumption)

    # ── Ledger entry: OUT ─────────────────────────────────────────────────
    db.add(SparePartsLedger(
        part_id=part_id, entry_type="OUT", qty=qty_used,
        cost_per_unit=float(part.unit_price), total_cost=total,
        reference_type="device_repair",
        reference_id=str(device_id) if device_id else None,
        device_id=device_id,
        created_by=current_user.username,
        notes=notes or None,
    ))

    # Keep qty_in_stock in sync
    part.qty_in_stock = max(0, part.qty_in_stock - qty_used)

    # ── Update device_costing if device is known ──────────────────────────
    if device_id:
        from services.cost_engine import refresh_parts_cost
        dev_r = await db.execute(select(Device).where(Device.id == device_id))
        dev   = dev_r.scalar_one_or_none()
        if dev:
            await refresh_parts_cost(dev, db)

    log = await audit(db, action="PARTS_CONSUMED", user=current_user,
                      table_name="spare_parts_ledger",
                      notes=f"OUT {qty_used}x {part.name} for device {device_barcode or 'N/A'}")
    db.add(log)

    await db.commit()
    return RedirectResponse(url="/spare-parts/consume?success=Consumption+recorded", status_code=302)


@router.get("/ram-tracking", response_class=HTMLResponse)
async def ram_list(request: Request, db: AsyncSession = Depends(get_db),
                   current_user: User = Depends(allowed)):
    result = await db.execute(
        select(RAMTracking, Device.barcode)
        .outerjoin(Device, RAMTracking.device_id == Device.id)
        .order_by(RAMTracking.at.desc())
    )
    entries = result.all()
    devices_result = await db.execute(select(Device).order_by(Device.barcode))
    devices = devices_result.scalars().all()
    return templates.TemplateResponse("spare_parts/ram.html", {
        "request": request, "entries": entries, "devices": devices, "current_user": current_user,
    })


@router.post("/ram-tracking")
async def record_ram(
    action: str = Form(...),
    device_barcode: str = Form(""),
    destination_barcode: str = Form(""),
    ram_gb: int = Form(...),
    ram_type: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    device_id = None; dest_id = None
    if device_barcode:
        r = await db.execute(select(Device).where(Device.barcode == device_barcode))
        d = r.scalar_one_or_none()
        if d:
            device_id = d.id
            if action == "removed":
                d.ram_gb = max(0, (d.ram_gb or 0) - ram_gb)
    if destination_barcode:
        r = await db.execute(select(Device).where(Device.barcode == destination_barcode))
        d = r.scalar_one_or_none()
        if d:
            dest_id = d.id
            if action in ("added", "cannibalized"):
                d.ram_gb = (d.ram_gb or 0) + ram_gb
    db.add(RAMTracking(action=action, device_id=device_id, destination_device_id=dest_id,
                       ram_gb=ram_gb, ram_type=ram_type or None,
                       by_user=current_user.username, notes=notes or None))
    await db.commit()
    return RedirectResponse(url="/ram-tracking?success=RAM+logged", status_code=302)


@router.post("/spare-parts/{part_id}/procure")
async def procure_from_master(part_id: str, request: Request,
                              db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(allowed)):
    import uuid as _uuid
    try:
        uid = _uuid.UUID(part_id)
    except ValueError:
        raise HTTPException(404, "Part not found")
    part = (await db.execute(select(SparePart).where(SparePart.id == uid))).scalar_one_or_none()
    if not part:
        raise HTTPException(404, "Part not found")
    db.add(PartSourcingRequest(
        part_id=part.id,
        part_code=part.part_code,
        part_name=part.name,
        qty_requested=1,
        raised_by=current_user.username,
        status="open",
    ))
    await audit(db, action="PART_MASTER_PROCURE", user=current_user,
                table_name="part_sourcing_requests", record_id=str(part.id))
    await db.commit()
    return RedirectResponse(url="/spare-parts?success=Sent+to+sourcing", status_code=302)
