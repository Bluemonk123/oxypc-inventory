"""CRM Quotes router — quote builder with line items and print view."""
import os
from datetime import datetime, date, timedelta
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import (
    FileResponse, HTMLResponse, RedirectResponse, StreamingResponse, Response,
)
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from templates_config import templates
from config import UPLOADS_DIR
from database import get_db
from auth.dependencies import get_current_user, verify_csrf
from services.po_pdf import build_po_pdf
from models.user import User, UserRole
from models.crm import (
    CRMContact, CRMQuote, CRMQuoteItem, CRMSalesOpportunity,
    MATERIAL_TYPES, GRADES,
)

router = APIRouter(prefix="/crm/quotes", tags=["crm-quotes"], dependencies=[Depends(verify_csrf)])


async def _next_quote_number(db: AsyncSession) -> str:
    result = await db.execute(select(func.count(CRMQuote.id)))
    n = (result.scalar() or 0) + 1
    year = app_now().year
    return f"QT-{year}-{n:04d}"


# ── LIST ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def list_quotes(
    request: Request,
    q: str = Query(default=""),
    status: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(CRMQuote)
    if status:
        query = query.where(CRMQuote.status == status)
    result = await db.execute(query.order_by(CRMQuote.created_at.desc()))
    quotes = result.scalars().all()

    contact_ids = list({qt.contact_id for qt in quotes if qt.contact_id})
    contacts_map = {}
    if contact_ids:
        cr = await db.execute(select(CRMContact).where(CRMContact.id.in_(contact_ids)))
        for c in cr.scalars().all():
            contacts_map[str(c.id)] = c

    # build opps_map keyed by quote.id (CRMSalesOpportunity.quote_id → opportunity)
    quote_ids = [qt.id for qt in quotes]
    opps_map = {}
    if quote_ids:
        or_result = await db.execute(
            select(CRMSalesOpportunity).where(CRMSalesOpportunity.quote_id.in_(quote_ids))
        )
        for opp in or_result.scalars().all():
            opps_map[str(opp.quote_id)] = opp

    if q:
        quotes = [qt for qt in quotes
                  if q.lower() in qt.quote_number.lower()
                  or (qt.contact_id and contacts_map.get(str(qt.contact_id)) and
                      q.lower() in contacts_map[str(qt.contact_id)].company_name.lower())]

    return templates.TemplateResponse("crm/quotes/list.html", {
        "request": request, "current_user": current_user,
        "quotes": quotes, "contacts_map": contacts_map, "opps_map": opps_map,
        "q": q, "status": status,
        "today": date.today(), "now": app_now(),
    })


# ── NEW ───────────────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_quote_form(
    request: Request,
    contact_id: str = Query(default=""),
    opp_id: str = Query(default=""),
    # "Quote for Direct Deal" — the operator is writing the lines themselves,
    # so suppress lot autofill even when the deal has a lot behind it.
    direct: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    contacts_r = await db.execute(
        select(CRMContact)
        .where(CRMContact.status == "active", CRMContact.contact_type.in_(["buyer", "both"]))
        .order_by(CRMContact.company_name)
    )
    contacts = contacts_r.scalars().all()

    # ── Prefill from the Buyer Deal's lot ────────────────────────────────────
    # Opened via "Create Quote" on a Buyer Deal. The opportunity has no lot FK —
    # it was created by Marking Won a partner bid, so the bid is what carries
    # the lot. Fill the buyer and one line per model in that lot.
    prefill_items, preselect, source_lot = [], contact_id, None
    is_direct = direct.strip() not in ("", "0", "false")
    # A direct-deal quote behaves like one written from scratch: one blank row,
    # no lot, no "nothing was auto-filled" notice.
    from_opp = bool(opp_id and opp_id.strip()) and not is_direct
    if opp_id and opp_id.strip():
        opp = (await db.execute(
            select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == opp_id)
        )).scalar_one_or_none()
        if opp:
            if not preselect and opp.contact_id:
                preselect = str(opp.contact_id)
            if not is_direct:
                from services.opportunity_lot import lot_for_opportunity
                source_lot = await lot_for_opportunity(db, opp.id)
                if source_lot:
                    prefill_items = await _lot_prefill_for_opp(db, opp)

    today = date.today()
    valid_until = today + timedelta(days=15)
    return templates.TemplateResponse("crm/quotes/form.html", {
        "request": request, "current_user": current_user,
        "quote": None, "contacts": contacts,
        "preselect": preselect, "opp_id": opp_id,
        # from_opp + source_lot let the form say WHERE its lines came from, and
        # render no rows at all when a deal has no lot — a lone blank row there
        # is what browser autofill latches onto and fills with unrelated data.
        "prefill_items": prefill_items, "from_opp": from_opp,
        "source_lot": source_lot,
        "material_types": MATERIAL_TYPES, "grades": GRADES,
        "today": today.isoformat(), "valid_until": valid_until.isoformat(),
    })


