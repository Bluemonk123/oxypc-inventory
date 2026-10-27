from templates_config import templates
from datetime import datetime
from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from database import get_db
from models.user import User
from models.dealer_quotation import DealerQuotation
from models.dealers import Dealer
from models.user import UserRole
from routers.company_settings import get_company_settings
from auth.dependencies import get_current_user, verify_csrf, require_module_perm

router = APIRouter(prefix="/quotations", tags=["quotations"], dependencies=[Depends(verify_csrf)])


@router.get("", response_class=HTMLResponse)
async def list_quotations(
    request: Request,
    q: str = Query(default=""),
    status: str = Query(default=""),
    created_by: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _perm: User = Depends(require_module_perm("quotations", "enable")),
):
    """All Dealer Quotations, system-wide — the standalone list page linked
    from the sidebar. Reuses the same DealerQuotation rows as the "Quotation
    Shared" table embedded on Dealer Management/Detail/Telecalling Dashboard
    (single source of truth, no data divergence)."""
    filters = []
    if q:
        like = f"%{q}%"
        filters.append(or_(
            DealerQuotation.customer_name.ilike(like),
            DealerQuotation.quote_number.ilike(like),
            DealerQuotation.dealer_phone.ilike(like),
        ))
    if status:
        filters.append(DealerQuotation.status == status)
    if created_by:
        filters.append(DealerQuotation.created_by == created_by)
    if date_from:
        try:
            filters.append(DealerQuotation.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            filters.append(DealerQuotation.created_at <= datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59))
        except ValueError:
            pass

    stmt = select(DealerQuotation).order_by(DealerQuotation.created_at.desc())
    if filters:
        stmt = stmt.where(*filters)
    quotations = (await db.execute(stmt)).scalars().all()

    creators = sorted({q.created_by for q in (await db.execute(select(DealerQuotation))).scalars().all() if q.created_by})

    # Add Quote (2026-10-27): dealers for the Create Quotation modal's picker.
    # Same scope as the Dealers page — a Telecaller sees only their own
    # dealers; trashed dealers are left out.
    dq = select(Dealer.id, Dealer.business_name, Dealer.dealer_type, Dealer.phone,
                Dealer.email, Dealer.address, Dealer.gstin).where(Dealer.trashed_at.is_(None))
    if current_user.role == UserRole.telecaller:
        dq = dq.where(Dealer.assigned_to == current_user.username)
    qtn_dealers = [
        {"id": str(r.id), "business_name": r.business_name or "", "dealer_type": r.dealer_type or "",
         "phone": r.phone or "", "email": r.email or "", "address": r.address or "", "gstin": r.gstin or ""}
        for r in (await db.execute(dq.order_by(Dealer.business_name))).all()
    ]

    return templates.TemplateResponse("quotations/list.html", {
        "request": request, "current_user": current_user,
        "quotations": quotations,
        "q": q, "status": status, "created_by": created_by,
        "date_from": date_from, "date_to": date_to,
        "creators": creators,
        "qtn_dealers": qtn_dealers,
        "company_settings": await get_company_settings(db),
    })
