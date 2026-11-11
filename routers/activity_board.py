"""Activity Board (2026-11-06, Pankaj) — one page for work waiting on people:

  * Supplier Deals — Sourcing Deals at stage Inspection with someone assigned
    to Inspection. Verify Inspection -> "Inspection Started"; uploading the
    Inspection Report -> "Inspection Done" (Reupload replaces the file).
  * Customer Deals — Buyer Deals at stage Screening, same flow (Screening).
  * Account Verification — accounts with their Manager and Credit; Verify
    Credit (account's mapped AVP/VP or Team Leader, or admin) and Map Manager.

Status and file live on the deal's own Stage Progress items (crm_sourcing_steps
/ crm_sales_steps: *_assign = who, *_report = status + file), so the deal
detail page shows the same thing. No new tables.

Who sees what (2026-11-09, Pankaj): the page is three cards, in this order —
Account Verification, Supplier Deals, Customer Deals — each with its own date
filter and Export in the card header.
  * Admin / Activity Board -> Edit: all three cards, every row.
  * Account team members (mapped AVP/VP or Team Leader on any account): all three
    cards; Account Verification lists the accounts they are on, the deal cards
    only the deals assigned to them.
  * Everyone else: only the Supplier and/or Customer card, and only when a deal
    is assigned to them there (Inspection / Screening).
"""
import os
import uuid as _uuid
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
import csv
import io
from datetime import date, datetime, time

from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import UPLOADS_DIR
from database import get_db
from auth.dependencies import get_current_user, verify_csrf
from models.user import User, ROLE_LABELS
from models.crm import (CRMContact, CRMSourcingDeal, CRMSourcingStep, CRMSalesOpportunity, CRMSalesStep,
                        CRMActivity, CRMQuote, SOURCING_STEP_INDEX, SALES_STEP_INDEX)
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


ACCOUNT_BUCKETS = ("vp_avp", "am_dm_gm")   # mapped on an account = an Account Verification user


async def account_user(db, user) -> bool:
    """Sees all three cards: admin / Activity Board -> Edit, or mapped VP/AVP
    or Team Leader on any account (they verify credit)."""
    if see_all(user):
        return True
    hit = (await db.execute(select(CRMContactTeamMember.id).where(
        CRMContactTeamMember.user_id == user.id,
        CRMContactTeamMember.role_bucket.in_(ACCOUNT_BUCKETS)).limit(1))).first()
    return hit is not None


def _day(v):
    """'YYYY-MM-DD' -> date, else None."""
    try:
        return date.fromisoformat((v or "").strip()[:10])
    except ValueError:
        return None


def _in_range(q, col, d_from, d_to):
    if d_from:
        q = q.where(col >= datetime.combine(d_from, time.min))
    if d_to:
        q = q.where(col <= datetime.combine(d_to, time.max))
    return q


def _number(deal):
    return getattr(deal, "deal_number", None) or getattr(deal, "opp_number", "")


STAGE_CARD_ANCHORS = ("stage-inspection", "stage-screening")
REPORT_DOC_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}


def _back(tab, msg, ok=True):
    # Back to that tab (2026-11-09: #ab-<card> opens its tab on the board);
    # the Inspection / Screening cards below the tabs come back to their card.
    return RedirectResponse(url=f"/activity-board?{'success' if ok else 'error'}={quote_plus(msg)}#ab-{tab}",
                            status_code=302)


def status_of(report_step) -> str:
    """Pending -> Started (Verify clicked) -> Done (report attached)."""
    if report_step and report_step.file_path:
        return "done"
    if report_step and report_step.status == "started":
        return "started"
    return "pending"


async def _deal_rows(db, kind, user, d_from=None, d_to=None):
    """Deals waiting at this stage with someone assigned; Added On (created_at)
    within the card's date range when one is set."""
    Deal, Step, stage, k_assign, k_report, word, url, _dt = KINDS[kind]
    q = _in_range(select(Deal).where(Deal.stage == stage), Deal.created_at, d_from, d_to)
    deals = (await db.execute(q.order_by(Deal.created_at.desc()))).scalars().all()
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


