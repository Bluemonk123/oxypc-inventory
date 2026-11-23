"""External Partner → Customer Deal (2026-11-15, Pankaj).

- Book request approved or Bid Marked Won (2026-11-16): a Customer Deal at
  stage Quote with a Quotation whose lines are the lot's tags, one line per
  configuration (services/quote_lines.lot_lines). Booking: lot Base Price ÷
  lot qty per unit, for the qty booked. Bid: the bid amount spread equally per
  unit. The Quotation PDF is attached to the bid / shown on the booking, and
  the partner downloads it from My Bids / My Bookings.
- Sales Order approved on the deal (Accounts → Sales Order → Verify): its PDF
  is attached to the bid / booking and the partner is told (sales_order_approved).
- Partner payment verified (Accounts → Partner Payments card, or the Partner
  Payments page): a Customer Receipt is posted against the deal and its Sales
  Order, and the deal moves on to Stock Handover.
Each step tells the partner (bell + toast, PartnerNotification) and the
partner's sales owner / Account Manager (staff bell, Notification).
"""
import os
import uuid as uuid_mod
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select, func

from utils.timezone import app_now


# ── Notifications ───────────────────────────────────────────────────────────
async def notify_partner(db, dealer_id, title, message="", kind="info", link=None):
    from models.partner import PartnerNotification
    if dealer_id:
        db.add(PartnerNotification(dealer_id=dealer_id, title=title[:200], message=message or None,
                                   kind=kind, link=link))


async def notify_staff(db, usernames, title, message, ntype="info"):
    """Staff bell (models/notification.py) for each username given."""
    from models.notification import Notification
    from models.user import User
    names = sorted({u for u in usernames if u})
    if not names:
        return
    for u in (await db.execute(select(User).where(User.username.in_(names), User.status == True))).scalars().all():  # noqa: E712
        db.add(Notification(user_id=u.id, title=title[:255], message=message, notification_type=ntype))


async def account_managers(db, dealer):
    """Who looks after this partner: its sales owner plus the Account's mapped
    Manager / Team Leader (Map Team), when there is one."""
    from models.crm_team import CRMContactTeamMember
    from models.user import User
    names = {getattr(dealer, "sales_owner_username", None)}
    if getattr(dealer, "crm_contact_id", None):
        names |= {u for (u,) in (await db.execute(
            select(User.username).join(CRMContactTeamMember, CRMContactTeamMember.user_id == User.id)
            .where(CRMContactTeamMember.contact_id == dealer.crm_contact_id,
                   CRMContactTeamMember.role_bucket.in_(("executive", "am_dm_gm", "manager"))))).all()}
    return names


