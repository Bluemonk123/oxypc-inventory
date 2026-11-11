"""CRM Contacts router — unified buyer/supplier registry."""
from utils.price_export import credit_new as _credit_new, credit_edit as _credit_edit   # Pricing Add (2026-11-03)
from utils.price_export import strip_price_columns as _strip_prices   # Pricing Export (2026-11-03)
import re
import csv
import io
import json
import os
import uuid as _uuid
from datetime import datetime
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select, func, or_, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from templates_config import templates
from database import get_db
from config import UPLOADS_DIR
from utils.csv_decode import decode_csv_bytes
from utils.timezone import app_now
from services.audit_engine import audit
from auth.dependencies import get_current_user, verify_csrf, require_module_perm
from models.user import User, UserRole, ROLE_LABELS
from models.crm import (
    CRMContact, CRMContactNumber, CRMContactLocation, CRMSourcingDeal, CRMSalesOpportunity,
    CRMActivity, CRMPurchaseOrder, SOURCE_TYPES, BUYER_TYPES,
)
from models.crm_team import CRMContactTeamMember
from services.crm_team import ROLE_BUCKETS, BUCKET_LABELS, eligible_users_for_bucket

PERSON_ROLES = ["Directors", "Finance", "Manager", "Other"]


def _kyc_up(v):
    """KYC numbers are stored upper-case (2026-11-10, Pankaj)."""
    v = (v or "").strip().upper()
    return v or None


def _credit_days(v):
    """Credit Days dropdown value -> whole number of days, or None (2026-11-10)."""
    m = re.search(r"\d+", v or "")
    return int(m.group()) if m else None


async def _latest_deal_stage(db, ids):
    from services.deal_stage_status import latest_deal_stage
    return await latest_deal_stage(db, ids)


def _parse_locations(form) -> list[dict]:
    """Extract Location rows (email, address, city, state) from the repeating
    Locations section on the form, along with each location's own nested
    Contact Numbers rows (matched via the parallel cn_location_index[] array
    the template writes just before submit). Skips fully-blank location rows.
    Used by create + update."""
    emails = form.getlist("loc_email[]")
    addresses = form.getlist("loc_address[]")
    cities = form.getlist("loc_city[]")
    states = form.getlist("loc_state[]")
    n_locations = max(len(emails), len(addresses), len(cities), len(states))

    cn_indexes = form.getlist("cn_location_index[]")
    cn_roles = form.getlist("cn_role[]")
    cn_names = form.getlist("cn_person[]")
    cn_phones = form.getlist("cn_phone[]")
    cn_emails = form.getlist("cn_email[]")

    locations: list[dict] = []
    for i in range(n_locations):
        em = (emails[i] if i < len(emails) else "").strip()
        ad = (addresses[i] if i < len(addresses) else "").strip()
        ci = (cities[i] if i < len(cities) else "").strip()
        st = (states[i] if i < len(states) else "").strip()
        numbers: list[tuple[str, str, str, str]] = []
        for j in range(len(cn_indexes)):
            try:
                loc_idx = int(cn_indexes[j])
            except ValueError:
                continue
            if loc_idx != i:
                continue
            rl = (cn_roles[j] if j < len(cn_roles) else "").strip()
            nm = (cn_names[j] if j < len(cn_names) else "").strip()
            ph = (cn_phones[j] if j < len(cn_phones) else "").strip()
            em2 = (cn_emails[j] if j < len(cn_emails) else "").strip()
            if rl or nm or ph or em2:
                numbers.append((rl, nm, ph, em2))
        # 2026-11-10: the location's own email field is gone from the form, so
        # a location with only Contact Numbers filled in is kept too.
        if not (em or ad or ci or st or numbers):
            continue
        locations.append({"email": em, "address": ad, "city": ci, "state": st, "numbers": numbers})
    return locations


async def _save_kyc_upload(upload: UploadFile):
    """Save a KYC document under uploads/crm/ with a UUID-hex safe name,
    same pattern as CRM Sourcing's document uploads."""
    if not upload or not upload.filename:
        return None
    ext = os.path.splitext(upload.filename)[1].lower()
    uploads_dir = os.path.join(UPLOADS_DIR, "crm")
    os.makedirs(uploads_dir, exist_ok=True)
    safe_name = f"{_uuid.uuid4().hex}{ext}"
    dest = os.path.join(uploads_dir, safe_name)
    content = await upload.read()
    with open(dest, "wb") as f:
        f.write(content)
    return safe_name

router = APIRouter(prefix="/crm/contacts", tags=["crm-contacts"], dependencies=[Depends(verify_csrf)])

CRM_ROLES = (
    UserRole.admin, UserRole.sales, UserRole.sales_manager,
    UserRole.telecaller, UserRole.inventory_manager,
)

# ── helpers ──────────────────────────────────────────────────────────────────

async def _next_code(db: AsyncSession) -> str:
    result = await db.execute(select(func.count(CRMContact.id)))
    n = (result.scalar() or 0) + 1
    return f"CRM{n:04d}"


# ── LIST ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def list_contacts(
    request: Request,
    q: str = Query(default=""),
    from_entity: str = Query(default=""),      # Entity filter (2026-11-10)
    contact_type: str = Query(default=""),
    source_type: str = Query(default=""),
    buyer_type: str = Query(default=""),
    contacted: str = Query(default=""),        # "yes" | "no" | ""
    created_by_filter: str = Query(default=""),  # admin-only: filter by who added the contact
    f_vp_avp: str = Query(default=""),
    f_manager: str = Query(default=""),
    f_am_dm_gm: str = Query(default=""),
    f_executive: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Subquery of contact IDs that have at least one activity log
    has_activity_subq = select(CRMActivity.contact_id).distinct()

    # Distinct list of (username, full_name) for contacts creator dropdown — admin only
    crm_users: list[tuple[str, str]] = []
    if current_user.role == UserRole.admin:
        users_result = await db.execute(
            select(CRMContact.created_by, User.full_name)
            .join(User, CRMContact.created_by == User.username, isouter=True)
            .where(CRMContact.created_by.isnot(None))
            .distinct()
            .order_by(User.full_name, CRMContact.created_by)
        )
        crm_users = [(row[0], row[1] or row[0]) for row in users_result.all()]

    # Active (non-trashed) contacts with all filters applied
    query = select(CRMContact).where(CRMContact.is_trashed == False)
    if q:
        like = f"%{q}%"
        query = query.where(or_(
            CRMContact.company_name.ilike(like),
            CRMContact.contact_person.ilike(like),
            CRMContact.phone.ilike(like),
            CRMContact.city.ilike(like),
            CRMContact.state.ilike(like),
            CRMContact.gstin.ilike(like),
        ))
    if from_entity:
        query = query.where(CRMContact.from_entity == from_entity)
    if contact_type:
        query = query.where(CRMContact.contact_type == contact_type)
    if source_type:
        query = query.where(CRMContact.source_type == source_type)
    if buyer_type:
        query = query.where(CRMContact.buyer_type == buyer_type)
    if contacted == "yes":
        query = query.where(CRMContact.id.in_(has_activity_subq))
    elif contacted == "no":
        query = query.where(CRMContact.id.notin_(has_activity_subq))
    # Admin-only: filter by the user who created the contact
    if created_by_filter and current_user.role == UserRole.admin:
        query = query.where(CRMContact.created_by == created_by_filter)

    _bucket_filter_params = {
        "vp_avp": f_vp_avp, "manager": f_manager,
        "am_dm_gm": f_am_dm_gm, "executive": f_executive,
    }
    for _bucket, _raw in _bucket_filter_params.items():
        _ids = []
        for _v in _raw.split(","):
            _v = _v.strip()
            if not _v:
                continue
            try:
                _ids.append(_uuid.UUID(_v))
            except ValueError:
                continue
        if _ids:
            _subq = select(CRMContactTeamMember.contact_id).where(
                CRMContactTeamMember.role_bucket == _bucket,
                CRMContactTeamMember.user_id.in_(_ids),
            )
            query = query.where(CRMContact.id.in_(_subq))

    result = await db.execute(query.order_by(CRMContact.company_name))
    contacts = result.scalars().all()

    # Team mapping — bucket dropdown options (all active users, once) and the
    # current mapping per contact (one JOIN, no N+1), same pattern as the
    # existing locations_map/numbers_map below.
    users_result = await db.execute(select(User).where(User.status == True).order_by(User.full_name))
    all_users = users_result.scalars().all()
    from services.crm_team import bucket_options as _bucket_options
    bucket_options = await _bucket_options(db, all_users)

    team_map: dict = {}
    _contact_ids_scope = [c.id for c in contacts]
    if _contact_ids_scope:
        team_rows = (await db.execute(
            select(CRMContactTeamMember, User.full_name, User.designation, User.role)
            .join(User, CRMContactTeamMember.user_id == User.id)
            .where(CRMContactTeamMember.contact_id.in_(_contact_ids_scope))
        )).all()
        for member, full_name, designation, role in team_rows:
            _cid = str(member.contact_id)
            team_map.setdefault(_cid, {b: [] for b in ROLE_BUCKETS})
            team_map[_cid][member.role_bucket].append({
                "id": str(member.user_id), "name": full_name,
                "designation": designation or "—", "role": ROLE_LABELS.get(role, role),
            })

    # Trashed contacts (always full list, no filters)
    trashed_result = await db.execute(
        select(CRMContact).where(CRMContact.is_trashed == True).order_by(CRMContact.company_name)
    )
    trashed_contacts = trashed_result.scalars().all()

    # Most-recent activity per contact — drives "Contacted/Not Yet" pill + Last Outcome column
    all_ids = [c.id for c in contacts] + [c.id for c in trashed_contacts]
    # activity_map: str(contact_id) -> {"outcome": str, "date": datetime|None}
    activity_map: dict = {}
    if all_ids:
        rn_col = func.row_number().over(
            partition_by=CRMActivity.contact_id,
            order_by=CRMActivity.activity_date.desc()
        ).label("rn")
        act_inner = select(
            CRMActivity.contact_id,
            CRMActivity.outcome,
            CRMActivity.activity_date,
            rn_col,
        ).where(CRMActivity.contact_id.in_(all_ids)).subquery()
        act_rows = (await db.execute(
            select(act_inner.c.contact_id, act_inner.c.outcome, act_inner.c.activity_date)
            .where(act_inner.c.rn == 1)
        )).all()
        activity_map = {
            str(r.contact_id): {
                "outcome": r.outcome or "",
                "date":    r.activity_date,
            }
            for r in act_rows
        }

    contacted_set = set(activity_map.keys())   # contact IDs that have ≥1 activity

    # Locations + their nested Contact Numbers, grouped per contact (one query, no N+1).
    # locations_map: str(contact_id) -> list[{address, city, state}]
    # numbers_map:   str(contact_id) -> list[{location, numbers: [{role, name, phone, email}]}]
    locations_map: dict = {}
    numbers_map: dict = {}
    if all_ids:
        locs_result = await db.execute(
            select(CRMContactLocation)
            .where(CRMContactLocation.contact_id.in_(all_ids))
            .options(selectinload(CRMContactLocation.contact_numbers))
            .order_by(CRMContactLocation.contact_id, CRMContactLocation.sort_order)
        )
        for loc in locs_result.scalars().all():
            cid = str(loc.contact_id)
            locations_map.setdefault(cid, []).append({
                "address": loc.address or "",
                "city": loc.city or "",
                "state": loc.state or "",
            })
            numbers_map.setdefault(cid, []).append({
                "location": ", ".join(filter(None, [loc.address, loc.city, loc.state])) or "Location",
                "numbers": [
                    {
                        "role": n.person_role or "",
                        "name": n.person_name or "",
                        "phone": n.phone or "",
                        "email": n.email or "",
                    }
                    for n in loc.contact_numbers
                ],
            })

    counts = {
        "total":     len(contacts),
        "suppliers": sum(1 for c in contacts if c.contact_type in ("supplier", "both")),
        "buyers":    sum(1 for c in contacts if c.contact_type in ("buyer", "both")),
        "active":    sum(1 for c in contacts if c.status == "active"),
    }

    from utils.user_lookup import added_by_display_map
    added_by = await added_by_display_map(db)

    return templates.TemplateResponse("crm/contacts/list.html", {
        "request": request, "current_user": current_user,
        "contacts": contacts, "counts": counts,
        "trashed_contacts": trashed_contacts,
        "activity_map": activity_map,
        "deal_stage_map": await _latest_deal_stage(db, [c.id for c in contacts]),   # Last Activity (2026-11-10)
        "locations_map": locations_map,
        "numbers_map": numbers_map,
        "team_map": team_map,
        "bucket_options": bucket_options,
        "bucket_labels": BUCKET_LABELS,
        **(await _partner_move_context(db, all_users, [c.id for c in contacts])),   # Move to External Partner (2026-11-11)
        "account_manager_names": {u.username: (u.full_name or u.username) for u in all_users},   # Account Manager column (2026-11-11 PM)
        "person_roles": PERSON_ROLES,   # Location Mapping form (2026-11-11 PM)
        "f_vp_avp": f_vp_avp, "f_manager": f_manager,
        "f_am_dm_gm": f_am_dm_gm, "f_executive": f_executive,
        "contacted_set": contacted_set,
        "q": q, "contact_type": contact_type,
        "source_type": source_type, "buyer_type": buyer_type,
        "contacted": contacted,
        "created_by_filter": created_by_filter,
        "from_entity": from_entity,
        "crm_users": crm_users,
        "added_by": added_by,
        "source_types": SOURCE_TYPES, "buyer_types": BUYER_TYPES,
        "source_labels": dict(SOURCE_TYPES), "buyer_labels": dict(BUYER_TYPES),
    })


