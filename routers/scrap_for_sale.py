"""
Scrap for Sale — batches created from /transfers when Transfer Type =
Scrap for Sale. Each batch (ScrapForSale) is a Scrap ID; its Tag Numbers
and Spare Parts are the StockTransfer rows that reference it
(StockTransfer.scrap_for_sale_id) — same "the transfer log is the
membership list" pattern buckets/lots already use.
"""
import uuid as _uuid
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import quote_plus
from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from templates_config import templates
from database import get_db
from models.user import User, UserRole
from models.device import Device, DeviceStage, StageMovement
from models.stock_transfer import StockTransfer
from models.scrap_for_sale import ScrapForSale
from models.scrap_sale import ScrapSale, ScrapSaleItem, ScrapSaleTag
from models.company import Company
from auth.dependencies import get_current_user, require_roles, verify_csrf, require_module_perm
from utils.master_data import entity_values, master_values
from utils.sales_person import sales_person_options
from utils.timezone import app_now
from services.audit_engine import audit

router = APIRouter(tags=["scrap_for_sale"], dependencies=[Depends(verify_csrf)])
allowed = require_roles(UserRole.admin, UserRole.inventory_manager, UserRole.sales_manager)
# The sale form also opens from Tags Scrapped (/scrap-products), which sales
# users can reach — they could already sell a scrapped tag from there.
sale_allowed = require_roles(UserRole.admin, UserRole.inventory_manager, UserRole.sales_manager,
                             UserRole.sales)


@router.get("/scrap-for-sale", response_class=HTMLResponse)
async def scrap_for_sale_list(request: Request, db: AsyncSession = Depends(get_db),
                              current_user: User = Depends(allowed)):
    batches = (await db.execute(
        select(ScrapForSale).where(ScrapForSale.is_trashed == False)  # noqa: E712
        .order_by(ScrapForSale.scrap_id.desc())
    )).scalars().all()

    assigned_ids = {b.assigned_to_user_id for b in batches if b.assigned_to_user_id}
    user_name_map = {}
    if assigned_ids:
        user_name_map = {
            u.id: (u.full_name or u.username)
            for u in (await db.execute(select(User).where(User.id.in_(assigned_ids)))).scalars().all()
        }

    rows = []
    if batches:
        batch_ids = [b.id for b in batches]
        transfers = (await db.execute(
            select(StockTransfer, Device.model)
            .outerjoin(Device, StockTransfer.device_id == Device.id)
            .where(StockTransfer.scrap_for_sale_id.in_(batch_ids))
        )).all()
        by_batch: dict = {}
        for t, device_model in transfers:
            by_batch.setdefault(t.scrap_for_sale_id, []).append((t, device_model))

        for b in batches:
            entries = by_batch.get(b.id, [])
            tag_models = Counter()
            part_categories = Counter()
            tags_qty = 0
            parts_qty = 0
            for t, device_model in entries:
                if t.move_kind == "parts":
                    part_categories[t.model or "—"] += (t.quantity or 0)
                    parts_qty += (t.quantity or 0)
                else:
                    tag_models[device_model or "—"] += 1
                    tags_qty += 1
            rows.append({
                "id": str(b.id), "scrap_id": b.scrap_id,
                "tag_count": tags_qty, "part_count": parts_qty,
                "tag_breakdown": [{"name": n, "count": c} for n, c in sorted(tag_models.items())],
                "part_breakdown": [{"name": n, "count": c} for n, c in sorted(part_categories.items())],
                "total_qty": tags_qty + parts_qty,
                "min_price": float(b.min_selling_price) if b.min_selling_price is not None else None,
                "max_price": float(b.max_selling_price) if b.max_selling_price is not None else None,
                "status": b.status,
                "assigned_to_name": user_name_map.get(b.assigned_to_user_id, "—"),
            })

    # "Devices Not Yet in a Batch" removed (2026-10-23): those tags are on
    # Tags Scrapped until they are Verified and batched through Transfers.
    return templates.TemplateResponse("scrap/for_sale.html", {
        "request": request, "current_user": current_user, "rows": rows,
        **(await _custom_sale_form_context(db)),
    })


