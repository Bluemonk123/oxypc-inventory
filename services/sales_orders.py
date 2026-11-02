"""Sales Orders (2026-11-01): a Buyer Deal's quotation becomes a Sales Order
when its Quotation Level 2 approval is given. Header and lines are copied
from the quote (snapshot) so later quote edits never change the SO."""
from decimal import Decimal
from sqlalchemy import select, func

from utils.timezone import app_now
from models.crm import CRMQuote, CRMQuoteItem, CRMSalesOrder, CRMSalesOrderItem


async def next_so_number(db) -> str:
    year = app_now().year
    prefix = f"SO-{year}-"
    last = (await db.execute(
        select(func.max(CRMSalesOrder.so_number)).where(CRMSalesOrder.so_number.like(f"{prefix}%"))
    )).scalar()
    seq = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}{seq:04d}"


async def create_sales_order_from_deal(db, opp, user, request=None):
    """Create the SO for opp.quote_id. Returns the SO, or None when the deal
    has no quote or an open SO already exists for that quote."""
    from services.audit_engine import audit
    if not opp.quote_id:
        return None
    existing = (await db.execute(
        select(CRMSalesOrder).where(CRMSalesOrder.quote_id == opp.quote_id,
                                    CRMSalesOrder.status != "cancelled")
    )).scalars().first()
    if existing:
        return None
    quote = (await db.execute(select(CRMQuote).where(CRMQuote.id == opp.quote_id))).scalar_one_or_none()
    if not quote:
        return None
    items = (await db.execute(
        select(CRMQuoteItem).where(CRMQuoteItem.quote_id == quote.id)
        .order_by(CRMQuoteItem.sort_order, CRMQuoteItem.line_number)
    )).scalars().all()
    so = CRMSalesOrder(
        so_number=await next_so_number(db), quote_id=quote.id, deal_id=opp.id,
        contact_id=quote.contact_id or opp.contact_id, so_date=app_now().date(),
        payment_terms=quote.payment_terms, status="open", created_by=user.username,
    )
    total = Decimal("0")
    for n, it in enumerate(items, 1):
        line_total = Decimal(it.unit_price or 0) * int(it.quantity or 0)
        total += line_total
        so.items.append(CRMSalesOrderItem(
            line_number=n, device_type=it.device_type, material_type=it.material_type,
            grade=it.grade, po_category=it.po_category, quantity=it.quantity,
            unit_price=it.unit_price, total_price=line_total, specs_note=it.specs_note,
        ))
    so.total_amount = total
    db.add(so)
    await db.flush()
    await audit(db, action="CRM_SALES_ORDER_CREATED", user=user, table_name="crm_sales_orders",
                record_id=str(so.id),
                new_value={"so_number": so.so_number, "quote": quote.quote_number,
                           "deal": opp.opp_number, "total": str(total)}, request=request)
    return so
