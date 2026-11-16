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


@router.get("/modal-data")
async def quotation_modal_data(
    opp_id: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create Quotation modal (2026-11-16, Pankaj) — ONE modal for CRM Quotes
    and Telecaller Quotations. Dealers (Telecaller, scoped like the Dealers
    page), Accounts (Buyer / Supplier), company profiles and the line-item
    dropdown lists; with opp_id, lines prefilled from the deal's lot."""
    from fastapi.responses import JSONResponse
    from models.crm import CRMContact
    from models.company import Company
    from sqlalchemy import func
    from services.quote_lines import options
    dq = select(Dealer).where(Dealer.trashed_at.is_(None))
    if current_user.role == UserRole.telecaller:
        dq = dq.where(Dealer.assigned_to == current_user.username)
    dealers = [{"id": str(d.id), "name": d.business_name or "", "type": (d.dealer_type or "").upper(),
                "phone": d.phone or "", "email": d.email or "", "address": d.address or "", "gstin": d.gstin or ""}
               for d in (await db.execute(dq.order_by(Dealer.business_name))).scalars().all()]
    accounts = [{"id": str(c.id), "name": c.company_name or "", "type": (c.contact_type or "").lower(),
                 "phone": c.phone or "", "email": c.email or "",
                 "address": ", ".join(x for x in (c.address, c.city, c.state, c.pincode) if x), "gstin": c.gstin or ""}
                for c in (await db.execute(select(CRMContact).where(
                    CRMContact.is_trashed.isnot(True), func.coalesce(CRMContact.status, "") != "blacklisted")
                    .order_by(CRMContact.company_name))).scalars().all()]
    companies = [{"id": str(c.id), "name": c.company_name or "", "entity": c.company_entity or "",
                  "address": c.company_address or "", "gstin": c.company_gstin or "",
                  "phone": c.company_phone or "", "email": c.company_email or ""}
                 for c in (await db.execute(select(Company).where(Company.is_active == True)  # noqa: E712
                                            .order_by(Company.created_at))).scalars().all()]
    prefill, contact_id = [], ""
    if opp_id:
        try:
            from models.crm import CRMSalesOpportunity
            from services.opportunity_lot import lot_for_opportunity, winning_bid_for_opportunity
            from services.quote_lines import lot_lines
            opp = (await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == opp_id))).scalar_one_or_none()
            if opp:
                contact_id = str(opp.contact_id) if opp.contact_id else ""
                lot = await lot_for_opportunity(db, opp.id)
                if lot:
                    bid = await winning_bid_for_opportunity(db, opp.id)
                    prefill = [{k: (str(v) if k in ("unit_price", "total_price") else v) for k, v in ln.items() if k != "tags"}
                               for ln in await lot_lines(db, lot, total=bid.bid_amount if bid else None,
                                                         unit=None if bid else (lot.selling_price or 0) / max(1, lot.qty or 1))]
        except Exception:
            prefill = []
    return JSONResponse({"dealers": dealers, "accounts": accounts, "companies": companies,
                         "options": options(), "prefill": prefill, "contact_id": contact_id},
                        headers={"Cache-Control": "no-store"})


@router.get("/tags")
async def quotation_tags(
    kind: str = Query(default="tele"),
    ids: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Tags column (2026-11-16, Pankaj): Ready-for-Sale tags whose configuration
    matches each quotation's lines (services/quote_lines.line_matches) — up to
    each line's quantity. kind = tele (Telecaller) | crm (CRM quotes)."""
    import uuid as _u
    from fastapi.responses import JSONResponse
    from services.quote_lines import ready_devices, tags_for, item_dict
    qids = []
    for x in (ids or "").split(",")[:300]:
        try:
            qids.append(_u.UUID(x.strip()))
        except ValueError:
            pass
    if not qids:
        return JSONResponse({})
    if kind == "crm":
        from models.crm import CRMQuoteItem as Item
        key = Item.quote_id
    else:
        from models.dealer_quotation import DealerQuotationItem as Item
        key = Item.quotation_id
    by_q = {}
    for it in (await db.execute(select(Item).where(key.in_(qids)))).scalars().all():
        by_q.setdefault(str(getattr(it, key.key)), []).append(item_dict(it))
    devices = await ready_devices(db)
    return JSONResponse({q: tags_for(lines, devices) for q, lines in by_q.items()},
                        headers={"Cache-Control": "no-store"})