async def _lot_prefill_for_opp(db: AsyncSession, opp) -> list[dict]:
    """One quote line per distinct model+grade in the lot this deal came from.

    Returns [] when the opportunity was created by hand rather than from a won
    bid — there is no lot to read in that case, and the form falls back to a
    single blank row.
    """
    from services.opportunity_lot import (
        lot_for_opportunity, winning_bid_for_opportunity,
    )

    lot = await lot_for_opportunity(db, opp.id)
    if not lot:
        return []
    # Price at the bid that won this deal, not the lot's internal target.
    bid = await winning_bid_for_opportunity(db, opp.id)
    return await lot_quote_lines(
        db, lot, amount=float(bid.bid_amount) if bid and bid.bid_amount else None)


async def quote_summary_rows(db: AsyncSession, quotes: list) -> list[dict]:
    """Summarise quotes for the "Quotes Generated" table.

    Shared by the Account and Buyer Deal pages so both show the same columns
    computed the same way.
    """
    if not quotes:
        return []
    qids = [q.id for q in quotes]
    opp_by_quote = {
        str(o.quote_id): o for o in (await db.execute(
            select(CRMSalesOpportunity).where(CRMSalesOpportunity.quote_id.in_(qids))
        )).scalars().all()
    }
    items_by_quote: dict = {}
    for it in (await db.execute(
        select(CRMQuoteItem).where(CRMQuoteItem.quote_id.in_(qids))
    )).scalars().all():
        items_by_quote.setdefault(str(it.quote_id), []).append(it)

    rows = []
    for q in quotes:
        its = items_by_quote.get(str(q.id), [])
        opp = opp_by_quote.get(str(q.id))
        rows.append({
            "quote": q,
            "opp_number": opp.opp_number if opp else None,
            "opp_id": str(opp.id) if opp else None,
            "total_models": len(its),
            "total_qty": sum(int(i.quantity or 0) for i in its),
            "total_price": float(q.total_amount or 0),
            "has_pdf": quote_pdf_exists(q.id),
        })
    return rows


async def active_terms_by_type(db: AsyncSession) -> dict:
    """Active Terms & Conditions bucketed by type, for the Generate modal."""
    from models.terms import TermsCondition

    out = {"payment": [], "delivery": [], "disclaimer": []}
    for t in (await db.execute(
        select(TermsCondition).where(TermsCondition.is_active == True)  # noqa: E712
        .order_by(TermsCondition.display_order, TermsCondition.created_at)
    )).scalars().all():
        out.setdefault(t.term_type, []).append(t)
    return out


