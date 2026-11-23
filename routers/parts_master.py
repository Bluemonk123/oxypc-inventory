"""Parts dropdowns on the Parts GRN page (2026-11-23, Pankaj).

The three parts lists — Spare Part Brands, Spare Part Names, Parts: Part
Category — are shown as cards under the Parts GRN table so the Parts Manager
(who knows the parts best) can keep them up to date, not only the Admin on
Master Data. Both pages read and write the SAME master_data rows, so a change
made on either page shows on both at once (refresh_master_cache after every
save). Only these three categories can be changed through these routes.
"""
import csv
import io
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_roles, verify_csrf
from database import get_db
from models.master import MasterData
from models.user import User, UserRole
from services.audit_engine import audit

router = APIRouter(prefix="/parts-grn/master", tags=["parts_master"], dependencies=[Depends(verify_csrf)])
allowed = require_roles(UserRole.admin, UserRole.spare_parts_manager)

PARTS_LISTS = [  # (Master Data category, label) — same labels as Master Data → Repair & QC
    ("spare_part_brand", "Spare Part Brands"),
    ("part_category", "Spare Part Names"),
    ("iqc_part_category", "Parts: Part Category"),
]
_CATS = {k for k, _ in PARTS_LISTS}


def _perm(user, action):
    from models.role_permissions import has_perm
    role = getattr(user.role, "value", user.role)
    if role != "admin" and not has_perm(role, "spare_parts_purchase", action):
        raise HTTPException(403, f"Your role does not have {action.title()} permission for Parts GRN.")


def _back(msg="", err=""):
    q = f"success={quote(msg)}" if msg else f"error={quote(err)}"
    return RedirectResponse(url=f"/parts-grn?{q}#partsLists", status_code=302)


async def _refresh(db):
    from utils.master_data import refresh_master_cache
    await refresh_master_cache(db)


async def parts_lists(db):
    """[{key, label, items}] for the Parts GRN page cards."""
    rows = (await db.execute(select(MasterData).where(MasterData.category.in_(_CATS))
                             .order_by(MasterData.display_order, MasterData.value))).scalars().all()
    by = {k: [] for k in _CATS}
    for r in rows:
        by[r.category].append(r)
    return [{"key": k, "label": lab, "items": by[k]} for k, lab in PARTS_LISTS]


async def _item(db, item_id):
    import uuid
    try:
        iid = uuid.UUID(str(item_id))
    except ValueError:
        iid = None
    it = (await db.execute(select(MasterData).where(MasterData.id == iid))).scalar_one_or_none() if iid else None
    if it is None or it.category not in _CATS:
        raise HTTPException(404, "Value not found in the parts lists")
    return it


@router.post("/add")
async def parts_master_add(request: Request, category: str = Form(...), value: str = Form(...),
                           description: str = Form(""), db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(allowed)):
    _perm(current_user, "add")
    if category not in _CATS:
        raise HTTPException(400, "Not a parts list")
    value = value.strip()[:200]
    if not value:
        return _back(err="Type a value.")
    dup = (await db.execute(select(MasterData.id).where(
        MasterData.category == category, func.lower(MasterData.value) == value.lower()))).first()
    if dup:
        return _back(err=f"{value} is already in the list.")
    db.add(MasterData(category=category, value=value, description=description.strip() or None))
    await audit(db, user=current_user, action="PARTS_LIST_VALUE_ADDED", table_name="master_data",
                record_id=category, new_value={"category": category, "value": value}, request=request)
    await db.commit()
    await _refresh(db)
    return _back(msg=f"{value} added")


@router.post("/{item_id}/edit")
async def parts_master_edit(request: Request, item_id: str, value: str = Form(...), description: str = Form(""),
                            db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed)):
    _perm(current_user, "edit")
    it = await _item(db, item_id)
    value = value.strip()[:200]
    if not value:
        return _back(err="Type a value.")
    dup = (await db.execute(select(MasterData.id).where(
        MasterData.category == it.category, func.lower(MasterData.value) == value.lower(),
        MasterData.id != it.id))).first()
    if dup:
        return _back(err=f"{value} is already in the list.")
    old = {"value": it.value, "description": it.description}
    it.value, it.description = value, description.strip() or None
    await audit(db, user=current_user, action="PARTS_LIST_VALUE_EDITED", table_name="master_data",
                record_id=str(it.id), old_value=old, new_value={"value": it.value, "description": it.description},
                request=request)
    await db.commit()
    await _refresh(db)
    return _back(msg=f"{value} saved")


@router.post("/{item_id}/toggle")
async def parts_master_toggle(request: Request, item_id: str, db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(allowed)):
    _perm(current_user, "edit")                      # Active / Inactive counts as edit
    it = await _item(db, item_id)
    it.is_active = not it.is_active
    await audit(db, user=current_user, action="PARTS_LIST_VALUE_TOGGLED", table_name="master_data",
                record_id=str(it.id), new_value={"value": it.value, "active": it.is_active}, request=request)
    await db.commit()
    await _refresh(db)
    return _back(msg=f"{it.value} {'shown' if it.is_active else 'hidden'}")


@router.post("/{item_id}/delete")
async def parts_master_delete(request: Request, item_id: str, db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(allowed)):
    _perm(current_user, "delete")
    it = await _item(db, item_id)
    await audit(db, user=current_user, action="PARTS_LIST_VALUE_DELETED", table_name="master_data",
                record_id=str(it.id), old_value={"category": it.category, "value": it.value}, request=request)
    name = it.value
    await db.execute(delete(MasterData).where(MasterData.id == it.id))
    await db.commit()
    await _refresh(db)
    return _back(msg=f"{name} deleted")


@router.get("/export/{category}")
async def parts_master_export(category: str, db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(allowed)):
    _perm(current_user, "export")
    if category not in _CATS:
        raise HTTPException(400, "Not a parts list")
    rows = (await db.execute(select(MasterData).where(MasterData.category == category)
                             .order_by(MasterData.display_order, MasterData.value))).scalars().all()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["value", "description", "is_active", "display_order"])
    for r in rows:
        w.writerow([r.value, r.description or "", "yes" if r.is_active else "no", r.display_order])
    return Response(content=out.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=master_{category}.csv"})


@router.post("/bulk-upload/{category}")
async def parts_master_upload(request: Request, category: str, file: UploadFile = File(...),
                              db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed)):
    _perm(current_user, "upload")
    if category not in _CATS:
        raise HTTPException(400, "Not a parts list")
    data = await file.read()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    have = {v.lower() for (v,) in (await db.execute(select(MasterData.value).where(
        MasterData.category == category))).all()}
    added = skipped = 0
    for row in csv.DictReader(io.StringIO(text)):
        v = (row.get("value") or "").strip()[:200]
        if not v:
            continue
        if v.lower() in have:
            skipped += 1
            continue
        db.add(MasterData(category=category, value=v, description=(row.get("description") or "").strip() or None))
        have.add(v.lower())
        added += 1
    await audit(db, user=current_user, action="PARTS_LIST_UPLOADED", table_name="master_data", record_id=category,
                new_value={"category": category, "added": added, "skipped": skipped}, request=request)
    await db.commit()
    await _refresh(db)
    return _back(msg=f"{added} added, {skipped} already there")
