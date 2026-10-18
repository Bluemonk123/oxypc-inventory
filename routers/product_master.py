"""
Product Master, Hardware, Spare Parts and Product Pricing pages (2026-10-14).

All four live under /product-master and each has its own module permission
key (see routers/master.py PERM_MODULES). Those four keys are disabled by
default for every role (models.role_permissions.DEFAULT_DISABLED_MODULES),
so a role only sees them once an admin enables them in Master Data ->
Permission Matrix. Admin always has access.

Pricing figures (Base Price, Discount Amount, Selling Price) are computed on
the server by models.product_master.product_figures — the browser only
displays what the server returns.
"""
import uuid as _uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from templates_config import templates
from auth.dependencies import require_module_perm, verify_csrf
from models.user import User
from models.product_master import (
    PMDropdownValue, HWRam, HWStorage, PMProduct,
    DROPDOWN_CATEGORIES, DROPDOWN_LABELS, DROPDOWN_PARENT, PART_FIELDS,
    build_product_name, parts_done, product_figures, new_sku, PMCalcUpload, PMCalcRow,
    PMCompetitiveList, PMCompetitorPrice, COMPETITIVE_METHODS, COMPETITIVE_METHOD_KEY,
    competitive_method, recompute_sell_prices,
)
from services.audit_engine import audit
from utils.timezone import app_now

router = APIRouter(prefix="/product-master", tags=["product-master"], dependencies=[Depends(verify_csrf)])

PERM_PRODUCT = "pm_product_master"
PERM_HARDWARE = "pm_hardware"
PERM_SPARE = "pm_spare_parts"
PERM_PRICING = "pm_product_pricing"
PERM_CALCULATOR = "pm_pricing_calculator"


# ── helpers ───────────────────────────────────────────────────────────────────

def _err(msg: str, status: int = 400):
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


def _uuid_or_none(v):
    try:
        return _uuid.UUID(str(v))
    except (ValueError, TypeError):
        return None


def _money(v: str, label: str):
    """'' -> None; a non-negative number -> Decimal; anything else -> ValueError."""
    v = (v or "").strip().replace(",", "")
    if v == "":
        return None
    try:
        d = Decimal(v)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number.")
    if d < 0:
        raise ValueError(f"{label} cannot be negative.")
    if d >= Decimal("10000000000"):
        raise ValueError(f"{label} is too large.")
    return d.quantize(Decimal("0.01"))


def _num(v) -> float | None:
    return float(v) if v is not None else None


async def _dropdowns(db: AsyncSession) -> dict:
    """category -> [PMDropdownValue...] ordered."""
    rows = (await db.execute(
        select(PMDropdownValue).order_by(PMDropdownValue.category, PMDropdownValue.display_order,
                                         PMDropdownValue.value)
    )).scalars().all()
    out = {k: [] for k, _ in DROPDOWN_CATEGORIES}
    for r in rows:
        out.setdefault(r.category, []).append(r)
    if out.get("discount_pct"):
        def _k(r):
            try:
                return float(r.value)
            except ValueError:
                return 1e9
        out["discount_pct"].sort(key=_k)
    return out


def _dropdown_json(dd: dict) -> dict:
    return {cat: [{"id": str(r.id), "value": r.value,
                   "parent_id": str(r.parent_id) if r.parent_id else None} for r in rows]
            for cat, rows in dd.items()}


async def _hw_maps(db: AsyncSession):
    """Hardware rows by lower-case name, for resolving a product's saved picks."""
    rams = {r.ram_name.lower(): r for r in (await db.execute(select(HWRam))).scalars()}
    stos = {r.storage_name.lower(): r for r in (await db.execute(select(HWStorage))).scalars()}
    return rams, stos


def _resolve(p: PMProduct, rams: dict, stos: dict):
    """The Hardware rows matching a product's saved Type/Size picks (2026-10-18).
    A pick with no Hardware row yet -> None (price shows Pending); once Hardware
    saves that combination, its price is used automatically."""
    ram = rams.get(f"{p.ram_type}/{p.ram_size}".lower()) if p.ram_type and p.ram_size else None
    sto = stos.get(f"{p.storage_type}/{p.storage_size}".lower()) if p.storage_type and p.storage_size else None
    return ram, sto


async def _product_rows(db: AsyncSession) -> list:
    rams, stos = await _hw_maps(db)
    rows = (await db.execute(select(PMProduct).order_by(PMProduct.updated_at.desc()))).scalars().all()
    return [_product_dict(p, *_resolve(p, rams, stos)) for p in rows]


def _pick_name(t, z):
    return f"{t}/{z}" if t and z else (t or z or None)


def _product_dict(p: PMProduct, ram, sto) -> dict:
    fig = product_figures(p, ram.ram_price if ram else None, sto.storage_price if sto else None)
    ram_pick = _pick_name(p.ram_type, p.ram_size)
    sto_pick = _pick_name(p.storage_type, p.storage_size)
    return {
        "id": str(p.id), "product_name": p.product_name, "sku": p.sku or "",
        "make": p.make, "model": p.model, "cpu": p.cpu, "cpu_gen": p.cpu_gen,
        # RAM / Storage: the saved pick, priced or not (2026-10-18)
        "ram_id": str(ram.id) if ram else "", "ram": ram_pick,
        "ram_category": p.ram_type or "", "ram_capacity": p.ram_size or "",
        "ram_price": _num(ram.ram_price) if ram else None,
        "storage_id": str(sto.id) if sto else "", "storage": sto_pick,
        "storage_category": p.storage_type or "", "storage_capacity": p.storage_size or "",
        "storage_price": _num(sto.storage_price) if sto else None,
        "product_price": _num(p.product_price),
        "pricing_done": p.product_price is not None and ram is not None and sto is not None,
        # Product Price Pending on Product Master: any of RAM, Storage, RAM
        # Price, Storage Price or Product Price still missing (2026-10-14)
        "price_complete": (p.product_price is not None and ram is not None and sto is not None
                           and ram.ram_price is not None and sto.storage_price is not None),
        "parts": {col: _num(getattr(p, col)) for col, _ in PART_FIELDS},
        "parts_done": parts_done(p),
        "parts_any": any(getattr(p, col) is not None for col, _ in PART_FIELDS),
        "discount_pct": _num(p.discount_pct),
        "base_price": _num(fig["base_price"]),
        "discount_amount": _num(fig["discount_amount"]),
        "selling_price": _num(fig["selling_price"]),
        "updated_at": p.updated_at.strftime("%d-%m-%Y") if p.updated_at else "",
    }


