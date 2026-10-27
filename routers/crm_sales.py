"""CRM Sales Opportunities router — sales pipeline from enquiry to invoice."""
import os
import uuid as _uuid
from datetime import datetime
from utils.timezone import app_now
from config import UPLOADS_DIR
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from templates_config import templates
from database import get_db
from auth.dependencies import get_current_user, verify_csrf, require_module_perm
from models.user import User, UserRole
from models.crm import (
    CRMContact, CRMSalesOpportunity, CRMActivity, CRMQuote,
    BUYER_TYPES, MATERIAL_TYPES, SALES_STAGES, PRIORITIES, GRADES,
    CRMSalesStep, SALES_STAGE_STEPS, SALES_STEP_INDEX,
)
from models.sales import Sale
from models.device import Device
from models.lot import Lot

router = APIRouter(prefix="/crm/sales", tags=["crm-sales"], dependencies=[Depends(verify_csrf)])


async def _next_opp_number(db: AsyncSession) -> str:
    result = await db.execute(select(func.count(CRMSalesOpportunity.id)))
    n = (result.scalar() or 0) + 1
    year = app_now().year
    return f"OPP-{year}-{n:04d}"


# ── LIST ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def list_opportunities(
    request: Request,
    q: str = Query(default=""),
    stage: str = Query(default=""),
    buyer_type: str = Query(default=""),
    priority: str = Query(default=""),
    assigned: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(CRMSalesOpportunity)
    if q:
        query = query.where(CRMSalesOpportunity.title.ilike(f"%{q}%"))
    if stage:
        query = query.where(CRMSalesOpportunity.stage == stage)
    if buyer_type:
        query = query.where(CRMSalesOpportunity.buyer_type == buyer_type)
    if priority:
        query = query.where(CRMSalesOpportunity.priority == priority)
    if assigned:
        query = query.where(CRMSalesOpportunity.assigned_to == assigned)
    elif current_user.role in (UserRole.sales, UserRole.telecaller):
        query = query.where(CRMSalesOpportunity.assigned_to == current_user.username)

    result = await db.execute(query.order_by(CRMSalesOpportunity.created_at.desc()))
    opps = result.scalars().all()

    open_opps = [o for o in opps if o.stage not in ("won", "lost")]
    pipeline_value = sum(float(o.estimated_value or 0) for o in open_opps)

    now = app_now()
    overdue_r = await db.execute(
        select(func.count(CRMActivity.id)).where(
            CRMActivity.deal_type == "sales",
            CRMActivity.followup_done == False,
            CRMActivity.next_followup <= now,
        )
    )
    overdue_count = overdue_r.scalar() or 0

    contact_ids = list({o.contact_id for o in opps if o.contact_id})
    contacts_map = {}
    if contact_ids:
        cr = await db.execute(select(CRMContact).where(CRMContact.id.in_(contact_ids)))
        for c in cr.scalars().all():
            contacts_map[str(c.id)] = c

    # ── Lot behind each Buyer Deal that came from a won bid ─────────────────
    # The opportunity has no lot FK — Mark Won only puts the lot number in the
    # title — so resolve it through the winning bid that created it.
    from models.partner import PartnerBid
    from models.lot import Lot

    opp_lots = {}
    opp_ids = [o.id for o in opps]
    if opp_ids:
        won_bids = (await db.execute(
            select(PartnerBid).where(
                PartnerBid.opportunity_id.in_(opp_ids),
                PartnerBid.lot_id.isnot(None),
            )
        )).scalars().all()
        if won_bids:
            lots_map = {l.id: l for l in (await db.execute(select(Lot).where(
                Lot.id.in_({b.lot_id for b in won_bids})))).scalars().all()}
            for b in won_bids:
                lot = lots_map.get(b.lot_id)
                if lot:
                    opp_lots[str(b.opportunity_id)] = {
                        "lot_id": str(lot.id), "lot_number": lot.lot_number,
                    }

    return templates.TemplateResponse("crm/sales/list.html", {
        "request": request, "current_user": current_user,
        "opps": opps, "contacts_map": contacts_map, "opp_lots": opp_lots,
        "pipeline_value": pipeline_value, "overdue_count": overdue_count,
        "q": q, "stage": stage, "buyer_type": buyer_type,
        "priority": priority, "assigned": assigned,
        "sales_stages": SALES_STAGES, "buyer_types": BUYER_TYPES,
        "stage_labels": dict(SALES_STAGES), "priorities": PRIORITIES,
    })


