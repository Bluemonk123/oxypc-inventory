from utils.price_export import blank_prices as _blank_prices   # Pricing View (2026-11-03)
"""
Partner Payments — finance verification of partner bid payments.

Shows the same Marked Won set as Bids Created, but the action is Verify
Payment: finance opens what the partner submitted (date / mode / UTR /
amount) and confirms it. Verification is what moves the bid's derived
status to "Payment Done" on every page that shows it.
"""
from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import uuid as uuid_mod

from templates_config import templates
from database import get_db
from utils.timezone import app_now
from models.user import User, UserRole
from models.partner import PartnerBid
from models.partner_payment import PartnerBidPayment
from services.audit_engine import audit
from auth.dependencies import require_roles, verify_csrf

router = APIRouter(prefix="/partner-payments", tags=["partner_payments"],
                   dependencies=[Depends(verify_csrf)])

# Finance-gated: verifying money is not a trade-partner-desk action.
allowed = require_roles(UserRole.admin, UserRole.sales_manager)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def partner_payments(request: Request, db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(allowed)):
    from routers.partner_admin import _bid_rows, _payment_map, _decorate_payments
    won = (await db.execute(
        select(PartnerBid).where(PartnerBid.status == "won")
        .order_by(PartnerBid.won_at.desc())
    )).scalars().all()
    rows = _decorate_payments(await _bid_rows(db, won),
                              await _payment_map(db, [b.id for b in won]))
    return templates.TemplateResponse("trade_partner/partner_payments.html", {
        "request": request, "current_user": current_user, "rows": rows,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.get("/{bid_id}/details")
async def payment_details(bid_id: str, db: AsyncSession = Depends(get_db),
                          current_user: User = Depends(allowed)):
    try:
        bid_uuid = uuid_mod.UUID(bid_id)
    except (ValueError, AttributeError):
        raise HTTPException(404)
    pay = (await db.execute(
        select(PartnerBidPayment).where(PartnerBidPayment.bid_id == bid_uuid)
        .order_by(PartnerBidPayment.submitted_at.desc())
    )).scalars().first()
    if not pay:
        return JSONResponse(_blank_prices(current_user, 'partner_payments', {"has_payment": False,
                             "message": "The partner has not submitted payment details yet."}, ['payment_amount', 'amount', 'bid_amount', 'total', 'price']))
    return JSONResponse(_blank_prices(current_user, 'partner_payments', {
        "has_payment": True,
        "payment_date": pay.payment_date.strftime("%d-%b-%Y") if pay.payment_date else "-",
        "payment_mode": (pay.payment_mode or "-").replace("_", " ").title(),
        "payment_utr": pay.payment_utr or "-",
        "payment_amount": float(pay.payment_amount or 0),
        "submitted_by": pay.submitted_by or "-",
        "submitted_at": pay.submitted_at.strftime("%d-%b-%Y %H:%M") if pay.submitted_at else "-",
        "verified": bool(pay.verified),
    }, ['payment_amount', 'amount', 'bid_amount', 'total', 'price']))


@router.post("/{bid_id}/verify")
async def verify_payment(bid_id: str, request: Request, verify_notes: str = Form(""),
                         db: AsyncSession = Depends(get_db),
                         current_user: User = Depends(allowed)):
    try:
        bid_uuid = uuid_mod.UUID(bid_id)
    except (ValueError, AttributeError):
        raise HTTPException(404)
    pay = (await db.execute(
        select(PartnerBidPayment).where(PartnerBidPayment.bid_id == bid_uuid)
        .order_by(PartnerBidPayment.submitted_at.desc())
    )).scalars().first()
    if not pay:
        return RedirectResponse(
            url="/partner-payments?error=No+payment+has+been+submitted+for+this+bid",
            status_code=302)
    if pay.verified:
        return RedirectResponse(url="/partner-payments?success=Payment+already+verified",
                                status_code=302)
    pay.verified = True
    pay.verified_by = current_user.username
    pay.verified_at = app_now()
    pay.verify_notes = (verify_notes or "").strip() or None
    await audit(db, user=current_user, action="PARTNER_PAYMENT_VERIFIED",
                table_name="partner_bid_payments", record_id=str(pay.id),
                new_value={"bid_id": str(bid_uuid), "utr": pay.payment_utr},
                request=request)
    await db.commit()
    return RedirectResponse(url="/partner-payments?success=Payment+verified", status_code=302)
