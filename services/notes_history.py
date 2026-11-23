"""Notes History capture (2026-11-23, Pankaj). See models/notes_history.py.

Hooked on the ORM flush (after_flush, so new rows already have ids): every
tracked note field that a save sets or changes adds one notes_history row.
No route has to change, and nothing that already works is touched — the
original columns still hold the latest value.

RULES maps a model class name to
    (record_type, record-id attribute, device-id attribute, {field: note type}, label attribute)
The record is the one people look notes up by: a dealer's calls / orders /
credit notes are under the dealer, a deal's steps under the deal.
Special cases are in code: Final QC Fail (one row, Fail category from
fqc_failure_reason), Pre-Sale QC Fail (read from its audit entry),
receipts that belong to a dealer.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.util import identity_key

from models.notes_history import NotesHistory
from services.repair_location import current_user_name
from utils.timezone import app_now

RULES = {
    # ── TRC flow (tag) ──
    "Device": ("device", "id", "id", {
        "repair_notes": "L1/L2 Notes",
        "stress_notes": "Stress Fail Notes",
        "fqc_pass_notes": "Final QC Pass Notes",
        "notes": "Device Notes",
    }, "barcode"),
    "RepairAttempt": ("device", "device_id", "device_id", {"notes": "Resolution Notes"}, None),
    "IQCInspection": ("device", "device_id", "device_id", {"remarks": "IQC Notes"}, None),
    # ── Returns ──
    "Return": ("return", "id", "device_id", {"reason": "Return Reason", "notes": "Return Notes"}, None),
    "RentalReturn": ("rental_return", "id", None, {
        "return_reason": "Rental Return Reason", "notes": "Complaint Description",
        "waiver_reason": "Waiver Reason"}, None),
    # ── Dealers ──
    "Dealer": ("dealer", "id", None, {"notes": "Dealer Notes"}, "business_name"),
    "DealerAssignment": ("dealer", "dealer_id", None, {"notes": "Dealer Assignment Notes"}, None),
    "DealerCall": ("dealer", "dealer_id", None, {"notes": "Dealer Call Notes", "calling_remark": "Calling Remark"}, None),
    "DealerOrder": ("dealer", "dealer_id", None, {
        "notes": "Dealer Order Notes", "items_description": "Dealer Order Items"}, "order_number"),
    "DealerCreditNote": ("dealer", "dealer_id", None, {
        "reason": "Credit Note Reason", "notes": "Credit Note Notes",
        "items_description": "Credit Note Items"}, "credit_number"),
    # ── Customers / suppliers ──
    "CRMContact": ("contact", "id", None, {
        "notes": "Contact Notes", "do_not_contact_reason": "Do-Not-Contact Reason",
        "credit_verify_reason": "Credit Verify Reason"}, None),
    "CustomerReceipt": ("contact", "contact_id", None, {"notes": "Customer Receipt Notes"}, None),
    "SupplierPayment": ("contact", "contact_id", None, {"notes": "Supplier Payment Notes"}, None),
    # ── Quotations / orders / deals ──
    "DealerQuotation": ("quotation", "id", None, {
        "additional_notes": "Telecaller Quotation Notes",
        "special_conditions": "Telecaller Quotation Special Conditions"}, "quote_number"),
    "CRMQuote": ("quotation", "id", None, {
        "notes": "Account Quotation Notes",
        "special_conditions": "Account Quotation Special Conditions"}, "quote_number"),
    "CRMPurchaseOrder": ("purchase_order", "id", None, {"notes": "Purchase Order Notes"}, "po_number"),
    "CRMSalesOrder": ("sales_order", "id", None, {"notes": "Sales Order Notes"}, "so_number"),
    "CRMSourcingDeal": ("supplier_deal", "id", None, {
        "notes": "Supplier Deal Notes", "inspection_notes": "Inspection Notes",
        "win_loss_reason": "Supplier Deal Win/Loss Reason"}, "deal_number"),
    "CRMSourcingStep": ("supplier_deal", "deal_id", None, {"note": "Supplier Deal Step Note"}, None),
    "CRMSalesOpportunity": ("customer_deal", "id", None, {
        "notes": "Customer Deal Notes", "win_loss_reason": "Customer Deal Win/Loss Reason"}, "opp_number"),
    # ── Telecalling ──
    "CRMLead": ("lead", "id", None, {"full_remark": "Lead Remark"}, None),
    "CRMLeadCall": ("lead", "lead_id", None, {"full_remarks": "Lead Call Remarks"}, None),
    "TelecallingSession": ("telecalling", "id", None, {"notes": "Telecalling Session Notes"}, None),
    "TelecallingRecord": ("telecalling", "id", "device_id", {"notes": "Telecalling Record Notes"}, None),
    "TelecallingAssignment": ("telecalling", "id", None, {"notes": "Telecalling Assignment Notes"}, None),
}

RECORD_LABELS = {
    "device": "Tag", "return": "Return", "rental_return": "Rental Return", "dealer": "Dealer",
    "contact": "Customer / Supplier", "quotation": "Quotation", "purchase_order": "Purchase Order",
    "sales_order": "Sales Order", "supplier_deal": "Supplier Deal", "customer_deal": "Customer Deal",
    "lead": "Lead", "telecalling": "Telecalling",
}


def _text(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _stage_of(session, device_id):
    """The tag's stage if the Device is already loaded in this session (no query in a flush)."""
    if not device_id:
        return None
    from models.device import Device
    try:
        dev = session.identity_map.get(identity_key(Device, device_id))
    except Exception:
        dev = None
    st = getattr(dev, "current_stage", None) if dev is not None else None
    return getattr(st, "value", st)