# ── Custom Sale (2026-10-16) ──────────────────────────────────────────────────
# A stand-alone scrap sale entered from the "Custom Sale" modal on this page and
# listed on the Scrap Sale Report page (/scrap-sale-report). It does not touch
# any Tag Number or Scrap ID batch and is NOT part of Business P&L. Totals are
# always recomputed here from the raw item fields — never read from the client.

SCRAP_SALE_REPORT_MODULE = "scrap_sale_report"   # OFF by default (DEFAULT_DISABLED_MODULES)


def _company_details(c) -> dict:
    return {
        "company_id": str(c.id), "name": c.company_name or "", "address": c.company_address or "",
        "gstin": c.company_gstin or "", "state": c.company_state or "",
        "phone": c.company_phone or "", "email": c.company_email or "",
    }


async def _entity_companies(db: AsyncSession) -> dict:
    """{entity: Company} — the oldest active Company Setting row tagged with
    each entity (same rule routers/sales.py uses to pick a sale's company)."""
    out = {}
    for c in (await db.execute(
        select(Company).where(Company.is_active == True).order_by(Company.created_at)  # noqa: E712
    )).scalars().all():
        if c.company_entity:
            out.setdefault(c.company_entity, c)
    return out


async def _custom_sale_form_context(db: AsyncSession) -> dict:
    by_entity = await _entity_companies(db)
    return {
        "cs_entities": await entity_values(db),
        "cs_entity_details": {e: _company_details(c) for e, c in by_entity.items()},
        "cs_categories": await master_values(db, "sub_category"),
        "cs_sales_people": await sales_person_options(db),
        "cs_today": app_now().date().isoformat(),
    }


def _dec(v: str):
    v = (v or "").strip().replace(",", "")
    if not v:
        return None
    try:
        d = Decimal(v)
    except (InvalidOperation, ValueError):
        raise ValueError(f"'{v}' is not a number")
    if d < 0:
        raise ValueError("Amounts cannot be negative")
    return d


async def _next_scrap_sale_number(db: AsyncSession) -> str:
    nums = (await db.execute(select(ScrapSale.sale_number))).scalars().all()
    top = 0
    for n in nums:
        try:
            top = max(top, int(str(n).split("-")[-1]))
        except ValueError:
            continue
    return f"SCS-{top + 1:04d}"


def _cs_redirect(msg: str, ok: bool = False):
    key = "success" if ok else "error"
    return RedirectResponse(url=f"/scrap-for-sale?{key}={quote_plus(msg)}", status_code=302)


# ── Sell from a Scrap ID batch / from Tags Scrapped (2026-10-23) ────────────
# Both open the Custom Sale form filled with the tags (grouped by model) and,
# for a batch, its parts. Saving links the tags (scrap_sale_tags) and the
# batch (scrap_sales.scrap_for_sale_id) to the sale, moves the tags to Sold
# and marks the batch Sold, so the sale shows on the Scrap Sale Report.

async def _batch_sell_tags(db: AsyncSession, batch) -> tuple[list, list]:
    """(devices still in Scrap for Sale in this batch, part transfer rows)."""
    trs = (await db.execute(
        select(StockTransfer).where(StockTransfer.scrap_for_sale_id == batch.id)
    )).scalars().all()
    dev_ids = [t.device_id for t in trs if t.device_id and t.move_kind != "parts"]
    devices = []
    if dev_ids:
        devices = (await db.execute(
            select(Device).where(Device.id.in_(dev_ids), Device.is_trashed.isnot(True),
                                 Device.current_stage == DeviceStage.scrap_for_sale)
            .order_by(Device.barcode)
        )).scalars().all()
    return devices, [t for t in trs if t.move_kind == "parts"]