async def lot_quote_lines(db: AsyncSession, lot, amount=None) -> list[dict]:
    """The model summary of a lot, shaped as quote line items.

    Single source for both the Create Quote form's prefill and the one-click
    "Quote for Bid Won" path, so the two can never disagree about what a lot
    contains or what it should be priced at.

    `amount` is the total the lines must add up to. For a bid-won quote that is
    the winning bid — the price the dealer actually agreed to. It falls back to
    the lot's Target Selling Price only when no bid amount is known, since a
    target is an internal aspiration, not an agreed figure.
    """
    from models.device import Device
    from utils.master_data import master_options

    rows = (await db.execute(
        select(Device.model, Device.grade, Device.sub_category,
               func.count(Device.id).label("qty"))
        .where(Device.lot_id == lot.id, Device.is_active == True, Device.is_trashed == False)  # noqa: E712
        .group_by(Device.model, Device.grade, Device.sub_category)
        .order_by(func.count(Device.id).desc())
    )).all()
    # Devices with no model recorded would otherwise each become their own
    # nameless line — a junk row on a customer-facing quote. Drop them BEFORE
    # pricing, so the divisor below counts only what is actually being quoted.
    rows = [r for r in rows if (r.model or "").strip()]
    if not rows:
        return []

    # Per-unit price spreads the agreed total over the units being quoted —
    # deliberately NOT device_price, which is a buying cost and would quote the
    # customer at cost if the operator didn't notice. Dividing by the quoted
    # quantity (not the whole lot) is what makes the line totals add back up to
    # that total; counting unquotable stock would silently under-price.
    total = amount if amount else (
        float(lot.selling_price) if lot.selling_price else None)
    quoted_qty = sum(r.qty for r in rows)
    unit = round(float(total) / quoted_qty, 2) if total and quoted_qty else None

    # po_category is a Master Data list; only preselect when the device's
    # sub_category is actually one of its options, else leave it for the user.
    try:
        categories = set(master_options("po_category") or [])
    except Exception:
        categories = set()

    return [{
        "model": r.model,
        "grade": getattr(r.grade, "value", r.grade) or "",
        "po_category": r.sub_category if r.sub_category in categories else "",
        "quantity": r.qty,
        "unit_price": unit if unit else "",
    } for r in rows]


@router.post("/new")
async def create_quote(
    request: Request,
    contact_id:         str = Form(default=None),
    opp_id:             str = Form(default=None),
    quote_date:         str = Form(default=None),
    valid_until:        str = Form(default=None),
    payment_terms:      str = Form(default=None),
    special_conditions: str = Form(default=None),
    # line items sent as repeating fields
    device_type:        list = Form(default=[]),
    grade:              list = Form(default=[]),
    po_category:        list = Form(default=[]),
    quantity:           list = Form(default=[]),
    unit_price:         list = Form(default=[]),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    def _d(v):
        if v and v.strip():
            try: return datetime.strptime(v.strip(), "%Y-%m-%d").date()
            except: return None
        return None

    quote_number = await _next_quote_number(db)
    quote = CRMQuote(
        quote_number=quote_number,
        contact_id=contact_id or None,
        quote_date=_d(quote_date) or date.today(),
        valid_until=_d(valid_until),
        payment_terms=payment_terms or None,
        special_conditions=special_conditions or None,
        status="draft",
        created_by=current_user.username,
    )
    db.add(quote)
    await db.flush()  # get quote.id

    grand_total = 0.0
    for i, dt in enumerate(device_type):
        if not dt or not dt.strip():
            continue
        qty  = int(quantity[i]) if i < len(quantity) and quantity[i].strip() else 1
        uprc = float(unit_price[i]) if i < len(unit_price) and unit_price[i].strip() else 0
        tot  = round(qty * uprc, 2)
        grand_total += tot
        item = CRMQuoteItem(
            quote_id=quote.id,
            line_number=i + 1,
            # Column header is "Model Name" on the form — the column keeps its
            # original name so existing quotes and the print view still read.
            device_type=dt.strip(),
            grade=grade[i] if i < len(grade) else None,
            po_category=(po_category[i].strip() or None) if i < len(po_category) and po_category[i] else None,
            quantity=qty,
            unit_price=uprc,
            total_price=tot,
            sort_order=i,
        )
        db.add(item)

    quote.total_amount = round(grand_total, 2)

    # link to opportunity if provided
    if opp_id and opp_id.strip():
        opp_r = await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == opp_id))
        opp = opp_r.scalar_one_or_none()
        if opp:
            opp.quote_id = quote.id
            if opp.stage in ("lead", "contacted", "requirement", "availability"):
                opp.stage = "quoted"

    await db.commit()
    return RedirectResponse(url=f"/crm/quotes/{quote.id}?success=Quote+created", status_code=302)