# ── NEW ───────────────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_opp_form(
    request: Request,
    contact_id: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    contacts_r = await db.execute(
        select(CRMContact)
        .where(CRMContact.status == "active", CRMContact.contact_type.in_(["buyer", "both"]))
        .order_by(CRMContact.company_name)
    )
    contacts = contacts_r.scalars().all()
    return templates.TemplateResponse("crm/sales/form.html", {
        "request": request, "current_user": current_user,
        "opp": None, "contacts": contacts, "preselect": contact_id,
        "buyer_types": BUYER_TYPES, "material_types": MATERIAL_TYPES,
        "grades": GRADES, "priorities": PRIORITIES,
    })


@router.post("/new")
async def create_opp(
    request: Request,
    title:               str = Form(...),
    contact_id:          str = Form(default=None),
    buyer_type:          str = Form(default=None),
    device_type:         str = Form(default=None),
    required_qty:        str = Form(default=None),
    material_type:       str = Form(default=None),
    grade_required:      str = Form(default=None),
    budget_per_unit:     str = Form(default=None),
    estimated_value:     str = Form(default=None),
    expected_close_date: str = Form(default=None),
    priority:            str = Form(default="medium"),
    assigned_to:         str = Form(default=None),
    notes:               str = Form(default=None),
    product_records:     UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _perm: User = Depends(require_module_perm("crm_sales_opp", "add")),
):
    def _n(v): return float(v) if v and v.strip() else None
    def _i(v): return int(v) if v and v.strip() else None
    def _d(v):
        if v and v.strip():
            try: return datetime.strptime(v.strip(), "%Y-%m-%d").date()
            except: return None
        return None

    # Save uploaded product records file if provided
    saved_filename = None
    if product_records and product_records.filename:
        ext = os.path.splitext(product_records.filename)[1].lower()
        if ext in (".xls", ".xlsx", ".csv"):
            safe_name = f"{_uuid.uuid4().hex}{ext}"
            uploads_dir = os.path.join(UPLOADS_DIR, "crm")
            os.makedirs(uploads_dir, exist_ok=True)
            dest = os.path.join(uploads_dir, safe_name)
            content = await product_records.read()
            with open(dest, "wb") as f:
                f.write(content)
            saved_filename = safe_name

    qty = _i(required_qty)
    bpu = _n(budget_per_unit)
    est = _n(estimated_value) or (round(qty * bpu, 2) if qty and bpu else None)

    opp_number = await _next_opp_number(db)
    opp = CRMSalesOpportunity(
        opp_number=opp_number,
        contact_id=contact_id or None,
        title=title,
        buyer_type=buyer_type or None,
        device_type=device_type or None,
        required_qty=qty,
        material_type=material_type or None,
        grade_required=grade_required or None,
        budget_per_unit=bpu,
        estimated_value=est,
        expected_close_date=_d(expected_close_date),
        priority=priority,
        assigned_to=assigned_to or current_user.username,
        notes=notes or None,
        product_records_file=saved_filename,
        created_by=current_user.username,
        stage="lead",
    )
    db.add(opp)
    await db.commit()
    return RedirectResponse(url=f"/crm/sales/{opp.id}?success=Opportunity+created", status_code=302)


# ── DETAIL ────────────────────────────────────────────────────────────────────

