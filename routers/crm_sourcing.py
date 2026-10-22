"""CRM Sourcing Deals router — purchase pipeline from lead to lot creation."""
import os
import uuid as _uuid
from datetime import datetime
from utils.timezone import app_now
from config import UPLOADS_DIR
import io
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, Response
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from templates_config import templates
from database import get_db
from auth.dependencies import get_current_user, verify_csrf, require_module_perm
from models.user import User, UserRole
from services.audit_engine import audit
from services.po_pdf import build_po_pdf
from models.crm import (
    CRMContact, CRMSourcingDeal, CRMActivity, CRMPurchaseOrder, CRMPOLineItem,
    CRMSourcingDealLineItem, SOURCE_TYPES, MATERIAL_TYPES, SOURCING_STAGES, PRIORITIES,
)
from routers.settings import get_company_settings
from routers.terms_conditions import get_active_terms
from routers.crm_purchase_orders import _next_po_number
from models.lot import Lot
from models.device import Device, DeviceStage

router = APIRouter(prefix="/crm/sourcing", tags=["crm-sourcing"], dependencies=[Depends(verify_csrf)])

PURCHASE_ROLES = (UserRole.admin, UserRole.inventory_manager, UserRole.sales_manager)


async def _next_deal_number(db: AsyncSession) -> str:
    result = await db.execute(select(func.count(CRMSourcingDeal.id)))
    n = (result.scalar() or 0) + 1
    year = app_now().year
    return f"SD-{year}-{n:04d}"


# ── LIST ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def list_sourcing_deals(
    request: Request,
    q: str = Query(default=""),
    stage: str = Query(default=""),
    source_type: str = Query(default=""),
    priority: str = Query(default=""),
    assigned: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(CRMSourcingDeal)
    if q:
        like = f"%{q}%"
        query = query.where(CRMSourcingDeal.title.ilike(like))
    if stage:
        query = query.where(CRMSourcingDeal.stage == stage)
    if source_type:
        query = query.where(CRMSourcingDeal.source_type == source_type)
    if priority:
        query = query.where(CRMSourcingDeal.priority == priority)
    if assigned:
        query = query.where(CRMSourcingDeal.assigned_to == assigned)
    elif current_user.role in (UserRole.sales,):
        query = query.where(CRMSourcingDeal.assigned_to == current_user.username)

    result = await db.execute(query.order_by(CRMSourcingDeal.created_at.desc()))
    deals = result.scalars().all()

    # pipeline value (open deals only)
    open_deals = [d for d in deals if d.stage not in ("won", "lost")]
    pipeline_value = sum(
        float(d.our_offer_total or d.asking_price_total or 0) for d in open_deals
    )

    # overdue follow-ups
    now = app_now()
    overdue_r = await db.execute(
        select(func.count(CRMActivity.id)).where(
            CRMActivity.deal_type == "sourcing",
            CRMActivity.followup_done == False,
            CRMActivity.next_followup <= now,
        )
    )
    overdue_count = overdue_r.scalar() or 0

    # fetch contacts for display
    contact_ids = list({d.contact_id for d in deals if d.contact_id})
    contacts_map = {}
    if contact_ids:
        cr = await db.execute(select(CRMContact).where(CRMContact.id.in_(contact_ids)))
        for c in cr.scalars().all():
            contacts_map[str(c.id)] = c

    # Qty / Value per deal — aggregated from the deal's PO Line Items (not the
    # static asking/offer deal fields, which the Financials card no longer drives).
    deal_ids = [d.id for d in deals]
    line_totals_map = {}
    if deal_ids:
        lt_r = await db.execute(
            select(
                CRMSourcingDealLineItem.deal_id,
                func.coalesce(func.sum(CRMSourcingDealLineItem.quantity), 0),
                func.coalesce(func.sum(CRMSourcingDealLineItem.total_price), 0),
            )
            .where(CRMSourcingDealLineItem.deal_id.in_(deal_ids))
            .group_by(CRMSourcingDealLineItem.deal_id)
        )
        for did, qty_sum, value_sum in lt_r.all():
            line_totals_map[str(did)] = {"qty": int(qty_sum or 0), "value": float(value_sum or 0)}

    # Latest generated PO per deal, for the Download column.
    latest_po_map = {}
    if deal_ids:
        po_r = await db.execute(
            select(CRMPurchaseOrder)
            .where(CRMPurchaseOrder.deal_id.in_(deal_ids))
            .order_by(CRMPurchaseOrder.created_at.desc())
        )
        for po in po_r.scalars().all():
            latest_po_map.setdefault(str(po.deal_id), po)

    return templates.TemplateResponse("crm/sourcing/list.html", {
        "request": request, "current_user": current_user,
        "deals": deals, "contacts_map": contacts_map,
        "pipeline_value": pipeline_value, "overdue_count": overdue_count,
        "line_totals_map": line_totals_map, "latest_po_map": latest_po_map,
        "q": q, "stage": stage, "source_type": source_type,
        "priority": priority, "assigned": assigned,
        "sourcing_stages": SOURCING_STAGES,
        "source_types": SOURCE_TYPES,
        "priorities": PRIORITIES,
        "stage_labels": dict(SOURCING_STAGES),
    })