async def _product_payload(db, product_id) -> dict:
    p = (await db.execute(select(PMProduct).where(PMProduct.id == product_id))).scalar_one()
    rams, stos = await _hw_maps(db)
    return _product_dict(p, *_resolve(p, rams, stos))


# ── Product Master page ───────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def product_master_page(request: Request, db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(require_module_perm(PERM_PRODUCT))):
    dd = await _dropdowns(db)
    rams = (await db.execute(select(HWRam).order_by(HWRam.ram_name))).scalars().all()
    stos = (await db.execute(select(HWStorage).order_by(HWStorage.storage_name))).scalars().all()
    by_id = {str(r.id): r for rows in dd.values() for r in rows}
    return templates.TemplateResponse("product_master/product_master.html", {
        "request": request, "current_user": current_user,
        "products": await _product_rows(db),
        "dropdowns": dd, "dropdown_json": _dropdown_json(dd), "dropdown_by_id": by_id,
        "categories": DROPDOWN_CATEGORIES, "dropdown_parent": DROPDOWN_PARENT,
        "part_fields": PART_FIELDS, "rams": rams, "storages": stos,
        "comp_lists": await _comp_lists(db), "comp_method": await competitive_method(db),
        "comp_methods": COMPETITIVE_METHODS,
        "active_tab": request.query_params.get("tab", "products"),
    })


@router.post("/dropdown/save")
async def dropdown_save(request: Request,
                        category: str = Form(...), value: str = Form(""),
                        parent_id: str = Form(""), item_id: str = Form(""),
                        db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(require_module_perm(PERM_PRODUCT))):
    """Add (no item_id) or edit (item_id) one Dropdown Configuration value."""
    if category not in DROPDOWN_LABELS:
        return _err("Unknown dropdown.")
    value = (value or "").strip()
    if not value:
        return _err("Enter a value.")
    parent = None
    if category in DROPDOWN_PARENT:
        parent = (await db.execute(select(PMDropdownValue).where(
            PMDropdownValue.id == _uuid_or_none(parent_id),
            PMDropdownValue.category == DROPDOWN_PARENT[category]))).scalar_one_or_none()
        if not parent and category != "cpu_gen":
            return _err(f"Select a {DROPDOWN_LABELS[DROPDOWN_PARENT[category]]}.")
    # CPU Gen is saved as typed with no CPU (2026-10-14). An existing mapped
    # value keeps its link when edited (parent_id not sent from the modal).
    if category == "discount_pct":
        try:
            pct = Decimal(value.rstrip("%").strip())
        except InvalidOperation:
            return _err("Discount % must be a number.")
        if pct < 0 or pct > 100:
            return _err("Discount % must be between 0 and 100.")
        value = format(pct.normalize(), "f")
    if len(value) > 100:
        return _err("Value is too long (100 characters max).")

    dup = (await db.execute(select(PMDropdownValue).where(
        PMDropdownValue.category == category,
        func.lower(PMDropdownValue.value) == value.lower(),
        (PMDropdownValue.parent_id == parent.id) if parent else PMDropdownValue.parent_id.is_(None),
    ))).scalars().first()

    if item_id:
        row = (await db.execute(select(PMDropdownValue).where(
            PMDropdownValue.id == _uuid_or_none(item_id),
            PMDropdownValue.category == category))).scalar_one_or_none()
        if not row:
            return _err("Value not found.", 404)
        if dup and dup.id != row.id:
            return _err(f'"{value}" already exists.')
        old = {"value": row.value, "parent_id": str(row.parent_id) if row.parent_id else None}
        row.value = value
        if category != "cpu_gen":
            row.parent_id = parent.id if parent else None
        if category == "cpu":
            # keep each linked CPU Gen's "<CPU>/<Gen>" prefix in step
            for child in (await db.execute(select(PMDropdownValue).where(
                    PMDropdownValue.category == "cpu_gen", PMDropdownValue.parent_id == row.id))).scalars():
                gen = child.value.split("/", 1)[1] if "/" in child.value else child.value
                child.value = f"{value}/{gen}"
        await audit(db, action="PM_DROPDOWN_EDITED", user=current_user, table_name="pm_dropdown_values",
                    record_id=str(row.id), old_value=old,
                    new_value={"category": category, "value": value}, request=request)
    else:
        if dup:
            return _err(f'"{value}" already exists.')
        order = (await db.execute(select(func.coalesce(func.max(PMDropdownValue.display_order), -1))
                                  .where(PMDropdownValue.category == category))).scalar() + 1
        row = PMDropdownValue(category=category, value=value, parent_id=parent.id if parent else None,
                              display_order=order, created_by=current_user.username)
        db.add(row)
        await db.flush()
        await audit(db, action="PM_DROPDOWN_ADDED", user=current_user, table_name="pm_dropdown_values",
                    record_id=str(row.id), new_value={"category": category, "value": value}, request=request)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/dropdown/{item_id}/delete")
async def dropdown_delete(item_id: str, request: Request, db: AsyncSession = Depends(get_db),
                          current_user: User = Depends(require_module_perm(PERM_PRODUCT))):
    row = (await db.execute(select(PMDropdownValue).where(
        PMDropdownValue.id == _uuid_or_none(item_id)))).scalar_one_or_none()
    if not row:
        return _err("Value not found.", 404)
    children = (await db.execute(select(func.count()).select_from(PMDropdownValue)
                                 .where(PMDropdownValue.parent_id == row.id))).scalar() or 0
    if children:
        child_label = "Models" if row.category == "make" else "CPU Gens"
        return _err(f'Delete the {children} {child_label} under "{row.value}" first.')
    await audit(db, action="PM_DROPDOWN_DELETED", user=current_user, table_name="pm_dropdown_values",
                record_id=str(row.id), old_value={"category": row.category, "value": row.value},
                request=request)
    await db.delete(row)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/products/save")