@router.get("/{opp_id}", response_class=HTMLResponse)
async def opp_detail(
    request: Request,
    opp_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == opp_id))
    opp = result.scalar_one_or_none()
    if not opp:
        return RedirectResponse(url="/crm/sales?error=Opportunity+not+found", status_code=302)

    contact = None
    if opp.contact_id:
        cr = await db.execute(select(CRMContact).where(CRMContact.id == opp.contact_id))
        contact = cr.scalar_one_or_none()

    quote = None
    if opp.quote_id:
        qr = await db.execute(select(CRMQuote).where(CRMQuote.id == opp.quote_id))
        quote = qr.scalar_one_or_none()

    # The lot this deal came from — the same one Create Quote reads its line
    # items out of, so the page shows the source the quote will use.
    from services.opportunity_lot import lot_for_opportunity
    lot = await lot_for_opportunity(db, opp.id)

    # Quotes Generated — every quote raised against this deal. Keyed off the
    # opportunity, plus this deal's own quote_id, so a quote linked before the
    # opportunity_id was recorded still shows up.
    from routers.crm_quotes import quote_summary_rows, active_terms_by_type
    deal_quotes = (await db.execute(
        select(CRMQuote)
        .join(CRMSalesOpportunity, CRMSalesOpportunity.quote_id == CRMQuote.id)
        .where(CRMSalesOpportunity.id == opp.id)
        .order_by(CRMQuote.created_at.desc())
    )).scalars().all()
    quote_rows = await quote_summary_rows(db, deal_quotes)
    terms_by_type = await active_terms_by_type(db)
    from models.company import Company
    companies = (await db.execute(
        select(Company).where(Company.is_active == True).order_by(Company.company_name)
    )).scalars().all()

    acts_r = await db.execute(
        select(CRMActivity)
        .where(CRMActivity.deal_id == opp.id, CRMActivity.deal_type == "sales")
        .order_by(CRMActivity.activity_date.desc())
    )
    activities = acts_r.scalars().all()

    next_fu = next((a for a in activities if not a.followup_done and a.next_followup), None)

    stage_list = list(enumerate(SALES_STAGES))
    current_idx = next((i for i, (v, _) in stage_list if v == opp.stage), 0)
    # Stage Progress (2026-10-27, same layout as Sourcing Deals): latest
    # logged contact under Contacted, and the stage Next Stage moves to.
    last_contact = next((a for a in activities if a.activity_type != "note"), None)
    step_rows = (await db.execute(
        select(CRMSalesStep).where(CRMSalesStep.deal_id == opp.id))).scalars().all()
    steps = {r.step_key: r for r in step_rows}
    latest_quote = deal_quotes[0] if deal_quotes else None
    staff = (await db.execute(
        select(User).where(User.status == True).order_by(User.full_name)  # noqa: E712
    )).scalars().all()
    user_names = {u.username: (u.full_name or u.username) for u in staff}
    open_keys = [k for k, _ in SALES_STAGES if k not in ("won", "lost")]
    next_stage = None
    if opp.stage in open_keys and opp.stage != open_keys[-1]:
        next_stage = open_keys[open_keys.index(opp.stage) + 1]

    return templates.TemplateResponse("crm/sales/detail.html", {
        "request": request, "current_user": current_user,
        "opp": opp, "contact": contact, "quote": quote, "activities": activities,
        "lot": lot,
        "quote_rows": quote_rows, "terms_by_type": terms_by_type,
        "companies": companies,
        "next_fu": next_fu,
        "sales_stages": SALES_STAGES, "stage_list": stage_list, "current_idx": current_idx,
        "stage_labels": dict(SALES_STAGES), "last_contact": last_contact, "next_stage": next_stage,
        "last_open_stage": open_keys[-1],
        "stage_steps": SALES_STAGE_STEPS, "steps": steps, "latest_quote": latest_quote,
        "staff": staff, "user_names": user_names,
        "buyer_map": dict(BUYER_TYPES), "material_map": dict(MATERIAL_TYPES),
        "now": app_now(),
    })


# ── MOVE STAGE ────────────────────────────────────────────────────────────────