# ── Lot helpers ─────────────────────────────────────────────────────────────
async def lot_summary(db, lot):
    """'Laptop × 40, Desktop × 2' and the number of live tags in the lot."""
    from models.device import Device
    rows = (await db.execute(
        select(Device.device_type, Device.sub_category, func.count(Device.id))
        .where(Device.lot_id == lot.id, Device.is_active == True, Device.is_trashed.isnot(True))  # noqa: E712
        .group_by(Device.device_type, Device.sub_category))).all()
    counts = {}
    for dt, sc, n in rows:
        k = (dt or sc or "Item").strip().title()
        counts[k] = counts.get(k, 0) + n
    text = ", ".join("%s × %d" % (k, v) for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    return text, sum(counts.values())


def _money(v):
    return Decimal(str(v or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _new_deal(db, *, dealer, title, stage, qty, value, notes, user):
    from models.crm import CRMSalesOpportunity
    from routers.crm_sales import _next_opp_number
    opp = CRMSalesOpportunity(
        opp_number=await _next_opp_number(db),
        contact_id=getattr(dealer, "crm_contact_id", None),
        title=title[:300], buyer_type="dealer", required_qty=qty, stage=stage,
        estimated_value=value, assigned_to=getattr(dealer, "sales_owner_username", None),
        notes=notes, created_by=user.username,
    )
    db.add(opp)
    await db.flush()
    return opp


async def _quote_from_lines(db, *, opp, lines, user, status="added"):
    """A CRM Quotation for the deal from lot lines; its PDF is saved."""
    from models.crm import CRMQuote, CRMQuoteItem
    from routers.crm_quotes import _next_quote_number, save_quote_pdf
    from services.quote_lines import title_of, spec_note
    total = sum((Decimal(str(l["total_price"])) for l in lines), Decimal("0"))
    q = CRMQuote(quote_number=await _next_quote_number(db), contact_id=opp.contact_id,
                 opportunity_id=opp.id, quote_date=app_now().date(), total_amount=total,
                 status=status, created_by=user.username)
    for i, l in enumerate(lines):
        q.items.append(CRMQuoteItem(
            line_number=i + 1, device_type=title_of(l), grade=(l.get("grade") or None),
            po_category=l.get("device_category"), device_category=l.get("device_category"), make=l.get("make"),
            cpu=l.get("cpu"), cpu_gen=l.get("cpu_gen"), ram=l.get("ram"), storage=l.get("storage"),
            specs_note=spec_note(l) or None, quantity=l["quantity"], unit_price=l["unit_price"],
            total_price=l["total_price"], sort_order=i))
    db.add(q)
    await db.flush()
    opp.quote_id = q.id
    opp.estimated_value = total
    await save_quote_pdf(db, q)
    return q


async def _partner_label(db, lot):
    """What the partner sees for a lot: its Sub-lot Number when set (2026-11-23)."""
    from routers.partner_portal import partner_lot_labels
    return (await partner_lot_labels(db, [lot.id])).get(lot.id, lot.lot_number)


async def _lines_for(db, lot, listing, *, qty=None, total=None, unit=None):
    from services.quote_lines import lot_lines
    if lot is not None:
        lines = await lot_lines(db, lot, qty=qty, total=total, unit=unit)
        if lines:
            return lines
    # a listing bid, or a lot with no live tags: one line for the whole thing
    q = int(qty or (listing.qty_available if listing else (lot.qty if lot else 1)) or 1)
    t = _money(total) if total is not None else _money(Decimal(str(unit or 0)) * q)
    return [{"device_category": None, "make": listing.brand if listing else None,
             # partners see the Sub-lot Number, not the real lot number (2026-11-23)
             "model_name": (listing.title if listing else f"Lot {await _partner_label(db, lot)}")[:100],
             "cpu": None, "cpu_gen": None, "ram": None, "storage": None,
             "grade": (listing.grade_summary or "")[:10] or None if listing else None,
             "quantity": q, "unit_price": _money(t / q), "total_price": t}]


def _attach_quote_to_bid(db, bid, quote, user):
    """Copy the saved Quotation PDF onto the bid (doc_type 'quote')."""
    import shutil
    from models.partner import PartnerBidDocument
    from routers.crm_quotes import quote_pdf_path
    from routers.partner_admin import BIDS_DIR
    src = quote_pdf_path(quote.id)
    if not os.path.isfile(src):
        return None
    os.makedirs(BIDS_DIR, exist_ok=True)
    fname = f"{quote.quote_number}-{uuid_mod.uuid4().hex[:8]}.pdf"
    shutil.copyfile(src, os.path.join(BIDS_DIR, fname))
    doc = PartnerBidDocument(bid_id=bid.id, doc_type="quote", filename=fname,
                             original_name=f"{quote.quote_number}.pdf", uploaded_by=user.username)
    db.add(doc)
    return doc


# ── Book request approved ───────────────────────────────────────────────────
async def deal_from_booking(db, booking, lot, dealer, user, request=None):
    from services.audit_engine import audit
    _summary, live = await lot_summary(db, lot)
    lot_qty = int(lot.qty or 0) or live or 1
    qty = max(1, int(booking.qty_requested or 1))
    unit = _money(Decimal(str(lot.buying_price or 0)) / lot_qty)
    lines = await _lines_for(db, lot, None, qty=qty, unit=unit)
    total = sum((Decimal(str(l["total_price"])) for l in lines), Decimal("0"))
    opp = await _new_deal(
        db, dealer=dealer, stage="quote", qty=qty, value=total, user=user,
        title=f"Lot {lot.lot_number} — booked by {dealer.business_name}",
        notes=f"Created from External Partner booking on lot {lot.lot_number} "
              f"({qty} of {lot_qty} units), approved by {user.username}.")
    quote = await _quote_from_lines(db, opp=opp, lines=lines, user=user)
    booking.status, booking.opportunity_id = "approved", opp.id
    booking.approved_by, booking.approved_at = user.username, app_now()
    await audit(db, action="PARTNER_BOOKING_APPROVED", user=user, table_name="lot_booking_requests",
                record_id=str(booking.id), new_value={"lot": lot.lot_number, "qty": qty, "deal": opp.opp_number,
                                                      "quote": quote.quote_number, "total": str(total)},
                request=request)
    await notify_partner(db, dealer.id, f"Booking approved — Lot {await _partner_label(db, lot)}",
                         f"Your booking of {qty} unit(s) is approved. Quotation {quote.quote_number} "
                         f"for ₹{total:,.2f} is ready to download in My Bookings.",
                         "success", "/partner/bookings")
    await notify_staff(db, await account_managers(db, dealer),
                       f"Booking approved — {dealer.business_name}",
                       f"Lot {lot.lot_number}, {qty} unit(s). Customer Deal {opp.opp_number} opened at Quote "
                       f"with Quotation {quote.quote_number} (₹{total:,.2f}). /crm/sales/{opp.id}", "success")
    return opp, quote


# ── Bid marked won ──────────────────────────────────────────────────────────
async def deal_from_won_bid(db, bid, dealer, listing, lot, user, request=None):
    """Customer Deal at Quote + Quotation from the lot's tags (bid amount spread
    equally per unit); the Quotation PDF is attached to the bid."""
    what = f"Lot {lot.lot_number}" if lot is not None else (listing.title if listing else "Listing")
    lines = await _lines_for(db, lot, listing, total=bid.bid_amount)
    total = _money(bid.bid_amount)
    qty = sum(int(l["quantity"]) for l in lines)
    opp = await _new_deal(
        db, dealer=dealer, stage="quote", qty=qty, value=total, user=user,
        title=f"{what} — won by {dealer.business_name} ({bid.bid_number})",
        notes=f"Created from External Partner bid {bid.bid_number} ({bid.bid_type} price), "
              f"marked won by {user.username}.")
    quote = await _quote_from_lines(db, opp=opp, lines=lines, user=user)
    bid.opportunity_id = opp.id
    _attach_quote_to_bid(db, bid, quote, user)
    await notify_partner(db, dealer.id, f"You won {what}",
                         f"Bid {bid.bid_number} for ₹{total:,.2f} is accepted. Quotation {quote.quote_number} "
                         "is ready to download in My Bids; the Sales Order follows once it is approved.",
                         "success", "/partner/bids")
    await notify_staff(db, await account_managers(db, dealer),
                       f"Bid won — {dealer.business_name}",
                       f"{what}: {bid.bid_number} for ₹{total:,.2f}. Customer Deal {opp.opp_number} opened at Quote "
                       f"with Quotation {quote.quote_number}. /crm/sales/{opp.id}", "success")
    return opp, quote, None


async def so_pdf_bytes(db, so):
    """The Sales Order as a PDF (lines from the SO itself — a snapshot of the
    quote, loaded here: a lazy relationship read fails in an async session)."""
    from services.po_pdf import build_po_pdf
    from routers.settings import get_company_settings
    from models.crm import CRMContact, CRMSalesOrderItem
    contact = (await db.execute(select(CRMContact).where(CRMContact.id == so.contact_id))).scalar_one_or_none() \
        if so.contact_id else None
    items = (await db.execute(select(CRMSalesOrderItem).where(CRMSalesOrderItem.so_id == so.id)
                              .order_by(CRMSalesOrderItem.line_number))).scalars().all()

    class _Item:
        def __init__(self, it):
            self.item_name, self.description = it.device_type, it.specs_note or ""
            self.quantity, self.unit_price, self.total_price = it.quantity, it.unit_price, it.total_price
    return build_po_pdf(
        po_number=so.so_number, po_date=(so.so_date or app_now()).strftime("%d-%b-%Y"),
        company=await get_company_settings(db), contact=contact,
        line_items=[_Item(i) for i in items], payment_terms=[], delivery_terms=[], disclaimers=[],
        sections={"account": True, "company": True, "items": True, "payment": False,
                  "delivery": False, "conditions": False},
        total_amount=float(so.total_amount or 0), doc_title="SALES ORDER",
        account_label="Account Details (Buyer)")


async def attach_so_pdf(db, bid, so, quote, opp, user):
    """Sales Order PDF on the bid (doc_type 'so') for My Bids."""
    from models.partner import PartnerBidDocument
    from routers.partner_admin import BIDS_DIR
    try:
        pdf = await so_pdf_bytes(db, so)
    except Exception:
        return None
    os.makedirs(BIDS_DIR, exist_ok=True)
    fname = f"{so.so_number}-{uuid_mod.uuid4().hex[:8]}.pdf"
    with open(os.path.join(BIDS_DIR, fname), "wb") as fh:
        fh.write(pdf)
    doc = PartnerBidDocument(bid_id=bid.id, doc_type="so", filename=fname,
                             original_name=f"{so.so_number}.pdf", uploaded_by=user.username)
    db.add(doc)
    return doc


async def deal_docs(db, opp_id):
    """(quote, verified sales order) of a partner deal, either may be None."""
    from models.crm import CRMSalesOpportunity, CRMQuote, CRMSalesOrder
    if not opp_id:
        return None, None
    opp = (await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == opp_id))).scalar_one_or_none()
    quote = (await db.execute(select(CRMQuote).where(CRMQuote.id == opp.quote_id))).scalar_one_or_none() \
        if opp and opp.quote_id else None
    so = (await db.execute(select(CRMSalesOrder).where(
        CRMSalesOrder.deal_id == opp_id, CRMSalesOrder.status != "cancelled", CRMSalesOrder.verified == True)  # noqa: E712
        .order_by(CRMSalesOrder.created_at.desc()))).scalars().first() if opp else None
    return quote, so


async def notify_bid_lost(db, bids, won_label):
    """Tell each losing partner (one note per dealer)."""
    seen = set()
    for b in bids:
        if b.dealer_id in seen:
            continue
        seen.add(b.dealer_id)
        await notify_partner(db, b.dealer_id, f"Bid not selected — {won_label}",
                             f"Bid {b.bid_number} for ₹{_money(b.bid_amount):,.2f} was not selected; "
                             "the lot went to another offer.", "warning", "/partner/bids")


# ── Payment verified ────────────────────────────────────────────────────────
async def _payment_step(db, opp, status, by, note):
    """The deal's Payment → Level 1 item (crm_sales_steps), created if missing."""
    from models.crm import CRMSalesStep
    st = (await db.execute(select(CRMSalesStep).where(CRMSalesStep.deal_id == opp.id,
                                                      CRMSalesStep.step_key == "payment_l1"))).scalar_one_or_none()
    if st is None:
        st = CRMSalesStep(deal_id=opp.id, stage="payment", step_key="payment_l1", created_by=by)
        db.add(st)
    st.status, st.note = status, note
    if status != "pending":
        st.acted_by, st.acted_at = by, app_now()
    else:
        st.acted_by = st.acted_at = None
    return st


def _deal_note(opp, by, text):
    from models.crm import CRMActivity
    return CRMActivity(contact_id=opp.contact_id, deal_id=opp.id, deal_type="sales",
                       activity_type="note", summary=text, performed_by=by, outcome="done")


async def payment_receipt_shared(db, bid, pay, dealer):
    """Partner shared a payment receipt (2026-11-16, Pankaj): the Customer Deal
    moves to its Payment stage with Level 1 pending ("Payment Pending") until
    Accounts approves it on the Accounts board."""
    from models.crm import CRMSalesOpportunity, SALES_STAGES
    opp = (await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == bid.opportunity_id))
           ).scalar_one_or_none() if bid.opportunity_id else None
    if opp is None:
        return None
    keys = [k for k, _ in SALES_STAGES]
    if opp.stage in keys and keys.index(opp.stage) < keys.index("payment"):
        opp.stage = "payment"
    by = f"partner:{dealer.business_name}"[:50]
    ref = f"UTR {pay.payment_utr}" if pay.payment_utr else "no UTR"
    await _payment_step(db, opp, "pending", by, f"Payment receipt shared by partner ({ref}) — waiting for Accounts")
    db.add(_deal_note(opp, by[:50], f"Payment Pending: partner shared the payment receipt for {bid.bid_number} ({ref})"))
    await notify_staff(db, await account_managers(db, dealer), f"Payment receipt shared — {dealer.business_name}",
                       f"{bid.bid_number}: deal {opp.opp_number} is at Payment, waiting for Accounts to approve.")
    return opp


