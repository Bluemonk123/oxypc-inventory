"""Stage + status of an Account's latest deal (2026-11-10, Pankaj) — the
Accounts list's Last Activity column shows "<Stage Name> [status badge]".

Latest deal = the Account's most recently created Supplier or Customer Deal.
Status of its current stage, from the stage's own steps:
  * Won / Lost                       -> Won (green) / Lost (red)
  * a stage with Level approvals     -> the furthest decided level: Approved /
                                        Rejected, else Pending (assigned) /
                                        Not Assigned
  * a stage with a report / file     -> Completed (file attached), Started,
                                        else Pending
  * anything else                    -> In Progress
"""
from sqlalchemy import select

from models.crm import (CRMSourcingDeal, CRMSourcingStep, CRMSalesOpportunity, CRMSalesStep,
                        SOURCING_STAGES, SOURCING_STAGE_STEPS, SALES_STAGES, SALES_STAGE_STEPS)

_KINDS = (
    (CRMSourcingDeal, CRMSourcingStep, dict(SOURCING_STAGES), SOURCING_STAGE_STEPS, "/crm/sourcing/", "deal_number"),
    (CRMSalesOpportunity, CRMSalesStep, dict(SALES_STAGES), SALES_STAGE_STEPS, "/crm/sales/", "opp_number"),
)


def _status(stage, stage_steps, rows):
    if stage == "won":
        return "Won", "success"
    if stage == "lost":
        return "Lost", "danger"
    steps = stage_steps.get(stage, [])
    approvals = [k for k, _l, kind in steps if kind == "approval"]
    if approvals:
        decided = [rows[k] for k in approvals if k in rows and rows[k].status in ("approved", "rejected")]
        if decided and decided[-1].status == "rejected":
            return "Rejected", "danger"
        if decided and len(decided) == len(approvals):
            return "Approved", "success"
        if any(k in rows and rows[k].assigned_to for k in approvals):
            return "Pending", "warning"
        return "Not Assigned", "secondary"
    files = [k for k, _l, kind in steps if kind == "file"]
    if files:
        if any(k in rows and rows[k].file_path for k in files):
            return "Completed", "success"
        if any(k in rows and rows[k].status == "started" for k in files):
            return "Started", "info"
        return "Pending", "warning"
    return "In Progress", "primary"


async def latest_deal_stage(db, contact_ids) -> dict:
    """{str(contact_id): {"stage": label, "status": text, "color": bootstrap colour,
    "url": deal page, "number": deal ID}} for Accounts that have a deal."""
    out, latest = {}, {}
    ids = [c for c in contact_ids if c]
    if not ids:
        return out
    for Deal, Step, labels, stage_steps, url, num_col in _KINDS:
        for d in (await db.execute(select(Deal).where(Deal.contact_id.in_(ids)))).scalars().all():
            k = str(d.contact_id)
            if k not in latest or (d.created_at and (latest[k][0].created_at or d.created_at) < d.created_at):
                latest[k] = (d, Step, labels, stage_steps, url, num_col)
    by_kind = {}
    for k, (d, Step, *_r) in latest.items():
        by_kind.setdefault(Step, []).append(d.id)
    rows = {}
    for Step, deal_ids in by_kind.items():
        for s in (await db.execute(select(Step).where(Step.deal_id.in_(deal_ids)))).scalars().all():
            rows.setdefault(s.deal_id, {})[s.step_key] = s
    for k, (d, Step, labels, stage_steps, url, num_col) in latest.items():
        text, color = _status(d.stage, stage_steps, rows.get(d.id, {}))
        stage_label = "Closed" if d.stage in ("won", "lost") else labels.get(d.stage, (d.stage or "").replace("_", " ").title())
        out[k] = {"stage": stage_label, "status": text,
                  "color": color, "url": f"{url}{d.id}", "number": getattr(d, num_col, "")}
    return out
