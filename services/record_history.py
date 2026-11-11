"""Record History (2026-11-09, Pankaj) — the "History" card on the Supplier
Deal, Customer Deal, Account, Quote, Purchase Order and Sales Order detail
pages: every action taken on the record, with Taken By and Taken On.

Source: audit_logs rows whose record_id is the record's id. Ids are UUIDs, so
this also picks up rows written against the same record from other tables
(Stage Progress steps, team mapping, KYC / credit / Accounts verification).
Read-only; nothing here writes.
"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.engines import AuditLog
from models.user import User

HISTORY_LIMIT = 300

# Friendly names for the actions these records write. Anything not listed is
# shown from its code, e.g. CRM_DEAL_DOCS_UPLOADED -> "Deal Docs Uploaded".
ACTION_LABELS = {
    "DEALER_QUOTATION_CREATED": "Quotation created",
    "DEALER_QUOTATION_STATUS": "Status changed",
    "DEALER_QUOTATION_APPROVED": "Level 1 approved",
    "DEALER_QUOTATION_REJECTED": "Level 1 rejected",
    "DEALER_QUOTATION_RENUMBERED": "Renumbered",
    "CRM_QUOTE_RENUMBERED": "Renumbered",
    "CRM_SOURCING_DEAL_RENUMBERED": "Renumbered",
    "CRM_SALES_DEAL_RENUMBERED": "Renumbered",
    "CRM_SALES_ORDER_RENUMBERED": "Renumbered",
    "CRM_PO_RENUMBERED": "Renumbered",
    "ACCOUNT_CREATED": "Account created",
    "ACCOUNT_EDITED": "Account edited",
    "ACCOUNT_TRASHED": "Moved to trash",
    "ACCOUNT_RESTORED": "Restored from trash",
    "ACCOUNT_KYC_VERIFIED": "KYC verified",
    "ACCOUNT_CREDIT_VERIFIED": "Credit verified",
    "TEAM_MAPPING_UPDATED": "Team mapping updated",
    "CRM_DEAL_CREATED": "Deal created",
    "CRM_SALES_DEAL_CREATED": "Deal created",
    "CRM_SOURCING_DEAL_EDITED": "Deal edited",
    "CRM_SALES_DEAL_EDITED": "Deal edited",
    "CRM_SOURCING_STAGE_MOVED": "Stage changed",
    "CRM_SALES_STAGE_MOVED": "Stage changed",
    "CRM_SOURCING_NEXT_STAGE": "Moved to next stage",
    "CRM_SALES_NEXT_STAGE": "Moved to next stage",
    "CRM_SOURCING_STEP_ASSIGNED": "Assigned",
    "CRM_SALES_STEP_ASSIGNED": "Assigned",
    "CRM_SOURCING_STEP_DECIDED": "Approval decided",
    "CRM_SALES_STEP_DECIDED": "Approval decided",
    "CRM_SOURCING_STEP_FILE": "Document attached",
    "CRM_SALES_STEP_FILE": "Document attached",
    "CRM_DEAL_DOCS_UPLOADED": "Documents uploaded",
    "CRM_SOURCING_LOT_LINKED": "Lot linked",
    "CRM_SALES_SALE_LINKED": "Sale records linked",
    "CRM_QUOTE_CREATED": "Quote created",
    "CRM_QUOTE_STATUS_CHANGED": "Quote status changed",
    "CRM_QUOTE_DOCUMENT_GENERATED": "Quote document generated",
    "CRM_QUOTE_LINKED_TO_DEAL": "Quote linked to deal",
    "CRM_QUOTE_INVOICE_UPLOADED": "Invoice uploaded",
    "QUOTE_PAYMENT_COMPLETED": "Payment completed",
    "CRM_PO_CREATED": "PO created",
    "CRM_PO_GENERATED": "PO generated from deal",
    "CRM_PO_ISSUED": "PO issued",
    "CRM_PO_INVOICE_UPLOADED": "Invoice uploaded",
    "PO_PAYMENT_COMPLETED": "Payment completed",
    "ACCOUNTS_PO_VERIFIED": "Verified by Accounts",
    "CRM_SALES_ORDER_CREATED": "Sales Order created",
    "ACCOUNTS_SO_VERIFIED": "Verified by Accounts",
}


def _label(action: str) -> str:
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    words = action.split("_")
    if words and words[0] in ("CRM", "ACCOUNTS"):
        words = words[1:]
    return " ".join(words).capitalize() or action


def _load(raw):
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def _step_label(step_key: str) -> str:
    """'quote_l2' -> 'Quote — Level 2' from either deal type's step list."""
    from models.crm import SOURCING_STEP_INDEX, SALES_STEP_INDEX, SOURCING_STAGES, SALES_STAGES
    stages = dict(SOURCING_STAGES) | dict(SALES_STAGES)
    for idx in (SOURCING_STEP_INDEX, SALES_STEP_INDEX):
        if step_key in idx:
            stage, label, _kind = idx[step_key]
            return f"{stages.get(stage, stage.replace('_', ' ').title())} — {label}"
    return step_key.replace("_", " ")