async def _tags_sell_devices(db: AsyncSession, barcodes: list) -> tuple[list, list]:
    """(sellable devices, [(barcode, reason)]) for Sell on Tags Scrapped:
    on Tags Scrapped (Scrap for Sale stage, not in a live batch) and Verified."""
    from services.scrap_flow import batched_device_ids
    ups = [b.upper() for b in barcodes]
    found = (await db.execute(
        select(Device).where(func.upper(Device.barcode).in_(ups))
    )).scalars().all() if ups else []
    in_batch = await batched_device_ids(db, [d.id for d in found])
    by_up = {d.barcode.upper(): d for d in found}
    ok, bad = [], []
    for b in barcodes:
        d = by_up.get(b.upper())
        if d is None:
            bad.append((b, "not found"))
        elif d.is_trashed or d.current_stage != DeviceStage.scrap_for_sale:
            bad.append((b, "not on Tags Scrapped"))
        elif d.id in in_batch:
            bad.append((b, f"in Scrap ID {in_batch[d.id]} — sell the batch"))
        elif not d.scrap_verified:
            bad.append((b, "not verified"))
        elif d not in ok:
            ok.append(d)
    return ok, bad


def _tag_lines(devices) -> list:
    groups = {}
    for d in devices:
        key = (" ".join(x for x in (d.brand, d.model) if x) or "Scrap device", d.sub_category or "")
        groups[key] = groups.get(key, 0) + 1
    return [{"name": n, "category": c, "qty": q} for (n, c), q in sorted(groups.items())]


@router.get("/scrap-for-sale/sell-prefill")
async def sell_prefill(batch: str = "", tags: str = "", db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(sale_allowed)):
    from services.scrap_flow import refused_text
    if batch:
        try:
            b = (await db.execute(select(ScrapForSale).where(ScrapForSale.id == _uuid.UUID(batch)))).scalar_one_or_none()
        except ValueError:
            b = None
        if b is None or b.is_trashed:
            return JSONResponse({"ok": False, "error": "Scrap ID not found."})
        if b.status == "sold":
            return JSONResponse({"ok": False, "error": f"Scrap ID {b.scrap_id} is already sold."})
        devices, parts = await _batch_sell_tags(db, b)
        lines = _tag_lines(devices) + [
            {"name": t.model or "Spare part", "category": t.category or "", "qty": t.quantity or 1} for t in parts]
        if not lines:
            return JSONResponse({"ok": False, "error": f"Scrap ID {b.scrap_id} has nothing left to sell."})
        return JSONResponse({"ok": True, "title": f"Sell Scrap ID {b.scrap_id}", "scrap_for_sale_id": str(b.id),
                             "barcodes": [d.barcode for d in devices], "lines": lines})
    wanted = [t.strip() for t in tags.split(",") if t.strip()]
    ok, bad = await _tags_sell_devices(db, wanted)
    if not ok:
        return JSONResponse({"ok": False, "error": "Nothing to sell. " + refused_text(bad)})
    return JSONResponse({"ok": True, "title": f"Sell {len(ok)} Scrapped Tag(s)", "scrap_for_sale_id": "",
                         "barcodes": [d.barcode for d in ok], "lines": _tag_lines(ok),
                         "skipped": refused_text(bad) if bad else ""})