async def product_save(request: Request,
                       make_id: str = Form(""), model_id: str = Form(""), cpu_id: str = Form(""),
                       cpu_gen_id: str = Form(""),
                       product_id: str = Form(""),
                       db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(require_module_perm(PERM_PRODUCT))):
    """Add (no product_id) or edit a product. Values are picked from the
    Dropdown Configuration lists and the Hardware RAM / Storage tables."""
    async def pick(cat, vid, parent=None):
        q = select(PMDropdownValue).where(PMDropdownValue.id == _uuid_or_none(vid),
                                          PMDropdownValue.category == cat)
        if parent is not None:
            q = q.where(PMDropdownValue.parent_id == parent.id)
        return (await db.execute(q)).scalar_one_or_none()

    make = await pick("make", make_id)
    model = await pick("model", model_id, make) if make else None
    cpu = await pick("cpu", cpu_id)
    gen = None
    if cpu:
        # a CPU Gen mapped to this CPU, or a plain one mapped to no CPU
        gen = await pick("cpu_gen", cpu_gen_id)   # independent of CPU
    missing = [n for n, v in (("Make", make), ("Model", model), ("CPU", cpu), ("CPU Gen", gen)) if v is None]
    if missing:
        return _err("Select " + ", ".join(missing) + ".")

    # RAM / Storage are set on the Product Pricing page (v2)
    name = build_product_name(make.value, model.value, cpu.value, gen.value)
    dup = (await db.execute(select(PMProduct).where(func.lower(PMProduct.product_name) == name.lower())
                            )).scalars().first()
    values = {"make": make.value, "model": model.value, "cpu": cpu.value, "cpu_gen": gen.value,
              "product_name": name}
    if product_id:
        p = (await db.execute(select(PMProduct).where(PMProduct.id == _uuid_or_none(product_id)))).scalar_one_or_none()
        if not p:
            return _err("Product not found.", 404)
        if dup and dup.id != p.id:
            return _err(f'Product "{name}" already exists.')
        old = {"product_name": p.product_name}
        for k, v in values.items():
            setattr(p, k, v)
        await audit(db, action="PM_PRODUCT_EDITED", user=current_user, table_name="pm_products",
                    record_id=str(p.id), old_value=old, new_value={"product_name": name}, request=request)
    else:
        if dup:
            return _err(f'Product "{name}" already exists.')
        taken = set((await db.execute(select(PMProduct.sku).where(PMProduct.sku.isnot(None)))).scalars().all())
        p = PMProduct(created_by=current_user.username, sku=new_sku(taken), **values)
        db.add(p)
        await db.flush()
        await audit(db, action="PM_PRODUCT_ADDED", user=current_user, table_name="pm_products",
                    record_id=str(p.id), new_value={"product_name": name}, request=request)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/products/{product_id}/delete")
async def product_delete(product_id: str, request: Request, db: AsyncSession = Depends(get_db),
                         current_user: User = Depends(require_module_perm(PERM_PRODUCT))):
    p = (await db.execute(select(PMProduct).where(PMProduct.id == _uuid_or_none(product_id)))).scalar_one_or_none()
    if not p:
        return _err("Product not found.", 404)
    await audit(db, action="PM_PRODUCT_DELETED", user=current_user, table_name="pm_products",
                record_id=str(p.id), old_value={"product_name": p.product_name,
                                                "product_price": str(p.product_price) if p.product_price is not None else None},
                request=request)
    await db.delete(p)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/products/{product_id}/discount")
async def product_discount(product_id: str, request: Request, discount_pct: str = Form(""),
                           db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(require_module_perm(PERM_PRODUCT))):
    p = (await db.execute(select(PMProduct).where(PMProduct.id == _uuid_or_none(product_id)))).scalar_one_or_none()
    if not p:
        return _err("Product not found.", 404)
    pct = None
    if discount_pct.strip():
        allowed = {r.value for r in (await db.execute(select(PMDropdownValue).where(
            PMDropdownValue.category == "discount_pct"))).scalars()}
        if discount_pct.strip() not in allowed:
            return _err("Pick a Discount % from the list.")
        pct = Decimal(discount_pct.strip())
    old = str(p.discount_pct) if p.discount_pct is not None else None
    p.discount_pct = pct
    await audit(db, action="PM_DISCOUNT_SET", user=current_user, table_name="pm_products",
                record_id=str(p.id), old_value={"discount_pct": old},
                new_value={"discount_pct": str(pct) if pct is not None else None}, request=request)
    await db.commit()
    return JSONResponse({"ok": True, "product": await _product_payload(db, p.id)})


# ── Competitive Pricings tab ──────────────────────────────────────────────────

async def _comp_lists(db):
    return (await db.execute(select(PMCompetitiveList).order_by(
        PMCompetitiveList.display_order, PMCompetitiveList.name))).scalars().all()


@router.post("/competitive/save")
async def competitive_save(request: Request, name: str = Form(""), item_id: str = Form(""),
                           db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(require_module_perm(PERM_PRODUCT))):
    name = (name or "").strip()
    if not name:
        return _err("Enter a Competitive List Name.")
    if len(name) > 100:
        return _err("Name is too long (100 characters max).")
    dup = (await db.execute(select(PMCompetitiveList).where(
        func.lower(PMCompetitiveList.name) == name.lower()))).scalars().first()
    if item_id:
        row = (await db.execute(select(PMCompetitiveList).where(
            PMCompetitiveList.id == _uuid_or_none(item_id)))).scalar_one_or_none()
        if not row:
            return _err("List not found.", 404)
        if dup and dup.id != row.id:
            return _err(f'"{name}" already exists.')
        old = row.name
        row.name = name
        await audit(db, action="PM_COMPETITIVE_LIST_EDITED", user=current_user, table_name="pm_competitive_lists",
                    record_id=str(row.id), old_value={"name": old}, new_value={"name": name}, request=request)
    else:
        if dup:
            return _err(f'"{name}" already exists.')
        order = (await db.execute(select(func.coalesce(func.max(PMCompetitiveList.display_order), -1)))).scalar() + 1
        row = PMCompetitiveList(name=name, display_order=order, created_by=current_user.username)
        db.add(row)
        await db.flush()
        await audit(db, action="PM_COMPETITIVE_LIST_ADDED", user=current_user, table_name="pm_competitive_lists",
                    record_id=str(row.id), new_value={"name": name}, request=request)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/competitive/{item_id}/delete")
async def competitive_delete(item_id: str, request: Request, db: AsyncSession = Depends(get_db),
                             current_user: User = Depends(require_module_perm(PERM_PRODUCT))):
    from sqlalchemy import delete as sa_delete
    row = (await db.execute(select(PMCompetitiveList).where(
        PMCompetitiveList.id == _uuid_or_none(item_id)))).scalar_one_or_none()
    if not row:
        return _err("List not found.", 404)
    affected = set((await db.execute(select(PMCompetitorPrice.product_id).where(
        PMCompetitorPrice.list_id == row.id))).scalars().all())
    await audit(db, action="PM_COMPETITIVE_LIST_DELETED", user=current_user, table_name="pm_competitive_lists",
                record_id=str(row.id), old_value={"name": row.name, "prices_removed": len(affected)}, request=request)
    await db.execute(sa_delete(PMCompetitorPrice).where(PMCompetitorPrice.list_id == row.id))
    await db.delete(row)
    await db.flush()
    await recompute_sell_prices(db, affected)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/competitive/method")
