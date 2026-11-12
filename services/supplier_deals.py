"""Supplier Deal card (2026-11-10, Pankaj; on Inventory Manager and Parts
Manager since 2026-11-12) — templates/_supplier_deal_card.html.

Supplier (sourcing) deals at stage Stock Received are "Pending"; closed Won
after it are "Received". Date Added = when the deal reached Stock Received
(latest audit row that set stage "received"), else its last update.
"""
from datetime import datetime

from sqlalchemy import select, or_

from models.crm import CRMSourcingDeal
from models.engines import AuditLog


async def supplier_deal_context(db, current_user) -> dict:
    """Context for the Supplier Deal card. Only for users with CRM Sourcing
    enabled; everyone else gets sd_enabled=False and empty lists."""
    from models.role_permissions import has_perm
    rv = getattr(current_user.role, "value", current_user.role)
    out = {"sd_enabled": False, "sd_pending": [], "sd_received": [], "sd_files": {}, "sd_added": {}}
    if not has_perm(rv, "crm_sourcing", "enable"):
        return out
    out["sd_enabled"] = True
    deals = (await db.execute(
        select(CRMSourcingDeal).where(CRMSourcingDeal.stage.in_(("received", "won")))
        .order_by(CRMSourcingDeal.updated_at.desc().nullslast())
    )).scalars().all()
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
    out["sd_pending"] = sorted((d for d in deals if d.stage == "received"),
                               key=lambda d: out["sd_added"][str(d.id)] or datetime.min, reverse=True)
    out["sd_received"] = [d for d in deals if d.stage == "won"]
    return out
