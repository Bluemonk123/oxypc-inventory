"""Accounts & Payments — supplier payments and customer receipts."""
import csv
from urllib.parse import quote_plus
import io
import os
import uuid as _uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape as esc
from uuid import UUID
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from templates_config import templates
from database import get_db
from config import UPLOADS_DIR
from auth.dependencies import get_current_user, verify_csrf
from services.audit_engine import audit
from models.user import User, UserRole
from models.crm import (
    SupplierPayment, CustomerReceipt, CRMContact,
    CRMSourcingDeal, CRMSalesOpportunity, CRMPurchaseOrder, CRMQuote, CRMSalesOrder,
)
from utils.timezone import app_now
from models.dealers import Dealer, DealerOrder
from models.lot import Lot

router = APIRouter(prefix="/accounts", tags=["accounts"])

FINANCE_ROLES = (UserRole.admin, UserRole.inventory_manager, UserRole.sales_manager)
PAYMENT_MODES = ["cash", "upi", "neft", "rtgs", "cheque", "card"]


def _payment_fields(payment_date, payment_amount, payment_mode):
    """Complete Payment fields (2026-11-09, Pankaj): Payment Date, Payment
    Amount, Payment UTR/Invoice Number, Payment Mode. Returns (date, Decimal,
    mode, error)."""
    try:
        pd = date.fromisoformat((payment_date or "").strip())
    except ValueError:
        return None, None, None, "Enter a valid Payment Date"
    try:
        amt = Decimal(str(payment_amount).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None, None, None, "Enter a valid Payment Amount"
    if amt <= 0:
        return None, None, None, "Payment Amount must be more than 0"
    mode = (payment_mode or "").strip().lower()
    if mode not in PAYMENT_MODES:
        return None, None, None, "Choose a Payment Mode"
    return pd, amt.quantize(Decimal("0.01")), mode, None


def _parse_date(v: str) -> date:
    try:
        return date.fromisoformat(v) if v and v.strip() else date.today()
    except ValueError:
        return date.today()


async def _save_upload(upload: UploadFile) -> str:
    """Save an uploaded file under uploads/accounts/ with a UUID-hex safe
    name, same pattern as CRM Sourcing's document uploads. Returns None if
    no file was actually selected."""
    if not upload or not upload.filename:
        return None
    ext = os.path.splitext(upload.filename)[1].lower()
    uploads_dir = os.path.join(UPLOADS_DIR, "accounts")
    os.makedirs(uploads_dir, exist_ok=True)
    safe_name = f"{_uuid.uuid4().hex}{ext}"
    dest = os.path.join(uploads_dir, safe_name)
    content = await upload.read()
    with open(dest, "wb") as f:
        f.write(content)
    return safe_name


@router.get("", response_class=HTMLResponse)
async def accounts_index(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in FINANCE_ROLES:
        return RedirectResponse(url="/?error=Access+denied", status_code=302)
    sp_total = (await db.execute(
        select(func.coalesce(func.sum(SupplierPayment.amount), 0))
    )).scalar()
    cr_total = (await db.execute(
        select(func.coalesce(func.sum(CustomerReceipt.amount), 0))
    )).scalar()
    # ── Pending lists (2026-11-01) ─────────────────────────────────────────
    pend_kyc = (await db.execute(
        select(CRMContact)
        .where(CRMContact.kyc_verified.isnot(True), CRMContact.is_trashed.isnot(True),
               func.coalesce(CRMContact.status, "") != "blacklisted")
        .order_by(CRMContact.created_at.desc())
    )).scalars().all()
    pend_po = (await db.execute(
        select(CRMPurchaseOrder).options(selectinload(CRMPurchaseOrder.deal), selectinload(CRMPurchaseOrder.contact))
        .where(CRMPurchaseOrder.verified.isnot(True),
               func.coalesce(CRMPurchaseOrder.status, "") != "cancelled")
        .order_by(CRMPurchaseOrder.created_at.desc())
    )).scalars().all()
    pend_so = (await db.execute(
        select(CRMSalesOrder).options(selectinload(CRMSalesOrder.contact), selectinload(CRMSalesOrder.deal))
        .where(CRMSalesOrder.verified.isnot(True), CRMSalesOrder.status != "cancelled")
        .order_by(CRMSalesOrder.created_at.desc())
    )).scalars().all()
    pend_sp = (await db.execute(
        select(SupplierPayment)
        .options(selectinload(SupplierPayment.contact), selectinload(SupplierPayment.po),
                 selectinload(SupplierPayment.sourcing_deal))
        .where(SupplierPayment.verified.isnot(True))
        .order_by(SupplierPayment.created_at.desc())
    )).scalars().all()
    pend_cr = (await db.execute(
        select(CustomerReceipt)
        .options(selectinload(CustomerReceipt.contact), selectinload(CustomerReceipt.dealer),
                 selectinload(CustomerReceipt.sales_order), selectinload(CustomerReceipt.opportunity))
        .where(CustomerReceipt.verified.isnot(True))
        .order_by(CustomerReceipt.created_at.desc())
    )).scalars().all()
    # ── Completed lists (2026-11-09, Pankaj): each card's Completed pill tab —
    #    the latest DONE_LIMIT verified records, newest verification first.
    #    Supplier Payment / Customer Payment's Completed replaces the old
    #    "Recent Supplier Payments" / "Recent Customer Receipts" cards. ──────
    DONE_LIMIT = 50
    done_kyc = (await db.execute(
        select(CRMContact)
        .where(CRMContact.kyc_verified.is_(True), CRMContact.is_trashed.isnot(True))
        .order_by(CRMContact.kyc_verified_at.desc().nullslast(), CRMContact.created_at.desc())
        .limit(DONE_LIMIT)
    )).scalars().all()
    done_po = (await db.execute(
        select(CRMPurchaseOrder).options(selectinload(CRMPurchaseOrder.deal), selectinload(CRMPurchaseOrder.contact)).where(CRMPurchaseOrder.verified.is_(True))
        .order_by(CRMPurchaseOrder.verified_at.desc().nullslast()).limit(DONE_LIMIT)
    )).scalars().all()
    done_so = (await db.execute(
        select(CRMSalesOrder).options(selectinload(CRMSalesOrder.deal), selectinload(CRMSalesOrder.contact)).where(CRMSalesOrder.verified.is_(True))
        .order_by(CRMSalesOrder.verified_at.desc().nullslast()).limit(DONE_LIMIT)
    )).scalars().all()
    done_sp = (await db.execute(
        select(SupplierPayment)
        .options(selectinload(SupplierPayment.contact), selectinload(SupplierPayment.po),
                 selectinload(SupplierPayment.sourcing_deal))
        .where(SupplierPayment.verified.is_(True))
        .order_by(SupplierPayment.verified_at.desc().nullslast()).limit(DONE_LIMIT)
    )).scalars().all()
    done_cr = (await db.execute(
        select(CustomerReceipt)
        .options(selectinload(CustomerReceipt.contact), selectinload(CustomerReceipt.dealer),
                 selectinload(CustomerReceipt.sales_order), selectinload(CustomerReceipt.opportunity))
        .where(CustomerReceipt.verified.is_(True))
        .order_by(CustomerReceipt.verified_at.desc().nullslast()).limit(DONE_LIMIT)
    )).scalars().all()
    pend_qt, done_qt = await _quote_cards(db, DONE_LIMIT)
    from services.deal_payments import payment_requests   # Payment stage requests (2026-11-11)
    from services.order_approvals import order_approval_rows   # PO / SO Level 1-2 (2026-11-11)
    po_appr = await order_approval_rows(db, "po", current_user)
    so_appr = await order_approval_rows(db, "so", current_user)
    sp_req = await payment_requests(db, "supplier")
    cr_req = await payment_requests(db, "customer")
    user_names = {u.username: (u.full_name or u.username)
                  for u in (await db.execute(select(User))).scalars().all()}
    return templates.TemplateResponse("accounts/index.html", {
        "pend_qt": pend_qt, "done_qt": done_qt, "sp_req": sp_req, "cr_req": cr_req, "po_appr": po_appr, "so_appr": so_appr,
        "payment_modes": PAYMENT_MODES,
        "request": request, "current_user": current_user,
        "pend_kyc": pend_kyc, "pend_po": pend_po, "pend_so": pend_so,
        "pend_sp": pend_sp, "pend_cr": pend_cr, "user_names": user_names,
        "done_kyc": done_kyc, "done_po": done_po, "done_so": done_so,
        "done_sp": done_sp, "done_cr": done_cr, "done_limit": DONE_LIMIT,
        "sp_total": float(sp_total or 0),
        "cr_total": float(cr_total or 0),
        "can_edit": current_user.role in FINANCE_ROLES,
        "kyc_docs": {str(c.id): kyc_doc_list(c) for c in pend_kyc},
    })


# KYC documents of an Account, in the order of the Account form (2026-11-10).
KYC_DOCS = [
    ("GSTIN", "gstin", "gstin_doc_path"),
    ("PAN", "pan", "pan_doc_path"),
    ("Bank Cheque", "bank_cheque_number", "bank_cheque_doc_path"),
    ("MSME/UDHYOG Certificate", "msme_number", "msme_doc_path"),
    ("Director 1 Aadhar/PAN", "director1_id_number", "director1_doc_path"),
    ("Director 2 Aadhar/PAN", "director2_id_number", "director2_doc_path"),
]


def _kyc_file(name):
    """Stored KYC upload under uploads/crm/, or None when missing."""
    if not name:
        return None
    path = os.path.join(UPLOADS_DIR, "crm", os.path.basename(name))
    return path if os.path.isfile(path) else None


def kyc_doc_list(c) -> list[dict]:
    """[{label, number, url}] for the Verify KYC pop-up; url is None when no
    file was uploaded (or the file is missing on disk)."""
    out = []
    for label, num_attr, path_attr in KYC_DOCS:
        name = getattr(c, path_attr, None)
        out.append({"label": label, "number": getattr(c, num_attr, None) or "",
                    "url": f"/uploads/crm/{os.path.basename(name)}" if _kyc_file(name) else None})
    return out


@router.get("/kyc-zip/{contact_id}")
async def accounts_kyc_zip(
    contact_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Zip Download in the Verify KYC pop-up (2026-11-10, Pankaj): every
    uploaded KYC document of the Account in one .zip, named by document."""
    import re as _re
    import zipfile
    try:
        c = (await db.execute(select(CRMContact).where(CRMContact.id == UUID(contact_id)))).scalar_one_or_none()
    except ValueError:
        c = None
    if not c:
        return RedirectResponse(url="/accounts?error=Not+found", status_code=302)
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for label, num_attr, path_attr in KYC_DOCS:
            path = _kyc_file(getattr(c, path_attr, None))
            if not path:
                continue
            num = getattr(c, num_attr, None) or ""
            base = _re.sub(r"[^A-Za-z0-9._ -]+", "_", f"{label} {num}".strip())
            zf.write(path, base + os.path.splitext(path)[1].lower())
            n += 1
    if not n:
        return RedirectResponse(url="/accounts?error=No+KYC+documents+uploaded", status_code=302)
    buf.seek(0)
    fname = _re.sub(r"[^A-Za-z0-9._-]+", "_", f"KYC_{c.contact_code or c.company_name}") + ".zip"
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


async def _quote_cards(db: AsyncSession, done_limit: int):
    """Quote Records card on /accounts (2026-11-09, Pankaj): (pending, completed).

    Completed = the quote is Approved, OR a Sales Order was created from it,
    OR its deal's Quote Level 2 approval is approved (that approval is what
    creates the SO on a Customer Deal; a Supplier Deal goes on to a PO).
    Pending = every other open quote (Added / Negotiation). Rejected
    and expired quotes are closed and show in neither tab.
    Each row carries the deal's Quote Level 1 / Level 2 approvers from Stage
    Progress (crm_sales_steps / crm_sourcing_steps)."""
    from models.crm import CRMSalesStep, CRMSourcingStep
    quotes = (await db.execute(
        select(CRMQuote).options(selectinload(CRMQuote.contact))
        .where(func.coalesce(CRMQuote.status, "added").notin_(["rejected", "expired"]))
        .order_by(CRMQuote.created_at.desc())
    )).scalars().all()
    if not quotes:
        return [], []
    opp_ids = {q.opportunity_id for q in quotes if q.opportunity_id}
    deal_ids = {q.sourcing_deal_id for q in quotes if q.sourcing_deal_id}
    steps = {}   # (kind, deal_id) -> {step_key: step}
    for Step, kind, ids in ((CRMSalesStep, "sales", opp_ids), (CRMSourcingStep, "sourcing", deal_ids)):
        if ids:
            for st in (await db.execute(select(Step).where(
                    Step.deal_id.in_(ids), Step.step_key.in_(["quote_l1", "quote_l2"])))).scalars().all():
                steps.setdefault((kind, st.deal_id), {})[st.step_key] = st
    so_by_quote = {qid: (num, at) for qid, num, at in (await db.execute(
        select(CRMSalesOrder.quote_id, CRMSalesOrder.so_number, CRMSalesOrder.created_at)
        .where(CRMSalesOrder.quote_id.in_([q.id for q in quotes]),
               func.coalesce(CRMSalesOrder.status, "open") != "cancelled")
    )).all()}
    pending, done = [], []
    for q in quotes:
        key = ("sales", q.opportunity_id) if q.opportunity_id else ("sourcing", q.sourcing_deal_id)
        st = steps.get(key, {})
        l1, l2 = st.get("quote_l1"), st.get("quote_l2")
        so = so_by_quote.get(q.id)
        l2_ok = bool(l2 and l2.status == "approved" and l1 and l1.status == "approved")   # both levels, any order (2026-11-11)
        row = {
            "quote": q,
            "deal_kind": "Customer" if q.opportunity_id else ("Supplier" if q.sourcing_deal_id else ""),
            "approvers": [(lbl, s) for lbl, s in (("L1", l1), ("L2", l2)) if s and s.assigned_to],
            "so_number": so[0] if so else None,
        }
        if q.status == "approved" or so or l2_ok:
            # Approved by: whichever level approved last (levels approve in any order)
            last = max((x for x in (l1, l2) if x and x.status == "approved" and x.acted_at),
                       key=lambda x: x.acted_at, default=None)
            row["approved_by"] = last.acted_by if last else None
            row["completed_at"] = (last.acted_at if (l2_ok and last) else None) or (so[1] if so else None) or q.updated_at
            done.append(row)
        else:
            pending.append(row)
    from datetime import datetime as _dt
    done.sort(key=lambda r: r["completed_at"] or r["quote"].created_at or _dt.min, reverse=True)
    return pending, done[:done_limit]


async def _user_names(db) -> dict:
    return {u.username: (u.full_name or u.username) for u in (await db.execute(select(User))).scalars().all()}


from services.deal_payments import payment_requests   # Payment stage requests (2026-11-11)


def _supplier_payments_query(contact_id: str = ""):
    q = (select(SupplierPayment)
         .options(selectinload(SupplierPayment.contact), selectinload(SupplierPayment.lot))
         .order_by(SupplierPayment.payment_date.desc()))
    if contact_id:
        q = q.where(SupplierPayment.contact_id == contact_id)
    return q


@router.get("/supplier-payments", response_class=HTMLResponse)
async def supplier_payments(
    request: Request,
    contact_id: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Pricing View (2026-11-03): this page is all amounts — not shown without it.
    from utils.price_export import price_view_block as _pvb
    _pvblk = _pvb(current_user, "finance_supplier", "/accounts")
    if _pvblk is not None:
        return _pvblk
    if current_user.role not in FINANCE_ROLES:
        return RedirectResponse(url="/?error=Access+denied", status_code=302)
    total = (await db.execute(
        select(func.coalesce(func.sum(SupplierPayment.amount), 0))
        .select_from(_supplier_payments_query(contact_id).subquery())
    )).scalar() or 0
    suppliers = (await db.execute(
        select(CRMContact)
        .where(CRMContact.contact_type.in_(["supplier", "both"]))
        .where(CRMContact.status == "active")
        .order_by(CRMContact.company_name)
    )).scalars().all()
    lots = (await db.execute(
        select(Lot).order_by(Lot.created_at.desc()).limit(50)
    )).scalars().all()
    sourcing_deals = (await db.execute(
        select(CRMSourcingDeal).where(CRMSourcingDeal.stage != "lost")
        .order_by(CRMSourcingDeal.created_at.desc()).limit(200)
    )).scalars().all()
    # Account Name dropdown (2026-11-11): suppliers + every deal's account, linked to Supplier Deal ID
    _ids = {s.id for s in suppliers} 
    _extra = {d.contact_id for d in sourcing_deals if d.contact_id and d.contact_id not in _ids}
    if _extra:
        suppliers = list(suppliers) + list((await db.execute(select(CRMContact).where(CRMContact.id.in_(_extra)))).scalars().all())
        suppliers.sort(key=lambda c: (c.company_name or "").lower())
    purchase_orders = (await db.execute(
        select(CRMPurchaseOrder).options(selectinload(CRMPurchaseOrder.contact))
        .order_by(CRMPurchaseOrder.created_at.desc()).limit(100)
    )).scalars().all()
    return templates.TemplateResponse("accounts/supplier_payments.html", {
        "request": request, "current_user": current_user,
        "suppliers": suppliers, "lots": lots,
        "sourcing_deals": sourcing_deals, "purchase_orders": purchase_orders,
        "sp_req": await payment_requests(db, "supplier"), "user_names": await _user_names(db),   # 2026-11-11
        "total": float(total), "sel_contact": contact_id,
        "payment_modes": PAYMENT_MODES,
        "can_edit": current_user.role in FINANCE_ROLES,
    })


@router.get("/supplier-payments/data")
async def supplier_payments_data(
    draw: int = Query(1), start: int = Query(0), length: int = Query(25),
    contact_id: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """DataTables server-side feed for the Payment History table — this list
    only grows, so it was rendered in full on every page load."""
    from utils.price_export import can_view_prices as _cvp   # Pricing View (2026-11-03)
    _pv_sup = _cvp(current_user, "finance_supplier")
    if current_user.role not in FINANCE_ROLES:
        return JSONResponse({"error": "Access denied"}, status_code=403)
    q = _supplier_payments_query(contact_id)
    total = (await db.execute(
        select(func.count()).select_from(_supplier_payments_query().subquery())
    )).scalar() or 0
    filtered = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar() or 0
    payments = (await db.execute(q.offset(start).limit(length))).scalars().all()

    data = []
    for p in payments:
        doc_links = []
        if p.invoice_path:
            doc_links.append(f'<a href="/uploads/accounts/{esc(p.invoice_path)}" target="_blank" '
                             f'class="btn btn-sm btn-outline-secondary py-0 px-1" title="Invoice">'
                             f'<i class="bi bi-file-earmark-pdf"></i></a>')
        if p.payment_photo_path:
            doc_links.append(f'<a href="/uploads/accounts/{esc(p.payment_photo_path)}" target="_blank" '
                             f'class="btn btn-sm btn-outline-secondary py-0 px-1" title="Payment Photo">'
                             f'<i class="bi bi-image"></i></a>')
        data.append([
            p.payment_date.strftime("%d-%m-%Y") if p.payment_date else "—",
            esc(p.contact.company_name) if p.contact else "—",
            esc(p.lot.lot_number) if p.lot else "—",
            f'<span class="badge bg-secondary">{esc(p.payment_mode or "—")}</span>',
            f'<span class="font-monospace small">{esc(p.reference_no or "—")}</span>',
            ('<span class="badge bg-warning text-dark">Advance</span>' if p.is_advance else "—"),
            (f'<span class="fw-semibold text-danger">₹{p.amount:,.0f}</span>' if _pv_sup else '—'),
            (f'<div class="text-nowrap">{"".join(doc_links)}</div>' if doc_links else "—"),
            esc(p.created_by or "—"),
            ('<span class="badge bg-success" title="Verified by ' + esc(p.verified_by or "—") + (" on " + p.verified_at.strftime("%d-%m-%Y") if p.verified_at else "") + '">Verified</span>'
             if p.verified else '<span class="badge bg-warning text-dark">Pending</span>'),
        ])

    return JSONResponse({
        "draw": draw, "recordsTotal": total, "recordsFiltered": filtered, "data": data,
    })


@router.post("/supplier-payments/new")
async def create_supplier_payment(
    request: Request,
    _csrf: None = Depends(verify_csrf),
    contact_id: str = Form(default=""),
    lot_id: str = Form(default=""),
    po_id: str = Form(default=""),
    sourcing_deal_id: str = Form(default=""),
    payment_date: str = Form(...),
    amount: str = Form(...),
    payment_mode: str = Form(default=""),
    reference_no: str = Form(default=""),
    is_advance: str = Form(default="off"),
    notes: str = Form(default=""),
    invoice: UploadFile = File(default=None),
    payment_photo: UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in FINANCE_ROLES:
        return RedirectResponse(url="/accounts/supplier-payments?error=Permission+denied", status_code=302)
    try:
        amt = float(amount)
        if amt <= 0:
            raise ValueError("amount must be positive")
    except (ValueError, TypeError):
        return RedirectResponse(url="/accounts/supplier-payments?error=Invalid+amount", status_code=302)
    if payment_mode not in PAYMENT_MODES:
        return RedirectResponse(url="/accounts/supplier-payments?error=Choose+a+Payment+Mode", status_code=302)
    if not reference_no.strip():
        return RedirectResponse(url="/accounts/supplier-payments?error=Enter+the+Payment+UTR+%2F+Invoice+Number", status_code=302)
    # Supplier Deal ID and Account Name are linked (2026-11-11): the deal's account wins
    if sourcing_deal_id:
        try:
            _d = (await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == UUID(sourcing_deal_id)))).scalar_one_or_none()
        except ValueError:
            _d = None
        if _d and _d.contact_id:
            contact_id = str(_d.contact_id)
    pay = SupplierPayment(
        contact_id=contact_id or None,
        lot_id=lot_id or None,
        po_id=po_id or None,
        sourcing_deal_id=sourcing_deal_id or None,
        payment_date=_parse_date(payment_date),
        amount=amt,
        payment_mode=payment_mode or None,
        reference_no=reference_no or None,
        is_advance=(is_advance == "on"),
        notes=notes or None,
        invoice_path=await _save_upload(invoice),
        payment_photo_path=await _save_upload(payment_photo),
        created_by=current_user.username,
    )
    db.add(pay)
    await audit(
        db, user=current_user, action="SUPPLIER_PAYMENT_RECORDED",
        table_name="supplier_payments", record_id=str(pay.id),
        new_value={
            "amount": amt,
            "payment_mode": payment_mode or None,
            "contact_id": contact_id or None,
            "lot_id": lot_id or None,
            "reference_no": reference_no or None,
            "is_advance": (is_advance == "on"),
        },
        request=request,
    )
    await db.commit()
    return RedirectResponse(url="/accounts/supplier-payments?success=Payment+recorded", status_code=302)


def _customer_receipts_query(dealer_id: str = ""):
    q = (select(CustomerReceipt)
         .options(selectinload(CustomerReceipt.contact), selectinload(CustomerReceipt.dealer))
         .order_by(CustomerReceipt.receipt_date.desc()))
    if dealer_id:
        q = q.where(CustomerReceipt.dealer_id == dealer_id)
    return q


@router.get("/customer-receipts", response_class=HTMLResponse)
async def customer_receipts(
    request: Request,
    dealer_id: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Pricing View (2026-11-03): this page is all amounts — not shown without it.
    from utils.price_export import price_view_block as _pvb
    _pvblk = _pvb(current_user, "finance_customer", "/accounts")
    if _pvblk is not None:
        return _pvblk
    if current_user.role not in FINANCE_ROLES:
        return RedirectResponse(url="/?error=Access+denied", status_code=302)
    total = (await db.execute(
        select(func.coalesce(func.sum(CustomerReceipt.amount), 0))
        .select_from(_customer_receipts_query(dealer_id).subquery())
    )).scalar() or 0
    dealers = (await db.execute(
        select(Dealer).where(Dealer.status == "active").order_by(Dealer.business_name)
    )).scalars().all()
    opportunities = (await db.execute(
        select(CRMSalesOpportunity).where(CRMSalesOpportunity.stage != "lost")
        .order_by(CRMSalesOpportunity.created_at.desc()).limit(200)
    )).scalars().all()
    # Account Name dropdown (2026-11-11): buyers + every deal's account, linked to Customer Deal ID
    buyers = (await db.execute(
        select(CRMContact).where(CRMContact.contact_type.in_(["buyer", "both"]), CRMContact.is_trashed.isnot(True),
                                 CRMContact.status == "active").order_by(CRMContact.company_name))).scalars().all()
    _ids = {c.id for c in buyers}
    _extra = {o.contact_id for o in opportunities if o.contact_id and o.contact_id not in _ids}
    if _extra:
        buyers = list(buyers) + list((await db.execute(select(CRMContact).where(CRMContact.id.in_(_extra)))).scalars().all())
        buyers.sort(key=lambda c: (c.company_name or "").lower())
    quotes = (await db.execute(
        select(CRMQuote).options(selectinload(CRMQuote.contact))
        .order_by(CRMQuote.created_at.desc()).limit(100)
    )).scalars().all()
    return templates.TemplateResponse("accounts/customer_receipts.html", {
        "request": request, "current_user": current_user,
        "dealers": dealers,
        "opportunities": opportunities, "quotes": quotes, "buyers": buyers,
        "cr_req": await payment_requests(db, "customer"), "user_names": await _user_names(db),   # 2026-11-11
        "total": float(total), "sel_dealer": dealer_id,
        "payment_modes": PAYMENT_MODES,
        "can_edit": current_user.role in FINANCE_ROLES,
    })


@router.get("/customer-receipts/data")
async def customer_receipts_data(
    draw: int = Query(1), start: int = Query(0), length: int = Query(25),
    dealer_id: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """DataTables server-side feed for the Receipt History table."""
    from utils.price_export import can_view_prices as _cvp   # Pricing View (2026-11-03)
    _pv_cus = _cvp(current_user, "finance_customer")
    if current_user.role not in FINANCE_ROLES:
        return JSONResponse({"error": "Access denied"}, status_code=403)
    q = _customer_receipts_query(dealer_id)
    total = (await db.execute(
        select(func.count()).select_from(_customer_receipts_query().subquery())
    )).scalar() or 0
    filtered = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar() or 0
    receipts = (await db.execute(q.offset(start).limit(length))).scalars().all()

    data = []
    for r in receipts:
        doc_links = []
        if r.invoice_path:
            doc_links.append(f'<a href="/uploads/accounts/{esc(r.invoice_path)}" target="_blank" '
                             f'class="btn btn-sm btn-outline-secondary py-0 px-1" title="Invoice">'
                             f'<i class="bi bi-file-earmark-pdf"></i></a>')
        if r.payment_photo_path:
            doc_links.append(f'<a href="/uploads/accounts/{esc(r.payment_photo_path)}" target="_blank" '
                             f'class="btn btn-sm btn-outline-secondary py-0 px-1" title="Payment Photo">'
                             f'<i class="bi bi-image"></i></a>')
        data.append([
            r.receipt_date.strftime("%d-%m-%Y") if r.receipt_date else "—",
            esc(r.contact.company_name) if r.contact else "—",
            esc(r.dealer.business_name) if r.dealer else "—",
            f'<span class="badge bg-secondary">{esc(r.payment_mode or "—")}</span>',
            f'<span class="font-monospace small">{esc(r.reference_no or "—")}</span>',
            (f'<span class="fw-semibold text-success">₹{r.amount:,.0f}</span>' if _pv_cus else '—'),
            (f'<div class="text-nowrap">{"".join(doc_links)}</div>' if doc_links else "—"),
            esc(r.created_by or "—"),
            ('<span class="badge bg-success" title="Verified by ' + esc(r.verified_by or "—") + (" on " + r.verified_at.strftime("%d-%m-%Y") if r.verified_at else "") + '">Verified</span>'
             if r.verified else '<span class="badge bg-warning text-dark">Pending</span>'),
        ])

    return JSONResponse({
        "draw": draw, "recordsTotal": total, "recordsFiltered": filtered, "data": data,
    })


@router.post("/customer-receipts/new")
async def create_customer_receipt(
    request: Request,
    _csrf: None = Depends(verify_csrf),
    contact_id: str = Form(default=""),
    dealer_id: str = Form(default=""),
    sale_id: str = Form(default=""),
    dealer_order_id: str = Form(default=""),
    opportunity_id: str = Form(default=""),
    receipt_date: str = Form(...),
    amount: str = Form(...),
    payment_mode: str = Form(default=""),
    reference_no: str = Form(default=""),
    notes: str = Form(default=""),
    invoice: UploadFile = File(default=None),
    payment_photo: UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in FINANCE_ROLES:
        return RedirectResponse(url="/accounts/customer-receipts?error=Permission+denied", status_code=302)
    try:
        amt = float(amount)
        if amt <= 0:
            raise ValueError("amount must be positive")
    except (ValueError, TypeError):
        return RedirectResponse(url="/accounts/customer-receipts?error=Invalid+amount", status_code=302)
    if payment_mode not in PAYMENT_MODES:
        return RedirectResponse(url="/accounts/customer-receipts?error=Choose+a+Payment+Mode", status_code=302)
    if not reference_no.strip():
        return RedirectResponse(url="/accounts/customer-receipts?error=Enter+the+Payment+UTR+%2F+Invoice+Number", status_code=302)
    # Customer Deal ID and Account Name are linked (2026-11-11): the deal's account wins
    if opportunity_id:
        try:
            _o = (await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == UUID(opportunity_id)))).scalar_one_or_none()
        except ValueError:
            _o = None
        if _o and _o.contact_id:
            contact_id = str(_o.contact_id)
    rec = CustomerReceipt(
        contact_id=contact_id or None,
        dealer_id=dealer_id or None,
        sale_id=sale_id or None,
        dealer_order_id=dealer_order_id or None,
        opportunity_id=opportunity_id or None,
        receipt_date=_parse_date(receipt_date),
        amount=amt,
        payment_mode=payment_mode or None,
        reference_no=reference_no or None,
        notes=notes or None,
        invoice_path=await _save_upload(invoice),
        payment_photo_path=await _save_upload(payment_photo),
        created_by=current_user.username,
    )
    # Link the Buyer Deal's open Sales Order, if any (2026-11-01)
    if opportunity_id:
        try:
            rec.so_id = (await db.execute(
                select(CRMSalesOrder.id)
                .where(CRMSalesOrder.deal_id == UUID(str(opportunity_id)), CRMSalesOrder.status != "cancelled")
                .order_by(CRMSalesOrder.created_at.desc()).limit(1)
            )).scalar_one_or_none()
        except ValueError:
            pass
    db.add(rec)
    # Auto-reconcile against dealer order if linked
    from decimal import Decimal
    if dealer_order_id:
        ord_result = await db.execute(
            select(DealerOrder).where(DealerOrder.id == dealer_order_id)
        )
        linked_order = ord_result.scalar_one_or_none()
        if linked_order:
            apply_amt = min(Decimal(str(amt)), linked_order.due_amount or Decimal("0"))
            if apply_amt > Decimal("0"):
                linked_order.paid_amount = (linked_order.paid_amount or Decimal("0")) + apply_amt
                linked_order.due_amount = max(Decimal("0"), (linked_order.due_amount or Decimal("0")) - apply_amt)
                if linked_order.due_amount == Decimal("0") and linked_order.status not in ("cancelled", "paid"):
                    linked_order.status = "paid"
    await audit(db, user=current_user, action="RECEIPT_RECORDED",
                table_name="customer_receipts", record_id=str(rec.id),
                new_value={"amount": amt, "payment_mode": payment_mode or None,
                           "dealer_id": dealer_id or None,
                           "reference_no": reference_no or None},
                request=request)
    await db.commit()
    return RedirectResponse(url="/accounts/customer-receipts?success=Receipt+recorded", status_code=302)


# ── CSV Template Download ─────────────────────────────────────────────────────

@router.get("/customer-receipts/bulk-upload-template")
async def download_receipt_csv_template(
    current_user: User = Depends(get_current_user),
):
    """Return a sample CSV template for bulk receipt upload."""
    if current_user.role not in FINANCE_ROLES:
        return RedirectResponse(url="/?error=Access+denied", status_code=302)
    sample_rows = [
        ["amount", "payment_date", "payment_mode", "reference_no", "notes", "customer_name"],
        ["15000", "2026-05-15", "upi", "UTR123456789", "May payment", "ABC Corp"],
        ["8500", "2026-05-15", "neft", "UTR987654321", "", "XYZ Traders"],
    ]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(sample_rows)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=receipt_bulk_upload_template.csv"},
    )


# ── Bulk Upload Form ──────────────────────────────────────────────────────────

@router.get("/customer-receipts/bulk-upload", response_class=HTMLResponse)
async def receipt_bulk_upload_form(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    # Pricing View (2026-11-03): this page is all amounts — not shown without it.
    from utils.price_export import price_view_block as _pvb
    _pvblk = _pvb(current_user, "finance_customer", "/accounts")
    if _pvblk is not None:
        return _pvblk
    if current_user.role not in FINANCE_ROLES:
        return RedirectResponse(url="/?error=Access+denied", status_code=302)
    return templates.TemplateResponse("accounts/receipts_bulk_upload.html", {
        "request": request, "current_user": current_user,
        "results": None,
    })


@router.post("/customer-receipts/bulk-upload", response_class=HTMLResponse)
async def receipt_bulk_upload(
    request: Request,
    _csrf: None = Depends(verify_csrf),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in FINANCE_ROLES:
        return RedirectResponse(url="/?error=Access+denied", status_code=302)

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # handle BOM from Excel
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    results = []
    added = 0

    for i, row in enumerate(reader, start=2):  # row 1 is header
        row_num = i
        amount_raw = (row.get("amount") or "").strip()
        payment_date_raw = (row.get("payment_date") or "").strip()
        payment_mode = (row.get("payment_mode") or "").strip().lower()
        reference_no = (row.get("reference_no") or "").strip() or None
        notes = (row.get("notes") or "").strip() or None
        customer_name = (row.get("customer_name") or "").strip() or None

        # Validate amount
        try:
            amt = Decimal(amount_raw)
            if amt <= 0:
                raise ValueError("must be positive")
        except (InvalidOperation, ValueError) as e:
            results.append({
                "row": row_num, "amount": amount_raw, "date": payment_date_raw,
                "mode": payment_mode, "ref": reference_no or "",
                "customer": customer_name or "",
                "status": "error", "message": f"Invalid amount: {e}",
            })
            continue

        # Validate date
        pdate = _parse_date(payment_date_raw)

        # Validate payment mode
        if payment_mode and payment_mode not in PAYMENT_MODES:
            results.append({
                "row": row_num, "amount": str(amt), "date": str(pdate),
                "mode": payment_mode, "ref": reference_no or "",
                "customer": customer_name or "",
                "status": "error", "message": f"Invalid payment_mode '{payment_mode}'",
            })
            continue

        rec = CustomerReceipt(
            receipt_date=pdate,
            amount=amt,
            payment_mode=payment_mode or None,
            reference_no=reference_no,
            notes=notes,
            created_by=current_user.username,
            # contact_id left null for bulk uploads
        )
        db.add(rec)
        results.append({
            "row": row_num, "amount": str(amt), "date": str(pdate),
            "mode": payment_mode or "—", "ref": reference_no or "—",
            "customer": customer_name or "—",
            "status": "added", "message": "OK",
        })
        added += 1

    if added:
        await db.commit()

    return templates.TemplateResponse("accounts/receipts_bulk_upload.html", {
        "request": request, "current_user": current_user,
        "results": results, "added": added,
    })


# ── PO / Quote payment completion + invoice upload (Supplier/Customer
#    Payments pages' "List of PO Generated" / "List of Quotes Sent") ────────

@router.post("/po/{po_id}/complete-payment")
async def complete_po_payment(
    po_id: str,
    request: Request,
    _csrf: None = Depends(verify_csrf),
    utr_number: str = Form(...),
    payment_date: str = Form(""),
    payment_amount: str = Form(""),
    payment_mode: str = Form(""),
    payment_snapshot: UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in FINANCE_ROLES:
        return RedirectResponse(url="/accounts/supplier-payments?error=Permission+denied", status_code=302)
    po = (await db.execute(select(CRMPurchaseOrder).where(CRMPurchaseOrder.id == po_id))).scalar_one_or_none()
    if not po:
        raise HTTPException(404, "PO not found")
    pd, amt, mode, err = _payment_fields(payment_date, payment_amount, payment_mode)
    if err or not utr_number.strip():
        return RedirectResponse(url="/accounts/supplier-payments?error=" + quote_plus(err or "Enter the Payment UTR / Invoice Number"),
                                status_code=302)
    po.utr_number = utr_number.strip()
    snap_name = await _save_upload(payment_snapshot)
    if snap_name:
        po.payment_snapshot_path = snap_name
    po.payment_status = "paid"
    # The payment itself is saved as a Supplier Payment for this PO (date,
    # amount, mode, UTR) - listed under Payments and verified on Accounts.
    sp = SupplierPayment(po_id=po.id, contact_id=po.contact_id, sourcing_deal_id=po.deal_id,
                         payment_date=pd, amount=amt, payment_mode=mode, reference_no=po.utr_number,
                         notes=f"Complete Payment - {po.po_number}", payment_photo_path=snap_name or None,
                         created_by=current_user.username)
    db.add(sp)
    await db.flush()
    await audit(db, user=current_user, action="PO_PAYMENT_COMPLETED",
                table_name="crm_purchase_orders", record_id=str(po.id),
                new_value={"utr_number": po.utr_number, "payment_date": pd.isoformat(), "amount": str(amt),
                           "payment_mode": mode, "supplier_payment_id": str(sp.id)}, request=request)
    await db.commit()
    return RedirectResponse(url="/accounts/supplier-payments?success=Payment+completed", status_code=302)


@router.post("/po/{po_id}/upload-invoice")
async def upload_po_invoice(
    po_id: str,
    request: Request,
    _csrf: None = Depends(verify_csrf),
    invoice: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in FINANCE_ROLES:
        return RedirectResponse(url="/accounts/supplier-payments?error=Permission+denied", status_code=302)
    po = (await db.execute(select(CRMPurchaseOrder).where(CRMPurchaseOrder.id == po_id))).scalar_one_or_none()
    if not po:
        raise HTTPException(404, "PO not found")
    name = await _save_upload(invoice)
    if not name:
        return RedirectResponse(url="/accounts/supplier-payments?error=No+file+selected", status_code=302)
    po.invoice_path = name
    await audit(db, user=current_user, action="CRM_PO_INVOICE_UPLOADED", table_name="crm_purchase_orders",
                record_id=str(po.id), new_value={"file": name}, request=request)
    await db.commit()
    return RedirectResponse(url="/accounts/supplier-payments?success=Invoice+uploaded", status_code=302)


@router.post("/quote/{quote_id}/complete-payment")
async def complete_quote_payment(
    quote_id: str,
    request: Request,
    _csrf: None = Depends(verify_csrf),
    utr_number: str = Form(...),
    payment_date: str = Form(""),
    payment_amount: str = Form(""),
    payment_mode: str = Form(""),
    payment_snapshot: UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in FINANCE_ROLES:
        return RedirectResponse(url="/accounts/customer-receipts?error=Permission+denied", status_code=302)
    quote = (await db.execute(select(CRMQuote).where(CRMQuote.id == quote_id))).scalar_one_or_none()
    if not quote:
        raise HTTPException(404, "Quote not found")
    pd, amt, mode, err = _payment_fields(payment_date, payment_amount, payment_mode)
    if err or not utr_number.strip():
        return RedirectResponse(url="/accounts/customer-receipts?error=" + quote_plus(err or "Enter the Payment UTR / Invoice Number"),
                                status_code=302)
    quote.utr_number = utr_number.strip()
    snap_name = await _save_upload(payment_snapshot)
    if snap_name:
        quote.payment_snapshot_path = snap_name
    quote.payment_status = "paid"
    # Saved as a Customer Receipt (date, amount, mode, UTR), linked to the
    # quote's Sales Order when there is one, and to its deal.
    so_id = (await db.execute(select(CRMSalesOrder.id).where(CRMSalesOrder.quote_id == quote.id)
                              .order_by(CRMSalesOrder.created_at.desc()).limit(1))).scalar_one_or_none()
    cr = CustomerReceipt(contact_id=quote.contact_id, opportunity_id=quote.opportunity_id, so_id=so_id,
                         receipt_date=pd, amount=amt, payment_mode=mode, reference_no=quote.utr_number,
                         notes=f"Complete Payment - {quote.quote_number}", payment_photo_path=snap_name or None,
                         created_by=current_user.username)
    db.add(cr)
    await db.flush()
    await audit(db, user=current_user, action="QUOTE_PAYMENT_COMPLETED",
                table_name="crm_quotes", record_id=str(quote.id),
                new_value={"utr_number": quote.utr_number, "payment_date": pd.isoformat(), "amount": str(amt),
                           "payment_mode": mode, "customer_receipt_id": str(cr.id)}, request=request)
    await db.commit()
    return RedirectResponse(url="/accounts/customer-receipts?success=Payment+completed", status_code=302)


@router.post("/quote/{quote_id}/upload-invoice")
async def upload_quote_invoice(
    quote_id: str,
    request: Request,
    _csrf: None = Depends(verify_csrf),
    invoice: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in FINANCE_ROLES:
        return RedirectResponse(url="/accounts/customer-receipts?error=Permission+denied", status_code=302)
    quote = (await db.execute(select(CRMQuote).where(CRMQuote.id == quote_id))).scalar_one_or_none()
    if not quote:
        raise HTTPException(404, "Quote not found")
    name = await _save_upload(invoice)
    if not name:
        return RedirectResponse(url="/accounts/customer-receipts?error=No+file+selected", status_code=302)
    quote.invoice_path = name
    await audit(db, user=current_user, action="CRM_QUOTE_INVOICE_UPLOADED", table_name="crm_quotes",
                record_id=str(quote.id), new_value={"file": name}, request=request)
    await db.commit()
    return RedirectResponse(url="/accounts/customer-receipts?success=Invoice+uploaded", status_code=302)


# ── DEAL PAYMENT REQUESTS (2026-11-11, Pankaj) ───────────────────────────────
# The Payment stage's Level 1 approval of a Supplier / Customer Deal is
# verified here: Verify = the Complete Payment form; Reject = with a reason.
# services/deal_payments.py does the work; both pages + the dashboard post here.

def _deal_pay_back(return_to: str, kind: str, msg: str, ok: bool = True):
    dest = return_to if (return_to or "").startswith("/accounts") and "//" not in return_to else (
        "/accounts/supplier-payments" if kind == "supplier" else "/accounts/customer-receipts")
    base, _, frag = dest.partition("#")
    sep = "&" if "?" in base else "?"
    return RedirectResponse(url=f"{base}{sep}{'success' if ok else 'error'}={quote_plus(msg)}"
                                + (f"#{frag}" if frag else ""), status_code=302)


async def _deal_for_payment(db, kind, deal_id):
    from services.deal_payments import _models
    m = _models(kind)
    try:
        return (await db.execute(select(m["Deal"]).where(m["Deal"].id == UUID(deal_id)))).scalar_one_or_none()
    except ValueError:
        return None


@router.post("/deal-payment/{kind}/{deal_id}/verify")
async def verify_deal_payment(
    kind: str, deal_id: str, request: Request,
    _csrf: None = Depends(verify_csrf),
    utr_number: str = Form(default=""),
    payment_date: str = Form(""),
    payment_amount: str = Form(""),
    payment_mode: str = Form(""),
    payment_snapshot: UploadFile = File(default=None),
    return_to: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from services.deal_payments import KINDS, verify_payment
    if kind not in KINDS:
        return RedirectResponse(url="/accounts?error=Unknown+payment", status_code=302)
    if current_user.role not in FINANCE_ROLES:
        return _deal_pay_back(return_to, kind, "Permission denied", ok=False)
    deal = await _deal_for_payment(db, kind, deal_id)
    if not deal:
        return _deal_pay_back(return_to, kind, "Deal not found", ok=False)
    pd, amt, mode, err = _payment_fields(payment_date, payment_amount, payment_mode)
    if err or not utr_number.strip():
        return _deal_pay_back(return_to, kind, err or "Enter the Payment UTR / Invoice Number", ok=False)
    snap = await _save_upload(payment_snapshot)
    _pay, moved = await verify_payment(db, kind, deal, current_user, pd=pd, amt=amt, mode=mode,
                                       utr=utr_number.strip()[:100], snapshot=snap, request=request)
    await db.commit()
    num = getattr(deal, "deal_number", None) or getattr(deal, "opp_number", "")
    return _deal_pay_back(return_to, kind, f"{num}: payment verified" + (" — Payment stage Done" if moved else ""))


@router.post("/deal-payment/{kind}/{deal_id}/reject")
async def reject_deal_payment(
    kind: str, deal_id: str, request: Request,
    _csrf: None = Depends(verify_csrf),
    reason: str = Form(default=""),
    return_to: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from services.deal_payments import KINDS, reject_payment
    if kind not in KINDS:
        return RedirectResponse(url="/accounts?error=Unknown+payment", status_code=302)
    if current_user.role not in FINANCE_ROLES:
        return _deal_pay_back(return_to, kind, "Permission denied", ok=False)
    if not reason.strip():
        return _deal_pay_back(return_to, kind, "Enter a reason for the rejection", ok=False)
    deal = await _deal_for_payment(db, kind, deal_id)
    if not deal or not await reject_payment(db, kind, deal, current_user, reason.strip()[:500], request=request):
        return _deal_pay_back(return_to, kind, "Payment request not found", ok=False)
    await db.commit()
    num = getattr(deal, "deal_number", None) or getattr(deal, "opp_number", "")
    return _deal_pay_back(return_to, kind, f"{num}: payment rejected")


# ── VERIFY (Accounts Dashboard pending lists, 2026-11-01) ─────────────────────

_VERIFY_KINDS = {
    "po":       (CRMPurchaseOrder, "Purchase Order", "ACCOUNTS_PO_VERIFIED",       "crm_purchase_orders"),
    "so":       (CRMSalesOrder,    "Sales Order",    "ACCOUNTS_SO_VERIFIED",       "crm_sales_orders"),
    "supplier": (SupplierPayment,  "Supplier payment", "ACCOUNTS_SUPPLIER_PAYMENT_VERIFIED", "supplier_payments"),
    "customer": (CustomerReceipt,  "Customer payment", "ACCOUNTS_CUSTOMER_PAYMENT_VERIFIED", "customer_receipts"),
}


@router.post("/verify/{kind}/{record_id}", dependencies=[Depends(verify_csrf)])
async def accounts_verify(
    request: Request, kind: str, record_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if kind not in _VERIFY_KINDS:
        return RedirectResponse(url="/accounts?error=Unknown+item", status_code=302)
    model, label, action, table = _VERIFY_KINDS[kind]
    try:
        rec = (await db.execute(select(model).where(model.id == UUID(record_id)))).scalar_one_or_none()
    except ValueError:
        rec = None
    if not rec:
        return RedirectResponse(url="/accounts?error=Not+found", status_code=302)
    if kind == "po":
        # Verify = Verify + Issue + Purchase Order stage Done (2026-11-11, Pankaj)
        from services.po_complete import complete_po
        await complete_po(db, rec, current_user, request, via="verified by Accounts")
        await db.commit()
        return RedirectResponse(url="/accounts?success=Purchase+Order+verified+and+issued", status_code=302)
    if not rec.verified:
        rec.verified = True
        rec.verified_by = current_user.username
        rec.verified_at = app_now()
        await audit(db, user=current_user, action=action, table_name=table,
                    record_id=str(rec.id), new_value={"verified": True}, request=request)
        await db.commit()
    return RedirectResponse(url=f"/accounts?success={label.replace(' ', '+')}+verified", status_code=302)


@router.post("/verify-kyc/{contact_id}", dependencies=[Depends(verify_csrf)])
async def accounts_verify_kyc(
    request: Request, contact_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        c = (await db.execute(select(CRMContact).where(CRMContact.id == UUID(contact_id)))).scalar_one_or_none()
    except ValueError:
        c = None
    if not c:
        return RedirectResponse(url="/accounts?error=Not+found", status_code=302)
    if not c.kyc_verified:
        c.kyc_verified = True
        c.kyc_verified_by = current_user.username
        c.kyc_verified_at = app_now()
        c.apply_account_status()
        await audit(db, user=current_user, action="ACCOUNT_KYC_VERIFIED",
                    table_name="crm_contacts", record_id=str(c.id), request=request)
        await db.commit()
    return RedirectResponse(url="/accounts?success=KYC+verified", status_code=302)
