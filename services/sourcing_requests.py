"""Parts Sourcing / Device Sourcing requests (2026-11-15, Pankaj) — shared by
Procurement Board (actions) and Activity Board (Verify / Reject).

Parts Sourcing = PartSourcingRequest (production requests only once Part
Master has confirmed them, as before). Device Sourcing = ModelRequest.

Review: every new request is "pending" until it is verified or rejected on
Activity Board (review_status). Requests raised before this existed have no
review_status and count as verified. Procurement Board shows its Link Deal /
Close Deal buttons only on verified requests. Both pages split each table by
the pills Pending / Verified / Rejected; Process Status = Open / Closed plus
Verified / Unverified (Rejected).
"""
import uuid

from sqlalchemy import select, or_

from models.crm import CRMSourcingDeal
from models.model_requests import ModelRequest
from models.part_estimate import PartEstimate
from models.part_request import PartSourcingRequest

DEVICE_OPEN = ("open", "partially_fulfilled")


def review_of(r) -> str:
    return r.review_status or "verified"


def _uuid_strs(vals):
    out = []
    for v in vals:
        try:
            out.append(str(uuid.UUID(str(v))))
        except (ValueError, TypeError, AttributeError):
            continue
    return out


async def requests_context(db) -> dict:
    """Everything the shared _sourcing_requests.html partial needs."""
    from models.spare_parts import SparePart
    from routers.part_estimation import production_part_qty
    part_sourcing = (await db.execute(
        select(PartSourcingRequest)
        .where(or_(PartSourcingRequest.source != "production", PartSourcingRequest.confirmed == True))  # noqa: E712
        .order_by(PartSourcingRequest.created_at.desc()).limit(200))).scalars().all()
    device_sourcing = (await db.execute(
        select(ModelRequest).order_by(ModelRequest.created_at.desc()).limit(300))).scalars().all()

    estimates_by_batch = {}
    lot_ids = {s.lot_id for s in part_sourcing if s.source == "production" and s.lot_id}
    if lot_ids:
        by_lot_and_time = {}
        for e in (await db.execute(select(PartEstimate).where(
                PartEstimate.lot_id.in_(lot_ids), PartEstimate.file_name.isnot(None)))).scalars().all():
            by_lot_and_time.setdefault((e.lot_id, e.created_at), []).append(e)
        for s in part_sourcing:
            if s.source == "production" and s.lot_id:
                estimates_by_batch[str(s.id)] = by_lot_and_time.get((s.lot_id, s.created_at), [])

    linked = _uuid_strs([r.source_deal_id for r in list(part_sourcing) + list(device_sourcing) if r.source_deal_id])
    deal_map = {str(d.id): d for d in (await db.execute(
        select(CRMSourcingDeal).where(CRMSourcingDeal.id.in_(linked)))).scalars().all()} if linked else {}
    # Notes for Parts Sourcing (2026-11-15): the request has no notes field of its
    # own, so it shows what we know — the engineer's request (tag, capacity / type,
    # new / replace) or the estimate's notes, plus a rejection reason.
    from models.part_request import PartRequest
    pr_ids = [s.part_request_id for s in part_sourcing if s.part_request_id]
    prs = {p.id: p for p in (await db.execute(select(PartRequest).where(PartRequest.id.in_(pr_ids)))).scalars().all()} if pr_ids else {}
    est_ids = [s.estimate_id for s in part_sourcing if s.estimate_id]
    ests = {e.id: e for e in (await db.execute(select(PartEstimate).where(PartEstimate.id.in_(est_ids)))).scalars().all()} if est_ids else {}
    part_notes = {}
    for s in part_sourcing:
        bits = []
        pr = prs.get(s.part_request_id)
        if pr:
            spec = " ".join(x for x in (pr.part_capacity, pr.part_type) if x)
            bits += [x for x in (("Tag " + pr.barcode) if pr.barcode else "", spec,
                                 (pr.request_type or "").title() if pr.request_type and pr.request_type != "new" else "",
                                 pr.engineer_name or "") if x]
        est = ests.get(s.estimate_id)
        if est:
            bits.append((est.notes or "").strip() or ("Part Estimation" + (f" · Grade {est.grade}" if est.grade else "")))
        if (s.review_status or "") == "rejected" and s.review_note:
            bits.append("Rejected: " + s.review_note)
        part_notes[str(s.id)] = " · ".join(bits)
    pids = {s.part_id for s in part_sourcing if s.part_id}
    part_map = {str(p.id): p for p in (await db.execute(
        select(SparePart).where(SparePart.id.in_(pids)))).scalars().all()} if pids else {}
    return {
        "part_sourcing": part_sourcing, "device_sourcing": device_sourcing,
        "estimates_by_batch": estimates_by_batch, "deal_map": deal_map, "part_map": part_map, "part_notes": part_notes,
        "prod_qty": await production_part_qty(db, part_sourcing),
        "sourcing_deals_for_close": (await db.execute(
            select(CRMSourcingDeal).where(CRMSourcingDeal.stage.notin_(["won", "lost"]))
            .order_by(CRMSourcingDeal.created_at.desc()))).scalars().all(),
        "sourcing_deals_for_link": (await db.execute(
            select(CRMSourcingDeal).where(CRMSourcingDeal.stage != "lost")
            .order_by(CRMSourcingDeal.created_at.desc()))).scalars().all(),
    }