async def _stage_card_rows(db, kind, user):
    """Inspection (Supplier Deals) / Screening (Customer Deals) cards below the
    tabs (2026-11-10, Pankaj): every deal whose Inspection / Screening has
    someone assigned on the deal's Stage Progress — Pending, In Progress
    (Started) or Completed (report uploaded), shown with the deal's Account.
    Non-admins see only the ones assigned to them. Start / Upload work while
    the deal is still at that stage."""
    Deal, Step, stage, k_assign, k_report, word, url, _dt = KINDS[kind]
    q = (select(Step, Deal).join(Deal, Step.deal_id == Deal.id)
         .where(Step.step_key == k_assign, Step.assigned_to.isnot(None), Step.assigned_to != ""))
    if not see_all(user):
        q = q.where(Step.assigned_to == user.username)
    pairs = (await db.execute(q.order_by(Deal.created_at.desc()))).all()
    if not pairs:
        return [], None, None
    ids = [d.id for _a, d in pairs]
    reports = {s.deal_id: s for s in (await db.execute(
        select(Step).where(Step.deal_id.in_(ids), Step.step_key == k_report))).scalars().all()}
    cids = {d.contact_id for _a, d in pairs if d.contact_id}
    accounts = {c.id: c for c in (await db.execute(select(CRMContact).where(CRMContact.id.in_(cids)))).scalars().all()} if cids else {}
    rows = []
    today = app_now().date()
    for a, d in pairs:
        r = reports.get(d.id)
        # Assigned On + aging (2026-11-11): days since assignment; once the report
        # is in, the days it took (assigned -> report uploaded).
        assigned_on = a.acted_at or a.updated_at or a.created_at
        done_on = r.acted_at if (r and r.file_path and r.acted_at) else None
        age = ((done_on.date() if done_on else today) - assigned_on.date()).days if assigned_on else None
        rows.append({"deal": d, "number": _number(d), "url": f"{url}{d.id}", "assignee": a.assigned_to,
                     "assigned_on": assigned_on, "age": max(age, 0) if age is not None else None,
                     "account": accounts.get(d.contact_id), "status": status_of(r),
                     "file": r.file_path if r else None, "at_stage": d.stage == stage,
                     "can_act": (d.stage == stage or (bool(r and r.file_path) and d.stage not in ("won", "lost")))
                                and (a.assigned_to == user.username or see_all(user))})
    from services.account_cells import account_cell_maps
    return rows, await account_cell_maps(db, cids), word


async def _account_rows(db, user, d_from=None, d_to=None):
    q = _in_range(select(CRMContact).where(CRMContact.is_trashed.isnot(True)), CRMContact.created_at, d_from, d_to)
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


# Approval tabs (2026-11-10, Pankaj): Negotiation / Pricing Approval Level 1
# items of both Supplier and Customer Deals with an approver assigned, in
# Pending / Approved / Rejected. Approve / Reject posts to the deal's own
# decide route (same rules: the assigned approver or an admin) and comes back.
# Quotations tab (2026-11-11, Pankaj): Quote Level 1 + Level 2 — one row per
# level, decided in any order; the stage is Done once every level approves.
APPROVAL_TABS = {
    "negotiation": (("negotiation_l1",), "Negotiation"),
    "pricing": (("pricing_l1",), "Pricing Approval"),
    "quotations": (("quote_l1", "quote_l2"), "Quotations"),
}
LEVEL_LABELS = {"l1": "Level 1", "l2": "Level 2"}

CARDS = ("accounts", "supplier", "customer", "negotiation", "pricing", "quotations")   # page order
EXPORT_CARDS = ("accounts", "supplier", "customer")


