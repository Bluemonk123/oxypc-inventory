"""Dealer Quotations — a quotation document shared with a dealer, created from
the "Quotation" button on Dealer Management / Dealer Detail. Snapshots dealer
+ company details at creation time (so it stays accurate as a historical
document) and totals are always computed server-side from the submitted line
items, never trusted from the client."""
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
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

    count_result = await db.execute(select(func.count(DealerQuotation.id)))
    seq = (count_result.scalar() or 0) + 1
    quote_number = f"QTN-{seq:04d}"

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
        status="shared",
        created_by=current_user.username,
    )
    db.add(quotation)
    await db.flush()

    for item in items:
        db.add(DealerQuotationItem(quotation_id=quotation.id, **item))

    await audit(
        db, user=current_user, action="DEALER_QUOTATION_CREATED",
        table_name="dealer_quotations", record_id=quotation.quote_number,
        new_value={"dealer_id": str(dealer.id), "total_quantity": total_quantity, "total_price": str(total_price)},
        request=request,
    )
    await db.commit()
    return RedirectResponse(url=f"{back}?success=Quotation+{quote_number}+created", status_code=302)


@router.post("/dealer-quotations/{quotation_id}/confirm")
async def confirm_dealer_quotation(
    request: Request,
    quotation_id: str,
    return_to: str = Form(default="/dealers"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_sales),
):
    from utils.timezone import app_now
    quotation = (await db.execute(
        select(DealerQuotation).where(DealerQuotation.id == quotation_id)
    )).scalar_one_or_none()
    if not quotation:
        return RedirectResponse(url=f"{return_to}?error=Quotation+not+found", status_code=302)

    quotation.status = "confirmed"
    quotation.confirmed_by = current_user.username
    quotation.confirmed_at = app_now()

    await audit(
        db, user=current_user, action="DEALER_QUOTATION_CONFIRMED",
        table_name="dealer_quotations", record_id=quotation.quote_number,
        new_value={"status": "confirmed"}, request=request,
    )
    await db.commit()
    sep = "&" if "?" in return_to else "?"
    return RedirectResponse(url=f"{return_to}{sep}success=Quotation+{quotation.quote_number}+confirmed", status_code=302)


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
        table_name="dealer_quotations", record_id=quotation.quote_number,
        old_value={"status": quotation.status, "dealer_id": str(quotation.dealer_id)},
        request=request,
    )
    await db.delete(quotation)
    await db.commit()
    sep = "&" if "?" in return_to else "?"
    return RedirectResponse(url=f"{return_to}{sep}success=Quotation+deleted", status_code=302)
