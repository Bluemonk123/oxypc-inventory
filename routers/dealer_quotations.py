"""Dealer Quotations — a quotation document shared with a dealer, created from
the "Quotation" button on Dealer Management / Dealer Detail. Snapshots dealer
+ company details at creation time (so it stays accurate as a historical
document) and totals are always computed server-side from the submitted line
items, never trusted from the client."""
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models.user import User
from models.dealers import Dealer
from models.dealer_quotation import DealerQuotation, DealerQuotationItem
from auth.dependencies import require_roles, verify_csrf, require_module_perm
from services.audit_engine import audit
from routers.company_settings import get_company_settings
from routers.dealers import SALES_ROLES

router = APIRouter(tags=["dealer_quotations"], dependencies=[Depends(verify_csrf)])
require_sales = require_roles(*SALES_ROLES)


def _to_decimal(value: str, default: str = "0") -> Decimal:
    try:
        return Decimal(value.strip()) if (value or "").strip() else Decimal(default)
    except InvalidOperation:
        return Decimal(default)


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value.strip()) if (value or "").strip() else default
    except ValueError:
        return default


@router.post("/dealers/{dealer_id}/quotations")
async def create_dealer_quotation(
    request: Request,
    dealer_id: str,
    payment_terms: str = Form(default=""),
    additional_notes: str = Form(default=""),
    product_name: list[str] = Form(default=[]),
    model_name: list[str] = Form(default=[]),
    po_category: list[str] = Form(default=[]),
    quantity: list[str] = Form(default=[]),
    unit_price: list[str] = Form(default=[]),
    return_to: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
    _perm: User = Depends(require_module_perm("dealers", "add")),
):
    # 2026-10-27: the Quotations page's Add Quote opens this same modal and
    # sends return_to=/quotations so the user lands back there. Only that one
    # path is accepted (never an arbitrary URL).
    back = "/quotations" if return_to == "/quotations" else f"/dealers/{dealer_id}"
    dealer = (await db.execute(select(Dealer).where(Dealer.id == dealer_id))).scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url=("/quotations" if back == "/quotations" else "/dealers") + "?error=Dealer+not+found", status_code=302)

    items = []
    total_quantity = 0
    total_price = Decimal("0")
    for i in range(len(product_name)):
        name = (product_name[i] or "").strip()
        if not name:
            continue
        qty = _to_int(quantity[i] if i < len(quantity) else "", 0)
        price = _to_decimal(unit_price[i] if i < len(unit_price) else "", "0")
        line_total = (Decimal(qty) * price).quantize(Decimal("0.01"))
        items.append({
            "product_name": name,
            "po_category": (po_category[i] if i < len(po_category) else "").strip() or None,
            "model_name": (model_name[i] if i < len(model_name) else "").strip() or None,
            "quantity": qty,
            "unit_price": price,
            "total_price": line_total,
            "sort_order": i,
        })
        total_quantity += qty
        total_price += line_total

    if not items:
        return RedirectResponse(url=f"{back}?error=Add+at+least+one+line+item", status_code=302)

    # QT-TELE-<MMYYYY><NNNN> (2026-11-10, utils/quote_numbers.py)
    from utils.quote_numbers import next_quote_number
    from utils.timezone import app_now
    quote_number = await next_quote_number(db, DealerQuotation.quote_number, "TELE", app_now().date())

    company = await get_company_settings(db)

    quotation = DealerQuotation(
        quote_number=quote_number,
        dealer_id=dealer.id,
        customer_name=dealer.business_name,
        customer_type=dealer.dealer_type,
        dealer_phone=dealer.phone,
        dealer_email=dealer.email,
        dealer_address=dealer.address,
        dealer_gstin=dealer.gstin,
        company_name=company["company_name"] or None,
        company_address=company["company_address"] or None,
        company_gstin=company["company_gstin"] or None,
        company_phone=company["company_phone"] or None,
        company_email=company["company_email"] or None,
        payment_terms=payment_terms or None,
        additional_notes=additional_notes or None,
        total_quantity=total_quantity,
        total_price=total_price,
        status="added",
        created_by=current_user.username,
    )
    db.add(quotation)
    await db.flush()

    for item in items:
        db.add(DealerQuotationItem(quotation_id=quotation.id, **item))

    await audit(
        db, user=current_user, action="DEALER_QUOTATION_CREATED",
        table_name="dealer_quotations", record_id=str(quotation.id),
        new_value={"dealer_id": str(dealer.id), "total_quantity": total_quantity, "total_price": str(total_price)},
        request=request,
    )
    await db.commit()
    return RedirectResponse(url=f"{back}?success=Quotation+{quote_number}+created", status_code=302)