@router.post("/{opp_id}/stage")
async def move_stage(
    request: Request,
    opp_id: str,
    new_stage: str = Form(...),
    win_loss_reason: str = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == opp_id))
    opp = result.scalar_one_or_none()
    if not opp:
        return RedirectResponse(url="/crm/sales?error=Not+found", status_code=302)
    opp.stage = new_stage
    if win_loss_reason:
        opp.win_loss_reason = win_loss_reason
    activity = CRMActivity(
        contact_id=opp.contact_id, deal_id=opp.id, deal_type="sales",
        activity_type="note",
        summary=f"Stage moved to: {dict(SALES_STAGES).get(new_stage, new_stage)}",
        performed_by=current_user.username, outcome="done",
    )
    db.add(activity)
    await db.commit()
    return RedirectResponse(url=f"/crm/sales/{opp_id}?success=Stage+updated", status_code=302)


# ── STAGE PROGRESS ITEMS (2026-10-27, same as Sourcing Deals) ────────────────

STEP_FILE_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif",
                  ".xlsx", ".xls", ".csv", ".doc", ".docx"}
STEP_FILE_MAX_BYTES = 20 * 1024 * 1024


def _back(opp_id, msg, ok=True):
    from urllib.parse import quote_plus
    return RedirectResponse(url=f"/crm/sales/{opp_id}?{'success' if ok else 'error'}={quote_plus(msg)}",
                            status_code=302)


async def _opp_and_step(db, opp_id: str, step_key: str):
    opp = (await db.execute(
        select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == opp_id))).scalar_one_or_none()
    if not opp or step_key not in SALES_STEP_INDEX:
        return opp, None
    step = (await db.execute(select(CRMSalesStep).where(
        CRMSalesStep.deal_id == opp.id, CRMSalesStep.step_key == step_key))).scalar_one_or_none()
    return opp, step


def _stage_label(stage):
    return "Documents" if stage == "documents" else dict(SALES_STAGES).get(stage, stage)


def _note(opp, user, text):
    return CRMActivity(contact_id=opp.contact_id, deal_id=opp.id, deal_type="sales",
                       activity_type="note", summary=text, performed_by=user.username, outcome="done")