def _changed(obj, field, is_new):
    """New value when this save set / changed it (non-empty), else None."""
    val = _text(getattr(obj, field, None))
    if val is None:
        return None
    if is_new:
        return val
    hist = sa_inspect(obj).attrs[field].history
    if not hist.added:
        return None
    old = [_text(x) for x in hist.deleted]
    return None if old and old[0] == val else val


def _who(obj):
    return (current_user_name.get() or getattr(obj, "created_by", None)
            or getattr(obj, "updated_by", None) or "system")


def _row(record_type, record_id, device_id, note_type, text, who, *, category=None, stage=None, source=None,
         written_at=None, backfilled=False):
    return {"id": uuid.uuid4(), "record_type": record_type, "record_id": str(record_id),
            "device_id": device_id, "note_type": note_type, "note_text": text[:20000],
            "category": (category or None) and str(category)[:60], "stage": (stage or None) and str(stage)[:40],
            "written_by": str(who)[:100], "written_at": written_at or app_now(),
            "source": (source or "")[:120] or None, "backfilled": backfilled}


def rows_for(session, obj, is_new):
    """notes_history rows for one saved object (empty when nothing tracked changed)."""
    cls = type(obj).__name__
    out = []
    if cls == "AuditLog":                                   # Pre-Sale QC Fail lives on its audit entry
        if is_new and obj.action == "PRE_SALE_IQC_COMPLETED":
            try:
                nv = json.loads(obj.new_value or "{}")
            except (TypeError, ValueError):
                nv = {}
            if nv.get("result") == "fail":
                did = _as_uuid(obj.record_id)
                out.append(_row("device", obj.record_id, did, "Pre-Sale QC Fail Notes",
                                _text(nv.get("reason")) or "—", obj.username or _who(obj),
                                category=nv.get("category"), stage="ready_to_sale",
                                source=f"Pre-Sale QC Mark Complete ({nv.get('work_id') or ''})"))
        return out
    rule = RULES.get(cls)
    if not rule:
        return out
    rtype, rid_attr, dev_attr, fields, label_attr = rule
    rid = getattr(obj, rid_attr, None)
    if cls == "CustomerReceipt" and not rid and getattr(obj, "dealer_id", None):
        rtype, rid = "dealer", obj.dealer_id               # a dealer's receipt
    if not rid:
        return out
    did = getattr(obj, dev_attr, None) if dev_attr else None
    if cls == "Device":
        stage = getattr(obj.current_stage, "value", obj.current_stage)
    else:
        stage = _stage_of(session, did) if did else None
    label = getattr(obj, label_attr, None) if label_attr else None
    who = _who(obj)
    for field, note_type in fields.items():
        val = _changed(obj, field, is_new)
        if val:
            out.append(_row(rtype, rid, did, note_type, val, who, stage=stage,
                            source=f"{cls}.{field}" + (f" ({label})" if label else "")))
    if cls == "Device":                                     # Final QC Fail: one row, with its category
        reason = _text(obj.fqc_failure_reason)
        notes_new = _changed(obj, "fqc_final_notes", is_new)
        reason_new = _changed(obj, "fqc_failure_reason", is_new)
        if reason and (notes_new or reason_new):
            out.append(_row("device", obj.id, obj.id, "Final QC Fail Notes",
                            _text(obj.fqc_final_notes) or "—", who, category=reason,
                            stage=getattr(obj.current_stage, "value", obj.current_stage),
                            source=f"Final QC Fail ({obj.barcode})"))
    return out


