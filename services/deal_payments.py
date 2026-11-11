"""Deal Payment requests (2026-11-11, Pankaj).

A deal's Stage Progress → Payment stage has a "Level 1 approval" user. That
request is verified in Accounts, not on the deal:

  * Accounts dashboard — Supplier Payment card (Supplier Deals) and Customer
    Payment card (Customer Deals): pill tabs Pending / Verified / Rejected,
    Deal ID first column, Verify / Reject.
  * Supplier Payment / Customer Payment pages — tabs Pending / Completed /
    Rejected, Verify Payment / Reject Payment.

Verify opens the existing Complete Payment form (date, amount, UTR, mode,
snapshot). Saving it records the payment already verified, marks the
Payment approval Approved with the snapshot as its document, and the
Payment stage is Done (the deal moves on). Reject asks for a reason and marks
the approval Rejected. The deal page shows the same status and document.
"""
from sqlalchemy import select

KINDS = ("supplier", "customer")


def _models(kind):
    from models.crm import (CRMSourcingDeal, CRMSourcingStep, CRMSalesOpportunity, CRMSalesStep,
                            SupplierPayment, CustomerReceipt, SOURCING_STAGES, SALES_STAGES,
                            SOURCING_STAGE_STEPS, SALES_STAGE_STEPS)
    if kind == "supplier":
        return dict(Deal=CRMSourcingDeal, Step=CRMSourcingStep, Pay=SupplierPayment,
                    pay_fk="sourcing_deal_id", stages=SOURCING_STAGES, stage_steps=SOURCING_STAGE_STEPS,
                    url="/crm/sourcing/", num="deal_number", deal_type="sourcing")
    return dict(Deal=CRMSalesOpportunity, Step=CRMSalesStep, Pay=CustomerReceipt,
                pay_fk="opportunity_id", stages=SALES_STAGES, stage_steps=SALES_STAGE_STEPS,
                url="/crm/sales/", num="opp_number", deal_type="sales")


async def latest_deal_payment(db, kind, deal_id):
    """The newest verified payment recorded for a deal (its proof is the document)."""
    m = _models(kind)
    Pay = m["Pay"]
    return (await db.execute(
        select(Pay).where(getattr(Pay, m["pay_fk"]) == deal_id, Pay.verified.is_(True))
        .order_by(Pay.verified_at.desc().nullslast(), Pay.created_at.desc()).limit(1))).scalars().first()


async def payment_requests(db, kind) -> dict:
    """{'pending': [...], 'verified': [...], 'rejected': [...]} of Payment Level 1
    requests (someone assigned) on Supplier or Customer Deals."""
    from models.crm import CRMContact, CRMPurchaseOrder, CRMSalesOrder
    m = _models(kind)
    Deal, Step, Pay = m["Deal"], m["Step"], m["Pay"]
    pairs = (await db.execute(
        select(Step, Deal).join(Deal, Step.deal_id == Deal.id)
        .where(Step.step_key == "payment_l1", Step.assigned_to.isnot(None), Step.assigned_to != ""))).all()
    out = {"pending": [], "verified": [], "rejected": []}
    if not pairs:
        return out
    ids = [d.id for _s, d in pairs]
    cids = {d.contact_id for _s, d in pairs if d.contact_id}
    accounts = {c.id: c for c in (await db.execute(select(CRMContact).where(CRMContact.id.in_(cids)))).scalars().all()} if cids else {}
    # amount to prefill: the deal's latest PO (supplier) / Sales Order (customer)
    amounts, orders = {}, {}
    if kind == "supplier":
        for po in (await db.execute(select(CRMPurchaseOrder).where(CRMPurchaseOrder.deal_id.in_(ids))
                                    .order_by(CRMPurchaseOrder.created_at))).scalars().all():
            amounts[po.deal_id] = float(po.total_amount or 0)
            orders[po.deal_id] = {"number": po.po_number, "url": f"/crm/purchase-orders/{po.id}"}
    else:
        for so in (await db.execute(select(CRMSalesOrder).where(CRMSalesOrder.deal_id.in_(ids))
                                    .order_by(CRMSalesOrder.created_at))).scalars().all():
            amounts[so.deal_id] = float(so.total_amount or 0)
            orders[so.deal_id] = {"number": so.so_number, "url": f"/crm/sales-orders/{so.id}"}
    payments = {}
    for p in (await db.execute(select(Pay).where(getattr(Pay, m["pay_fk"]).in_(ids), Pay.verified.is_(True))
                               .order_by(Pay.created_at))).scalars().all():
        payments[getattr(p, m["pay_fk"])] = p                # newest verified payment per deal
    for st, d in pairs:
        status = st.status if st.status in ("approved", "rejected") else "pending"
        if status == "pending" and d.stage in ("won", "lost"):
            continue
        row = {"kind": kind, "deal": d, "number": getattr(d, m["num"]), "url": f"{m['url']}{d.id}",
               "account": accounts.get(d.contact_id), "assignee": st.assigned_to, "status": status,
               "acted_by": st.acted_by, "acted_at": st.acted_at, "note": st.note,
               "requested_at": st.updated_at or st.created_at, "amount": amounts.get(d.id, 0.0), "order": orders.get(d.id),   # PO / SO ID under the name (2026-11-11)
               "payment": payments.get(d.id) if status == "approved" else None}
        out["verified" if status == "approved" else status].append(row)
    for k in out:
        out[k].sort(key=lambda r: (r["acted_at"] or r["requested_at"] or r["deal"].created_at), reverse=True)
    return out