# ── BULK UPLOAD (simple direct import) ───────────────────────────────────────

async def _accounts_by_gstin_and_name(db, rows) -> tuple:
    """Existing (untrashed) Accounts for a CSV's rows: ({GSTIN upper: Account},
    {company_name lower: Account}). gstin is the match key; name the fallback."""
    want_gstins = {(r.get("gstin") or "").strip().upper() for r in rows if (r.get("gstin") or "").strip()}
    want_names = {(r.get("company_name") or "").strip().lower() for r in rows if (r.get("company_name") or "").strip()}
    by_gstin, by_name = {}, {}
    if want_gstins:
        res = await db.execute(select(CRMContact).where(
            func.upper(CRMContact.gstin).in_(want_gstins), CRMContact.is_trashed.isnot(True)))
        by_gstin = {(c.gstin or "").strip().upper(): c for c in res.scalars().all()}
    if want_names:
        res = await db.execute(select(CRMContact).where(
            func.lower(CRMContact.company_name).in_(want_names), CRMContact.is_trashed.isnot(True)))
        by_name = {(c.company_name or "").strip().lower(): c for c in res.scalars().all()}
    return by_gstin, by_name


@router.get("/upload", response_class=HTMLResponse)
async def upload_contacts_form(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return templates.TemplateResponse("crm/contacts/upload.html", {
        "request": request, "current_user": current_user,
        "result": None, "error": None,
    })


@router.post("/upload")
async def upload_contacts_csv(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bulk-create Accounts from one CSV, optionally with several Locations each.

    One row = one Account, or an extra Location for an Account already
    established earlier in the same file or already in the database. Rows for
    the same Account are matched by gstin first, then company_name, so a GST
    number can never create a second Account.

    Location Contacts (role / name / phone / email) are no longer part of this
    sheet (2026-11-11, Pankaj): they go in the separate Upload Locations sheet
    (POST /crm/contacts/upload-locations), which adds Locations and their
    Contacts to existing Accounts. A new Account's city / state still come from
    its first row's location_city / location_state.
    """
    from services.account_locations import LocationWriter, cell
    if not (file.filename or "").lower().endswith(".csv"):
        return templates.TemplateResponse("crm/contacts/upload.html", {
            "request": request, "current_user": current_user,
            "result": None, "error": "Please upload a .csv file",
        })

    text_content = decode_csv_bytes(await file.read())

    reader = csv.DictReader(io.StringIO(text_content))
    fieldnames_lower = {(c or "").strip().lower() for c in (reader.fieldnames or [])}
    if "company_name" not in fieldnames_lower:
        return templates.TemplateResponse("crm/contacts/upload.html", {
            "request": request, "current_user": current_user,
            "result": None,
            "error": "CSV must contain at least a 'company_name' column",
        })

    _valid_contact_types = {"supplier", "buyer", "both"}
    _valid_source_types  = {v for v, _ in SOURCE_TYPES} | {""}
    _valid_buyer_types   = {v for v, _ in BUYER_TYPES}  | {""}
    _valid_statuses      = {"active", "inactive", "blacklisted"}

    rows = list(reader)
    by_gstin, by_name = await _accounts_by_gstin_and_name(db, rows)
    locs = LocationWriter(db)

    created = skipped = 0
    errors: list[str] = []

    for i, row in enumerate(rows, start=2):
        company = (row.get("company_name") or "").strip()
        if not company:
            skipped += 1
            continue
        try:
            # Each row runs in its own SAVEPOINT: one bad row rolls back only
            # its own inserts, never the Accounts/Locations of earlier rows.
            async with db.begin_nested():
                gstin_val = (row.get("gstin") or "").strip().upper() or None
                contact = by_gstin.get(gstin_val) if gstin_val else None
                if contact is None:
                    contact = by_name.get(company.lower())

                row_added_something = False

                if contact is None:
                    contact_type = (row.get("contact_type") or "buyer").strip()
                    if contact_type not in _valid_contact_types:
                        contact_type = "buyer"
                    source_type = (row.get("source_type") or "").strip() or None
                    if source_type and source_type not in _valid_source_types:
                        source_type = None
                    buyer_type = (row.get("buyer_type") or "").strip() or None
                    if buyer_type and buyer_type not in _valid_buyer_types:
                        buyer_type = None
                    status = (row.get("status") or "active").strip()
                    if status not in _valid_statuses:
                        status = "active"

                    code = await _next_code(db)
                    contact = CRMContact(
                        contact_code=code,
                        company_name=company,
                        email=(row.get("email") or "").strip() or None,
                        contact_type=contact_type,
                        source_type=source_type,
                        buyer_type=buyer_type,
                        city=cell(row, "location_city") or None,
                        state=cell(row, "location_state") or None,
                        gstin=_kyc_up(row.get("gstin")),
                        status=status,
                        created_by=current_user.username,
                    )
                    contact.apply_account_status()
                    db.add(contact)
                    await db.flush()
                    if gstin_val:
                        by_gstin[gstin_val] = contact
                    by_name[company.lower()] = contact
                    created += 1
                    row_added_something = True

                _loc, loc_new = await locs.location(contact, row)
                row_added_something = row_added_something or loc_new

                if not row_added_something:
                    skipped += 1
        except Exception as e:
            errors.append(f"Row {i}: {str(e)[:100]}")
    locations_created, contacts_created = locs.locations_created, 0

    await db.commit()

    return templates.TemplateResponse("crm/contacts/upload.html", {
        "request": request, "current_user": current_user,
        "result": {
            "created": created, "skipped": skipped,
            "locations_created": locations_created,
            "contacts_created": contacts_created,
            "errors": errors,
        },
        "error": None,
    })


# ── UPLOAD LOCATIONS (2026-11-11, Pankaj) ────────────────────────────────────
# Several Locations per Account and several Contacts per Location, for Accounts
# that already exist. Columns: gstin, company_name, location_address,
# location_city, location_state, location_email, location_contact_role,
# location_contact_name, location_contact_phone, location_contact_email.

@router.get("/upload-locations", response_class=HTMLResponse)
async def upload_locations_form(
    request: Request,
    current_user: User = Depends(require_module_perm("crm_contacts", "upload")),
):
    return templates.TemplateResponse("crm/contacts/upload_locations.html", {
        "request": request, "current_user": current_user, "result": None, "error": None})


@router.post("/upload-locations")
async def upload_locations_csv(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("crm_contacts", "upload")),
):
    """Add Locations + Location Contacts to existing Accounts: matched by gstin,
    then company_name; a row whose Account isn't found is reported, never created."""
    from services.account_locations import LocationWriter, CONTACT_COLS, cell
    page = "crm/contacts/upload_locations.html"
    if not (file.filename or "").lower().endswith(".csv"):
        return templates.TemplateResponse(page, {"request": request, "current_user": current_user,
                                                 "result": None, "error": "Please upload a .csv file"})
    reader = csv.DictReader(io.StringIO(decode_csv_bytes(await file.read())))
    cols = {(c or "").strip().lower() for c in (reader.fieldnames or [])}
    if not ({"gstin", "company_name"} & cols):
        return templates.TemplateResponse(page, {"request": request, "current_user": current_user, "result": None,
                                                 "error": "CSV must have a 'gstin' or 'company_name' column"})
    rows = list(reader)
    by_gstin, by_name = await _accounts_by_gstin_and_name(db, rows)
    locs = LocationWriter(db)
    accounts, skipped, errors = set(), 0, []
    for i, row in enumerate(rows, start=2):
        gst, name = cell(row, "gstin").upper(), cell(row, "company_name")
        if not (gst or name):
            skipped += 1
            continue
        contact = (by_gstin.get(gst) if gst else None) or (by_name.get(name.lower()) if name else None)
        if contact is None:
            errors.append(f"Row {i}: no account found for " + (f"GSTIN {gst}" if gst else f"'{name}'") + " — add the account first")
            continue
        try:
            async with db.begin_nested():
                loc, loc_new = await locs.location(contact, row)
                person_new = False
                if any(cell(row, c) for c in CONTACT_COLS):
                    if loc is None:
                        raise ValueError("a contact needs location_address / city / state on the same row")
                    person_new = await locs.contact_person(contact, loc, row)
                if loc_new or person_new:
                    accounts.add(contact.id)
                else:
                    skipped += 1
        except Exception as e:
            errors.append(f"Row {i}: {str(e)[:120]}")
    if locs.locations_created or locs.contacts_created:
        await audit(db, user=current_user, action="ACCOUNT_LOCATIONS_UPLOADED", table_name="crm_contact_locations",
                    record_id=f"bulk:{len(accounts)}",
                    new_value={"file": file.filename, "accounts": len(accounts),
                               "locations": locs.locations_created, "contacts": locs.contacts_created},
                    request=request)
    await db.commit()
    return templates.TemplateResponse(page, {"request": request, "current_user": current_user, "error": None,
        "result": {"accounts": len(accounts), "locations_created": locs.locations_created,
                   "contacts_created": locs.contacts_created, "skipped": skipped, "errors": errors}})


@router.post("/locations-bulk")
async def add_locations_bulk(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("crm_contacts", "edit")),
):
    """Accounts List → Location Mapping → Use Form (2026-11-11 PM, Pankaj): the
    Add Account page's Locations section, added to every selected Account. A
    location already on an Account (same address + city + state) and a contact
    already at that location (same phone, else name) are reused, not duplicated."""
    from urllib.parse import quote_plus
    from services.account_locations import LocationWriter
    form = await request.form()
    cids = []
    for c in form.getlist("contact_ids"):
        try:
            cids.append(_uuid.UUID(c))
        except ValueError:
            continue
    locations = _parse_locations(form)
    back = "/crm/contacts"
    if not cids:
        return RedirectResponse(url=back + "?error=Select+at+least+one+account", status_code=302)
    if not locations:
        return RedirectResponse(url=back + "?error=Fill+in+at+least+one+location", status_code=302)
    if any(not (l["address"] or l["city"] or l["state"]) for l in locations):
        return RedirectResponse(url=back + "?error=" + quote_plus("Each location needs an Address, City or State"), status_code=302)
    contacts = (await db.execute(select(CRMContact).where(
        CRMContact.id.in_(cids), CRMContact.is_trashed.isnot(True)))).scalars().all()
    w = LocationWriter(db)
    for c in contacts:
        for l in locations:
            loc, _new = await w.location(c, {"location_address": l["address"], "location_city": l["city"],
                                            "location_state": l["state"], "location_email": l["email"]})
            for rl, nm, ph, em in l["numbers"]:
                await w.contact_person(c, loc, {"location_contact_role": rl, "location_contact_name": nm,
                                                "location_contact_phone": ph, "location_contact_email": em})
    await audit(db, user=current_user, action="ACCOUNT_LOCATIONS_ADDED", table_name="crm_contact_locations",
                record_id=f"bulk:{len(contacts)}",
                new_value={"accounts": [str(c.id) for c in contacts][:50], "locations": w.locations_created,
                           "contacts": w.contacts_created, "via": "Accounts List → Location Mapping"}, request=request)
    await db.commit()
    return RedirectResponse(url=back + "?success=" + quote_plus(
        f"Location Mapping: {w.locations_created} location(s) and {w.contacts_created} contact(s) added "
        f"across {len(contacts)} account(s); existing ones were kept"), status_code=302)


@router.post("/upload-gsts")
async def upload_gsts(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload GSTs (2026-11-11 PM, Pankaj): a one-column CSV of GST numbers →
    which Accounts have them, so the Accounts List can tick those rows (like
    Upload Tags on All Inventory). Read-only."""
    from fastapi.responses import JSONResponse
    reader = csv.DictReader(io.StringIO(decode_csv_bytes(await file.read())))
    fields = {(f or "").strip().lower(): f for f in (reader.fieldnames or [])}
    key = fields.get("gstin") or fields.get("gst") or fields.get("gst_number") or fields.get("gst_no")
    if not key:
        return JSONResponse({"error": "CSV must have a 'gstin' column header"}, status_code=400)
    gsts, seen, dups, rows_read, errors = [], set(), [], 0, []
    for i, row in enumerate(reader, start=2):
        rows_read += 1
        g = (row.get(key) or "").strip().upper()
        if not g:
            errors.append(f"Row {i}: gstin is empty")
            continue
        if g in seen:
            dups.append(g)
            continue
        seen.add(g)
        gsts.append(g)
    found = {}
    if gsts:
        for cid, g in (await db.execute(select(CRMContact.id, func.upper(func.trim(CRMContact.gstin))).where(
                func.upper(func.trim(CRMContact.gstin)).in_(gsts), CRMContact.is_trashed.isnot(True)))).all():
            found.setdefault(g, []).append(str(cid))
    return JSONResponse({"found": found, "not_found": [g for g in gsts if g not in found],
                         "rows_read": rows_read, "duplicates": dups, "errors": errors})


# ── ACCOUNT TEAM MAPPING ─────────────────────────────────────────────────────

@router.post("/team-mapping")
async def save_team_mapping(
    request: Request,
    contact_ids: list[str] = Form(...),
    mode: str = Form(...),
    owner_level: str = Form(default=""),
    owner_id: str = Form(default=""),
    pick_am_dm_gm: str = Form(default=""),
    pick_manager: str = Form(default=""),
    pick_vp_avp: str = Form(default=""),
    bucket_vp_avp: str = Form(default=""),
    bucket_manager: str = Form(default=""),
    bucket_am_dm_gm: str = Form(default=""),
    bucket_executive: str = Form(default=""),
    bucket_finance: str = Form(default=""),
    return_to: str = Form(default=""),   # deal pages' Team Mapping Edit (2026-11-11)
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("crm_contacts", "edit")),
):
    """Map Team (2026-11-11 PM, Pankaj): one Executive or Team Leader is the
    Account Manager (the account's Assigned To); the levels above it come from
    Report To in the modal (editable). Executive / Team Leader / Manager / AVP/VP
    are replaced together — in single mode always, in bulk mode only when an
    Account Manager is picked. Finance: single = as picked (blank clears),
    bulk = only when picked. The older per-level bucket_* fields still work."""
    from services.crm_team import replace_team_buckets, HIERARCHY
    from services.partner_move import levels_from
    _rt = return_to.strip() if return_to.strip().startswith("/crm/") and "//" not in return_to else ""

    def _parse_ids(s: str) -> list:
        out = []
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(_uuid.UUID(part))
            except ValueError:
                continue
        return out

    owner = None
    oid = (_parse_ids(owner_id) or [None])[0]
    if oid:
        owner = (await db.execute(select(User).where(User.id == oid))).scalar_one_or_none()
    if owner_level or owner_id:                       # the Account Manager modal
        lvl = owner_level if owner_level in ("executive", "am_dm_gm") else "executive"
        picks = {b: (_parse_ids(v) or [None])[0] for b, v in
                 (("am_dm_gm", pick_am_dm_gm), ("manager", pick_manager), ("vp_avp", pick_vp_avp))}
        team = levels_from(lvl, owner.id, picks) if owner else {}
        if owner or mode == "single":
            bucket_user_ids = {b: team.get(b, []) for b in HIERARCHY}   # the whole team is replaced
        else:
            bucket_user_ids = {}
        fin = _parse_ids(bucket_finance)
        if mode == "single" or fin:
            bucket_user_ids["finance"] = fin
    else:                                             # older per-level form
        raw_buckets = {
            "vp_avp": bucket_vp_avp, "manager": bucket_manager,
            "am_dm_gm": bucket_am_dm_gm, "executive": bucket_executive,
            "finance": bucket_finance,
        }
        if mode == "single":
            bucket_user_ids = {b: _parse_ids(v) for b, v in raw_buckets.items()}
        else:
            bucket_user_ids = {b: _parse_ids(v) for b, v in raw_buckets.items() if v.strip()}

    cids = []
    for c in contact_ids:
        try:
            cids.append(_uuid.UUID(c))
        except ValueError:
            continue
    if not cids:
        return RedirectResponse(url="/crm/contacts?error=No+accounts+selected", status_code=302)

    contacts = (await db.execute(select(CRMContact).where(CRMContact.id.in_(cids)))).scalars().all()

    for contact in contacts:
        old_rows = (await db.execute(
            select(CRMContactTeamMember).where(CRMContactTeamMember.contact_id == contact.id)
        )).scalars().all()
        old_value: dict = {}
        for r in old_rows:
            old_value.setdefault(r.role_bucket, []).append(str(r.user_id))
        old_value["account_manager"] = contact.assigned_to

        await replace_team_buckets(
            db, contact_id=contact.id, bucket_user_ids=bucket_user_ids,
            created_by=current_user.username,
        )
        if owner:
            contact.assigned_to = owner.username      # Account Manager

        new_value = {b: [str(u) for u in ids] for b, ids in bucket_user_ids.items()}
        if owner:
            new_value["account_manager"] = owner.username
        await audit(db, user=current_user, action="TEAM_MAPPING_UPDATED",
                    table_name="crm_contact_team_members", record_id=str(contact.id),
                    old_value=old_value, new_value=new_value, request=request)

    await db.commit()
    return RedirectResponse(url=(_rt or "/crm/contacts") + ("&" if "?" in (_rt or "") else "?") + "success=Team+mapping+updated",
                            status_code=302)


async def _partner_move_context(db, users, contact_ids) -> dict:
    """Move to External Partner modal: every active user (with designation) for
    the level dropdowns, each user's Report To chain for the auto-fill, and the
    Accounts on this page that are already External Partners (for the badge)."""
    from models.dealers import Dealer
    from models.partner import PRICE_SEGMENTS
    from routers.partner_admin import PARTNER_TYPES
    from services.crm_team import designation_hierarchy
    partner_ids = set()
    if contact_ids:
        partner_ids = {str(x) for x in (await db.execute(select(Dealer.crm_contact_id).where(
            Dealer.crm_contact_id.in_(contact_ids), Dealer.trashed_at.is_(None)))).scalars().all()}
    return {"pm_users": [(str(u.id), u.full_name or u.username, u.designation or "") for u in users],
            "pm_levels": designation_hierarchy(users), "partner_contact_ids": partner_ids,
            "pm_partner_types": PARTNER_TYPES, "pm_price_segments": PRICE_SEGMENTS}


@router.post("/move-to-partner")
async def move_accounts_to_partner(
    request: Request,
    contact_ids: list[str] = Form(default=[]),
    owner_level: str = Form("executive"),
    owner_id: str = Form(""),
    pick_am_dm_gm: str = Form(""),
    pick_manager: str = Form(""),
    pick_vp_avp: str = Form(""),
    partner_type: str = Form(""),       # not asked any more (2026-11-11 PM): blank =
    price_segment: str = Form(""),      # keep a partner's own, else dealer / new_dealer
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("trade_partner_partners", "add")),
):
    """Accounts List → Move to External Partner (2026-11-11, Pankaj): the
    selected Accounts become External Partners without a portal login. The
    modal is a plain confirmation (2026-11-11 PM); owner_id / pick_* / type /
    segment are still accepted but no longer sent (services/partner_move.py)."""
    from urllib.parse import quote_plus
    from models.partner import PRICE_SEGMENTS
    from routers.partner_admin import PARTNER_TYPES
    from services.partner_move import move_to_partner

    def _uid(v):
        try:
            return _uuid.UUID(v.strip()) if v and v.strip() else None
        except ValueError:
            return None

    back = "/crm/contacts"
    if owner_level not in ("executive", "am_dm_gm"):
        owner_level = "executive"
    oid = _uid(owner_id)
    owner = (await db.execute(select(User).where(User.id == oid, User.status == True))).scalar_one_or_none() if oid else None  # noqa: E712
    if oid and not owner:
        return RedirectResponse(url=back + "?error=" + quote_plus("That user is not active"), status_code=302)
    # team mapping is optional (2026-11-11 PM): no owner = move only, team kept
    cids = [x for x in (_uid(c) for c in contact_ids) if x]
    contacts = (await db.execute(select(CRMContact).where(
        CRMContact.id.in_(cids), CRMContact.is_trashed.isnot(True)))).scalars().all() if cids else []
    if not contacts:
        return RedirectResponse(url=back + "?error=" + quote_plus("Select at least one account"), status_code=302)
    picks = {"am_dm_gm": _uid(pick_am_dm_gm), "manager": _uid(pick_manager), "vp_avp": _uid(pick_vp_avp)}
    res = await move_to_partner(
        db, contacts, current_user, owner=owner, owner_level=owner_level, picks=picks,
        partner_type=partner_type if partner_type in PARTNER_TYPES else None,
        price_segment=price_segment if price_segment in PRICE_SEGMENTS else None,
        request=request)
    await db.commit()
    parts = []
    if res["moved"]:
        parts.append(f"{len(res['moved'])} account(s) moved to External Partner")
    if res["updated"]:
        parts.append(f"{len(res['updated'])} already a partner")
    if owner:
        parts.append(f"Team mapped — Account Manager / Sales Owner: {owner.full_name or owner.username}")
    parts.append("Create their logins with Generate Access")
    # Land on Partner Accounts, where the moved accounts are now listed (2026-11-11 PM)
    # (the route already needs the Partner Accounts "add" permission)
    return RedirectResponse(url="/trade-partner/partners?success=" + quote_plus(". ".join(parts)), status_code=302)


@router.get("/team-mapping/sample-csv")
async def team_mapping_sample_csv(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    contacts = (await db.execute(
        select(CRMContact).where(CRMContact.is_trashed == False).order_by(CRMContact.company_name)
    )).scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    from services.crm_team import CSV_COLUMNS
    writer.writerow(["gstin", "company_name"] + [CSV_COLUMNS[b] for b in ROLE_BUCKETS])   # Executive, Team Leader, Manager, AVP/VP, Finance (2026-11-11)
    for c in contacts:
        writer.writerow([c.gstin or "", c.company_name] + [""] * len(ROLE_BUCKETS))

    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename=team_mapping_sample.csv"},
    )


async def _team_mapping_reference_rows(db: AsyncSession) -> list:
    users = (await db.execute(select(User).where(User.status == True).order_by(User.full_name))).scalars().all()
    reference = []
    seen = set()
    for bucket in ROLE_BUCKETS:
        for u in eligible_users_for_bucket(users, bucket):
            if u.id in seen:
                continue
            seen.add(u.id)
            reference.append({"full_name": u.full_name, "role": u.role_label, "designation": u.designation or ""})
    return reference


@router.get("/team-mapping/upload", response_class=HTMLResponse)
async def team_mapping_upload_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    reference = await _team_mapping_reference_rows(db)
    return templates.TemplateResponse("crm/contacts/team_mapping_upload.html", {
        "request": request, "current_user": current_user,
        "reference": reference, "result": None,
    })


@router.post("/team-mapping/upload", response_class=HTMLResponse)
async def team_mapping_upload_apply(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("crm_contacts", "edit")),
):
    from services.crm_team import replace_team_buckets

    raw = decode_csv_bytes(await file.read())
    reader = csv.DictReader(io.StringIO(raw))

    all_users = (await db.execute(select(User).where(User.status == True))).scalars().all()
    by_name: dict = {}
    for u in all_users:
        by_name.setdefault(u.full_name, []).append(u)
    from services.crm_team import designation_hierarchy, HIERARCHY
    hierarchy = designation_hierarchy(all_users)
    user_by_id = {u.id: u for u in all_users}

    applied = 0
    skipped = 0
    errors: list = []

    for i, row in enumerate(reader, start=2):
        gstin = (row.get("gstin") or "").strip().upper()
        if not gstin:
            skipped += 1
            errors.append(f"Row {i}: missing GSTIN, skipped")
            continue

        contact = (await db.execute(
            select(CRMContact).where(func.upper(CRMContact.gstin) == gstin)
        )).scalars().first()
        if not contact:
            skipped += 1
            errors.append(f"Row {i}: no account found for GSTIN {gstin}, skipped")
            continue

        bucket_user_ids: dict = {}
        from services.crm_team import CSV_COLUMNS
        for bucket in ROLE_BUCKETS:
            cell = (row.get(CSV_COLUMNS[bucket]) or row.get(bucket) or "").strip()   # new column name, or the old key
            if not cell:
                continue
            resolved = []
            for name in [n.strip() for n in cell.split(",") if n.strip()]:
                matches = by_name.get(name, [])
                if len(matches) != 1:
                    reason = "not found" if not matches else "matches multiple users"
                    errors.append(f"Row {i}: '{name}' in {bucket} {reason}, skipped")
                    continue
                resolved.append(matches[0].id)
            bucket_user_ids[bucket] = resolved

        if not bucket_user_ids:
            skipped += 1
            continue

        # Executive / Team Leader filled (2026-11-11 PM, Pankaj): that person is the
        # Account Manager, and every level above it the row leaves empty is mapped
        # from their Report To chain by designation (Team Leader, Manager, AVP/VP).
        owner = None
        for lvl in ("executive", "am_dm_gm"):
            if bucket_user_ids.get(lvl):
                owner = (lvl, bucket_user_ids[lvl][0])
                break
        if owner:
            up = hierarchy.get(str(owner[1]), {})
            for b in HIERARCHY[HIERARCHY.index(owner[0]) + 1:]:
                if b not in bucket_user_ids and up.get(b):
                    bucket_user_ids[b] = [_uuid.UUID(up[b])]

        old_rows = (await db.execute(
            select(CRMContactTeamMember).where(CRMContactTeamMember.contact_id == contact.id)
        )).scalars().all()
        old_value: dict = {}
        for r in old_rows:
            old_value.setdefault(r.role_bucket, []).append(str(r.user_id))
        old_value["account_manager"] = contact.assigned_to

        await replace_team_buckets(
            db, contact_id=contact.id, bucket_user_ids=bucket_user_ids,
            created_by=current_user.username,
        )
        new_value = {b: [str(u) for u in ids] for b, ids in bucket_user_ids.items()}
        if owner and owner[1] in user_by_id:
            contact.assigned_to = user_by_id[owner[1]].username     # Account Manager
            new_value["account_manager"] = contact.assigned_to
        await audit(db, user=current_user, action="TEAM_MAPPING_UPDATED",
                    table_name="crm_contact_team_members", record_id=str(contact.id),
                    old_value=old_value, new_value=new_value, request=request)
        applied += 1

    await db.commit()

    reference = await _team_mapping_reference_rows(db)
    return templates.TemplateResponse("crm/contacts/team_mapping_upload.html", {
        "request": request, "current_user": current_user,
        "reference": reference,
        "result": {"applied": applied, "skipped": skipped, "errors": errors},
    })


# ── IMPORT CSV ────────────────────────────────────────────────────────────────

@router.get("/import-csv", response_class=HTMLResponse)
async def import_csv_form(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return templates.TemplateResponse("crm/contacts/import.html", {
        "request": request, "current_user": current_user,
        "preview": None, "errors": None,
    })


@router.post("/import-csv", response_class=HTMLResponse)
async def import_csv_preview(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        return templates.TemplateResponse("crm/contacts/import.html", {
            "request": request, "current_user": current_user,
            "preview": None, "errors": ["File too large. Maximum allowed size is 2 MB."],
        })
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    rows = list(reader)
    if len(rows) > 500:
        return templates.TemplateResponse("crm/contacts/import.html", {
            "request": request, "current_user": current_user,
            "preview": None, "errors": [f"Too many rows ({len(rows)}). Maximum 500 rows per import."],
        })
    errors = []
    preview = []
    for i, row in enumerate(rows, 1):
        company = (row.get("company_name") or "").strip()
        phone   = (row.get("phone") or "").strip()
        if not company:
            errors.append(f"Row {i}: company_name is required")
            continue
        dup = None
        if phone:
            r = await db.execute(select(CRMContact).where(CRMContact.phone == phone))
            dup = r.scalars().first()
        if dup is None and (row.get("gstin") or "").strip():
            dup = await _gst_owner(db, row.get("gstin"))      # GST is unique (2026-11-11)
        preview.append({
            "row": i,
            "company_name": company,
            "contact_person": (row.get("contact_person") or "").strip(),
            "phone": phone,
            "whatsapp": (row.get("whatsapp") or "").strip(),
            "email": (row.get("email") or "").strip(),
            "contact_type": (row.get("contact_type") or "supplier").strip(),
            "source_type": (row.get("source_type") or "").strip(),
            "buyer_type": (row.get("buyer_type") or "").strip(),
            "city": (row.get("city") or "").strip(),
            "state": (row.get("state") or "").strip(),
            "gstin": (row.get("gstin") or "").strip(),
            "tags": (row.get("tags") or "").strip(),
            "duplicate": dup.company_name if dup else None,
        })
    preview_json = json.dumps(preview)
    return templates.TemplateResponse("crm/contacts/import.html", {
        "request": request, "current_user": current_user,
        "preview": preview, "errors": errors,
        "preview_json": preview_json,
    })


@router.post("/import-confirm")
async def import_csv_confirm(
    request: Request,
    preview_data: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        rows = json.loads(preview_data)
        if not isinstance(rows, list):
            raise ValueError("invalid payload")
    except (ValueError, TypeError):
        return RedirectResponse(url="/crm/contacts?error=Invalid+import+data", status_code=302)

    _valid_contact_types = {"supplier", "buyer", "both"}
    _valid_source_types  = {v for v, _ in SOURCE_TYPES} | {""}
    _valid_buyer_types   = {v for v, _ in BUYER_TYPES}  | {""}

    imported = 0
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("duplicate"):
            skipped += 1
            continue
        company = (row.get("company_name") or "").strip()
        if not company:
            continue
        contact_type = (row.get("contact_type") or "supplier").strip()
        if contact_type not in _valid_contact_types:
            contact_type = "supplier"
        source_type = (row.get("source_type") or "").strip()
        if source_type not in _valid_source_types:
            source_type = None
        buyer_type = (row.get("buyer_type") or "").strip()
        if buyer_type not in _valid_buyer_types:
            buyer_type = None
        code = await _next_code(db)
        c = CRMContact(
            contact_code=code,
            company_name=company,
            contact_person=(row.get("contact_person") or "").strip() or None,
            phone=(row.get("phone") or "").strip() or None,
            whatsapp=(row.get("whatsapp") or "").strip() or None,
            email=(row.get("email") or "").strip() or None,
            contact_type=contact_type,
            source_type=source_type or None,
            buyer_type=buyer_type or None,
            city=(row.get("city") or "").strip() or None,
            state=(row.get("state") or "").strip() or None,
            gstin=_kyc_up(row.get("gstin")),
            tags=(row.get("tags") or "").strip() or None,
            created_by=current_user.username,
        )
        db.add(c)
        imported += 1
    await db.commit()
    return RedirectResponse(
        url=f"/crm/contacts?success=Imported+{imported}+contacts,+{skipped}+skipped+(duplicates)",
        status_code=302,
    )


# ── EXPORT CSV ────────────────────────────────────────────────────────────────

@router.get("/export-csv")
async def export_contacts_csv(
    request: Request,
    q: str = Query(default=""),
    from_entity: str = Query(default=""),      # Entity filter (2026-11-10)
    contact_type: str = Query(default=""),
    source_type: str = Query(default=""),
    buyer_type: str = Query(default=""),
    contacted: str = Query(default=""),
    created_by_filter: str = Query(default=""),
    f_vp_avp: str = Query(default=""),       # team filters too (2026-11-11 PM): the
    f_manager: str = Query(default=""),      # file = exactly the rows on the list,
    f_am_dm_gm: str = Query(default=""),     # matching the count on the button
    f_executive: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export filtered contacts as CSV — admin only."""
    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Admin only")

    # Same filter logic as list_contacts
    from models.crm import CRMActivity
    has_activity_subq = select(CRMActivity.contact_id).distinct()
    query = select(CRMContact).where(CRMContact.is_trashed == False)
    if q:
        like = f"%{q}%"
        query = query.where(or_(
            CRMContact.company_name.ilike(like),
            CRMContact.contact_person.ilike(like),
            CRMContact.phone.ilike(like),
            CRMContact.city.ilike(like),
            CRMContact.state.ilike(like),
            CRMContact.gstin.ilike(like),
        ))
    if from_entity:
        query = query.where(CRMContact.from_entity == from_entity)
    if contact_type:
        query = query.where(CRMContact.contact_type == contact_type)
    if source_type:
        query = query.where(CRMContact.source_type == source_type)
    if buyer_type:
        query = query.where(CRMContact.buyer_type == buyer_type)
    if contacted == "yes":
        query = query.where(CRMContact.id.in_(has_activity_subq))
    elif contacted == "no":
        query = query.where(CRMContact.id.notin_(has_activity_subq))
    if created_by_filter:
        query = query.where(CRMContact.created_by == created_by_filter)
    for _bucket, _raw in {"vp_avp": f_vp_avp, "manager": f_manager,
                          "am_dm_gm": f_am_dm_gm, "executive": f_executive}.items():
        _ids = []
        for _v in _raw.split(","):
            try:
                _ids.append(_uuid.UUID(_v.strip()))
            except ValueError:
                continue
        if _ids:
            query = query.where(CRMContact.id.in_(select(CRMContactTeamMember.contact_id).where(
                CRMContactTeamMember.role_bucket == _bucket, CRMContactTeamMember.user_id.in_(_ids))))

    result = await db.execute(query.order_by(CRMContact.company_name))
    contacts = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Contact Code", "Type", "Company Name", "Contact Person",
        "Phone", "WhatsApp", "Email", "GSTIN", "City", "State",
        "Source Type", "Buyer Type", "Status", "Tags",
        "Credit Limit", "Outstanding", "Account Manager", "Created By", "Created At",
    ])
    for c in contacts:
        writer.writerow([
            c.contact_code, c.contact_type, c.company_name, c.contact_person or "",
            c.phone or "", c.whatsapp or "", c.email or "", c.gstin or "",
            c.city or "", c.state or "",
            c.source_type or "", c.buyer_type or "", c.status,
            c.tags or "", float(c.credit_limit or 0), float(c.outstanding or 0),
            c.assigned_to or "", c.created_by or "",
            c.created_at.strftime("%Y-%m-%d %H:%M") if c.created_at else "",
        ])
    csv_bytes = output.getvalue().encode("utf-8-sig")
    from utils.timezone import app_now
    filename = f"crm-contacts-{app_now().strftime('%Y%m%d-%H%M')}.csv"
    return await _strip_prices(StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    ), current_user, 'crm_contacts', ['Credit Limit', 'Outstanding'])


