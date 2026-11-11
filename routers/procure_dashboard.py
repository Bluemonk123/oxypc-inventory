"""Procure Dashboard — the CRM Dashboard's KPIs/funnels/follow-ups moved
here, plus a "Pending Requests" view for procurement combining the two
request queues that used to live on separate pages: Part Sourcing
(formerly on CRM Dashboard) and Device Sourcing (previously only
actionable from the Telecaller Model Request queue as a stock
fulfillment). The Device Sourcing tab is procurement-only: it lets Sales
Manager/admin close a model request against an actual CRM Sourcing Deal,
distinct from TRC's "Update" (fulfil from existing stock)."""
import uuid
from templates_config import templates
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_

from database import get_db
from models.user import User, UserRole
from models.crm import (
    CRMContact, CRMSourcingDeal, CRMSalesOpportunity, CRMActivity,
    SOURCING_STAGES, SALES_STAGES,
)
from models.part_request import PartSourcingRequest
from models.part_estimate import PartEstimate
from models.model_requests import ModelRequest
from auth.dependencies import get_current_user, require_roles, verify_csrf
from services.audit_engine import audit

router = APIRouter(prefix="/procure-dashboard", tags=["procure-dashboard"],
                   dependencies=[Depends(verify_csrf)])
sm_allowed = require_roles(UserRole.admin, UserRole.sales_manager)

STATUS_BADGE = {
    "open": "warning text-dark",
    "partially_fulfilled": "info text-dark",
    "fulfilled": "success",
    "closed": "success",
    "cancelled": "secondary",
}