async def post_partner_payment(db, bid, pay, user, notes="", request=None):
    """Verify a partner payment: receipt on the deal + SO, deal → Stock Handover."""
    from models.crm import CRMSalesOpportunity, CRMSalesOrder, CustomerReceipt
    from models.dealers import Dealer
    from services.audit_engine import audit
    pay.verified, pay.verified_by, pay.verified_at = True, user.username, app_now()
    pay.verify_notes = (notes or "").strip() or None
    dealer = (await db.execute(select(Dealer).where(Dealer.id == bid.dealer_id))).scalar_one_or_none()
    opp = (await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == bid.opportunity_id))
           ).scalar_one_or_none() if bid.opportunity_id else None
    receipt = None
    if opp is not None:
        so = (await db.execute(select(CRMSalesOrder).where(CRMSalesOrder.deal_id == opp.id,
                                                           CRMSalesOrder.status != "cancelled")
                               .order_by(CRMSalesOrder.created_at.desc()))).scalars().first()
        receipt = CustomerReceipt(
            contact_id=opp.contact_id, dealer_id=bid.dealer_id, opportunity_id=opp.id,
            so_id=so.id if so else None,
            receipt_date=(pay.payment_date.date() if pay.payment_date else app_now().date()),
            amount=pay.payment_amount if pay.payment_amount is not None else bid.bid_amount,
            payment_mode={"bank_transfer": "neft", "upi": "upi"}.get(pay.payment_mode or "", pay.payment_mode),
            reference_no=pay.payment_utr,
            notes=f"External Partner payment for bid {bid.bid_number}" + (f" — {pay.verify_notes}" if pay.verify_notes else ""),
            created_by=user.username, verified=True, verified_by=user.username, verified_at=app_now())
        db.add(receipt)
        # Payment → Level 1 approved by Accounts = "Payment Done" (2026-11-16)
        await _payment_step(db, opp, "approved", user.username,
                            "Payment approved by Accounts" + (f" (UTR {pay.payment_utr})" if pay.payment_utr else ""))
        db.add(_deal_note(opp, user.username, f"Payment Done: Accounts approved the partner payment for {bid.bid_number}"))
        if opp.stage == "payment":   # only from Payment; earlier stages run their own approvals
            opp.stage = "received"
        await db.flush()
    await audit(db, user=user, action="PARTNER_PAYMENT_VERIFIED", table_name="partner_bid_payments",
                record_id=str(pay.id),
                new_value={"bid": bid.bid_number, "utr": pay.payment_utr,
                           "receipt": str(receipt.id) if receipt else None,
                           "deal": opp.opp_number if opp else None,
                           "deal_stage": opp.stage if opp else None}, request=request)
    if dealer:
        await notify_partner(db, dealer.id, f"Payment verified — {bid.bid_number}",
                             f"We have received ₹{_money(receipt.amount if receipt else bid.bid_amount):,.2f}"
                             + (f" (UTR {pay.payment_utr})" if pay.payment_utr else "") + ". Stock handover is next.",
                             "success", "/partner/bids")
        await notify_staff(db, await account_managers(db, dealer), f"Partner payment verified — {dealer.business_name}",
                           f"{bid.bid_number}: payment verified by {user.username}"
                           + (f"; Customer Deal {opp.opp_number} moved to Stock Handover. /crm/sales/{opp.id}" if opp else "."),
                           "success")
    return receipt, opp


