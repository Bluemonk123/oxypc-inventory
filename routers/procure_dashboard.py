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

    # ── Parts Sourcing / Device Sourcing requests (shared with Activity Board, 2026-11-15) ──
    from services.sourcing_requests import requests_context
    _src = await requests_context(db)
    part_sourcing = _src["part_sourcing"]

    # Option C dashboard (2026-11-14, Pankaj) — services/procure_dash.py
    from services.procure_dash import build as _build_dash
    try:
        _pending_parts = sum(1 for s in part_sourcing if (s.status or "").lower() in ("pending", "open", "requested"))
        from templates_config import _can
        dash_kpis, dash_widgets = await _build_dash(db, overdue_count, today_count, all_due, _pending_parts,
                                                    sp=_can(current_user, "crm_sourcing", "pricing"),
                                                    cp=_can(current_user, "crm_sales_opp", "pricing"))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("procurement dashboard failed")
        await db.rollback()
        dash_kpis, dash_widgets = [], []
    return templates.TemplateResponse("procure/dashboard.html", {
        "request": request, "current_user": current_user, "is_sm": is_sm,
        "dash_kpis": dash_kpis, "dash_widgets": dash_widgets,
        **_src,
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

    if (mr.review_status or "verified") != "verified":   # verify on Activity Board first (2026-11-15)
        return RedirectResponse(url="/procure-dashboard?error=Verify+this+request+on+Activity+Board+first", status_code=302)
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


@router.post("/device-sourcing/{request_id}/link-deal")
async def link_device_sourcing_deal(request_id: str, request: Request, source_deal_id: str = Form(""),
                                    db: AsyncSession = Depends(get_db), current_user: User = Depends(sm_allowed)):
    """Link Deal on a Device Sourcing request (2026-11-15, Pankaj): tie it to a
    supplier deal without closing it. Empty source_deal_id removes the link."""
    try:
        rid = uuid.UUID(request_id)
    except ValueError:
        raise HTTPException(404, "Request not found")
    mr = (await db.execute(select(ModelRequest).where(ModelRequest.id == rid))).scalar_one_or_none()
    if not mr:
        raise HTTPException(404, "Request not found")
    if (mr.review_status or "verified") != "verified":
        return RedirectResponse(url="/procure-dashboard?error=Verify+this+request+on+Activity+Board+first", status_code=302)
    new = (source_deal_id or "").strip() or None
    if new:
        try:
            deal = (await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == uuid.UUID(new)))).scalar_one_or_none()
        except ValueError:
            deal = None
        if not deal:
            return RedirectResponse(url="/procure-dashboard?error=Supplier+deal+not+found", status_code=302)
        new = str(deal.id)
    old = mr.source_deal_id
    mr.source_deal_id = new
    await audit(db, action="MODEL_REQUEST_DEAL_LINKED", user=current_user, table_name="model_requests",
                record_id=str(mr.id), old_value={"source_deal_id": old}, new_value={"source_deal_id": new}, request=request)
    await db.commit()
    return RedirectResponse(url="/procure-dashboard?success=" + ("Deal+linked" if new else "Link+removed"), status_code=302)
