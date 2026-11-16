"""Resolve the Lot behind a Buyer Deal.

A sales opportunity has no lot foreign key — it is created by Marking Won a
partner bid, and the bid is what carries the lot. Both the Buyer Deal detail
page (which shows the Lot Number) and the quote builder (which fills line items
from that lot's stock) need the same lookup, so it lives here rather than being
written twice and drifting apart.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def lot_for_opportunity(db: AsyncSession, opp_id):
    """The Lot this opportunity came from, or None if it was created by hand.

    Where several bids point at one opportunity, the highest is the winning one.
    """
    from models.partner import PartnerBid
    from models.lot import Lot

    bid = await winning_bid_for_opportunity(db, opp_id)
    if not bid:
        # An approved lot booking opens a deal too (2026-11-15)
        from models.partner import LotBookingRequest
        bk = (await db.execute(select(LotBookingRequest).where(
            LotBookingRequest.opportunity_id == opp_id))).scalars().first()
        if not bk:
            return None
        return (await db.execute(select(Lot).where(Lot.id == bk.lot_id))).scalar_one_or_none()
    if not bid.lot_id:
        return None
    return (await db.execute(select(Lot).where(Lot.id == bid.lot_id))).scalar_one_or_none()


async def winning_bid_for_opportunity(db: AsyncSession, opp_id):
    """The bid that produced this opportunity, or None.

    Carries the price the dealer actually agreed to — which is what a bid-won
    quote must be priced at, not the lot's internal Target Selling Price.
    """
    from models.partner import PartnerBid

    return (await db.execute(
        select(PartnerBid)
        .where(PartnerBid.opportunity_id == opp_id, PartnerBid.lot_id.isnot(None))
        .order_by(PartnerBid.bid_amount.desc())
    )).scalars().first()


async def won_bid_for_lot(db: AsyncSession, lot_id, contact_id=None):
    """The winning bid on a lot, optionally restricted to one Account's dealers.

    Only status 'won' counts: a losing bid names a price nobody agreed to.
    """
    from models.dealers import Dealer
    from models.partner import PartnerBid

    q = select(PartnerBid).where(
        PartnerBid.lot_id == lot_id, PartnerBid.status == "won")
    if contact_id:
        dealer_ids = [d for (d,) in (await db.execute(
            select(Dealer.id).where(Dealer.crm_contact_id == contact_id)
        )).all()]
        if dealer_ids:
            q = q.where(PartnerBid.dealer_id.in_(dealer_ids))
    return (await db.execute(q.order_by(PartnerBid.bid_amount.desc()))).scalars().first()


async def lots_won_by_contact(db: AsyncSession, contact_id) -> list[dict]:
    """Lots this Account won at auction, with the numbers needed to quote them.

    Portal logins hang off Dealer rows that link back to the Account, and one
    Account can hold several, so every dealer under it is considered. Returns
    quantity in the lot, its Base Price, and the winning bid amount.
    """
    from sqlalchemy import func
    from models.dealers import Dealer
    from models.device import Device
    from models.lot import Lot
    from models.partner import PartnerBid

    dealer_ids = [d for (d,) in (await db.execute(
        select(Dealer.id).where(Dealer.crm_contact_id == contact_id)
    )).all()]
    if not dealer_ids:
        return []

    bids = (await db.execute(
        select(PartnerBid).where(
            PartnerBid.dealer_id.in_(dealer_ids),
            PartnerBid.status == "won",
            PartnerBid.lot_id.isnot(None),
        ).order_by(PartnerBid.created_at.desc())
    )).scalars().all()
    if not bids:
        return []

    lot_ids = list({b.lot_id for b in bids})
    lots = {l.id: l for l in (await db.execute(
        select(Lot).where(Lot.id.in_(lot_ids)))).scalars().all()}
    qty_by_lot = {lid: n for lid, n in (await db.execute(
        select(Device.lot_id, func.count(Device.id))
        .where(Device.lot_id.in_(lot_ids), Device.is_active == True)  # noqa: E712
        .group_by(Device.lot_id)
    )).all()}

    out, seen = [], set()
    for b in bids:
        lot = lots.get(b.lot_id)
        if not lot or lot.id in seen:
            continue          # one row per lot, keyed to its most recent win
        seen.add(lot.id)
        out.append({
            "lot_id": str(lot.id),
            "lot_number": lot.lot_number,
            "total_qty": qty_by_lot.get(lot.id, 0),
            "base_price": float(lot.buying_price or 0),
            "won_bid_price": float(b.bid_amount or 0),
        })
    return out
