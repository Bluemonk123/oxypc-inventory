"""Entity Movement — card counts of devices per Entity (Deshwal / OxyPC
Computers / Renew Circuits) plus a bulk "Change Entities" tool: paste/upload
a list of Tag Numbers and reassign them to a different Entity in one go,
recorded as a Tag Number Movement (SOLD TO clears GRN, SEND TO keeps it)."""
import os
import re
import uuid
from templates_config import templates
from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from database import get_db
from utils.timezone import app_now
from models.user import User
from models.device import Device, DeviceStage, StageMovement, TagNumberMovement, MovementDirection
from models.master import EXTERNAL_PARTNER_TEST_ENTITY
from utils.master_data import entity_values
from auth.dependencies import get_current_user, require_module_perm, verify_csrf
from services.audit_engine import audit

router = APIRouter(prefix="/entity-movement", tags=["entity_movement"], dependencies=[Depends(verify_csrf)])
allowed = require_module_perm("entity_movement")

UPLOAD_DIR = "uploads/entity_movement_invoices"


# Tag Numbers never contain spaces, so any whitespace (incl. the TAB an Excel
# row paste puts between cells), comma or semicolon separates them. Quotes
# from a CSV cell and invisible BOM / zero-width characters are stripped —
# each of these used to make a real tag come back "not found" (2026-10-17).
_TAG_SPLIT = re.compile(r"[\s,;]+")
_INVISIBLE = dict.fromkeys(map(ord, "\ufeff\u200b\u200c\u200d\u00a0"), " ")


def _parse_tags(raw: str) -> list[str]:
    seen = set()
    out = []
    for t in _TAG_SPLIT.split((raw or "").translate(_INVISIBLE)):
        t = t.strip().strip("\"'")
        if t and t.upper() not in seen:  # same tag typed twice in different case = one tag
            seen.add(t.upper())
            out.append(t)
    return out


@router.get("", response_class=HTMLResponse)
async def entity_movement_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    # External Partner test-data entity is excluded from this per-entity
    # breakdown by default, same convention as dashboard/devices/stock — it
    # stays active (Bulk Upload IQC validation depends on that) but is never
    # shown mixed in with real entities here.
    entity_list = [e for e in await entity_values(db) if e != EXTERNAL_PARTNER_TEST_ENTITY]
    counts = {}
    for e in entity_list:
        counts[e] = (await db.execute(
            select(func.count()).select_from(Device).where(
                Device.entity == e, Device.is_active == True, Device.is_trashed == False,  # noqa: E712
            )
        )).scalar() or 0
    unassigned = (await db.execute(
        select(func.count()).select_from(Device).where(
            Device.entity.is_(None), Device.is_active == True, Device.is_trashed == False,  # noqa: E712
        )
    )).scalar() or 0

    movements = (await db.execute(
        select(TagNumberMovement).order_by(desc(TagNumberMovement.moved_at)).limit(200)
    )).scalars().all()

    return templates.TemplateResponse("entity_movement/index.html", {
        "request": request, "current_user": current_user,
        "entity_values": entity_list, "counts": counts, "unassigned": unassigned,
        "movements": movements,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/change-entities")
async def change_entities(
    request: Request,
    tags: str = Form(""),
    target: str = Form(...),
    invoice_number: str = Form(""),
    invoice_pdf: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    barcodes = _parse_tags(tags)
    if not barcodes:
        return RedirectResponse(url="/entity-movement?error=No+Tag+Numbers+provided", status_code=302)

    try:
        direction_str, to_entity = target.split(":", 1)
        direction = MovementDirection(direction_str)
    except (ValueError, KeyError):
        return RedirectResponse(url="/entity-movement?error=Invalid+SOLD+TO+/+SEND+TO+selection", status_code=302)

    # Case-insensitive (2026-10-17): an exact-case IN() reported tags typed
    # with a different letter case (e.g. "...l1840" for "...L1840") as "not
    # found" although All Inventory shows them. Where duplicate-case rows
    # exist, pick one per typed tag the same way the tag lookups do: live
    # (not trashed, active) first, then exact case, then most recently updated.
    candidates = (await db.execute(
        select(Device).where(func.upper(Device.barcode).in_([b.upper() for b in barcodes]))
    )).scalars().all()
    by_upper: dict[str, list[Device]] = {}
    for d in candidates:
        by_upper.setdefault((d.barcode or "").upper(), []).append(d)
    devices, missing, in_trash = [], [], []
    for b in barcodes:
        rows = by_upper.get(b.upper())
        if not rows:
            missing.append(b)
            continue
        rows.sort(key=lambda d: (bool(d.is_trashed), not d.is_active, d.barcode != b,
                                 -(d.updated_at.timestamp() if d.updated_at else 0)))
        best = rows[0]
        if best.is_trashed:  # only a Trashed copy exists — restore it first, don't move it silently
            in_trash.append(b)
            continue
        devices.append(best)

    from_entities = {d.entity for d in devices if d.entity}
    from_entity = next(iter(from_entities), None)
    notes = "mixed source entities" if len(from_entities) > 1 else None
    invoice_number = invoice_number.strip()

    # SEND TO: entity changes, GRN stays, stage -> Stock In.
    # SOLD TO: entity changes, GRN cleared, stage -> IQC (re-inspection required
    # before it can move forward again under the new entity).
    target_stage = DeviceStage.iqc if direction == MovementDirection.sold else DeviceStage.stock_in
    for d in devices:
        prev_stage = d.current_stage
        d.entity = to_entity
        if invoice_number:
            d.invoice_number = invoice_number
        if direction == MovementDirection.sold:
            d.grn_number = None
        d.current_stage = target_stage
        d.updated_at = app_now()
        if prev_stage != target_stage:
            db.add(StageMovement(
                device_id=d.id, from_stage=prev_stage, to_stage=target_stage,
                moved_by=current_user.username,
                notes=f"Entity Movement — {direction.value.upper()} to {to_entity}",
            ))

    invoice_pdf_path = None
    if invoice_pdf is not None and invoice_pdf.filename:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        ext = os.path.splitext(invoice_pdf.filename)[1] or ".pdf"
        fname = f"{uuid.uuid4().hex}{ext}"
        invoice_pdf_path = os.path.join(UPLOAD_DIR, fname)
        content = await invoice_pdf.read()
        with open(invoice_pdf_path, "wb") as f:
            f.write(content)

    db.add(TagNumberMovement(
        tag_count=len(devices), from_entity=from_entity, to_entity=to_entity,
        direction=direction, by_user_id=getattr(current_user, "id", None),
        by_user_name=current_user.username, invoice_pdf_path=invoice_pdf_path, notes=notes,
    ))

    await audit(db, user=current_user, action="ENTITY_MOVEMENT_BULK_CHANGE",
                table_name="devices", record_id=None,
                new_value={"entity": to_entity, "direction": direction.value,
                           "tags": len(devices), "missing": len(missing), "in_trash": len(in_trash),
                           "invoice_number": invoice_number or None},
                request=request)
    await db.commit()

    msg = f"Entity changed to {to_entity} ({direction.value}) for {len(devices)} tag(s)"
    if missing:
        msg += f" — {len(missing)} tag(s) not found: {', '.join(missing[:10])}"
        if len(missing) > 10:
            msg += "…"
    if in_trash:
        msg += f" — {len(in_trash)} tag(s) skipped, in Trash (restore first): {', '.join(in_trash[:10])}"
        if len(in_trash) > 10:
            msg += "…"
    return RedirectResponse(url=f"/entity-movement?success={msg}", status_code=302)
