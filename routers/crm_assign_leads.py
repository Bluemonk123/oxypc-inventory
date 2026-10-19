"""CRM Assign Leads router — ad-campaign lead management with call history."""
import csv
import io
import json as _json
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from templates_config import templates
from database import get_db
from auth.dependencies import get_current_user, verify_csrf
from models.user import User
from utils.timezone import app_now
from models.crm import (
    CRMLeadGroup, CRMLead, CRMLeadCall,
    LEAD_PLATFORMS, LEAD_CONTACT_MODES, LEAD_DEVICE_CATEGORIES,
    LEAD_DEALING_GRADES, LEAD_WHOM_TO_SELL, LEAD_DEALS_IN,
)
from models.master import MasterData

router = APIRouter(
    prefix="/crm/assign-social-leads",
    tags=["crm-assign-leads"],
    dependencies=[Depends(verify_csrf)],
)

_STATUS_BADGE = {
    "interested":     "success",
    "not_interested": "danger",
    "callback":       "warning text-dark",
    "order_placed":   "primary",
    "no_answer":      "secondary",
    "followup":       "info text-dark",
    "done":           "dark",
    "rescheduled":    "light text-dark border",
    "not_in_stock":   "danger",
    "high_price":     "warning text-dark",
    "invalid_no":     "dark",
}

# Pills shown as per-group counts in each accordion header
PILL_STATUSES = ["interested", "not_interested", "callback", "followup", "no_answer", "invalid_no"]


async def _asl_status_options(db: AsyncSession) -> list:
    """Status dropdown options for Assign Social Leads (call log modal + filter) —
    admin-managed via /admin/master, Assign Social Leads section."""
    result = await db.execute(
        select(MasterData.value)
        .where(MasterData.category == "asl_status", MasterData.is_active == True)
        .order_by(MasterData.display_order, MasterData.value)
    )
    return [row[0] for row in result.all()]


async def _next_lead_id(db: AsyncSession) -> str:
    result = await db.execute(select(func.count(CRMLead.id)))
    n = (result.scalar() or 0) + 1
    return str(100000000000 + n)


