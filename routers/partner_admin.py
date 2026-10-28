"""External Partner — internal admin screens (/trade-partner).

Staff-facing: partner account provisioning, listings manager, bookings queue,
settings, floors, My Desk. Guarded by staff JWT + a page-specific
trade_partner_* module permission (split 2026-09-22 from one shared
"trade_partner" key so each page can be enabled per role independently — all
default disabled for every role, see migrate_seed_trade_partner_split_perms.py).
Payment verification stays finance/admin-gated.
"""
import json
import os
import secrets
import string
import uuid as uuid_mod
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func

from database import get_db
from templates_config import templates
from models.dealers import Dealer
from models.crm import CRMContact
from models.user import User, UserRole
from models.lot import Lot
from models.device import Device, DeviceStage, DeviceGrade, STAGE_LABELS, StageMovement
from models.grn_import import GRNImport
from models.master import (
    EXTERNAL_PARTNER_TEST_ENTITY, EXTERNAL_PARTNER_TEST_LOT_PREFIX,
    EXTERNAL_PARTNER_TEST_GRN_SOURCE,
)
from models.partner import (
    PartnerLoginLog, PartnerListing, PartnerListingDevice, PartnerFloorConfig,
    PartnerBooking, PRICE_SEGMENTS, LISTING_TYPES,
    LotDealerVisibility, LotBookingRequest,
    PartnerBid, PartnerBidDocument, BID_DOC_TYPES,
)
from models.partner_payment import (
    PartnerBidPayment, payment_status, PAYMENT_STATUS_BADGE,
)
from models.lot import LotLineItem
from models.company import Company
from models.terms import TermsCondition
from models.crm import CRMSalesOpportunity
from auth.dependencies import (
    get_current_user, verify_csrf, hash_password_async, require_module_perm,
)
from services.audit_engine import audit
from services.location_defaults import ensure_stage_location
from services.partner_service import (
    next_listing_code, resolve_floor, ageing_bucket, get_settings, set_setting,
    photos_list, SETTING_DEFAULTS,
)
from auth.partner_auth import normalize_phone
from utils.dealer_code import next_dealer_code
from utils.timezone import app_now
from config import UPLOADS_DIR

router = APIRouter(prefix="/trade-partner", tags=["trade-partner-admin"])

PARTNER_TYPES = ["dealer", "trader", "repair_shop"]


