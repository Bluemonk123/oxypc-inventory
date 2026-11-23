"""NEW / SOLD IQC copies (2026-11-22, Pankaj). See models/iqc_snapshot.py.

Hooked on the ORM flush, so every code path is covered with no change to it:
  - IQC form, bulk upload, OxyQC / API, partner upload, external return all
    create IQCInspection rows -> NEW copy;
  - every Sale row (New Sale, Tag Sale, Multi-Sell, Customise → Sold, dealer
    orders, ...) -> SOLD copy of the tag's IQC at that moment.

NEW is created only while the tag is in intake (GRN / IQC) or is itself being
created in this flush — an old tag that gets an empty IQC record later (device
edit page) gets no misleading NEW. While the tag stays in intake its NEW copy
follows IQC edits (multi-step entry is captured whole); once it moves on, NEW
is frozen. The repair flow keeps writing only the live IQC (TRC IQC).
"""
import datetime as _dt
import decimal
import enum
import uuid

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from models.device import Device, DeviceStage
from models.iqc_inspection import IQCInspection
from models.iqc_snapshot import IQCSnapshot
from models.sales import Sale
from services.repair_location import current_user_name
from utils.timezone import app_now

_INTAKE = {DeviceStage.grn, DeviceStage.iqc}
# Device columns that are bookkeeping, not the tag's IQC / spec data.
_DEVICE_SKIP = {"id", "lot_id", "location_id", "created_at", "updated_at", "current_stage"}
_IQC_SKIP = {"id", "device_id"}


def _js(v):
    """A JSON-safe value."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, enum.Enum):
        return v.value
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    return str(v)


def _row(obj, skip):
    return {c.key: _js(getattr(obj, c.key, None)) for c in obj.__table__.columns if c.key not in skip}


def _iqc_payload(device, insp):
    inspection = _row(insp, _IQC_SKIP) if insp is not None else {}
    if insp is not None and not inspection.get("inspected_at"):
        inspection["inspected_at"] = _js(app_now())   # the column default is only set on insert
    return {"device": _row(device, _DEVICE_SKIP), "inspection": inspection}


def _context(session, device):
    """GRN / Lot / Sub-lot / Invoice / Location at this moment."""
    from models.lot import Lot
    from models.location import StorageLocation
    ctx = {"stage": _js(device.current_stage),
           "grn_number": getattr(device, "grn_number", None),
           "invoice_number": getattr(device, "invoice_number", None),
           "sub_lot_number": getattr(device, "sub_lot_number", None)}
    if device.lot_id:
        lot = session.get(Lot, device.lot_id)
        if lot is not None:
            ctx.update({"lot_number": lot.lot_number, "supplier": lot.supplier_name or lot.vendor_name,
                        "lot_grn_number": lot.grn_number_new or lot.grn_system_number,
                        "grn_date": _js(lot.grn_date), "lot_invoice_no": lot.invoice_no,
                        "invoice_date": _js(lot.invoice_date), "purchase_date": _js(lot.purchase_date)})
    if device.location_id:
        loc = session.get(StorageLocation, device.location_id)
        if loc is not None:
            ctx.update({"location": loc.unit_id, "location_zone": _js(loc.zone)})
    return ctx


def _inspection_for(session, device_id, pending):
    if device_id in pending:
        return pending[device_id]
    return session.execute(select(IQCInspection).where(IQCInspection.device_id == device_id)).scalars().first()


@event.listens_for(Session, "before_flush")
def _take_iqc_copies(session, flush_context, instances):
    pending_insp = {o.device_id: o for o in list(session.new) + list(session.dirty)
                    if isinstance(o, IQCInspection) and o.device_id}
    new_devices = {o.id for o in session.new if isinstance(o, Device)}
    new_sales = [o for o in session.new if isinstance(o, Sale) and o.device_id]
    intake_dirty = {o.id for o in session.dirty if isinstance(o, Device) and o.current_stage in _INTAKE}
    if not (pending_insp or new_sales or intake_dirty):
        return
    who = (current_user_name.get() or "system")[:100]
    with session.no_autoflush:
        # ── NEW ──
        for did in set(pending_insp) | intake_dirty:
            device = next((o for o in list(session.new) + list(session.dirty)
                           if isinstance(o, Device) and o.id == did), None) or session.get(Device, did)
            if device is None:
                continue
            in_intake = device.current_stage in _INTAKE
            snap = session.execute(select(IQCSnapshot).where(
                IQCSnapshot.device_id == did, IQCSnapshot.kind == "NEW")).scalars().first()
            if snap is None and (in_intake or did in new_devices):
                insp = _inspection_for(session, did, pending_insp)
                if insp is None:
                    continue
                session.add(IQCSnapshot(device_id=did, device=device, kind="NEW", taken_by=who, source="IQC",
                                        iqc=_iqc_payload(device, insp), context=_context(session, device)))
            elif snap is not None and in_intake:
                insp = _inspection_for(session, did, pending_insp)
                snap.iqc = _iqc_payload(device, insp)
                snap.context = _context(session, device)
        # ── SOLD ──
        for sale in new_sales:
            if sale.id is None:
                sale.id = uuid.uuid4()
            device = next((o for o in list(session.new) + list(session.dirty)
                           if isinstance(o, Device) and o.id == sale.device_id), None) \
                or session.get(Device, sale.device_id)
            if device is None:
                continue
            insp = _inspection_for(session, sale.device_id, pending_insp)
            session.add(IQCSnapshot(device_id=sale.device_id, device=device, kind="SOLD", sale_id=sale.id, sale=sale,
                                    taken_by=who, source=("Sale " + (sale.sale_number or ""))[:40],
                                    iqc=_iqc_payload(device, insp), context=_context(session, device)))