async def competitive_set_method(request: Request, method: str = Form(""), db: AsyncSession = Depends(get_db),
                                 current_user: User = Depends(require_module_perm(PERM_PRODUCT))):
    from models.settings import AppSetting
    if method not in dict(COMPETITIVE_METHODS):
        return _err("Pick Average, Maximum or Minimum.")
    row = (await db.execute(select(AppSetting).where(AppSetting.key == COMPETITIVE_METHOD_KEY))).scalar_one_or_none()
    old = row.value if row else None
    if row:
        row.value, row.updated_by = method, current_user.username
    else:
        db.add(AppSetting(key=COMPETITIVE_METHOD_KEY, value=method, updated_by=current_user.username,
                          description="Product Master: Competitive Pricings formula method"))
    await db.flush()
    await recompute_sell_prices(db)      # every product with competitor prices
    await audit(db, action="PM_COMPETITIVE_METHOD_SET", user=current_user, table_name="app_settings",
                record_id=COMPETITIVE_METHOD_KEY, old_value={"method": old}, new_value={"method": method},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True})


# ── Hardware page ─────────────────────────────────────────────────────────────

@router.get("/hardware", response_class=HTMLResponse)
async def hardware_page(request: Request, db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(require_module_perm(PERM_HARDWARE))):
    """Every Type x Size combination from Dropdown Configuration is listed
    (2026-10-14). A combination only becomes an hw_ram / hw_storage row once
    its price is saved, so unpriced rows need no schema change; a saved row
    whose Type or Size was later removed from the dropdowns is still listed."""
    dd = await _dropdowns(db)
    rams = (await db.execute(select(HWRam))).scalars().all()
    stos = (await db.execute(select(HWStorage))).scalars().all()

    # Updated By (2026-10-18): the user on the latest audit entry for each
    # saved row (no extra column needed), falling back to created_by.
    from models.engines import AuditLog
    ids = [str(r.id) for r in [*rams, *stos]]
    last_by = {}
    if ids:
        for rid, uname in (await db.execute(
            select(AuditLog.record_id, AuditLog.username)
            .where(AuditLog.table_name.in_([HWRam.__tablename__, HWStorage.__tablename__]),
                   AuditLog.record_id.in_(ids))
            .order_by(AuditLog.record_id, AuditLog.timestamp.desc())
            .distinct(AuditLog.record_id)
        )).all():
            last_by[rid] = uname
    unames = {u for u in [*last_by.values(), *[r.created_by for r in [*rams, *stos]]] if u}
    full = {}
    if unames:
        full = {u.username: (u.full_name or u.username) for u in
                (await db.execute(select(User).where(User.username.in_(unames)))).scalars()}

    def who(r):
        u = last_by.get(str(r.id)) or r.created_by
        return full.get(u, u or "")

    def stamp(r):
        d = r.updated_at or r.created_at
        return d.strftime("%d-%m-%Y %H:%M") if d else ""

    def grid(types, sizes, saved, name_of, cat_of, cap_of, price_of):
        by_name = {name_of(r).lower(): r for r in saved}
        rows, seen = [], set()

        def extra(r):
            return {"updated_on": stamp(r) if r else "", "updated_by": who(r) if r else "",
                    "updated_sort": (r.updated_at or r.created_at).strftime("%Y%m%d%H%M%S")
                    if r and (r.updated_at or r.created_at) else ""}
        for t in types:
            for z in sizes:
                name = f"{t.value}/{z.value}"
                r = by_name.get(name.lower())
                seen.add(name.lower())
                rows.append({"id": str(r.id) if r else "", "name": name, "category": t.value,
                             "capacity": z.value, "price": price_of(r) if r else None, **extra(r)})
        for r in saved:
            if name_of(r).lower() not in seen:
                rows.append({"id": str(r.id), "name": name_of(r), "category": cat_of(r),
                             "capacity": cap_of(r), "price": price_of(r), **extra(r)})
        return rows

    ram_rows = grid(dd["ram_category"], dd["ram_capacity"], rams, lambda r: r.ram_name,
                    lambda r: r.ram_category, lambda r: r.ram_capacity, lambda r: r.ram_price)
    sto_rows = grid(dd["storage_category"], dd["storage_capacity"], stos, lambda r: r.storage_name,
                    lambda r: r.storage_category, lambda r: r.storage_capacity, lambda r: r.storage_price)
    return templates.TemplateResponse("product_master/hardware.html", {
        "request": request, "current_user": current_user,
        "ram_rows": ram_rows, "storage_rows": sto_rows,
    })


async def _hw_save(request, db, current_user, kind, item_id, category, capacity, price):
    Model = HWRam if kind == "ram" else HWStorage
    label = "RAM" if kind == "ram" else "Storage"
    allowed_cat = {r.value for r in (await db.execute(select(PMDropdownValue).where(
        PMDropdownValue.category == f"{kind}_category"))).scalars()}
    allowed_cap = {r.value for r in (await db.execute(select(PMDropdownValue).where(
        PMDropdownValue.category == f"{kind}_capacity"))).scalars()}
    category, capacity = (category or "").strip(), (capacity or "").strip()
    if category not in allowed_cat:
        return _err(f"Select a {label} Category.")
    if capacity not in allowed_cap:
        return _err(f"Select a {label} Capacity.")
    try:
        amount = _money(price, f"{label} Price")
    except ValueError as e:
        return _err(str(e))
    if amount is None:
        return _err(f"Enter the {label} Price.")
    name = f"{category}/{capacity}"
    name_col = Model.ram_name if kind == "ram" else Model.storage_name
    dup = (await db.execute(select(Model).where(func.lower(name_col) == name.lower()))).scalars().first()
    fields = ({"ram_category": category, "ram_capacity": capacity, "ram_name": name, "ram_price": amount}
              if kind == "ram" else
              {"storage_category": category, "storage_capacity": capacity, "storage_name": name,
               "storage_price": amount})
    if item_id:
        row = (await db.execute(select(Model).where(Model.id == _uuid_or_none(item_id)))).scalar_one_or_none()
        if not row:
            return _err(f"{label} not found.", 404)
        if dup and dup.id != row.id:
            return _err(f'"{name}" already exists.')
        old_name = row.ram_name if kind == "ram" else row.storage_name
        for k, v in fields.items():
            setattr(row, k, v)
        await audit(db, action=f"HW_{kind.upper()}_EDITED", user=current_user, table_name=Model.__tablename__,
                    record_id=str(row.id), new_value={"name": name, "price": str(amount)}, request=request)
    else:
        if dup:
            return _err(f'"{name}" already exists.')
        row = Model(created_by=current_user.username, **fields)
        db.add(row)
        await db.flush()
        await audit(db, action=f"HW_{kind.upper()}_ADDED", user=current_user, table_name=Model.__tablename__,
                    record_id=str(row.id), new_value={"name": name, "price": str(amount)}, request=request)
    # Products already holding this Type/Size pick get linked to it, so their
    # Pending RAM/Storage price fills in on Product Master (2026-10-18).
    from sqlalchemy import update as _update
    if kind == "ram":
        await db.execute(_update(PMProduct).where(
            func.lower(PMProduct.ram_type) == category.lower(),
            func.lower(PMProduct.ram_size) == capacity.lower()).values(ram_id=row.id))
    else:
        await db.execute(_update(PMProduct).where(
            func.lower(PMProduct.storage_type) == category.lower(),
            func.lower(PMProduct.storage_size) == capacity.lower()).values(storage_id=row.id))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _err("Could not save — a product with the same name already exists.")
    stamp = row.updated_at or row.created_at
    return JSONResponse({"ok": True, "id": str(row.id),
                         "updated_on": stamp.strftime("%d-%m-%Y %H:%M") if stamp else "",
                         "updated_sort": stamp.strftime("%Y%m%d%H%M%S") if stamp else "",
                         "updated_by": current_user.full_name or current_user.username})


