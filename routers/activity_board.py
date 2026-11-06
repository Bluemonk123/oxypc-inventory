"""Activity Board (2026-11-06, Pankaj) — one page for work waiting on people:

  * Supplier Deals — Sourcing Deals at stage Inspection with someone assigned
    to Inspection. Verify Inspection -> "Inspection Started"; uploading the
    Inspection Report -> "Inspection Done" (Reupload replaces the file).
  * Customer Deals — Buyer Deals at stage Screening, same flow (Screening).
  * Account Verification — accounts with their Manager and Credit; Verify
    Credit (account's mapped VP/AVP or AM/DM/GM, or admin) and Map Manager.

Status and file live on the deal's own Stage Progress items (crm_sourcing_steps
/ crm_sales_steps: *_assign = who, *_report = status + file), so the deal
detail page shows the same thing. No new tables.

Who sees what: the person assigned (Inspection / Screening, or mapped on the
account's team) sees their own rows; admin and roles with Activity Board ->
Edit ticked see every row.
"""
import os
import uuid as _uuid
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import UPLOADS_DIR
from database import get_db
from auth.dependencies import get_current_user, verify_csrf
from models.user import User, ROLE_LABELS
from models.crm import (CRMContact, CRMSourcingDeal, CRMSourcingStep, CRMSalesOpportunity, CRMSalesStep,
                        CRMActivity, SOURCING_STEP_INDEX, SALES_STEP_INDEX)
from models.crm_team import CRMContactTeamMember
from services.audit_engine import audit
from templates_config import templates
from utils.timezone import app_now

router = APIRouter(prefix="/activity-board", tags=["activity_board"], dependencies=[Depends(verify_csrf)])

MODULE = "activity_board"
STEP_FILE_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif",
                  ".xls", ".xlsx", ".csv", ".doc", ".docx"}
STEP_FILE_MAX_BYTES = 20 * 1024 * 1024

# per tab: (deal model, step model, stage, assign step, report step, words, detail url, deal_type)
KINDS = {
    "supplier": (CRMSourcingDeal, CRMSourcingStep, "inspection", "inspection_assign", "inspection_report",
                 "Inspection", "/crm/sourcing/", "sourcing"),
    "customer": (CRMSalesOpportunity, CRMSalesStep, "screening", "screening_assign", "screening_report",
                 "Screening", "/crm/sales/", "sales"),
}


def _role(user) -> str:
    return getattr(user.role, "value", user.role)


def see_all(user) -> bool:
    """Admin, or Activity Board -> Edit explicitly ticked for the role."""
    from models.role_permissions import has_explicit_perm, has_perm
    r = _role(user)
    return r == "admin" or (has_explicit_perm(r, MODULE) and has_perm(r, MODULE, "edit"))


def _number(deal):
    return getattr(deal, "deal_number", None) or getattr(deal, "opp_number", "")


def _back(tab, msg, ok=True):
    return RedirectResponse(url=f"/activity-board?tab={tab}&{'success' if ok else 'error'}={quote_plus(msg)}",
                            status_code=302)


def status_of(report_step) -> str:
    """Pending -> Started (Verify clicked) -> Done (report attached)."""
    if report_step and report_step.file_path:
        return "done"
    if report_step and report_step.status == "started":
        return "started"
    return "pending"


async def _deal_rows(db, kind, user):
    Deal, Step, stage, k_assign, k_report, word, url, _dt = KINDS[kind]
    deals = (await db.execute(select(Deal).where(Deal.stage == stage).order_by(Deal.created_at.desc()))).scalars().all()
    if not deals:
        return []
    steps = (await db.execute(select(Step).where(Step.deal_id.in_([d.id for d in deals]),
                                                 Step.step_key.in_((k_assign, k_report))))).scalars().all()
    by = {(s.deal_id, s.step_key): s for s in steps}
    everyone = see_all(user)
    rows = []
    for d in deals:
        a = by.get((d.id, k_assign))
        if not a or not a.assigned_to:            # only deals with someone assigned
            continue
        if not everyone and a.assigned_to != user.username:
            continue
        r = by.get((d.id, k_report))
        rows.append({"deal": d, "number": _number(d), "assignee": a.assigned_to, "status": status_of(r),
                     "file": r.file_path if r else None, "url": f"{url}{d.id}"})
    return rows