# ── NEW ───────────────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_deal_form(
    request: Request,
    contact_id: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    contacts_r = await db.execute(
        select(CRMContact)
        .where(CRMContact.status == "active", CRMContact.contact_type.in_(["supplier", "both"]))
        .order_by(CRMContact.company_name)
    )
    contacts = contacts_r.scalars().all()
    preselect = contact_id or ""
    return templates.TemplateResponse("crm/sourcing/form.html", {
        "request": request, "current_user": current_user,
        "deal": None, "contacts": contacts, "preselect": preselect,
        "source_types": SOURCE_TYPES,
        "material_types": MATERIAL_TYPES,
        "priorities": PRIORITIES,
    })


@router.post("/new")
async def create_deal(
    request: Request,
    title:               str = Form(...),
    contact_id:          str = Form(default=None),
    source_type:         str = Form(default=None),
    device_type:         str = Form(default=None),
    est_quantity:        str = Form(default=None),
    material_type:       str = Form(default=None),
    asking_price_unit:   str = Form(default=None),
    asking_price_total:  str = Form(default=None),
    our_offer_unit:      str = Form(default=None),
    payment_advance_pct: str = Form(default="0"),
    payment_terms:       str = Form(default=None),
    priority:            str = Form(default="medium"),
    assigned_to:         str = Form(default=None),
    notes:               str = Form(default=None),
    product_records:     UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _perm: User = Depends(require_module_perm("crm_sourcing", "add")),
):
    def _n(v): return float(v) if v and v.strip() else None
    def _i(v): return int(v) if v and v.strip() else None

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

    deal_number = await _next_deal_number(db)
    aq_unit  = _n(asking_price_unit)
    aq_total = _n(asking_price_total)
    qty      = _i(est_quantity)
    # auto-calc total if not provided
    if aq_unit and qty and not aq_total:
        aq_total = round(aq_unit * qty, 2)
    our_unit = _n(our_offer_unit)
    our_total = round(our_unit * qty, 2) if our_unit and qty else None

    deal = CRMSourcingDeal(
        deal_number=deal_number,
        contact_id=contact_id or None,
        title=title,
        source_type=source_type or None,
        device_type=device_type or None,
        est_quantity=qty,
        material_type=material_type or None,
        asking_price_unit=aq_unit,
        asking_price_total=aq_total,
        our_offer_unit=our_unit,
        our_offer_total=our_total,
        payment_advance_pct=_i(payment_advance_pct) or 0,
        payment_terms=payment_terms or None,
        priority=priority,
        assigned_to=assigned_to or current_user.username,
        notes=notes or None,
        product_records_file=saved_filename,
        created_by=current_user.username,
        stage="lead",
    )
    db.add(deal)
    await db.flush()   # get deal.id
    await audit(db, action="CRM_DEAL_CREATED", user=current_user,
                table_name="crm_sourcing_deals", record_id=str(deal.id),
                new_value={"deal_number": deal_number, "title": title,
                           "device_type": device_type, "est_quantity": qty,
                           "asking_price_unit": aq_unit, "assigned_to": assigned_to},
                request=request)
    await db.commit()
    return RedirectResponse(url=f"/crm/sourcing/{deal.id}?success=Deal+created", status_code=302)