async def _approval_rows(db, tab, user, d_from=None, d_to=None):
    """Approval items of this tab on both deal types; deal Added On within the
    card's date range when set. Non-admins see only items assigned to them."""
    keys, _word = APPROVAL_TABS[tab]
    everyone = see_all(user)
    rows = []
    for kind in ("supplier", "customer"):
        Deal, Step, _st, _a, _r, _w, url, _dt = KINDS[kind]
        q = select(Step, Deal).join(Deal, Step.deal_id == Deal.id).where(
            Step.step_key.in_(keys), Step.assigned_to.isnot(None), Step.assigned_to != "")
        if not everyone:
            q = q.where(Step.assigned_to == user.username)
        q = _in_range(q, Deal.created_at, d_from, d_to)
        pairs = (await db.execute(q)).all()
        ids = list({d.id for _s, d in pairs})
        quotes = {}
        if ids and tab in ("quotations", "negotiation"):   # Negotiation carries the quote forward (2026-11-11)
            fk = CRMQuote.sourcing_deal_id if kind == "supplier" else CRMQuote.opportunity_id
            for qt in (await db.execute(select(CRMQuote).where(fk.in_(ids)).order_by(CRMQuote.created_at))).scalars().all():
                quotes[getattr(qt, fk.key)] = qt          # latest quote of the deal
        for st, d in pairs:
            status = st.status if st.status in ("approved", "rejected") else "pending"
            qt = quotes.get(d.id)
            rows.append({"deal": d, "number": _number(d), "kind": kind, "url": f"{url}{d.id}",
                         "decide": f"{url}{d.id}/steps/{st.step_key}/decide", "assignee": st.assigned_to,
                         "level": LEVEL_LABELS.get(st.step_key.rsplit("_", 1)[-1], ""),
                         "quote": ({"number": qt.quote_number, "url": f"/crm/quotes/{qt.id}"} if qt else None),
                         "status": status,
                         "note": st.note, "acted_by": st.acted_by, "acted_at": st.acted_at,
                         "can_decide": status == "pending" and d.stage not in ("won", "lost")
                                       and (st.assigned_to == user.username or _role(user) == "admin")})
    rows.sort(key=lambda r: (r["deal"].created_at or datetime.min, r["level"] == "Level 1"), reverse=True)
    return rows


def _ranges(request):
    """Each card's own date filter: acc_from/acc_to, sup_from/sup_to, cus_from/cus_to,
    neg_from/neg_to, prc_from/prc_to."""
    qp = request.query_params
    return {k: (_day(qp.get(f"{p}_from")), _day(qp.get(f"{p}_to")))
            for k, p in (("accounts", "acc"), ("supplier", "sup"), ("customer", "cus"),
                         ("negotiation", "neg"), ("pricing", "prc"), ("quotations", "qtn"))}


async def _visible(db, user):
    """Which cards this user sees (see the module docstring)."""
    if await account_user(db, user):
        return set(CARDS)
    out = set()
    for kind in ("supplier", "customer"):
        if await _deal_rows(db, kind, user):     # anything assigned to them, any date
            out.add(kind)
    for tab in APPROVAL_TABS:
        if await _approval_rows(db, tab, user):  # an approval assigned to them
            out.add(tab)
    return out