async def _account_rows(db, user):
    q = select(CRMContact).where(CRMContact.is_trashed.isnot(True))
    if not see_all(user):
        mine = select(CRMContactTeamMember.contact_id).where(CRMContactTeamMember.user_id == user.id)
        q = q.where(CRMContact.id.in_(mine))
    contacts = (await db.execute(q.order_by(CRMContact.credit_verified, CRMContact.company_name))).scalars().all()
    team = {}
    if contacts:
        for m, name, uid in (await db.execute(
                select(CRMContactTeamMember, User.full_name, User.id)
                .join(User, CRMContactTeamMember.user_id == User.id)
                .where(CRMContactTeamMember.contact_id.in_([c.id for c in contacts])))).all():
            t = team.setdefault(m.contact_id, {})
            t.setdefault(m.role_bucket, []).append({"id": str(uid), "name": name})
    return contacts, team


@router.get("", response_class=HTMLResponse)
async def activity_board(request: Request, tab: str = "supplier", db: AsyncSession = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    from models.role_permissions import has_perm
    if not has_perm(_role(current_user), MODULE, "enable"):
        return RedirectResponse(url="/dashboard?error=Activity+Board+is+not+enabled+for+your+role", status_code=302)
    from routers.crm_contacts import can_verify_credit, credit_deal_totals
    from services.crm_team import eligible_users_for_bucket
    supplier = await _deal_rows(db, "supplier", current_user)
    customer = await _deal_rows(db, "customer", current_user)
    contacts, team = await _account_rows(db, current_user)
    can_vc, totals = {}, {}
    for c in contacts:
        if not c.credit_verified:
            can_vc[c.id] = await can_verify_credit(db, c.id, current_user)
            if can_vc[c.id]:
                totals[c.id] = await credit_deal_totals(db, c.id)
    users = (await db.execute(select(User).where(User.status == True))).scalars().all()  # noqa: E712
    names = {u.username: (u.full_name or u.username) for u in users}
    managers = [(str(u.id), u.full_name or u.username) for u in eligible_users_for_bucket(users, "manager")]
    return templates.TemplateResponse("activity_board/index.html", {
        "request": request, "current_user": current_user, "tab": tab if tab in ("supplier", "customer", "accounts") else "supplier",
        "supplier": supplier, "customer": customer, "contacts": contacts, "team": team,
        "can_vc": can_vc, "totals": totals, "names": names, "managers": managers,
        "see_all": see_all(current_user), "role_labels": ROLE_LABELS,
    })


async def _load(db, kind, deal_id, user):
    Deal, Step, stage, k_assign, k_report, *_ = KINDS[kind]
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        return None, None, "Deal not found"
    a = (await db.execute(select(Step).where(Step.deal_id == deal.id, Step.step_key == k_assign))).scalar_one_or_none()
    if deal.stage != stage or not a or not a.assigned_to:
        return None, None, "This deal is no longer waiting here"
    if a.assigned_to != user.username and not see_all(user):
        return None, None, "Only the assigned person can do this"
    r = (await db.execute(select(Step).where(Step.deal_id == deal.id, Step.step_key == k_report))).scalar_one_or_none()
    return deal, r, None


def _note(kind, deal, user, text):
    return CRMActivity(contact_id=deal.contact_id, deal_id=deal.id, deal_type=KINDS[kind][7],
                       activity_type="note", summary=text, performed_by=user.username, outcome="done")


@router.post("/{kind}/{deal_id}/start")
async def start_check(kind: str, deal_id: str, request: Request, db: AsyncSession = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """Verify Inspection / Verify Screening -> "… Started"."""
    if kind not in KINDS:
        return _back("supplier", "Unknown board", ok=False)
    Deal, Step, stage, _ka, k_report, word, *_ = KINDS[kind]
    deal, r, err = await _load(db, kind, deal_id, current_user)
    if err:
        return _back(kind, err, ok=False)
    if r and (r.file_path or r.status == "started"):
        return _back(kind, f"{_number(deal)}: {word} already started")
    if not r:
        r = Step(deal_id=deal.id, stage=stage, step_key=k_report, created_by=current_user.username)
        db.add(r)
    r.status = "started"
    r.acted_by = current_user.username
    r.acted_at = app_now()
    db.add(_note(kind, deal, current_user, f"{word} started"))
    await audit(db, action=f"ACTIVITY_{word.upper()}_STARTED", user=current_user, table_name="crm_steps",
                record_id=str(deal.id), new_value={"deal": _number(deal), "step": k_report, "status": "started"},
                request=request)
    await db.commit()
    return _back(kind, f"{_number(deal)}: {word} started")


@router.post("/{kind}/{deal_id}/report")
async def upload_report(kind: str, deal_id: str, request: Request, file: UploadFile = File(...),
                        db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Inspection / Screening Report upload (and Reupload) -> "… Done". The
    file is the deal's own Stage Progress report, so the deal page shows it."""
    if kind not in KINDS:
        return _back("supplier", "Unknown board", ok=False)
    Deal, Step, stage, _ka, k_report, word, *_ = KINDS[kind]
    deal, r, err = await _load(db, kind, deal_id, current_user)
    if err:
        return _back(kind, err, ok=False)
    if not r or not (r.file_path or r.status == "started"):
        return _back(kind, f"Click Verify {word} first", ok=False)
    if not file or not file.filename:
        return _back(kind, "No file selected", ok=False)
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in STEP_FILE_EXTS:
        return _back(kind, "Allowed files: PDF, image, Excel or Word", ok=False)
    content = await file.read()
    if len(content) > STEP_FILE_MAX_BYTES:
        return _back(kind, "File is larger than 20 MB", ok=False)
    safe = f"{_uuid.uuid4().hex}{ext}"
    folder = os.path.join(UPLOADS_DIR, "crm")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, safe), "wb") as fh:
        fh.write(content)
    old = r.file_path
    r.file_path = safe
    r.status = "done"
    r.acted_by = current_user.username
    r.acted_at = app_now()
    label = f"{word} Report"
    db.add(_note(kind, deal, current_user, f"{label} {'re-uploaded' if old else 'attached'} ({file.filename[:120]})"))
    await audit(db, action=f"ACTIVITY_{word.upper()}_REPORT", user=current_user, table_name="crm_steps",
                record_id=str(deal.id), old_value={"file_path": old},
                new_value={"deal": _number(deal), "step": k_report, "file_path": safe, "original": file.filename[:200]},
                request=request)
    await db.commit()
    return _back(kind, f"{_number(deal)}: {label} {'replaced' if old else 'uploaded'} — {word} Done")


@router.post("/accounts/{contact_id}/map-manager")
async def map_manager(contact_id: str, request: Request, manager_id: str = Form(default=""),
                      db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Map Manager: sets the account's Manager (Map Team's Manager bucket only;
    the other buckets are left as they are). Blank clears it."""
    from services.crm_team import replace_team_buckets
    if not see_all(current_user):
        return _back("accounts", "Only admin or Activity Board → Edit can map the Manager", ok=False)
    contact = (await db.execute(select(CRMContact).where(CRMContact.id == contact_id))).scalar_one_or_none()
    if not contact:
        return _back("accounts", "Account not found", ok=False)
    ids = []
    if manager_id.strip():
        try:
            ids = [_uuid.UUID(manager_id.strip())]
        except ValueError:
            return _back("accounts", "Select a manager", ok=False)
    old = [str(m.user_id) for m in (await db.execute(select(CRMContactTeamMember).where(
        CRMContactTeamMember.contact_id == contact.id, CRMContactTeamMember.role_bucket == "manager"))).scalars().all()]
    await replace_team_buckets(db, contact_id=contact.id, bucket_user_ids={"manager": ids},
                               created_by=current_user.username)
    await audit(db, user=current_user, action="TEAM_MAPPING_UPDATED", table_name="crm_contact_team_members",
                record_id=str(contact.id), old_value={"manager": old},
                new_value={"manager": [str(i) for i in ids], "via": "Activity Board → Map Manager"}, request=request)
    await db.commit()
    return _back("accounts", f"Manager {'mapped' if ids else 'cleared'} for {contact.company_name}")
