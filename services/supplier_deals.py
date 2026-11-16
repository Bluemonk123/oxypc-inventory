"""Supplier Deal card (2026-11-10, Pankaj; on Inventory Manager and Parts
Manager since 2026-11-12) — templates/_supplier_deal_card.html.

Supplier (sourcing) deals at any open stage are "Pending"; closed Won are
"Received" (no stage gating since 2026-11-15). Date Added = when the deal reached Stock Received
(latest audit row that set stage "received"), else its last update.
"""
from datetime import datetime

from sqlalchemy import select, or_

from models.crm import CRMSourcingDeal
from models.engines import AuditLog


async def supplier_deal_context(db, current_user, page="inventory") -> dict:
    """Context for the Supplier Deal card. Only for users with CRM Sourcing
    enabled; everyone else gets sd_enabled=False and empty lists.

    Which deals each page sees (2026-11-15, Pankaj) — a deal is "linked" when a
    Part Sourcing Request points at it (PartSourcingRequest.source_deal_id, set
    by Close Deal or Link Deal on Procurement Board):
      inventory  — deals NOT linked to any sourcing request
      production — deals linked to requests raised by Production (source "production",
                   from Part Estimation)
      parts      — deals linked to requests the Parts Manager owns: its own procure
                   requests, and production requests it has confirmed
    All pages (2026-11-15, no stage gating): Pending = every open stage (Lead →
    Stock Received), Received = closed Won; Lost deals are left out. Confirm
    received / Reject only apply at Stock Received."""
    from models.role_permissions import has_perm
    rv = getattr(current_user.role, "value", current_user.role)
    from models.crm import SOURCING_STAGES
    out = {"sd_enabled": False, "sd_pending": [], "sd_received": [], "sd_files": {}, "sd_added": {},
           "sd_stage_labels": dict(SOURCING_STAGES)}
    if not has_perm(rv, "crm_sourcing", "enable"):
        return out
    out["sd_enabled"] = True
    from models.part_request import PartSourcingRequest
    links = (await db.execute(select(PartSourcingRequest.source_deal_id, PartSourcingRequest.source,
                                     PartSourcingRequest.confirmed)
                              .where(PartSourcingRequest.source_deal_id.isnot(None)))).all()
    linked_all = {str(d) for d, _s, _c in links}
    if page == "production":
        keep = {str(d) for d, src, _c in links if src == "production"}
    elif page == "parts":
        keep = {str(d) for d, src, conf in links if src != "production" or conf}
    else:
        keep = None
    deals = (await db.execute(
        select(CRMSourcingDeal).where(CRMSourcingDeal.stage != "lost")   # every stage (2026-11-15, Pankaj)
        .order_by(CRMSourcingDeal.updated_at.desc().nullslast())
    )).scalars().all()
    if keep is None:
        deals = [d for d in deals if str(d.id) not in linked_all]
    else:
        deals = [d for d in deals if str(d.id) in keep]
    if not deals:
        return out
    from services.doc_downloads import sourcing_downloads
    out["sd_files"] = await sourcing_downloads(db, deals)
    ids = [str(d.id) for d in deals]
    added = {}
    for rid, ts in (await db.execute(
        select(AuditLog.record_id, AuditLog.timestamp).where(
            AuditLog.record_id.in_(ids),
            or_(AuditLog.new_value.like('%"stage": "received"%'),
                AuditLog.new_value.like('%"stage":"received"%')))
    )).all():
        if ts and (rid not in added or ts > added[rid]):
            added[rid] = ts
    out["sd_added"] = {str(d.id): added.get(str(d.id)) or d.updated_at or d.created_at for d in deals}
    out["sd_pending"] = sorted((d for d in deals if d.stage != "won"),
                               key=lambda d: out["sd_added"][str(d.id)] or datetime.min, reverse=True)
    out["sd_received"] = [d for d in deals if d.stage == "won"]
    return out