def _as_uuid(v):
    try:
        return uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


@event.listens_for(Session, "after_flush")
def _capture_notes(session, flush_context):
    rows = []
    for obj in list(session.new):
        if type(obj).__name__ in RULES or type(obj).__name__ == "AuditLog":
            rows += rows_for(session, obj, True)
    for obj in list(session.dirty):
        if type(obj).__name__ in RULES and session.is_modified(obj, include_collections=False):
            rows += rows_for(session, obj, False)
    if rows:
        session.connection().execute(NotesHistory.__table__.insert(), rows)


# ── One-time copy of the notes saved before Notes History (Data Fixes card) ──
def _mapped_classes():
    from database import Base
    return {m.class_.__name__: m.class_ for m in Base.registry.mappers}


def _backfill(session, apply: bool):
    """Runs on a sync Session. Copies every tracked note's CURRENT value
    (backfilled = true, written_at = the record's own updated / created
    time). Only runs while no backfilled row exists, so it can't double up."""
    from sqlalchemy import exists, or_, select
    if session.execute(select(exists().where(NotesHistory.backfilled.is_(True)))).scalar():
        return {"done_before": True, "rows": 0}
    classes = _mapped_classes()
    total, batch = 0, []

    def flush_batch():
        nonlocal batch
        if apply and batch:
            session.connection().execute(NotesHistory.__table__.insert(), batch)
        batch = []

    targets = [(name, list(rule[3])) for name, rule in RULES.items()]
    targets[0][1].extend(["fqc_final_notes", "fqc_failure_reason"])          # Device: Final QC Fail
    targets.append(("AuditLog", None))
    for name, fields in targets:
        cls = classes.get(name)
        if cls is None:
            continue
        if name == "AuditLog":
            q = select(cls).where(cls.action == "PRE_SALE_IQC_COMPLETED")
        else:
            cols = [getattr(cls, f) for f in fields if hasattr(cls, f)]
            if not cols:
                continue
            q = select(cls).where(or_(*[c.isnot(None) for c in cols]))
        for obj in session.execute(q.execution_options(yield_per=1000)).scalars():
            when = (getattr(obj, "timestamp", None) or getattr(obj, "updated_at", None)
                    or getattr(obj, "created_at", None))
            for r in rows_for(session, obj, True):
                r["backfilled"] = True
                if when:
                    r["written_at"] = when
                if r["written_by"] == (current_user_name.get() or "system"):
                    r["written_by"] = getattr(obj, "created_by", None) or getattr(obj, "username", None) or "—"
                batch.append(r)
                total += 1
            if len(batch) >= 2000:
                flush_batch()
        flush_batch()
        session.expunge_all()
    return {"done_before": False, "rows": total}


async def backfill_preview(db):
    return await db.run_sync(lambda s: _backfill(s, False))


async def backfill(db):
    return await db.run_sync(lambda s: _backfill(s, True))


async def backfill_done(db):
    from sqlalchemy import exists, select
    return bool((await db.execute(select(exists().where(NotesHistory.backfilled.is_(True))))).scalar())
