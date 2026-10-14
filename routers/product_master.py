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

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
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
    build_product_name, parts_done, product_figures,
)
from services.audit_engine import audit

router = APIRouter(prefix="/product-master", tags=["product-master"], dependencies=[Depends(verify_csrf)])

PERM_PRODUCT = "pm_product_master"
PERM_HARDWARE = "pm_hardware"
PERM_SPARE = "pm_spare_parts"
PERM_PRICING = "pm_product_pricing"


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


async def _product_rows(db: AsyncSession) -> list:
    rows = (await db.execute(
        select(PMProduct, HWRam, HWStorage)
        .join(HWRam, PMProduct.ram_id == HWRam.id)
        .join(HWStorage, PMProduct.storage_id == HWStorage.id)
        .order_by(PMProduct.updated_at.desc())
    )).all()
    return [_product_dict(p, r, s) for p, r, s in rows]


def _product_dict(p: PMProduct, ram: HWRam, sto: HWStorage) -> dict:
    fig = product_figures(p, ram.ram_price, sto.storage_price)
    return {
        "id": str(p.id), "product_name": p.product_name,
        "make": p.make, "model": p.model, "cpu": p.cpu, "cpu_gen": p.cpu_gen,
        "ram_id": str(ram.id), "ram": ram.ram_name, "ram_price": _num(ram.ram_price),
        "storage_id": str(sto.id), "storage": sto.storage_name, "storage_price": _num(sto.storage_price),
        "product_price": _num(p.product_price),
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
    p, r, s = (await db.execute(
        select(PMProduct, HWRam, HWStorage)
        .join(HWRam, PMProduct.ram_id == HWRam.id)
        .join(HWStorage, PMProduct.storage_id == HWStorage.id)
        .where(PMProduct.id == product_id)
    )).one()
    return _product_dict(p, r, s)


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
        if not parent:
            return _err(f"Select a {DROPDOWN_LABELS[DROPDOWN_PARENT[category]]}.")
    if category == "cpu_gen":
        value = f"{parent.value}/{value}"
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
                       cpu_gen_id: str = Form(""), ram_id: str = Form(""), storage_id: str = Form(""),
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
    gen = await pick("cpu_gen", cpu_gen_id, cpu) if cpu else None
    ram = (await db.execute(select(HWRam).where(HWRam.id == _uuid_or_none(ram_id)))).scalar_one_or_none()
    sto = (await db.execute(select(HWStorage).where(HWStorage.id == _uuid_or_none(storage_id)))).scalar_one_or_none()
    missing = [n for n, v in (("Make", make), ("Model", model), ("CPU", cpu), ("CPU Gen", gen),
                              ("RAM", ram), ("Storage", sto)) if v is None]
    if missing:
        return _err("Select " + ", ".join(missing) + ".")

    name = build_product_name(make.value, model.value, gen.value, ram.ram_name, sto.storage_name)
    dup = (await db.execute(select(PMProduct).where(func.lower(PMProduct.product_name) == name.lower())
                            )).scalars().first()
    values = {"make": make.value, "model": model.value, "cpu": cpu.value, "cpu_gen": gen.value,
              "ram_id": ram.id, "storage_id": sto.id, "product_name": name}
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
        p = PMProduct(created_by=current_user.username, **values)
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


# ── Hardware page ─────────────────────────────────────────────────────────────

@router.get("/hardware", response_class=HTMLResponse)
async def hardware_page(request: Request, db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(require_module_perm(PERM_HARDWARE))):
    dd = await _dropdowns(db)
    rams = (await db.execute(select(HWRam).order_by(HWRam.ram_name))).scalars().all()
    stos = (await db.execute(select(HWStorage).order_by(HWStorage.storage_name))).scalars().all()
    return templates.TemplateResponse("product_master/hardware.html", {
        "request": request, "current_user": current_user,
        "rams": rams, "storages": stos, "dropdowns": dd,
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
        if old_name != name:
            # products carry the RAM / Storage name inside their Product Name
            fk = PMProduct.ram_id if kind == "ram" else PMProduct.storage_id
            for p, r, s in (await db.execute(
                    select(PMProduct, HWRam, HWStorage)
                    .join(HWRam, PMProduct.ram_id == HWRam.id)
                    .join(HWStorage, PMProduct.storage_id == HWStorage.id)
                    .where(fk == row.id))).all():
                p.product_name = build_product_name(p.make, p.model, p.cpu_gen, r.ram_name, s.storage_name)
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
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _err("Could not save — a product with the same name already exists.")
    return JSONResponse({"ok": True})


async def _hw_delete(request, db, current_user, kind, item_id):
    Model = HWRam if kind == "ram" else HWStorage
    label = "RAM" if kind == "ram" else "Storage"
    row = (await db.execute(select(Model).where(Model.id == _uuid_or_none(item_id)))).scalar_one_or_none()
    if not row:
        return _err(f"{label} not found.", 404)
    fk = PMProduct.ram_id if kind == "ram" else PMProduct.storage_id
    used = (await db.execute(select(func.count()).select_from(PMProduct).where(fk == row.id))).scalar() or 0
    if used:
        return _err(f"{used} product(s) use this {label}. Change or delete them first.")
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


# ── Product Pricing page ──────────────────────────────────────────────────────

@router.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request, db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(require_module_perm(PERM_PRICING))):
    return templates.TemplateResponse("product_master/pricing.html", {
        "request": request, "current_user": current_user, "products": await _product_rows(db),
    })


@router.post("/pricing/{product_id}/save")
async def pricing_save(product_id: str, request: Request, product_price: str = Form(""),
                       db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(require_module_perm(PERM_PRICING))):
    p = (await db.execute(select(PMProduct).where(PMProduct.id == _uuid_or_none(product_id)))).scalar_one_or_none()
    if not p:
        return _err("Product not found.", 404)
    try:
        amount = _money(product_price, "Product Price")
    except ValueError as e:
        return _err(str(e))
    if amount is None:
        return _err("Enter the Product Price.")
    old = str(p.product_price) if p.product_price is not None else None
    p.product_price = amount
    await audit(db, action="PM_PRODUCT_PRICE_SET", user=current_user, table_name="pm_products",
                record_id=str(p.id), old_value={"product_price": old},
                new_value={"product_price": str(amount)}, request=request)
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
