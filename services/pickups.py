"""Pickup Process (2026-11-11, Pankaj).

A pickup starts when someone is assigned on a deal's Stock Received (Supplier
Deal) or Stock Handover (Customer Deal) stage — the `received_assign` item of
Stage Progress. Its progress is kept in a separate deal step row,
`pickup_status` (status = one of the flow keys below), so re-assigning the
user never resets the pickup. No new table or column.

Supplier Deal flow: Created → Scheduled → In-progress → Completed → In-transit → Received
Customer Deal flow: Created → Scheduled → Completed → Handover
"""
from sqlalchemy import select

ASSIGN_KEY = "received_assign"
STATUS_KEY = "pickup_status"

FLOWS = {
    "supplier": [("created", "Pickup Created"), ("scheduled", "Pickup Scheduled"),
                 ("in_progress", "Pickup In-progress"), ("completed", "Pickup Completed"),
                 ("in_transit", "Pickup In-transit"), ("received", "Received")],
    "customer": [("created", "Pickup Created"), ("scheduled", "Pickup Scheduled"),
                 ("completed", "Pickup Completed"), ("handover", "Handover")],
}
# pill labels
SHORT = {"created": "Created", "scheduled": "Scheduled", "in_progress": "In-progress",
         "completed": "Completed", "in_transit": "In-transit", "received": "Received", "handover": "Handover"}
COLORS = {"created": "#6c757d", "scheduled": "#0d6efd", "in_progress": "#f0ad00",
          "completed": "#198754", "in_transit": "#6f42c1", "received": "#0F7A37", "handover": "#0F7A37"}
# summary tiles: (title, supplier status, customer status — None = Not Required)
TILES = [("Created", "created", "created"), ("Scheduled", "scheduled", "scheduled"),
         ("In-progress", "in_progress", None), ("Completed", "completed", "completed"),
         ("In-transit", "in_transit", None), ("Received / Handover", "received", "handover")]


def _models(kind):
    from models.crm import CRMSourcingDeal, CRMSourcingStep, CRMSalesOpportunity, CRMSalesStep
    if kind == "supplier":
        return CRMSourcingDeal, CRMSourcingStep, "deal_number", "/crm/sourcing/", "sourcing"
    return CRMSalesOpportunity, CRMSalesStep, "opp_number", "/crm/sales/", "sales"


def next_status(kind, status):
    keys = [k for k, _ in FLOWS[kind]]
    i = keys.index(status) if status in keys else 0
    return keys[i + 1] if i + 1 < len(keys) else None


async def pickup_rows(db, kind, user=None, see_all=True) -> list:
    """One row per deal with someone assigned on Stock Received / Handover."""
    from models.crm import CRMContact
    Deal, Step, num, url, _dt = _models(kind)
    q = select(Step, Deal).join(Deal, Step.deal_id == Deal.id).where(
        Step.step_key == ASSIGN_KEY, Step.assigned_to.isnot(None), Step.assigned_to != "",
        Deal.stage != "lost")
    if not see_all and user is not None:
        q = q.where(Step.assigned_to == user.username)
    pairs = (await db.execute(q)).all()
    if not pairs:
        return []
    ids = [d.id for _s, d in pairs]
    status = {s.deal_id: s for s in (await db.execute(
        select(Step).where(Step.deal_id.in_(ids), Step.step_key == STATUS_KEY))).scalars().all()}
    cids = {d.contact_id for _s, d in pairs if d.contact_id}
    accounts = {c.id: c for c in (await db.execute(select(CRMContact).where(CRMContact.id.in_(cids)))).scalars().all()} if cids else {}
    keys = [k for k, _ in FLOWS[kind]]
    rows = []
    for a, d in pairs:
        ps = status.get(d.id)
        st = ps.status if ps and ps.status in keys else "created"
        nxt = next_status(kind, st)
        rows.append({"kind": kind, "deal": d, "number": getattr(d, num), "url": f"{url}{d.id}",
                     "account": accounts.get(d.contact_id), "assignee": a.assigned_to,
                     "assigned_on": a.acted_at or a.updated_at or a.created_at,
                     "status": st, "status_label": dict(FLOWS[kind])[st],
                     "updated_by": ps.acted_by if ps else None, "updated_at": ps.acted_at if ps else None,
                     "note": ps.note if ps else None,
                     "next": nxt, "next_label": dict(FLOWS[kind]).get(nxt) if nxt else None})
    rows.sort(key=lambda r: (r["updated_at"] or r["assigned_on"] or r["deal"].created_at), reverse=True)
    return rows


async def advance_pickup(db, kind, deal_id, user, note="", request=None):
    """Move a pickup one step on. Returns (old, new) or an error string."""
    from utils.timezone import app_now
    from models.crm import CRMActivity
    from services.audit_engine import audit
    Deal, Step, num, _url, deal_type = _models(kind)
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        return "Deal not found"
    a = (await db.execute(select(Step).where(Step.deal_id == deal.id, Step.step_key == ASSIGN_KEY))).scalar_one_or_none()
    if not a or not a.assigned_to:
        return "No one is assigned to this pickup"
    ps = (await db.execute(select(Step).where(Step.deal_id == deal.id, Step.step_key == STATUS_KEY))).scalar_one_or_none()
    keys = [k for k, _ in FLOWS[kind]]
    old = ps.status if ps and ps.status in keys else "created"
    new = next_status(kind, old)
    if not new:
        return "This pickup is already finished"
    if not ps:
        ps = Step(deal_id=deal.id, stage="received", step_key=STATUS_KEY, created_by=user.username)
        db.add(ps)
    ps.status, ps.acted_by, ps.acted_at = new, user.username, app_now()
    ps.note = (note or "").strip()[:500] or None
    label = dict(FLOWS[kind])[new]
    db.add(CRMActivity(contact_id=deal.contact_id, deal_id=deal.id, deal_type=deal_type, activity_type="note",
                       summary=f"Pickup: {label}" + (f" — {ps.note}" if ps.note else ""),
                       performed_by=user.username, outcome="done"))
    await audit(db, action="CRM_PICKUP_STATUS", user=user,
                table_name=("crm_sourcing_steps" if kind == "supplier" else "crm_sales_steps"),
                record_id=str(deal.id), old_value={"pickup": old},
                new_value={"deal": getattr(deal, num), "pickup": new, "note": ps.note}, request=request)
    return old, new
