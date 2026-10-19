from templates_config import templates
import math
import re
import subprocess
import sys
from datetime import datetime
from utils.timezone import app_now, app_from_timestamp, get_app_timezone_name
from decimal import Decimal
from pathlib import Path
from fastapi import APIRouter, Depends, Form, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from database import get_db
from models.user import User, UserRole, LoginLog, UserPermission, ROLE_LABELS
from models.engines import AuditLog
from models.cost_config import CostConfig
from auth.dependencies import get_current_user, require_roles, hash_password_async, verify_csrf, require_module_perm
from services.audit_engine import audit

PERMISSION_GROUPS = {
    "Inventory & Lots": {
        "can_add_lot": "Add / Create Lots",
        "can_edit_lot": "Edit Lots",
        "can_delete_lot": "Delete Lots",
        "can_stock_in": "Register Devices (Stock In)",
        "can_bulk_upload": "Bulk Upload (CSV)",
        "can_view_all_devices": "View All Devices",
        "can_transfer_stock": "Transfer Stock Between Locations",
        "can_manage_locations": "Manage Inventory Locations",
        "can_manage_grn": "Record / Manage GRN",
    },
    "IQC & Repair": {
        "can_do_iqc": "IQC Inspection",
        "can_do_l1": "L1 Repair",
        "can_do_l2": "L2 Repair",
        "can_do_l3": "L3 Repair",
        "can_do_qc": "QC Check",
        "can_do_cosmetic": "Cosmetic / Cleaning Stage",
        "can_move_device": "Move Device Between Stages",
        "can_view_repair_history": "View Full Repair History",
    },
    "Sales & Invoices": {
        "can_create_sale": "Create Sales",
        "can_view_all_sales": "View All Sales (not just own)",
        "can_apply_discount": "Apply Discounts",
        "can_process_return": "Process Returns",
        "can_view_invoices": "View Invoices",
        "can_create_invoice": "Create / Print Invoices",
        "can_view_cost_data": "View Device Buying / Cost Data",
    },
    "CRM — Contacts & Deals": {
        "can_view_crm_contacts": "View CRM Contacts",
        "can_add_crm_contact": "Add / Edit CRM Contacts",
        "can_delete_crm_contact": "Delete / Trash CRM Contacts",
        "can_view_crm_sourcing": "View Sourcing Deals",
        "can_manage_crm_sourcing": "Create / Edit Sourcing Deals",
        "can_view_crm_sales_opp": "View CRM Sales Opportunities",
        "can_manage_crm_sales_opp": "Create / Edit Sales Opportunities",
        "can_view_crm_quotes": "View Quotes / Proformas",
        "can_create_crm_quotes": "Create / Edit Quotes",
        "can_view_purchase_orders": "View Purchase Orders",
        "can_create_purchase_orders": "Create / Edit Purchase Orders",
    },
    "Telecalling & Dealers": {
        "can_use_telecalling": "Use Telecalling Module",
        "can_view_all_calls": "View All Agent Calls (not just own)",
        "can_manage_dealers": "Manage Dealers / CRM Contacts",
        "can_send_whatsapp": "Send WhatsApp Messages",
        "can_view_crm_dashboard": "View CRM Dashboard",
        "can_view_crm_reports": "View CRM / Telecalling Reports",
    },
    "Spare Parts": {
        "can_add_spare_parts": "Add / Edit Spare Parts Stock",
        "can_consume_parts": "Record Parts Consumption",
        "can_manage_ram_tracking": "RAM Swap Tracking",
        "can_view_parts_report": "View Parts Usage Reports",
    },
    "Reports & Market": {
        "can_view_reports": "View Operational Reports",
        "can_export_data": "Export Data (CSV / Excel)",
        "can_view_market_prices": "View Market Price Reference",
        "can_view_financial_reports": "View Financial / P&L Reports",
    },
    "Attendance": {
        "can_mark_own_attendance": "Mark Own Attendance",
        "can_mark_attendance": "Mark Attendance for Others",
        "can_view_attendance_all": "View All Staff Attendance",
        "can_view_attendance_reports": "View Attendance Reports",
    },
    "System & Settings": {
        "can_access_settings": "Access System Settings",
        "can_manage_master_data": "Manage Master / Reference Data",
        "can_view_audit_log": "View Audit Log",
        "can_run_backup": "Trigger Manual DB Backup",
    },
}

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(verify_csrf)])
require_admin = require_roles(UserRole.admin)