# ── Booking / Bidding Details card on the Customer Deal ─────────────────────
async def deal_partner_origin(db, opp):
    """What the Customer Deal page's Booking/Bidding Details card shows — only
    for a deal opened by an approved lot booking or a won bid; else None."""
    from models.partner import PartnerBid, PartnerBidDocument, LotBookingRequest, PartnerListing
    from models.partner_payment import PartnerBidPayment
    from models.dealers import Dealer
    from models.lot import Lot
    bid = (await db.execute(select(PartnerBid).where(PartnerBid.opportunity_id == opp.id,
                                                     PartnerBid.status == "won"))).scalars().first()
    booking = None
    if bid is None:
        booking = (await db.execute(select(LotBookingRequest).where(
            LotBookingRequest.opportunity_id == opp.id, LotBookingRequest.status == "approved"))).scalars().first()
        if booking is None:
            return None
    src = bid or booking
    dealer = (await db.execute(select(Dealer).where(Dealer.id == src.dealer_id))).scalar_one_or_none()
    lot = (await db.execute(select(Lot).where(Lot.id == src.lot_id))).scalar_one_or_none() if src.lot_id else None
    listing = None
    if bid is not None and bid.listing_id:
        listing = (await db.execute(select(PartnerListing).where(PartnerListing.id == bid.listing_id))).scalar_one_or_none()
    summary = (await lot_summary(db, lot))[0] if lot else (listing.model_summary if listing else "")
    out = {
        "kind": "bid" if bid else "booking",
        "label": "Bid Won" if bid else "Booking Approved",
        "number": bid.bid_number if bid else "Booking " + str(booking.id)[:8].upper(),
        "partner": dealer.business_name if dealer else "—",
        "partner_code": dealer.dealer_code if dealer else "",
        "login": " / ".join(x for x in ((dealer.portal_phone if dealer else None), (dealer.email if dealer else None)) if x),
        "lot_id": str(lot.id) if lot else None,
        "lot_number": lot.lot_number if lot else None,
        "listing": listing.title if listing else None,
        "items": summary,
        "lot_qty": lot.qty if lot else (listing.qty_total if listing else None),
        "base_price": (lot.buying_price if lot else (listing.dealer_price if listing else None)),
        "placed_at": src.created_at,
        "decided_by": bid.won_by if bid else booking.approved_by,
        "decided_at": bid.won_at if bid else booking.approved_at,
    }
    if bid is not None:
        out.update({
            "bid_type": "Custom price" if bid.bid_type == "custom" else "Bid (+10% ladder)",
            "amount": bid.bid_amount, "previous": bid.previous_amount, "notes": bid.notes,
            "runner_up": (await db.execute(select(func.max(PartnerBid.bid_amount)).where(
                (PartnerBid.lot_id == bid.lot_id) if bid.lot_id else (PartnerBid.listing_id == bid.listing_id),
                PartnerBid.status == "lost"))).scalar(),
            "documents": (await db.execute(select(PartnerBidDocument).where(PartnerBidDocument.bid_id == bid.id)
                                           .order_by(PartnerBidDocument.created_at))).scalars().all(),
            "payment": (await db.execute(select(PartnerBidPayment).where(PartnerBidPayment.bid_id == bid.id)
                                         .order_by(PartnerBidPayment.submitted_at.desc()))).scalars().first(),
            "bid_id": str(bid.id),
        })
    else:
        out.update({"qty": booking.qty_requested})
    out["quote"], out["so"] = await deal_docs(db, opp.id)
    return out