async def _hw_delete(request, db, current_user, kind, item_id):
    Model = HWRam if kind == "ram" else HWStorage
    label = "RAM" if kind == "ram" else "Storage"
    row = (await db.execute(select(Model).where(Model.id == _uuid_or_none(item_id)))).scalar_one_or_none()
    if not row:
        return _err(f"{label} not found.", 404)
    # Products using it keep their Type/Size pick; their price goes back to
    # Pending until Hardware prices it again (2026-10-18).
    from sqlalchemy import update as _update
    fk = PMProduct.ram_id if kind == "ram" else PMProduct.storage_id
    await db.execute(_update(PMProduct).where(fk == row.id).values({fk.key: None}))
    name = row.ram_name if kind == "ram" else row.storage_name
    await audit(db, action=f"HW_{kind.upper()}_DELETED", user=current_user, table_name=Model.__tablename__,
                record_id=str(row.id), old_value={"name": name}, request=request)
    await db.delete(row)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/hardware/{kind}/save")
async def hardware_save(kind: str, request: Request, item_id: str = Form(""),
                        category: str = Form(""), capacity: str = Form(""), price: str = Form(""),
                        db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(require_module_perm(PERM_HARDWARE))):
    if kind not in ("ram", "storage"):
        return _err("Unknown hardware type.", 404)
    return await _hw_save(request, db, current_user, kind, item_id, category, capacity, price)


@router.post("/hardware/{kind}/{item_id}/delete")
async def hardware_delete(kind: str, item_id: str, request: Request, db: AsyncSession = Depends(get_db),
                          current_user: User = Depends(require_module_perm(PERM_HARDWARE))):
    if kind not in ("ram", "storage"):
        return _err("Unknown hardware type.", 404)
    return await _hw_delete(request, db, current_user, kind, item_id)


# ── Pricing Calculator page (2026-10-18) ──────────────────────────────────────
# OFF by default for every role (DEFAULT_DISABLED_MODULES); admin always sees it.
#
# Upload a file of devices (SKU, Make, Model, CPU, CPU Gen, RAM Type/Size,
# Storage Type/Size, plus optional part columns). Only the file's values are
# stored (pm_calc_uploads / pm_calc_rows); every price is worked out live:
#   Selling Price = (Base Price + RAM Price + Storage Price)
#                   - (price of every part the file marks Damage / Not Working)
#   Base Price    = Product Master Base Price of the SKU
#   RAM / Storage = Hardware price of "<Type>/<Size>" from the file
#   Part prices   = Spare Parts prices of the SKU
# Anything missing shows Pending and fills in once it is priced.

CALC_COLUMNS = ["SKU", "Make", "Model", "CPU", "CPU Gen", "Ram Type", "Ram Size",
                "Storage Type", "Storage Size"] + [label for _, label in PART_FIELDS]
_CALC_HEAD = {"sku": "sku", "make": "make", "model": "model", "cpu": "cpu", "cpugen": "cpu_gen",
              "cpugeneration": "cpu_gen", "generation": "cpu_gen",
              "ramtype": "ram_type", "ramsize": "ram_size", "ramcapacity": "ram_size",
              "storagetype": "storage_type", "storagesize": "storage_size", "storagecapacity": "storage_size",
              "ssdtype": "storage_type", "ssdsize": "storage_size",
              "ram": "ram", "storage": "storage", "ssd": "storage"}
_CALC_PART_HEAD = {"bazel": "bezel_price", "bezel": "bezel_price", "screen": "screen_price",
                   "hinge": "hinge_price", "hingecover": "hinge_cover_price", "touchpad": "touchpad_price",
                   "base": "base_part_price", "keyboard": "keyboard_price", "battery": "battery_price"}
_PART_LABEL = dict(PART_FIELDS)


def _norm_head(h) -> str:
    import re
    k = re.sub(r"[^a-z0-9]", "", str(h or "").lower())
    for suf in ("price", "status", "condition"):
        if k.endswith(suf) and k[:-len(suf)] in _CALC_PART_HEAD:
            k = k[:-len(suf)]
    return k


def _is_damaged(v) -> bool:
    import re
    return re.sub(r"[\s_\-]", "", str(v or "").lower()) in ("damage", "damaged", "notworking")


def _read_calc_file(name: str, raw: bytes) -> list[list]:
    """File -> list of rows (first row = headers). CSV (UTF-8, falling back to
    Windows-1252) or Excel .xlsx."""
    import csv, io
    if name.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [[("" if c is None else str(c).strip()) for c in r] for r in ws.iter_rows(values_only=True)]
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Could not read the file's text encoding. Save it as CSV (UTF-8) or Excel.")
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text))]