async def _role_data(db: AsyncSession):
    """(choices, label_map) for role dropdowns/labels: built-in enum + custom roles (string keys)."""
    from models.role_permissions import CustomRole
    choices = [(r.value, ROLE_LABELS.get(r, r.value)) for r in UserRole]
    seen = {v for v, _ in choices}
    rows = (await db.execute(select(CustomRole).order_by(CustomRole.display_name))).scalars().all()
    for c in rows:
        if c.role_name not in seen:
            choices.append((c.role_name, c.display_name or c.role_name))
            seen.add(c.role_name)
    return choices, {v: l for v, l in choices}


async def _entity_options(db: AsyncSession, current: str | None = None) -> list[str]:
    """Entity dropdown for Add/Edit User: Master Data entities (same list as
    Entity Movement, minus the External Partner test entity). A user's
    current value is kept selectable even if it was later removed."""
    from utils.master_data import entity_values
    from models.master import EXTERNAL_PARTNER_TEST_ENTITY
    opts = [e for e in await entity_values(db) if e != EXTERNAL_PARTNER_TEST_ENTITY]
    if current and current not in opts:
        opts.append(current)
    return opts


@router.get("/users", response_class=HTMLResponse)
async def list_users(
    request: Request,
    role: str = Query(default=""),
    last_login_date: str = Query(default=""),
    show_inactive: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    query = select(User)
    if role:
        query = query.where(User.role == role)
    if last_login_date:
        try:
            day = datetime.strptime(last_login_date, "%Y-%m-%d").date()
            query = query.where(func.date(User.last_login) == day)
        except ValueError:
            pass
    # Delete (User Management's own soft-delete) sets status=False — without
    # this filter that row just sat there re-labeled "Inactive" instead of
    # disappearing, which read as "the button doesn't work". Same pattern as
    # is_trashed/is_active everywhere else in this app: soft-deleted rows are
    # excluded from the default list, recoverable via Show Inactive.
    show_inactive_on = show_inactive == "on"
    if not show_inactive_on:
        query = query.where(User.status == True)
    result = await db.execute(query.order_by(User.created_at.desc()))
    users = result.scalars().all()
    role_choices, role_label_map = await _role_data(db)

    today = app_now().date()
    today_login_count = (await db.execute(
        select(func.count(User.id)).where(func.date(User.last_login) == today)
    )).scalar() or 0

    all_users_result = await db.execute(select(User.username, User.full_name))
    users_by_username = {u.username: u.full_name for u in all_users_result.all()}

    return templates.TemplateResponse("admin/users.html", {
        "request": request, "users": users, "current_user": current_user,
        "role_choices": role_choices, "role_label_map": role_label_map,
        "role_filter": role, "last_login_date_filter": last_login_date,
        "show_inactive_filter": show_inactive_on,
        "today_login_count": today_login_count,
        "users_by_username": users_by_username,
    })


@router.get("/users/new", response_class=HTMLResponse)
async def new_user_form(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    role_choices, role_label_map = await _role_data(db)
    users_list = (await db.execute(
        select(User).where(User.status == True).order_by(User.full_name)
    )).scalars().all()
    return templates.TemplateResponse("admin/user_form.html", {
        "request": request, "current_user": current_user,
        "entity_options": await _entity_options(db, None),
        "role_choices": role_choices, "role_label_map": role_label_map, "edit_user": None,
        "users": users_list,
    })


@router.post("/users/new")
async def create_user(
    request: Request,
    entity: str = Form(""),
    username: str = Form(...),
    full_name: str = Form(...),
    role: str = Form(...),
    password: str = Form(...),
    whatsapp_number: str = Form(""),
    email: str = Form(""),
    designation: str = Form(""),
    reports_to: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    entity = entity.strip()
    entity_error = None if entity and entity in await _entity_options(db) else "Select the user's Entity"
    existing = await db.execute(select(User).where(User.username == username))
    if entity_error or existing.scalar_one_or_none():
        role_choices, role_label_map = await _role_data(db)
        users_list = (await db.execute(
            select(User).where(User.status == True).order_by(User.full_name)
        )).scalars().all()
        return templates.TemplateResponse("admin/user_form.html", {
            "request": request, "current_user": current_user,
            "entity_options": await _entity_options(db, None),
            "role_choices": role_choices, "role_label_map": role_label_map,
            "edit_user": None, "error": entity_error or "Username already exists",
            "users": users_list,
        })
    user = User(
        username=username, full_name=full_name, entity=entity,
        role=role, password_hash=await hash_password_async(password),
        whatsapp_number=(whatsapp_number.strip() or None),
        email=(email.strip() or None),
        designation=(designation.strip() or None),
        reports_to=(reports_to or None),
        created_by=current_user.username, status=True,
    )
    db.add(user)
    await db.flush()   # get user.id before audit
    await audit(db, action="USER_CREATED", user=current_user,
                table_name="users", record_id=str(user.id),
                new_value={"username": username, "role": role, "full_name": full_name, "entity": entity},
                request=request)
    await db.commit()
    return RedirectResponse(url="/admin/users?success=User+created", status_code=302)


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def edit_user_form(user_id: str, request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    result = await db.execute(select(User).where(User.id == user_id))
    edit_user = result.scalar_one_or_none()
    if not edit_user:
        raise HTTPException(404)
    role_choices, role_label_map = await _role_data(db)
    users_list = (await db.execute(
        select(User).where(User.status == True, User.id != user_id).order_by(User.full_name)
    )).scalars().all()
    return templates.TemplateResponse("admin/user_form.html", {
        "request": request, "current_user": current_user,
        "entity_options": await _entity_options(db, edit_user.entity),
        "role_choices": role_choices, "role_label_map": role_label_map, "edit_user": edit_user,
        "users": users_list,
    })


@router.post("/users/{user_id}/edit")
async def update_user(
    request: Request,
    user_id: str,
    full_name: str = Form(...),
    role: str = Form(...),
    username: str = Form(...),
    entity: str = Form(""),
    status: str = Form(None),
    whatsapp_number: str = Form(""),
    email: str = Form(""),
    designation: str = Form(""),
    reports_to: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404)

    entity = entity.strip()
    if not entity or entity not in await _entity_options(db, user.entity):
        role_choices, role_label_map = await _role_data(db)
        users_list = (await db.execute(
            select(User).where(User.status == True, User.id != user.id).order_by(User.full_name)
        )).scalars().all()
        return templates.TemplateResponse("admin/user_form.html", {
            "request": request, "current_user": current_user,
            "entity_options": await _entity_options(db, user.entity),
            "role_choices": role_choices, "role_label_map": role_label_map,
            "edit_user": user, "error": "Select the user's Entity",
            "users": users_list,
        }, status_code=400)

    new_username = username.strip()
    if new_username and new_username != user.username:
        dup = await db.execute(
            select(User).where(func.lower(User.username) == new_username.lower(),
                                User.id != user.id)
        )
        if dup.scalar_one_or_none():
            role_choices, role_label_map = await _role_data(db)
            users_list = (await db.execute(
                select(User).where(User.status == True, User.id != user.id).order_by(User.full_name)
            )).scalars().all()
            return templates.TemplateResponse("admin/user_form.html", {
                "request": request, "current_user": current_user,
                "entity_options": await _entity_options(db, user.entity),
                "role_choices": role_choices, "role_label_map": role_label_map,
                "edit_user": user,
                "error": f"Username '{new_username}' is already taken.",
                "users": users_list,
            }, status_code=400)
        user.username = new_username

    old_vals = {"full_name": user.full_name, "role": user.role.value, "status": user.status,
                "entity": user.entity}
    user.entity = entity
    user.full_name = full_name
    user.role = role
    user.whatsapp_number = whatsapp_number.strip() or None
    user.email = email.strip() or None
    user.designation = designation.strip() or None
    user.reports_to = reports_to or None
    new_status = (status == "on")
    user.status = new_status
    action = "USER_DISABLED" if not new_status and old_vals["status"] else "USER_UPDATED"
    await audit(db, action=action, user=current_user,
                table_name="users", record_id=str(user.id),
                old_value=old_vals,
                new_value={"full_name": full_name, "role": role, "status": new_status, "entity": entity})
    await db.commit()
    return RedirectResponse(url="/admin/users?success=User+updated", status_code=302)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Soft-delete only — sets status=False, same as unchecking "Account
    Active" on the Edit User form. This project never hard-deletes a user
    row: login history, audit logs, and anything else with a users.id FK
    stays intact and attributable."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404)
    if user.id == current_user.id:
        raise HTTPException(400, "You cannot delete your own account.")

    old_vals = {"full_name": user.full_name, "role": user.role.value, "status": user.status}
    user.status = False
    await audit(db, action="USER_DELETED", user=current_user,
                table_name="users", record_id=str(user.id),
                old_value=old_vals, new_value={"status": False})
    await db.commit()
    return RedirectResponse(url=f"/admin/users?success=User+{user.username}+deleted", status_code=302)


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    new_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404)
    user.password_hash = await hash_password_async(new_password)
    await db.commit()
    return RedirectResponse(url="/admin/users?success=Password+reset", status_code=302)


@router.get("/users/{user_id}/permissions", response_class=HTMLResponse)
async def user_permissions_form(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(404)

    perms_result = await db.execute(
        select(UserPermission).where(UserPermission.user_id == user_id, UserPermission.granted == True)
    )
    granted_permissions = {p.permission for p in perms_result.scalars().all()}

    return templates.TemplateResponse("admin/permissions.html", {
        "request": request,
        "current_user": current_user,
        "target_user": target_user,
        "permission_groups": PERMISSION_GROUPS,
        "granted_permissions": granted_permissions,
    })


@router.post("/users/{user_id}/permissions")
async def save_permissions(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(404)

    form = await request.form()
    selected = set(form.getlist("permissions"))

    # Delete all existing permissions for this user
    await db.execute(delete(UserPermission).where(UserPermission.user_id == user_id))

    # Re-insert selected ones
    for perm in selected:
        db.add(UserPermission(
            user_id=target_user.id,
            permission=perm,
            granted=True,
            granted_by=current_user.username,
        ))
    await db.commit()
    return RedirectResponse(url=f"/admin/users?success=Permissions+saved+for+{target_user.full_name.replace(' ', '+')}", status_code=302)


@router.get("/login-log", response_class=HTMLResponse)
async def login_log(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    result = await db.execute(
        select(LoginLog, User.username, User.full_name)
        .join(User, LoginLog.user_id == User.id)
        .order_by(LoginLog.timestamp.desc())
    )
    logs = result.all()
    return templates.TemplateResponse("admin/login_log.html", {
        "request": request, "logs": logs, "current_user": current_user
    })


@router.get("/audit-log", response_class=HTMLResponse)
async def audit_log_view(
    request: Request,
    username: str = Query(default=""),
    action: str = Query(default=""),
    table_name: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    q = select(AuditLog)
    if username:
        q = q.where(AuditLog.username.ilike(f"%{username}%"))
    if action:
        q = q.where(AuditLog.action.ilike(f"%{action}%"))
    if table_name:
        q = q.where(AuditLog.table_name == table_name)

    logs = (await db.execute(
        q.order_by(AuditLog.timestamp.desc())
    )).scalars().all()
    total_count = len(logs)

    # Distinct table names for the filter dropdown
    table_names_result = await db.execute(
        select(AuditLog.table_name).distinct().order_by(AuditLog.table_name)
    )
    table_names = [r[0] for r in table_names_result.all() if r[0]]

    return templates.TemplateResponse("admin/audit_log.html", {
        "request": request,
        "current_user": current_user,
        "logs": logs,
        "total_count": total_count,
        "username": username,
        "action": action,
        "table_name": table_name,
        "table_names": table_names,
    })


# ── Cost Config ────────────────────────────────────────────────────────────

COST_CONFIG_DEFS = [
    ("repair_labour_rate", "Labour Rate per Repair Attempt (Rs)",
     "Used when engineer leaves cost field blank. Default: Rs 150"),
    # Label renamed 2026-09-14 (was "Cosmetic Rework Rate per Device (Rs)") —
    # now also feeds Final QC's "Other Price" row (routers/cosmetic.py). Key
    # stays cosmetic_rate: routers/dashboard.py, routers/stock.py and
    # db_validator.py all still read it by this exact key.
    ("cosmetic_rate", "Other Rates per Device (Rs)",
     "Applied per device that passed through cleaning/rework/other stage. Default: Rs 50"),
    ("gst_rate_intra", "GST Rate — Intra-State (% total)",
     "CGST + SGST for same-state sales. Default: 18 (9+9). Enter total %, system splits equally."),
    ("gst_rate_inter", "GST Rate — Inter-State IGST (%)",
     "IGST for out-of-state sales. Default: 18. Typically same as intra total."),
    ("default_paid_repair", "Default Paid Repair Cost (Rs)",
     "Shown on Process Return when the device's Warranty Status is Out of Warranty. Default: Rs 1500"),
]

# Ready to Sale margins (services/ready_pricing.py). Each is EITHER an Amount
# or a Percentage, never both — stored as two cost_config keys
# (<prefix>_amount / <prefix>_percent) with the unused one zeroed on save.
MARGIN_DEFS = [
    ("margin_item", "Margin for Individual Item",
     "Added to every Ready to Sale tag's Min Selling Price (Unit price + Other Rates + this margin). "
     "A percentage is applied to the tag's unit price."),
    ("margin_asis", "Margin for As-Is Lot",
     "Added to every As-Is Lot's Min Selling Price (Lot Price + this margin). "
     "A percentage is applied to the lot price."),
]


def _margin_state(rows: dict, prefix: str) -> dict:
    amt = rows.get(f"{prefix}_amount")
    pct = rows.get(f"{prefix}_percent")
    a = amt.value if amt is not None and amt.value else Decimal("0")
    p = pct.value if pct is not None and pct.value else Decimal("0")
    if p > 0 and a <= 0:
        return {"type": "percent", "value": p}
    return {"type": "amount", "value": a}


async def _upsert_cost_config(db: AsyncSession, key: str, value: Decimal, username: str) -> None:
    row = (await db.execute(select(CostConfig).where(CostConfig.key == key))).scalar_one_or_none()
    if row:
        row.value = value
        row.updated_by = username
    else:
        db.add(CostConfig(key=key, value=value, updated_by=username))


@router.get("/cost-config", response_class=HTMLResponse)
async def cost_config_view(
    request: Request,
    db: AsyncSession = Depends(get_db),
    # Was require_admin (hard role check) — the Module Permission matrix has
    # a "Cost Config" entry admins can grant to other roles, but that entry
    # was a dead control: this route never consulted it, so a granted
    # non-admin role still got 403. require_module_perm reads the same
    # matrix require_admin bypassed (admin still always passes — see its
    # own "admin always passes" behaviour).
    current_user: User = Depends(require_module_perm("cost_config")),
):
    result = await db.execute(select(CostConfig))
    rows = {r.key: r for r in result.scalars().all()}
    return templates.TemplateResponse("admin/cost_config.html", {
        "request": request,
        "current_user": current_user,
        "defs": COST_CONFIG_DEFS,
        "rows": rows,
        "margin_defs": MARGIN_DEFS,
        "margins": {prefix: _margin_state(rows, prefix) for prefix, *_ in MARGIN_DEFS},
    })


@router.post("/cost-config")
async def cost_config_save(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_module_perm("cost_config", "edit")),
):
    form = await request.form()
    for key, _label, _hint in COST_CONFIG_DEFS:
        raw = form.get(key, "").strip()
        try:
            new_val = Decimal(raw)
        except Exception:
            continue
        result = await db.execute(select(CostConfig).where(CostConfig.key == key))
        row = result.scalar_one_or_none()
        if row:
            row.value = new_val
            row.updated_by = current_user.username
        else:
            db.add(CostConfig(key=key, value=new_val, updated_by=current_user.username))

    audited_margins = {}
    for prefix, _label, _hint in MARGIN_DEFS:
        mtype = "percent" if form.get(f"{prefix}_type") == "percent" else "amount"
        raw = (form.get(f"{prefix}_value") or "").strip()
        try:
            val = Decimal(raw) if raw else Decimal("0")
        except Exception:
            continue
        if val < 0:
            continue
        # One field, one mode: the unused key is zeroed so a switch from
        # Amount to Percentage (or back) can never leave both populated.
        zero = Decimal("0")
        amount, percent = (zero, val) if mtype == "percent" else (val, zero)
        await _upsert_cost_config(db, f"{prefix}_amount", amount, current_user.username)
        await _upsert_cost_config(db, f"{prefix}_percent", percent, current_user.username)
        audited_margins[prefix] = {"type": mtype, "value": str(val)}

    await audit(db, action="COST_CONFIG_UPDATE", user=current_user,
                table_name="cost_config",
                new_value={**{k: form.get(k) for k, _, _ in COST_CONFIG_DEFS},
                           **audited_margins},
                request=request)
    await db.commit()
    return RedirectResponse(url="/admin/cost-config?success=Rates+saved", status_code=302)


# ── Backup ────────────────────────────────────────────────────────────────

_BACKUP_DIR = Path(__file__).resolve().parent.parent / "backups"
_BACKUP_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backup_db.py"


@router.get("/backup-status")
async def backup_status(current_user: User = Depends(require_admin)):
    """Return info about the most recent backup file."""
    if not _BACKUP_DIR.exists():
        return JSONResponse({"status": "no_backups", "last_backup": None})

    backups = sorted(
        _BACKUP_DIR.glob("oxypc_*.sql.gz"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not backups:
        return JSONResponse({"status": "no_backups", "last_backup": None})

    latest = backups[0]
    # app_from_timestamp, not utcfromtimestamp: app_now() is app-local, so a UTC
    # mtime made every backup look older by the UTC offset (5.5h on IST).
    mtime   = app_from_timestamp(latest.stat().st_mtime)
    age_h   = round((app_now() - mtime).total_seconds() / 3600, 1)
    size_mb = round(latest.stat().st_size / (1024 * 1024), 2)

    return JSONResponse({
        "status": "ok",
        "last_backup": {
            "filename":  latest.name,
            "size_mb":   size_mb,
            "age_hours": age_h,
            "taken_at":  mtime.strftime("%Y-%m-%d %H:%M ") + get_app_timezone_name(),
        },
        "total_backups": len(backups),
    })


# Filename must match this pattern exactly — guards backup-download/backup-delete
# against path traversal (../, absolute paths, or any file outside _BACKUP_DIR).
_BACKUP_FILENAME_RE = re.compile(r"^oxypc_\d{8}_\d{6}\.sql\.gz$")


def _safe_backup_path(filename: str) -> Path:
    if not _BACKUP_FILENAME_RE.match(filename or ""):
        raise HTTPException(400, "Invalid backup filename")
    path = _BACKUP_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "Backup file not found")
    return path


@router.get("/backup-list")
async def backup_list(current_user: User = Depends(require_admin)):
    """Return every backup file on disk (newest first) — each Run Backup Now
    click creates a new distinct timestamped file, none are ever overwritten."""
    if not _BACKUP_DIR.exists():
        return JSONResponse({"backups": []})

    files = sorted(
        _BACKUP_DIR.glob("oxypc_*.sql.gz"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    backups = []
    for f in files:
        mtime = app_from_timestamp(f.stat().st_mtime)
        backups.append({
            "filename":  f.name,
            "size_mb":   round(f.stat().st_size / (1024 * 1024), 2),
            "age_hours": round((app_now() - mtime).total_seconds() / 3600, 1),
            "taken_at":  mtime.strftime("%Y-%m-%d %H:%M ") + get_app_timezone_name(),
        })
    return JSONResponse({"backups": backups})


@router.get("/backup-download/{filename}")
async def backup_download(filename: str, current_user: User = Depends(require_admin)):
    """Download a specific backup file."""
    from fastapi.responses import FileResponse
    path = _safe_backup_path(filename)
    return FileResponse(path, filename=filename, media_type="application/gzip")


@router.post("/backup-delete/{filename}")
async def backup_delete(
    filename: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete a specific backup file."""
    path = _safe_backup_path(filename)
    path.unlink()

    await audit(db, action="DELETE_BACKUP", user=current_user,
                table_name="system", new_value={"filename": filename},
                request=request)
    await db.commit()

    return JSONResponse({"success": True, "filename": filename})


@router.post("/backup-now")
async def backup_now(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Trigger an immediate database backup synchronously."""
    python = sys.executable
    # --no-prune (2026-10-19): the script's default also deletes every backup
    # older than 30 days; a button press must only ADD a backup — same policy
    # as the nightly job (deploy/oxypc-backup.sh), which never prunes.
    result = subprocess.run(
        [python, str(_BACKUP_SCRIPT), "--no-prune"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    success = result.returncode == 0
    output  = (result.stdout + result.stderr).strip()

    await audit(db, action="MANUAL_BACKUP", user=current_user,
                table_name="system",
                new_value={"success": success, "output": output[:500]},
                request=request)
    await db.commit()

    msg = "Backup+completed" if success else "Backup+failed"
    return RedirectResponse(
        url=f"/admin/users?{'success' if success else 'error'}={msg}",
        status_code=302,
    )
