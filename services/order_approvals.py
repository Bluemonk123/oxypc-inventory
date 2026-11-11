"""Order approvals on the Accounts Dashboard (2026-11-11, Pankaj).

The Purchase Order stage (Supplier Deal: po_l1 / po_l2) and the Invoice /
Sales Order stage (Customer Deal: so_l1 / so_l2) have Level 1 / Level 2
approvers in Stage Progress, but no page listed them. The Accounts Dashboard's
Purchase Order and Sales Order cards now do, with Verify / Reject for the
assigned approver (or an admin). Verify / Reject post to the deal's own
decide route with return_to=/accounts, so the usual rules apply: any level
first, and the stage is Done (deal moves on) once every level is approved.
"""
from sqlalchemy import select

ORDER_KINDS = {
    # Level 1 only (2026-11-11, Pankaj)
    "po": ("supplier", ("po_l1",)),
    "so": ("customer", ("so_l1",)),
}


async def order_approval_rows(db, which, user) -> dict:
    """{'pending': [...], 'done': [...]} for 'po' or 'so'."""
    from models.crm import (CRMSourcingDeal, CRMSourcingStep, CRMSalesOpportunity, CRMSalesStep,
                            CRMPurchaseOrder, CRMSalesOrder, CRMContact)
    kind, keys = ORDER_KINDS[which]
    if kind == "supplier":
        Deal, Step, num, url, Order, onum, ourl = (CRMSourcingDeal, CRMSourcingStep, "deal_number", "/crm/sourcing/",
                                                   CRMPurchaseOrder, "po_number", "/crm/purchase-orders/")
    else:
        Deal, Step, num, url, Order, onum, ourl = (CRMSalesOpportunity, CRMSalesStep, "opp_number", "/crm/sales/",
                                                   CRMSalesOrder, "so_number", "/crm/sales-orders/")
    pairs = (await db.execute(select(Step, Deal).join(Deal, Step.deal_id == Deal.id).where(
        Step.step_key.in_(keys), Step.assigned_to.isnot(None), Step.assigned_to != ""))).all()
    out = {"pending": [], "done": []}
    if not pairs:
        return out
    ids = list({d.id for _s, d in pairs})
    orders = {}
    for o in (await db.execute(select(Order).where(Order.deal_id.in_(ids)).order_by(Order.created_at))).scalars().all():
        orders[o.deal_id] = o                                   # latest order of the deal
    cids = {d.contact_id for _s, d in pairs if d.contact_id}
    accounts = {c.id: c for c in (await db.execute(select(CRMContact).where(CRMContact.id.in_(cids)))).scalars().all()} if cids else {}
    role = getattr(user.role, "value", user.role)
    for st, d in pairs:
        status = st.status if st.status in ("approved", "rejected") else "pending"
        if status == "pending" and d.stage in ("won", "lost"):
            continue
        o = orders.get(d.id)
        out["pending" if status == "pending" else "done"].append({
            "deal": d, "number": getattr(d, num), "url": f"{url}{d.id}", "account": accounts.get(d.contact_id),
            "order": ({"number": getattr(o, onum), "url": f"{ourl}{o.id}"} if o else None),
            "level": "Level 1" if st.step_key.endswith("l1") else "Level 2",
            "assignee": st.assigned_to, "status": status, "note": st.note,
            "acted_by": st.acted_by, "acted_at": st.acted_at,
            "decide": f"{url}{d.id}/steps/{st.step_key}/decide",
            "can_decide": status == "pending" and (st.assigned_to == user.username or role == "admin"),
        })
    for k in out:
        out[k].sort(key=lambda r: (r["acted_at"] or r["deal"].created_at), reverse=True)
    return out