async def verify_payment(db, kind, deal, user, *, pd, amt, mode, utr, snapshot, request=None):
    """Complete Payment for a deal's payment request: payment recorded already
    verified, approval Approved (snapshot = document), stage Done."""
    from utils.timezone import app_now
    from models.crm import CRMActivity, CRMPurchaseOrder, CRMSalesOrder
    from services.audit_engine import audit
    from services.stage_advance import advance, reopen_approvals
    m = _models(kind)
    Step, Pay = m["Step"], m["Pay"]
    now = app_now()
    step = (await db.execute(select(Step).where(Step.deal_id == deal.id, Step.step_key == "payment_l1"))).scalar_one_or_none()
    if kind == "supplier":
        po = (await db.execute(select(CRMPurchaseOrder).where(CRMPurchaseOrder.deal_id == deal.id)
                               .order_by(CRMPurchaseOrder.created_at.desc()).limit(1))).scalar_one_or_none()
        pay = Pay(po_id=po.id if po else None, contact_id=deal.contact_id, sourcing_deal_id=deal.id,
                  payment_date=pd, amount=amt, payment_mode=mode, reference_no=utr,
                  notes=f"Payment verified - {deal.deal_number}", payment_photo_path=snapshot or None,
                  created_by=user.username, verified=True, verified_by=user.username, verified_at=now)
        if po:
            po.payment_status, po.utr_number = "paid", utr
            if snapshot:
                po.payment_snapshot_path = snapshot
    else:
        so = (await db.execute(select(CRMSalesOrder).where(CRMSalesOrder.deal_id == deal.id)
                               .order_by(CRMSalesOrder.created_at.desc()).limit(1))).scalar_one_or_none()
        pay = Pay(contact_id=deal.contact_id, opportunity_id=deal.id, so_id=so.id if so else None,
                  receipt_date=pd, amount=amt, payment_mode=mode, reference_no=utr,
                  notes=f"Payment verified - {deal.opp_number}", payment_photo_path=snapshot or None,
                  created_by=user.username, verified=True, verified_by=user.username, verified_at=now)
    db.add(pay)
    await db.flush()
    old = {"status": step.status if step else None}
    if step:
        step.status, step.acted_by, step.acted_at = "approved", user.username, now
        step.note = f"UTR {utr}"
    labels = dict(m["stages"])
    db.add(CRMActivity(contact_id=deal.contact_id, deal_id=deal.id, deal_type=m["deal_type"], activity_type="note",
                       summary=f"Payment — Level 1: Verified by Accounts (₹{amt}, {mode.upper()}, UTR {utr})",
                       performed_by=user.username, outcome="done"))
    moved = advance(deal, "payment", m["stages"])
    if moved:
        text = f"Stage moved to: {labels.get(moved[1], moved[1])} (payment verified by Accounts)"
        if await reopen_approvals(db, Step, deal.id, moved[1], m["stage_steps"]):
            text += " — its approvals reset to Pending"
        db.add(CRMActivity(contact_id=deal.contact_id, deal_id=deal.id, deal_type=m["deal_type"], activity_type="note",
                           summary=text, performed_by=user.username, outcome="done"))
    await audit(db, action="ACCOUNTS_DEAL_PAYMENT_VERIFIED", user=user,
                table_name=("crm_sourcing_steps" if kind == "supplier" else "crm_sales_steps"),
                record_id=str(deal.id), old_value=old,
                new_value={"deal": getattr(deal, m["num"]), "status": "approved", "payment_id": str(pay.id),
                           "amount": str(amt), "mode": mode, "utr": utr, "document": snapshot,
                           **({"stage": moved[1]} if moved else {})}, request=request)
    return pay, moved


async def reject_payment(db, kind, deal, user, reason, request=None):
    from utils.timezone import app_now
    from models.crm import CRMActivity
    from services.audit_engine import audit
    m = _models(kind)
    Step = m["Step"]
    step = (await db.execute(select(Step).where(Step.deal_id == deal.id, Step.step_key == "payment_l1"))).scalar_one_or_none()
    if not step:
        return False
    old = {"status": step.status}
    step.status, step.acted_by, step.acted_at, step.note = "rejected", user.username, app_now(), reason
    db.add(CRMActivity(contact_id=deal.contact_id, deal_id=deal.id, deal_type=m["deal_type"], activity_type="note",
                       summary=f"Payment — Level 1: Rejected by Accounts — {reason}",
                       performed_by=user.username, outcome="done"))
    await audit(db, action="ACCOUNTS_DEAL_PAYMENT_REJECTED", user=user,
                table_name=("crm_sourcing_steps" if kind == "supplier" else "crm_sales_steps"),
                record_id=str(deal.id), old_value=old,
                new_value={"deal": getattr(deal, m["num"]), "status": "rejected", "reason": reason}, request=request)
    return True
