"""CRM Activities router — log calls, WhatsApp, visits, notes; manage follow-ups."""
from datetime import datetime
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from templates_config import templates
from database import get_db
from auth.dependencies import get_current_user, verify_csrf
from models.user import User
from models.crm import (
    CRMActivity, CRMContact, CRMSourcingDeal, CRMSalesOpportunity,
    ACTIVITY_TYPES, ACTIVITY_OUTCOMES,
)

router = APIRouter(prefix="/crm/activities", tags=["crm-activities"], dependencies=[Depends(verify_csrf)])


def _parse_dt(v: str):
    if not v or not v.strip():
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v.strip(), fmt)
        except ValueError:
            continue
    return None


# ── LOG ACTIVITY (used inline from deal/opp/contact pages) ───────────────────

@router.post("/log")
async def log_activity(
    request: Request,
    contact_id:          str = Form(default=None),
    deal_id:             str = Form(default=None),
    deal_type:           str = Form(default=None),   # sourcing / sales
    activity_type:       str = Form(default="call"),
    direction:           str = Form(default="outbound"),
    summary:             str = Form(...),
    outcome:             str = Form(default=None),
    next_followup:       str = Form(default=None),
    followup_assigned_to:str = Form(default=None),
    redirect_to:         str = Form(default=None),   # URL to go back to
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    activity = CRMActivity(
        contact_id=contact_id or None,
        deal_id=deal_id or None,
        deal_type=deal_type or None,
        activity_type=activity_type,
        direction=direction,
        summary=summary,
        outcome=outcome or None,
        performed_by=current_user.username,
        activity_date=app_now(),
        next_followup=_parse_dt(next_followup),
        followup_assigned_to=followup_assigned_to or current_user.username,
        followup_done=False,
    )
    db.add(activity)
    # Sourcing Deal (2026-10-27): logging a contact on a deal still at Lead
    # Identified moves it to Contacted (system notes don't count).
    if deal_type == "sourcing" and deal_id and activity_type != "note":
        deal = (await db.execute(
            select(CRMSourcingDeal).where(CRMSourcingDeal.id == deal_id))).scalar_one_or_none()
        if deal and deal.stage == "lead":
            deal.stage = "contacted"
            db.add(CRMActivity(contact_id=deal.contact_id, deal_id=deal.id, deal_type="sourcing",
                               activity_type="note", summary="Stage moved to: Contacted",
                               performed_by=current_user.username, outcome="done",
                               activity_date=app_now()))
    # Buyer Deals too (2026-10-27): same rule.
    if deal_type == "sales" and deal_id and activity_type != "note":
        opp = (await db.execute(
            select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == deal_id))).scalar_one_or_none()
        if opp and opp.stage == "lead":
            opp.stage = "contacted"
            db.add(CRMActivity(contact_id=opp.contact_id, deal_id=opp.id, deal_type="sales",
                               activity_type="note", summary="Stage moved to: Contacted",
                               performed_by=current_user.username, outcome="done",
                               activity_date=app_now()))
    await db.commit()

    back = redirect_to or "/crm/"
    return RedirectResponse(url=f"{back}?success=Activity+logged", status_code=302)


# ── MARK FOLLOW-UP DONE ───────────────────────────────────────────────────────

@router.post("/{activity_id}/done")
async def mark_done(
    request: Request,
    activity_id: str,
    redirect_to: str = Form(default="/crm/"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMActivity).where(CRMActivity.id == activity_id))
    activity = result.scalar_one_or_none()
    if activity:
        activity.followup_done = True
        await db.commit()
    return RedirectResponse(url=f"{redirect_to}?success=Follow-up+marked+done", status_code=302)


# ── ALL FOLLOW-UPS DUE (dashboard-level view) ────────────────────────────────

@router.get("/followups", response_class=HTMLResponse)
async def followups_due(
    request: Request,
    filter_type: str = Query(default=""),   # sourcing / sales / ""
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    now = app_now()
    query = select(CRMActivity).where(
        CRMActivity.followup_done == False,
        CRMActivity.next_followup != None,
    )
    if filter_type:
        query = query.where(CRMActivity.deal_type == filter_type)
    # sales execs see only their own
    if current_user.role not in (
        __import__('models.user', fromlist=['UserRole']).UserRole.admin,
        __import__('models.user', fromlist=['UserRole']).UserRole.sales_manager,
        __import__('models.user', fromlist=['UserRole']).UserRole.inventory_manager,
    ):
        query = query.where(CRMActivity.followup_assigned_to == current_user.username)

    result = await db.execute(query.order_by(CRMActivity.next_followup))
    activities = result.scalars().all()

    overdue  = [a for a in activities if a.next_followup and a.next_followup <= now]
    due_today = [a for a in activities if a.next_followup and
                 now < a.next_followup and a.next_followup.date() == now.date()]
    upcoming  = [a for a in activities if a.next_followup and a.next_followup.date() > now.date()]

    # fetch deal titles
    deal_ids = [str(a.deal_id) for a in activities if a.deal_id]
    sourcing_map, sales_map = {}, {}
    if deal_ids:
        sr = await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id.in_(deal_ids)))
        for d in sr.scalars().all():
            sourcing_map[str(d.id)] = d
        or_ = await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id.in_(deal_ids)))
        for o in or_.scalars().all():
            sales_map[str(o.id)] = o

    return templates.TemplateResponse("crm/followups.html", {
        "request": request, "current_user": current_user,
        "overdue": overdue, "due_today": due_today, "upcoming": upcoming,
        "sourcing_map": sourcing_map, "sales_map": sales_map,
        "filter_type": filter_type, "now": now,
    })


# ── CRM DASHBOARD ─────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def crm_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Alias to /crm/ — kept here so the router is standalone."""
    return RedirectResponse(url="/crm/", status_code=302)