def _cats(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return _json.loads(raw)
    except Exception:
        return []


async def _latest_calls_map(db: AsyncSession, lead_ids: list) -> dict:
    """Return {lead_id_str: CRMLeadCall} — most recent call per lead (by created_at)."""
    if not lead_ids:
        return {}
    result = await db.execute(
        select(CRMLeadCall)
        .where(CRMLeadCall.lead_id.in_(lead_ids))
        .order_by(CRMLeadCall.created_at.desc())
    )
    latest = {}
    for c in result.scalars().all():
        key = str(c.lead_id)
        if key not in latest:
            latest[key] = c
    return latest


def _lead_dict(lead: CRMLead, latest_call: CRMLeadCall = None) -> dict:
    cats = _cats(lead.device_categories)
    grades = _cats(lead.dealing_grades)
    call_cats = _cats(latest_call.device_categories) if latest_call else []
    return {
        "id":                    str(lead.id),
        "lead_id":               lead.lead_id,
        "group_id":              str(lead.group_id),
        "lead_date":             lead.lead_date.isoformat() if lead.lead_date else "",
        "lead_date_display":     lead.lead_date.strftime("%d-%m-%Y") if lead.lead_date else "—",
        "platform":              lead.platform or "",
        "device_categories":     cats,
        "device_categories_display": ", ".join(cats) if cats else "—",
        "purchase_quantity":     lead.purchase_quantity or "",
        "selling_quantity":      lead.selling_quantity or "",
        "whom_to_sell":          lead.whom_to_sell or "",
        "deals_in":              lead.deals_in or "",
        "dealing_grades":        grades,
        "dealing_grades_display": ", ".join(grades) if grades else "—",
        "planning_to_buy":       lead.planning_to_buy or "",
        "contact_mode":          lead.contact_mode or "",
        "name":                  lead.name or "",
        "phone":                 lead.phone or "",
        "email":                 lead.email or "",
        "address":               lead.address or "",
        "call_status":           lead.call_status or "",
        "status_badge":          _STATUS_BADGE.get(lead.call_status or "", "secondary"),
        "full_remark":           lead.full_remark or "",
        "assigned_to":           lead.assigned_to or "",
        # ── Derived from the most recent call log entry ──────────────────────
        "latest_calling_date":        latest_call.calling_date.strftime("%d-%m-%Y") if latest_call and latest_call.calling_date else "—",
        "latest_quantity":            (latest_call.quantity if latest_call else "") or "—",
        "latest_device_categories":   ", ".join(call_cats) if call_cats else "—",
        "latest_full_remarks":        (latest_call.full_remarks if latest_call else "") or "—",
        "latest_deals_in":            (latest_call.deals_in if latest_call else "") or "—",
    }


# ── LIST ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def list_assign_leads(
    request: Request,
    q: str = Query(default=""),
    customer: str = Query(default=""),
    assigned: str = Query(default=""),
    status: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Groups (optionally filtered by name)
    gq = select(CRMLeadGroup).order_by(CRMLeadGroup.created_at.desc())
    if q:
        gq = gq.where(CRMLeadGroup.name.ilike(f"%{q}%"))
    groups_result = await db.execute(gq)
    groups = groups_result.scalars().all()

    # Leads for these groups (with optional customer / assigned / status filters)
    leads_by_group: dict[str, list[dict]] = {}
    pills_by_group: dict[str, dict] = {}
    if groups:
        group_ids = [g.id for g in groups]
        lq = (
            select(CRMLead)
            .where(CRMLead.group_id.in_(group_ids))
            .order_by(CRMLead.created_at.desc())
        )
        if customer:
            like = f"%{customer}%"
            lq = lq.where(
                or_(CRMLead.name.ilike(like), CRMLead.phone.ilike(like), CRMLead.email.ilike(like))
            )
        if assigned:
            lq = lq.where(CRMLead.assigned_to == assigned)
        if status:
            lq = lq.where(CRMLead.call_status == status)
        leads_result = await db.execute(lq)
        all_leads = leads_result.scalars().all()
        latest_calls = await _latest_calls_map(db, [l.id for l in all_leads])
        for lead in all_leads:
            gid = str(lead.group_id)
            leads_by_group.setdefault(gid, []).append(
                _lead_dict(lead, latest_calls.get(str(lead.id)))
            )
            pills = pills_by_group.setdefault(gid, {s: 0 for s in PILL_STATUSES})
            if lead.call_status in pills:
                pills[lead.call_status] += 1

    # All active users for assign dropdown
    users_result = await db.execute(
        select(User).where(User.status == True).order_by(User.full_name)
    )
    all_users = users_result.scalars().all()

    summary = await _compute_summary(db, q=q, customer=customer, assigned=assigned)
    asl_status_options = await _asl_status_options(db)

    # Item 20: single flat "Social Media Leads" table replaces the per-group
    # accordions — every lead from every group, newest first, tagged with its
    # group name (kept as an extra column since dropping it would remove the
    # only way to tell which ad campaign a lead came from).
    group_name_by_id = {str(g.id): g.name for g in groups}
    all_leads_flat = []
    for gid, leads in leads_by_group.items():
        gname = group_name_by_id.get(gid, "—")
        for ld in leads:
            all_leads_flat.append({**ld, "group_name": gname})
    all_leads_flat.sort(key=lambda l: l.get("lead_date") or "", reverse=True)

    return templates.TemplateResponse("crm/assign_leads.html", {
        "request":          request,
        "current_user":     current_user,
        "all_leads_flat":   all_leads_flat,
        "groups":           groups,
        "leads_by_group":   leads_by_group,
        "pills_by_group":   pills_by_group,
        "pill_statuses":    PILL_STATUSES,
        "all_users":        all_users,
        "q":                q,
        "customer":         customer,
        "assigned":         assigned,
        "status":           status,
        "platforms":        LEAD_PLATFORMS,
        "contact_modes":    LEAD_CONTACT_MODES,
        "device_categories": LEAD_DEVICE_CATEGORIES,
        "outcomes":         asl_status_options,
        "dealing_grades_options": LEAD_DEALING_GRADES,
        "whom_to_sell_options":   LEAD_WHOM_TO_SELL,
        "deals_in_options":       LEAD_DEALS_IN,
        "summary":          summary,
    })


# ── SUMMARY CARDS ─────────────────────────────────────────────────────────────

async def _compute_summary(db: AsyncSession, q: str = "", customer: str = "", assigned: str = "") -> dict:
    """Aggregate counts for the two summary card rows, scoped to the same
    filters as the group/lead list above."""
    gq = select(CRMLeadGroup.id)
    if q:
        gq = gq.where(CRMLeadGroup.name.ilike(f"%{q}%"))
    group_ids = [row[0] for row in (await db.execute(gq)).all()]

    if not group_ids:
        empty_counts = lambda keys: {k: 0 for k in keys}
        return {
            "platform":   {"Facebook": 0, "Instagram": 0, "Google Ads": 0},
            "connection": {"interested": 0, "no_answer": 0, "not_interested": 0},
            "calling":    {"callback": 0, "followup": 0, "order_placed": 0},
            "quantity":   {"purchase": 0, "sale": 0},
            "grades":     {g: 0 for g in LEAD_DEALING_GRADES},
            "categories": {c: 0 for c in LEAD_DEVICE_CATEGORIES},
        }

    lq = select(CRMLead).where(CRMLead.group_id.in_(group_ids))
    if customer:
        like = f"%{customer}%"
        lq = lq.where(or_(CRMLead.name.ilike(like), CRMLead.phone.ilike(like), CRMLead.email.ilike(like)))
    if assigned:
        lq = lq.where(CRMLead.assigned_to == assigned)
    leads = (await db.execute(lq)).scalars().all()

    def _to_num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0

    platform_counts   = {"Facebook": 0, "Instagram": 0, "Google Ads": 0}
    connection_counts = {"interested": 0, "no_answer": 0, "not_interested": 0}
    calling_counts     = {"callback": 0, "followup": 0, "order_placed": 0}
    purchase_total, sale_total = 0, 0
    grade_counts    = {g: 0 for g in LEAD_DEALING_GRADES}
    category_counts = {c: 0 for c in LEAD_DEVICE_CATEGORIES}

    for lead in leads:
        if lead.platform in platform_counts:
            platform_counts[lead.platform] += 1
        if lead.call_status in connection_counts:
            connection_counts[lead.call_status] += 1
        if lead.call_status in calling_counts:
            calling_counts[lead.call_status] += 1
        purchase_total += _to_num(lead.purchase_quantity)
        sale_total     += _to_num(lead.selling_quantity)
        for g in _cats(lead.dealing_grades):
            if g in grade_counts:
                grade_counts[g] += 1
        for c in _cats(lead.device_categories):
            if c in category_counts:
                category_counts[c] += 1

    return {
        "platform":   platform_counts,
        "connection": connection_counts,
        "calling":    calling_counts,
        "quantity":   {"purchase": int(purchase_total) if purchase_total == int(purchase_total) else purchase_total,
                       "sale": int(sale_total) if sale_total == int(sale_total) else sale_total},
        "grades":     grade_counts,
        "categories": category_counts,
    }


@router.get("/summary")
async def get_summary(
    q: str = Query(default=""),
    customer: str = Query(default=""),
    assigned: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return JSONResponse(await _compute_summary(db, q=q, customer=customer, assigned=assigned))


# ── GROUP ROWS REFRESH (used by JS after any modal submit — no page reload) ──

@router.get("/group/{group_id}/leads")
async def get_group_leads(
    group_id: str,
    status: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(CRMLead).where(CRMLead.group_id == group_id)
    if status:
        query = query.where(CRMLead.call_status == status)
    result = await db.execute(query.order_by(CRMLead.created_at.desc()))
    leads = result.scalars().all()
    latest_calls = await _latest_calls_map(db, [l.id for l in leads])
    pills = {s: 0 for s in PILL_STATUSES}
    for l in leads:
        if l.call_status in pills:
            pills[l.call_status] += 1
    return JSONResponse({
        "leads": [_lead_dict(l, latest_calls.get(str(l.id))) for l in leads],
        "pills": pills,
    })


# ── GROUP CRUD ────────────────────────────────────────────────────────────────

@router.post("/group")
async def create_group(
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    group = CRMLeadGroup(name=name.strip(), created_by=current_user.username)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return JSONResponse({"id": str(group.id), "name": group.name, "lead_count": 0})


@router.post("/group/{group_id}/edit")
async def edit_group(
    group_id: str,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMLeadGroup).where(CRMLeadGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        return JSONResponse({"error": "Group not found"}, status_code=404)
    group.name = name.strip()
    group.updated_at = app_now()
    await db.commit()
    return JSONResponse({"ok": True, "name": group.name})


# ── SAMPLE CSV ────────────────────────────────────────────────────────────────

@router.get("/sample")
async def download_sample():
    headers = [
        "lead_date", "platform", "device_categories", "purchase_quantity",
        "planning_to_buy", "contact_mode", "name", "phone", "email", "address",
        "assigned_to",
    ]
    rows = [
        headers,
        [
            "2026-06-30", "Facebook", "Laptop,Desktop", "10",
            "This week", "Phone Call", "Rahul Sharma", "9876543210",
            "rahul@email.com", "123 MG Road, Delhi", "",
        ],
        [
            "2026-07-01", "Instagram", "Monitor", "5",
            "Next month", "WhatsApp", "Priya Singh", "9988776655",
            "", "", "",
        ],
    ]
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return Response(
        content=buf.getvalue().encode("utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=assign_leads_sample.csv"},
    )


# ── IMPORT ────────────────────────────────────────────────────────────────────

@router.post("/import/{group_id}")
async def import_leads(
    group_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMLeadGroup).where(CRMLeadGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        return JSONResponse({"error": "Group not found"}, status_code=404)

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except Exception:
        text = content.decode("latin-1")

    imported, errors = 0, []
    for i, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        try:
            ld = None
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                try:
                    ld = datetime.strptime((row.get("lead_date") or "").strip(), fmt).date()
                    break
                except ValueError:
                    pass

            cats_raw = (row.get("device_categories") or "").strip()
            cats = [c.strip() for c in cats_raw.split(",") if c.strip()]

            lead = CRMLead(
                lead_id=await _next_lead_id(db),
                group_id=group.id,
                lead_date=ld,
                platform=(row.get("platform") or "").strip() or None,
                device_categories=_json.dumps(cats) if cats else None,
                purchase_quantity=(row.get("purchase_quantity") or "").strip() or None,
                planning_to_buy=(row.get("planning_to_buy") or "").strip() or None,
                contact_mode=(row.get("contact_mode") or "").strip() or None,
                name=(row.get("name") or "").strip() or None,
                phone=(row.get("phone") or "").strip() or None,
                email=(row.get("email") or "").strip() or None,
                address=(row.get("address") or "").strip() or None,
                assigned_to=(row.get("assigned_to") or "").strip() or None,
                created_by=current_user.username,
            )
            db.add(lead)
            await db.flush()
            imported += 1
        except Exception as exc:
            errors.append(f"Row {i}: {exc}")

    await db.commit()
    return JSONResponse({"imported": imported, "errors": errors})


# ── LEAD CRUD ─────────────────────────────────────────────────────────────────

@router.post("/lead")
async def add_lead(
    group_id: str = Form(...),
    lead_date: str = Form(""),
    platform: str = Form(""),
    device_categories: str = Form("[]"),
    purchase_quantity: str = Form(""),
    selling_quantity: str = Form(""),
    whom_to_sell: str = Form(""),
    deals_in: str = Form(""),
    dealing_grades: str = Form("[]"),
    planning_to_buy: str = Form(""),
    contact_mode: str = Form(""),
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    assigned_to: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMLeadGroup).where(CRMLeadGroup.id == group_id))
    if not result.scalar_one_or_none():
        return JSONResponse({"error": "Group not found"}, status_code=404)

    ld = None
    if lead_date:
        try:
            ld = datetime.strptime(lead_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    try:
        cats = _json.loads(device_categories) if device_categories else []
    except Exception:
        cats = []
    try:
        grades = _json.loads(dealing_grades) if dealing_grades else []
    except Exception:
        grades = []

    lead = CRMLead(
        lead_id=await _next_lead_id(db),
        group_id=group_id,
        lead_date=ld,
        platform=platform.strip() or None,
        device_categories=_json.dumps(cats) if cats else None,
        purchase_quantity=purchase_quantity.strip() or None,
        selling_quantity=selling_quantity.strip() or None,
        whom_to_sell=whom_to_sell.strip() or None,
        deals_in=deals_in.strip() or None,
        dealing_grades=_json.dumps(grades) if grades else None,
        planning_to_buy=planning_to_buy.strip() or None,
        contact_mode=contact_mode.strip() or None,
        name=name.strip() or None,
        phone=phone.strip() or None,
        email=email.strip() or None,
        address=address.strip() or None,
        assigned_to=assigned_to.strip() or None,
        created_by=current_user.username,
    )
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return JSONResponse({"ok": True, "lead": _lead_dict(lead)})


@router.post("/lead/{lead_id}/edit")
async def edit_lead(
    lead_id: str,
    lead_date: str = Form(""),
    platform: str = Form(""),
    device_categories: str = Form("[]"),
    purchase_quantity: str = Form(""),
    selling_quantity: str = Form(""),
    whom_to_sell: str = Form(""),
    deals_in: str = Form(""),
    dealing_grades: str = Form("[]"),
    planning_to_buy: str = Form(""),
    contact_mode: str = Form(""),
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    assigned_to: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMLead).where(CRMLead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        return JSONResponse({"error": "Lead not found"}, status_code=404)

    ld = None
    if lead_date:
        try:
            ld = datetime.strptime(lead_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    try:
        cats = _json.loads(device_categories) if device_categories else []
    except Exception:
        cats = []
    try:
        grades = _json.loads(dealing_grades) if dealing_grades else []
    except Exception:
        grades = []

    lead.lead_date         = ld
    lead.platform          = platform.strip() or None
    lead.device_categories = _json.dumps(cats) if cats else None
    lead.purchase_quantity = purchase_quantity.strip() or None
    lead.selling_quantity  = selling_quantity.strip() or None
    lead.whom_to_sell      = whom_to_sell.strip() or None
    lead.deals_in          = deals_in.strip() or None
    lead.dealing_grades    = _json.dumps(grades) if grades else None
    lead.planning_to_buy   = planning_to_buy.strip() or None
    lead.contact_mode      = contact_mode.strip() or None
    lead.name              = name.strip() or None
    lead.phone             = phone.strip() or None
    lead.email             = email.strip() or None
    lead.address           = address.strip() or None
    lead.assigned_to       = assigned_to.strip() or None
    lead.updated_at        = app_now()

    await db.commit()
    return JSONResponse({"ok": True})


# ── CALL LOG ──────────────────────────────────────────────────────────────────

@router.post("/lead/{lead_id}/call")
async def log_call(
    lead_id: str,
    calling_date: str = Form(...),
    followup_date: str = Form(""),
    outcome: str = Form(""),          # shown to users as "Status"
    device_categories: str = Form("[]"),
    quantity: str = Form(""),
    full_remarks: str = Form(""),
    purchase_quantity: str = Form(""),
    selling_quantity: str = Form(""),
    whom_to_sell: str = Form(""),
    deals_in: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMLead).where(CRMLead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        return JSONResponse({"error": "Lead not found"}, status_code=404)

    try:
        cd = datetime.strptime(calling_date, "%Y-%m-%d").date()
    except ValueError:
        cd = date.today()

    fd = None
    if followup_date:
        try:
            fd = datetime.strptime(followup_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    try:
        cats = _json.loads(device_categories) if device_categories else []
    except Exception:
        cats = []

    call = CRMLeadCall(
        lead_id=lead.id,
        calling_date=cd,
        followup_date=fd,
        outcome=outcome.strip() or None,
        device_categories=_json.dumps(cats) if cats else None,
        quantity=quantity.strip() or None,
        full_remarks=full_remarks.strip() or None,
        purchase_quantity=purchase_quantity.strip() or None,
        selling_quantity=selling_quantity.strip() or None,
        whom_to_sell=whom_to_sell.strip() or None,
        deals_in=deals_in.strip() or None,
        logged_by=current_user.username,
    )
    db.add(call)

    # Update lead's latest call status
    if outcome:
        lead.call_status = outcome.strip()
        lead.updated_at = app_now()

    await db.commit()
    return JSONResponse({"ok": True})


# ── CALL HISTORY ─────────────────────────────────────────────────────────────

@router.get("/lead/{lead_id}/history")
async def get_call_history(
    lead_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(CRMLeadCall)
        .where(CRMLeadCall.lead_id == lead_id)
        .order_by(CRMLeadCall.created_at.desc())
    )
    calls = []
    for c in result.scalars().all():
        calls.append({
            "calling_date":      c.calling_date.strftime("%d-%m-%Y") if c.calling_date else "",
            "followup_date":     c.followup_date.strftime("%d-%m-%Y") if c.followup_date else "",
            "outcome":           c.outcome or "",       # shown to users as "Status"
            "device_categories": _cats(c.device_categories),
            "quantity":          c.quantity,
            "full_remarks":      c.full_remarks or "",
            "purchase_quantity": c.purchase_quantity or "",
            "selling_quantity":  c.selling_quantity or "",
            "whom_to_sell":      c.whom_to_sell or "",
            "deals_in":          c.deals_in or "",
            "logged_by":         c.logged_by,
        })
    return JSONResponse({"calls": calls})
