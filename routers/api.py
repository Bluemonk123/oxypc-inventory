from utils.price_export import blank_prices as _blank_prices   # Pricing View (2026-11-03)
"""
JSON API endpoints for Smart UX auto-fill and async lookups.
All routes require an authenticated session.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from sqlalchemy.exc import DBAPIError

from database import get_db
from models.lot import Lot, LotLineItem
from models.grn_import import GRNImport
from models.master import MasterData
from models.dealers import Dealer
from models.device import Device
from models.stage_control import AllowedTransition
from auth.dependencies import get_current_user
from models.user import User

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/lot-line-item/{item_id}")
async def get_lot_line_item(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return LotLineItem fields for IQC form auto-fill."""
    result = await db.execute(select(LotLineItem).where(LotLineItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Line item not found")
    return JSONResponse(_blank_prices(current_user, 'lots', {
        "brand": item.brand or "",
        "model": item.model or "",
        "cpu": item.cpu or "",
        "generation": str(item.generation or ""),
        "ram_gb": str(item.ram_gb or ""),
        "storage_gb": str(item.storage_gb or ""),
        "storage_type": item.storage_type or "",
        "unit_price": str(item.unit_price or ""),
    }, ['unit_price']))


@router.get("/lot-line-items-by-lot/{lot_id}")
async def get_lot_line_items(
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return all line items for a lot (for IQC form dropdown population)."""
    result = await db.execute(
        select(LotLineItem).where(LotLineItem.lot_id == lot_id).order_by(LotLineItem.sub_category)
    )
    items = result.scalars().all()
    return JSONResponse(_blank_prices(current_user, 'lots', [{
        "id": str(i.id),
        "sub_category": i.sub_category or "",
        "brand": i.brand or "",
        "model": i.model or "",
        "unit_price": str(i.unit_price or "0"),
    } for i in items], ['unit_price']))


@router.get("/lot-meta")
async def get_lot_meta(
    lot_id: str = "",
    tag_prefix: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Resolve a lot plus the GRN / invoice numbers wired to it.

    Two ways in, so the IQC form can use one endpoint for both behaviours:
      - `tag_prefix` — the first 4 characters of a scanned Tag Number, which
        encode the lot, so scanning selects the Lot dropdown by itself.
      - `lot_id`     — the operator picked a lot manually.

    `grn_number` / `invoice_number` come back empty when nothing is wired to the
    lot; the form leaves those inputs editable rather than blocking entry.
    """
    lot = None
    if lot_id:
        try:
            lot = (await db.execute(select(Lot).where(Lot.id == lot_id))).scalars().first()
        except (ValueError, DBAPIError):
            lot = None          # malformed UUID — treat as "no match", not a 500
    elif tag_prefix:
        p = tag_prefix.strip()
        lot = (await db.execute(select(Lot).where(Lot.lot_number == p))).scalars().first()
        if not lot:
            lot = (await db.execute(
                select(Lot).where(Lot.lot_number.ilike(f"{p}%")).limit(1)
            )).scalars().first()
    if not lot:
        return JSONResponse({"found": False})

    # A lot can carry several GRNs; the most recent one is what a fresh IQC
    # entry belongs to. Fall back to the lot's own invoice reference when no
    # GRN has been imported yet.
    # Match via the lot_id FK first (the mapping set by GRN in TRC's Add/Edit
    # Lot flow) — the free-text lot_number match is kept as a fallback for
    # older rows that predate that FK, since not every GRN gets lot_number
    # written back to it.
    grn = (await db.execute(
        select(GRNImport)
        .where(or_(GRNImport.lot_id == lot.id, GRNImport.lot_number == lot.lot_number),
               GRNImport.is_deleted == False)
        .order_by(GRNImport.created_at.desc()).limit(1)
    )).scalars().first()
    return JSONResponse({
        "found": True,
        "lot_id": str(lot.id),
        "lot_number": lot.lot_number,
        "grn_number": (grn.grn_number if grn else "") or "",
        "invoice_number": (grn.invoice_number if grn else None)
                          or getattr(lot, "invoice_number", None) or "",
    })


@router.get("/port-profile")
async def get_port_profile(
    brand: str = "",
    model: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Physical port counts for a Make + Model, from the `port_profile` master list.

    Windows cannot report how many USB-A / USB-C / RJ45 sockets a chassis has, so
    the agent's counts are a form-factor guess. A match here replaces them with
    the real figures. Rows are maintained in Master Data as
    `<Brand>|<Model>|A=<n>,C=<n>,E=<n>`.

    Matching is case-insensitive and takes the LONGEST matching model prefix, so
    "ThinkPad X1 Carbon" wins over "ThinkPad X1" for an X1 Carbon.
    """
    b, m = (brand or "").strip().lower(), (model or "").strip().lower()
    if not b or not m:
        return JSONResponse({"found": False})

    rows = (await db.execute(
        select(MasterData.value).where(MasterData.category == "port_profile",
                                       MasterData.is_active == True)
    )).scalars().all()

    best = None
    for raw in rows:
        parts = (raw or "").split("|")
        if len(parts) != 3:
            continue                     # malformed row — skip, don't 500
        r_brand, r_model, spec = (p.strip() for p in parts)
        if r_brand.lower() != b or not m.startswith(r_model.lower()):
            continue
        if best is None or len(r_model) > len(best[0]):
            best = (r_model, spec)

    if not best:
        return JSONResponse({"found": False})

    counts = {}
    for token in best[1].split(","):
        key, _, val = token.partition("=")
        try:
            counts[key.strip().upper()] = int(val)
        except ValueError:
            continue
    return JSONResponse({
        "found": True, "matched_model": best[0],
        "usb_a_ports": counts.get("A"),
        "usb_c_ports": counts.get("C"),
        "ethernet_ports": counts.get("E"),
    })


@router.get("/dealers/search")
async def search_dealers(
    q: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full-text dealer search for typeahead/autocomplete widgets."""
    result = await db.execute(
        select(Dealer)
        .where(Dealer.business_name.ilike(f"%{q}%"))
        .limit(10)
    )
    dealers = result.scalars().all()
    return JSONResponse([{
        "id": str(d.id),
        "name": d.business_name,
        "phone": d.phone or "",
        "state": d.state or "",
        "city": d.city or "",
    } for d in dealers])


@router.get("/device/{device_id}/next-stages")
async def get_next_stages(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return allowed next stages for a device based on AllowedTransitions table."""
    dev = (await db.execute(
        select(Device).where(Device.id == device_id)
    )).scalar_one_or_none()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    if not dev.current_stage:
        return JSONResponse([])
    transitions = (await db.execute(
        select(AllowedTransition).where(
            AllowedTransition.from_stage == dev.current_stage.value
        )
    )).scalars().all()
    return JSONResponse([{
        "stage": t.to_stage,
        "label": t.to_stage.replace("_", " ").title(),
    } for t in transitions])
