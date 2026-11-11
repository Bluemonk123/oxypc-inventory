"""Activity Board completes a stage (2026-11-11, Pankaj): when an item is
completed there — an Inspection / Screening Report uploaded, or a stage's
Level approvals all Approved (Pricing Approvals / Negotiation tabs) — the
deal moves on one stage, so its Stage Progress shows that stage as Done.
Only when the deal is still at that stage; Stock Received (Won / Lost) is
never closed from here."""
from sqlalchemy import select


def next_stage_key(stages, stage):
    order = [k for k, _ in stages if k not in ("won", "lost")]
    if stage not in order or stage == order[-1]:
        return None
    return order[order.index(stage) + 1]


async def all_approvals_done(db, Step, deal_id, stage, stage_steps) -> bool:
    keys = [k for k, _l, kind in stage_steps.get(stage, []) if kind == "approval"]
    if not keys:
        return False
    rows = {s.step_key: s for s in (await db.execute(
        select(Step).where(Step.deal_id == deal_id, Step.step_key.in_(keys)))).scalars().all()}
    return all(k in rows and rows[k].status == "approved" for k in keys)


def advance(deal, stage, stages):
    """Move the deal from `stage` to the next one; returns (old, new) or None."""
    if deal.stage != stage or stage == "received":
        return None
    new = next_stage_key(stages, stage)
    if not new:
        return None
    old, deal.stage = deal.stage, new
    return old, new


async def reopen_approvals(db, Step, deal_id, stage, stage_steps) -> list:
    """Entering a stage asks its approvals again (2026-11-11, Pankaj): a
    Negotiation rejection sends the deal back to Pricing Approval; when it comes
    forward again the Quote levels go back to Pending, and after them
    Negotiation. Every already-decided approval of `stage` is reset to Pending
    (approver kept, old decision kept in the Activity Log / audit). Returns the
    keys that were reset."""
    keys = [k for k, _l, kind in stage_steps.get(stage, []) if kind == "approval"]
    if not keys:
        return []
    out = []
    for s in (await db.execute(select(Step).where(Step.deal_id == deal_id, Step.step_key.in_(keys)))).scalars().all():
        if s.status in ("approved", "rejected"):
            s.status, s.note, s.acted_by, s.acted_at = "pending", None, None, None
            out.append(s.step_key)
    return out
