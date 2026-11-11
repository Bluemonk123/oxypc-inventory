"""Deal Pricing workflow (2026-11-10, Pankaj) — shared by Supplier Deals
(crm_sourcing_steps) and Customer Deals (crm_sales_steps).

  * Saving the Pricing card records a "pricing_saved" step (who / when) — the
    "Pricing Added" stage shows it.
  * "Pricing Approval" has one Level 1 approval (pricing_l1), assigned and
    decided like Quote Level 1.
  * A Negotiation Level 1 rejection sends the deal back to Pricing Approval
    (pricing_l1 marked Rejected with a note). Changing the prices afterwards
    resubmits pricing_l1 to the same approver as Pending, and puts a rejected
    Negotiation Level 1 back to Pending too.
No new tables: the steps tables already hold one row per deal + step key.
"""
from sqlalchemy import select

from utils.timezone import app_now

SAVED_KEY = "pricing_saved"
APPROVAL_KEY = "pricing_l1"


async def _step(db, Step, deal_id, key):
    return (await db.execute(select(Step).where(Step.deal_id == deal_id, Step.step_key == key))).scalar_one_or_none()


async def record_pricing_saved(db, Step, deal, user, prices_changed: bool) -> bool:
    """Mark Pricing Added; resubmit Pricing Approval when prices changed.
    Returns True when Pricing Approval went back to Pending."""
    now = app_now()
    st = await _step(db, Step, deal.id, SAVED_KEY)
    if not st:
        st = Step(deal_id=deal.id, stage="pricing", step_key=SAVED_KEY, created_by=user.username)
        db.add(st)
    st.status, st.acted_by, st.acted_at = "done", user.username, now
    # Pricing saved while the deal still sits at Lead / Pricing Added -> Pricing Added is done
    _has_price = any(getattr(deal, c, None) for c in
                     ("asking_price_unit", "our_offer_unit", "budget_per_unit", "estimated_value"))
    if _has_price and getattr(deal, "stage", None) in ("lead", "pricing"):
        deal.stage = "contacted"
    if not prices_changed:
        return False
    ap = await _step(db, Step, deal.id, APPROVAL_KEY)
    if ap and ap.assigned_to and ap.status != "pending":
        ap.status, ap.acted_by, ap.acted_at = "pending", None, None
        ap.note = "Resubmitted after a pricing change"
        neg = await _step(db, Step, deal.id, "negotiation_l1")
        if neg and neg.status == "rejected":
            neg.status, neg.acted_by, neg.acted_at = "pending", None, None
        return True
    return False


async def on_step_decided(db, Step, deal, step_key, decision, user) -> str | None:
    """After an approval decision. A rejected Negotiation Level 1 moves the
    deal back to Pricing Approval; returns the new stage when it moved."""
    if step_key != "negotiation_l1" or decision != "rejected" or deal.stage in ("won", "lost"):
        return None
    deal.stage = "pricing_approval"
    ap = await _step(db, Step, deal.id, APPROVAL_KEY)
    if not ap:
        ap = Step(deal_id=deal.id, stage="pricing_approval", step_key=APPROVAL_KEY, created_by=user.username)
        db.add(ap)
    ap.status = "rejected"
    ap.acted_by, ap.acted_at = user.username, app_now()
    ap.note = "Negotiation rejected — update Pricing to resubmit"
    return "pricing_approval"


def safe_return(path: str | None) -> str | None:
    """Only same-site Activity Board / Accounts paths are allowed as a return target."""
    p = (path or "").strip()
    # Activity Board approvals, and the Accounts Dashboard order approvals (2026-11-11)
    return p if (p.startswith("/activity-board") or p.startswith("/accounts")) and "//" not in p and "\\" not in p else None


def back_to(path: str, msg: str, ok: bool = True):
    """Redirect to a safe Activity Board path with a success / error message."""
    from urllib.parse import quote_plus
    from fastapi.responses import RedirectResponse
    base, _, frag = path.partition("#")
    sep = "&" if "?" in base else "?"
    return RedirectResponse(url=f"{base}{sep}{'success' if ok else 'error'}={quote_plus(msg)}"
                                + (f"#{frag}" if frag else ""), status_code=302)