async def _calc_view(db, upload_id) -> dict:
    """Live-priced, grouped table for one upload."""
    from decimal import Decimal as D
    rows = (await db.execute(select(PMCalcRow).where(PMCalcRow.upload_id == upload_id)
                             .order_by(PMCalcRow.row_no))).scalars().all()
    skus = {r.sku for r in rows if r.sku}
    prods = {p.sku: p for p in (await db.execute(select(PMProduct).where(PMProduct.sku.in_(skus)))).scalars()} if skus else {}
    rams, stos = await _hw_maps(db)
    groups, order = {}, []
    for r in rows:
        p = prods.get(r.sku)
        pending, parts_used = [], []
        if p is None:
            pending.append("SKU not found in Product Master")
            base = None
        else:
            pram, psto = _resolve(p, rams, stos)
            base = product_figures(p, pram.ram_price if pram else None, psto.storage_price if psto else None)["base_price"]
            if base is None:
                pending.append("Base Price")
        ram_name = f"{r.ram_type}/{r.ram_size}" if r.ram_type and r.ram_size else (r.ram_type or r.ram_size or "")
        sto_name = f"{r.storage_type}/{r.storage_size}" if r.storage_type and r.storage_size else (r.storage_type or r.storage_size or "")
        hr = rams.get(ram_name.lower()) if r.ram_type and r.ram_size else None
        hs = stos.get(sto_name.lower()) if r.storage_type and r.storage_size else None
        if hr is None:
            pending.append("RAM Price")
        if hs is None:
            pending.append("Storage Price")
        deduct = D("0")
        for col in [c for c in (r.damaged_parts or "").split(",") if c]:
            v = getattr(p, col, None) if p is not None else None
            parts_used.append(_PART_LABEL.get(col, col))
            if v is None:
                if p is not None:
                    pending.append(f"{_PART_LABEL.get(col, col)} Price")
            else:
                deduct += D(v)
        unit = None
        if not pending:
            unit = (D(base) + D(hr.ram_price) + D(hs.storage_price) - deduct).quantize(D("0.01"))
        make = p.make if p else (r.make or "")
        model = p.model if p else (r.model or "")
        cpu = p.cpu if p else (r.cpu or "")
        gen = p.cpu_gen if p else (r.cpu_gen or "")
        # one line per Make + Model + CPU + CPU Gen + RAM + SSD
        key = (make.lower(), model.lower(), cpu.lower(), gen.lower(), ram_name.lower(), sto_name.lower())
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"sku": r.sku or "", "make": make, "model": model, "cpu": cpu, "cpu_gen": gen,
                               "ram": ram_name.replace("/", " / "), "ssd": sto_name.replace("/", " / "),
                               "qty": 0, "total": D("0"), "pending": [], "sku_missing": False,
                               "skus": [], "damaged": {}, "row_ids": []}
            order.append(key)
        g["qty"] += 1
        g["row_ids"].append(str(r.id))
        if r.sku and r.sku not in g["skus"]:
            g["skus"].append(r.sku)
        if p is None:
            g["sku_missing"] = True
        for x in pending:
            if x not in g["pending"]:
                g["pending"].append(x)
        for x in parts_used:
            g["damaged"][x] = g["damaged"].get(x, 0) + 1
        if unit is not None:
            g["total"] += unit
    out = []
    for k in order:
        g = groups[k]
        done = not g["pending"]
        out.append({**g, "selling": float(g["total"]) if done else None,
                    "avg": float((g["total"] / g["qty"]).quantize(D("0.01"))) if done and g["qty"] else None,
                    "damaged": ", ".join(f"{n} ×{c}" for n, c in g["damaged"].items())})
    return {"rows": out, "devices": len(rows)}


@router.get("/pricing-calculator", response_class=HTMLResponse)
async def pricing_calculator_page(request: Request, db: AsyncSession = Depends(get_db),
                                  current_user: User = Depends(require_module_perm(PERM_CALCULATOR))):
    uploads = (await db.execute(select(PMCalcUpload).order_by(PMCalcUpload.uploaded_at.desc()))).scalars().all()
    sel = _uuid_or_none(request.query_params.get("upload")) or (uploads[0].id if uploads else None)
    current = next((u for u in uploads if u.id == sel), None)
    view = await _calc_view(db, current.id) if current else {"rows": [], "devices": 0}
    names = {}
    who = {u.uploaded_by for u in uploads if u.uploaded_by}
    if who:
        names = {u.username: (u.full_name or u.username) for u in
                 (await db.execute(select(User).where(User.username.in_(who)))).scalars()}
    return templates.TemplateResponse("product_master/pricing_calculator.html", {
        "request": request, "current_user": current_user, "uploads": uploads, "current": current,
        "view": view, "user_names": names, "columns": CALC_COLUMNS,
        "msg": request.query_params.get("success", ""), "err": request.query_params.get("error", ""),
    })


