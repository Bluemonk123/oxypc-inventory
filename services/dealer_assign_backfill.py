"""Data Fix (2026-11-03): past "Assigned On" dates for dealers.

Until 2026-11-03 nothing wrote dealer_assignments, so the Dealers table's
"Assigned On" column had no history. Every bulk assignment was audited
(DEALER_LEADS_BULK_ASSIGNED, new_value = {"assigned_to", "dealer_ids"}), so
for each dealer still with that assignee the latest such entry gives the
date it was assigned. Applying adds one dealer_assignments row per dealer.
Dealers with no audited assignment keep the fallback (the date added).
"""
import json

from sqlalchemy import select

from models.dealers import Dealer, DealerAssignment
from models.engines import AuditLog


async def assign_backfill_plan(db) -> list:
    """[(dealer_id, business_name, assigned_to, assigned_at, assigned_by)] —
    assigned dealers with no dealer_assignments row for their current
    assignee whose assignment is in the audit log."""
    latest: dict = {}
    for ts, who_by, nv in (await db.execute(
            select(AuditLog.timestamp, AuditLog.username, AuditLog.new_value)
            .where(AuditLog.action == "DEALER_LEADS_BULK_ASSIGNED"))).all():
        try:
            v = json.loads(nv or "{}")
        except (TypeError, ValueError):
            continue
        to = v.get("assigned_to")
        for did in v.get("dealer_ids") or []:
            cur = latest.get(did)
            if cur is None or ts > cur[0]:
                latest[did] = (ts, to, who_by)
    if not latest:
        return []
    have = {(str(d), u) for d, u in (await db.execute(
        select(DealerAssignment.dealer_id, DealerAssignment.assigned_to))).all()}
    out = []
    for did, name, owner in (await db.execute(
            select(Dealer.id, Dealer.business_name, Dealer.assigned_to)
            .where(Dealer.assigned_to.isnot(None), Dealer.trashed_at.is_(None)))).all():
        hit = latest.get(str(did))
        if hit and hit[1] == owner and (str(did), owner) not in have:
            out.append((did, name, owner, hit[0], hit[2] or "system"))
    out.sort(key=lambda r: r[3], reverse=True)
    return out


async def assign_backfill_apply(db, user, request=None) -> dict:
    from services.audit_engine import audit
    plan = await assign_backfill_plan(db)
    for did, _n, owner, at, by in plan:
        db.add(DealerAssignment(dealer_id=did, assigned_to=owner, assigned_by=by, assigned_at=at))
    await audit(db, user=user, action="DEALER_ASSIGNED_ON_BACKFILL", table_name="dealer_assignments",
                record_id=f"{len(plan)} dealers",
                new_value={"count": len(plan)},
                notes="Assigned On dates copied from DEALER_LEADS_BULK_ASSIGNED audit entries. "
                      "Reverse: delete the dealer_assignments rows added at this time.",
                request=request)
    await db.commit()
    return {"ok": True, "added": len(plan)}