# ── DETAIL ────────────────────────────────────────────────────────────────────

@router.get("/{deal_id}", response_class=HTMLResponse)
async def deal_detail(
    request: Request,
    deal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        return RedirectResponse(url="/crm/sourcing?error=Deal+not+found", status_code=302)

    contact = None
    if deal.contact_id:
        cr = await db.execute(select(CRMContact).where(CRMContact.id == deal.contact_id))
        contact = cr.scalar_one_or_none()

    acts_r = await db.execute(
        select(CRMActivity)
        .where(CRMActivity.deal_id == deal.id, CRMActivity.deal_type == "sourcing")
        .order_by(CRMActivity.activity_date.desc())
    )
    activities = acts_r.scalars().all()

    # next follow-up
    next_fu = next(
        (a for a in activities if not a.followup_done and a.next_followup), None
    )

    lot = None
    lot_registered = 0
    lot_sold = 0
    if deal.linked_lot_id:
        lr = await db.execute(select(Lot).where(Lot.id == deal.linked_lot_id))
        lot = lr.scalar_one_or_none()
        if lot:
            reg_r = await db.execute(
                select(func.count(Device.id))
                .where(Device.lot_id == lot.id, Device.is_active == True, Device.is_trashed == False)
            )
            lot_registered = int(reg_r.scalar() or 0)

            sold_r = await db.execute(
                select(func.count(Device.id))
                .where(Device.lot_id == lot.id, Device.current_stage == DeviceStage.sold,
                       Device.is_trashed == False)
            )
            lot_sold = int(sold_r.scalar() or 0)

    stage_list = list(enumerate(SOURCING_STAGES))
    current_idx = next((i for i, (v, _) in stage_list if v == deal.stage), 0)

    # Purchase Deals — POs raised from this sourcing deal (eager-load .contact
    # for the supplier column in the shared partial).
    pos_r = await db.execute(
        select(CRMPurchaseOrder)
        .options(selectinload(CRMPurchaseOrder.contact))
        .where(CRMPurchaseOrder.deal_id == deal.id)
        .order_by(CRMPurchaseOrder.created_at.desc())
    )
    purchase_orders = pos_r.scalars().all()

    # Draft PO line items on this deal (the "PO Line Items" section)
    dpi_r = await db.execute(
        select(CRMSourcingDealLineItem)
        .where(CRMSourcingDealLineItem.deal_id == deal.id)
        .order_by(CRMSourcingDealLineItem.sort_order)
    )
    deal_po_items = dpi_r.scalars().all()
    deal_po_items_total = sum(float(li.total_price or 0) for li in deal_po_items)
    # Effective offer total: use each row's Offer Price where set, otherwise
    # fall back to that row's Total Price - a row with no offer price yet
    # shouldn't drag the bottom total down to 0 for that item.
    deal_po_items_offer_total = sum(
        float(li.offer_price) if li.offer_price else float(li.total_price or 0)
        for li in deal_po_items
    )

    from models.company import Company
    companies = (await db.execute(
        select(Company).where(Company.is_active == True).order_by(Company.company_name)
    )).scalars().all()
    from routers.crm_quotes import active_terms_by_type
    terms_by_type = await active_terms_by_type(db)

    return templates.TemplateResponse("crm/sourcing/detail.html", {
        "request": request, "current_user": current_user,
        "deal": deal, "contact": contact, "activities": activities,
        "purchase_orders": purchase_orders,
        "deal_po_items": deal_po_items,
        "deal_po_items_total": deal_po_items_total,
        "deal_po_items_offer_total": deal_po_items_offer_total,
        "companies": companies, "terms_by_type": terms_by_type,
        "next_fu": next_fu, "lot": lot,
        "lot_registered": lot_registered,
        "lot_sold": lot_sold,
        "sourcing_stages": SOURCING_STAGES,
        "stage_list": stage_list, "current_idx": current_idx,
        "stage_labels": dict(SOURCING_STAGES),
        "source_map": dict(SOURCE_TYPES),
        "material_map": dict(MATERIAL_TYPES),
        "now": app_now(),
    })


# ── MOVE STAGE ────────────────────────────────────────────────────────────────

@router.post("/{deal_id}/stage")
async def move_stage(
    request: Request,
    deal_id: str,
    new_stage: str = Form(...),
    win_loss_reason: str = Form(default=None),
    final_price_unit: str = Form(default=None),
    final_price_total: str = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        return RedirectResponse(url="/crm/sourcing?error=Deal+not+found", status_code=302)

    deal.stage = new_stage
    if win_loss_reason:
        deal.win_loss_reason = win_loss_reason
    if final_price_unit and final_price_unit.strip():
        try:
            deal.final_price_unit = float(final_price_unit)
        except (ValueError, TypeError):
            pass
    if final_price_total and final_price_total.strip():
        try:
            deal.final_price_total = float(final_price_total)
        except (ValueError, TypeError):
            pass

    # log automatic activity
    activity = CRMActivity(
        contact_id=deal.contact_id,
        deal_id=deal.id,
        deal_type="sourcing",
        activity_type="note",
        summary=f"Stage moved to: {dict(SOURCING_STAGES).get(new_stage, new_stage)}",
        performed_by=current_user.username,
        outcome="done",
    )
    db.add(activity)
    await db.commit()
    return RedirectResponse(url=f"/crm/sourcing/{deal_id}?success=Stage+updated", status_code=302)


# ── EDIT ─────────────────────────────────────────────────────────────────────

@router.get("/{deal_id}/edit", response_class=HTMLResponse)
async def edit_deal_form(
    request: Request,
    deal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        return RedirectResponse(url="/crm/sourcing?error=Not+found", status_code=302)
    contacts_r = await db.execute(
        select(CRMContact)
        .where(CRMContact.status == "active", CRMContact.contact_type.in_(["supplier", "both"]))
        .order_by(CRMContact.company_name)
    )
    contacts = contacts_r.scalars().all()
    return templates.TemplateResponse("crm/sourcing/form.html", {
        "request": request, "current_user": current_user,
        "deal": deal, "contacts": contacts, "preselect": str(deal.contact_id) if deal.contact_id else "",
        "source_types": SOURCE_TYPES,
        "material_types": MATERIAL_TYPES,
        "priorities": PRIORITIES,
    })


@router.post("/{deal_id}/edit")
async def update_deal(
    request: Request,
    deal_id: str,
    title:               str = Form(...),
    contact_id:          str = Form(default=None),
    source_type:         str = Form(default=None),
    device_type:         str = Form(default=None),
    est_quantity:        str = Form(default=None),
    material_type:       str = Form(default=None),
    asking_price_unit:   str = Form(default=None),
    asking_price_total:  str = Form(default=None),
    our_offer_unit:      str = Form(default=None),
    our_offer_total:     str = Form(default=None),
    payment_advance_pct: str = Form(default="0"),
    payment_terms:       str = Form(default=None),
    priority:            str = Form(default="medium"),
    assigned_to:         str = Form(default=None),
    notes:               str = Form(default=None),
    product_records:     UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        return RedirectResponse(url="/crm/sourcing?error=Not+found", status_code=302)

    def _n(v): return float(v) if v and v.strip() else None
    def _i(v): return int(v) if v and v.strip() else None

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
            deal.product_records_file = safe_name

    deal.title = title
    deal.contact_id = contact_id or None
    deal.source_type = source_type or None
    deal.device_type = device_type or None
    deal.est_quantity = _i(est_quantity)
    deal.material_type = material_type or None
    deal.asking_price_unit = _n(asking_price_unit)
    deal.asking_price_total = _n(asking_price_total)
    deal.our_offer_unit = _n(our_offer_unit)
    deal.our_offer_total = _n(our_offer_total)
    deal.payment_advance_pct = _i(payment_advance_pct) or 0
    deal.payment_terms = payment_terms or None
    deal.priority = priority
    deal.assigned_to = assigned_to
    deal.notes = notes or None
    await db.commit()
    return RedirectResponse(url=f"/crm/sourcing/{deal_id}?success=Deal+updated", status_code=302)


# ── LINK LOT ─────────────────────────────────────────────────────────────────

@router.post("/{deal_id}/upload")
async def upload_documents(
    request: Request,
    deal_id: str,
    purchase_invoice: UploadFile = File(default=None),
    purchase_order: UploadFile = File(default=None),
    eway_bill: UploadFile = File(default=None),
    quote_received: UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload Purchase Invoice / Purchase Order / E-way Bill / Quote Received for
    a sourcing deal. Reuses the UUID-based safe-filename pattern used for
    product_records above."""
    result = await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        return RedirectResponse(url="/crm/sourcing?error=Deal+not+found", status_code=302)

    async def _save(upload: UploadFile):
        if not upload or not upload.filename:
            return None
        ext = os.path.splitext(upload.filename)[1].lower()
        safe_name = f"{_uuid.uuid4().hex}{ext}"
        uploads_dir = os.path.join(UPLOADS_DIR, "crm")
        os.makedirs(uploads_dir, exist_ok=True)
        dest = os.path.join(uploads_dir, safe_name)
        content = await upload.read()
        with open(dest, "wb") as f:
            f.write(content)
        return safe_name

    saved_any = False
    inv_name = await _save(purchase_invoice)
    if inv_name:
        deal.purchase_invoice_path = inv_name
        saved_any = True
    po_name = await _save(purchase_order)
    if po_name:
        deal.purchase_order_path = po_name
        saved_any = True
    eway_name = await _save(eway_bill)
    if eway_name:
        deal.eway_bill_path = eway_name
        saved_any = True
    quote_name = await _save(quote_received)
    if quote_name:
        deal.quote_received_path = quote_name
        saved_any = True

    if saved_any:
        await audit(db, action="CRM_DEAL_DOCS_UPLOADED", user=current_user,
                    table_name="crm_sourcing_deals", record_id=str(deal.id),
                    new_value={"purchase_invoice": inv_name, "purchase_order": po_name,
                               "eway_bill": eway_name, "quote_received": quote_name},
                    request=request)
        await db.commit()
        return RedirectResponse(url=f"/crm/sourcing/{deal_id}?success=Documents+uploaded", status_code=302)
    return RedirectResponse(url=f"/crm/sourcing/{deal_id}?error=No+file+selected", status_code=302)


@router.post("/{deal_id}/link-lot")
async def link_lot(
    request: Request,
    deal_id: str,
    lot_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        return RedirectResponse(url="/crm/sourcing?error=Deal+not+found", status_code=302)
    deal.linked_lot_id = lot_id
    deal.stage = "received"   # Stock Received — operator closes to Won after IQC is done
    activity = CRMActivity(
        contact_id=deal.contact_id, deal_id=deal.id, deal_type="sourcing",
        activity_type="note",
        summary="Lot linked — stage auto-advanced to Stock Received.",
        performed_by=current_user.username, outcome="done",
    )
    db.add(activity)
    await db.commit()
    return RedirectResponse(url=f"/crm/sourcing/{deal_id}?success=Linked+to+Lot+%26+marked+Received", status_code=302)


# ── PO LINE ITEMS (on the deal) + GENERATE PO ─────────────────────────────────

def _num(v):
    try:
        return float(v) if v is not None and str(v).strip() else 0.0
    except (ValueError, TypeError):
        return 0.0


@router.post("/{deal_id}/po-items/add")
async def add_deal_po_item(
    request: Request,
    deal_id: str,
    item_name: str = Form(...),
    description: str = Form(default=None),
    po_category: str = Form(default=None),
    lot_number: str = Form(default=None),
    qty: str = Form(default="1"),
    unit_price: str = Form(default="0"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    deal = (await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == deal_id))).scalar_one_or_none()
    if not deal:
        return RedirectResponse(url="/crm/sourcing?error=Deal+not+found", status_code=302)
    if not item_name.strip():
        return RedirectResponse(url=f"/crm/sourcing/{deal_id}?error=Item+name+required", status_code=302)
    try:
        q = int(float(qty)) if qty and str(qty).strip() else 1
    except (ValueError, TypeError):
        q = 1
    up = _num(unit_price)
    n = (await db.execute(
        select(func.count(CRMSourcingDealLineItem.id)).where(CRMSourcingDealLineItem.deal_id == deal.id)
    )).scalar() or 0
    db.add(CRMSourcingDealLineItem(
        deal_id=deal.id, item_name=item_name.strip(),
        description=(description or None), po_category=(po_category or None),
        lot_number=(lot_number or None), quantity=q, unit_price=up,
        total_price=round(q * up, 2), sort_order=n,
    ))
    await db.commit()
    return RedirectResponse(url=f"/crm/sourcing/{deal_id}?success=PO+item+added", status_code=302)


@router.post("/{deal_id}/po-items/{item_id}/delete")
async def delete_deal_po_item(
    deal_id: str,
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (await db.execute(
        select(CRMSourcingDealLineItem).where(
            CRMSourcingDealLineItem.id == item_id,
            CRMSourcingDealLineItem.deal_id == deal_id,
        )
    )).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return RedirectResponse(url=f"/crm/sourcing/{deal_id}?success=PO+item+removed", status_code=302)


@router.post("/{deal_id}/po-items/{item_id}/offer")
async def set_deal_po_item_offer(
    deal_id: str,
    item_id: str,
    offer_price: str = Form(default="0"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (await db.execute(
        select(CRMSourcingDealLineItem).where(
            CRMSourcingDealLineItem.id == item_id,
            CRMSourcingDealLineItem.deal_id == deal_id,
        )
    )).scalar_one_or_none()
    if row:
        row.offer_price = _num(offer_price)
        await db.commit()
    return RedirectResponse(url=f"/crm/sourcing/{deal_id}?success=Offer+price+saved", status_code=302)


@router.post("/{deal_id}/generate-po")
async def generate_po_from_deal(
    request: Request,
    deal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    username = current_user.username   # capture before any rollback (MissingGreenlet guard)
    form = await request.form()
    term_ids = {
        "payment":    (form.get("payment_term_id")    or "").strip(),
        "delivery":   (form.get("delivery_term_id")   or "").strip(),
        "disclaimer": (form.get("disclaimer_term_id") or "").strip(),
    }
    sections = {
        "account":    bool(form.get("include_account")),
        "company":    bool(form.get("include_company")),
        "items":      bool(form.get("include_items")),
        # A terms block is included only when a specific policy was picked
        # for it — same rule as the Quote generate modal.
        "payment":    bool(term_ids["payment"]),
        "delivery":   bool(term_ids["delivery"]),
        "conditions": bool(term_ids["disclaimer"]),
    }

    deal = (await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == deal_id))).scalar_one_or_none()
    if not deal:
        return RedirectResponse(url="/crm/sourcing?error=Deal+not+found", status_code=302)

    items = (await db.execute(
        select(CRMSourcingDealLineItem)
        .where(CRMSourcingDealLineItem.deal_id == deal.id)
        .order_by(CRMSourcingDealLineItem.sort_order)
    )).scalars().all()

    contact = None
    if deal.contact_id:
        contact = (await db.execute(select(CRMContact).where(CRMContact.id == deal.contact_id))).scalar_one_or_none()

    # Create the PO record (gap-safe numbering; retry on the rare collision).
    total = round(sum(float(li.total_price or 0) for li in items), 2)
    po = None
    for _attempt in range(3):
        po = CRMPurchaseOrder(
            po_number=await _next_po_number(db),
            deal_id=deal.id, contact_id=deal.contact_id,
            supplier_gstin=(contact.gstin if contact else None),
            total_amount=total, status="draft", created_by=username,
            # Our offer (sum of the line items' Offer Price) — shown as Offer
            # Price in Purchase Deals (2026-10-22). None when no offer is set.
            offer_total=(round(sum(float(li.offer_price) for li in items if li.offer_price is not None), 2)
                         if any(li.offer_price is not None for li in items) else None),
        )
        db.add(po)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            po = None
            continue
        for i, li in enumerate(items):
            db.add(CRMPOLineItem(
                po_id=po.id, item_name=li.item_name,
                description=li.description or "",   # column is NOT NULL
                po_category=li.po_category, lot_number=li.lot_number,
                device_type=li.device_type, grade=li.grade,
                quantity=li.quantity, unit_price=li.unit_price,
                total_price=li.total_price, sort_order=i,
            ))
        await db.commit()
        break
    if po is None:
        return RedirectResponse(url=f"/crm/sourcing/{deal_id}?error=Could+not+generate+PO+number", status_code=302)

    # Gather company + the specific term chosen per section (falls back to
    # every active policy of that type if somehow no id was picked but the
    # section is enabled — mirrors the Quote generate flow).
    from models.terms import TermsCondition

    async def _chosen_term(kind, enabled):
        if not enabled:
            return []
        chosen = term_ids.get(kind)
        if chosen:
            row = (await db.execute(
                select(TermsCondition).where(TermsCondition.id == chosen)
            )).scalar_one_or_none()
            return [row] if row else []
        return await get_active_terms(db, kind)

    company = await get_company_settings(db, (form.get("company_id") or "").strip())
    payment_terms  = await _chosen_term("payment",    sections["payment"])
    delivery_terms = await _chosen_term("delivery",   sections["delivery"])
    disclaimers    = await _chosen_term("disclaimer", sections["conditions"])

    pdf_bytes = build_po_pdf(
        po_number=po.po_number, po_date=po.po_date.strftime("%d %b %Y") if po.po_date else "",
        company=company, contact=contact, line_items=items,
        payment_terms=payment_terms, delivery_terms=delivery_terms, disclaimers=disclaimers,
        sections=sections, total_amount=total, offer_total=None,
    )
    filename = f"{po.po_number}.pdf"
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{deal_id}/download-po")
async def download_deal_po(
    deal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-download the most recently generated PO for this deal (Download
    column on the Buyer Deals list) by rebuilding the PDF from the PO's own
    stored line items — the deal's draft line items may have changed since."""
    deal = (await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == deal_id))).scalar_one_or_none()
    if not deal:
        return RedirectResponse(url="/crm/sourcing?error=Deal+not+found", status_code=302)

    po = (await db.execute(
        select(CRMPurchaseOrder)
        .options(selectinload(CRMPurchaseOrder.line_items), selectinload(CRMPurchaseOrder.contact))
        .where(CRMPurchaseOrder.deal_id == deal_id)
        .order_by(CRMPurchaseOrder.created_at.desc())
    )).scalars().first()
    if not po:
        return RedirectResponse(url=f"/crm/sourcing/{deal_id}?error=No+PO+generated+yet", status_code=302)

    company = await get_company_settings(db)
    payment_terms  = await get_active_terms(db, "payment")
    delivery_terms = await get_active_terms(db, "delivery")
    disclaimers    = await get_active_terms(db, "disclaimer")

    pdf_bytes = build_po_pdf(
        po_number=po.po_number, po_date=po.po_date.strftime("%d %b %Y") if po.po_date else "",
        company=company, contact=po.contact, line_items=po.line_items,
        payment_terms=payment_terms, delivery_terms=delivery_terms, disclaimers=disclaimers,
        sections={"account": True, "company": True, "items": True, "payment": True, "delivery": True, "conditions": True},
        total_amount=float(po.total_amount or 0), offer_total=(float(po.offer_total) if po.offer_total else None),
    )
    filename = f"{po.po_number}.pdf"
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