@router.get("", response_class=HTMLResponse)
async def activity_board(request: Request, db: AsyncSession = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    from models.role_permissions import has_perm
    if not has_perm(_role(current_user), MODULE, "enable"):
        return RedirectResponse(url="/dashboard?error=Activity+Board+is+not+enabled+for+your+role", status_code=302)
    from routers.crm_contacts import can_verify_credit, credit_deal_totals
    from services.crm_team import eligible_users_for_bucket
    rng = _ranges(request)
    visible = await _visible(db, current_user)
    supplier = await _deal_rows(db, "supplier", current_user, *rng["supplier"]) if "supplier" in visible else []
    customer = await _deal_rows(db, "customer", current_user, *rng["customer"]) if "customer" in visible else []
    contacts, team = (await _account_rows(db, current_user, *rng["accounts"])) if "accounts" in visible else ([], {})
    approvals = {t: (await _approval_rows(db, t, current_user, *rng[t]) if t in visible else []) for t in APPROVAL_TABS}
    can_vc, totals = {}, {}
    for c in contacts:
        if not c.credit_verified:
            can_vc[c.id] = await can_verify_credit(db, c.id, current_user)
            if can_vc[c.id]:
                totals[c.id] = await credit_deal_totals(db, c.id)
    stage_cards = {}
    for kind, key in (("supplier", "inspection"), ("customer", "screening")):
        rows, maps, _w = await _stage_card_rows(db, kind, current_user)
        if rows or see_all(current_user):      # a card shows only to the people it's assigned to
            locs, nums, tms = maps if maps else ({}, {}, {})
            stage_cards[key] = {"kind": kind, "rows": rows, "locs": locs, "nums": nums, "team": tms}
    users = (await db.execute(select(User).where(User.status == True))).scalars().all()  # noqa: E712
    names = {u.username: (u.full_name or u.username) for u in users}
    # Map Manager (2026-11-11 PM, Pankaj): Executive + Team Leader dropdowns; the
    # levels above come from Report To by designation (services/crm_team).
    from services.crm_team import bucket_options as _bopts, designation_hierarchy
    _opts = await _bopts(db, sorted(users, key=lambda u: (u.full_name or u.username or "").lower()))
    managers = {"executive": _opts["executive"], "am_dm_gm": _opts["am_dm_gm"]}
    mgr_levels = designation_hierarchy(users)
    return templates.TemplateResponse("activity_board/index.html", {
        "request": request, "current_user": current_user,
        "cards": [k for k in CARDS if k in visible], "qp": request.query_params,
        "supplier": supplier, "customer": customer, "contacts": contacts, "team": team,
        "approvals": approvals, "approval_tabs": APPROVAL_TABS, "stage_cards": stage_cards,
        "can_vc": can_vc, "totals": totals, "names": names, "managers": managers, "mgr_levels": mgr_levels,
        "see_all": see_all(current_user), "role_labels": ROLE_LABELS,
    })


@router.get("/export/{kind}")
async def export_card(kind: str, request: Request, db: AsyncSession = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """Export (2026-11-09): one card's rows as CSV — the same rows the card
    shows for this user, within its date filter (?from=&to=)."""
    from models.role_permissions import has_perm
    if kind not in EXPORT_CARDS or not has_perm(_role(current_user), MODULE, "enable"):
        return RedirectResponse(url="/activity-board?error=Export+not+available", status_code=302)
    if kind not in await _visible(db, current_user):
        return RedirectResponse(url="/activity-board?error=Nothing+to+export+here", status_code=302)
    d_from, d_to = _day(request.query_params.get("from")), _day(request.query_params.get("to"))
    users = (await db.execute(select(User))).scalars().all()
    names = {u.username: (u.full_name or u.username) for u in users}
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "accounts":
        contacts, team = await _account_rows(db, current_user, d_from, d_to)
        w.writerow(["Account Name", "Type", "Manager", "GST No.", "KYC", "Credit", "City", "State",
                    "Contact Person", "AVP/VP", "Team Leader", "Executive", "Finance Manager", "Status", "Created By", "Created On"])
        for c in contacts:
            t = team.get(c.id, {})
            nm = lambda b: ", ".join(x["name"] or "" for x in t.get(b, []))  # noqa: E731
            w.writerow([c.company_name, (c.contact_type or "").title(), nm("manager"), c.gstin or "",
                        "Verified" if c.kyc_verified else "Pending", "Verified" if c.credit_verified else "Pending",
                        c.city or "", c.state or "", c.contact_person or "", nm("vp_avp"), nm("am_dm_gm"),
                        nm("executive"), nm("finance"), (c.status or "").title(), names.get(c.created_by, c.created_by or ""),
                        c.created_at.strftime("%Y-%m-%d") if c.created_at else ""])
        title = "Account_Verification"
    else:
        word = KINDS[kind][5]
        rows = await _deal_rows(db, kind, current_user, d_from, d_to)
        w.writerow(["Supplier Deal ID" if kind == "supplier" else "Customer Deal ID", "Title", "Assigned To",
                    "Added On", "Added By", "Status", f"{word} Report"])
        for r in rows:
            d = r["deal"]
            w.writerow([r["number"], d.title or "", names.get(r["assignee"], r["assignee"]),
                        d.created_at.strftime("%Y-%m-%d") if d.created_at else "",
                        names.get(d.created_by, d.created_by or ""), f"{word} {r['status'].title()}",
                        "Yes" if r["file"] else "No"])
        title = "Supplier_Deals" if kind == "supplier" else "Customer_Deals"
    fname = f"Activity_Board_{title}_{app_now().strftime('%Y%m%d')}.csv"
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


async def _load(db, kind, deal_id, user, reupload=False):
    Deal, Step, stage, k_assign, k_report, *_ = KINDS[kind]
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        return None, None, "Deal not found"
    a = (await db.execute(select(Step).where(Step.deal_id == deal.id, Step.step_key == k_assign))).scalar_one_or_none()
    r = (await db.execute(select(Step).where(Step.deal_id == deal.id, Step.step_key == k_report))).scalar_one_or_none()
    # A finished report moves the deal on (2026-11-11), but its Reupload stays allowed
    _reup = reupload and r is not None and bool(r.file_path) and deal.stage not in ("won", "lost")
    if (deal.stage != stage and not _reup) or not a or not a.assigned_to:
        return None, None, "This deal is no longer waiting here"
    if a.assigned_to != user.username and not see_all(user):
        return None, None, "Only the assigned person can do this"
    return deal, r, None


def _note(kind, deal, user, text):
    return CRMActivity(contact_id=deal.contact_id, deal_id=deal.id, deal_type=KINDS[kind][7],
                       activity_type="note", summary=text, performed_by=user.username, outcome="done")


@router.post("/{kind}/{deal_id}/start")
async def start_check(kind: str, deal_id: str, request: Request, back: str = Form(default=""),
                      db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Verify Inspection / Verify Screening -> "… Started"."""
    if kind not in KINDS:
        return _back(back if back in STAGE_CARD_ANCHORS else "supplier", "Unknown board", ok=False)
    Deal, Step, stage, _ka, k_report, word, *_ = KINDS[kind]
    deal, r, err = await _load(db, kind, deal_id, current_user)
    if err:
        return _back(back if back in STAGE_CARD_ANCHORS else kind, err, ok=False)
    if r and (r.file_path or r.status == "started"):
        return _back(back if back in STAGE_CARD_ANCHORS else kind, f"{_number(deal)}: {word} already started")
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
    return _back(back if back in STAGE_CARD_ANCHORS else kind, f"{_number(deal)}: {word} started")


@router.post("/{kind}/{deal_id}/report")
async def upload_report(kind: str, deal_id: str, request: Request, file: UploadFile = File(...),
                        back: str = Form(default=""),
                        db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Inspection / Screening Report upload (and Reupload) -> "… Done". The
    file is the deal's own Stage Progress report, so the deal page shows it."""
    if kind not in KINDS:
        return _back(back if back in STAGE_CARD_ANCHORS else "supplier", "Unknown board", ok=False)
    Deal, Step, stage, _ka, k_report, word, *_ = KINDS[kind]
    deal, r, err = await _load(db, kind, deal_id, current_user, reupload=True)
    if err:
        return _back(back if back in STAGE_CARD_ANCHORS else kind, err, ok=False)
    if not r or not (r.file_path or r.status == "started"):
        return _back(back if back in STAGE_CARD_ANCHORS else kind, f"Click Verify {word} first", ok=False)
    if not file or not file.filename:
        return _back(back if back in STAGE_CARD_ANCHORS else kind, "No file selected", ok=False)
    ext = os.path.splitext(file.filename)[1].lower()
    if back in STAGE_CARD_ANCHORS:
        # Inspection / Screening cards (2026-11-10): reports in PDF, Word, PowerPoint or Excel
        if ext not in REPORT_DOC_EXTS:
            return _back(back, "Allowed files: PDF, Word, PowerPoint or Excel", ok=False)
    elif ext not in STEP_FILE_EXTS:
        return _back(back if back in STAGE_CARD_ANCHORS else kind, "Allowed files: PDF, image, Excel or Word", ok=False)
    content = await file.read()
    if len(content) > STEP_FILE_MAX_BYTES:
        return _back(back if back in STAGE_CARD_ANCHORS else kind, "File is larger than 20 MB", ok=False)
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
    # Completed here -> the deal's Stage Progress shows this stage Done (2026-11-11)
    from services.stage_advance import advance
    from models.crm import SOURCING_STAGES, SALES_STAGES
    _stages = SOURCING_STAGES if kind == "supplier" else SALES_STAGES
    moved = advance(deal, stage, _stages)
    if moved:
        db.add(_note(kind, deal, current_user, f"Stage moved to: {dict(_stages).get(moved[1], moved[1])} ({word} completed on Activity Board)"))
        from services.stage_advance import reopen_approvals
        from models.crm import SOURCING_STAGE_STEPS, SALES_STAGE_STEPS
        if await reopen_approvals(db, Step, deal.id, moved[1], SOURCING_STAGE_STEPS if kind == "supplier" else SALES_STAGE_STEPS):
            db.add(_note(kind, deal, current_user, f"{dict(_stages).get(moved[1], moved[1])} approvals reset to Pending — approve again"))
        await audit(db, action=("CRM_SOURCING_NEXT_STAGE" if kind == "supplier" else "CRM_SALES_NEXT_STAGE"),
                    user=current_user, table_name=("crm_sourcing_deals" if kind == "supplier" else "crm_sales_opportunities"),
                    record_id=str(deal.id), old_value={"stage": moved[0]}, new_value={"stage": moved[1]}, request=request)
    await audit(db, action=f"ACTIVITY_{word.upper()}_REPORT", user=current_user, table_name="crm_steps",
                record_id=str(deal.id), old_value={"file_path": old},
                new_value={"deal": _number(deal), "step": k_report, "file_path": safe, "original": file.filename[:200]},
                request=request)
    await db.commit()
    return _back(back if back in STAGE_CARD_ANCHORS else kind, f"{_number(deal)}: {label} {'replaced' if old else 'uploaded'} — {word} Done")


@router.post("/accounts/{contact_id}/map-manager")
async def map_manager(contact_id: str, request: Request, executive_id: str = Form(default=""),
                      team_leader_id: str = Form(default=""),
                      db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Map Manager (2026-11-11 PM, Pankaj): pick an Executive and / or a Team
    Leader. The Executive (else the Team Leader) is the Account Manager; the
    levels above it come from the Report To chain by designation (a picked
    Team Leader wins over the chain's). Executive / Team Leader / Manager /
    AVP/VP are replaced together; Finance is left as it is."""
    from services.crm_team import replace_team_buckets, designation_hierarchy, team_from_owner, HIERARCHY
    if not see_all(current_user):
        return _back("accounts", "Only admin or Activity Board → Edit can map the team", ok=False)
    contact = (await db.execute(select(CRMContact).where(CRMContact.id == contact_id))).scalar_one_or_none()
    if not contact:
        return _back("accounts", "Account not found", ok=False)

    def _uid(v):
        try:
            return str(_uuid.UUID(v.strip())) if v and v.strip() else None
        except ValueError:
            return None
    ex, tl = _uid(executive_id), _uid(team_leader_id)
    if not (ex or tl):
        return _back("accounts", "Pick an Executive or a Team Leader", ok=False)
    users = (await db.execute(select(User).where(User.status == True))).scalars().all()  # noqa: E712
    by_id = {str(u.id): u for u in users}
    lvl, owner = ("executive", ex) if ex else ("am_dm_gm", tl)
    if owner not in by_id:
        return _back("accounts", "That user is not active", ok=False)
    team = team_from_owner(lvl, owner, designation_hierarchy(users), {"am_dm_gm": tl} if (ex and tl) else None)
    buckets = {b: [_uuid.UUID(x) for x in team.get(b, [])] for b in HIERARCHY}
    old = {}
    for m in (await db.execute(select(CRMContactTeamMember).where(
            CRMContactTeamMember.contact_id == contact.id,
            CRMContactTeamMember.role_bucket.in_(HIERARCHY)))).scalars().all():
        old.setdefault(m.role_bucket, []).append(str(m.user_id))
    old["account_manager"] = contact.assigned_to
    await replace_team_buckets(db, contact_id=contact.id, bucket_user_ids=buckets, created_by=current_user.username)
    contact.assigned_to = by_id[owner].username
    await audit(db, user=current_user, action="TEAM_MAPPING_UPDATED", table_name="crm_contact_team_members",
                record_id=str(contact.id), old_value=old,
                new_value={**{b: [str(x) for x in v] for b, v in buckets.items()},
                           "account_manager": contact.assigned_to, "via": "Activity Board → Map Manager"}, request=request)
    await db.commit()
    nm = by_id[owner].full_name or by_id[owner].username
    return _back("accounts", f"Team mapped for {contact.company_name} — Account Manager {nm}")