@router.post("/scrap-for-sale/custom-sale")
async def create_custom_scrap_sale(request: Request, db: AsyncSession = Depends(get_db),
                                   current_user: User = Depends(sale_allowed)):
    form = await request.form()
    g = lambda k: (form.get(k) or "").strip()
    # Where to land afterwards: Tags Scrapped when the form was opened there.
    back = "/scrap-products" if g("back") == "scrap-products" else "/scrap-for-sale"

    def _cs_redirect(msg: str, ok: bool = False):
        key = "success" if ok else "error"
        return RedirectResponse(url=f"{back}?{key}={quote_plus(msg)}", status_code=302)

    # Selling a Scrap ID batch or Tags Scrapped tags (2026-10-23).
    batch, sell_devices = None, []
    sell_barcodes = [b.strip() for b in g("device_barcodes").split(",") if b.strip()]
    if g("scrap_for_sale_id"):
        try:
            batch = (await db.execute(select(ScrapForSale).where(
                ScrapForSale.id == _uuid.UUID(g("scrap_for_sale_id"))))).scalar_one_or_none()
        except ValueError:
            batch = None
        if batch is None or batch.is_trashed:
            return _cs_redirect("Scrap ID not found.")
        if batch.status == "sold":
            return _cs_redirect(f"Scrap ID {batch.scrap_id} is already sold.")
        sell_devices, _parts = await _batch_sell_tags(db, batch)
    elif sell_barcodes:
        from services.scrap_flow import refused_text
        sell_devices, bad = await _tags_sell_devices(db, sell_barcodes)
        if bad:
            return _cs_redirect("Not sold — " + refused_text(bad))

    entity = g("entity")
    if not entity:
        return _cs_redirect("Select an Entity.")
    try:
        sale_date = date.fromisoformat(g("sale_date"))
    except ValueError:
        return _cs_redirect("Enter a valid Sale Date.")
    customer_company = g("customer_company")
    if not customer_company:
        return _cs_redirect("Enter the customer's Company Name.")

    names = form.getlist("item_name")
    cats = form.getlist("item_category")
    weights = form.getlist("total_weight")
    wprices = form.getlist("unit_price_weight")
    qtys = form.getlist("quantity")
    uprices = form.getlist("unit_price")

    items, total = [], Decimal("0")
    for i, name in enumerate(names):
        pick = lambda arr: arr[i] if i < len(arr) else ""
        name = (name or "").strip()
        raw = [pick(weights), pick(wprices), pick(qtys), pick(uprices)]
        if not name and not any((x or "").strip() for x in raw):
            continue  # blank row
        line = i + 1
        if not name:
            return _cs_redirect(f"Item row {line}: enter the Item Name.")
        try:
            w, wp, uq, up = (_dec(x) for x in raw)
        except ValueError as e:
            return _cs_redirect(f"Item row {line}: {e}.")
        has_w = w is not None or wp is not None
        has_q = uq is not None or up is not None
        if has_w and has_q:
            return _cs_redirect(f"Item row {line}: use either Weight x Price/Weight or Quantity x Unit Price, not both.")
        if has_w:
            if w is None or wp is None:
                return _cs_redirect(f"Item row {line}: enter both Total Weight and Unit Price (Weight).")
            basis, line_total, qty_int = "weight", w * wp, None
        elif has_q:
            if uq is None or up is None:
                return _cs_redirect(f"Item row {line}: enter both Quantity and Unit Price.")
            if uq != uq.to_integral_value():
                return _cs_redirect(f"Item row {line}: Quantity must be a whole number.")
            basis, line_total, qty_int = "quantity", uq * up, int(uq)
        else:
            return _cs_redirect(f"Item row {line}: enter Total Weight x Unit Price (Weight) or Quantity x Unit Price.")
        line_total = line_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total += line_total
        items.append(ScrapSaleItem(
            line_no=len(items) + 1, item_name=name[:200],
            item_category=(pick(cats) or "").strip()[:200] or None,
            total_weight=w if basis == "weight" else None,
            unit_price_weight=wp if basis == "weight" else None,
            quantity=qty_int, unit_price=up if basis == "quantity" else None,
            pricing_basis=basis, total_price=line_total,
        ))
    if not items:
        return _cs_redirect("Add at least one item.")

    company = (await _entity_companies(db)).get(entity)
    sale = ScrapSale(
        sale_number=await _next_scrap_sale_number(db),
        company_id=company.id if company else None,
        entity=entity[:50],
        entity_name=company.company_name if company else None,
        entity_address=company.company_address if company else None,
        entity_gstin=company.company_gstin if company else None,
        entity_state=company.company_state if company else None,
        entity_phone=company.company_phone if company else None,
        entity_email=company.company_email if company else None,
        sale_date=sale_date,
        sales_person=g("sales_person")[:100] or None,
        invoice_number=g("invoice_number")[:50] or None,
        customer_company=customer_company[:200],
        customer_phone=g("customer_phone")[:50] or None,
        customer_email=g("customer_email")[:100] or None,
        customer_address=g("customer_address")[:500] or None,
        customer_city=g("customer_city")[:100] or None,
        customer_state=g("customer_state")[:100] or None,
        total_sale_price=total.quantize(Decimal("0.01")),
        created_by=current_user.username,
        items=items,
    )
    if batch is not None:
        sale.scrap_for_sale_id = batch.id
    db.add(sale)
    await db.flush()
    sold_barcodes = []
    for d in sell_devices:
        db.add(ScrapSaleTag(scrap_sale_id=sale.id, device_id=d.id))
        prev = d.current_stage
        d.current_stage = DeviceStage.sold
        d.updated_at = app_now()
        db.add(StageMovement(device_id=d.id, from_stage=prev, to_stage=DeviceStage.sold,
                             moved_by=current_user.username,
                             notes=f"Sold in scrap sale {sale.sale_number}"
                                   + (f" (Scrap ID {batch.scrap_id})" if batch is not None else "")))
        sold_barcodes.append(d.barcode)
    if batch is not None:
        batch.status = "sold"
    await audit(db, user=current_user, action="SCRAP_CUSTOM_SALE_CREATED",
                table_name="scrap_sales", record_id=str(sale.id),
                new_value={"sale_number": sale.sale_number, "entity": entity,
                           "customer_company": sale.customer_company,
                           "invoice_number": sale.invoice_number,
                           "items": len(items), "total_sale_price": str(sale.total_sale_price),
                           "scrap_id": batch.scrap_id if batch is not None else None,
                           "tags_sold": sold_barcodes},
                request=request)
    await db.commit()
    what = (f"Scrap ID {batch.scrap_id} sold" if batch is not None
            else f"{len(sold_barcodes)} tag(s) sold" if sold_barcodes else "Custom Sale")
    return _cs_redirect(f"{what}: {sale.sale_number} saved (Rs {sale.total_sale_price:,.2f})", ok=True)