# ── ACCOUNTS BULK UPLOAD (accounts + call records in one sheet) ───────────────
#
# Registered ABOVE the "/{contact_id}" route on purpose: that path parameter
# matches any single segment, so a template route declared after it would be
# swallowed and "/crm/contacts/calls" would be looked up as a contact id.

@router.get("/calls/bulk-upload-template")
async def account_calls_bulk_upload_template(
    current_user: User = Depends(get_current_user),
):
    """Sample CSV for the Accounts bulk upload.

    Auth-guarded to match the upload endpoint it feeds. The rows are fictional,
    but a public endpoint still hands out the module's exact field names to
    anyone who asks, and only signed-in staff have any use for it.

    Row 1 is a full account + a call. Row 2 is account detail only (no call
    columns) — that is the supported way to import or update accounts without
    manufacturing a call record for each one.
    """
    header = (
        "company_name,contact_person,phone,whatsapp,email,gstin,address,city,state,pincode,"
        "contact_type,source_type,buyer_type,assigned_to,"
        "call_date,call_type,call_mode,call_outcome,summary,items_discussed,"
        "next_followup_date,notes\n"
    )
    sample = (
        "Example Traders,Ravi Kumar,9876543210,9876543210,ravi@example.com,"
        "07AABCU9603R1ZM,12 Nehru Place,New Delhi,Delhi,110019,"
        "buyer,trader,dealer,sales_user,"
        "2026-07-15,outbound,phone,interested,Discussed i5 8th gen stock,"
        "i5 8th Gen laptops,2026-07-22,Wants 20 units\n"
        "ABC Electronics,Sunita Rao,9812345678,,sunita@abc.com,,,Mumbai,Maharashtra,,"
        "buyer,,retail,,,,,,,,,\n"
    )
    return StreamingResponse(
        iter([(header + sample).encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition":
                 "attachment; filename=accounts_bulk_upload_template.csv"},
    )


@router.post("/calls/bulk-upload", response_class=HTMLResponse)
async def account_calls_bulk_upload(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One uploader covering all five cases the operator asked for:

      1. update an existing Account and log a call
      2. update an existing Account with no call columns filled
      3. create a new Account and log a call
      4. create a new Account with no call columns filled
      5. plain account sheets, so the separate Bulk Upload CSV button is
         redundant — its column names are accepted here unchanged

    Mirrors the dealer call-records uploader (routers/dealers.py) deliberately:
    same header aliasing, same "one bad row never aborts the file" contract,
    same set-based lookup instead of a query per row.
    """
    form = await request.form()
    upload = form.get("file")
    if not upload or not hasattr(upload, "read"):
        return RedirectResponse(url="/crm/contacts?error=No+file+uploaded",
                                status_code=302)

    text_content = decode_csv_bytes(await upload.read())
    text_content = text_content.replace("\r\n", "\n").replace("\r", "\n").strip()

    def _norm(s) -> str:
        if not s or not isinstance(s, str):
            return ""
        return s.lower().strip().replace(" ", "_").replace("-", "_")

    # Accepts the headers of the existing Bulk Upload CSV template as well as
    # the natural spellings a telecaller's own sheet uses.
    _ALIASES = {
        "account":        "company_name", "account_name":  "company_name",
        "company":        "company_name", "business_name": "company_name",
        "firm_name":      "company_name", "dealer_name":   "company_name",
        "contact":        "contact_person", "person_name": "contact_person",
        "owner_name":     "contact_person", "contact_name": "contact_person",
        "mobile":         "phone",        "mobile_number": "phone",
        "phone_number":   "phone",        "contact_no":    "phone",
        "whatsapp_number": "whatsapp",
        "email_id":       "email",        "mail":          "email",
        "gst":            "gstin",        "gst_no":        "gstin",
        "gst_number":     "gstin",
        "pin":            "pincode",      "pin_code":      "pincode",
        "type":           "contact_type",
        "assigned":       "assigned_to",  "sales_person":  "assigned_to",
        "date":           "call_date",    "called_on":     "call_date",
        "mode":           "call_mode",
        "outcome":        "call_outcome",
        "remark":         "notes",        "remarks":       "notes",
        "discussion":     "summary",      "call_summary":  "summary",
        "followup_date":  "next_followup_date",
        "next_followup":  "next_followup_date",
    }

    # csv key -> CRMContact attribute. Only non-blank values are applied, so a
    # sparse sheet never blanks out detail already on the account.
    _ACCOUNT_COLS = {
        "company_name": "company_name", "contact_person": "contact_person",
        "phone": "phone", "whatsapp": "whatsapp", "email": "email",
        "gstin": "gstin", "pan": "pan", "address": "address",
        "city": "city", "state": "state", "pincode": "pincode",
        "contact_type": "contact_type", "source_type": "source_type",
        "buyer_type": "buyer_type", "assigned_to": "assigned_to",
        "notes": "notes",
    }

    # A row is a CALL only if it carries one of these. Without this test, a
    # plain account list would manufacture one blank activity per account and
    # inflate every call count on the CRM dashboard.
    _CALL_COLS = (
        "call_date", "call_type", "call_mode", "call_outcome", "summary",
        "items_discussed", "next_followup_date",
    )

    reader = csv.DictReader(io.StringIO(text_content))
    raw_fields = reader.fieldnames or []
    reader.fieldnames = [_ALIASES.get(_norm(h), _norm(h)) for h in raw_fields]
    rows = [
        {k: (str(v).strip() if v is not None else "") for k, v in r.items() if k}
        for r in reader
    ]
    if not rows:
        return RedirectResponse(url="/crm/contacts?error=No+rows+found+in+file",
                                status_code=302)

    # Two set-based lookups rather than one query per row.
    want_phones = {r.get("phone", "") for r in rows if r.get("phone", "")}
    want_names = {r.get("company_name", "").lower()
                  for r in rows if r.get("company_name", "")}

    by_phone: dict = {}
    by_name: dict = {}
    if want_phones:
        res = await db.execute(select(CRMContact).where(
            CRMContact.phone.in_(want_phones), CRMContact.is_trashed == False))  # noqa: E712
        by_phone = {c.phone: c for c in res.scalars().all()}
    if want_names:
        res = await db.execute(select(CRMContact).where(
            func.lower(CRMContact.company_name).in_(want_names),
            CRMContact.is_trashed == False))  # noqa: E712
        by_name = {(c.company_name or "").strip().lower(): c
                   for c in res.scalars().all()}

    by_gst: dict = {}      # GST → Account, filled as rows are read (2026-11-11)

    def _to_date(val: str):
        if not val:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
        raise ValueError(f"unrecognised date '{val}'")

    valid_dirs = {"outbound", "inbound"}
    valid_modes = {"call", "whatsapp", "visit", "email", "meeting", "note"}

    logged, errors = 0, []
    created_accounts, updated_accounts = 0, 0
    code_base = ((await db.execute(select(func.count(CRMContact.id)))).scalar() or 0) + 1
    new_seq = 0

    for i, row in enumerate(rows, start=2):   # row 1 = header
        try:
            phone = row.get("phone", "")
            name = row.get("company_name", "")
            gst = (row.get("gstin", "") or "").strip().upper()

            # GST is unique per Account (2026-11-11): a known GST is always that Account
            contact = by_gst.get(gst) if gst else None
            if contact is None and gst:
                contact = await _gst_owner(db, gst)
                if contact is not None:
                    by_gst[gst] = contact
            if contact is None and phone:
                contact = by_phone.get(phone)
            if contact is None and name:
                contact = by_name.get(name.lower())

            if contact is None:
                if not (phone or name):
                    errors.append(f"Row {i}: needs a company_name or a phone")
                    continue
                new_seq += 1
                contact = CRMContact(
                    contact_code=f"CRM{code_base + new_seq - 1:04d}",
                    company_name=name or phone,
                    contact_type=(row.get("contact_type", "") or "buyer"),
                    status="inactive",
                    created_by=current_user.username,
                )
                for col, attr in _ACCOUNT_COLS.items():
                    val = row.get(col, "")
                    if val:
                        setattr(contact, attr, val)
                db.add(contact)
                # flush so contact.id exists for the CRMActivity FK, and so a
                # later row for the same account reuses this one rather than
                # creating a second copy from within the same file.
                await db.flush()
                if phone:
                    by_phone[phone] = contact
                if name:
                    by_name[name.lower()] = contact
                if gst:
                    by_gst[gst] = contact
                created_accounts += 1
            else:
                changed = False
                for col, attr in _ACCOUNT_COLS.items():
                    val = row.get(col, "")
                    if attr == "gstin" and val and await _gst_owner(db, val, contact.id):
                        errors.append(f"Row {i}: GST {val.upper()} is already used by another account — GST not changed")
                        continue
                    if val and (getattr(contact, attr, None) or "") != val:
                        setattr(contact, attr, val)
                        changed = True
                if changed:
                    updated_accounts += 1

            if not any(row.get(c, "") for c in _CALL_COLS):
                continue   # account-detail-only row: cases 2 and 4

            direction = (row.get("call_type", "").lower() or "outbound")
            if direction not in valid_dirs:
                direction = "outbound"
            mode = (row.get("call_mode", "").lower() or "call")
            if mode == "phone":          # dealer sheets spell it this way
                mode = "call"
            if mode == "in_person":
                mode = "visit"
            if mode not in valid_modes:
                mode = "call"

            # summary is NOT NULL on crm_activities. Fall back through the
            # columns most likely to carry the gist before giving up, so a row
            # that plainly is a call is never rejected over a blank cell.
            summary = (row.get("summary", "") or row.get("items_discussed", "")
                       or row.get("notes", "")
                       or f"{mode} on {row.get('call_date', '') or 'unspecified date'}")

            # performed_by is the rep the row names, not whoever ran the upload
            # — crediting the uploader would pile the whole file onto one
            # account and hide each call from the rep who actually made it.
            row_agent = row.get("assigned_to", "")
            db.add(CRMActivity(
                contact_id=contact.id,
                activity_type=mode,
                direction=direction,
                summary=summary,
                outcome=row.get("call_outcome", "").lower() or None,
                performed_by=row_agent or current_user.username,
                activity_date=_to_date(row.get("call_date", "")) or app_now(),
                next_followup=_to_date(row.get("next_followup_date", "")),
                followup_assigned_to=row_agent or None,
            ))
            logged += 1
        except Exception as exc:
            errors.append(f"Row {i}: {exc}")

    await audit(db, action="CRM_ACCOUNT_BULK_UPLOAD", user=current_user,
                table_name="crm_contacts", record_id="bulk",
                new_value={"created_accounts": created_accounts,
                           "updated_accounts": updated_accounts,
                           "calls_logged": logged,
                           "skipped": len(errors), "rows": len(rows)})
    await db.commit()

    return templates.TemplateResponse("bulk_upload/result.html", {
        "request": request, "current_user": current_user,
        "upload_type": " · ".join(
            ["Accounts"]
            + ([f"{created_accounts} account(s) created"] if created_accounts else [])
            + ([f"{updated_accounts} account(s) updated"] if updated_accounts else [])
            + ([f"{logged} call(s) logged"] if logged else [])),
        "inserted": created_accounts + updated_accounts + logged,
        "errors": errors,
        "back_url": "/crm/contacts",
    })


# ── NEW ───────────────────────────────────────────────────────────────────────

async def _gst_owner(db, gstin, exclude_id=None):
    """The live (untrashed) Account already using this GST number, if any.
    GST is unique per Account (2026-11-11, Pankaj) — checked here in the app;
    there is no database unique index on crm_contacts.gstin."""
    g = (gstin or "").strip().upper()
    if not g:
        return None
    q = select(CRMContact).where(func.upper(func.trim(CRMContact.gstin)) == g, CRMContact.is_trashed.isnot(True))
    if exclude_id:
        q = q.where(CRMContact.id != exclude_id)
    return (await db.execute(q.limit(1))).scalars().first()


@router.get("/gst-check")
async def gst_check(gstin: str = "", exclude: str = "", db: AsyncSession = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """Account form: warn as soon as a GST already on another Account is typed."""
    from fastapi.responses import JSONResponse
    try:
        ex = _uuid.UUID(exclude) if exclude else None
    except ValueError:
        ex = None
    c = await _gst_owner(db, gstin, ex)
    return JSONResponse({"taken": bool(c), "company": c.company_name if c else "",
                         "code": c.contact_code if c else "", "url": f"/crm/contacts/{c.id}" if c else ""})


@router.get("/new", response_class=HTMLResponse)
async def new_contact_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    users = (await db.execute(
        select(User).where(User.status == True).order_by(User.full_name)
    )).scalars().all()
    return templates.TemplateResponse("crm/contacts/form.html", {
        "request": request, "current_user": current_user,
        "contact": None, "locations": [],
        "source_types": SOURCE_TYPES, "buyer_types": BUYER_TYPES,
        "users": users, "person_roles": PERSON_ROLES,
    })


@router.post("/new")
async def create_contact(
    request: Request,
    contact_type:   str = Form(default="supplier"),
    from_entity:    str = Form(default=""),
    company_name:   str = Form(...),
    email:          str = Form(default=None),
    gstin:          str = Form(default=None),
    pan:            str = Form(default=None),
    address:        str = Form(default=None),
    city:           str = Form(default=None),
    state:          str = Form(default=None),
    pincode:        str = Form(default=None),
    source_type:    str = Form(default=None),
    buyer_type:     str = Form(default=None),
    credit_limit:   str = Form(default=""),   # str: an Add-only role sends it blank (2026-11-03)
    credit_days:    str = Form(default=""),   # Credit Days (2026-11-10)
    tags:           str = Form(default=None),
    notes:          str = Form(default=None),
    status:         str = Form(default="active"),
    assigned_to:    str = Form(default=None),
    bank_cheque_number:  str = Form(default=None),
    msme_number:         str = Form(default=None),
    director1_id_number: str = Form(default=None),
    director2_id_number: str = Form(default=None),
    gstin_doc:      UploadFile = File(default=None),
    pan_doc:        UploadFile = File(default=None),
    bank_cheque_doc:UploadFile = File(default=None),
    msme_doc:       UploadFile = File(default=None),
    director1_doc:  UploadFile = File(default=None),
    director2_doc:  UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _perm: User = Depends(require_module_perm("crm_contacts", "add")),
):
    # GST is unique per Account (2026-11-11, Pankaj)
    _dup = await _gst_owner(db, gstin)
    if _dup:
        from urllib.parse import quote_plus
        return RedirectResponse(url="/crm/contacts/new?error=" + quote_plus(
            f"GST {_kyc_up(gstin)} is already used by {_dup.company_name} ({_dup.contact_code}) — not saved"), status_code=302)
    # Contact person / phone / whatsapp now live per-person in each Location's Contact Numbers.
    location_rows = _parse_locations(await request.form())
    from utils.master_data import clean_from_entity
    from_entity = await clean_from_entity(db, from_entity)   # From Entity (2026-11-10)
    for _attempt in range(3):
        code = await _next_code(db)
        contact = CRMContact(
            contact_code=code, contact_type=contact_type, from_entity=from_entity,
            company_name=company_name,
            email=email or None, gstin=_kyc_up(gstin), pan=_kyc_up(pan),
            address=address or None, city=city or None,
            state=state or None, pincode=pincode or None,
            source_type=source_type or None, buyer_type=buyer_type or None,
            credit_limit=_credit_new(current_user, 'crm_contacts', credit_limit),
            credit_days=_credit_days(credit_days),
            tags=tags or None, notes=notes or None, status=status,
            assigned_to=assigned_to or current_user.username,
            bank_cheque_number=_kyc_up(bank_cheque_number),
            msme_number=_kyc_up(msme_number),
            director1_id_number=_kyc_up(director1_id_number),
            director2_id_number=_kyc_up(director2_id_number),
            gstin_doc_path=await _save_kyc_upload(gstin_doc),
            pan_doc_path=await _save_kyc_upload(pan_doc),
            bank_cheque_doc_path=await _save_kyc_upload(bank_cheque_doc),
            msme_doc_path=await _save_kyc_upload(msme_doc),
            director1_doc_path=await _save_kyc_upload(director1_doc),
            director2_doc_path=await _save_kyc_upload(director2_doc),
            created_by=current_user.username,
        )
        contact.apply_account_status()
        db.add(contact)
        try:
            await db.flush()   # assigns contact.id; raises on duplicate code
        except IntegrityError:
            await db.rollback()
            continue
        for i, loc in enumerate(location_rows):
            location = CRMContactLocation(
                contact_id=contact.id,
                contact_email=loc["email"] or None,
                address=loc["address"] or None,
                city=loc["city"] or None,
                state=loc["state"] or None,
                sort_order=i,
            )
            db.add(location)
            await db.flush()   # assigns location.id
            for j, (rl, nm, ph, em) in enumerate(loc["numbers"]):
                db.add(CRMContactNumber(
                    contact_id=contact.id, location_id=location.id,
                    person_role=rl or None, person_name=nm or None,
                    phone=ph or None, email=em or None, sort_order=j,
                ))
        await audit(db, user=current_user, action="ACCOUNT_CREATED", table_name="crm_contacts",
                    record_id=str(contact.id), new_value={"company_name": contact.company_name,
                    "contact_type": contact.contact_type}, request=request)
        await db.commit()
        return RedirectResponse(url="/crm/contacts?success=Contact+created", status_code=302)
    return RedirectResponse(url="/crm/contacts?error=Failed+to+generate+unique+contact+code,+please+retry", status_code=302)


# ── PROFILE ───────────────────────────────────────────────────────────────────

@router.get("/{contact_id}", response_class=HTMLResponse)
async def contact_profile(
    request: Request,
    contact_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMContact).where(CRMContact.id == contact_id))
    contact = result.scalar_one_or_none()
    if not contact:
        return RedirectResponse(url="/crm/contacts?error=Contact+not+found", status_code=302)

    acts_r = await db.execute(
        select(CRMActivity)
        .where(CRMActivity.contact_id == contact.id)
        .order_by(CRMActivity.activity_date.desc()).limit(20)
    )
    activities = acts_r.scalars().all()

    deals_r = await db.execute(
        select(CRMSourcingDeal)
        .where(CRMSourcingDeal.contact_id == contact.id)
        .order_by(CRMSourcingDeal.created_at.desc())
    )
    sourcing_deals = deals_r.scalars().all()

    opps_r = await db.execute(
        select(CRMSalesOpportunity)
        .where(CRMSalesOpportunity.contact_id == contact.id)
        .order_by(CRMSalesOpportunity.created_at.desc())
    )
    sales_opps = opps_r.scalars().all()

    # ── Trade Partner bids for this Account ─────────────────────────────────
    # Portal logins hang off Dealer rows, which link back here via
    # crm_contact_id. One Account can have several dealer logins, so collect
    # them all before querying bids.
    from models.dealers import Dealer
    from models.partner import PartnerBid
    from models.lot import Lot

    dealer_ids = [d for (d,) in (await db.execute(
        select(Dealer.id).where(Dealer.crm_contact_id == contact.id)
    )).all()]

    lost_bids, won_bid_lots = [], {}
    if dealer_ids:
        bids = (await db.execute(
            select(PartnerBid).where(
                PartnerBid.dealer_id.in_(dealer_ids),
                PartnerBid.status.in_(("lost", "won")),
            ).order_by(PartnerBid.bid_amount.desc())
        )).scalars().all()

        lot_ids = {b.lot_id for b in bids if b.lot_id}
        lots = {}
        if lot_ids:
            lots = {l.id: l for l in (await db.execute(
                select(Lot).where(Lot.id.in_(lot_ids)))).scalars().all()}

        for b in bids:
            lot = lots.get(b.lot_id) if b.lot_id else None
            if b.status == "lost":
                base = b.base_amount
                lost_bids.append({
                    "bid": b,
                    "lot_id": str(lot.id) if lot else None,
                    "lot_number": lot.lot_number if lot else "—",
                    "shortfall": (b.bid_amount - base) if base else None,
                })
            elif b.opportunity_id and lot:
                # Lets the Sales Opportunities table link the lot a won bid
                # produced — the opportunity itself has no lot FK, only a title.
                won_bid_lots[str(b.opportunity_id)] = {
                    "lot_id": str(lot.id), "lot_number": lot.lot_number,
                }

    # Purchase Deals — POs where this contact is the supplier (eager-load .contact
    # so the shared partial can show the supplier name without an async lazy load).
    pos_r = await db.execute(
        select(CRMPurchaseOrder)
        .options(selectinload(CRMPurchaseOrder.contact))
        .where(CRMPurchaseOrder.contact_id == contact.id)
        .order_by(CRMPurchaseOrder.created_at.desc())
    )
    purchase_orders = pos_r.scalars().all()

    # ── Quotes Generated ─────────────────────────────────────────────────────
    # Every quote raised for this Account, summarised by its line items. The
    # Opportunity number comes from the Buyer Deal that points at the quote —
    # a quote written straight from the Account has none, so it shows "—".
    from models.crm import CRMQuote
    from routers.crm_quotes import quote_summary_rows, active_terms_by_type
    from services.opportunity_lot import lots_won_by_contact

    quotes = (await db.execute(
        select(CRMQuote).where(CRMQuote.contact_id == contact.id)
        .order_by(CRMQuote.created_at.desc())
    )).scalars().all()
    quote_rows = await quote_summary_rows(db, quotes)
    terms_by_type = await active_terms_by_type(db)
    from models.company import Company
    companies = (await db.execute(
        select(Company).where(Company.is_active == True).order_by(Company.company_name)
    )).scalars().all()
    # Lots this Account won at auction — the "Quote for Bid Won" picker.
    won_lots = await lots_won_by_contact(db, contact.id)

    source_map = dict(SOURCE_TYPES)
    buyer_map  = dict(BUYER_TYPES)

    # ── Scorecard stats ───────────────────────────────────────────────────────
    scorecard = {
        "total_deals":   len(sourcing_deals),
        "won_deals":     sum(1 for d in sourcing_deals if d.stage == "won"),
        "lost_deals":    sum(1 for d in sourcing_deals if d.stage == "lost"),
        "open_deals":    sum(1 for d in sourcing_deals if d.stage not in ("won", "lost")),
        "pipeline_value": sum(float(d.our_offer_total or d.asking_price_total or 0)
                              for d in sourcing_deals if d.stage not in ("won", "lost")),
        "won_value":     sum(float(d.final_price_total or d.our_offer_total or 0)
                              for d in sourcing_deals if d.stage == "won"),
        "total_opps":    len(sales_opps),
        "won_opps":      sum(1 for o in sales_opps if o.stage == "won"),
        "open_opps":     sum(1 for o in sales_opps if o.stage not in ("won", "lost")),
        "total_activities": 0,  # filled below
        "call_count":    sum(1 for a in activities if a.activity_type == "call"),
        "whatsapp_count":sum(1 for a in activities if a.activity_type == "whatsapp"),
        "visit_count":   sum(1 for a in activities if a.activity_type == "visit"),
    }
    # total activity count (not limited to 20)
    act_count_r = await db.execute(
        select(func.count(CRMActivity.id)).where(CRMActivity.contact_id == contact.id)
    )
    scorecard["total_activities"] = act_count_r.scalar() or 0
    scorecard["win_rate"] = (
        round(scorecard["won_deals"] / scorecard["total_deals"] * 100)
        if scorecard["total_deals"] > 0 else None
    )

    _can_vc = await can_verify_credit(db, contact.id, current_user)
    _vc_tot = await credit_deal_totals(db, contact.id)
    from services.record_history import record_history   # History card (2026-11-09)
    _history = await record_history(db, contact.id)
    return templates.TemplateResponse("crm/contacts/profile.html", {
        "history": _history,
        "can_verify_credit": _can_vc, "credit_totals": _vc_tot,
        "request": request, "current_user": current_user,
        "contact": contact, "activities": activities,
        "sourcing_deals": sourcing_deals, "sales_opps": sales_opps,
        "lost_bids": lost_bids, "won_bid_lots": won_bid_lots,
        "purchase_orders": purchase_orders,
        "quote_rows": quote_rows, "terms_by_type": terms_by_type,
        "companies": companies,
        "won_lots": won_lots,
        "source_map": source_map, "buyer_map": buyer_map,
        "scorecard": scorecard,
    })


# ── EDIT ─────────────────────────────────────────────────────────────────────

@router.get("/{contact_id}/edit", response_class=HTMLResponse)
async def edit_contact_form(
    request: Request,
    contact_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMContact).where(CRMContact.id == contact_id))
    contact = result.scalar_one_or_none()
    if not contact:
        return RedirectResponse(url="/crm/contacts?error=Not+found", status_code=302)
    locs_r = await db.execute(
        select(CRMContactLocation)
        .where(CRMContactLocation.contact_id == contact.id)
        .options(selectinload(CRMContactLocation.contact_numbers))
        .order_by(CRMContactLocation.sort_order)
    )
    locations = locs_r.scalars().all()
    users = (await db.execute(
        select(User).where(User.status == True).order_by(User.full_name)
    )).scalars().all()
    return templates.TemplateResponse("crm/contacts/form.html", {
        "request": request, "current_user": current_user,
        "contact": contact, "locations": locations,
        "source_types": SOURCE_TYPES, "buyer_types": BUYER_TYPES,
        "users": users, "person_roles": PERSON_ROLES,
    })


@router.post("/{contact_id}/edit")
async def update_contact(
    request: Request,
    contact_id: str,
    contact_type:   str = Form(default="supplier"),
    from_entity:    str = Form(default=""),
    company_name:   str = Form(...),
    email:          str = Form(default=None),
    gstin:          str = Form(default=None),
    pan:            str = Form(default=None),
    address:        str = Form(default=None),
    city:           str = Form(default=None),
    state:          str = Form(default=None),
    pincode:        str = Form(default=None),
    source_type:    str = Form(default=None),
    buyer_type:     str = Form(default=None),
    credit_limit:   str = Form(default=""),   # str: an Add-only role sends it blank (2026-11-03)
    credit_days:    str = Form(default=""),   # Credit Days (2026-11-10)
    tags:           str = Form(default=None),
    notes:          str = Form(default=None),
    status:         str = Form(default="active"),
    assigned_to:    str = Form(default=None),
    bank_cheque_number:  str = Form(default=None),
    msme_number:         str = Form(default=None),
    director1_id_number: str = Form(default=None),
    director2_id_number: str = Form(default=None),
    gstin_doc:      UploadFile = File(default=None),
    pan_doc:        UploadFile = File(default=None),
    bank_cheque_doc:UploadFile = File(default=None),
    msme_doc:       UploadFile = File(default=None),
    director1_doc:  UploadFile = File(default=None),
    director2_doc:  UploadFile = File(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMContact).where(CRMContact.id == contact_id))
    contact = result.scalar_one_or_none()
    if not contact:
        return RedirectResponse(url="/crm/contacts?error=Not+found", status_code=302)

    # GST is unique per Account (2026-11-11, Pankaj)
    _dup = await _gst_owner(db, gstin, contact.id)
    if _dup:
        from urllib.parse import quote_plus
        return RedirectResponse(url=f"/crm/contacts/{contact.id}/edit?error=" + quote_plus(
            f"GST {_kyc_up(gstin)} is already used by {_dup.company_name} ({_dup.contact_code}) — not saved"), status_code=302)
    contact.contact_type = contact_type
    from utils.master_data import clean_from_entity
    contact.from_entity = await clean_from_entity(db, from_entity, contact.from_entity)
    contact.company_name = company_name
    contact.email = email or None
    # contact_person / phone / whatsapp are no longer on the form (they live in
    # Contact Numbers now) — existing DB values are left untouched, not wiped.
    contact.gstin = _kyc_up(gstin)
    contact.pan = _kyc_up(pan)
    contact.address = address or None
    contact.city = city or None
    contact.state = state or None
    contact.pincode = pincode or None
    contact.source_type = source_type or None
    contact.buyer_type = buyer_type or None
    contact.credit_limit = _credit_edit(current_user, 'crm_contacts', credit_limit, contact.credit_limit)
    contact.credit_days = _credit_days(credit_days)
    contact.tags = tags or None
    contact.notes = notes or None
    contact.status = status
    contact.apply_account_status()
    contact.assigned_to = assigned_to
    contact.bank_cheque_number = _kyc_up(bank_cheque_number)
    contact.msme_number = _kyc_up(msme_number)
    contact.director1_id_number = _kyc_up(director1_id_number)
    contact.director2_id_number = _kyc_up(director2_id_number)
    # Uploads only overwrite the stored document when a new file is actually
    # selected — leaving the existing one in place otherwise.
    gstin_name = await _save_kyc_upload(gstin_doc)
    if gstin_name:
        contact.gstin_doc_path = gstin_name
    pan_name = await _save_kyc_upload(pan_doc)
    if pan_name:
        contact.pan_doc_path = pan_name
    cheque_name = await _save_kyc_upload(bank_cheque_doc)
    if cheque_name:
        contact.bank_cheque_doc_path = cheque_name
    msme_name = await _save_kyc_upload(msme_doc)
    if msme_name:
        contact.msme_doc_path = msme_name
    dir1_name = await _save_kyc_upload(director1_doc)
    if dir1_name:
        contact.director1_doc_path = dir1_name
    dir2_name = await _save_kyc_upload(director2_doc)
    if dir2_name:
        contact.director2_doc_path = dir2_name

    # Replace the Locations (and their nested Contact Numbers) with whatever the form submitted.
    location_rows = _parse_locations(await request.form())
    await db.execute(
        delete(CRMContactNumber).where(
            CRMContactNumber.location_id.in_(
                select(CRMContactLocation.id).where(CRMContactLocation.contact_id == contact.id)
            )
        )
    )
    await db.execute(delete(CRMContactLocation).where(CRMContactLocation.contact_id == contact.id))
    await db.flush()
    for i, loc in enumerate(location_rows):
        location = CRMContactLocation(
            contact_id=contact.id,
            contact_email=loc["email"] or None,
            address=loc["address"] or None,
            city=loc["city"] or None,
            state=loc["state"] or None,
            sort_order=i,
        )
        db.add(location)
        await db.flush()   # assigns location.id
        for j, (rl, nm, ph, em) in enumerate(loc["numbers"]):
            db.add(CRMContactNumber(
                contact_id=contact.id, location_id=location.id,
                person_role=rl or None, person_name=nm or None,
                phone=ph or None, email=em or None, sort_order=j,
            ))
    await audit(db, user=current_user, action="ACCOUNT_EDITED", table_name="crm_contacts",
                record_id=str(contact.id), new_value={"company_name": contact.company_name,
                "status": contact.status}, request=request)
    await db.commit()
    return RedirectResponse(url="/crm/contacts?success=Contact+updated", status_code=302)


# ── KYC VERIFICATION ─────────────────────────────────────────────────────────

@router.post("/{contact_id}/verify-kyc")
async def verify_kyc(
    contact_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    contact = (await db.execute(select(CRMContact).where(CRMContact.id == contact_id))).scalar_one_or_none()
    if not contact:
        return RedirectResponse(url="/crm/contacts?error=Not+found", status_code=302)
    contact.kyc_verified = True
    contact.kyc_verified_by = current_user.username
    contact.kyc_verified_at = app_now()
    contact.apply_account_status()
    await audit(db, user=current_user, action="ACCOUNT_KYC_VERIFIED",
                table_name="crm_contacts", record_id=str(contact.id), request=request)
    await db.commit()
    return RedirectResponse(url=f"/crm/contacts/{contact_id}?success=KYC+verified", status_code=302)


async def can_verify_credit(db, contact_id, user) -> bool:
    """Verify Credit (2026-11-06, Pankaj): the account's mapped VP/AVP or
    Team Leader (Map Team), or an admin."""
    role = getattr(user.role, "value", user.role)
    if role == "admin":
        return True
    if not getattr(user, "id", None):
        return False
    row = (await db.execute(select(CRMContactTeamMember.id).where(
        CRMContactTeamMember.contact_id == contact_id, CRMContactTeamMember.user_id == user.id,
        CRMContactTeamMember.role_bucket.in_(("vp_avp", "am_dm_gm"))).limit(1))).first()
    return row is not None


async def credit_deal_totals(db, contact_id) -> dict:
    """Deals History in the Verify Credit modal: this account's Sales Orders
    and Purchase Orders in total (cancelled ones left out)."""
    from sqlalchemy import func as _f
    from models.crm import CRMSalesOrder, CRMPurchaseOrder
    so = (await db.execute(select(_f.coalesce(_f.sum(CRMSalesOrder.total_amount), 0), _f.count()).where(
        CRMSalesOrder.contact_id == contact_id, CRMSalesOrder.status != "cancelled"))).one()
    po = (await db.execute(select(_f.coalesce(_f.sum(CRMPurchaseOrder.total_amount), 0), _f.count()).where(
        CRMPurchaseOrder.contact_id == contact_id, CRMPurchaseOrder.status != "cancelled"))).one()
    return {"so_total": so[0] or 0, "so_count": so[1], "po_total": po[0] or 0, "po_count": po[1]}


def _credit_back(contact_id, return_to, msg, ok=True):
    from urllib.parse import quote_plus
    base = return_to if (return_to or "").startswith("/activity-board") else f"/crm/contacts/{contact_id}"
    sep = "&" if "?" in base else "?"
    return RedirectResponse(url=f"{base}{sep}{'success' if ok else 'error'}={quote_plus(msg)}", status_code=302)


@router.post("/{contact_id}/verify-credit")
async def verify_credit(
    contact_id: str,
    request: Request,
    new_credit_limit: str = Form(default=""),
    reason: str = Form(default=""),
    return_to: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Verify Credit modal (2026-11-06): New Credit Limit + Reason; the old
    limit is kept on the account. Allowed for the account's mapped VP/AVP or
    Team Leader, or an admin. Same route from Account Detail and Activity Board."""
    from decimal import Decimal, InvalidOperation
    contact = (await db.execute(select(CRMContact).where(CRMContact.id == contact_id))).scalar_one_or_none()
    if not contact:
        return RedirectResponse(url="/crm/contacts?error=Not+found", status_code=302)
    if not await can_verify_credit(db, contact.id, current_user):
        return _credit_back(contact_id, return_to,
                            "Only this account's mapped AVP/VP or Team Leader can verify credit", ok=False)
    try:
        new_limit = Decimal((new_credit_limit or "").replace(",", "").strip())
        if new_limit < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return _credit_back(contact_id, return_to, "Enter the New Credit Limit (a number)", ok=False)
    if not (reason or "").strip():
        return _credit_back(contact_id, return_to, "Reason is required", ok=False)
    old = {"credit_limit": str(contact.credit_limit or 0), "credit_verified": contact.credit_verified}
    contact.credit_old_limit = contact.credit_limit or 0
    contact.credit_limit = new_limit
    contact.credit_verify_reason = reason.strip()[:2000]
    contact.credit_verified = True
    contact.credit_verified_by = current_user.username
    contact.credit_verified_at = app_now()
    contact.apply_account_status()
    await audit(db, user=current_user, action="ACCOUNT_CREDIT_VERIFIED",
                table_name="crm_contacts", record_id=str(contact.id), old_value=old,
                new_value={"credit_limit": str(new_limit), "old_limit": old["credit_limit"],
                           "reason": contact.credit_verify_reason}, request=request)
    await db.commit()
    return _credit_back(contact_id, return_to, f"Credit verified for {contact.company_name}")


# ── TRASH / RESTORE ───────────────────────────────────────────────────────────

@router.post("/{contact_id}/trash")
async def trash_contact(
    request: Request,
    contact_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMContact).where(CRMContact.id == contact_id))
    contact = result.scalar_one_or_none()
    if contact:
        contact.is_trashed = True
        await audit(db, user=current_user, action="ACCOUNT_TRASHED", table_name="crm_contacts",
                    record_id=str(contact.id), request=request)
        await db.commit()
    return RedirectResponse(url="/crm/contacts?success=Contact+moved+to+trash", status_code=302)


@router.post("/{contact_id}/restore")
async def restore_contact(
    request: Request,
    contact_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMContact).where(CRMContact.id == contact_id))
    contact = result.scalar_one_or_none()
    if contact:
        contact.is_trashed = False
        await audit(db, user=current_user, action="ACCOUNT_RESTORED", table_name="crm_contacts",
                    record_id=str(contact.id), request=request)
        await db.commit()
    return RedirectResponse(url="/crm/contacts?success=Contact+restored", status_code=302)
