"""Trade Partner portal — dealer-facing routes (/partner).

Data boundary: these routes render dealer-safe data ONLY (partner_listings
snapshots). They must never touch lots.buying_price, supplier_name, internal
margins, cost_basis, or staff data. Auth is the partner JWT cookie — staff
sessions cannot open these routes.
"""
import os
import secrets
import time
import uuid as uuid_mod
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func

from urllib.parse import quote, quote_plus

from database import get_db
from templates_config import templates
from models.dealers import Dealer
from models.user import User
from models.partner import (
    PartnerListing, PartnerLoginLog, PartnerListingView, PartnerBooking,
    PartnerPaymentProof, LotDealerVisibility, LotBookingRequest,
    PartnerBid, min_next_bid,
)
from decimal import Decimal, InvalidOperation
from models.lot import Lot
from models.device import Device, DeviceStage
from models.iqc_inspection import IQCInspection
from auth.dependencies import verify_password_async
from auth.partner_auth import (
    get_current_partner, verify_partner_csrf, create_partner_token,
    new_partner_csrf, normalize_phone,
    PARTNER_TOKEN_COOKIE, PARTNER_CSRF_COOKIE, PARTNER_SESSION_MINUTES,
)
from services.audit_engine import audit
from services.partner_service import (
    get_settings, ageing_bucket, photos_list, expire_stale_bookings,
    create_booking, BookingError,
)
from utils.timezone import app_now
from config import COOKIE_SECURE
from limiter import limiter, ip_key_func

router = APIRouter(prefix="/partner", tags=["trade-partner-portal"])

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


def _login_page(request: Request, error: str = None):
    csrf_tok = new_partner_csrf()
    resp = templates.TemplateResponse("partner/login.html", {
        "request": request, "error": error, "login_csrf": csrf_tok,
    })
    resp.set_cookie(PARTNER_CSRF_COOKIE, csrf_tok, httponly=False,
                    samesite="strict", secure=COOKIE_SECURE)
    # Prevent the browser/bfcache from serving a stale copy of this page —
    # a cached page carries an old CSRF hidden field that no longer matches
    # the cookie, which fails with a confusing "Session expired" on submit.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _login_page(request)


@router.post("/login")
@limiter.limit("5/minute", key_func=ip_key_func)
async def login(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    # CSRF: double-submit against the partner cookie set on the login page
    cookie_token = request.cookies.get(PARTNER_CSRF_COOKIE, "")
    form = await request.form()
    if not cookie_token or form.get("csrf_token", "") != cookie_token:
        return _login_page(request, "Session expired — please try again.")

    norm = normalize_phone(phone)
    result = await db.execute(select(Dealer).where(Dealer.portal_phone == norm))
    dealer = result.scalar_one_or_none()

    ua = (request.headers.get("user-agent") or "")[:300]
    ip = request.client.host if request.client else None

    if dealer:
        cutoff = app_now() - timedelta(minutes=LOCKOUT_MINUTES)
        fail_r = await db.execute(
            select(func.count(PartnerLoginLog.id)).where(
                PartnerLoginLog.dealer_id == dealer.id,
                PartnerLoginLog.success == False,  # noqa: E712
                PartnerLoginLog.created_at >= cutoff,
            )
        )
        if (fail_r.scalar() or 0) >= MAX_FAILED_ATTEMPTS:
            return _login_page(
                request,
                f"Account locked — too many failed attempts. Try again in {LOCKOUT_MINUTES} minutes.",
            )

    if (
        not dealer
        or not dealer.portal_enabled
        or dealer.status != "active"
        or not dealer.portal_password_hash
        or not await verify_password_async(password, dealer.portal_password_hash)
    ):
        db.add(PartnerLoginLog(dealer_id=dealer.id if dealer else None,
                               phone_attempted=norm, ip_address=ip,
                               user_agent=ua, success=False))
        await db.commit()
        return _login_page(request, "Invalid phone number or password")

    token = create_partner_token(dealer)
    db.add(PartnerLoginLog(dealer_id=dealer.id, phone_attempted=norm,
                           ip_address=ip, user_agent=ua, success=True))
    await db.execute(update(Dealer).where(Dealer.id == dealer.id)
                     .values(portal_last_login_at=app_now()))
    await db.commit()

    csrf_tok = new_partner_csrf()
    _max_age = PARTNER_SESSION_MINUTES * 60
    resp = RedirectResponse(url="/partner/catalog", status_code=302)
    resp.set_cookie(PARTNER_TOKEN_COOKIE, token, httponly=True, samesite="strict",
                    max_age=_max_age, secure=COOKIE_SECURE)
    resp.set_cookie(PARTNER_CSRF_COOKIE, csrf_tok, httponly=False, samesite="strict",
                    max_age=_max_age, secure=COOKIE_SECURE)
    return resp


@router.post("/logout")
async def logout(request: Request, _csrf=Depends(verify_partner_csrf)):
    resp = RedirectResponse(url="/partner/login", status_code=302)
    resp.delete_cookie(PARTNER_TOKEN_COOKIE)
    resp.delete_cookie(PARTNER_CSRF_COOKIE)
    return resp


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, dealer: Dealer = Depends(get_current_partner)):
    return RedirectResponse(url="/partner/catalog", status_code=302)


