"""Assign Dealer Leads — admin/manager view over the shared Dealer table with
checkbox multi-select bulk assignment to a user. Add/Bulk-upload/Sample reuse
the existing Dealers module endpoints; this module only adds the bulk-assign
workflow on top of the same Dealer records."""
from templates_config import templates
from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_

from database import get_db
from models.dealers import Dealer
from models.user import User
from auth.dependencies import get_current_user, verify_csrf, require_module_perm
from services.audit_engine import audit
from routers.dealers import (
    _tc_field_options, _sub_category_filter, _can_manage_dealers,
    _assignee_name_map, _split_ids, dealer_export_response,
    prune_unused_dealer_values, _dealer_type_filter,
)
from utils.master_data import refresh_master_cache
from utils.master_data import master_values

router = APIRouter(prefix="/assign-dealer-leads", tags=["assign-dealer-leads"])

require_view = require_module_perm("assign_dealer_leads", "enable")

UNASSIGNED = "__unassigned__"


@router.get("", response_class=HTMLResponse)
async def list_assign_dealer_leads(
    request: Request,
    q: str = Query(default=""),
    loc: str = Query(default=""),
    status: str = Query(default=""),
    assigned: str = Query(default=""),
    dealer_type: str = Query(default=""),
    sub_category: str = Query(default=""),
    source: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_view),
):
    base_query = select(Dealer).where(Dealer.trashed_at.is_(None))  # exclude soft-deleted
    if q:
        like = f"%{q}%"
        base_query = base_query.where(or_(
            Dealer.business_name.ilike(like),
            Dealer.contact_person.ilike(like),
            Dealer.phone.ilike(like),
            Dealer.city.ilike(like),
            Dealer.dealer_code.ilike(like),
            Dealer.dealer_sub_category.ilike(like),
        ))
    if loc:
        loc_like = f"%{loc}%"
        base_query = base_query.where(or_(
            Dealer.city.ilike(loc_like),
            Dealer.state.ilike(loc_like),
            Dealer.address.ilike(loc_like),
        ))
    if status:
        base_query = base_query.where(Dealer.status == status)
    base_query = _dealer_type_filter(base_query, dealer_type)
    base_query = _sub_category_filter(base_query, sub_category)
    if source:
        # Not shown as a control: lets a link (or a one-off clean-up) open just
        # the dealers from one import, e.g. ?source=Google Maps.
        base_query = base_query.where(Dealer.source.ilike(f"{source}%"))
    if assigned:
        if assigned == "__unassigned__":
            base_query = base_query.where(Dealer.assigned_to.is_(None))
        else:
            base_query = base_query.where(Dealer.assigned_to == assigned)

    dealers = (await db.execute(
        base_query.order_by(Dealer.created_at.desc().nulls_last(), Dealer.dealer_code.desc())
    )).scalars().all()

    # Active users for the Assign User modal + filter dropdown
    users_result = await db.execute(
        select(User).where(User.status == True).order_by(User.full_name)
    )
    all_users = users_result.scalars().all()

    total_count = len(dealers)
    unassigned_count = sum(1 for d in dealers if not d.assigned_to)
    # Tile cards (for the current filters). Inactive = every status other than
    # active (inactive + blacklisted), so Active + Inactive = all dealers shown.
    active_count = sum(1 for d in dealers if (d.status or "").lower() == "active")
    tc_options = await _tc_field_options(db)

    return templates.TemplateResponse("assign_dealer_leads/list.html", {
        "request": request,
        "current_user": current_user,
        "dealers": dealers,
        # Assigned column shows the user's display name (username kept in
        # data-assigned for the Unassigned / Exclude Unassigned checkboxes).
        "assignee_names": await _assignee_name_map(db, dealers),
        "all_users": all_users,
        "q": q,
        "loc": loc,
        "status": status,
        "assigned": assigned,
        "dealer_type": dealer_type,
        "sub_category": sub_category,
        "dealer_type_options": await master_values(db, "dealer_dealer_type"),
        # Delete Selected (permanent) uses /dealers/bulk-delete, gated by the
        # same check — so the button only shows to people that endpoint allows.
        "can_manage_dealers": await _can_manage_dealers(db, current_user, "/dealers/bulk-delete"),
        "total_count": total_count,
        "unassigned_count": unassigned_count,
        "assigned_count": total_count - unassigned_count,
        "active_count": active_count,
        "inactive_count": total_count - active_count,
        "whom_to_sell_options": tc_options["whom_to_sell"],
        "deals_in_options": tc_options["deals_in"],
        "call_mode_options": await master_values(db, "call_mode"),
        "call_type_options": await master_values(db, "call_type"),
    })


