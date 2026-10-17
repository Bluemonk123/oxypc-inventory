"""
Extended Warranty — manage/extend a sold device's warranty (2026-09-14)
========================================================================
Top form (re)assigns a Warranty Type to any already-sold device, sourced
from the same Master Data "Sales: Warranty Type" dropdown (sale_warranty_type)
the New Sale form uses. Table lists every Sale with a warranty on file.
Update modal extends an existing warranty out to a new date, adding the
day-delta onto Sale.warranty_days (a running total) and logging the change
into Sale.notes.

No new table — everything lives on the existing Sale row (Sale.sold_at is
"Warranty Starts", Sale.warranty_expires_at is "Warranty Stops",
Sale.warranty_days is the running day count), per the smaller-schema-change
call made for this feature.
"""
import uuid as _uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from templates_config import templates
from database import get_db
from utils.timezone import app_now
from models.user import User
from models.device import Device
from models.lot import Lot
from models.sales import Sale
from auth.dependencies import verify_csrf, require_module_perm
from services.audit_engine import audit
from utils.warranty import WARRANTY_DURATIONS, parse_warranty_duration as _parse_warranty_duration

router = APIRouter(prefix="/extended-warranty", tags=["extended_warranty"],
                   dependencies=[Depends(verify_csrf)])
allowed = require_module_perm("extended_warranty")


@router.get("", response_class=HTMLResponse)
async def extended_warranty_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    raw_rows = (await db.execute(
        select(Sale, Device.barcode, Lot.lot_number)
        .join(Device, Sale.device_id == Device.id)
        .outerjoin(Lot, Device.lot_id == Lot.id)
        .where(Sale.warranty_type != 'none')
        .order_by(Sale.warranty_expires_at.is_(None), Sale.warranty_expires_at.asc())
    )).all()

    rows = []
    today = app_now().date()
    for sale, barcode, lot_number in raw_rows:
        days = sale.warranty_days
        if days is None:
            # Sales whose warranty was set at time-of-sale (not via this
            # page) never got warranty_days populated — fall back to the
            # same duration table the Sale's own warranty_type maps to.
            delta = WARRANTY_DURATIONS.get(sale.warranty_type)
            days = delta.days if delta else None

        if sale.warranty_expires_at:
            left_days = (sale.warranty_expires_at.date() - today).days
            warranty_left = f"{left_days} days" if left_days > 0 else "Expired"
        else:
            left_days, warranty_left = None, "—"

        rows.append({
            "sale": sale, "barcode": barcode, "lot_number": lot_number,
            "warranty_days": days, "warranty_left": warranty_left, "left_days": left_days,
        })

    return templates.TemplateResponse("extended_warranty/index.html", {
        "request": request, "current_user": current_user, "rows": rows,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/set")
async def extended_warranty_set(
    request: Request,
    barcode: str = Form(...),
    warranty_type: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    bc = barcode.strip()
    # Case-insensitive (2026-10-17): legacy tags are stored in mixed case
    # (e.g. "4488260526l0731"), and the page's own tag lookup (/devices/api/
    # brief) already matches case-insensitively — an exact match here made a
    # tag the lookup had just found fail with "not found" on Set. .first()
    # rather than scalar_one_or_none() because duplicate-case rows exist.
    device = (await db.execute(
        select(Device).where(func.upper(Device.barcode) == bc.upper())
        .order_by(Device.is_trashed.asc(), Device.updated_at.desc())
    )).scalars().first()
    if not device:
        return RedirectResponse(url=f"/extended-warranty?error=Device+{bc}+not+found", status_code=302)

    sale = (await db.execute(
        select(Sale).where(Sale.device_id == device.id).order_by(Sale.sold_at.desc()).limit(1)
    )).scalars().first()
    if not sale:
        return RedirectResponse(url=f"/extended-warranty?error=No+sale+found+for+{bc}", status_code=302)

    slug, days = _parse_warranty_duration(warranty_type)
    if not slug or not days:
        return RedirectResponse(url="/extended-warranty?error=Select+a+valid+Warranty+Type", status_code=302)

    sale.warranty_type = slug
    sale.warranty_days = days
    sale.warranty_expires_at = (sale.sold_at or app_now()) + timedelta(days=days)

    await audit(db, user=current_user, action="WARRANTY_SET",
                table_name="sales", record_id=str(sale.id),
                new_value={"barcode": bc, "warranty_type": slug, "warranty_days": sale.warranty_days},
                request=request)
    await db.commit()
    return RedirectResponse(url="/extended-warranty?success=Warranty+set", status_code=302)


@router.post("/{sale_id}/update")
async def extended_warranty_update(
    sale_id: str,
    extended_warranty: str = Form(...),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    try:
        sid = _uuid.UUID(sale_id)
    except ValueError:
        raise HTTPException(404, "Sale not found")
    sale = (await db.execute(select(Sale).where(Sale.id == sid))).scalar_one_or_none()
    if not sale:
        raise HTTPException(404, "Sale not found")

    try:
        new_expiry = datetime.strptime(extended_warranty.strip(), "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Invalid Extended Warranty date")

    current_expiry = sale.warranty_expires_at or sale.sold_at or app_now()
    added_days = (new_expiry.date() - current_expiry.date()).days
    if added_days <= 0:
        raise HTTPException(400, "Extended Warranty date must be after the current Warranty Stops date.")

    sale.warranty_expires_at = new_expiry
    sale.warranty_days = (sale.warranty_days or 0) + added_days
    if notes.strip():
        stamp = (f"[Warranty extended by {added_days}d by {current_user.username} "
                 f"on {app_now().strftime('%d-%m-%Y')}] {notes.strip()}")
        sale.notes = f"{sale.notes}\n{stamp}" if sale.notes else stamp

    await audit(db, user=current_user, action="WARRANTY_EXTENDED",
                table_name="sales", record_id=str(sale.id),
                new_value={"added_days": added_days, "new_expiry": new_expiry.isoformat(),
                           "warranty_days": sale.warranty_days})
    await db.commit()
    return JSONResponse({
        "ok": True,
        "warranty_days": sale.warranty_days,
        "warranty_expires_at": sale.warranty_expires_at.strftime("%d-%m-%Y"),
    })
