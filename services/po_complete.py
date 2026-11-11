"""Purchase Order complete (2026-11-11, Pankaj): Verify on the Accounts page
or Issue on the PO page does the same three things —
  1. the PO is Verified by Accounts,
  2. the PO is Issued (a draft becomes issued),
  3. the deal's Purchase Order stage is Done (the deal moves on to Payment).
Whichever happens first does all of it; the other then has nothing left to do."""
from sqlalchemy import select
from utils.timezone import app_now


async def complete_po(db, po, user, request=None, via="") -> list:
    """Returns what changed: any of 'verified', 'issued', 'stage'."""
    from models.crm import (CRMSourcingDeal, CRMSourcingStep, CRMActivity,
                            SOURCING_STAGES, SOURCING_STAGE_STEPS)
    from services.audit_engine import audit
    done, now = [], app_now()
    old = {"status": po.status, "verified": bool(po.verified)}
    if not po.verified:
        po.verified, po.verified_by, po.verified_at = True, user.username, now
        done.append("verified")
    if (po.status or "draft") == "draft":
        po.status, po.issued_by, po.issued_at = "issued", user.username, now
        done.append("issued")
    deal_old = None
    if po.deal_id:
        deal = (await db.execute(select(CRMSourcingDeal).where(CRMSourcingDeal.id == po.deal_id))).scalar_one_or_none()
        order = [k for k, _ in SOURCING_STAGES if k not in ("won", "lost")]
        po_i = order.index("purchase_order")
        if deal and deal.stage in order and order.index(deal.stage) <= po_i:
            deal_old, deal.stage = deal.stage, order[po_i + 1]      # Purchase Order Done -> Payment
            done.append("stage")
            labels = dict(SOURCING_STAGES)
            text = f"Stage moved to: {labels.get(deal.stage)} (PO {po.po_number} verified & issued{(' — ' + via) if via else ''})"
            from services.stage_advance import reopen_approvals
            if await reopen_approvals(db, CRMSourcingStep, deal.id, deal.stage, SOURCING_STAGE_STEPS):
                text += " — its approvals reset to Pending"
            db.add(CRMActivity(contact_id=deal.contact_id, deal_id=deal.id, deal_type="sourcing",
                               activity_type="note", summary=text, performed_by=user.username, outcome="done"))
            await audit(db, action="CRM_SOURCING_NEXT_STAGE", user=user, table_name="crm_sourcing_deals",
                        record_id=str(deal.id), old_value={"stage": deal_old}, new_value={"stage": deal.stage},
                        request=request)
    if done:
        await audit(db, action="CRM_PO_COMPLETED", user=user, table_name="crm_purchase_orders",
                    record_id=str(po.id), old_value=old,
                    new_value={"status": po.status, "verified": True, "done": done, "via": via},
                    request=request)
    return done
