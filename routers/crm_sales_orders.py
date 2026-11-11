"""Sales Orders (2026-11-01) — read-only list + detail. SOs are created by
services/sales_orders.py when a Buyer Deal's Quotation Level 2 is approved;
Accounts verifies them from the Accounts Dashboard."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from templates_config import templates
from database import get_db
from auth.dependencies import get_current_user, verify_csrf
from models.user import User
from models.crm import CRMSalesOrder, MATERIAL_TYPES

router = APIRouter(prefix="/crm/sales-orders", tags=["crm-sales-orders"], dependencies=[Depends(verify_csrf)])


@router.get("", response_class=HTMLResponse)
async def list_sales_orders(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    orders = (await db.execute(
        select(CRMSalesOrder)
        .options(selectinload(CRMSalesOrder.contact), selectinload(CRMSalesOrder.quote),
                 selectinload(CRMSalesOrder.deal))
        .order_by(CRMSalesOrder.created_at.desc())
    )).scalars().all()
    from services.doc_downloads import so_downloads   # Download column (2026-11-10)
    return templates.TemplateResponse("crm/sales_orders/list.html", {
        "request": request, "current_user": current_user, "orders": orders,
        "downloads": await so_downloads(db, orders),
    })


@router.get("/{so_id}", response_class=HTMLResponse)
async def sales_order_detail(
    request: Request, so_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        so = (await db.execute(
            select(CRMSalesOrder)
            .options(selectinload(CRMSalesOrder.contact), selectinload(CRMSalesOrder.quote),
                     selectinload(CRMSalesOrder.deal), selectinload(CRMSalesOrder.items))
            .where(CRMSalesOrder.id == so_id)
        )).scalar_one_or_none()
    except Exception:
        so = None
    if not so:
        return RedirectResponse(url="/crm/sales-orders?error=Sales+Order+not+found", status_code=302)
    from services.record_history import record_history   # History card (2026-11-09)
    _history = await record_history(db, so.id)
    return templates.TemplateResponse("crm/sales_orders/detail.html", {
        "history": _history,
        "request": request, "current_user": current_user, "so": so,
        "material_map": dict(MATERIAL_TYPES),
    })


@router.get("/{so_id}/print", response_class=HTMLResponse)
async def print_sales_order(
    request: Request, so_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Printable Sales Order (2026-11-10, Download column). Company details:
    the Company Setting company of the quote's From Entity."""
    try:
        so = (await db.execute(
            select(CRMSalesOrder)
            .options(selectinload(CRMSalesOrder.contact), selectinload(CRMSalesOrder.quote),
                     selectinload(CRMSalesOrder.deal), selectinload(CRMSalesOrder.items))
            .where(CRMSalesOrder.id == so_id)
        )).scalar_one_or_none()
    except Exception:
        so = None
    if not so:
        return RedirectResponse(url="/crm/sales-orders?error=Sales+Order+not+found", status_code=302)
    from routers.settings import get_company_settings
    entity = (so.quote.from_entity if so.quote else None) or (so.deal.from_entity if so.deal else None)
    company = await get_company_settings(db, entity=entity)
    return templates.TemplateResponse("crm/sales_orders/print.html", {
        "request": request, "current_user": current_user, "so": so, "company": company,
        "material_map": dict(MATERIAL_TYPES),
    })