# ── QUOTE FOR BID WON (one click, straight from the lot) ─────────────────────

@router.post("/from-lot")
async def create_quote_from_lot(
    request: Request,
    lot_id: str = Form(...),
    contact_id: str = Form(default=""),
    opp_id: str = Form(default=""),
    return_to: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Build a quote from a won lot without going through the form.

    Same line-item derivation the Create Quote page uses, so a bid-won quote and
    a hand-built one from the same lot come out identical.
    """
    from models.lot import Lot

    lot = (await db.execute(select(Lot).where(Lot.id == lot_id))).scalar_one_or_none()
    if not lot:
        return RedirectResponse(url=f"{return_to or '/crm/quotes'}?error=Lot+not+found",
                                status_code=302)

    # Price at the winning bid for this lot — the figure the dealer agreed to.
    from services.opportunity_lot import won_bid_for_lot
    bid = await won_bid_for_lot(db, lot.id, contact_id or None)
    lines = await lot_quote_lines(
        db, lot, amount=float(bid.bid_amount) if bid and bid.bid_amount else None)
    if not lines:
        return RedirectResponse(
            url=f"{return_to or '/crm/quotes'}?error=Lot+{lot.lot_number}+has+no+stock+to+quote",
            status_code=302)

    # An opportunity supplies the buyer when the caller did not name one.
    opp = None
    if opp_id.strip():
        opp = (await db.execute(select(CRMSalesOpportunity)
                                .where(CRMSalesOpportunity.id == opp_id))).scalar_one_or_none()
    if not contact_id.strip() and opp and opp.contact_id:
        contact_id = str(opp.contact_id)

    quote = CRMQuote(
        quote_number=await _next_quote_number(db),
        contact_id=contact_id or None,
        quote_date=date.today(),
        valid_until=date.today() + timedelta(days=15),
        status="draft",
        created_by=current_user.username,
    )
    db.add(quote)
    await db.flush()

    grand_total = 0.0
    for i, ln in enumerate(lines):
        qty  = int(ln["quantity"] or 1)
        uprc = float(ln["unit_price"] or 0)
        tot  = round(qty * uprc, 2)
        grand_total += tot
        db.add(CRMQuoteItem(
            quote_id=quote.id, line_number=i + 1,
            device_type=ln["model"], grade=ln["grade"] or None,
            po_category=ln["po_category"] or None,
            quantity=qty, unit_price=uprc, total_price=tot, sort_order=i,
        ))
    quote.total_amount = round(grand_total, 2)

    if opp:
        opp.quote_id = quote.id
        if opp.stage in ("lead", "contacted", "requirement", "availability"):
            opp.stage = "quoted"

    await db.commit()
    dest = return_to or f"/crm/quotes/{quote.id}"
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(
        url=f"{dest}{sep}success=Quote+{quote.quote_number}+created+from+Lot+{lot.lot_number}",
        status_code=302)


# ── DETAIL / PRINT PREVIEW ────────────────────────────────────────────────────

@router.get("/{quote_id}", response_class=HTMLResponse)
async def quote_detail(
    request: Request,
    quote_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMQuote).where(CRMQuote.id == quote_id))
    quote = result.scalar_one_or_none()
    if not quote:
        return RedirectResponse(url="/crm/quotes?error=Quote+not+found", status_code=302)

    items_r = await db.execute(
        select(CRMQuoteItem).where(CRMQuoteItem.quote_id == quote.id)
        .order_by(CRMQuoteItem.sort_order)
    )
    items = items_r.scalars().all()

    contact = None
    if quote.contact_id:
        cr = await db.execute(select(CRMContact).where(CRMContact.id == quote.contact_id))
        contact = cr.scalar_one_or_none()

    opp = None
    opp_r = await db.execute(
        select(CRMSalesOpportunity).where(CRMSalesOpportunity.quote_id == quote.id)
    )
    opp = opp_r.scalar_one_or_none()

    material_map = dict(MATERIAL_TYPES)
    today = date.today()
    is_expired = (quote.valid_until and quote.valid_until < today and
                  quote.status not in ("accepted", "rejected"))

    return templates.TemplateResponse("crm/quotes/detail.html", {
        "request": request, "current_user": current_user,
        "quote": quote, "items": items, "contact": contact, "opp": opp,
        "material_map": material_map, "is_expired": is_expired, "today": today,
    })


# ── PRINT VIEW (no sidebar — for printing) ───────────────────────────────────

@router.get("/{quote_id}/print", response_class=HTMLResponse)
async def quote_print(
    request: Request,
    quote_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMQuote).where(CRMQuote.id == quote_id))
    quote = result.scalar_one_or_none()
    if not quote:
        return RedirectResponse(url="/crm/quotes", status_code=302)

    items_r = await db.execute(
        select(CRMQuoteItem).where(CRMQuoteItem.quote_id == quote.id)
        .order_by(CRMQuoteItem.sort_order)
    )
    items = items_r.scalars().all()

    contact = None
    if quote.contact_id:
        cr = await db.execute(select(CRMContact).where(CRMContact.id == quote.contact_id))
        contact = cr.scalar_one_or_none()

    return templates.TemplateResponse("crm/quotes/print.html", {
        "request": request, "current_user": current_user,
        "quote": quote, "items": items, "contact": contact,
        "material_map": dict(MATERIAL_TYPES),
    })


# ── DOCUMENT GENERATION ───────────────────────────────────────────────────────

class _QuoteLine:
    """Adapter so quote items can feed the shared PO/Quote PDF layout, which
    reads .item_name / .description / .quantity / .total_price."""

    def __init__(self, item):
        self.item_name   = item.device_type or ""
        bits = [b for b in (item.grade, item.po_category, item.specs_note) if b]
        self.description = " · ".join(bits)
        self.quantity    = item.quantity
        self.unit_price  = item.unit_price
        self.total_price = item.total_price


async def _quote_pdf(db: AsyncSession, quote, *, sections, term_ids=None, company_id=None):
    """Build the quote PDF. term_ids maps 'payment'/'delivery'/'disclaimer' to a
    single chosen TermsCondition id; absent or blank means include every active
    policy of that type (which is what the plain Download link does)."""
    import io
    from models.terms import TermsCondition
    from routers.settings import get_company_settings
    from routers.terms_conditions import get_active_terms

    items = (await db.execute(
        select(CRMQuoteItem).where(CRMQuoteItem.quote_id == quote.id)
        .order_by(CRMQuoteItem.sort_order)
    )).scalars().all()

    contact = None
    if quote.contact_id:
        contact = (await db.execute(
            select(CRMContact).where(CRMContact.id == quote.contact_id)
        )).scalar_one_or_none()

    async def _terms(kind, enabled):
        if not enabled:
            return []
        chosen = (term_ids or {}).get(kind)
        if chosen:
            row = (await db.execute(
                select(TermsCondition).where(TermsCondition.id == chosen)
            )).scalar_one_or_none()
            return [row] if row else []
        return await get_active_terms(db, kind)

    company = await get_company_settings(db, company_id)
    pdf_bytes: bytes = build_po_pdf(
        po_number=quote.quote_number,
        po_date=quote.quote_date.strftime("%d %b %Y") if quote.quote_date else "",
        company=company, contact=contact,
        line_items=[_QuoteLine(i) for i in items],
        payment_terms=await _terms("payment",    sections.get("payment")),
        delivery_terms=await _terms("delivery",  sections.get("delivery")),
        disclaimers=await _terms("disclaimer",   sections.get("conditions")),
        sections=sections, total_amount=float(quote.total_amount or 0),
        doc_title="QUOTATION", account_label="Account Details (Buyer)",
    )

    # Keep the generated document so the Download column serves this exact file
    # later. A failure to persist must not cost the user their download, so the
    # response goes out regardless.
    try:
        path = quote_pdf_path(quote.id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(pdf_bytes)
    except OSError:
        pass

    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{quote.quote_number}.pdf"'},
    )


@router.post("/{quote_id}/generate")
async def generate_quote_doc(
    request: Request,
    quote_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate + download the quote document with the sections and the
    specific Terms & Conditions entries picked in the modal."""
    quote = (await db.execute(select(CRMQuote).where(CRMQuote.id == quote_id))).scalar_one_or_none()
    if not quote:
        return RedirectResponse(url="/crm/quotes?error=Quote+not+found", status_code=302)

    form = await request.form()
    term_ids = {
        "payment":    (form.get("payment_term_id")    or "").strip(),
        "delivery":   (form.get("delivery_term_id")   or "").strip(),
        "disclaimer": (form.get("disclaimer_term_id") or "").strip(),
    }
    sections = {
        "account":    bool(form.get("include_account")),
        "company":    bool(form.get("include_company")),
        "items":      bool(form.get("include_items")),
        # A terms block is included when the operator picked a policy for it.
        "payment":    bool(term_ids["payment"]),
        "delivery":   bool(term_ids["delivery"]),
        "conditions": bool(term_ids["disclaimer"]),
    }
    company_id = (form.get("company_id") or "").strip()
    return await _quote_pdf(db, quote, sections=sections, term_ids=term_ids, company_id=company_id)


def quote_pdf_path(quote_id) -> str:
    """Where a generated quote document is kept on disk.

    Keyed by the quote's id, not its number: quote numbers come from a count
    and are reused after a deletion, so a new quote would otherwise inherit the
    deleted one's document and offer it as its own.
    """
    return os.path.join(UPLOADS_DIR, "quotes", f"{quote_id}.pdf")


def quote_pdf_exists(quote_id) -> bool:
    """Whether Generate has actually produced a document for this quote.

    Download links off this: the column shows an attachment only when one was
    generated, rather than silently re-rendering a document nobody has seen.
    """
    try:
        return os.path.isfile(quote_pdf_path(quote_id))
    except OSError:
        return False


@router.get("/{quote_id}/download")
async def download_quote_doc(
    quote_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Serve the document produced by Generate — the stored attachment, not a
    fresh render, so what is downloaded is what was generated."""
    quote = (await db.execute(select(CRMQuote).where(CRMQuote.id == quote_id))).scalar_one_or_none()
    if not quote:
        return RedirectResponse(url="/crm/quotes?error=Quote+not+found", status_code=302)

    path = quote_pdf_path(quote.id)
    if not os.path.isfile(path):
        return RedirectResponse(
            url=f"/crm/quotes/{quote_id}?error=No+document+generated+yet+-+use+Generate+first",
            status_code=302)
    return FileResponse(path, media_type="application/pdf",
                        filename=f"{quote.quote_number}.pdf")


# ── STATUS UPDATE ─────────────────────────────────────────────────────────────

@router.post("/{quote_id}/status")
async def update_status(
    request: Request,
    quote_id: str,
    new_status: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(CRMQuote).where(CRMQuote.id == quote_id))
    quote = result.scalar_one_or_none()
    if not quote:
        return RedirectResponse(url="/crm/quotes?error=Not+found", status_code=302)
    quote.status = new_status
    if new_status == "sent":
        quote.sent_at = app_now()
    await db.commit()
    return RedirectResponse(url=f"/crm/quotes/{quote_id}?success=Status+updated+to+{new_status}", status_code=302)
