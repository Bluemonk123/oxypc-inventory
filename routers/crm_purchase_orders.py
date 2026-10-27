# routers/crm_purchase_orders.py
"""CRM Purchase Orders — formal PO to supplier, linked from sourcing deal."""
import json
from datetime import datetime, date
from utils.timezone import app_now
from uuid import UUID
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from templates_config import templates
from database import get_db
from auth.dependencies import get_current_user, require_roles, verify_csrf
from models.user import User, UserRole
from models.crm import (
    CRMPurchaseOrder, CRMPOLineItem, CRMContact, CRMSourcingDeal,
    GRADES,
)
from routers.settings import get_company_settings

router = APIRouter(prefix="/crm/purchase-orders", tags=["crm-purchase-orders"], dependencies=[Depends(verify_csrf)])

ADMIN_ROLES = (UserRole.admin, UserRole.sales_manager, UserRole.inventory_manager)
# Roles that may view POs (broader, includes sales + telecaller)
VIEW_ROLES  = (UserRole.admin, UserRole.sales_manager, UserRole.inventory_manager,
               UserRole.sales, UserRole.telecaller)


async def _next_po_number(db: AsyncSession) -> str:
    # Derive from the highest existing suffix for this year, NOT count(*):
    # count-based numbering collides whenever a PO has ever been deleted (gap),
    # producing a duplicate po_number and an IntegrityError on insert.
    prefix = f"PO-{app_now().year}-"
    result = await db.execute(
        select(func.max(CRMPurchaseOrder.po_number))
        .where(CRMPurchaseOrder.po_number.like(prefix + "%"))
    )
    mx = result.scalar()
    n = 1
    if mx:
        try:
            n = int(str(mx).rsplit("-", 1)[-1]) + 1
        except (ValueError, IndexError):
            n = 1
    return f"{prefix}{n:04d}"


@router.get("", response_class=HTMLResponse)
async def list_pos(
    request: Request,
    status: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in VIEW_ROLES:
        return RedirectResponse(url="/?error=Access+denied", status_code=302)
    q = (
        select(CRMPurchaseOrder)
        .options(
            selectinload(CRMPurchaseOrder.contact),
            selectinload(CRMPurchaseOrder.deal),
        )
        .order_by(CRMPurchaseOrder.created_at.desc())
    )
    if status:
        q = q.where(CRMPurchaseOrder.status == status)
    result = await db.execute(q)
    pos = result.scalars().all()
    return templates.TemplateResponse("crm/purchase_orders/list.html", {
        "request": request, "current_user": current_user,
        "pos": pos, "status": status,
        "can_edit": current_user.role in ADMIN_ROLES,
    })


@router.get("/new", response_class=HTMLResponse)
async def new_po_form(
    request: Request,
    deal_id: str = Query(default=None),
    contact_id: str = Query(default=None),   # prefill supplier directly (e.g. from a contact profile)
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ADMIN_ROLES:
        return RedirectResponse(url="/crm/purchase-orders?error=Permission+denied", status_code=302)
    deal = None
    if deal_id:
        r = await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == deal_id))
        deal = r.scalar_one_or_none()
    contacts_r = await db.execute(
        select(CRMContact).where(CRMContact.status == "active").order_by(CRMContact.company_name)
    )
    contacts = contacts_r.scalars().all()

    # Resolve the supplier to pre-select: an explicit ?contact_id= wins, else the
    # deal's contact. Prefill GSTIN from that contact and default advance to 0.
    pre_cid = contact_id or (str(deal.contact_id) if deal and deal.contact_id else None)
    prefill_contact = None
    if pre_cid:
        pcr = await db.execute(select(CRMContact).where(CRMContact.id == pre_cid))
        prefill_contact = pcr.scalar_one_or_none()

    po_number = await _next_po_number(db)
    return templates.TemplateResponse("crm/purchase_orders/form.html", {
        "request": request, "current_user": current_user,
        "po": None, "deal": deal, "contacts": contacts,
        "po_number": po_number, "grades": GRADES,
        "today": date.today().isoformat(),
        "prefill_contact_id": str(prefill_contact.id) if prefill_contact else "",
        "prefill_gstin": (prefill_contact.gstin or "") if prefill_contact else "",
        "prefill_advance": "0" if pre_cid else "",
    })


