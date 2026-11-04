from utils.price_export import blank_prices as _blank_prices   # Pricing View (2026-11-03)
"""
Market Intelligence Router
----------------------------
Tracks models/items available in the market via WhatsApp groups.
Enables buy/sell discovery and price benchmarking.

Endpoints:
  GET  /market                — dashboard listing
  GET  /market/add            — add entry form
  POST /market/add            — save new entry
  GET  /market/{id}/edit      — edit entry form
  POST /market/{id}/edit      — update entry
  POST /market/{id}/toggle    — activate / deactivate
  POST /market/{id}/delete    — soft delete
  GET  /market/api/search     — JSON search (AJAX)
  POST /market/from-message   — create entry from a WA group message
  POST /market/link-dealer    — link WA group to a dealer
"""
import re
from datetime import datetime
from utils.timezone import app_now
from templates_config import templates
from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func as sa_func
from database import get_db
from models.market import MarketAvailability
from models.whatsapp import WhatsAppGroup, WhatsAppMessage
from models.dealers import Dealer
from models.user import User
from auth.dependencies import get_current_user, verify_csrf

router = APIRouter(prefix="/market", tags=["market"], dependencies=[Depends(verify_csrf)])

PAGE_SIZE = 40


# ── Dashboard ──────────────────────────────────────────────────────────────
@router.get("", response_class=HTMLResponse)
async def market_dashboard(
    request:  Request,
    q:        str = Query(default=""),
    brand:    str = Query(default=""),
    category: str = Query(default=""),
    trade:    str = Query(default=""),   # buy / sell / ""
    active:   str = Query(default="1"),  # 1 = active only
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(MarketAvailability)
    if active == "1":
        query = query.where(MarketAvailability.is_active == True)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(
            MarketAvailability.model.ilike(like),
            MarketAvailability.brand.ilike(like),
            MarketAvailability.processor.ilike(like),
            MarketAvailability.notes.ilike(like),
            MarketAvailability.dealer_name.ilike(like),
            MarketAvailability.source_message_text.ilike(like),
        ))
    if brand.strip():
        query = query.where(MarketAvailability.brand.ilike(f"%{brand.strip()}%"))
    if category.strip():
        query = query.where(MarketAvailability.category.ilike(f"%{category.strip()}%"))
    if trade in ("buy", "sell"):
        query = query.where(MarketAvailability.trade_type == trade)

    result = await db.execute(
        query.order_by(MarketAvailability.posted_date.desc())
    )
    entries = result.scalars().all()
    total = len(entries)

    # Summary stats
    sell_total = (await db.execute(
        select(sa_func.count()).where(MarketAvailability.is_active == True, MarketAvailability.trade_type == "sell")
    )).scalar() or 0
    buy_total = (await db.execute(
        select(sa_func.count()).where(MarketAvailability.is_active == True, MarketAvailability.trade_type == "buy")
    )).scalar() or 0

    # Distinct brands / categories for filter dropdowns
    brands_result = await db.execute(
        select(MarketAvailability.brand).where(MarketAvailability.brand != None).distinct().order_by(MarketAvailability.brand)
    )
    all_brands = [r[0] for r in brands_result.fetchall() if r[0]]

    cats_result = await db.execute(
        select(MarketAvailability.category).where(MarketAvailability.category != None).distinct().order_by(MarketAvailability.category)
    )
    all_categories = [r[0] for r in cats_result.fetchall() if r[0]]

    # Active dealers for "add" form
    dealers_result = await db.execute(
        select(Dealer).where(Dealer.status == "active").order_by(Dealer.business_name)
    )
    dealers = dealers_result.scalars().all()

    # WA groups (dealer category) for quick-link
    groups_result = await db.execute(
        select(WhatsAppGroup).order_by(WhatsAppGroup.group_name)
    )
    wa_groups = groups_result.scalars().all()

    return templates.TemplateResponse("market/index.html", {
        "request":       request,
        "current_user":  current_user,
        "entries":       entries,
        "total":         total,
        "q":             q,
        "brand":         brand,
        "category":      category,
        "trade":         trade,
        "active":        active,
        "sell_total":    sell_total,
        "buy_total":     buy_total,
        "all_brands":    all_brands,
        "all_categories":all_categories,
        "dealers":       dealers,
        "wa_groups":     wa_groups,
    })