@router.get("/scrap-sale-report", response_class=HTMLResponse)
async def scrap_sale_report(request: Request, db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(require_module_perm(SCRAP_SALE_REPORT_MODULE))):
    sales = (await db.execute(
        select(ScrapSale).where(ScrapSale.is_trashed.isnot(True))
        .order_by(ScrapSale.sale_date.desc(), ScrapSale.sale_number.desc())
    )).scalars().all()
    # Entity name LIVE from Company Setting (2026-10-22, on request); the copy
    # saved on the sale is used only if its company row is gone.
    from models.company import Company
    _cids = list({s.company_id for s in sales if s.company_id})
    _live = {c.id: c for c in (await db.execute(select(Company).where(Company.id.in_(_cids)))).scalars().all()} if _cids else {}
    # Scrap ID and Tag Numbers sold in each sale (2026-10-23).
    _bids = list({s.scrap_for_sale_id for s in sales if s.scrap_for_sale_id})
    _scrap_ids = dict((await db.execute(
        select(ScrapForSale.id, ScrapForSale.scrap_id).where(ScrapForSale.id.in_(_bids))
    )).all()) if _bids else {}
    _tag_ids = [t.device_id for s in sales for t in s.tags]
    _bc = dict((await db.execute(select(Device.id, Device.barcode).where(Device.id.in_(_tag_ids)))).all()) if _tag_ids else {}
    rows = [{
        "sale_number": s.sale_number, "sale_date": s.sale_date.isoformat(),
        "sale_date_label": s.sale_date.strftime("%d-%m-%Y"),
        "entity": s.entity or "—", "entity_name": (_live[s.company_id].company_name if s.company_id in _live else s.entity_name) or "",
        "invoice_number": s.invoice_number or "—", "sales_person": s.sales_person or "—",
        "customer_company": s.customer_company, "customer_phone": s.customer_phone or "",
        "customer_email": s.customer_email or "",
        "customer_place": ", ".join(x for x in (s.customer_city, s.customer_state) if x),
        "customer_address": s.customer_address or "",
        "item_count": len(s.items), "total": float(s.total_sale_price or 0),
        "scrap_id": _scrap_ids.get(s.scrap_for_sale_id, "") if s.scrap_for_sale_id else "",
        "tags": sorted(_bc.get(t.device_id, "") for t in s.tags),
        "created_by": s.created_by or "—",
        "items": [{
            "name": it.item_name, "category": it.item_category or "—", "basis": it.pricing_basis,
            "weight": float(it.total_weight) if it.total_weight is not None else None,
            "price_weight": float(it.unit_price_weight) if it.unit_price_weight is not None else None,
            "qty": it.quantity,
            "unit_price": float(it.unit_price) if it.unit_price is not None else None,
            "total": float(it.total_price or 0),
        } for it in s.items],
    } for s in sales]
    return templates.TemplateResponse("scrap/sale_report.html", {
        "request": request, "current_user": current_user, "rows": rows,
        "grand_total": sum(r["total"] for r in rows),
    })