@router.get("/pricing-calculator/template")
async def pricing_calculator_template(current_user: User = Depends(require_module_perm(PERM_CALCULATOR))):
    head = ",".join(CALC_COLUMNS)
    sample = ",".join(["123456789012", "HP", "EliteBook 840 G5", "i5", "8th", "DDR4", "8GB", "SSD", "256GB"]
                      + ["Damaged" if lbl == "Screen" else "" for _, lbl in PART_FIELDS])
    return Response("﻿" + head + "\r\n" + sample + "\r\n", media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="pricing_calculator_template.csv"'})


@router.post("/pricing-calculator/rows/delete")
async def pricing_calculator_rows_delete(request: Request, upload_id: str = Form(""), row_ids: str = Form(""),
                                         db: AsyncSession = Depends(get_db),
                                         current_user: User = Depends(require_module_perm(PERM_CALCULATOR))):
    """Delete one table line of an upload (all its device rows). An upload left
    with no rows is removed too (2026-10-18)."""
    up = (await db.execute(select(PMCalcUpload).where(PMCalcUpload.id == _uuid_or_none(upload_id)))).scalar_one_or_none()
    if not up:
        return _err("Upload not found.", 404)
    ids = [x for x in (_uuid_or_none(v) for v in row_ids.split(",")) if x]
    rows = (await db.execute(select(PMCalcRow).where(PMCalcRow.upload_id == up.id,
                                                     PMCalcRow.id.in_(ids)))).scalars().all() if ids else []
    if not rows:
        return _err("Nothing to delete.")
    # audit first — the rows are gone after commit
    await audit(db, action="PM_CALC_ROWS_DELETED", user=current_user, table_name="pm_calc_rows",
                record_id=str(up.id), old_value={"file": up.file_name, "count": len(rows),
                                                 "rows": [{"row": r.row_no, "sku": r.sku, "ram": f"{r.ram_type}/{r.ram_size}",
                                                           "storage": f"{r.storage_type}/{r.storage_size}"} for r in rows]},
                request=request)
    for r in rows:
        await db.delete(r)
    up.row_count = max(0, (up.row_count or 0) - len(rows))
    gone = up.row_count == 0
    if gone:
        await db.delete(up)
    await db.commit()
    return JSONResponse({"ok": True, "deleted": len(rows), "upload_removed": gone})


def _calc_sku(v: str):
    """Normalise a SKU cell: ="123…" (Product Master Export), "123….0" (Excel number)."""
    v = (v or "").strip()
    if v.startswith("="):
        v = v[1:]
    v = v.strip().strip('"').strip()
    if v.endswith(".0") and v[:-2].isdigit():
        v = v[:-2]
    return v[:40] or None


def _calc_missing_file(name: str, header: list, rows: list, suffix: str = "SKU_not_found") -> tuple[str, str]:
    """The uploaded file again, with only the rows whose SKU was not found —
    same format (CSV or Excel). Returns (download file name, base64 content)."""
    import base64, csv, io, os
    stem, ext = os.path.splitext(name)
    out_name = f"{stem}_{suffix}{ext if ext.lower() in ('.csv', '.xlsx') else '.csv'}"
    if out_name.lower().endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(header)
        for r in rows:
            ws.append(r)
        for c in ws[1]:
            c.font = openpyxl.styles.Font(bold=True)
        # keep long SKUs as text so Excel shows all digits
        for col in ws.iter_cols(min_row=1):
            for c in col:
                c.number_format = "@"
        buf = io.BytesIO()
        wb.save(buf)
        data = buf.getvalue()
    else:
        sio = io.StringIO()
        w = csv.writer(sio, lineterminator="\r\n")
        w.writerow(header)
        w.writerows(rows)
        data = ("﻿" + sio.getvalue()).encode("utf-8")
    return out_name, base64.b64encode(data).decode("ascii")


@router.post("/pricing-calculator/upload")
async def pricing_calculator_upload(request: Request, file: UploadFile = File(...),
                                    db: AsyncSession = Depends(get_db),
                                    current_user: User = Depends(require_module_perm(PERM_CALCULATOR))):
    """Only rows whose SKU is in Product Master are added (2026-10-18). Rows
    with an unknown or blank SKU are left out and handed back as a file in the
    same format, so they can be corrected and uploaded again."""
    name = (file.filename or "upload").strip()[:255]
    if not name.lower().endswith((".csv", ".xlsx", ".xlsm")):
        return _err("Upload a .csv or .xlsx file.")
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        return _err("The file is larger than 10 MB.")
    try:
        table = _read_calc_file(name, raw)
    except Exception as e:  # noqa: BLE001 — bad workbook / encoding
        return _err(f"Could not read the file: {e}")
    table = [r for r in table if any(c for c in r)]
    if not table:
        return _err("The file is empty.")
    header = table[0]
    heads = [_norm_head(h) for h in header]
    fmap, pmap = {}, {}
    for i, h in enumerate(heads):
        if h in _CALC_HEAD and _CALC_HEAD[h] not in fmap:
            fmap[_CALC_HEAD[h]] = i
        elif h in _CALC_PART_HEAD and _CALC_PART_HEAD[h] not in pmap:
            pmap[_CALC_PART_HEAD[h]] = i
    if "sku" not in fmap:
        return _err("The file needs a SKU column. Download the template for the expected columns.")
    body = table[1:]
    if len(body) > 20000:
        return _err("The file has more than 20,000 rows.")

    si = fmap["sku"]
    file_skus = {_calc_sku(r[si]) for r in body if si < len(r)} - {None}
    known = set()
    if file_skus:
        known = set((await db.execute(select(PMProduct.sku).where(PMProduct.sku.in_(file_skus)))).scalars().all())

    # A row is "already added" when its Make + Model + CPU + CPU Gen (from
    # Product Master, by SKU) + RAM + SSD match a row already in the
    # calculator, or an earlier row of this file (2026-10-18).
    prod_of = {p.sku: p for p in (await db.execute(select(PMProduct).where(PMProduct.sku.in_(known)))).scalars()} if known else {}

    def cell(r, f):
        i = fmap.get(f)
        return (r[i].strip()[:100] or None) if i is not None and i < len(r) else None

    def split(r, f):
        v = cell(r, f) or ""
        if "/" in v:
            a, b = v.split("/", 1)
            return a.strip()[:100] or None, b.strip()[:100] or None
        return None, None

    def picks(r):
        rt, rs = cell(r, "ram_type"), cell(r, "ram_size")
        if not rt and not rs:
            rt, rs = split(r, "ram")
        st, ss = cell(r, "storage_type"), cell(r, "storage_size")
        if not st and not ss:
            st, ss = split(r, "storage")
        return rt, rs, st, ss

    def combo(p, rt, rs, st, ss):
        return tuple((x or "").strip().lower() for x in (p.make, p.model, p.cpu, p.cpu_gen, rt, rs, st, ss))

    existing = set()
    for er, ep in (await db.execute(select(PMCalcRow, PMProduct).join(PMProduct, PMProduct.sku == PMCalcRow.sku))).all():
        existing.add(combo(ep, er.ram_type, er.ram_size, er.storage_type, er.storage_size))

    good, missing_rows, missing_skus, blank = [], [], set(), 0
    dup_rows, dup_skus = [], set()
    for idx, r in enumerate(body, start=2):
        sku = _calc_sku(r[si]) if si < len(r) else None
        if sku in known:
            pk = picks(r)
            key = combo(prod_of[sku], *pk)
            if key in existing:
                dup_rows.append(r)
                dup_skus.add(sku)
                continue
            existing.add(key)
            good.append((idx, sku, r, pk))
        else:
            missing_rows.append(r)
            if sku:
                missing_skus.add(sku)
            else:
                blank += 1

    up = None
    if good:
        up = PMCalcUpload(file_name=name, uploaded_by=current_user.username, row_count=len(good))
        db.add(up)
        await db.flush()
        for idx, sku, r, (rt, rs, st, ss) in good:
            def g(f):
                return cell(r, f)
            damaged = [col for col, i in pmap.items() if i < len(r) and _is_damaged(r[i])]
            db.add(PMCalcRow(upload_id=up.id, row_no=idx, sku=sku, make=g("make"), model=g("model"),
                             cpu=g("cpu"), cpu_gen=g("cpu_gen"), ram_type=rt, ram_size=rs,
                             storage_type=st, storage_size=ss, damaged_parts=",".join(damaged) or None))
        await audit(db, action="PM_CALC_UPLOADED", user=current_user, table_name="pm_calc_uploads",
                    record_id=str(up.id), new_value={"file": name, "rows": len(good),
                                                     "rows_sku_not_found": len(missing_rows),
                                                     "rows_already_added": len(dup_rows)},
                    request=request)
        await db.commit()

    out = {"ok": True, "added": len(good), "upload_id": str(up.id) if up else None,
           "missing_rows": len(missing_rows), "missing_skus": len(missing_skus), "blank_skus": blank,
           "dup_rows": len(dup_rows), "dup_skus": len(dup_skus)}
    width = len(header)
    if missing_rows:
        rows = [list(r) + [""] * (width - len(r)) for r in missing_rows]
        out["missing_file_name"], out["missing_file_b64"] = _calc_missing_file(name, header, rows, "SKU_not_found")
    if dup_rows:
        rows = [list(r) + [""] * (width - len(r)) for r in dup_rows]
        out["dup_file_name"], out["dup_file_b64"] = _calc_missing_file(name, header, rows, "already_added")
    return JSONResponse(out)


# ── Product Pricing page ──────────────────────────────────────────────────────

@router.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request, db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(require_module_perm(PERM_PRICING))):
    return templates.TemplateResponse("product_master/pricing.html", {
        "request": request, "current_user": current_user, "products": await _product_rows(db),
        "dropdowns": await _dropdowns(db), "comp_lists": await _comp_lists(db),
        "comp_prices": await _comp_price_map(db), "comp_method": await competitive_method(db),
    })