# ── Add entry ──────────────────────────────────────────────────────────────
@router.post("/add")
async def add_entry(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await request.form()

    dealer_id = form.get("dealer_id") or None
    dealer_name = None
    if dealer_id:
        d = (await db.execute(select(Dealer).where(Dealer.id == dealer_id))).scalar_one_or_none()
        if d:
            dealer_name = d.first_name or d.business_name

    group_wa_id = form.get("group_wa_id") or None
    group_name  = form.get("group_name")  or None

    entry = MarketAvailability(
        brand               = form.get("brand")          or None,
        model               = form.get("model")          or None,
        category            = form.get("category")       or None,
        generation          = form.get("generation")     or None,
        processor           = form.get("processor")      or None,
        ram                 = form.get("ram")            or None,
        storage             = form.get("storage")        or None,
        condition           = form.get("condition")      or "refurb",
        grade               = form.get("grade")          or None,
        trade_type          = form.get("trade_type")     or "sell",
        qty                 = int(form.get("qty") or 0) or None,
        price_per_unit      = float(form.get("price_per_unit") or 0) or None,
        warranty_months     = int(form.get("warranty_months") or 0) or None,
        is_negotiable       = form.get("is_negotiable") == "1",
        dealer_id           = dealer_id,
        dealer_name         = dealer_name,
        group_wa_id         = group_wa_id,
        group_name          = group_name,
        source_message_text = form.get("source_message_text") or None,
        notes               = form.get("notes")          or None,
        is_active           = True,
        created_by          = current_user.username,
    )
    db.add(entry)
    await db.commit()
    return RedirectResponse(url="/market?success=Entry+added", status_code=302)


# ── Edit entry ─────────────────────────────────────────────────────────────
@router.get("/{entry_id}/edit", response_class=HTMLResponse)
async def edit_entry_form(
    request:  Request,
    entry_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from utils.price_export import price_add_block   # market entries are prices (2026-11-03)
    _blk = price_add_block(current_user, "market", "/market")
    if _blk is not None:
        return _blk
    entry = (await db.execute(select(MarketAvailability).where(MarketAvailability.id == entry_id))).scalar_one_or_none()
    if not entry:
        return RedirectResponse(url="/market?error=Entry+not+found", status_code=302)

    dealers_result = await db.execute(select(Dealer).where(Dealer.status == "active").order_by(Dealer.business_name))
    dealers = dealers_result.scalars().all()

    groups_result = await db.execute(select(WhatsAppGroup).order_by(WhatsAppGroup.group_name))
    wa_groups = groups_result.scalars().all()

    return templates.TemplateResponse("market/form.html", {
        "request": request,
        "current_user": current_user,
        "entry": entry,
        "dealers": dealers,
        "wa_groups": wa_groups,
    })


@router.post("/{entry_id}/edit")
async def edit_entry_save(
    request:  Request,
    entry_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    entry = (await db.execute(select(MarketAvailability).where(MarketAvailability.id == entry_id))).scalar_one_or_none()
    if not entry:
        return RedirectResponse(url="/market?error=Entry+not+found", status_code=302)

    form = await request.form()
    dealer_id = form.get("dealer_id") or None
    dealer_name = entry.dealer_name
    if dealer_id:
        d = (await db.execute(select(Dealer).where(Dealer.id == dealer_id))).scalar_one_or_none()
        if d:
            dealer_name = d.first_name or d.business_name

    entry.brand               = form.get("brand")          or None
    entry.model               = form.get("model")          or None
    entry.category            = form.get("category")       or None
    entry.generation          = form.get("generation")     or None
    entry.processor           = form.get("processor")      or None
    entry.ram                 = form.get("ram")            or None
    entry.storage             = form.get("storage")        or None
    entry.condition           = form.get("condition")      or "refurb"
    entry.grade               = form.get("grade")          or None
    entry.trade_type          = form.get("trade_type")     or "sell"
    entry.qty                 = int(form.get("qty") or 0) or None
    entry.price_per_unit      = float(form.get("price_per_unit") or 0) or None
    entry.warranty_months     = int(form.get("warranty_months") or 0) or None
    entry.is_negotiable       = form.get("is_negotiable") == "1"
    entry.dealer_id           = dealer_id
    entry.dealer_name         = dealer_name
    entry.group_wa_id         = form.get("group_wa_id") or entry.group_wa_id
    entry.notes               = form.get("notes")          or None
    entry.updated_at          = app_now()

    await db.commit()
    return RedirectResponse(url="/market?success=Entry+updated", status_code=302)


# ── Toggle active ──────────────────────────────────────────────────────────
@router.post("/{entry_id}/toggle")
async def toggle_entry(
    request:  Request,
    entry_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    entry = (await db.execute(select(MarketAvailability).where(MarketAvailability.id == entry_id))).scalar_one_or_none()
    if entry:
        entry.is_active  = not entry.is_active
        entry.updated_at = app_now()
        await db.commit()
    ref = request.headers.get("referer", "/market")
    return RedirectResponse(url=ref, status_code=302)


# ── JSON search API ────────────────────────────────────────────────────────
@router.get("/api/search")
async def market_api_search(
    request: Request,
    q:       str = Query(default=""),
    trade:   str = Query(default=""),
    brand:   str = Query(default=""),
    category:str = Query(default=""),
    limit:   int = Query(default=20),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(MarketAvailability).where(MarketAvailability.is_active == True)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(
            MarketAvailability.model.ilike(like),
            MarketAvailability.brand.ilike(like),
            MarketAvailability.processor.ilike(like),
        ))
    if trade in ("buy", "sell"):
        query = query.where(MarketAvailability.trade_type == trade)
    if brand.strip():
        query = query.where(MarketAvailability.brand.ilike(f"%{brand.strip()}%"))
    if category.strip():
        query = query.where(MarketAvailability.category.ilike(f"%{category.strip()}%"))

    result = await db.execute(query.order_by(MarketAvailability.posted_date.desc()).limit(limit))
    entries = result.scalars().all()
    return JSONResponse(_blank_prices(current_user, 'market', [{
        "id":           str(e.id),
        "brand":        e.brand,
        "model":        e.model,
        "category":     e.category,
        "condition":    e.condition,
        "grade":        e.grade,
        "trade_type":   e.trade_type,
        "qty":          e.qty,
        "price":        float(e.price_per_unit) if e.price_per_unit else None,
        "warranty":     e.warranty_months,
        "dealer_name":  e.dealer_name,
        "group_name":   e.group_name,
        "posted_date":  e.posted_date.strftime("%d-%m-%Y") if e.posted_date else None,
    } for e in entries], ['price']))


# ── Create entry from a WA group message ──────────────────────────────────
@router.post("/from-message")
async def add_from_message(
    request: Request,
    message_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    msg = (await db.execute(select(WhatsAppMessage).where(WhatsAppMessage.id == message_id))).scalar_one_or_none()
    if not msg:
        return JSONResponse({"error": "Message not found"}, status_code=404)

    # Auto-parse the message text for common patterns
    parsed = _parse_message(msg.message_text or "")

    entry = MarketAvailability(
        brand               = parsed.get("brand"),
        model               = parsed.get("model"),
        category            = parsed.get("category"),
        qty                 = parsed.get("qty"),
        price_per_unit      = parsed.get("price"),
        trade_type          = parsed.get("trade_type", "sell"),
        group_wa_id         = msg.recipient_phone,
        group_name          = msg.recipient_name,
        source_message_id   = msg.id,
        source_message_text = msg.message_text,
        created_by          = current_user.username,
        is_active           = True,
    )
    db.add(entry)
    await db.commit()
    return JSONResponse({"success": True, "id": str(entry.id), "parsed": parsed})


# ── Link WA group to dealer ────────────────────────────────────────────────
@router.post("/link-dealer")
async def link_group_dealer(
    request:     Request,
    group_wa_id: str = Form(...),
    dealer_id:   str = Form(default=""),
    group_category: str = Form(default="other"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    group = (await db.execute(
        select(WhatsAppGroup).where(WhatsAppGroup.group_wa_id == group_wa_id)
    )).scalar_one_or_none()
    if group:
        group.linked_dealer_id = dealer_id or None
        group.group_category   = group_category or "other"
        await db.commit()
    return RedirectResponse(url="/whatsapp?tab=groups&success=Group+updated", status_code=302)


# ── Helper: parse WA message for model/qty/price ──────────────────────────
def _parse_message(text: str) -> dict:
    """
    Light regex parser: look for common patterns like:
    "HP 840 G3 x20 @15000"  / "Dell Latitude 5480 - 10 units - 18k"
    """
    result = {}
    lower  = text.lower()

    # trade type
    if any(w in lower for w in ["required", "need", "looking", "want", "buy", "purchase"]):
        result["trade_type"] = "buy"
    else:
        result["trade_type"] = "sell"

    # brand detection
    for brand in ["hp", "dell", "lenovo", "acer", "asus", "apple", "toshiba", "sony",
                  "samsung", "panasonic", "fujitsu", "msi", "lg"]:
        if brand in lower:
            result["brand"] = brand.upper() if brand != "apple" else "Apple"
            break

    # category
    for cat in ["laptop", "desktop", "monitor", "tft", "printer", "server", "tablet", "projector"]:
        if cat in lower:
            result["category"] = cat.title()
            break

    # qty: look for patterns like "x10", "10 units", "qty 5", "5 pcs", "5 nos"
    qty_match = re.search(r'(?:x|qty|quantity|units?|pcs?|nos?)[:\s]*(\d+)|\b(\d+)\s*(?:units?|pcs?|nos?|qty)', lower)
    if qty_match:
        result["qty"] = int(qty_match.group(1) or qty_match.group(2))

    # price: ₹15000, @15000, 15k, rs.15000
    price_match = re.search(r'(?:₹|rs\.?|@)\s*([0-9,]+(?:\.\d+)?)\s*k?|(\d+)\s*k\b', lower)
    if price_match:
        raw = price_match.group(1) or price_match.group(2)
        val = float(raw.replace(",", "")) if raw else 0
        if "k" in (price_match.group(0) or ""):
            val *= 1000
        result["price"] = val

    # model: look for alphanumeric patterns typical of laptop models
    # e.g. 840 G3, 5480, E14, X1 Carbon, ThinkPad E470
    model_match = re.search(
        r'\b([A-Z][a-z]*(?:\s+[A-Z0-9][A-Z0-9a-z]*){1,3})\b|'
        r'\b([A-Z]{2,}\d{3,}[A-Z0-9]*)\b|'
        r'\b(\d{3,5}\s*[Gg]\d)\b',
        text
    )
    if model_match:
        result["model"] = (model_match.group(1) or model_match.group(2) or model_match.group(3) or "").strip()

    return result