@router.post("/assign")
async def bulk_assign_dealers(
    request: Request,
    dealer_ids: list[str] = Form(...),
    assigned_to: str = Form(...),
    _csrf: None = Depends(verify_csrf),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_view),
    _perm: User = Depends(require_module_perm("assign_dealer_leads", "edit")),
):
    result = await db.execute(select(Dealer).where(Dealer.id.in_(dealer_ids)))
    dealers = result.scalars().all()
    # "__unassigned__" (the modal's Unassigned choice) clears the assignment.
    unassign = assigned_to == UNASSIGNED
    new_owner = None if unassign else assigned_to
    old_owners = {str(d.id): d.assigned_to for d in dealers}
    for dealer in dealers:
        dealer.assigned_to = new_owner

    await audit(
        db, user=current_user, action="DEALER_LEADS_BULK_ASSIGNED",
        table_name="dealers", record_id=f"bulk:{len(dealers)}",
        old_value={"assigned_to": old_owners} if len(old_owners) <= 2000 else None,
        new_value={"assigned_to": new_owner, "dealer_ids": [str(d.id) for d in dealers]},
        request=request,
    )
    await db.commit()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        from fastapi.responses import JSONResponse
        return JSONResponse({
            "success": True,
            "count": len(dealers),
            "assigned_to": assigned_to,
        })

    who = "nobody+(Unassigned)" if unassign else assigned_to
    return RedirectResponse(
        url=f"/assign-dealer-leads?success={len(dealers)}+dealer(s)+assigned+to+{who}",
        status_code=302,
    )


@router.post("/{dealer_id}/delete")
async def delete_dealer_lead(
    request: Request,
    dealer_id: str,
    _csrf: None = Depends(verify_csrf),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_view),
    _perm: User = Depends(require_module_perm("assign_dealer_leads", "edit")),
):
    """Soft-delete (Trash): dealer orders/calls/credit-notes reference this
    record, so business rows are never hard-deleted — set trashed_at instead so
    the dealer disappears from BOTH Assign Dealer Leads and Dealer Management."""
    from utils.timezone import app_now
    result = await db.execute(select(Dealer).where(Dealer.id == dealer_id))
    dealer = result.scalar_one_or_none()
    if not dealer:
        return RedirectResponse(url="/assign-dealer-leads?error=Dealer+not+found", status_code=302)
    if dealer.trashed_at is not None:
        return RedirectResponse(url="/assign-dealer-leads?success=Dealer+already+deleted", status_code=302)

    await audit(
        db, user=current_user, action="DEALER_TRASHED",
        table_name="dealers", record_id=str(dealer.id),
        old_value={"trashed_at": None},
        new_value={"trashed_at": "now", "trashed_by": current_user.username},
        request=request,
    )
    dealer.trashed_at = app_now()
    dealer.trashed_by = current_user.username
    # Its Sub Category / Dealer Type leave the Master Dropdown if no other
    # dealer uses them.
    removed_subs = await prune_unused_dealer_values(
        db, [(dealer.dealer_sub_category, dealer.dealer_type)], current_user, request)
    await db.commit()
    if removed_subs:
        await refresh_master_cache(db)
    return RedirectResponse(url="/assign-dealer-leads?success=Dealer+deleted", status_code=302)


@router.post("/export")
async def export_dealer_leads(
    request: Request,
    dealer_ids: str = Form(default=""),
    _csrf: None = Depends(verify_csrf),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_view),
):
    """Export button: the page sends the ids of the selected rows, or of every
    row its filters, checkboxes and search box currently show."""
    ids = _split_ids(dealer_ids)
    await audit(
        db, user=current_user, action="DEALERS_EXPORTED", table_name="dealers",
        record_id=f"export:{len(ids)}", new_value={"count": len(ids), "page": "assign_dealer_leads"},
        request=request,
    )
    await db.commit()
    return await dealer_export_response(db, ids, "dealer_leads")