@router.get("", response_class=HTMLResponse)
async def procure_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    is_sm = current_user.role in (UserRole.admin, UserRole.sales_manager)
    now = app_now()

    # ── Contact counts (moved from CRM Dashboard) ───────────────────────────
    contact_total = (await db.execute(select(func.count(CRMContact.id)))).scalar() or 0
    buyer_count = (await db.execute(
        select(func.count(CRMContact.id)).where(CRMContact.contact_type.in_(["buyer", "both"]))
    )).scalar() or 0
    supplier_count = (await db.execute(
        select(func.count(CRMContact.id)).where(CRMContact.contact_type.in_(["supplier", "both"]))
    )).scalar() or 0

    # ── Sourcing pipeline ────────────────────────────────────────────────────
    sd_r = await db.execute(
        select(CRMSourcingDeal)
        .where(CRMSourcingDeal.stage.notin_(["won", "lost"]))
        .order_by(CRMSourcingDeal.created_at.desc())
    )
    open_sourcing = sd_r.scalars().all()
    sourcing_pipeline_value = sum(
        float(d.our_offer_total or d.asking_price_total or 0) for d in open_sourcing
    )
    sourcing_funnel = {}
    for stage_key, _ in SOURCING_STAGES:
        if stage_key in ("won", "lost"):
            continue
        sourcing_funnel[stage_key] = sum(1 for d in open_sourcing if d.stage == stage_key)

    # ── Sales pipeline ───────────────────────────────────────────────────────
    so_r = await db.execute(
        select(CRMSalesOpportunity)
        .where(CRMSalesOpportunity.stage.notin_(["won", "lost"]))
        .order_by(CRMSalesOpportunity.created_at.desc())
    )
    open_sales = so_r.scalars().all()
    sales_pipeline_value = sum(float(o.estimated_value or 0) for o in open_sales)
    sales_funnel = {}
    for stage_key, _ in SALES_STAGES:
        if stage_key in ("won", "lost"):
            continue
        sales_funnel[stage_key] = sum(1 for o in open_sales if o.stage == stage_key)

    # ── Follow-ups ───────────────────────────────────────────────────────────
    overdue_r = await db.execute(
        select(CRMActivity).where(
            CRMActivity.followup_done == False,
            CRMActivity.next_followup != None,
            CRMActivity.next_followup <= now,
        ).order_by(CRMActivity.next_followup).limit(10)
    )
    overdue_activities = overdue_r.scalars().all()

    today_r = await db.execute(
        select(CRMActivity).where(
            CRMActivity.followup_done == False,
            CRMActivity.next_followup != None,
            CRMActivity.next_followup > now,
            func.date(CRMActivity.next_followup) == now.date(),
        ).order_by(CRMActivity.next_followup)
    )
    today_activities = today_r.scalars().all()

    overdue_count = len(overdue_activities)
    today_count = len(today_activities)
    all_due = overdue_activities + today_activities

    deal_ids = [str(a.deal_id) for a in all_due if a.deal_id]
    sourcing_map, sales_map = {}, {}
    if deal_ids:
        sr = await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id.in_(deal_ids)))
        for d in sr.scalars().all():
            sourcing_map[str(d.id)] = d
        or2 = await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id.in_(deal_ids)))
        for o in or2.scalars().all():
            sales_map[str(o.id)] = o

    # ── Part Sourcing tab (same data as CRM Dashboard's table) ──────────────
    # A production-sourced (Part Estimate) request only belongs here once the
    # Spare Parts Manager has clicked Confirm Request on it, on Part Master ->
    # Sourcing Requests — until then it's still theirs to review, not
    # Procurement's to act on. 'procure'-sourced rows have no such gate and
    # always show, same as before.
    ps_r = await db.execute(
        select(PartSourcingRequest)
        .where(or_(PartSourcingRequest.source != "production",
                  PartSourcingRequest.confirmed == True))  # noqa: E712
        .order_by(PartSourcingRequest.created_at.desc()).limit(200)
    )
    part_sourcing = ps_r.scalars().all()

    # Every estimate file from the same Generate click as each production
    # request shown here — mirrors the grouping on Part Master -> Sourcing
    # Requests (routers/spare_parts.py) so the attachment shows the same way
    # in both places.
    lot_ids = {s.lot_id for s in part_sourcing if s.source == "production" and s.lot_id}
    estimates_by_batch = {}
    if lot_ids:
        est_rows = (await db.execute(
            select(PartEstimate).where(
                PartEstimate.lot_id.in_(lot_ids), PartEstimate.file_name.isnot(None))
        )).scalars().all()
        by_lot_and_time = {}
        for e in est_rows:
            by_lot_and_time.setdefault((e.lot_id, e.created_at), []).append(e)
        for s in part_sourcing:
            if s.source == "production" and s.lot_id:
                estimates_by_batch[str(s.id)] = by_lot_and_time.get((s.lot_id, s.created_at), [])

    open_deals_r = await db.execute(
        select(CRMSourcingDeal)
        .where(CRMSourcingDeal.stage.notin_(["won", "lost"]))
        .order_by(CRMSourcingDeal.created_at.desc())
    )
    sourcing_deals_for_close = open_deals_r.scalars().all()

    deal_map = {}
    linked_ids = [s.source_deal_id for s in part_sourcing if s.source_deal_id]
    if linked_ids:
        valid_ids = []
        for lid in linked_ids:
            try:
                valid_ids.append(str(uuid.UUID(lid)))
            except (ValueError, AttributeError, TypeError):
                continue
        if valid_ids:
            dm_r = await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id.in_(valid_ids)))
            for d in dm_r.scalars().all():
                deal_map[str(d.id)] = d

    # ── Device Sourcing tab (Telecaller Model Request queue, without
    #    Matching Stock, with a procurement-only Close Deal action) ─────────
    dr_r = await db.execute(
        select(ModelRequest)
        .order_by(ModelRequest.created_at.desc())   # closed ones too: Closed Request pill (2026-11-11)
        .limit(300)
    )
    device_sourcing = dr_r.scalars().all()
    # Part Name shows "<part name> - <model> - <make>" (2026-11-11, Pankaj)
    from models.spare_parts import SparePart
    _pids = {s.part_id for s in part_sourcing if s.part_id}
    part_map = {str(p.id): p for p in (await db.execute(select(SparePart).where(SparePart.id.in_(_pids)))).scalars().all()} if _pids else {}

    return templates.TemplateResponse("procure/dashboard.html", {
        "request": request, "current_user": current_user, "is_sm": is_sm,
        "part_sourcing": part_sourcing,
        "estimates_by_batch": estimates_by_batch,
        "sourcing_deals_for_close": sourcing_deals_for_close,
        "deal_map": deal_map,
        "device_sourcing": device_sourcing,
        "part_map": part_map,
        "status_badge": STATUS_BADGE,
        "success": request.query_params.get("success"),
        # contacts
        "contact_total": contact_total,
        "buyer_count": buyer_count,
        "supplier_count": supplier_count,
        # sourcing
        "open_sourcing_count": len(open_sourcing),
        "sourcing_pipeline_value": sourcing_pipeline_value,
        "sourcing_funnel": sourcing_funnel,
        # sales
        "open_sales_count": len(open_sales),
        "sales_pipeline_value": sales_pipeline_value,
        "sales_funnel": sales_funnel,
        # follow-ups
        "overdue_count": overdue_count,
        "today_count": today_count,
        "all_due": all_due,
        "sourcing_map": sourcing_map,
        "sales_map": sales_map,
        "now": now,
    })


@router.post("/device-sourcing/{request_id}/close")
async def close_device_sourcing(
    request_id: str,
    request: Request,
    source_deal_id: str = Form(...),
    qty_sourced: int = Form(...),
    sourcing_notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(sm_allowed),
):
    try:
        rid = uuid.UUID(request_id)
    except ValueError:
        raise HTTPException(404, "Request not found")
    mr = (await db.execute(select(ModelRequest).where(ModelRequest.id == rid))).scalar_one_or_none()
    if not mr:
        raise HTTPException(404, "Request not found")

    mr.source_deal_id = source_deal_id.strip()
    mr.qty_fulfilled = max(0, qty_sourced)
    mr.sourcing_notes = sourcing_notes.strip() or None
    mr.status = "closed"
    mr.closed_by = current_user.username
    mr.closed_at = app_now()

    await audit(db, action="MODEL_REQUEST_SOURCING_CLOSED", user=current_user,
                table_name="model_requests", record_id=str(mr.id),
                new_value={"source_deal_id": source_deal_id, "qty_sourced": qty_sourced}, request=request)
    await db.commit()
    return RedirectResponse(url="/procure-dashboard?success=Sourcing+deal+closed", status_code=302)