@router.post("/new")
async def create_po(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ADMIN_ROLES:
        return RedirectResponse(url="/crm/purchase-orders?error=Permission+denied", status_code=302)
    # Capture scalar now: a rollback in the retry loop below expires all ORM
    # objects, so a later current_user.username would trigger a sync lazy-load
    # (MissingGreenlet) inside the async handler.
    username = current_user.username
    form = await request.form()
    def _n(v): return float(v) if v and str(v).strip() else None
    def _i(v): return int(v) if v and str(v).strip() else 1
    def _d(v):
        if not v or not str(v).strip():
            return date.today()
        try:
            return date.fromisoformat(str(v).strip())
        except ValueError:
            return date.today()

    for attempt in range(3):
        try:
            po = CRMPurchaseOrder(
                po_number=await _next_po_number(db),
                deal_id=form.get("deal_id") or None,
                contact_id=form.get("contact_id") or None,
                po_date=_d(form.get("po_date")),
                expected_delivery_date=_d(form.get("expected_delivery_date")),
                supplier_gstin=form.get("supplier_gstin") or None,
                delivery_address=form.get("delivery_address") or None,
                delivery_city=form.get("delivery_city") or None,
                delivery_state=form.get("delivery_state") or None,
                delivery_pincode=form.get("delivery_pincode") or None,
                payment_terms=form.get("payment_terms") or None,
                advance_amount=_n(form.get("advance_amount")),
                offer_total=_n(form.get("offer_total")),
                status="draft",
                notes=form.get("notes") or None,
                created_by=username,
            )
            db.add(po)
            await db.flush()

            # Line items now come from the "Add PO Line Items" modal as JSON.
            try:
                items = json.loads(form.get("line_items_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                items = []
            items = [it for it in items if isinstance(it, dict) and str(it.get("item_name", "")).strip()]
            if not items:
                await db.rollback()
                return templates.TemplateResponse("crm/purchase_orders/form.html", {
                    "request": request, "current_user": current_user,
                    "po": None, "deal": None, "contacts": [],
                    "po_number": await _next_po_number(db), "grades": GRADES,
                    "today": date.today().isoformat(),
                    "error": "At least one line item is required.",
                }, status_code=400)
            total = 0.0
            for i, it in enumerate(items):
                qty = _i(it.get("qty"))
                up = _n(it.get("unit_price")) or 0.0
                tp = round(qty * up, 2)
                total += tp
                db.add(CRMPOLineItem(
                    po_id=po.id, item_name=str(it.get("item_name")).strip(),
                    description=(str(it.get("description")).strip() if it.get("description") else ""),  # NOT NULL
                    po_category=(str(it.get("po_category")).strip() or None) if it.get("po_category") else None,
                    lot_number=(str(it.get("lot_number")).strip() or None) if it.get("lot_number") else None,
                    quantity=qty, unit_price=up, total_price=tp, sort_order=i,
                ))
            po.total_amount = round(total, 2)
            await db.commit()
            return RedirectResponse(url=f"/crm/purchase-orders/{po.id}?success=PO+created", status_code=302)
        except IntegrityError:
            await db.rollback()
            if attempt == 2:
                return RedirectResponse(url="/crm/purchase-orders?error=Failed+to+generate+PO+number,+try+again", status_code=302)


@router.get("/{po_id}", response_class=HTMLResponse)
async def view_po(
    request: Request,
    po_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    r = await db.execute(
        select(CRMPurchaseOrder)
        .options(
            selectinload(CRMPurchaseOrder.line_items),
            selectinload(CRMPurchaseOrder.contact),
            selectinload(CRMPurchaseOrder.deal),
        )
        .where(CRMPurchaseOrder.id == po_id)
    )
    po = r.scalar_one_or_none()
    if not po:
        return RedirectResponse(url="/crm/purchase-orders?error=Not+found", status_code=302)
    return templates.TemplateResponse("crm/purchase_orders/detail.html", {
        "request": request, "current_user": current_user,
        "po": po, "can_edit": current_user.role in ADMIN_ROLES,
    })


@router.post("/{po_id}/issue")
async def issue_po(
    request: Request,
    po_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ADMIN_ROLES:
        return RedirectResponse(url=f"/crm/purchase-orders/{po_id}?error=Permission+denied", status_code=302)
    r = await db.execute(select(CRMPurchaseOrder).where(CRMPurchaseOrder.id == po_id))
    po = r.scalar_one_or_none()
    if po and po.status == "draft":
        po.status = "issued"
        po.issued_by = current_user.username
        po.issued_at = app_now()
        if po.deal_id:
            dr = await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == po.deal_id))
            deal = dr.scalar_one_or_none()
            if deal and deal.stage in ("lead", "contacted", "inspection", "quote", "negotiation"):
                deal.stage = "purchase_order"
        await db.commit()
        return RedirectResponse(url=f"/crm/purchase-orders/{po_id}?success=PO+issued", status_code=302)
    status_val = po.status if po else "unknown"
    return RedirectResponse(url=f"/crm/purchase-orders/{po_id}?error=Cannot+issue+a+{status_val}+PO", status_code=302)


@router.get("/{po_id}/print", response_class=HTMLResponse)
async def print_po(
    request: Request,
    po_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    r = await db.execute(
        select(CRMPurchaseOrder)
        .options(
            selectinload(CRMPurchaseOrder.line_items),
            selectinload(CRMPurchaseOrder.contact),
            selectinload(CRMPurchaseOrder.deal),
        )
        .where(CRMPurchaseOrder.id == po_id)
    )
    po = r.scalar_one_or_none()
    if not po:
        return RedirectResponse(url="/crm/purchase-orders?error=Not+found", status_code=302)
    company = await get_company_settings(db)
    return templates.TemplateResponse("crm/purchase_orders/print.html", {
        "request": request, "current_user": current_user, "po": po, "company": company,
    })
