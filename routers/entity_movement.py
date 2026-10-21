"""Entity Movement — card counts of devices per Entity (Deshwal / OxyPC
Computers / Renew Circuits) plus the bulk "Change Entities" tool.

Since 2026-10-21 (Flow 2 — ReGRN) Change Entities moves the submitted Tag
Numbers onto a selected GRN: each tag takes that GRN's grn_number, invoice
number and entity. Tags in Sold go back to Ready to Sale (the old sale record
is kept as history); every other stage is left as it is. Only the submitted
tags move — the rest of their lot keeps its original GRN. Replaces the old
SOLD TO / SEND TO rules."""
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
from models.grn_import import GRNImport
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

    # GRNs to ReGRN onto (Flow 2) — newest first, deleted ones hidden.
    grns = (await db.execute(
        select(GRNImport).where(GRNImport.is_deleted.isnot(True))
        .order_by(GRNImport.created_at.desc()).limit(1000)
    )).scalars().all()

    return templates.TemplateResponse("entity_movement/index.html", {
        "request": request, "current_user": current_user,
        "entity_values": entity_list, "counts": counts, "unassigned": unassigned,
        "movements": movements, "grns": grns,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/change-entities")
async def change_entities(
    request: Request,
    tags: str = Form(""),
    grn_id: str = Form(""),
    invoice_pdf: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Flow 2 — ReGRN (2026-10-21). See the module docstring for the rules."""
    from urllib.parse import quote
    barcodes = _parse_tags(tags)
    if not barcodes:
        return RedirectResponse(url="/entity-movement?error=No+Tag+Numbers+provided", status_code=302)
    try:
        gid = uuid.UUID((grn_id or "").strip())
    except ValueError:
        return RedirectResponse(url="/entity-movement?error=Select+a+GRN", status_code=302)
    g = (await db.execute(
        select(GRNImport).where(GRNImport.id == gid, GRNImport.is_deleted.isnot(True))
    )).scalar_one_or_none()
    if g is None:
        return RedirectResponse(url="/entity-movement?error=GRN+not+found", status_code=302)
    if not g.entity:
        return RedirectResponse(
            url="/entity-movement?error=" + quote(
                f"GRN {g.grn_number} has no Entity — set it with Edit GRN first."),
            status_code=302)

    # Case-insensitive (2026-10-17): an exact-case IN() reported tags typed
    # with a different letter case (e.g. "...l1840" for "...L1840") as "not
    # found" although All Inventory shows them. Where duplicate-case rows
    # exist, pick one per typed tag the same way the tag lookups do: live
    # (not trashed, active) first, then exact case, then most recently updated.
    candidates = []
    upper = [b.upper() for b in barcodes]
    for i in range(0, len(upper), 5000):
        candidates.extend((await db.execute(
            select(Device).where(func.upper(Device.barcode).in_(upper[i:i + 5000]))
        )).scalars().all())
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
    now = app_now()
    back_to_rts = 0
    for d in devices:
        old = {"grn_number": d.grn_number, "invoice_number": d.invoice_number,
               "entity": d.entity, "stage": d.current_stage.value if d.current_stage else None}
        d.grn_number = g.grn_number
        d.invoice_number = g.invoice_number
        d.entity = g.entity
        if d.current_stage == DeviceStage.sold:
            # Sold -> Ready to Sale under the new entity. The Sale row is NOT
            # touched: it stays as history; the tag simply leaves sold counts
            # and can be sold again.
            prev = (await db.execute(
                select(StageMovement).where(
                    StageMovement.device_id == d.id, StageMovement.to_stage == DeviceStage.sold,
                    StageMovement.exited_at.is_(None),
                ).order_by(StageMovement.moved_at.desc())
            )).scalars().first()
            if prev:
                prev.exited_at = now
            db.add(StageMovement(
                device_id=d.id, from_stage=DeviceStage.sold, to_stage=DeviceStage.ready_to_sale,
                moved_by=current_user.username,
                notes=f"ReGRN to GRN {g.grn_number} ({g.entity}) — back to Ready to Sale"))
            d.current_stage = DeviceStage.ready_to_sale
            back_to_rts += 1
        d.updated_at = now
        new = {"grn_number": d.grn_number, "invoice_number": d.invoice_number,
               "entity": d.entity, "stage": d.current_stage.value if d.current_stage else None}
        # One audit row per tag, written before the commit.
        await audit(db, user=current_user, action="REGRN_TAG",
                    table_name="devices", record_id=str(d.id),
                    old_value={"barcode": d.barcode, **old},
                    new_value={"barcode": d.barcode, **new},
                    request=request)

    invoice_pdf_path = None
    if invoice_pdf is not None and invoice_pdf.filename:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        ext = os.path.splitext(invoice_pdf.filename)[1] or ".pdf"
        fname = f"{uuid.uuid4().hex}{ext}"
        invoice_pdf_path = os.path.join(UPLOAD_DIR, fname)
        content = await invoice_pdf.read()
        with open(invoice_pdf_path, "wb") as f:
            f.write(content)

    if devices:
        # History row. `direction` is a required enum (send/sold); ReGRN is
        # stored as "send" and named in the notes (the history table shows a
        # ReGRN badge from the notes) — a new enum value would be a schema change.
        db.add(TagNumberMovement(
            tag_count=len(devices), from_entity=from_entity, to_entity=g.entity,
            direction=MovementDirection.send, by_user_id=getattr(current_user, "id", None),
            by_user_name=current_user.username, invoice_pdf_path=invoice_pdf_path,
            notes=(f"ReGRN — GRN {g.grn_number}"
                   + (" (mixed source entities)" if len(from_entities) > 1 else ""))[:255],
        ))
    await audit(db, user=current_user, action="REGRN_BULK",
                table_name="devices", record_id=g.grn_number,
                new_value={"grn_number": g.grn_number, "entity": g.entity,
                           "invoice_number": g.invoice_number, "tags": len(devices),
                           "sold_to_ready_to_sale": back_to_rts,
                           "missing": len(missing), "in_trash": len(in_trash)},
                request=request)
    await db.commit()

    msg = (f"{len(devices)} tag(s) moved to GRN {g.grn_number} ({g.entity})"
           + (f" — {back_to_rts} were Sold and are now Ready to Sale" if back_to_rts else ""))
    if missing:
        msg += f" — {len(missing)} tag(s) not found: {', '.join(missing[:10])}"
        if len(missing) > 10:
            msg += "…"
    if in_trash:
        msg += f" — {len(in_trash)} tag(s) skipped, in Trash (restore first): {', '.join(in_trash[:10])}"
        if len(in_trash) > 10:
            msg += "…"
    return RedirectResponse(url="/entity-movement?success=" + quote(msg), status_code=302)