def _gen_temp_password(length: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def _sales_users(db: AsyncSession):
    r = await db.execute(
        select(User.username, User.full_name).where(User.status == True)  # noqa: E712
        .order_by(User.username)
    )
    return r.all()


async def _account_candidates(db: AsyncSession):
    """Accounts (CRM Contact Leads) offered in the Enable Portal Access picker.

    Deliberately NOT filtered down to accounts that have no portal login yet.
    Working out whether an account already has one means resolving it through
    the dealer bridge (link, then phone, then name), and a near-miss there would
    silently hide a real account from the operator with no way to tell why.
    Instead every live account is listed and provisioning skips the ones that
    already have access, reporting each by name.
    """
    r = await db.execute(
        select(CRMContact)
        .where(CRMContact.status == "active", CRMContact.is_trashed == False)  # noqa: E712
        .order_by(CRMContact.company_name)
    )
    return r.scalars().all()


async def _dealer_for_account(db: AsyncSession, contact: CRMContact,
                              username: str) -> tuple[Dealer, bool]:
    """Resolve the Dealer row that backs a CRM Account, creating it if needed.

    Returns (dealer, created). Match order is most-trustworthy first:
      1. an explicit crm_contact_id link from a previous provisioning
      2. normalised phone — the portal login *is* a phone, so a collision here
         would break login for whichever dealer lost the race
      3. business name, case-insensitively

    Only live (untrashed) dealers are considered: binding a portal login to a
    trashed dealer would give the partner an account nobody can see or service.
    """
    if contact.id:
        d = (await db.execute(select(Dealer).where(
            Dealer.crm_contact_id == contact.id,
            Dealer.trashed_at.is_(None),
        ))).scalars().first()
        if d:
            return d, False

    phone = normalize_phone(contact.phone or "")
    if phone:
        d = (await db.execute(select(Dealer).where(
            Dealer.phone == phone, Dealer.trashed_at.is_(None),
        ))).scalars().first()
        if d:
            return d, False

    name = (contact.company_name or "").strip()
    if name:
        d = (await db.execute(select(Dealer).where(
            func.lower(Dealer.business_name) == name.lower(),
            Dealer.trashed_at.is_(None),
        ))).scalars().first()
        if d:
            return d, False

    dealer = Dealer(
        # From MAX(code), not count(*): production has 915 dealers numbered up
        # to 0916, so count(*)+1 returned an existing code and the UNIQUE index
        # on dealer_code made this whole request a 500. See utils/dealer_code.
        dealer_code=await next_dealer_code(db),
        business_name=name or (phone or "Unknown Account"),
        contact_person=contact.contact_person,
        phone=phone or None,
        whatsapp_number=normalize_phone(contact.whatsapp or "") or None,
        email=contact.email,
        address=contact.address,
        city=contact.city,
        state=contact.state,
        pincode=contact.pincode,
        gstin=contact.gstin,
        dealer_type="retail",
        assigned_to=contact.assigned_to,
        created_by=username,
        added_by=username,
        source="External Partner: provisioned from CRM Account",
    )
    db.add(dealer)
    await db.flush()   # need dealer.id before the caller writes portal fields
    return dealer, True


async def _render_partners(
    request, current_user, db, q="",
    location="", segment="", sales_owner="",
    provisioned=None, failed=None, success=None, error=None,
):
    """Render the External Partner Accounts page.

    Shared by the GET list view and by POST handlers that must show a
    one-time temp-password banner (enable / reset). Rendering directly —
    rather than redirecting with the password in the query string — keeps
    the secret out of browser history and proxy logs.
    """
    query = select(Dealer).where(Dealer.portal_enabled == True)  # noqa: E712
    if q:
        like = f"%{q}%"
        query = query.where(
            (Dealer.business_name.ilike(like))
            | (Dealer.portal_phone.ilike(like))
            | (Dealer.dealer_code.ilike(like))
        )
    if location:
        query = query.where(Dealer.city == location)
    if segment:
        query = query.where(Dealer.price_segment == segment)
    if sales_owner:
        query = query.where(Dealer.sales_owner_username == sales_owner)
    result = await db.execute(query.order_by(Dealer.business_name))
    partners = result.scalars().all()

    from services.partner_service import compute_dealer_scores
    scores = await compute_dealer_scores(db, [p.id for p in partners])

    # Location filter options — every city among portal-enabled dealers
    # (not scoped to the current filtered result, so the dropdown doesn't
    # shrink as other filters narrow the table).
    location_options = (await db.execute(
        select(Dealer.city).where(
            Dealer.portal_enabled == True,  # noqa: E712
            Dealer.city.isnot(None), Dealer.city != "",
        ).distinct().order_by(Dealer.city)
    )).scalars().all()

    sales_users = await _sales_users(db)
    # Sales Owner column shows the person's name, not the raw username stored
    # in Dealer.sales_owner_username (see the Enable/Edit modals' own comment
    # on why the FORM VALUE stays the username).
    full_name_by_username = {u: (n or u) for u, n in sales_users}

    return templates.TemplateResponse("trade_partner/partners.html", {
        "request": request, "current_user": current_user,
        "partners": partners, "accounts": await _account_candidates(db),
        "q": q, "scores": scores,
        "sales_users": sales_users, "full_name_by_username": full_name_by_username,
        "partner_types": PARTNER_TYPES, "price_segments": PRICE_SEGMENTS,
        "location_options": location_options,
        "f_location": location, "f_segment": segment, "f_sales_owner": sales_owner,
        "provisioned": provisioned, "failed": failed,
        "success": success, "error": error,
    })


@router.get("/partners", response_class=HTMLResponse)
async def partners_list(
    request: Request,
    q: str = "",
    location: str = "",
    segment: str = "",
    sales_owner: str = "",
    current_user: User = Depends(require_module_perm("trade_partner_partners")),
    db: AsyncSession = Depends(get_db),
):
    return await _render_partners(
        request, current_user, db, q=q,
        location=location, segment=segment, sales_owner=sales_owner,
        success=request.query_params.get("success"),
        error=request.query_params.get("error"),
    )


@router.post("/partners/enable")
async def enable_partner(
    request: Request,
    contact_ids: list[str] = Form(default=[]),
    portal_phone: str = Form(""),
    partner_type: str = Form("dealer"),
    price_segment: str = Form("new_dealer"),
    sales_owner_username: str = Form(""),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_partners", "add")),
    db: AsyncSession = Depends(get_db),
):
    """Enable portal access for one or more CRM Accounts — one login each.

    Each selected Account is resolved (or bridged) to its Dealer row, because
    every portal foreign key in the system points at dealers.id. See
    _dealer_for_account for the matching order.

    Login phone: with a single Account selected the operator may type an
    override; with several, each Account's own phone is used, since there is no
    sane way to hand-enter N numbers in one modal. Accounts without a usable
    10-digit number are skipped and named in the result, never silently dropped.

    One bad Account does not abort the batch — the others are still provisioned.

    Renders the page directly rather than redirecting: temp passwords must be
    shown once, and the previous redirect put the password in the query string,
    where it landed in browser history, the access log and any proxy in between.
    """
    if partner_type not in PARTNER_TYPES:
        partner_type = "dealer"
    if price_segment not in PRICE_SEGMENTS:
        price_segment = "new_dealer"
    owner = sales_owner_username.strip() or None

    ids = [c for c in (contact_ids or []) if c and c.strip()]
    if not ids:
        return RedirectResponse(
            url="/trade-partner/partners?error=Select+at+least+one+account",
            status_code=302)

    contacts = (await db.execute(
        select(CRMContact).where(CRMContact.id.in_(ids))
    )).scalars().all()
    by_id = {str(c.id): c for c in contacts}

    override = normalize_phone(portal_phone) if len(ids) == 1 else ""
    provisioned, failed = [], []

    # Phones claimed earlier in THIS batch: two accounts sharing a number would
    # both pass the DB check (neither is committed yet) and then collide on the
    # unique index, failing the whole request.
    claimed: set[str] = set()

    for cid in ids:
        contact = by_id.get(cid)
        if contact is None:
            failed.append({"name": cid, "reason": "Account not found"})
            continue
        label = contact.company_name or contact.contact_code

        norm = override or normalize_phone(contact.phone or "")
        if len(norm) != 10:
            failed.append({"name": label, "reason":
                           "No valid 10-digit mobile on the account"})
            continue
        if norm in claimed:
            failed.append({"name": label, "reason":
                           "Another account in this batch uses the same mobile"})
            continue

        dealer, created = await _dealer_for_account(db, contact, current_user.username)
        if dealer.portal_enabled:
            failed.append({"name": label, "reason":
                           f"Already has portal access ({dealer.portal_phone})"})
            continue
        dup = (await db.execute(select(Dealer).where(
            Dealer.portal_phone == norm, Dealer.id != dealer.id
        ))).scalars().first()
        if dup:
            failed.append({"name": label, "reason":
                           f"{norm} is already the login for {dup.business_name}"})
            continue

        temp_password = _gen_temp_password()
        dealer.crm_contact_id = contact.id
        dealer.portal_enabled = True
        dealer.portal_phone = norm
        dealer.portal_password_hash = await hash_password_async(temp_password)
        dealer.partner_type = partner_type
        dealer.price_segment = price_segment
        dealer.sales_owner_username = owner
        dealer.portal_password_version = (dealer.portal_password_version or 1) + 1
        claimed.add(norm)

        await audit(db, action="PARTNER_PORTAL_ENABLED", user=current_user,
                    table_name="dealers", record_id=str(dealer.id),
                    new_value={"portal_phone": norm, "partner_type": partner_type,
                               "price_segment": price_segment,
                               "sales_owner": owner,
                               "crm_contact_id": str(contact.id),
                               "dealer_created": created},
                    request=request)
        provisioned.append({
            "name": label, "dealer_code": dealer.dealer_code,
            "phone": norm, "password": temp_password,
            "dealer_created": created,
        })

    if provisioned:
        await db.commit()
    else:
        # Nothing to keep, and _dealer_for_account may have flushed new Dealer
        # rows for accounts that then failed validation. Rolling back stops
        # those half-built dealers from being left behind with no portal login.
        await db.rollback()

    return await _render_partners(
        request, current_user, db,
        provisioned=provisioned, failed=failed,
        success=(f"Portal access enabled for {len(provisioned)} account(s)"
                 if provisioned else None),
        error=(None if provisioned else "No portal access was enabled"),
    )


@router.post("/partners/{dealer_id}/update")
async def update_partner(
    request: Request,
    dealer_id: str,
    partner_type: str = Form("dealer"),
    price_segment: str = Form("new_dealer"),
    sales_owner_username: str = Form(""),
    portal_phone: str = Form(""),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_partners", "edit")),
    db: AsyncSession = Depends(get_db),
):
    dealer = (await db.execute(select(Dealer).where(Dealer.id == dealer_id))).scalar_one_or_none()
    if not dealer:
        raise HTTPException(status_code=404, detail="Dealer not found")

    new_phone = dealer.portal_phone
    if portal_phone.strip():
        norm = normalize_phone(portal_phone)
        if len(norm) != 10:
            return RedirectResponse(
                url="/trade-partner/partners?error=Enter+a+valid+10-digit+login+mobile+number",
                status_code=302)
        dup = (await db.execute(
            select(Dealer).where(Dealer.portal_phone == norm, Dealer.id != dealer.id)
        )).scalar_one_or_none()
        if dup:
            return RedirectResponse(
                url="/trade-partner/partners?error=That+phone+is+already+a+portal+login",
                status_code=302)
        new_phone = norm

    old = {"partner_type": dealer.partner_type, "price_segment": dealer.price_segment,
           "sales_owner": dealer.sales_owner_username, "portal_phone": dealer.portal_phone}
    dealer.partner_type = partner_type if partner_type in PARTNER_TYPES else dealer.partner_type
    dealer.price_segment = price_segment if price_segment in PRICE_SEGMENTS else dealer.price_segment
    dealer.sales_owner_username = sales_owner_username.strip() or None
    dealer.portal_phone = new_phone
    await audit(db, action="PARTNER_PORTAL_UPDATED", user=current_user,
                table_name="dealers", record_id=str(dealer.id),
                old_value=old,
                new_value={"partner_type": dealer.partner_type,
                           "price_segment": dealer.price_segment,
                           "sales_owner": dealer.sales_owner_username,
                           "portal_phone": dealer.portal_phone},
                request=request)
    await db.commit()
    return RedirectResponse(url="/trade-partner/partners?success=Partner+updated", status_code=302)


@router.post("/partners/{dealer_id}/toggle")
async def toggle_partner(
    request: Request,
    dealer_id: str,
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_partners", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Disable/re-enable portal access (does NOT touch the dealer master record)."""
    dealer = (await db.execute(select(Dealer).where(Dealer.id == dealer_id))).scalar_one_or_none()
    if not dealer:
        raise HTTPException(status_code=404, detail="Dealer not found")
    dealer.portal_enabled = not dealer.portal_enabled
    # Kill live sessions on disable
    dealer.portal_password_version = (dealer.portal_password_version or 1) + 1
    await audit(db, action="PARTNER_PORTAL_TOGGLED", user=current_user,
                table_name="dealers", record_id=str(dealer.id),
                new_value={"portal_enabled": dealer.portal_enabled}, request=request)
    await db.commit()
    state = "enabled" if dealer.portal_enabled else "disabled"
    return RedirectResponse(url=f"/trade-partner/partners?success=Portal+{state}", status_code=302)


@router.post("/partners/{dealer_id}/reset-password")
async def reset_partner_password(
    request: Request,
    dealer_id: str,
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_partners", "edit")),
    db: AsyncSession = Depends(get_db),
):
    dealer = (await db.execute(select(Dealer).where(Dealer.id == dealer_id))).scalar_one_or_none()
    if not dealer:
        raise HTTPException(status_code=404, detail="Dealer not found")
    temp_password = _gen_temp_password()
    dealer.portal_password_hash = await hash_password_async(temp_password)
    dealer.portal_password_version = (dealer.portal_password_version or 1) + 1  # invalidates old JWTs
    await audit(db, action="PARTNER_PASSWORD_RESET", user=current_user,
                table_name="dealers", record_id=str(dealer.id), request=request)
    await db.commit()
    # Render directly with the one-time banner (like the enable flow). The temp
    # password must never go into the URL — a redirect would leak it into
    # browser history and proxy logs.
    return await _render_partners(
        request, current_user, db,
        provisioned=[{
            "name": dealer.business_name,
            "dealer_code": dealer.dealer_code,
            "phone": dealer.portal_phone or "",
            "password": temp_password,
            "dealer_created": False,
        }],
        success=f"Password reset for {dealer.business_name}",
    )


@router.post("/partners/{dealer_id}/delete")
async def delete_partner(
    request: Request,
    dealer_id: str,
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_partners", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete a partner record.

    A partner is a Dealer row (see routers/dealers.py) — reuses that router's
    cascade/financial-guard logic so this and /dealers/{id}/delete never drift:
    a partner holding financial records (orders, credit notes, quotations,
    receipts) is trashed instead of hard-deleted, and the response says which
    happened."""
    from routers.dealers import _partition_deletable, _hard_delete_dealers

    dealer = (await db.execute(select(Dealer).where(Dealer.id == dealer_id))).scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/trade-partner/partners?error=Partner+not+found", status_code=302)

    name = dealer.business_name or str(dealer.id)
    deletable, blocked = await _partition_deletable(db, [str(dealer.id)])

    if not deletable:
        if dealer.trashed_at is None:
            dealer.trashed_at = app_now()
            dealer.trashed_by = current_user.username
        reason = blocked.get(str(dealer.id), "financial records")
        await audit(db, action="PARTNER_TRASHED_HAS_FINANCIALS", user=current_user,
                    table_name="dealers", record_id=str(dealer.id),
                    new_value={"business_name": name, "retained_because": reason},
                    request=request)
        await db.commit()
        msg = quote_plus(
            f"{name} has {reason} and was archived instead of deleted — "
            f"financial records must be retained.")
        return RedirectResponse(url=f"/trade-partner/partners?warning={msg}", status_code=302)

    await audit(db, action="PARTNER_DELETED", user=current_user,
                table_name="dealers", record_id=str(dealer.id),
                old_value={"business_name": name, "phone": dealer.phone,
                           "dealer_code": dealer.dealer_code},
                new_value={"deleted": True, "by": current_user.username},
                request=request)
    await _hard_delete_dealers(db, deletable)
    await db.commit()
    msg = quote_plus(f"{name} deleted permanently")
    return RedirectResponse(url=f"/trade-partner/partners?success={msg}", status_code=302)


# ═══════════════════════════════════════════════════════════════════════════
# Listings Manager
# ═══════════════════════════════════════════════════════════════════════════

def _parse_decimal(raw, default=None):
    try:
        return Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, AttributeError, TypeError):
        return default


def _is_floor_approver(user: User) -> bool:
    """Who may publish below floor. Admin-only in MVP — the module-permission
    matrix defaults to permissive when unconfigured, which would silently let
    every role override the margin gate, so we keep this restrictive."""
    role_name = user.role.value if hasattr(user.role, "value") else str(user.role)
    return role_name == "admin"


async def _save_listing_photos(files) -> list:
    """Listing photos are dealer-visible marketing content — served via the
    public /uploads mount (uploads/partner/photos/). Payment proofs are NOT
    stored here; they live outside uploads/ and are served via an authed route."""
    saved = []
    uploads_dir = os.path.join(UPLOADS_DIR, "partner", "photos")
    os.makedirs(uploads_dir, exist_ok=True)
    for f in files or []:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        safe_name = f"{uuid_mod.uuid4().hex}{ext}"
        content = await f.read()
        if not content or len(content) > 5 * 1024 * 1024:
            continue
        with open(os.path.join(uploads_dir, safe_name), "wb") as out:
            out.write(content)
        saved.append(safe_name)
    return saved


@router.get("/listings", response_class=HTMLResponse)
async def listings_manager(
    request: Request,
    status: str = "",
    listing_type: str = "",
    current_user: User = Depends(require_module_perm("trade_partner_listings")),
    db: AsyncSession = Depends(get_db),
):
    query = select(PartnerListing).where(PartnerListing.is_active == True)  # noqa: E712
    if status:
        query = query.where(PartnerListing.status == status)
    if listing_type:
        query = query.where(PartnerListing.listing_type == listing_type)
    listings = (await db.execute(query.order_by(PartnerListing.created_at.desc()))).scalars().all()

    ageing = {str(l.id): ageing_bucket(l.stock_intake_date or l.created_at) for l in listings}
    # Ageing summary card: count + ₹ value (dealer_price × qty_available) per bucket
    summary = {}
    for l in listings:
        if l.status not in ("published", "paused", "draft"):
            continue
        b = ageing[str(l.id)]["tag"]
        s = summary.setdefault(b, {"count": 0, "value": Decimal("0")})
        s["count"] += 1
        s["value"] += (l.dealer_price or 0) * (l.qty_available or 0)

    stale_cutoff = app_now() - timedelta(hours=48)
    stale_ids = {
        str(l.id) for l in listings
        if l.status == "published" and (l.price_reviewed_at or l.created_at) < stale_cutoff
    }

    lots = (await db.execute(
        select(Lot).where(Lot.is_trashed == False)  # noqa: E712
        .order_by(Lot.created_at.desc()).limit(300)
    )).scalars().all()

    settings = await get_settings(db)
    return templates.TemplateResponse("trade_partner/listings.html", {
        "request": request, "current_user": current_user,
        "listings": listings, "ageing": ageing, "summary": summary,
        "stale_ids": stale_ids, "lots": lots,
        "listing_types": LISTING_TYPES, "segments": ["all"] + PRICE_SEGMENTS,
        "settings": settings,
        "f_status": status, "f_type": listing_type,
        "is_floor_approver": _is_floor_approver(current_user),
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/listings/create")
async def create_listing(
    request: Request,
    listing_type: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    lot_id: str = Form(""),
    fg_brand: str = Form(""),
    fg_grade: str = Form(""),
    fg_qty: int = Form(0),
    qty_total: int = Form(0),
    moq: int = Form(1),
    dealer_price: str = Form(...),
    token_mode: str = Form("pct"),      # pct | flat
    token_value: str = Form(""),
    hold_hours: int = Form(0),
    visible_to_segment: str = Form("all"),
    photos: list[UploadFile] = File(default=[]),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_listings", "add")),
    db: AsyncSession = Depends(get_db),
):
    if listing_type not in LISTING_TYPES:
        return RedirectResponse(url="/trade-partner/listings?error=Invalid+listing+type", status_code=302)
    price = _parse_decimal(dealer_price)
    if price is None or price <= 0:
        return RedirectResponse(url="/trade-partner/listings?error=Enter+a+valid+dealer+price", status_code=302)

    settings = await get_settings(db)
    if hold_hours <= 0:
        hold_hours = int(settings.get("default_hold_hours") or 24)

    # Token per unit — server-side resolution only
    tval = _parse_decimal(token_value)
    if token_mode == "flat" and tval and tval > 0:
        token_per_unit = tval
    else:
        pct = tval if (tval and tval > 0) else _parse_decimal(settings.get("default_token_pct"), Decimal("10"))
        token_per_unit = (price * pct / Decimal("100")).quantize(Decimal("0.01"))

    cost_basis = None
    stock_intake = None
    device_ids = []

    if listing_type == "finished_goods":
        # Server picks N oldest ready devices matching brand/grade, not already listed
        if fg_qty <= 0:
            return RedirectResponse(url="/trade-partner/listings?error=Enter+quantity+of+ready+devices", status_code=302)
        dq = select(Device).where(
            Device.current_stage == DeviceStage.ready_to_sale,
            Device.is_active == True,   # noqa: E712
            Device.is_trashed == False, # noqa: E712
            Device.partner_listed == False,  # noqa: E712
        )
        if fg_brand:
            dq = dq.where(Device.brand.ilike(fg_brand))
        if fg_grade:
            dq = dq.where(Device.grade == fg_grade)
        devices = (await db.execute(dq.order_by(Device.created_at.asc()).limit(fg_qty))).scalars().all()
        if len(devices) < fg_qty:
            return RedirectResponse(
                url=f"/trade-partner/listings?error=Only+{len(devices)}+matching+ready+devices+available",
                status_code=302)
        device_ids = [d.id for d in devices]
        prices = [d.device_price for d in devices if d.device_price]
        cost_basis = (sum(prices) / len(prices)).quantize(Decimal("0.01")) if prices else None
        stock_intake = min((d.created_at for d in devices if d.created_at), default=None)
        qty_total = fg_qty
    else:
        if not lot_id:
            return RedirectResponse(url="/trade-partner/listings?error=Select+a+source+lot", status_code=302)
        lot = (await db.execute(select(Lot).where(Lot.id == lot_id))).scalar_one_or_none()
        if not lot:
            return RedirectResponse(url="/trade-partner/listings?error=Lot+not+found", status_code=302)
        if qty_total <= 0:
            qty_total = lot.qty or 0
        if qty_total <= 0:
            return RedirectResponse(url="/trade-partner/listings?error=Enter+quantity", status_code=302)
        if lot.buying_price and lot.qty:
            cost_basis = (Decimal(lot.buying_price) / Decimal(lot.qty)).quantize(Decimal("0.01"))
        stock_intake = lot.grn_date or lot.purchase_date or lot.created_at

    saved_photos = await _save_listing_photos(photos)

    listing = PartnerListing(
        listing_code=await next_listing_code(db),
        listing_type=listing_type,
        lot_id=lot_id or None,
        title=title.strip(),
        description=description.strip() or None,
        brand=fg_brand.strip() or None,
        grade_summary=fg_grade.strip() or None,
        qty_total=qty_total,
        qty_available=qty_total,
        moq=max(moq, 1),
        dealer_price=price,
        token_amount=token_per_unit,
        hold_hours=hold_hours,
        photos=json.dumps(saved_photos) if saved_photos else None,
        status="draft",
        cost_basis=cost_basis,
        visible_to_segment=visible_to_segment if visible_to_segment in (["all"] + PRICE_SEGMENTS) else "all",
        stock_intake_date=stock_intake,
        created_by=current_user.username,
    )
    db.add(listing)
    await db.flush()

    for did in device_ids:
        db.add(PartnerListingDevice(listing_id=listing.id, device_id=did))
    if device_ids:
        await db.execute(update(Device).where(Device.id.in_(device_ids)).values(partner_listed=True))

    await audit(db, action="PARTNER_LISTING_CREATED", user=current_user,
                table_name="partner_listings", record_id=str(listing.id),
                new_value={"code": listing.listing_code, "type": listing_type,
                           "qty": qty_total, "price": str(price),
                           "token_per_unit": str(token_per_unit)},
                request=request)
    await db.commit()
    return RedirectResponse(
        url=f"/trade-partner/listings?success=Listing+{listing.listing_code}+created+as+draft",
        status_code=302)


@router.post("/listings/{listing_id}/publish")
async def publish_listing(
    request: Request,
    listing_id: str,
    floor_override_reason: str = Form(""),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_listings", "edit")),
    db: AsyncSession = Depends(get_db),
):
    listing = (await db.execute(select(PartnerListing).where(PartnerListing.id == listing_id))).scalar_one_or_none()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    if listing.status not in ("draft", "paused"):
        return RedirectResponse(url="/trade-partner/listings?error=Only+draft%2Fpaused+listings+can+be+published", status_code=302)

    # ── Margin guardrail (rule 11): hard publish gate ──
    floor = await resolve_floor(db, listing.listing_type, listing.cost_basis)
    listing.floor_value = floor
    if floor is not None and Decimal(listing.dealer_price) < floor:
        if not _is_floor_approver(current_user):
            return RedirectResponse(
                url="/trade-partner/listings?error=Below+floor+—+needs+admin%2Ffinance+approval",
                status_code=302)
        if not floor_override_reason.strip():
            return RedirectResponse(
                url="/trade-partner/listings?error=Floor+override+requires+a+reason",
                status_code=302)
        listing.floor_override_by = current_user.username
        listing.floor_override_reason = floor_override_reason.strip()
        await audit(db, action="PARTNER_FLOOR_OVERRIDE", user=current_user,
                    table_name="partner_listings", record_id=str(listing.id),
                    new_value={"code": listing.listing_code,
                               "price": str(listing.dealer_price),
                               "floor": str(floor),
                               "reason": listing.floor_override_reason},
                    request=request)

    listing.status = "published"
    listing.price_reviewed_at = app_now()
    await audit(db, action="PARTNER_LISTING_PUBLISHED", user=current_user,
                table_name="partner_listings", record_id=str(listing.id),
                new_value={"code": listing.listing_code}, request=request)
    await db.commit()
    return RedirectResponse(url=f"/trade-partner/listings?success={listing.listing_code}+published", status_code=302)


@router.post("/listings/{listing_id}/pause")
async def pause_listing(
    request: Request,
    listing_id: str,
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_listings", "edit")),
    db: AsyncSession = Depends(get_db),
):
    listing = (await db.execute(select(PartnerListing).where(PartnerListing.id == listing_id))).scalar_one_or_none()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing.status = "paused" if listing.status == "published" else listing.status
    await audit(db, action="PARTNER_LISTING_PAUSED", user=current_user,
                table_name="partner_listings", record_id=str(listing.id),
                new_value={"code": listing.listing_code}, request=request)
    await db.commit()
    return RedirectResponse(url=f"/trade-partner/listings?success={listing.listing_code}+paused", status_code=302)


@router.post("/listings/{listing_id}/reprice")
async def reprice_listing(
    request: Request,
    listing_id: str,
    dealer_price: str = Form(""),
    moq: int = Form(0),
    token_value: str = Form(""),
    hold_hours: int = Form(0),
    confirm_only: str = Form(""),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_listings", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Reprice/edit a listing, or just re-confirm the price (48h review rule).
    Existing bookings keep their snapshots — future bookings only."""
    listing = (await db.execute(select(PartnerListing).where(PartnerListing.id == listing_id))).scalar_one_or_none()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")

    old = {"price": str(listing.dealer_price), "moq": listing.moq,
           "token": str(listing.token_amount), "hold_hours": listing.hold_hours}

    if not confirm_only:
        price = _parse_decimal(dealer_price)
        if price and price > 0:
            # Repricing below floor on a published listing hits the same gate
            floor = await resolve_floor(db, listing.listing_type, listing.cost_basis)
            if (floor is not None and price < floor
                    and listing.status == "published"
                    and not _is_floor_approver(current_user)):
                return RedirectResponse(
                    url="/trade-partner/listings?error=Below+floor+—+needs+admin%2Ffinance+approval",
                    status_code=302)
            listing.dealer_price = price
        tval = _parse_decimal(token_value)
        if tval and tval > 0:
            listing.token_amount = tval
        if moq > 0:
            listing.moq = moq
        if hold_hours > 0:
            listing.hold_hours = hold_hours

    listing.price_reviewed_at = app_now()
    await audit(db, action="PARTNER_LISTING_REPRICED" if not confirm_only else "PARTNER_PRICE_CONFIRMED",
                user=current_user, table_name="partner_listings",
                record_id=str(listing.id), old_value=old,
                new_value={"price": str(listing.dealer_price), "moq": listing.moq,
                           "token": str(listing.token_amount), "hold_hours": listing.hold_hours},
                request=request)
    await db.commit()
    return RedirectResponse(url=f"/trade-partner/listings?success={listing.listing_code}+updated", status_code=302)


# ═══════════════════════════════════════════════════════════════════════════
# Bookings queue + balance chain (rule 12)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/bookings", response_class=HTMLResponse)
async def bookings_queue(
    request: Request,
    status: str = "",
    q: str = "",
    current_user: User = Depends(require_module_perm("trade_partner_bookings")),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    from services.partner_service import expire_stale_bookings, compute_dealer_scores
    await expire_stale_bookings(db)
    await db.commit()

    query = (select(PartnerBooking)
             .options(selectinload(PartnerBooking.proofs),
                      selectinload(PartnerBooking.listing),
                      selectinload(PartnerBooking.dealer))
             .order_by(PartnerBooking.created_at.desc()))
    if status:
        query = query.where(PartnerBooking.status == status)
    bookings = (await db.execute(query.limit(500))).scalars().all()
    if q:
        ql = q.lower()
        bookings = [b for b in bookings
                    if ql in (b.booking_number or "").lower()
                    or (b.dealer and ql in (b.dealer.business_name or "").lower())
                    or (b.listing and ql in (b.listing.title or "").lower())]

    # ── Lot bookings from the catalog ───────────────────────────────────────
    # Booking a Lot raises a LotBookingRequest, not a PartnerBooking (the latter
    # is priced against PartnerListing fields a Lot does not have). They were
    # therefore invisible on this queue — a dealer could book a lot and nobody
    # would ever see it. Listed separately because the two carry different
    # fields, not merged into a shape that fits neither.
    lot_bookings = []
    lot_reqs = (await db.execute(
        select(LotBookingRequest)
        .where(LotBookingRequest.status != "withdrawn")
        .order_by(LotBookingRequest.created_at.desc()).limit(500)
    )).scalars().all()
    if lot_reqs:
        lb_lots = {l.id: l for l in (await db.execute(select(Lot).where(
            Lot.id.in_({r.lot_id for r in lot_reqs})))).scalars().all()}
        lb_dealers = {d.id: d for d in (await db.execute(select(Dealer).where(
            Dealer.id.in_({r.dealer_id for r in lot_reqs})))).scalars().all()}
        for r in lot_reqs:
            lot = lb_lots.get(r.lot_id)
            dealer = lb_dealers.get(r.dealer_id)
            if q:
                hay = f"{lot.lot_number if lot else ''} {dealer.business_name if dealer else ''}".lower()
                if q.lower() not in hay:
                    continue
            lot_bookings.append({
                "req": r,
                "lot_id": str(r.lot_id),
                "lot_number": lot.lot_number if lot else "—",
                "dealer_name": dealer.business_name if dealer else "—",
                "dealer_phone": (dealer.portal_phone or dealer.phone) if dealer else None,
            })

    scores = await compute_dealer_scores(db, [b.dealer_id for b in bookings])
    settings = await get_settings(db)
    base_url = str(request.base_url).rstrip("/")
    return templates.TemplateResponse("trade_partner/bookings.html", {
        "request": request, "current_user": current_user,
        "bookings": bookings, "lot_bookings": lot_bookings,
        "scores": scores, "settings": settings,
        "base_url": base_url, "f_status": status, "q": q, "now_ts": app_now(),
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


async def _get_booking(db, booking_id) -> PartnerBooking:
    from sqlalchemy.orm import selectinload
    booking = (await db.execute(
        select(PartnerBooking)
        .options(selectinload(PartnerBooking.proofs),
                 selectinload(PartnerBooking.dealer),
                 selectinload(PartnerBooking.listing))
        .where(PartnerBooking.id == booking_id)
    )).scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


def _pending_proof(booking: PartnerBooking, proof_type: str):
    pending = [p for p in booking.proofs
               if p.proof_type == proof_type and p.status == "pending"]
    return pending[-1] if pending else None


@router.post("/bookings/{booking_id}/verify")
async def verify_token_proof(
    request: Request,
    booking_id: str,
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_bookings", "upload")),
    db: AsyncSession = Depends(get_db),
):
    """Verify the token proof → booking confirmed_token. Finance-separated via
    the 'upload' action bit on the trade_partner_bookings module (sales get add/edit only)."""
    booking = await _get_booking(db, booking_id)
    if booking.status != "proof_uploaded":
        return RedirectResponse(url="/trade-partner/bookings?error=Booking+is+not+awaiting+token+verification", status_code=302)
    proof = _pending_proof(booking, "token")
    if proof:
        proof.status = "verified"
        proof.verified_by = current_user.username
        proof.verified_at = app_now()
    booking.status = "confirmed_token"
    booking.confirmed_by = current_user.username
    await audit(db, action="PARTNER_TOKEN_VERIFIED", user=current_user,
                table_name="partner_bookings", record_id=str(booking.id),
                new_value={"booking": booking.booking_number,
                           "token_total": str(booking.token_total)},
                request=request)
    await db.commit()
    return RedirectResponse(url=f"/trade-partner/bookings?success={booking.booking_number}+token+confirmed", status_code=302)


@router.post("/bookings/{booking_id}/reject")
async def reject_booking(
    request: Request,
    booking_id: str,
    rejection_reason: str = Form(...),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_bookings", "upload")),
    db: AsyncSession = Depends(get_db),
):
    from services.partner_service import restore_booking_qty
    booking = await _get_booking(db, booking_id)
    if booking.status not in ("pending_payment", "proof_uploaded"):
        return RedirectResponse(url="/trade-partner/bookings?error=Only+open+token+bookings+can+be+rejected", status_code=302)
    if not rejection_reason.strip():
        return RedirectResponse(url="/trade-partner/bookings?error=Rejection+requires+a+reason", status_code=302)
    for p in booking.proofs:
        if p.status == "pending":
            p.status = "rejected"
            p.verified_by = current_user.username
            p.verified_at = app_now()
    booking.status = "rejected"
    booking.rejection_reason = rejection_reason.strip()
    await restore_booking_qty(db, booking)
    await audit(db, action="PARTNER_BOOKING_REJECTED", user=current_user,
                table_name="partner_bookings", record_id=str(booking.id),
                new_value={"booking": booking.booking_number,
                           "reason": booking.rejection_reason},
                request=request)
    await db.commit()
    return RedirectResponse(url=f"/trade-partner/bookings?success={booking.booking_number}+rejected+and+stock+released", status_code=302)


@router.post("/bookings/{booking_id}/balance-verify")
async def verify_balance(
    request: Request,
    booking_id: str,
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_bookings", "upload")),
    db: AsyncSession = Depends(get_db),
):
    """balance_pending (proof) or confirmed_token (offline payment, audited) → ready_for_dispatch."""
    booking = await _get_booking(db, booking_id)
    if booking.status not in ("balance_pending", "confirmed_token"):
        return RedirectResponse(url="/trade-partner/bookings?error=Booking+is+not+awaiting+balance", status_code=302)
    proof = _pending_proof(booking, "balance")
    if proof:
        proof.status = "verified"
        proof.verified_by = current_user.username
        proof.verified_at = app_now()
    booking.status = "ready_for_dispatch"
    booking.balance_verified_by = current_user.username
    await audit(db, action="PARTNER_BALANCE_VERIFIED", user=current_user,
                table_name="partner_bookings", record_id=str(booking.id),
                new_value={"booking": booking.booking_number,
                           "offline_no_proof": proof is None},
                request=request)
    await db.commit()
    return RedirectResponse(url=f"/trade-partner/bookings?success={booking.booking_number}+ready+for+dispatch", status_code=302)


@router.post("/bookings/{booking_id}/dispatched")
async def mark_dispatched(
    request: Request,
    booking_id: str,
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_bookings", "edit")),
    db: AsyncSession = Depends(get_db),
):
    booking = await _get_booking(db, booking_id)
    if booking.status != "ready_for_dispatch":
        return RedirectResponse(url="/trade-partner/bookings?error=Booking+is+not+ready+for+dispatch", status_code=302)
    booking.status = "dispatched"
    booking.dispatched_at = app_now()
    await audit(db, action="PARTNER_BOOKING_DISPATCHED", user=current_user,
                table_name="partner_bookings", record_id=str(booking.id),
                new_value={"booking": booking.booking_number}, request=request)
    await db.commit()
    return RedirectResponse(url=f"/trade-partner/bookings?success={booking.booking_number}+dispatched", status_code=302)


@router.post("/bookings/{booking_id}/extend")
async def extend_booking(
    request: Request,
    booking_id: str,
    extra_hours: int = Form(24),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_bookings", "edit")),
    db: AsyncSession = Depends(get_db),
):
    booking = await _get_booking(db, booking_id)
    if booking.status != "pending_payment":
        return RedirectResponse(url="/trade-partner/bookings?error=Only+pending+bookings+can+be+extended", status_code=302)
    extra = min(max(extra_hours, 1), 168)
    booking.expires_at = booking.expires_at + timedelta(hours=extra)
    await audit(db, action="PARTNER_BOOKING_EXTENDED", user=current_user,
                table_name="partner_bookings", record_id=str(booking.id),
                new_value={"booking": booking.booking_number, "extra_hours": extra,
                           "new_expiry": booking.expires_at.isoformat()},
                request=request)
    await db.commit()
    return RedirectResponse(url=f"/trade-partner/bookings?success={booking.booking_number}+extended+{extra}h", status_code=302)


@router.get("/proof-file/{proof_id}")
async def staff_proof_file(
    proof_id: str,
    current_user: User = Depends(require_module_perm("trade_partner_bookings")),
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import FileResponse
    from models.partner import PartnerPaymentProof
    from services.partner_service import PROOFS_DIR
    proof = (await db.execute(
        select(PartnerPaymentProof).where(PartnerPaymentProof.id == proof_id)
    )).scalar_one_or_none()
    if not proof or not proof.screenshot_path:
        raise HTTPException(status_code=404, detail="Proof not found")
    path = os.path.join(PROOFS_DIR, os.path.basename(proof.screenshot_path))
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Proof file missing")
    return FileResponse(path)


# ═══════════════════════════════════════════════════════════════════════════
# My Desk — sales owner dashboard (section 19)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/my-desk", response_class=HTMLResponse)
async def my_desk(
    request: Request,
    current_user: User = Depends(require_module_perm("trade_partner_my_desk")),
    db: AsyncSession = Depends(get_db),
):
    """Everything the logged-in sales owner needs to run dealer activation:
    assigned dealers, pending proofs, expiring holds, expired-needing-a-call,
    never-logged-in dealers, and stale-price listings. Admin sees everything."""
    from sqlalchemy.orm import selectinload
    from services.partner_service import expire_stale_bookings, compute_dealer_scores
    await expire_stale_bookings(db)
    await db.commit()

    role_name = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    is_admin = role_name == "admin"

    dq = select(Dealer).where(Dealer.portal_enabled == True)  # noqa: E712
    if not is_admin:
        dq = dq.where(Dealer.sales_owner_username == current_user.username)
    dealers = (await db.execute(dq.order_by(Dealer.business_name))).scalars().all()
    dealer_ids = [d.id for d in dealers]
    scores = await compute_dealer_scores(db, dealer_ids)

    bookings = []
    if dealer_ids:
        bookings = (await db.execute(
            select(PartnerBooking)
            .options(selectinload(PartnerBooking.dealer), selectinload(PartnerBooking.listing))
            .where(PartnerBooking.dealer_id.in_(dealer_ids))
            .order_by(PartnerBooking.created_at.desc()).limit(300)
        )).scalars().all()

    now = app_now()
    soon = now + timedelta(hours=6)
    pending_proofs = [b for b in bookings if b.status == "proof_uploaded"]
    balance_waiting = [b for b in bookings if b.status in ("confirmed_token", "balance_pending")]
    expiring_soon = [b for b in bookings if b.status == "pending_payment" and b.expires_at <= soon]
    recently_expired = [b for b in bookings if b.status == "expired"][:10]
    never_logged_in = [d for d in dealers if not d.portal_last_login_at]

    # Stale-price queue: published listings not reviewed in 48h
    stale_cutoff = now - timedelta(hours=48)
    stale_listings = (await db.execute(
        select(PartnerListing).where(
            PartnerListing.status == "published",
            PartnerListing.is_active == True,  # noqa: E712
        ).order_by(PartnerListing.price_reviewed_at.asc().nulls_first())
    )).scalars().all()
    stale_listings = [l for l in stale_listings
                      if (l.price_reviewed_at or l.created_at) < stale_cutoff]

    settings = await get_settings(db)
    base_url = str(request.base_url).rstrip("/")
    return templates.TemplateResponse("trade_partner/my_desk.html", {
        "request": request, "current_user": current_user, "is_admin": is_admin,
        "dealers": dealers, "scores": scores,
        "pending_proofs": pending_proofs, "balance_waiting": balance_waiting,
        "expiring_soon": expiring_soon, "recently_expired": recently_expired,
        "never_logged_in": never_logged_in, "stale_listings": stale_listings,
        "settings": settings, "base_url": base_url,
    })


# ═══════════════════════════════════════════════════════════════════════════
# Floor config (admin/finance only, versioned)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/floors", response_class=HTMLResponse)
async def floors_page(
    request: Request,
    current_user: User = Depends(require_module_perm("trade_partner_floors")),
    db: AsyncSession = Depends(get_db),
):
    if not _is_floor_approver(current_user):
        raise HTTPException(status_code=403, detail="Floor config is admin/finance only")
    rows = (await db.execute(
        select(PartnerFloorConfig).order_by(
            PartnerFloorConfig.listing_type, PartnerFloorConfig.effective_from.desc())
    )).scalars().all()
    return templates.TemplateResponse("trade_partner/floors.html", {
        "request": request, "current_user": current_user, "rows": rows,
        "listing_types": LISTING_TYPES,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/floors/create")
async def create_floor(
    request: Request,
    listing_type: str = Form(...),
    floor_rule_type: str = Form(...),
    floor_pct: str = Form(""),
    floor_value: str = Form(""),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_floors")),
    db: AsyncSession = Depends(get_db),
):
    if not _is_floor_approver(current_user):
        raise HTTPException(status_code=403, detail="Floor config is admin/finance only")
    if listing_type not in LISTING_TYPES:
        raise HTTPException(status_code=400, detail="Invalid listing type")
    pct = _parse_decimal(floor_pct)
    val = _parse_decimal(floor_value)
    if pct is None and val is None:
        return RedirectResponse(url="/trade-partner/floors?error=Enter+a+floor+%25+or+value", status_code=302)
    row = PartnerFloorConfig(
        listing_type=listing_type, floor_rule_type=floor_rule_type.strip()[:30],
        floor_pct=pct, floor_value=val, effective_from=app_now(),
        created_by=current_user.username,
    )
    db.add(row)
    await db.flush()
    await audit(db, action="PARTNER_FLOOR_CONFIG_ADDED", user=current_user,
                table_name="partner_floor_config", record_id=str(row.id),
                new_value={"listing_type": listing_type, "pct": str(pct), "value": str(val)},
                request=request)
    await db.commit()
    return RedirectResponse(url="/trade-partner/floors?success=Floor+version+added", status_code=302)


# ═══════════════════════════════════════════════════════════════════════════
# Settings
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    current_user: User = Depends(require_module_perm("trade_partner_settings")),
    db: AsyncSession = Depends(get_db),
):
    settings = await get_settings(db)
    return templates.TemplateResponse("trade_partner/settings.html", {
        "request": request, "current_user": current_user, "settings": settings,
        "success": request.query_params.get("success"),
    })


@router.post("/settings")
async def settings_save(
    request: Request,
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_settings", "edit")),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    changed = {}
    for key in SETTING_DEFAULTS:
        if key in form:
            await set_setting(db, key, str(form.get(key, "")).strip(), current_user.username)
            changed[key] = str(form.get(key, ""))[:80]
    await audit(db, action="PARTNER_SETTINGS_UPDATED", user=current_user,
                table_name="partner_settings", new_value=changed, request=request)
    await db.commit()
    return RedirectResponse(url="/trade-partner/settings?success=Settings+saved", status_code=302)


# ── Manage Lots (item 27) — test-data intake hub for the External Partner
# portal (2026-09-22). Previously a generic "restrict any live Lot, grant
# visibility to any real dealer" tool; replaced because that let admin-
# created test Lots leak into the live dealer catalog by default (Lot.is_
# restricted defaults to open-to-everyone). Rebuilt again same day: an
# earlier version of this page only LINKED OUT to the real GRN/Bulk-Upload
# pages — the actual asks was to EMBED the real GRN & Lot CRUD and the real
# Bulk Upload IQC form directly here, as two tabs, so the flow never leaves
# this page. Test GRNs are tagged via GRNImport.source =
# EXTERNAL_PARTNER_TEST_GRN_SOURCE (reusing the existing column the live GRN
# pages already filter on — see models/master.py's comment on that constant)
# so they're automatically excluded from /grn and /grn/post-iqc with no
# separate exclusion query needed on those pages beyond the one added to
# grn_import_list. Test Lots stay on the EXTERNAL_PARTNER_TEST_LOT_PREFIX
# convention and auto-restrict on creation (routers/grn.py grn_add_lot), so
# they're never open to the live dealer catalog and no visibility-grant UI
# is needed here.
# ─────────────────────────────────────────────────────────────────────────

@router.get("/manage-lots", response_class=HTMLResponse)
async def manage_lots(
    request: Request,
    current_user: User = Depends(require_module_perm("trade_partner_manage_lots")),
    db: AsyncSession = Depends(get_db),
):
    from routers.grn import _stocked_map

    # ── Tab 1: GRN & Lot ──────────────────────────────────────────────────
    test_grns = (await db.execute(
        select(GRNImport).where(GRNImport.source == EXTERNAL_PARTNER_TEST_GRN_SOURCE,
                                GRNImport.is_deleted == False)
        .order_by(GRNImport.created_at.desc())
    )).scalars().all()

    grn_numbers = [g.grn_number for g in test_grns if g.grn_number]
    lots_by_grn: dict = {}
    if grn_numbers:
        lot_rows = (await db.execute(
            select(Lot).where(Lot.grn_system_number.in_(grn_numbers)).order_by(Lot.lot_number)
        )).scalars().all()
        for l in lot_rows:
            lots_by_grn.setdefault(l.grn_system_number, []).append(l)

    stocked = await _stocked_map(db, test_grns)

    # Per-lot mapped-device count, for the Lot Number(s) popup — a breakdown
    # of the aggregate Stocked figure above by which specific Lot each mapped
    # device actually belongs to (Device.lot_id), not just "any lot under
    # this GRN". "Mapped" = grn_number is set at all, same definition the
    # Asset IQC tab's own Mapped/Unmapped badge uses.
    all_lot_ids = [l.id for lots in lots_by_grn.values() for l in lots]
    lot_mapped_counts: dict = {}
    if all_lot_ids:
        mapped_rows = (await db.execute(
            select(Device.lot_id, func.count(Device.id))
            .where(Device.lot_id.in_(all_lot_ids),
                   Device.grn_number.isnot(None), Device.grn_number != "",
                   Device.is_active == True, Device.is_trashed == False)
            .group_by(Device.lot_id)
        )).all()
        lot_mapped_counts = {str(lid): c for lid, c in mapped_rows}

    # JSON-serializable form for the "N Lots" popup — id + lot_number + mapped
    # count only (Lot itself isn't JSON-safe to dump into a data attribute).
    lots_json = {
        grn_number: [
            {"id": str(l.id), "lot_number": l.lot_number,
             "mapped": lot_mapped_counts.get(str(l.id), 0)}
            for l in lots
        ]
        for grn_number, lots in lots_by_grn.items()
    }

    # Reverse of lots_by_grn — which GRN (if any) a given Lot Number is
    # mapped to, for the Asset IQC tab's GRN column. A Lot Number could in
    # theory be listed under more than one GRN row; last-write-wins here is
    # fine since Add/Edit Lot itself only ever lets one GRN claim a given
    # Lot Number at a time (create-or-merge, not multi-assign).
    lot_number_to_grn = {}
    for grn_number, lots in lots_by_grn.items():
        for l in lots:
            lot_number_to_grn[l.lot_number] = grn_number

    # ── Tab 2: Asset IQC ─────────────────────────────────────────────────
    device_rows = (await db.execute(
        select(Device, Lot.lot_number)
        .join(Lot, Device.lot_id == Lot.id, isouter=True)
        .where(Device.entity == EXTERNAL_PARTNER_TEST_ENTITY)
        .order_by(Device.created_at.desc())
    )).all()
    test_devices = [{"device": d, "lot_number": ln,
                     "matched_grn": lot_number_to_grn.get(ln)} for d, ln in device_rows]

    total_lots = sum(len(lots) for lots in lots_by_grn.values())

    return templates.TemplateResponse("trade_partner/manage_lots.html", {
        "request": request, "current_user": current_user,
        "test_grns": test_grns, "lots_by_grn": lots_by_grn, "stocked": stocked,
        "lots_json": lots_json,
        "test_devices": test_devices,
        "total_grns": len(test_grns), "total_lots": total_lots, "total_tags": len(test_devices),
        "test_entity_name": EXTERNAL_PARTNER_TEST_ENTITY,
        "test_lot_prefix": EXTERNAL_PARTNER_TEST_LOT_PREFIX,
        "test_grn_source": EXTERNAL_PARTNER_TEST_GRN_SOURCE,
        "stage_labels": STAGE_LABELS,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/manage-lots/map-grn")
async def manage_lots_map_grn(
    request: Request,
    barcode: str = Form(...),
    grn_number: str = Form(...),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_manage_lots", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Asset IQC tab's per-row "Map" action — writes the GRN this device's
    Lot Number is already mapped to (in the GRN & Lot tab) onto the device's
    own grn_number field, so it counts under that GRN's Stocked figure
    (routers/grn.py's _stocked_map — the same mechanism the live GRN
    Post-IQC page uses; nothing test-specific about it beyond scoping the
    device lookup to the test entity)."""
    device = (await db.execute(
        select(Device).where(func.upper(Device.barcode) == barcode.strip().upper(),
                             Device.entity == EXTERNAL_PARTNER_TEST_ENTITY)
    )).scalar_one_or_none()
    if not device:
        return RedirectResponse(url="/trade-partner/manage-lots?error=Test+device+not+found", status_code=302)
    if device.grn_number != grn_number.strip():
        from services.grn_stocked import remember_safely   # GRN Stocked: keep old GRN's count
        await remember_safely(db, [device.id])
    device.grn_number = grn_number.strip()
    await audit(db, action="MANAGE_LOTS_GRN_MAPPED", user=current_user,
                table_name="devices", record_id=str(device.id),
                new_value={"barcode": device.barcode, "grn_number": grn_number.strip()},
                request=request)
    await db.commit()
    return RedirectResponse(
        url=f"/trade-partner/manage-lots?tab=asset&success={quote_plus(f'{device.barcode} mapped to GRN {grn_number.strip()}')}",
        status_code=302)


@router.post("/manage-lots/move-ready-for-sale")
async def manage_lots_move_ready_for_sale(
    request: Request,
    barcode: list[str] = Form(...),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_manage_lots", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Asset IQC tab's bulk checkbox action — moves the selected test devices'
    stage straight to Ready to Sale."""
    codes = [b.strip().upper() for b in barcode if b.strip()]
    if not codes:
        return RedirectResponse(url="/trade-partner/manage-lots?tab=asset&error=Select+at+least+one+tag", status_code=302)

    devices = (await db.execute(
        select(Device).where(func.upper(Device.barcode).in_(codes),
                             Device.entity == EXTERNAL_PARTNER_TEST_ENTITY)
    )).scalars().all()
    if not devices:
        return RedirectResponse(url="/trade-partner/manage-lots?tab=asset&error=No+matching+test+devices+found", status_code=302)

    for device in devices:
        from_stage = device.current_stage
        device.current_stage = DeviceStage.ready_to_sale
        device.updated_at = app_now()
        db.add(StageMovement(
            device_id=device.id, from_stage=from_stage, to_stage=DeviceStage.ready_to_sale,
            moved_by=current_user.username, notes="Manage Lots — bulk moved to Ready for Sale",
        ))
        await ensure_stage_location(db, device, DeviceStage.ready_to_sale, current_user)
    await audit(db, action="MANAGE_LOTS_BULK_READY_FOR_SALE", user=current_user,
                table_name="devices", record_id=",".join(str(d.id) for d in devices),
                new_value={"barcodes": [d.barcode for d in devices]},
                request=request)
    await db.commit()
    return RedirectResponse(
        url=f"/trade-partner/manage-lots?tab=asset&success={quote_plus(f'{len(devices)} tag(s) moved to Ready for Sale')}",
        status_code=302)


@router.post("/manage-lots/edit-device")
async def manage_lots_edit_device(
    request: Request,
    barcode: str = Form(...),
    serial_no: str = Form(""), brand: str = Form(""), model: str = Form(""),
    device_type: str = Form(""), cpu: str = Form(""), cpu_make: str = Form(""),
    generation: str = Form(""), ram_gb: str = Form(""), storage_gb: str = Form(""),
    storage_type: str = Form(""), grade: str = Form(""), notes: str = Form(""),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_manage_lots", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Asset IQC tab's Test Assets table — Edit button on a device already
    uploaded here. Scoped to the test entity, same as map-grn above; never
    touches Lot/GRN/stage fields, only the device-spec columns shown in the
    modal."""
    device = (await db.execute(
        select(Device).where(func.upper(Device.barcode) == barcode.strip().upper(),
                             Device.entity == EXTERNAL_PARTNER_TEST_ENTITY)
    )).scalar_one_or_none()
    if not device:
        return RedirectResponse(url="/trade-partner/manage-lots?error=Test+device+not+found", status_code=302)

    device.serial_no = serial_no.strip() or None
    device.brand = brand.strip() or None
    device.model = model.strip() or None
    device.device_type = device_type.strip() or None
    device.cpu = cpu.strip() or None
    device.cpu_make = cpu_make.strip() or None
    device.generation = generation.strip() or None
    try:
        device.ram_gb = int(ram_gb) if ram_gb.strip() else None
    except ValueError:
        pass
    try:
        device.storage_gb = int(storage_gb) if storage_gb.strip() else None
    except ValueError:
        pass
    device.storage_type = storage_type.strip() or None
    if grade.strip():
        try:
            device.grade = DeviceGrade(grade.strip())
        except ValueError:
            pass
    else:
        device.grade = None
    device.notes = notes.strip() or None

    await audit(db, action="MANAGE_LOTS_DEVICE_EDITED", user=current_user,
                table_name="devices", record_id=str(device.id),
                new_value={"barcode": device.barcode, "brand": device.brand, "model": device.model},
                request=request)
    await db.commit()
    return RedirectResponse(
        url=f"/trade-partner/manage-lots?success={quote_plus(f'{device.barcode} updated')}&tab=asset",
        status_code=302)


async def _grant_lot_visibility(db: AsyncSession, lot_ids: list[str],
                                dealer_ids: list[str], current_user: User,
                                request: Request) -> RedirectResponse:
    """Grant every (lot x dealer) pair, skipping ones that already exist.

    The single-lot route and the bulk route both come through here so there is
    one implementation of what "assign visibility" means — two copies would be
    two places for the already-assigned rule to drift.
    """
    lot_ids = [x for x in (lot_ids or []) if x and x.strip()]
    dealer_ids = [x for x in (dealer_ids or []) if x and x.strip()]
    if not lot_ids or not dealer_ids:
        return RedirectResponse(
            url="/trade-partner/manage-lots?error=Select+at+least+one+lot+and+one+dealer",
            status_code=302)

    lots = (await db.execute(select(Lot).where(Lot.id.in_(lot_ids)))).scalars().all()
    dealers = (await db.execute(
        select(Dealer).where(Dealer.id.in_(dealer_ids)))).scalars().all()
    if not lots or not dealers:
        return RedirectResponse(
            url="/trade-partner/manage-lots?error=Lot+or+dealer+not+found",
            status_code=302)

    # One query for every pair already granted, rather than a lookup per pair —
    # 20 lots x 10 dealers would otherwise be 200 round trips to a remote DB.
    existing = {
        (str(l), str(d)) for l, d in (await db.execute(
            select(LotDealerVisibility.lot_id, LotDealerVisibility.dealer_id)
            .where(LotDealerVisibility.lot_id.in_([l.id for l in lots]),
                   LotDealerVisibility.dealer_id.in_([d.id for d in dealers]))
        )).all()
    }

    created, skipped = 0, 0
    for lot in lots:
        for dealer in dealers:
            if (str(lot.id), str(dealer.id)) in existing:
                skipped += 1
                continue
            db.add(LotDealerVisibility(lot_id=lot.id, dealer_id=dealer.id,
                                       assigned_by=current_user.username))
            created += 1

    if created:
        # One audit row for the action, not one per pair: a 20x10 assign would
        # otherwise bury 200 near-identical entries in the audit log and make
        # the thing it is supposed to make reviewable unreadable.
        await audit(db, action="LOT_VISIBILITY_ASSIGNED", user=current_user,
                    table_name="lot_dealer_visibility",
                    record_id=str(lots[0].id) if len(lots) == 1 else "bulk",
                    new_value={"lots": [l.lot_number for l in lots],
                               "dealers": [d.business_name for d in dealers],
                               "granted": created, "already_had": skipped},
                    request=request)
        await db.commit()

    msg = f"{created} visibility grant(s) added"
    if skipped:
        msg += f", {skipped} already assigned"
    return RedirectResponse(
        url=f"/trade-partner/manage-lots?success={quote_plus(msg)}", status_code=302)


@router.post("/manage-lots/assign-visibility")
async def assign_lot_visibility_bulk(
    request: Request,
    lot_ids: list[str] = Form(default=[]),
    dealer_ids: list[str] = Form(default=[]),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_manage_lots", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Assign any number of lots to any number of dealers in one submit."""
    return await _grant_lot_visibility(db, lot_ids, dealer_ids, current_user, request)


@router.post("/manage-lots/set-restricted")
async def set_lots_restricted(
    request: Request,
    lot_ids: list[str] = Form(default=[]),
    restricted: str = Form("1"),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_manage_lots", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Flag any number of lots Restricted (or open them back up).

    Restricted lots appear in the partner catalog only for dealers granted
    them via Assign Visibility; open lots are visible to every partner.
    """
    ids = []
    for v in lot_ids:
        try:
            ids.append(uuid_mod.UUID(str(v)))
        except (ValueError, AttributeError):
            pass
    if not ids:
        return RedirectResponse(
            url="/trade-partner/manage-lots?error=Select+at+least+one+lot",
            status_code=302)

    want = str(restricted) in ("1", "true", "True", "on", "yes")
    lots = (await db.execute(select(Lot).where(Lot.id.in_(ids)))).scalars().all()
    for lot in lots:
        lot.is_restricted = want
    await audit(db, action="LOT_RESTRICTED_SET", user=current_user,
                table_name="lots",
                record_id=str(lots[0].id) if len(lots) == 1 else "bulk",
                new_value={"lots": [l.lot_number for l in lots], "restricted": want},
                request=request)
    await db.commit()
    verb = "restricted" if want else "opened to all partners"
    return RedirectResponse(
        url=f"/trade-partner/manage-lots?success={quote_plus(f'{len(lots)} lot(s) {verb}')}",
        status_code=302)


@router.post("/manage-lots/{lot_id}/assign-visibility")
async def assign_lot_visibility(
    lot_id: str,
    request: Request,
    dealer_id: str = Form(default=""),
    dealer_ids: list[str] = Form(default=[]),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_manage_lots", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Single-lot assign. Still accepts the original single `dealer_id` field so
    anything posting the old shape keeps working."""
    wanted = list(dealer_ids) + ([dealer_id] if dealer_id else [])
    return await _grant_lot_visibility(db, [lot_id], wanted, current_user, request)


# ═══════════════════════════════════════════════════════════════════════════
# Bids Created — captured bids + Mark Won  (sidebar: after Listings Manager)
# ═══════════════════════════════════════════════════════════════════════════

BIDS_DIR = os.path.join(UPLOADS_DIR, "partner", "bids")


async def _bid_rows(db: AsyncSession, bids):
    """Decorate bids with the account/lot/manager columns the page shows.

    Everything is fetched set-based. Doing it per row meant six extra queries
    per bid, and this table is sorted by amount across every listing, so it is
    exactly the page most likely to be long.
    """
    if not bids:
        return []

    dealer_ids = {b.dealer_id for b in bids}
    dealers = {d.id: d for d in (await db.execute(
        select(Dealer).where(Dealer.id.in_(dealer_ids)))).scalars().all()}

    contact_ids = {d.crm_contact_id for d in dealers.values() if d.crm_contact_id}
    contacts = {}
    if contact_ids:
        contacts = {c.id: c for c in (await db.execute(
            select(CRMContact).where(CRMContact.id.in_(contact_ids)))).scalars().all()}

    # A bid targets either a listing or a lot directly (catalog "Lots Available
    # to You"). Nones must be filtered out — `in_({None})` matches nothing but
    # still ships a pointless query, and mixing them in hides the real ids.
    listing_ids = {b.listing_id for b in bids if b.listing_id}
    listings = {}
    if listing_ids:
        listings = {l.id: l for l in (await db.execute(
            select(PartnerListing).where(PartnerListing.id.in_(listing_ids)))).scalars().all()}

    # Lots reached two ways: straight off the bid, or via the listing behind it.
    lot_ids = {b.lot_id for b in bids if b.lot_id}
    lot_ids |= {l.lot_id for l in listings.values() if l.lot_id}
    lots = {}
    if lot_ids:
        lots = {l.id: l for l in (await db.execute(
            select(Lot).where(Lot.id.in_(lot_ids)))).scalars().all()}

    owners = {u: (fn or u) for u, fn in (await db.execute(
        select(User.username, User.full_name))).all()}

    bid_ids = [b.id for b in bids]
    docs: dict = {}
    for d in (await db.execute(
        select(PartnerBidDocument).where(PartnerBidDocument.bid_id.in_(bid_ids))
    )).scalars().all():
        docs.setdefault(str(d.bid_id), []).append(d)

    rows = []
    for b in bids:
        dealer = dealers.get(b.dealer_id)
        listing = listings.get(b.listing_id) if b.listing_id else None
        contact = contacts.get(dealer.crm_contact_id) if dealer and dealer.crm_contact_id else None
        lot = (lots.get(b.lot_id) if b.lot_id
               else (lots.get(listing.lot_id) if listing and listing.lot_id else None))
        # Uplift over the base the bid opened against. base_amount is snapshotted
        # onto the bid row when it is placed, so this stays correct even after
        # the lot is later repriced — recomputing it from the lot would silently
        # restate the profit on every historical bid.
        base = Decimal(str(b.base_amount)) if b.base_amount else None
        profit = pct = None
        if base and base > 0:
            profit = Decimal(str(b.bid_amount)) - base
            pct = (profit / base * 100).quantize(Decimal("0.1"))
        rows.append({
            "bid": b,
            "dealer_name": dealer.business_name if dealer else "-",
            # Carried so the page can link the lot; the listing fallback has no
            # lot behind it, so the template renders plain text in that case.
            "lot_id": str(lot.id) if lot else None,
            "base_amount": base,
            "profit": profit,
            "profit_pct": pct,
            "lot_number": (lot.lot_number if lot
                           else (listing.listing_code if listing else "-")),
            "listing_title": (listing.title if listing
                              else (f"Lot {lot.lot_number}" if lot else "-")),
            # Account columns come from the CRM Account behind the dealer. A
            # dealer created directly in Dealer Management has none, so these
            # read as a dash rather than silently borrowing the dealer's name.
            "account_name": contact.company_name if contact else "-",
            "account_contact": (contact.contact_person or contact.phone or "-")
                               if contact else "-",
            "account_category": ((contact.buyer_type or contact.source_type
                                  or contact.contact_type) if contact else None) or "-",
            "manager_name": owners.get(dealer.sales_owner_username, "-")
                            if dealer and dealer.sales_owner_username else "-",
            "documents": docs.get(str(b.id), []),
            "contact": contact,
        })
    return rows


async def _payment_map(db: AsyncSession, bid_ids):
    """bid_id -> latest PartnerBidPayment (only one is expected per bid)."""
    if not bid_ids:
        return {}
    out = {}
    for p in (await db.execute(
        select(PartnerBidPayment)
        .where(PartnerBidPayment.bid_id.in_(bid_ids))
        .order_by(PartnerBidPayment.submitted_at.desc())
    )).scalars().all():
        out.setdefault(str(p.bid_id), p)
    return out


def _decorate_payments(rows, payments):
    """Attach the derived four-state payment label to won rows."""
    for r in rows:
        has_po = any(d.doc_type == "po" for d in r["documents"])
        pay = payments.get(str(r["bid"].id))
        r["payment"] = pay
        r["payment_status"] = payment_status(has_po, pay)
        r["payment_badge"] = PAYMENT_STATUS_BADGE.get(r["payment_status"], "bg-secondary")
        r["has_po"] = has_po
    return rows


def _summarise_by_lot(rows):
    """'Total Bids Created' — one row per lot, aggregating its live bids.

    Account Manager / Account Name describe the CURRENT highest bidder, since
    that is who Mark Won would award the lot to.
    """
    groups = {}
    for r in rows:
        key = r["lot_number"]
        g = groups.setdefault(key, {
            "lot_number": key, "lot_id": r["lot_id"], "bids": [],
        })
        g["bids"].append(r)
    out = []
    for g in groups.values():
        amounts = [Decimal(str(b["bid"].bid_amount)) for b in g["bids"]]
        top = max(g["bids"], key=lambda b: Decimal(str(b["bid"].bid_amount)))
        out.append({
            "lot_number": g["lot_number"], "lot_id": g["lot_id"],
            "total_bids": len(g["bids"]),
            "highest": max(amounts), "lowest": min(amounts),
            "manager_name": top["manager_name"], "account_name": top["account_name"],
            "top_bid": top["bid"], "documents": top["documents"],
        })
    out.sort(key=lambda x: x["highest"], reverse=True)
    return out


@router.get("/bids", response_class=HTMLResponse)
async def bids_created(
    request: Request,
    current_user: User = Depends(require_module_perm("trade_partner_bids")),
    db: AsyncSession = Depends(get_db),
):
    """Three tables: bids still in play, those Marked Won, and those lost.

    Captured bids sort by amount DESC - the operator's question on this page is
    always "what is the best offer on the table", so the answer is row one.

    Lost bids are the losing side of a Mark Won and are worth keeping visible:
    they are the runner-up prices on lots already awarded, which is what you
    reach for when a won deal falls through.
    """
    captured = (await db.execute(
        select(PartnerBid).where(PartnerBid.status == "active")
        .order_by(PartnerBid.bid_amount.desc())
    )).scalars().all()
    won = (await db.execute(
        select(PartnerBid).where(PartnerBid.status == "won")
        .order_by(PartnerBid.won_at.desc())
    )).scalars().all()
    # No won_at on a loser, so order by amount — same "best offer first" reading
    # as the Captured table.
    lost = (await db.execute(
        select(PartnerBid).where(PartnerBid.status == "lost")
        .order_by(PartnerBid.bid_amount.desc())
    )).scalars().all()

    captured_rows = await _bid_rows(db, captured)
    won_rows = await _bid_rows(db, won)
    won_rows = _decorate_payments(won_rows, await _payment_map(db, [b.id for b in won]))

    companies = (await db.execute(
        select(Company).where(Company.is_active.is_(True)).order_by(Company.company_name)
    )).scalars().all()
    terms = (await db.execute(
        select(TermsCondition).where(TermsCondition.is_active.is_(True))
        .order_by(TermsCondition.display_order, TermsCondition.title)
    )).scalars().all()
    terms_by_type = {}
    for t in terms:
        terms_by_type.setdefault(t.term_type, []).append(t)

    return templates.TemplateResponse("trade_partner/bids.html", {
        "request": request, "current_user": current_user,
        "captured": captured_rows,
        "summary": _summarise_by_lot(captured_rows),
        "won": won_rows,
        "lost": await _bid_rows(db, lost),
        "doc_types": BID_DOC_TYPES,
        "companies": companies,
        "terms_by_type": terms_by_type,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.get("/bids/lot-captured")
async def lot_captured_bids(
    lot_number: str,
    current_user: User = Depends(require_module_perm("trade_partner_bids")),
    db: AsyncSession = Depends(get_db),
):
    """Live bids for one lot — feeds the modal opened from a Lot Number."""
    rows = await _bid_rows(db, (await db.execute(
        select(PartnerBid).where(PartnerBid.status == "active")
        .order_by(PartnerBid.bid_amount.desc())
    )).scalars().all())
    rows = [r for r in rows if r["lot_number"] == lot_number]
    from fastapi.responses import JSONResponse
    return JSONResponse({"lot_number": lot_number, "bids": [{
        "bid_id": str(r["bid"].id), "bid_number": r["bid"].bid_number,
        "amount": float(r["bid"].bid_amount),
        "account_name": r["account_name"], "manager_name": r["manager_name"],
        "account_contact": r["account_contact"],
    } for r in rows]})


async def _lot_model_summary(db: AsyncSession, lot_id) -> list[dict]:
    """Model / Make / Device Type / Total Quantity for one lot, across ALL
    stages (no current_stage filter).

    Two bits of real-world messiness are folded out, because leaving them in
    splits one model across several rows and reads as a bug:

    * device_type is NULL on about a quarter of devices while sub_category is
      set, so Device Type falls back to sub_category.
    * Make and Model spellings vary in case ("HP"/"Hp", "Lenovo"/"LENOVO").
      Rows are grouped case-insensitively and displayed using whichever
      spelling covers the most units.

    Quantities are unaffected by either — every device still counts exactly
    once.
    """
    rows = (await db.execute(
        select(Device.model, Device.brand,
               func.coalesce(Device.device_type, Device.sub_category).label("dtype"),
               func.count(Device.id).label("qty"))
        .where(Device.lot_id == lot_id,
               Device.is_active == True, Device.is_trashed == False)  # noqa: E712
        .group_by(Device.model, Device.brand,
                  func.coalesce(Device.device_type, Device.sub_category))
    )).all()

    merged: dict = {}
    for r in rows:
        model = (r.model or "").strip()
        make = (r.brand or "").strip()
        dtype = (r.dtype or "").strip()
        key = (model.upper(), make.upper(), dtype.upper())
        e = merged.setdefault(key, {"qty": 0, "spellings": {}})
        e["qty"] += r.qty
        # remember how many units back each spelling so the biggest one wins
        e["spellings"][(model, make, dtype)] = \
            e["spellings"].get((model, make, dtype), 0) + r.qty

    out = []
    for e in merged.values():
        model, make, dtype = max(e["spellings"].items(), key=lambda kv: kv[1])[0]
        out.append({"model": model or "-", "make": make or "-",
                    "device_type": dtype or "-", "qty": e["qty"]})
    out.sort(key=lambda d: (-d["qty"], d["model"]))
    return out


@router.get("/bids/{bid_id}/po-preview")
async def po_preview(
    bid_id: str,
    current_user: User = Depends(require_module_perm("trade_partner_bids")),
    db: AsyncSession = Depends(get_db),
):
    """Account details + line items for the PO Preview modal."""
    from fastapi.responses import JSONResponse
    try:
        bid = (await db.execute(
            select(PartnerBid).where(PartnerBid.id == uuid_mod.UUID(bid_id))
        )).scalar_one_or_none()
    except (ValueError, AttributeError):
        raise HTTPException(404)
    if not bid:
        raise HTTPException(404, "Bid not found")
    rows = await _bid_rows(db, [bid])
    r = rows[0]

    # Summary of the bid's lot: one row per Model / Make / Device Type with the
    # total quantity, replacing the old per-spec line item table.
    items = []
    if bid.lot_id:
        items = await _lot_model_summary(db, bid.lot_id)

        # Fallback for lots stocked only via LotLineItem entries (no Device
        # rows created yet). LotLineItem has no device_type — sub_category is
        # the equivalent field there.
        if not items:
            for li in (await db.execute(
                select(LotLineItem).where(LotLineItem.lot_id == bid.lot_id)
                .order_by(LotLineItem.sub_category, LotLineItem.model)
            )).scalars().all():
                items.append({
                    "model": li.model or "-",
                    "make": li.brand or "-",
                    "device_type": li.sub_category or "-",
                    "qty": li.qty or 0,
                })
    return JSONResponse({
        "bid_number": bid.bid_number,
        "lot_number": r["lot_number"],
        "account_name": r["account_name"],
        "account_contact": r["account_contact"],
        "manager_name": r["manager_name"],
        "total": float(bid.bid_amount),
        "items": items,
    })


@router.post("/bids/{bid_id}/generate-po")
async def generate_bid_po(
    bid_id: str, request: Request,
    company_id: str = Form(""),
    payment_term_id: str = Form(""),
    delivery_term_id: str = Form(""),
    current_user: User = Depends(require_module_perm("trade_partner_bids", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Generate the PO PDF and ATTACH it to the bid (doc_type 'po').

    Unlike the CRM sourcing PO — which streams a one-off download — this one is
    persisted, because the partner has to be able to download it from My Bids
    and because an attached PO is what moves the bid to "Payment Pending".
    """
    from services.po_pdf import build_po_pdf
    from routers.settings import get_company_settings
    try:
        bid = (await db.execute(
            select(PartnerBid).where(PartnerBid.id == uuid_mod.UUID(bid_id))
        )).scalar_one_or_none()
    except (ValueError, AttributeError):
        raise HTTPException(404)
    if not bid:
        raise HTTPException(404, "Bid not found")

    rows = await _bid_rows(db, [bid])
    r = rows[0]
    company = await get_company_settings(db, (company_id or "").strip())

    async def _term(tid):
        if not tid:
            return []
        try:
            t = (await db.execute(
                select(TermsCondition).where(TermsCondition.id == uuid_mod.UUID(tid))
            )).scalar_one_or_none()
        except (ValueError, AttributeError):
            return []
        return [t] if t else []

    class _Item:
        """Duck-typed for build_po_pdf. Per-row prices are deliberately blank —
        the PO shows only the total bid-won value."""
        def __init__(self, name, desc, qty):
            self.item_name = name
            self.description = desc
            self.quantity = qty
            self.unit_price = None
            self.total_price = None

    items = []
    if bid.lot_id:
        for li in (await db.execute(
            select(LotLineItem).where(LotLineItem.lot_id == bid.lot_id)
            .order_by(LotLineItem.sub_category, LotLineItem.model)
        )).scalars().all():
            desc = " | ".join(x for x in [
                " ".join(y for y in [li.cpu, li.generation] if y),
                f"{li.ram_gb} GB RAM" if li.ram_gb else None,
                (f"{li.storage_gb} GB {li.storage_type or ''}".strip()
                 if li.storage_gb else None),
            ] if x)
            items.append(_Item(li.model or li.sub_category or "Item", desc, li.qty or 0))

    pdf_bytes = build_po_pdf(
        po_number=f"PO-{bid.bid_number}",
        po_date=app_now().strftime("%d-%b-%Y"),
        company=company, contact=r.get("contact"),
        line_items=items,
        payment_terms=await _term(payment_term_id),
        delivery_terms=await _term(delivery_term_id),
        disclaimers=[],
        sections={"account": True, "company": True, "items": True,
                  "payment": bool(payment_term_id), "delivery": bool(delivery_term_id),
                  "conditions": False},
        total_amount=float(bid.bid_amount),
        doc_title="PURCHASE ORDER",
        account_label="Account Details (Buyer)",
    )

    os.makedirs(BIDS_DIR, exist_ok=True)
    fname = f"PO-{bid.bid_number}-{uuid_mod.uuid4().hex[:8]}.pdf"
    with open(os.path.join(BIDS_DIR, fname), "wb") as fh:
        fh.write(pdf_bytes)
    db.add(PartnerBidDocument(
        bid_id=bid.id, doc_type="po", filename=fname,
        original_name=f"PO-{bid.bid_number}.pdf",
        uploaded_by=current_user.username,
    ))
    await audit(db, user=current_user, action="BID_PO_GENERATED",
                table_name="partner_bids", record_id=str(bid.id),
                new_value={"bid": bid.bid_number, "file": fname}, request=request)
    await db.commit()
    return RedirectResponse(
        url=f"/trade-partner/bids?success=PO+generated+and+attached+to+{bid.bid_number}",
        status_code=302)


@router.get("/bids/{bid_id}/payment")
async def bid_payment_details(
    bid_id: str,
    current_user: User = Depends(require_module_perm("trade_partner_bids")),
    db: AsyncSession = Depends(get_db),
):
    """Payment details behind a clickable BID ID."""
    from fastapi.responses import JSONResponse
    try:
        bid_uuid = uuid_mod.UUID(bid_id)
    except (ValueError, AttributeError):
        raise HTTPException(404)
    bid = (await db.execute(
        select(PartnerBid).where(PartnerBid.id == bid_uuid)
    )).scalar_one_or_none()
    if not bid:
        raise HTTPException(404, "Bid not found")
    pay = (await db.execute(
        select(PartnerBidPayment).where(PartnerBidPayment.bid_id == bid_uuid)
        .order_by(PartnerBidPayment.submitted_at.desc())
    )).scalars().first()
    if not pay:
        return JSONResponse({"bid_number": bid.bid_number, "has_payment": False,
                             "message": "Payment not received yet."})
    return JSONResponse({
        "bid_number": bid.bid_number, "has_payment": True,
        "payment_date": pay.payment_date.strftime("%d-%b-%Y") if pay.payment_date else "-",
        "payment_mode": (pay.payment_mode or "-").replace("_", " ").title(),
        "payment_utr": pay.payment_utr or "-",
        "payment_amount": float(pay.payment_amount or 0),
        "submitted_at": pay.submitted_at.strftime("%d-%b-%Y %H:%M") if pay.submitted_at else "-",
        "verified": bool(pay.verified),
        "status_text": ("Verified" if pay.verified
                        else "Verification Pending at Finance team"),
        "verified_by": pay.verified_by or "",
        "verified_at": pay.verified_at.strftime("%d-%b-%Y %H:%M") if pay.verified_at else "",
    })


@router.post("/bids/{bid_id}/mark-won")
async def mark_bid_won(
    request: Request,
    bid_id: str,
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_bids", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Award the lot to this bid and open a Buyer Deal for it.

    Marking one bid won marks every other active bid on the SAME listing lost.
    Leaving them active would keep a sold lot showing live bids on the partner
    side and let a second bid be marked won against stock already committed.
    """
    bid = (await db.execute(
        select(PartnerBid).where(PartnerBid.id == bid_id).with_for_update()
    )).scalar_one_or_none()
    if not bid:
        raise HTTPException(status_code=404, detail="Bid not found")
    if bid.status == "won":
        return RedirectResponse(
            url="/trade-partner/bids?error=That+bid+is+already+marked+won",
            status_code=302)
    if bid.status != "active":
        return RedirectResponse(
            url=f"/trade-partner/bids?error=Bid+is+{bid.status}", status_code=302)

    dealer = (await db.execute(
        select(Dealer).where(Dealer.id == bid.dealer_id))).scalar_one_or_none()
    listing = None
    if bid.listing_id:
        listing = (await db.execute(
            select(PartnerListing).where(PartnerListing.id == bid.listing_id)
        )).scalar_one_or_none()
    lot = None
    if bid.lot_id:
        lot = (await db.execute(
            select(Lot).where(Lot.id == bid.lot_id))).scalar_one_or_none()

    # The bid names either a listing or a lot; the Buyer Deal has to describe
    # whichever it actually was, not assume a listing exists.
    what = (listing.title if listing
            else (f"Lot {lot.lot_number}" if lot else "Lot"))
    source_ref = (listing.listing_code if listing
                  else (lot.lot_number if lot else "-"))

    n = ((await db.execute(select(func.count(CRMSalesOpportunity.id)))).scalar() or 0) + 1
    opp = CRMSalesOpportunity(
        opp_number=f"OPP-{app_now().year}-{n:04d}",
        # Links to the CRM Account behind the dealer when there is one, so the
        # Buyer Deal lands on that account's profile rather than floating free.
        contact_id=dealer.crm_contact_id if dealer else None,
        title=f"{what} - won by "
              f"{dealer.business_name if dealer else 'dealer'} ({bid.bid_number})",
        buyer_type="dealer",
        required_qty=(listing.qty_available if listing
                      else (lot.qty if lot else None)),
        grade_required=listing.grade_summary if listing else None,
        stage="won",
        estimated_value=bid.bid_amount,
        assigned_to=dealer.sales_owner_username if dealer else None,
        notes=(f"Created from External Partner bid {bid.bid_number} "
               f"({bid.bid_type} price) on "
               f"{'listing' if listing else 'lot'} {source_ref}."),
        created_by=current_user.username,
    )
    db.add(opp)
    await db.flush()

    bid.status = "won"
    bid.won_by = current_user.username
    bid.won_at = app_now()
    bid.opportunity_id = opp.id

    # Losers are the other live bids on the SAME target — matched on whichever
    # of the two columns this bid actually uses. Matching on listing_id alone
    # would, for a lot bid, compare NULL == NULL and mark nothing lost.
    same_target = (PartnerBid.lot_id == bid.lot_id if bid.lot_id
                   else PartnerBid.listing_id == bid.listing_id)
    await db.execute(
        update(PartnerBid)
        .where(same_target,
               PartnerBid.id != bid.id,
               PartnerBid.status == "active")
        .values(status="lost")
    )

    await audit(db, action="PARTNER_BID_MARKED_WON", user=current_user,
                table_name="partner_bids", record_id=str(bid.id),
                new_value={"bid_number": bid.bid_number,
                           "amount": str(bid.bid_amount),
                           "dealer": dealer.business_name if dealer else None,
                           "opportunity": opp.opp_number},
                request=request)
    await db.commit()
    return RedirectResponse(
        url=f"/trade-partner/bids?success=Buyer+Deal+{opp.opp_number}+created",
        status_code=302)


@router.post("/bids/{bid_id}/upload")
async def upload_bid_document(
    request: Request,
    bid_id: str,
    doc_type: str = Form(...),
    file: UploadFile = File(...),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(require_module_perm("trade_partner_bids", "edit")),
    db: AsyncSession = Depends(get_db),
):
    """Attach a Quote / PO / Invoice to a bid for the download column."""
    if doc_type not in BID_DOC_TYPES:
        return RedirectResponse(url="/trade-partner/bids?error=Unknown+document+type",
                                status_code=302)
    bid = (await db.execute(
        select(PartnerBid).where(PartnerBid.id == bid_id))).scalar_one_or_none()
    if not bid:
        raise HTTPException(status_code=404, detail="Bid not found")

    os.makedirs(BIDS_DIR, exist_ok=True)
    # Stored under a generated name: the uploaded filename is attacker-supplied
    # and would otherwise let a path like ../../config.ini escape the directory,
    # or one upload silently overwrite another's.
    ext = os.path.splitext(file.filename or "")[1][:10]
    stored = f"{bid.bid_number}_{doc_type}_{uuid_mod.uuid4().hex[:8]}{ext}"
    with open(os.path.join(BIDS_DIR, stored), "wb") as fh:
        fh.write(await file.read())

    db.add(PartnerBidDocument(
        bid_id=bid.id, doc_type=doc_type, filename=stored,
        original_name=file.filename, uploaded_by=current_user.username,
    ))
    await audit(db, action="PARTNER_BID_DOC_UPLOADED", user=current_user,
                table_name="partner_bid_documents", record_id=str(bid.id),
                new_value={"bid": bid.bid_number, "type": doc_type,
                           "file": file.filename},
                request=request)
    await db.commit()
    return RedirectResponse(url="/trade-partner/bids?success=Document+attached",
                            status_code=302)


@router.get("/bids/document/{doc_id}")
async def download_bid_document(
    doc_id: str,
    current_user: User = Depends(require_module_perm("trade_partner_bids")),
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import FileResponse
    doc = (await db.execute(
        select(PartnerBidDocument).where(PartnerBidDocument.id == doc_id)
    )).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    # basename() again on read: even though upload generates the name, a row
    # edited by any other path must not be able to walk out of BIDS_DIR.
    path = os.path.join(BIDS_DIR, os.path.basename(doc.filename))
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(path, filename=doc.original_name or doc.filename)