@router.get("/sw.js")
async def service_worker():
    """Serve the PWA service worker from /partner/sw.js so its scope covers
    the whole dealer portal (a /static path would scope it to /static)."""
    from fastapi.responses import FileResponse
    from config import BASE_DIR
    return FileResponse(
        os.path.join(BASE_DIR, "static", "partner", "sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


async def _sales_owner_wa(db: AsyncSession, dealer: Dealer):
    """WhatsApp number of the dealer's assigned sales owner (digits only)."""
    if not dealer.sales_owner_username:
        return None
    owner = (await db.execute(
        select(User).where(User.username == dealer.sales_owner_username)
    )).scalar_one_or_none()
    if not owner or not owner.whatsapp_number:
        return None
    digits = "".join(ch for ch in owner.whatsapp_number if ch.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    return digits or None


@router.get("/catalog", response_class=HTMLResponse)
async def catalog(
    request: Request,
    listing_type: str = "",
    brand: str = "",
    grade: str = "",
    sort: str = "newest",
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    """Published listings, dealer-safe fields only."""
    await expire_stale_bookings(db)
    await db.commit()

    q = select(PartnerListing).where(
        PartnerListing.status == "published",
        PartnerListing.is_active == True,  # noqa: E712
        PartnerListing.qty_available > 0,
    )
    if listing_type:
        q = q.where(PartnerListing.listing_type == listing_type)
    if brand:
        q = q.where(PartnerListing.brand.ilike(f"%{brand}%"))
    if grade:
        q = q.where(PartnerListing.grade_summary.ilike(f"%{grade}%"))
    if sort == "price_low":
        q = q.order_by(PartnerListing.dealer_price.asc())
    elif sort == "price_high":
        q = q.order_by(PartnerListing.dealer_price.desc())
    else:
        q = q.order_by(PartnerListing.created_at.desc())
    listings = (await db.execute(q)).scalars().all()

    # visible_to_segment gate (MVP segment logic that IS live)
    seg = dealer.price_segment or "new_dealer"
    listings = [l for l in listings if l.visible_to_segment in ("all", seg)]

    ageing = {str(l.id): ageing_bucket(l.stock_intake_date or l.created_at) for l in listings}
    if sort == "fresh":
        listings.sort(key=lambda l: ageing[str(l.id)]["days"] or 0)
    elif sort == "deals":
        # Older stock first — where the deals are
        listings.sort(key=lambda l: ageing[str(l.id)]["days"] or 0, reverse=True)

    settings = await get_settings(db)

    # ── Requests for Lot visibility (item 28) — direct Lot Management rows
    # this dealer has been granted visibility on.
    #
    # "Price" here is lot.buying_price, surfaced in the admin UI as Base Price:
    # the operator-set floor that catalog bidding starts from. This is a
    # deliberate, instructed exception to the no-cost-to-dealers rule at the top
    # of this file — the base IS the offer. lot.selling_price (Target Selling
    # Price) stays internal, as do supplier_name and every other cost field.
    # Lots are open to every partner account by default. A lot flagged
    # Restricted is the exception: it appears only for dealers explicitly
    # granted it in LotDealerVisibility (Manage Lots -> Assign Visibility).
    granted_ids = {r for (r,) in (await db.execute(
        select(LotDealerVisibility.lot_id)
        .where(LotDealerVisibility.dealer_id == dealer.id)
    )).all()}
    vis_rows = [
        l for l in (await db.execute(
            select(Lot).order_by(Lot.purchase_date.desc())
        )).scalars().all()
        if not getattr(l, "is_restricted", False) or l.id in granted_ids
    ]
    # A lot reaches the partner catalog only once EVERY one of its tags is at
    # Ready to Sale. A lot still part-way through refurbishment cannot be sold
    # as a lot, so showing it invites bids on stock that is not there yet.
    if vis_rows:
        stage_rows = (await db.execute(
            select(Device.lot_id, Device.current_stage, func.count(Device.id))
            .where(Device.lot_id.in_([l.id for l in vis_rows]),
                   Device.is_active == True, Device.is_trashed == False)  # noqa: E712
            .group_by(Device.lot_id, Device.current_stage)
        )).all()
        total_by_lot, ready_by_lot = {}, {}
        for lid, stage, cnt in stage_rows:
            total_by_lot[lid] = total_by_lot.get(lid, 0) + cnt
            if stage == DeviceStage.ready_to_sale:
                ready_by_lot[lid] = ready_by_lot.get(lid, 0) + cnt
        vis_rows = [
            l for l in vis_rows
            if total_by_lot.get(l.id, 0) > 0
            and ready_by_lot.get(l.id, 0) == total_by_lot[l.id]
        ]

    visible_lots = []
    if vis_rows:
        lot_ids = [l.id for l in vis_rows]
        avail_map = {str(lid): ready_by_lot.get(lid, 0) for lid in lot_ids}
        my_bookings = (await db.execute(
            select(LotBookingRequest).where(
                LotBookingRequest.dealer_id == dealer.id,
                LotBookingRequest.lot_id.in_(lot_ids),
                LotBookingRequest.status != "withdrawn",
            )
        )).scalars().all()
        booking_by_lot = {str(b.lot_id): b for b in my_bookings}
        for lot in vis_rows:
            lid = str(lot.id)
            visible_lots.append({
                "id": lid, "lot_number": lot.lot_number,
                "purchase_date": lot.purchase_date,
                "available_quantity": avail_map.get(lid, 0),
                # buying_price is NOT NULL and defaults to 0, so "unset" arrives
                # as 0 rather than None. Map it to None: the template then shows
                # "Not set" instead of "₹0.00" (which reads as free), and it
                # matches the bid floor, where 0 is likewise treated as no reserve.
                "price": float(lot.buying_price) if lot.buying_price else None,
                "booking": booking_by_lot.get(lid),
            })

    # ── Available Stock, rendered inline (this used to be a per-lot modal) ───
    # Model-wise breakdown across every lot this dealer can see. Lot No is part
    # of the grouping key now that rows from different lots share one table:
    # without it, two lots holding the same model+spec would collapse into a
    # single row and the dealer could not tell which lot to bid on.
    # One row PER DEVICE, not per model group. Cosmetic condition is recorded
    # per unit (a scratch on one lid says nothing about the next), so it cannot
    # be aggregated — grouping would have to drop those columns entirely. The
    # table scrolls in both directions to carry the width and the depth.
    lot_stock = []
    if vis_rows:
        lot_no = {l.id: l.lot_number for l in vis_rows}
        srows = (await db.execute(
            select(Device, IQCInspection)
            .join(IQCInspection, IQCInspection.device_id == Device.id, isouter=True)
            .where(Device.lot_id.in_(lot_ids), Device.is_active == True,  # noqa: E712
                   Device.is_trashed == False)  # noqa: E712
            .order_by(Device.lot_id, Device.model, Device.barcode)
        )).all()

        def _v(x):
            """Blank cells read as 'not recorded'; '—' is the table's empty mark."""
            return x if x not in (None, "") else "—"

        for dev, iqc in srows:
            lot_stock.append({
                "lot_id": str(dev.lot_id),
                "lot_number": lot_no.get(dev.lot_id, "—"),
                # Hardware specification
                "model": _v(dev.model),
                "brand": _v(dev.brand),
                "device_type": _v(dev.device_type),
                "cpu": _v(dev.cpu),
                "cpu_make": _v(dev.cpu_make),
                "generation": _v(dev.generation),
                "ram": f"{dev.ram_gb} GB" if dev.ram_gb else "—",
                "ram_type": _v(dev.ram_type),
                "total_ram_size": _v(dev.total_ram_size),
                "storage": (f"{dev.storage_gb} GB {dev.storage_type or ''}".strip()
                            if dev.storage_gb else "—"),
                "total_hdd_size": _v(dev.total_hdd_size),
                "screen_size": _v(dev.screen_size),
                "battery_health": (f"{dev.battery_health_pct}%"
                                   if dev.battery_health_pct is not None else "—"),
                "color": _v(dev.color),
                "grade": getattr(dev.grade, "value", dev.grade) or "—",
                # Cosmetics, straight off the IQC inspection
                "display_scratch":  _v(getattr(iqc, "panel_a_scratch", None)),
                "display_dent":     _v(getattr(iqc, "panel_a_dent", None)),
                "display_broken":   _v(getattr(iqc, "panel_a_broken", None)),
                "bezel_scratch":    _v(getattr(iqc, "panel_c_scratch", None)),
                "base_scratch":     _v(getattr(iqc, "panel_b_scratch", None)),
                "palmrest_scratch": _v(getattr(iqc, "panel_d_scratch", None)),
                "screen_dot":       _v(getattr(iqc, "screen_dot", None)),
                "screen_line":      _v(getattr(iqc, "screen_line", None)),
                "screen_scratch":   _v(getattr(iqc, "screen_scratch", None)),
                "touch_screen":     _v(getattr(iqc, "touch_screen", None)),
                "hinge":            _v(getattr(iqc, "hinge_condition", None)),
                "keyboard":         _v(getattr(iqc, "keyboard_working", None)),
                "touchpad":         _v(getattr(iqc, "touchpad_working", None)),
                "speaker":          _v(getattr(iqc, "speaker_status", None)),
                "webcam":           _v(getattr(iqc, "webcam_status", None)),
            })

    return templates.TemplateResponse("partner/catalog.html", {
        "request": request, "dealer": dealer, "listings": listings,
        "lot_stock": lot_stock,
        "ageing": ageing, "incentive_text": settings.get("incentive_text") or "",
        "f_type": listing_type, "f_brand": brand, "f_grade": grade, "f_sort": sort,
        "partner_csrf": request.cookies.get(PARTNER_CSRF_COOKIE, ""),
        "visible_lots": visible_lots,
        "bids": await _bid_state(db, listings, dealer.id),
        "lot_bids": await _lot_bid_state(db, visible_lots, dealer.id),
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.get("/listings/{listing_id}", response_class=HTMLResponse)
async def listing_detail(
    request: Request,
    listing_id: str,
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    listing = (await db.execute(
        select(PartnerListing).where(PartnerListing.id == listing_id)
    )).scalar_one_or_none()
    seg = dealer.price_segment or "new_dealer"
    if (not listing or not listing.is_active
            or listing.status not in ("published", "sold_out")
            or listing.visible_to_segment not in ("all", seg)):
        return RedirectResponse(url="/partner/catalog", status_code=302)

    # Engagement capture for the dealer score
    db.add(PartnerListingView(listing_id=listing.id, dealer_id=dealer.id))
    await db.commit()

    share_text = quote(
        f"{listing.title}\n{listing.qty_available} units · "
        f"Grade {listing.grade_summary or '-'} · ₹{listing.dealer_price:,.0f}/unit "
        f"(MOQ {listing.moq})\n{str(request.base_url).rstrip('/')}/partner/listings/{listing.id}"
    )
    owner_wa = await _sales_owner_wa(db, dealer)
    ask_text = quote(f"Hi, I'm interested in {listing.listing_code} — {listing.title}")

    return templates.TemplateResponse("partner/listing_detail.html", {
        "request": request, "dealer": dealer, "listing": listing,
        "photos": photos_list(listing),
        "ageing": ageing_bucket(listing.stock_intake_date or listing.created_at),
        "share_text": share_text, "owner_wa": owner_wa, "ask_text": ask_text,
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
        "partner_csrf": request.cookies.get(PARTNER_CSRF_COOKIE, ""),
        "bid": (await _bid_state(db, [listing], dealer.id)).get(str(listing.id)),
    })


# ── Bidding ──────────────────────────────────────────────────────────────────

async def _bid_state(db: AsyncSession, listings, dealer_id):
    """Per-listing bid summary for the dealer-facing pages.

    Exposes the standing highest AMOUNT but never which dealer placed it —
    knowing a rival's exact position is competitive intelligence, and the file's
    rule is dealer-safe fields only. The dealer's own bid is returned in full.
    """
    ids = [l.id for l in listings]
    if not ids:
        return {}

    highest = dict((str(lid), amt) for lid, amt in (await db.execute(
        select(PartnerBid.listing_id, func.max(PartnerBid.bid_amount))
        .where(PartnerBid.listing_id.in_(ids), PartnerBid.status == "active")
        .group_by(PartnerBid.listing_id)
    )).all())

    mine = {}
    for b in (await db.execute(
        select(PartnerBid).where(
            PartnerBid.listing_id.in_(ids), PartnerBid.dealer_id == dealer_id,
            PartnerBid.status.in_(("active", "won")),
        ).order_by(PartnerBid.bid_amount.desc())
    )).scalars().all():
        mine.setdefault(str(b.listing_id), b)

    out = {}
    for l in listings:
        key = str(l.id)
        top = highest.get(key)
        out[key] = {
            "highest": top,
            "min_next": min_next_bid(l.dealer_price, top),
            "mine": mine.get(key),
            "leading": bool(top is not None and mine.get(key) is not None
                            and mine[key].bid_amount == top),
        }
    return out


async def _lot_bid_state(db: AsyncSession, lots, dealer_id):
    """Same shape as _bid_state, for Lots rather than Listings.

    Base price is lot.buying_price — the "Base Price" column in Lot Management.
    lot.selling_price is the internal Target Selling Price and is never the
    bidding floor. A lot with no base price set has no reserve, which the caller
    surfaces rather than silently treating as zero.
    """
    # The catalog builds visible_lots as dicts with a stringified id; the bid
    # queries compare against a UUID column, so coerce rather than lean on the
    # driver to cast (asyncpg will not).
    def _uid(v):
        return v if isinstance(v, uuid_mod.UUID) else uuid_mod.UUID(str(v))

    ids = [_uid(l["id"] if isinstance(l, dict) else l.id) for l in lots]
    if not ids:
        return {}

    highest = dict((str(lid), amt) for lid, amt in (await db.execute(
        select(PartnerBid.lot_id, func.max(PartnerBid.bid_amount))
        .where(PartnerBid.lot_id.in_(ids), PartnerBid.status == "active")
        .group_by(PartnerBid.lot_id)
    )).all())

    mine = {}
    for b in (await db.execute(
        select(PartnerBid).where(
            PartnerBid.lot_id.in_(ids), PartnerBid.dealer_id == dealer_id,
            PartnerBid.status.in_(("active", "won")),
        ).order_by(PartnerBid.bid_amount.desc())
    )).scalars().all():
        mine.setdefault(str(b.lot_id), b)

    # How many bids this dealer has already placed BELOW the base price — a
    # dealer gets exactly one of those, but unlimited bids at or above base.
    below_counts = {}
    for b in (await db.execute(
        select(PartnerBid).where(
            PartnerBid.lot_id.in_(ids), PartnerBid.dealer_id == dealer_id,
            PartnerBid.status.in_(("active", "won", "lost")),
        )
    )).scalars().all():
        key = str(b.lot_id)
        base_at_bid = b.base_amount
        if base_at_bid and Decimal(str(b.bid_amount)) < Decimal(str(base_at_bid)):
            below_counts[key] = below_counts.get(key, 0) + 1

    out = {}
    for l in lots:
        lid = str(l["id"] if isinstance(l, dict) else l.id)
        base = l["price"] if isinstance(l, dict) else l.buying_price
        top = highest.get(lid)
        # Competitive position is withheld while the standing high is below the
        # base price: a partner must not learn that the lot is currently going
        # for less than the asking price.
        reveal = bool(top is not None and base and Decimal(str(top)) >= Decimal(str(base)))
        if top is not None and not base:
            reveal = True          # no base set = nothing to protect
        out[lid] = {
            "highest": top if reveal else None,
            "highest_hidden": bool(top is not None and not reveal),
            "base": base,
            # No reserve when neither a base price nor a standing bid exists —
            # any positive amount is then a valid opening bid.
            "min_next": (min_next_bid(base or 0, top) if (base or top) else None),
            "mine": mine.get(lid),
            "leading": bool(reveal and top is not None and mine.get(lid) is not None
                            and mine[lid].bid_amount == top),
            "below_base_used": below_counts.get(lid, 0) > 0,
        }
    return out


async def _existing_lot_bid(db: AsyncSession, lot_id, dealer_id):
    """The dealer's standing bid on this lot, of either kind, or None.

    A partner account gets ONE bid per lot — an increment bid or a custom
    price, not one of each and not several of the same. 'lost' is excluded so a
    closed auction does not block a dealer forever if the lot is reopened.
    """
    lid = lot_id if isinstance(lot_id, uuid_mod.UUID) else uuid_mod.UUID(str(lot_id))
    return (await db.execute(
        select(PartnerBid).where(
            PartnerBid.lot_id == lid,
            PartnerBid.dealer_id == dealer_id,
            PartnerBid.status.in_(("active", "won")),
        ).order_by(PartnerBid.created_at.desc())
    )).scalars().first()


def _parse_amount(raw):
    try:
        amt = Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    return amt if amt > 0 else None


async def _next_bid_number(db: AsyncSession) -> str:
    n = ((await db.execute(select(func.count(PartnerBid.id)))).scalar() or 0) + 1
    return f"BID-{n:04d}"


@router.post("/listings/{listing_id}/bid")
async def place_bid(
    request: Request,
    listing_id: str,
    amount: str = Form(...),
    _csrf=Depends(verify_partner_csrf),
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    """Auction bid. Must clear the standing highest (or the base price) by 10%.

    The minimum is re-derived here from a row-locked read rather than trusting
    the figure the page rendered: between page load and submit another dealer
    may have bid, and a client-side-only check would let the second submitter
    win at a stale, lower price.
    """
    back = f"/partner/listings/{listing_id}"

    # Lock the listing row for the duration. Two dealers submitting at the same
    # instant would otherwise both read the same highest bid, both pass the
    # check, and both be recorded as valid at the same amount.
    listing = (await db.execute(
        select(PartnerListing).where(PartnerListing.id == listing_id)
        .with_for_update()
    )).scalar_one_or_none()

    seg = dealer.price_segment or "new_dealer"
    if (not listing or not listing.is_active or listing.status != "published"
            or listing.visible_to_segment not in ("all", seg)):
        await db.rollback()
        return RedirectResponse(url="/partner/catalog?error=Listing+not+available",
                                status_code=302)

    amt = _parse_amount(amount)
    if amt is None:
        await db.rollback()
        return RedirectResponse(url=f"{back}?error=Enter+a+valid+bid+amount",
                                status_code=302)

    top = (await db.execute(
        select(func.max(PartnerBid.bid_amount)).where(
            PartnerBid.listing_id == listing.id, PartnerBid.status == "active")
    )).scalar()
    floor = min_next_bid(listing.dealer_price, top)
    if amt < floor:
        await db.rollback()
        return RedirectResponse(
            url=f"{back}?error=Minimum+next+bid+is+Rs+{floor:.0f}", status_code=302)

    bid = PartnerBid(
        bid_number=await _next_bid_number(db),
        listing_id=listing.id, dealer_id=dealer.id,
        bid_amount=amt, bid_type="increment",
        previous_amount=top, base_amount=listing.dealer_price,
        status="active",
    )
    db.add(bid)
    await audit(db, action="PARTNER_BID_PLACED", table_name="partner_bids",
                record_id=str(bid.id),
                new_value={"listing": listing.listing_code, "amount": str(amt),
                           "previous": str(top) if top is not None else None,
                           "dealer": dealer.business_name},
                notes=f"dealer:{dealer.business_name}", request=request)
    await db.commit()
    return RedirectResponse(url=f"{back}?success=Bid+placed", status_code=302)


@router.post("/listings/{listing_id}/custom-price")
async def request_custom_price(
    request: Request,
    listing_id: str,
    amount: str = Form(...),
    notes: str = Form(""),
    _csrf=Depends(verify_partner_csrf),
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    """Dealer names their own figure outside the auction ladder.

    Deliberately NOT held to the 10% floor — the whole point is to let a dealer
    say what the lot is worth to them when the ladder has run past it. It lands
    on the same Bids page flagged as custom, for staff to accept or ignore.
    """
    back = f"/partner/listings/{listing_id}"
    listing = (await db.execute(
        select(PartnerListing).where(PartnerListing.id == listing_id)
    )).scalar_one_or_none()
    seg = dealer.price_segment or "new_dealer"
    if (not listing or not listing.is_active or listing.status != "published"
            or listing.visible_to_segment not in ("all", seg)):
        return RedirectResponse(url="/partner/catalog?error=Listing+not+available",
                                status_code=302)

    amt = _parse_amount(amount)
    if amt is None:
        return RedirectResponse(url=f"{back}?error=Enter+a+valid+amount",
                                status_code=302)

    bid = PartnerBid(
        bid_number=await _next_bid_number(db),
        listing_id=listing.id, dealer_id=dealer.id,
        bid_amount=amt, bid_type="custom",
        base_amount=listing.dealer_price, status="active",
        notes=notes.strip() or None,
    )
    db.add(bid)
    await audit(db, action="PARTNER_CUSTOM_PRICE_REQUESTED",
                table_name="partner_bids", record_id=str(bid.id),
                new_value={"listing": listing.listing_code, "amount": str(amt),
                           "dealer": dealer.business_name},
                notes=f"dealer:{dealer.business_name}", request=request)
    await db.commit()
    return RedirectResponse(url=f"{back}?success=Custom+price+request+sent",
                            status_code=302)


async def _visible_lot_or_none(db: AsyncSession, lot_id: str, dealer_id):
    """The Lot, but only if this dealer may act on it.

    Open lots are available to everyone. A Restricted lot is limited to the
    dealers granted it — re-checked here, not just hidden in the catalog, so a
    dealer who learns a restricted lot's id still cannot bid or book on it.
    """
    try:
        lot = (await db.execute(
            select(Lot).where(Lot.id == lot_id)
        )).scalar_one_or_none()
    except Exception:
        return None
    if not lot:
        return None
    if not getattr(lot, "is_restricted", False):
        return lot
    granted = (await db.execute(select(LotDealerVisibility).where(
        LotDealerVisibility.lot_id == lot.id,
        LotDealerVisibility.dealer_id == dealer_id,
    ))).scalar_one_or_none()
    return lot if granted else None


async def _below_base_bid_used(db: AsyncSession, lot_id, dealer_id) -> bool:
    """Has this dealer already used its single below-base-price bid on this lot?

    A partner may bid under the base price once. Above or at base, there is no
    limit — that is the normal competitive ladder.
    """
    for b in (await db.execute(select(PartnerBid).where(
        PartnerBid.lot_id == lot_id, PartnerBid.dealer_id == dealer_id,
        PartnerBid.status.in_(("active", "won", "lost")),
    ))).scalars().all():
        if b.base_amount and Decimal(str(b.bid_amount)) < Decimal(str(b.base_amount)):
            return True
    return False


@router.get("/lots/{lot_id}/models")
async def lot_model_summary(
    lot_id: str,
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    """Model-wise breakdown of a lot's stock, for the catalog's Available Stock
    modal. Specs only — no tag numbers, no costs, no internal stage."""
    lot = await _visible_lot_or_none(db, lot_id, dealer.id)
    if not lot:
        return JSONResponse({"error": "Lot not available to you"}, status_code=404)

    rows = (await db.execute(
        select(
            Device.model, Device.cpu, Device.generation,
            Device.ram_gb, Device.storage_gb, Device.storage_type, Device.grade,
            func.count(Device.id).label("qty"),
        )
        .where(Device.lot_id == lot.id, Device.is_active == True)  # noqa: E712
        .group_by(Device.model, Device.cpu, Device.generation, Device.ram_gb,
                  Device.storage_gb, Device.storage_type, Device.grade)
        .order_by(func.count(Device.id).desc())
    )).all()

    def _grade(g):
        # grade is an enum on Device; .value where present, else the raw string.
        return getattr(g, "value", g) if g else "—"

    models = [{
        "model": r.model or "—",
        "qty": r.qty,
        "cpu": r.cpu or "—",
        "generation": r.generation or "—",
        "ram": f"{r.ram_gb} GB" if r.ram_gb else "—",
        "storage": (f"{r.storage_gb} GB {r.storage_type or ''}".strip()
                    if r.storage_gb else "—"),
        "grade": _grade(r.grade),
    } for r in rows]

    return JSONResponse({
        "lot_number": lot.lot_number,
        "total": sum(m["qty"] for m in models),
        "models": models,
    })


@router.post("/lots/{lot_id}/bid")
async def place_lot_bid(
    request: Request,
    lot_id: str,
    amount: str = Form(...),
    _csrf=Depends(verify_partner_csrf),
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    """Auction bid against a visible Lot. Same 10% rule as listing bids."""
    back = "/partner/catalog"

    # Lock the lot row for the duration, exactly as the listing path does: two
    # dealers submitting together would otherwise both read the same highest
    # bid and both be recorded valid at the same amount.
    lot = (await db.execute(
        select(Lot).where(Lot.id == lot_id).with_for_update()
    )).scalar_one_or_none()
    if not lot or not await _visible_lot_or_none(db, lot_id, dealer.id):
        await db.rollback()
        return RedirectResponse(url=f"{back}?error=Lot+not+available+to+you",
                                status_code=302)

    # One bid per lot per dealer, general or custom. Checked under the row lock
    # taken above, so two simultaneous submits cannot both get through.
    existing = await _existing_lot_bid(db, lot.id, dealer.id)
    if existing:
        await db.rollback()
        return RedirectResponse(
            url=f"{back}?error=You+have+already+bid+on+{quote_plus(lot.lot_number)}"
                f"+({quote_plus(existing.bid_number)}).+Only+one+bid+per+lot+is+allowed",
            status_code=302)

    amt = _parse_amount(amount)
    if amt is None:
        await db.rollback()
        return RedirectResponse(url=f"{back}?error=Enter+a+valid+bid+amount",
                                status_code=302)

    top = (await db.execute(
        select(func.max(PartnerBid.bid_amount)).where(
            PartnerBid.lot_id == lot.id, PartnerBid.status == "active")
    )).scalar()
    base = lot.buying_price
    if base or top:
        floor = min_next_bid(base or 0, top)
        if amt < floor:
            await db.rollback()
            return RedirectResponse(
                url=f"{back}?error=Minimum+next+bid+is+Rs+{floor:.0f}",
                status_code=302)

    bid = PartnerBid(
        bid_number=await _next_bid_number(db),
        lot_id=lot.id, dealer_id=dealer.id,
        bid_amount=amt, bid_type="increment",
        previous_amount=top, base_amount=base,
        status="active",
    )
    db.add(bid)
    await audit(db, action="PARTNER_BID_PLACED", table_name="partner_bids",
                record_id=str(bid.id),
                new_value={"lot": lot.lot_number, "amount": str(amt),
                           "previous": str(top) if top is not None else None,
                           "dealer": dealer.business_name},
                notes=f"dealer:{dealer.business_name}", request=request)
    await db.commit()
    return RedirectResponse(url=f"{back}?success=Bid+placed+on+{lot.lot_number}",
                            status_code=302)


@router.post("/lots/{lot_id}/custom-price")
async def request_lot_custom_price(
    request: Request,
    lot_id: str,
    amount: str = Form(...),
    notes: str = Form(""),
    _csrf=Depends(verify_partner_csrf),
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    """Dealer names their own figure for a Lot — not held to the 10% ladder."""
    back = "/partner/catalog"
    lot = await _visible_lot_or_none(db, lot_id, dealer.id)
    if not lot:
        return RedirectResponse(url=f"{back}?error=Lot+not+available+to+you",
                                status_code=302)

    # One bid per lot per dealer, general or custom. This supersedes the older
    # one-below-base-offer rule, which only ever constrained custom bids.
    existing = await _existing_lot_bid(db, lot.id, dealer.id)
    if existing:
        return RedirectResponse(
            url=f"{back}?error=You+have+already+bid+on+{quote_plus(lot.lot_number)}"
                f"+({quote_plus(existing.bid_number)}).+Only+one+bid+per+lot+is+allowed",
            status_code=302)

    amt = _parse_amount(amount)
    if amt is None:
        return RedirectResponse(url=f"{back}?error=Enter+a+valid+amount",
                                status_code=302)

    bid = PartnerBid(
        bid_number=await _next_bid_number(db),
        lot_id=lot.id, dealer_id=dealer.id,
        bid_amount=amt, bid_type="custom",
        base_amount=lot.buying_price, status="active",
        notes=notes.strip() or None,
    )
    db.add(bid)
    await audit(db, action="PARTNER_CUSTOM_PRICE_REQUESTED",
                table_name="partner_bids", record_id=str(bid.id),
                new_value={"lot": lot.lot_number, "amount": str(amt),
                           "dealer": dealer.business_name},
                notes=f"dealer:{dealer.business_name}", request=request)
    await db.commit()
    return RedirectResponse(url=f"{back}?success=Custom+price+request+sent",
                            status_code=302)


@router.post("/lots/{lot_id}/book")
async def book_lot(
    request: Request,
    lot_id: str,
    qty: int = Form(1),
    _csrf=Depends(verify_partner_csrf),
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    """Catalog page 'Book' button (item 28) — only for Lots this dealer has
    been granted visibility on. Raises a lightweight LotBookingRequest, not a
    priced PartnerBooking (Lots aren't priced/tokened like PartnerListings)."""
    visible = (await db.execute(
        select(LotDealerVisibility).where(
            LotDealerVisibility.lot_id == lot_id, LotDealerVisibility.dealer_id == dealer.id,
        )
    )).scalar_one_or_none()
    if not visible:
        return RedirectResponse(url="/partner/catalog?error=Lot+not+available+to+you", status_code=302)

    existing = (await db.execute(
        select(LotBookingRequest).where(
            LotBookingRequest.lot_id == lot_id, LotBookingRequest.dealer_id == dealer.id,
            LotBookingRequest.status != "withdrawn",
        )
    )).scalar_one_or_none()
    if existing:
        return RedirectResponse(url="/partner/catalog?error=Already+booked+this+lot", status_code=302)

    booking = LotBookingRequest(lot_id=lot_id, dealer_id=dealer.id, qty_requested=max(1, qty))
    db.add(booking)
    await audit(db, action="LOT_BOOKING_REQUESTED", table_name="lot_booking_requests",
                record_id=str(booking.id), new_value={"lot_id": lot_id, "dealer": dealer.business_name, "qty": qty},
                notes=f"dealer:{dealer.business_name}", request=request)
    await db.commit()
    return RedirectResponse(url="/partner/catalog?success=Lot+booking+requested", status_code=302)


@router.post("/lots/{lot_id}/withdraw")
async def withdraw_lot_booking(
    request: Request,
    lot_id: str,
    _csrf=Depends(verify_partner_csrf),
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    """Catalog page 'Withdraw' button (item 28)."""
    booking = (await db.execute(
        select(LotBookingRequest).where(
            LotBookingRequest.lot_id == lot_id, LotBookingRequest.dealer_id == dealer.id,
            LotBookingRequest.status != "withdrawn",
        )
    )).scalar_one_or_none()
    if not booking:
        return RedirectResponse(url="/partner/catalog?error=No+active+booking+to+withdraw", status_code=302)
    booking.status = "withdrawn"
    await audit(db, action="LOT_BOOKING_WITHDRAWN", table_name="lot_booking_requests",
                record_id=str(booking.id), notes=f"dealer:{dealer.business_name}", request=request)
    await db.commit()
    return RedirectResponse(url="/partner/catalog?success=Booking+withdrawn", status_code=302)


@router.post("/listings/{listing_id}/book")
async def book_listing(
    request: Request,
    listing_id: str,
    qty: int = Form(...),
    _csrf=Depends(verify_partner_csrf),
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    await expire_stale_bookings(db)
    try:
        booking = await create_booking(db, dealer, listing_id, qty)
    except BookingError as e:
        await db.rollback()
        return RedirectResponse(
            url=f"/partner/listings/{listing_id}?error={quote(str(e))}", status_code=302)
    await audit(db, action="PARTNER_BOOKING_CREATED",
                table_name="partner_bookings", record_id=str(booking.id),
                new_value={"booking": booking.booking_number, "dealer": dealer.dealer_code,
                           "qty": qty, "token_total": str(booking.token_total)},
                notes=f"dealer:{dealer.business_name}", request=request)
    await db.commit()
    return RedirectResponse(url=f"/partner/bookings/{booking.id}", status_code=302)


@router.get("/bookings", response_class=HTMLResponse)
async def my_bookings(
    request: Request,
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    await expire_stale_bookings(db)
    await db.commit()
    bookings = (await db.execute(
        select(PartnerBooking).where(PartnerBooking.dealer_id == dealer.id)
        .order_by(PartnerBooking.created_at.desc())
    )).scalars().all()
    listings = {}
    for b in bookings:
        if str(b.listing_id) not in listings:
            listings[str(b.listing_id)] = await db.get(PartnerListing, b.listing_id)

    # Lot Booking Requests (item 28) — shown alongside the priced-listing
    # bookings above, kept as a separate lightweight section since they're
    # not part of the token-payment flow.
    lot_bookings = (await db.execute(
        select(LotBookingRequest).where(
            LotBookingRequest.dealer_id == dealer.id, LotBookingRequest.status != "withdrawn",
        ).order_by(LotBookingRequest.created_at.desc())
    )).scalars().all()
    lots_by_id = {}
    for lb in lot_bookings:
        if str(lb.lot_id) not in lots_by_id:
            lots_by_id[str(lb.lot_id)] = await db.get(Lot, lb.lot_id)

    return templates.TemplateResponse("partner/bookings.html", {
        "request": request, "dealer": dealer, "bookings": bookings,
        "listings": listings,
        "lot_bookings": lot_bookings, "lots_by_id": lots_by_id,
        "partner_csrf": request.cookies.get(PARTNER_CSRF_COOKIE, ""),
    })


@router.get("/bids", response_class=HTMLResponse)
async def my_bids_analysis(
    request: Request,
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    """Bids Analysis — this dealer's own settled bids, won and lost.

    Live bids are deliberately absent: the catalog already shows those inline
    with a leading/outbid badge, and duplicating them here would give a dealer
    two places to read the same number and no way to tell which is fresher.

    Strictly scoped to dealer.id. A dealer must never see another dealer's
    bid history — that is the competing side of the same auction.
    """
    bids = (await db.execute(
        select(PartnerBid).where(
            PartnerBid.dealer_id == dealer.id,
            PartnerBid.status.in_(("won", "lost")),
        ).order_by(PartnerBid.bid_amount.desc())
    )).scalars().all()

    lot_ids = {b.lot_id for b in bids if b.lot_id}
    lots = {}
    if lot_ids:
        lots = {l.id: l for l in (await db.execute(
            select(Lot).where(Lot.id.in_(lot_ids)))).scalars().all()}
    listing_ids = {b.listing_id for b in bids if b.listing_id}
    listings = {}
    if listing_ids:
        listings = {l.id: l for l in (await db.execute(
            select(PartnerListing).where(
                PartnerListing.id.in_(listing_ids)))).scalars().all()}

    def _row(b):
        lot = lots.get(b.lot_id) if b.lot_id else None
        listing = listings.get(b.listing_id) if b.listing_id else None
        base = Decimal(str(b.base_amount)) if b.base_amount else None
        diff = pct = None
        if base and base > 0:
            diff = Decimal(str(b.bid_amount)) - base
            pct = (diff / base * 100).quantize(Decimal("0.1"))
        return {
            "bid": b,
            # No lot link: /lots/<id> is a staff route. Dealers get the number
            # for reference only.
            "target": (lot.lot_number if lot
                       else (listing.title if listing else "—")),
            "base_amount": base, "diff": diff, "diff_pct": pct,
        }

    won = [_row(b) for b in bids if b.status == "won"]
    lost = [_row(b) for b in bids if b.status == "lost"]
    settled = len(won) + len(lost)
    stats = {
        "won": len(won), "lost": len(lost), "settled": settled,
        "win_rate": round(len(won) / settled * 100) if settled else None,
        "won_value": sum((Decimal(str(r["bid"].bid_amount)) for r in won),
                         Decimal("0")),
    }

    # Won rows carry the payment leg: the derived status, any attached PO for
    # download, and whatever payment the dealer has already submitted.
    from models.partner import PartnerBidDocument
    from models.partner_payment import (PartnerBidPayment, payment_status,
                                        PAYMENT_STATUS_BADGE)
    won_ids = [r["bid"].id for r in won]
    docs, pays = {}, {}
    if won_ids:
        for d in (await db.execute(
            select(PartnerBidDocument).where(PartnerBidDocument.bid_id.in_(won_ids))
        )).scalars().all():
            docs.setdefault(str(d.bid_id), []).append(d)
        for p in (await db.execute(
            select(PartnerBidPayment).where(PartnerBidPayment.bid_id.in_(won_ids))
            .order_by(PartnerBidPayment.submitted_at.desc())
        )).scalars().all():
            pays.setdefault(str(p.bid_id), p)
    for r in won:
        bid_key = str(r["bid"].id)
        r["documents"] = docs.get(bid_key, [])
        r["has_po"] = any(d.doc_type == "po" for d in r["documents"])
        r["payment"] = pays.get(bid_key)
        r["payment_status"] = payment_status(r["has_po"], r["payment"])
        r["payment_badge"] = PAYMENT_STATUS_BADGE.get(r["payment_status"], "bg-secondary")

    return templates.TemplateResponse("partner/bids.html", {
        "request": request, "dealer": dealer,
        "won": won, "lost": lost, "stats": stats,
        "partner_csrf": request.cookies.get(PARTNER_CSRF_COOKIE, ""),
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.get("/bids/{bid_id}/document/{doc_id}")
async def partner_bid_document(
    bid_id: str, doc_id: str,
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    """Download a document (typically the PO) attached to the dealer's own bid."""
    from fastapi.responses import FileResponse
    from models.partner import PartnerBidDocument
    from config import UPLOADS_DIR
    try:
        bid = (await db.execute(select(PartnerBid).where(
            PartnerBid.id == uuid_mod.UUID(bid_id),
            PartnerBid.dealer_id == dealer.id,      # scoped: never another dealer's
        ))).scalar_one_or_none()
        doc = (await db.execute(select(PartnerBidDocument).where(
            PartnerBidDocument.id == uuid_mod.UUID(doc_id)
        ))).scalar_one_or_none()
    except (ValueError, AttributeError):
        raise HTTPException(404)
    if not bid or not doc or str(doc.bid_id) != str(bid.id):
        raise HTTPException(404, "Document not found")
    path = os.path.join(UPLOADS_DIR, "partner", "bids", doc.filename)
    if not os.path.exists(path):
        raise HTTPException(404, "File missing")
    return FileResponse(path, filename=doc.original_name or doc.filename,
                        media_type="application/pdf")


@router.post("/bids/{bid_id}/payment")
async def submit_bid_payment(
    request: Request, bid_id: str,
    payment_date: str = Form(""),
    payment_mode: str = Form("bank_transfer"),
    payment_utr: str = Form(""),
    payment_amount: str = Form(""),
    _csrf=Depends(verify_partner_csrf),
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    """Mark Paid — the dealer submits payment details against a won bid.

    This moves the bid to "Payment Verifying" on both this page and the staff
    side; finance confirms it on Partner Payments to reach "Payment Done".
    """
    from models.partner_payment import PartnerBidPayment
    back = "/partner/bids"
    try:
        bid = (await db.execute(select(PartnerBid).where(
            PartnerBid.id == uuid_mod.UUID(bid_id),
            PartnerBid.dealer_id == dealer.id,
            PartnerBid.status == "won",
        ))).scalar_one_or_none()
    except (ValueError, AttributeError):
        raise HTTPException(404)
    if not bid:
        return RedirectResponse(url=f"{back}?error=Bid+not+found", status_code=302)

    existing = (await db.execute(
        select(PartnerBidPayment).where(PartnerBidPayment.bid_id == bid.id)
        .order_by(PartnerBidPayment.submitted_at.desc())
    )).scalars().first()
    if existing and existing.verified:
        return RedirectResponse(
            url=f"{back}?error=This+payment+has+already+been+verified", status_code=302)

    amt = _parse_amount(payment_amount)
    pdate = None
    if payment_date:
        try:
            pdate = datetime.strptime(payment_date, "%Y-%m-%d")
        except ValueError:
            pdate = None
    mode = payment_mode if payment_mode in ("bank_transfer", "upi") else "bank_transfer"

    # Re-submitting before verification updates in place rather than stacking
    # rows, so finance always reviews one current set of details.
    target = existing or PartnerBidPayment(bid_id=bid.id)
    target.payment_date = pdate
    target.payment_mode = mode
    target.payment_utr = (payment_utr or "").strip()[:100] or None
    target.payment_amount = amt
    target.submitted_by = dealer.business_name
    target.submitted_at = app_now()
    if existing is None:
        db.add(target)
    await audit(db, action="PARTNER_PAYMENT_SUBMITTED",
                table_name="partner_bid_payments", record_id=str(bid.id),
                new_value={"bid": bid.bid_number, "utr": target.payment_utr,
                           "amount": str(amt) if amt is not None else None},
                notes=f"dealer:{dealer.business_name}", request=request)
    await db.commit()
    return RedirectResponse(
        url=f"{back}?success=Payment+details+submitted+for+{bid.bid_number}", status_code=302)


@router.get("/bookings/{booking_id}", response_class=HTMLResponse)
async def booking_detail(
    request: Request,
    booking_id: str,
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    await expire_stale_bookings(db)
    await db.commit()
    from sqlalchemy.orm import selectinload
    booking = (await db.execute(
        select(PartnerBooking)
        .options(selectinload(PartnerBooking.proofs))
        .where(
            PartnerBooking.id == booking_id,
            PartnerBooking.dealer_id == dealer.id,
        )
    )).scalar_one_or_none()
    if not booking:
        return RedirectResponse(url="/partner/bookings", status_code=302)
    listing = await db.get(PartnerListing, booking.listing_id)
    settings = await get_settings(db)
    owner_wa = await _sales_owner_wa(db, dealer)
    ask_text = quote(f"Hi, regarding my booking {booking.booking_number} ({listing.title if listing else ''})")
    return templates.TemplateResponse("partner/booking_detail.html", {
        "request": request, "dealer": dealer, "booking": booking,
        "listing": listing, "settings": settings,
        "owner_wa": owner_wa, "ask_text": ask_text,
        "proofs": booking.proofs if booking else [],
        "error": request.query_params.get("error"),
        "success": request.query_params.get("success"),
        "partner_csrf": request.cookies.get(PARTNER_CSRF_COOKIE, ""),
    })


@router.post("/bookings/{booking_id}/proof")
async def upload_proof(
    request: Request,
    booking_id: str,
    proof_type: str = Form("token"),
    utr_reference: str = Form(...),
    amount_claimed: str = Form(""),
    screenshot: UploadFile = File(...),
    _csrf=Depends(verify_partner_csrf),
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    booking = (await db.execute(
        select(PartnerBooking).where(
            PartnerBooking.id == booking_id,
            PartnerBooking.dealer_id == dealer.id,
        )
    )).scalar_one_or_none()
    if not booking:
        return RedirectResponse(url="/partner/bookings", status_code=302)

    proof_type = "balance" if proof_type == "balance" else "token"
    if proof_type == "token" and booking.status != "pending_payment":
        return RedirectResponse(
            url=f"/partner/bookings/{booking.id}?error={quote('Token proof already submitted or booking is closed.')}",
            status_code=302)
    if proof_type == "balance" and booking.status not in ("confirmed_token", "balance_pending"):
        return RedirectResponse(
            url=f"/partner/bookings/{booking.id}?error={quote('Balance proof can be uploaded after the token is confirmed.')}",
            status_code=302)

    from services.partner_service import save_proof_file
    safe_name = await save_proof_file(screenshot)
    if not safe_name:
        return RedirectResponse(
            url=f"/partner/bookings/{booking.id}?error={quote('Upload a jpg/png/pdf up to 5 MB.')}",
            status_code=302)

    from decimal import Decimal, InvalidOperation
    try:
        amount = Decimal(str(amount_claimed).replace(",", "").strip()) if amount_claimed.strip() else None
    except InvalidOperation:
        amount = None

    proof = PartnerPaymentProof(
        booking_id=booking.id, proof_type=proof_type,
        utr_reference=utr_reference.strip()[:100],
        amount_claimed=amount, screenshot_path=safe_name,
    )
    db.add(proof)
    # Rule 6: proof upload freezes the countdown / advances the chain
    if proof_type == "token":
        booking.status = "proof_uploaded"
    else:
        booking.status = "balance_pending"
    await db.flush()
    await audit(db, action="PARTNER_PROOF_UPLOADED",
                table_name="partner_payment_proofs", record_id=str(proof.id),
                new_value={"booking": booking.booking_number, "type": proof_type,
                           "utr": proof.utr_reference,
                           "amount": str(amount) if amount is not None else None},
                notes=f"dealer:{dealer.business_name}", request=request)
    await db.commit()
    return RedirectResponse(
        url=f"/partner/bookings/{booking.id}?success={quote('Proof submitted — verification usually takes a few business hours.')}",
        status_code=302)


@router.get("/bookings/{booking_id}/proof-file/{proof_id}")
async def proof_file(
    booking_id: str,
    proof_id: str,
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    """Authed proof serving — only the owning dealer (staff use the admin route)."""
    from fastapi.responses import FileResponse
    from services.partner_service import PROOFS_DIR
    proof = (await db.execute(
        select(PartnerPaymentProof)
        .join(PartnerBooking, PartnerBooking.id == PartnerPaymentProof.booking_id)
        .where(
            PartnerPaymentProof.id == proof_id,
            PartnerPaymentProof.booking_id == booking_id,
            PartnerBooking.dealer_id == dealer.id,
        )
    )).scalar_one_or_none()
    if not proof or not proof.screenshot_path:
        return RedirectResponse(url="/partner/bookings", status_code=302)
    path = os.path.join(PROOFS_DIR, os.path.basename(proof.screenshot_path))
    if not os.path.exists(path):
        return RedirectResponse(url="/partner/bookings", status_code=302)
    return FileResponse(path)


@router.post("/bookings/{booking_id}/cancel")
async def cancel_booking(
    request: Request,
    booking_id: str,
    _csrf=Depends(verify_partner_csrf),
    dealer: Dealer = Depends(get_current_partner),
    db: AsyncSession = Depends(get_db),
):
    booking = (await db.execute(
        select(PartnerBooking).where(
            PartnerBooking.id == booking_id,
            PartnerBooking.dealer_id == dealer.id,
        )
    )).scalar_one_or_none()
    if not booking or booking.status not in ("pending_payment", "proof_uploaded"):
        return RedirectResponse(url="/partner/bookings", status_code=302)
    from services.partner_service import restore_booking_qty
    booking.status = "cancelled"
    await restore_booking_qty(db, booking)
    await audit(db, action="PARTNER_BOOKING_CANCELLED",
                table_name="partner_bookings", record_id=str(booking.id),
                new_value={"booking": booking.booking_number, "by": "dealer"},
                notes=f"dealer:{dealer.business_name}", request=request)
    await db.commit()
    return RedirectResponse(url="/partner/bookings", status_code=302)


@router.get("/profile", response_class=HTMLResponse)
async def profile(request: Request, dealer: Dealer = Depends(get_current_partner)):
    return templates.TemplateResponse("partner/profile.html", {
        "request": request, "dealer": dealer,
        "partner_csrf": request.cookies.get(PARTNER_CSRF_COOKIE, ""),
    })