# ── Sales Order approved on a partner deal ──────────────────────────────────
async def sales_order_approved(db, so, user):
    """Accounts verified a Sales Order: when its deal came from a partner bid or
    booking, attach the SO PDF (bid) and tell the partner and Account Manager."""
    from models.partner import PartnerBid, LotBookingRequest
    from models.crm import CRMSalesOpportunity, CRMQuote
    from models.dealers import Dealer
    if not so.deal_id:
        return
    bid = (await db.execute(select(PartnerBid).where(PartnerBid.opportunity_id == so.deal_id,
                                                     PartnerBid.status == "won"))).scalars().first()
    booking = None if bid else (await db.execute(select(LotBookingRequest).where(
        LotBookingRequest.opportunity_id == so.deal_id))).scalars().first()
    src = bid or booking
    if src is None:
        return
    dealer = (await db.execute(select(Dealer).where(Dealer.id == src.dealer_id))).scalar_one_or_none()
    opp = (await db.execute(select(CRMSalesOpportunity).where(CRMSalesOpportunity.id == so.deal_id))).scalar_one_or_none()
    if bid is not None:
        quote = (await db.execute(select(CRMQuote).where(CRMQuote.id == so.quote_id))).scalar_one_or_none()
        if quote is not None and opp is not None:
            await attach_so_pdf(db, bid, so, quote, opp, user)
    where = "My Bids" if bid is not None else "My Bookings"
    await notify_partner(db, src.dealer_id, f"Sales Order {so.so_number} ready",
                         f"Download it from {where}" + ("; pay and click Mark Paid with the UTR." if bid is not None else "."),
                         "success", "/partner/bids" if bid is not None else "/partner/bookings")
    if dealer:
        await notify_staff(db, await account_managers(db, dealer), f"Sales Order approved — {dealer.business_name}",
                           f"{so.so_number} approved and shared with the partner."
                           + (f" /crm/sales/{opp.id}" if opp else ""), "success")