@router.post("/{opp_id}/steps/{step_key}/assign")
async def assign_step(
    request: Request, opp_id: str, step_key: str,
    username: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """User icon on a Stage Progress item: assign a user or an approver; the
    assignee gets a bell notification."""
    from services.audit_engine import audit
    opp, step = await _opp_and_step(db, opp_id, step_key)
    if not opp or step_key not in SALES_STEP_INDEX:
        return _back(opp_id, "Step not found", ok=False)
    stage, label, kind = SALES_STEP_INDEX[step_key]
    if kind == "file":
        return _back(opp_id, "This item takes a file, not a user", ok=False)
    user = (await db.execute(select(User).where(User.username == username.strip(),
                                                User.status == True))).scalar_one_or_none()  # noqa: E712
    if not user:
        return _back(opp_id, "Select an active user", ok=False)
    old = {"assigned_to": step.assigned_to, "status": step.status} if step else None
    if not step:
        step = CRMSalesStep(deal_id=opp.id, stage=stage, step_key=step_key, created_by=current_user.username)
        db.add(step)
    step.assigned_to = user.username
    step.status = "pending" if kind == "approval" else "done"
    step.acted_by = None if kind == "approval" else current_user.username
    step.acted_at = None if kind == "approval" else app_now()
    step.note = None
    who = user.full_name or user.username
    db.add(_note(opp, current_user, f"{_stage_label(stage)} — {label}: assigned to {who}"))
    await audit(db, action="CRM_SALES_STEP_ASSIGNED", user=current_user, table_name="crm_sales_steps",
                record_id=str(opp.id), old_value=old,
                new_value={"step": step_key, "assigned_to": user.username}, request=request)
    await db.commit()
    from services.notifications import create_notification
    what = f"{label} approval" if kind == "approval" else label
    await create_notification(
        db, user_id=user.id, notification_type="info",
        title=f"{opp.opp_number}: {_stage_label(stage)} — {what}",
        message=f"{current_user.full_name or current_user.username} assigned you to {what} "
                f"on Buyer Deal {opp.opp_number} ({opp.title[:80]}).")
    return _back(opp_id, f"{label} assigned to {who}")


@router.post("/{opp_id}/steps/{step_key}/decide")
async def decide_step(
    request: Request, opp_id: str, step_key: str,
    decision: str = Form(...),
    note: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Approve / Reject — only the assigned approver or an admin."""
    from services.audit_engine import audit
    opp, step = await _opp_and_step(db, opp_id, step_key)
    if not opp or not step or SALES_STEP_INDEX[step_key][2] != "approval":
        return _back(opp_id, "Approval not found", ok=False)
    role = getattr(current_user.role, "value", current_user.role)
    if step.assigned_to != current_user.username and role != "admin":
        return _back(opp_id, "Only the assigned approver can approve or reject", ok=False)
    if decision not in ("approved", "rejected"):
        return _back(opp_id, "Choose Approve or Reject", ok=False)
    old = {"status": step.status, "note": step.note}
    step.status = decision
    step.note = (note or "").strip() or None
    step.acted_by = current_user.username
    step.acted_at = app_now()
    stage, label, _k = SALES_STEP_INDEX[step_key]
    db.add(_note(opp, current_user, f"{_stage_label(stage)} — {label}: {decision.title()}"
                                    + (f" — {step.note}" if step.note else "")))
    await audit(db, action="CRM_SALES_STEP_DECIDED", user=current_user, table_name="crm_sales_steps",
                record_id=str(opp.id), old_value=old,
                new_value={"step": step_key, "status": decision, "note": step.note}, request=request)
    await db.commit()
    return _back(opp_id, f"{label} {decision}")


@router.post("/{opp_id}/steps/{step_key}/attach")
async def attach_step_file(
    request: Request, opp_id: str, step_key: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Attach a document (stage files and the Deal Info Upload section). PDF /
    image / Excel / Word, up to 20 MB; replaces the item's earlier file."""
    from services.audit_engine import audit
    opp, step = await _opp_and_step(db, opp_id, step_key)
    if not opp or step_key not in SALES_STEP_INDEX or SALES_STEP_INDEX[step_key][2] != "file":
        return _back(opp_id, "Document item not found", ok=False)
    if not file or not file.filename:
        return _back(opp_id, "No file selected", ok=False)
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in STEP_FILE_EXTS:
        return _back(opp_id, "Allowed files: PDF, image, Excel or Word", ok=False)
    content = await file.read()
    if len(content) > STEP_FILE_MAX_BYTES:
        return _back(opp_id, "File is larger than 20 MB", ok=False)
    safe_name = f"{_uuid.uuid4().hex}{ext}"
    uploads_dir = os.path.join(UPLOADS_DIR, "crm")
    os.makedirs(uploads_dir, exist_ok=True)
    with open(os.path.join(uploads_dir, safe_name), "wb") as f:
        f.write(content)
    stage, label, _k = SALES_STEP_INDEX[step_key]
    old = {"file_path": step.file_path} if step else None
    if not step:
        step = CRMSalesStep(deal_id=opp.id, stage=stage, step_key=step_key, created_by=current_user.username)
        db.add(step)
    step.file_path = safe_name
    step.status = "done"
    step.acted_by = current_user.username
    step.acted_at = app_now()
    db.add(_note(opp, current_user, f"{_stage_label(stage)} — {label} attached ({file.filename[:120]})"))
    await audit(db, action="CRM_SALES_STEP_FILE", user=current_user, table_name="crm_sales_steps",
                record_id=str(opp.id), old_value=old,
                new_value={"step": step_key, "file_path": safe_name, "original": file.filename[:200]},
                request=request)
    await db.commit()
    return _back(opp_id, f"{label} attached")


@router.post("/{opp_id}/next-stage")
async def next_stage(
    request: Request,
    opp_id: str,
    outcome: str = Form(default=""),
    win_loss_reason: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Next Stage (2026-10-27, same as Sourcing Deals): one stage on, never
    blocked. From the last open stage (Payment Received) it closes the deal
    as Won or Lost with a reason. Audit-logged and noted in the Activity Log."""
    from urllib.parse import quote_plus
    from services.audit_engine import audit
    opp = (await db.execute(
        select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == opp_id))).scalar_one_or_none()
    if not opp:
        return RedirectResponse(url="/crm/sales?error=Not+found", status_code=302)
    back = lambda m, ok=True: RedirectResponse(
        url=f"/crm/sales/{opp_id}?{'success' if ok else 'error'}={quote_plus(m)}", status_code=302)
    order = [k for k, _ in SALES_STAGES if k not in ("won", "lost")]
    if opp.stage in ("won", "lost"):
        return back("Deal is already closed", ok=False)
    if opp.stage == order[-1]:
        if outcome not in ("won", "lost"):
            return back("Choose Won or Lost", ok=False)
        new = outcome
        opp.win_loss_reason = (win_loss_reason or "").strip() or opp.win_loss_reason
    else:
        cur = order.index(opp.stage) if opp.stage in order else 0
        new = order[cur + 1]
    old = opp.stage
    opp.stage = new
    labels = dict(SALES_STAGES)
    text = f"Stage moved to: {labels.get(new, new)}"
    if new in ("won", "lost") and opp.win_loss_reason:
        text += f" — {opp.win_loss_reason}"
    db.add(CRMActivity(contact_id=opp.contact_id, deal_id=opp.id, deal_type="sales",
                       activity_type="note", summary=text,
                       performed_by=current_user.username, outcome="done"))
    await audit(db, action="CRM_SALES_NEXT_STAGE", user=current_user,
                table_name="crm_sales_opportunities", record_id=str(opp.id),
                old_value={"stage": old}, new_value={"stage": new, "win_loss_reason": opp.win_loss_reason},
                request=request)
    await db.commit()
    return back(f"Moved to {labels.get(new, new)}")


# ── EDIT ─────────────────────────────────────────────────────────────────────

@router.get("/{opp_id}/edit", response_class=HTMLResponse)
async def edit_opp_form(
    request: Request,
    opp_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == opp_id))
    opp = result.scalar_one_or_none()
    if not opp:
        return RedirectResponse(url="/crm/sales?error=Not+found", status_code=302)
    contacts_r = await db.execute(
        select(CRMContact)
        .where(CRMContact.status == "active", CRMContact.contact_type.in_(["buyer", "both"]))
        .order_by(CRMContact.company_name)
    )
    contacts = contacts_r.scalars().all()
    return templates.TemplateResponse("crm/sales/form.html", {
        "request": request, "current_user": current_user,
        "opp": opp, "contacts": contacts,
        "preselect": str(opp.contact_id) if opp.contact_id else "",
        "buyer_types": BUYER_TYPES, "material_types": MATERIAL_TYPES,
        "grades": GRADES, "priorities": PRIORITIES,
    })


@router.post("/{opp_id}/edit")
async def update_opp(
    request: Request,
    opp_id: str,
    title:               str = Form(...),
    contact_id:          str = Form(default=None),
    buyer_type:          str = Form(default=None),
    device_type:         str = Form(default=None),
    required_qty:        str = Form(default=None),
    material_type:       str = Form(default=None),
    grade_required:      str = Form(default=None),
    budget_per_unit:     str = Form(default=None),
    estimated_value:     str = Form(default=None),
    expected_close_date: str = Form(default=None),
    priority:            str = Form(default="medium"),
    assigned_to:         str = Form(default=None),
    notes:               str = Form(default=None),
    product_records:     UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == opp_id))
    opp = result.scalar_one_or_none()
    if not opp:
        return RedirectResponse(url="/crm/sales?error=Not+found", status_code=302)

    def _n(v): return float(v) if v and v.strip() else None
    def _i(v): return int(v) if v and v.strip() else None
    def _d(v):
        if v and v.strip():
            try: return datetime.strptime(v.strip(), "%Y-%m-%d").date()
            except: return None
        return None

    # Replace file if a new one is uploaded
    if product_records and product_records.filename:
        ext = os.path.splitext(product_records.filename)[1].lower()
        if ext in (".xls", ".xlsx", ".csv"):
            safe_name = f"{_uuid.uuid4().hex}{ext}"
            uploads_dir = os.path.join(UPLOADS_DIR, "crm")
            os.makedirs(uploads_dir, exist_ok=True)
            dest = os.path.join(uploads_dir, safe_name)
            content = await product_records.read()
            with open(dest, "wb") as f:
                f.write(content)
            opp.product_records_file = safe_name

    opp.title = title
    opp.contact_id = contact_id or None
    opp.buyer_type = buyer_type or None
    opp.device_type = device_type or None
    opp.required_qty = _i(required_qty)
    opp.material_type = material_type or None
    opp.grade_required = grade_required or None
    opp.budget_per_unit = _n(budget_per_unit)
    opp.estimated_value = _n(estimated_value)
    opp.expected_close_date = _d(expected_close_date)
    opp.priority = priority
    opp.assigned_to = assigned_to
    opp.notes = notes or None
    await db.commit()
    return RedirectResponse(url=f"/crm/sales/{opp_id}?success=Opportunity+updated", status_code=302)


# ── LINK SALE RECORD ──────────────────────────────────────────────────────────

@router.get("/{opp_id}/link-sale", response_class=HTMLResponse)
async def link_sale_form(
    request: Request,
    opp_id: str,
    q: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == opp_id))
    opp = result.scalar_one_or_none()
    if not opp:
        return RedirectResponse(url="/crm/sales?error=Not+found", status_code=302)

    # already linked sale IDs
    existing_ids = set((opp.linked_sale_ids or "").split(",")) - {""}

    # fetch all sales (client-side DataTables pagination), join device + lot for context
    sale_q = (
        select(Sale, Device.barcode, Device.brand, Device.model, Device.grade, Lot.lot_number)
        .join(Device, Sale.device_id == Device.id)
        .join(Lot, Device.lot_id == Lot.id)
        .order_by(Sale.sold_at.desc())
    )
    if q:
        like = f"%{q}%"
        sale_q = sale_q.where(
            Device.barcode.ilike(like) | Device.brand.ilike(like) |
            Device.model.ilike(like) | Sale.sale_number.ilike(like)
        )
    sales_r = await db.execute(sale_q)
    sales = sales_r.all()

    return templates.TemplateResponse("crm/sales/link_sale.html", {
        "request": request, "current_user": current_user,
        "opp": opp, "sales": sales, "existing_ids": existing_ids, "q": q,
    })


@router.post("/{opp_id}/link-sale")
async def link_sale_submit(
    request: Request,
    opp_id: str,
    sale_ids: list = Form(default=[]),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == opp_id))
    opp = result.scalar_one_or_none()
    if not opp:
        return RedirectResponse(url="/crm/sales?error=Not+found", status_code=302)

    # merge existing + newly selected (deduplicate)
    existing = set((opp.linked_sale_ids or "").split(",")) - {""}
    merged = existing | set(sale_ids)
    opp.linked_sale_ids = ",".join(merged)

    # auto-advance stage if still early
    # 2026-10-27: new stage list — linking a sale moves an early deal to
    # Invoice / Sales Order ("invoiced" no longer exists).
    if opp.stage in ("lead", "contacted", "screening", "quote", "negotiation"):
        opp.stage = "sales_order"

    activity = CRMActivity(
        contact_id=opp.contact_id, deal_id=opp.id, deal_type="sales",
        activity_type="note",
        summary=f"Linked {len(sale_ids)} sale record(s) to opportunity.",
        performed_by=current_user.username, outcome="done",
    )
    db.add(activity)
    await db.commit()
    return RedirectResponse(url=f"/crm/sales/{opp_id}?success=Sale+records+linked", status_code=302)