def _details(action: str, old: dict, new: dict, notes: str, names: dict, id_names: dict | None = None) -> str:
    """A short, readable line for the Details column."""
    from models.crm import SOURCING_STAGES, SALES_STAGES
    stages = dict(SOURCING_STAGES) | dict(SALES_STAGES)
    parts = []
    if action == "TEAM_MAPPING_UPDATED":
        # new_value = {bucket: [user ids]} — "Manager: Ravi; Finance Manager: (cleared)"
        from services.crm_team import BUCKET_LABELS
        for b, ids in new.items():
            who = ", ".join((id_names or {}).get(str(i), "?") for i in (ids or []))
            parts.append(f"{BUCKET_LABELS.get(b, b)}: {who or '(cleared)'}")
        return "; ".join(parts)
    if "stage" in new and ("STAGE" in action or "LOT_LINKED" in action or "SALE_LINKED" in action):
        before = old.get("stage")
        after = new.get("stage")
        txt = stages.get(after, after)
        if before and before != after:
            txt = f"{stages.get(before, before)} → {txt}"
        parts.append(txt)
        if new.get("win_loss_reason") and after in ("won", "lost"):
            parts.append(f"reason: {new['win_loss_reason']}")
    if new.get("step"):
        parts.append(_step_label(new["step"]))
    if "assigned_to" in new and "STEP_ASSIGNED" in action:
        who = new.get("assigned_to")
        parts.append(f"to {names.get(who, who)}" if who else "cleared")
    if "STEP_DECIDED" in action and new.get("status"):
        parts.append(new["status"].title() + (f" — {new['note']}" if new.get("note") else ""))
    elif "status" in new and "STATUS" in action or action == "CRM_PO_ISSUED":
        before, after = old.get("status"), new.get("status")
        parts.append(f"{before} → {after}" if before else str(after))
    for key, lbl in (("po_number", "PO"), ("so_number", "SO"), ("quote_number", "Quote"),
                     ("credit_limit", "Credit limit")):
        if new.get(key) and not any(lbl in p for p in parts):
            parts.append(f"{lbl} {new[key]}")
    if not parts and notes:
        parts.append(notes)
    return " · ".join(str(p) for p in parts if p)


async def record_history(db: AsyncSession, record_id) -> list:
    """Newest first: [{"action", "details", "by", "at"}] for one record."""
    if not record_id:
        return []
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.record_id == str(record_id))
        .order_by(AuditLog.timestamp.desc()).limit(HISTORY_LIMIT)
    )).scalars().all()
    if not rows:
        return []
    users = (await db.execute(select(User))).scalars().all()
    names = {u.username: (u.full_name or u.username) for u in users}
    id_names = {str(u.id): (u.full_name or u.username) for u in users}
    out = []
    for r in rows:
        old, new = _load(r.old_value), _load(r.new_value)
        out.append({
            "action": _label(r.action),
            "details": _details(r.action, old, new, r.notes or "", names, id_names),
            "by": names.get(r.username, r.username or "System"),
            "at": r.timestamp,
        })
    return out