@router.post("/scrap-for-sale/{scrap_id}/price")
async def set_scrap_for_sale_price(
    request: Request, scrap_id: str,
    min_selling_price: str = Form(""), max_selling_price: str = Form(""),
    db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed),
):
    try:
        bid = _uuid.UUID(scrap_id)
    except ValueError:
        raise HTTPException(404)
    b = (await db.execute(select(ScrapForSale).where(ScrapForSale.id == bid))).scalar_one_or_none()
    if not b:
        raise HTTPException(404, "Scrap for Sale batch not found")
    try:
        b.min_selling_price = Decimal(min_selling_price) if min_selling_price.strip() else None
        b.max_selling_price = Decimal(max_selling_price) if max_selling_price.strip() else None
    except Exception:
        return RedirectResponse(url="/scrap-for-sale?error=Invalid+price", status_code=302)
    await audit(db, user=current_user, action="SCRAP_FOR_SALE_PRICE_SET",
                table_name="scrap_for_sale", record_id=str(b.id),
                new_value={"min_selling_price": str(b.min_selling_price) if b.min_selling_price is not None else None,
                           "max_selling_price": str(b.max_selling_price) if b.max_selling_price is not None else None},
                request=request)
    await db.commit()
    return RedirectResponse(url="/scrap-for-sale?success=Selling+price+updated", status_code=302)


@router.post("/scrap-for-sale/{scrap_id}/sell")
async def sell_scrap_for_sale(request: Request, scrap_id: str,
                              db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed)):
    try:
        bid = _uuid.UUID(scrap_id)
    except ValueError:
        raise HTTPException(404)
    b = (await db.execute(select(ScrapForSale).where(ScrapForSale.id == bid))).scalar_one_or_none()
    if not b:
        raise HTTPException(404, "Scrap for Sale batch not found")
    b.status = "sold"
    await audit(db, user=current_user, action="SCRAP_FOR_SALE_SOLD",
                table_name="scrap_for_sale", record_id=str(b.id), new_value={"status": "sold"}, request=request)
    await db.commit()
    return RedirectResponse(url="/scrap-for-sale?success=Marked+sold", status_code=302)


@router.post("/scrap-for-sale/{scrap_id}/delete")
async def delete_scrap_for_sale(request: Request, scrap_id: str,
                                db: AsyncSession = Depends(get_db), current_user: User = Depends(allowed)):
    try:
        bid = _uuid.UUID(scrap_id)
    except ValueError:
        raise HTTPException(404)
    b = (await db.execute(select(ScrapForSale).where(ScrapForSale.id == bid))).scalar_one_or_none()
    if not b:
        raise HTTPException(404, "Scrap for Sale batch not found")
    b.is_trashed = True
    await audit(db, user=current_user, action="SCRAP_FOR_SALE_DELETED",
                table_name="scrap_for_sale", record_id=str(b.id), new_value={"is_trashed": True}, request=request)
    await db.commit()
    return RedirectResponse(url="/scrap-for-sale?success=Removed", status_code=302)