def _safe_back(return_to: str, default: str = "/quotations") -> str:
    """Only a local path (never another site) is used to come back."""
    rt = (return_to or "").strip()
    return rt if rt.startswith("/") and not rt.startswith("//") else default


def _with(url: str, key: str, msg: str) -> str:
    from urllib.parse import quote_plus
    return f"{url}{'&' if '?' in url else '?'}{key}={quote_plus(msg)}"


# ── Status (2026-11-10, Pankaj) ──────────────────────────────────────────────
# Added -> Negotiation -> Approved / Rejected (Level 1, TeleSales Dashboard) or
# Expired. Negotiation / Expired are set from the Quotations table; Approve /
# Reject only by the creator's Report To hierarchy (or admin).

@router.post("/dealer-quotations/{quotation_id}/status")
async def set_dealer_quotation_status(
    request: Request,
    quotation_id: str,
    new_status: str = Form(...),
    return_to: str = Form(default="/quotations"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    back = _safe_back(return_to)
    quotation = (await db.execute(
        select(DealerQuotation).where(DealerQuotation.id == quotation_id))).scalar_one_or_none()
    if not quotation:
        return RedirectResponse(url=_with(back, "error", "Quotation not found"), status_code=302)
    allowed = {"added": ("negotiation", "expired"), "negotiation": ("expired",)}
    if new_status not in allowed.get(quotation.status, ()):
        return RedirectResponse(url=_with(back, "error", f"{quotation.quote_number} cannot move to {new_status.title()}"),
                                status_code=302)
    old = quotation.status
    quotation.status = new_status
    await audit(db, user=current_user, action="DEALER_QUOTATION_STATUS",
                table_name="dealer_quotations", record_id=str(quotation.id),
                old_value={"status": old}, new_value={"status": new_status}, request=request)
    await db.commit()
    return RedirectResponse(url=_with(back, "success", f"{quotation.quote_number} moved to {new_status.title()}"),
                            status_code=302)


@router.post("/dealer-quotations/{quotation_id}/decision")
async def decide_dealer_quotation(
    request: Request,
    quotation_id: str,
    decision: str = Form(...),          # approve / reject
    return_to: str = Form(default="/telesales-dashboard"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """Level 1 approval: admin, or anyone above the creator in Report To."""
    from services.report_chain import can_approve
    from utils.timezone import app_now
    back = _safe_back(return_to, "/telesales-dashboard")
    quotation = (await db.execute(
        select(DealerQuotation).where(DealerQuotation.id == quotation_id))).scalar_one_or_none()
    if not quotation:
        return RedirectResponse(url=_with(back, "error", "Quotation not found"), status_code=302)
    if quotation.status not in ("added", "negotiation") or decision not in ("approve", "reject"):
        return RedirectResponse(url=_with(back, "error", f"{quotation.quote_number} is not waiting for approval"),
                                status_code=302)
    if not await can_approve(db, current_user, quotation.created_by):
        return RedirectResponse(url=_with(back, "error", "Only the creator's Report To managers can approve this quotation"),
                                status_code=302)
    old = quotation.status
    quotation.status = "approved" if decision == "approve" else "rejected"
    quotation.confirmed_by = current_user.username      # Level 1 decision by / at
    quotation.confirmed_at = app_now()
    await audit(db, user=current_user, action="DEALER_QUOTATION_" + ("APPROVED" if decision == "approve" else "REJECTED"),
                table_name="dealer_quotations", record_id=str(quotation.id),
                old_value={"status": old}, new_value={"status": quotation.status, "level": 1}, request=request)
    await db.commit()
    return RedirectResponse(url=_with(back, "success", f"{quotation.quote_number} {quotation.status}"), status_code=302)


@router.get("/dealer-quotations/{quotation_id}", response_class=HTMLResponse)
async def dealer_quotation_detail(
    request: Request,
    quotation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """Telecalling Quotation detail page (2026-11-10, Pankaj): dealer, lines,
    status, Level 1 decision, Download once approved, History."""
    try:
        quotation = (await db.execute(
            select(DealerQuotation).where(DealerQuotation.id == quotation_id))).scalar_one_or_none()
    except Exception:
        quotation = None
    if not quotation:
        return RedirectResponse(url="/quotations?error=Quotation+not+found", status_code=302)
    items = (await db.execute(select(DealerQuotationItem).where(DealerQuotationItem.quotation_id == quotation.id)
                              .order_by(DealerQuotationItem.sort_order))).scalars().all()
    from services.report_chain import can_approve
    from services.record_history import record_history
    creator_name = (await db.execute(select(User.full_name).where(User.username == quotation.created_by))).scalar_one_or_none()
    from templates_config import templates
    return templates.TemplateResponse("dealers/quotation_detail.html", {
        "request": request, "current_user": current_user, "q": quotation, "items": items,
        "creator_name": creator_name,
        "can_decide": quotation.status in ("added", "negotiation") and await can_approve(db, current_user, quotation.created_by),
        "history": await record_history(db, quotation.id),
    })


@router.get("/dealer-quotations/{quotation_id}/print", response_class=HTMLResponse)
async def print_dealer_quotation(
    request: Request,
    quotation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    """Download "Quote" (2026-11-10): printable quotation, only once approved."""
    quotation = (await db.execute(
        select(DealerQuotation).where(DealerQuotation.id == quotation_id))).scalar_one_or_none()
    if not quotation or quotation.status != "approved":
        return RedirectResponse(url="/quotations?error=" + ("Quotation+not+found" if not quotation else
                                "Only+approved+quotations+can+be+downloaded"), status_code=302)
    items = (await db.execute(select(DealerQuotationItem).where(DealerQuotationItem.quotation_id == quotation.id)
                              .order_by(DealerQuotationItem.sort_order))).scalars().all()
    # Company details: the Company Setting company of the creator's entity
    # (2026-11-10); the details saved on the quotation are the fallback.
    creator_entity = (await db.execute(select(User.entity).where(User.username == quotation.created_by))).scalar_one_or_none()
    company = None
    if creator_entity:
        from routers.settings import get_company_settings as _gcs
        from models.company import Company
        if (await db.execute(select(Company.id).where(Company.is_active == True, Company.company_entity == creator_entity))).first():
            company = await _gcs(db, entity=creator_entity)
    if not company:
        company = {"name": quotation.company_name or "", "address": quotation.company_address or "",
                   "gstin": quotation.company_gstin or "", "phone": quotation.company_phone or "",
                   "email": quotation.company_email or ""}
    from templates_config import templates
    return templates.TemplateResponse("dealers/quotation_print.html", {
        "request": request, "current_user": current_user, "q": quotation, "items": items, "company": company})


@router.post("/dealer-quotations/{quotation_id}/delete")
async def delete_dealer_quotation(
    request: Request,
    quotation_id: str,
    return_to: str = Form(default="/dealers"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    quotation = (await db.execute(
        select(DealerQuotation).where(DealerQuotation.id == quotation_id)
    )).scalar_one_or_none()
    if not quotation:
        return RedirectResponse(url=f"{return_to}?error=Quotation+not+found", status_code=302)

    await audit(
        db, user=current_user, action="DEALER_QUOTATION_DELETED",
        table_name="dealer_quotations", record_id=str(quotation.id),
        old_value={"status": quotation.status, "dealer_id": str(quotation.dealer_id)},
        request=request,
    )
    await db.delete(quotation)
    await db.commit()
    sep = "&" if "?" in return_to else "?"
    return RedirectResponse(url=f"{return_to}{sep}success=Quotation+deleted", status_code=302)