async def _comp_price_map(db, product_id=None) -> dict:
    """{product_id: {list_id: price}}"""
    q = select(PMCompetitorPrice.product_id, PMCompetitorPrice.list_id, PMCompetitorPrice.price)
    if product_id is not None:
        q = q.where(PMCompetitorPrice.product_id == product_id)
    out = {}
    for pid, lid, price in (await db.execute(q)).all():
        out.setdefault(str(pid), {})[str(lid)] = float(price)
    return out


@router.post("/pricing/{product_id}/save")
async def pricing_save(product_id: str, request: Request,
                       ram_type: str = Form(""), ram_size: str = Form(""),
                       storage_type: str = Form(""), storage_size: str = Form(""),
                       db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(require_module_perm(PERM_PRICING))):
    """RAM / Storage plus the competitor prices (form fields comp_<list id>).
    Sell Price is never posted: it is the Formula Method over the filled
    competitor prices (recompute_sell_prices). RAM is the Hardware RAM row
    named "<Type>/<Size>", so its price comes from the Hardware page."""
    p = (await db.execute(select(PMProduct).where(PMProduct.id == _uuid_or_none(product_id)))).scalar_one_or_none()
    if not p:
        return _err("Product not found.", 404)
    form = await request.form()

    # RAM / Storage picks are saved as chosen — no validation (2026-10-18).
    # A pick whose Hardware price isn't set yet shows Pending on Product
    # Master and is priced automatically once Hardware saves it.
    def clean(v):
        return (v or "").strip()[:100] or None
    p.ram_type, p.ram_size = clean(ram_type), clean(ram_size)
    p.storage_type, p.storage_size = clean(storage_type), clean(storage_size)
    rams, stos = await _hw_maps(db)
    ram, sto = _resolve(p, rams, stos)

    lists = await _comp_lists(db)
    new_prices = {}
    try:
        for cl in lists:
            new_prices[cl.id] = _money(form.get(f"comp_{cl.id}", ""), cl.name)
    except ValueError as e:
        return _err(str(e))
    existing = {r.list_id: r for r in (await db.execute(select(PMCompetitorPrice).where(
        PMCompetitorPrice.product_id == p.id))).scalars().all()}
    old = {"ram_id": str(p.ram_id) if p.ram_id else None, "storage_id": str(p.storage_id) if p.storage_id else None,
           "sell_price": str(p.product_price) if p.product_price is not None else None,
           "competitor": {str(k): str(v.price) for k, v in existing.items()}}
    for lid, price in new_prices.items():
        cur = existing.get(lid)
        if price is None:
            if cur is not None:
                await db.delete(cur)
        elif cur is None:
            db.add(PMCompetitorPrice(product_id=p.id, list_id=lid, price=price, updated_by=current_user.username))
        else:
            cur.price, cur.updated_by = price, current_user.username
    p.ram_id = ram.id if ram is not None else None
    p.storage_id = sto.id if sto is not None else None
    await db.flush()
    if lists:
        await recompute_sell_prices(db, [p.id])
    p.updated_at = app_now()
    await audit(db, action="PM_PRODUCT_PRICE_SET", user=current_user, table_name="pm_products",
                record_id=str(p.id), old_value=old,
                new_value={"ram": _pick_name(p.ram_type, p.ram_size), "storage": _pick_name(p.storage_type, p.storage_size),
                           "sell_price": str(p.product_price) if p.product_price is not None else None,
                           "competitor": {str(k): (str(v) if v is not None else None) for k, v in new_prices.items()}},
                request=request)
    await db.commit()
    return JSONResponse({"ok": True, "product": await _product_payload(db, p.id)})


# ── Spare Parts page ──────────────────────────────────────────────────────────

@router.get("/spare-parts", response_class=HTMLResponse)
async def spare_parts_page(request: Request, db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(require_module_perm(PERM_SPARE))):
    return templates.TemplateResponse("product_master/spare_parts.html", {
        "request": request, "current_user": current_user,
        "products": await _product_rows(db), "part_fields": PART_FIELDS,
    })


@router.post("/spare-parts/{product_id}/save")
async def spare_parts_save(product_id: str, request: Request, db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(require_module_perm(PERM_SPARE))):
    p = (await db.execute(select(PMProduct).where(PMProduct.id == _uuid_or_none(product_id)))).scalar_one_or_none()
    if not p:
        return _err("Product not found.", 404)
    form = await request.form()
    new = {}
    try:
        for col, label in PART_FIELDS:
            new[col] = _money(form.get(col, ""), label)
    except ValueError as e:
        return _err(str(e))
    if all(v is None for v in new.values()):
        return _err("Enter at least one part price.")
    old = {col: (str(getattr(p, col)) if getattr(p, col) is not None else None) for col, _ in PART_FIELDS}
    for col, v in new.items():
        setattr(p, col, v)
    await audit(db, action="PM_PART_PRICES_SET", user=current_user, table_name="pm_products",
                record_id=str(p.id), old_value=old,
                new_value={c: (str(v) if v is not None else None) for c, v in new.items()}, request=request)
    await db.commit()
    return JSONResponse({"ok": True, "product": await _product_payload(db, p.id)})
