"""Part Estimation — cost the spare parts a lot needs before production starts.

Lots Summary lists every lot with its tag count and how many Required=Yes part
rows its IQC data produces. Generate Estimate opens a costing modal (quantity
per part comes from IQC, unit cost is entered by the user); Download & Attach
Estimate writes an Excel workbook, attaches it to the lot row, and raises a
PartSourcingRequest so the request lands on Part Master -> Sourcing Requests.
"""
import io
import os
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from templates_config import templates
from utils.timezone import app_now
from models.user import User
from models.device import Device, DeviceStage
from models.lot import Lot
from models.iqc_inspection import IQCInspection
from models.part_estimate import (
    PartEstimate, PartEstimateLine, PartEstimateChecklistLine, PartEstimateOpenLine,
)
from models.part_request import PartSourcingRequest
from models.pna_part import DevicePNAPart
from auth.dependencies import require_module_perm, verify_csrf
from services.audit_engine import audit
from services.part_estimation import (
    device_counts_by_lot, required_counts_by_lot, required_parts_for_lot, PART_LABELS,
)
from services.part_estimate_matrix import (
    PART_GROUPS, group_meta, lot_model_matrix, lot_model_counts, quantities_for,
)
import services.part_estimate_checklist as checklist_svc
from routers.iqc import _EXPORT_DEVICE_FIELDS, _EXPORT_INSPECTION_FIELDS
from utils.grades import grade_options

router = APIRouter(prefix="/part-estimation", tags=["part_estimation"],
                   dependencies=[Depends(verify_csrf)])
allowed = require_module_perm("part_estimation")

UPLOAD_DIR = "uploads/part_estimates"


def _as_uuid(val):
    try:
        return uuid.UUID(str(val))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, "Invalid id")


def _money(val) -> Decimal:
    try:
        d = Decimal(str(val or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return d.quantize(Decimal("0.01"))


@router.get("", response_class=HTMLResponse)
async def part_estimation_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    lots = (await db.execute(
        select(Lot).where(Lot.is_trashed == False).order_by(desc(Lot.purchase_date))  # noqa: E712
    )).scalars().all()

    tag_counts = await device_counts_by_lot(db)
    parts_counts = await required_counts_by_lot(db)

    # Every attached estimate per lot, newest first — a lot can be estimated
    # more than once (re-priced, or one Generate click across several grades),
    # and the Download column now offers all of them, not just the latest.
    estimates = (await db.execute(
        select(PartEstimate)
        .where(PartEstimate.file_name.isnot(None))
        .order_by(desc(PartEstimate.created_at), desc(PartEstimate.id))
    )).scalars().all()
    estimates_by_lot = {}
    for e in estimates:
        estimates_by_lot.setdefault(e.lot_id, []).append(e)

    rows = [{
        "lot": lot,
        "total_products": tag_counts.get(lot.id, 0),
        "total_parts_required": parts_counts.get(lot.id, 0),
        "estimates": estimates_by_lot.get(lot.id, []),
    } for lot in lots]

    # Same scope as Production Manager's PNA "Summary" export
    # (/trc-production/pna-summary) — distinct tags, not part rows, so a tag
    # with 3 PNA parts still counts once, matching "Total Tags in PNA".
    pna_tag_count = (await db.execute(
        select(func.count(func.distinct(Device.id)))
        .join(DevicePNAPart, DevicePNAPart.device_id == Device.id)
        .where(
            Device.current_stage.in_([DeviceStage.l1, DeviceStage.l2, DeviceStage.l3]),
            Device.is_active == True, Device.is_trashed == False,
            DevicePNAPart.is_active == True,
        )
    )).scalar() or 0

    return templates.TemplateResponse("part_estimation/index.html", {
        "request": request, "current_user": current_user, "rows": rows,
        "grade_options": grade_options(),
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
        "pna_tag_count": pna_tag_count,
    })


@router.get("/{lot_id}/iqc-data.xlsx")
async def lot_iqc_data_xlsx(
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """IQC Data column — every device ever placed in this lot (any stage, not
    just IQC), with its full IQCInspection record where one exists. Streamed
    fresh on every click, same column set as IQC's own /iqc/export-csv so the
    two exports never drift apart, just in .xlsx instead of .csv."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    lid = _as_uuid(lot_id)
    lot = (await db.execute(select(Lot).where(Lot.id == lid))).scalar_one_or_none()
    if not lot:
        raise HTTPException(404, "Lot not found")

    rows = (await db.execute(
        select(Device, IQCInspection)
        .outerjoin(IQCInspection, IQCInspection.device_id == Device.id)
        .where(Device.lot_id == lid)
        .order_by(Device.created_at.desc())
    )).all()

    def _cell(v):
        if v is None:
            return ""
        v = getattr(v, "value", v)
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d %H:%M") if getattr(v, "hour", None) is not None \
                else v.strftime("%Y-%m-%d")
        return v

    wb = Workbook()
    ws = wb.active
    ws.title = "IQC Data"
    headers = (["lot_number", "stage"] + _EXPORT_DEVICE_FIELDS
               + ["created_at", "updated_at"] + _EXPORT_INSPECTION_FIELDS)
    head_fill = PatternFill("solid", fgColor="1F3864")
    head_font = Font(bold=True, color="FFFFFF")
    for col, title in enumerate(headers, start=1):
        c = ws.cell(row=1, column=col, value=title)
        c.fill, c.font = head_fill, head_font
    for r, (device, insp) in enumerate(rows, start=2):
        values = (
            [lot.lot_number or "", _cell(device.current_stage)]
            + [_cell(getattr(device, f, None)) for f in _EXPORT_DEVICE_FIELDS]
            + [_cell(device.created_at), _cell(device.updated_at)]
            + [_cell(getattr(insp, f, None)) if insp else "" for f in _EXPORT_INSPECTION_FIELDS]
        )
        for col, val in enumerate(values, start=1):
            ws.cell(row=r, column=col, value=val)
    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 20

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    safe_lot = "".join(ch for ch in (lot.lot_number or "lot") if ch.isalnum() or ch in "-_")
    filename = f"IQC_Data_{safe_lot}_{app_now().strftime('%Y%m%d-%H%M')}.xlsx"
    return Response(
        content=buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/{lot_id}/parts")
async def lot_parts(
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Modal payload: one row per required part, plus the unit costs from this
    lot's previous estimate so a re-estimate starts from what was priced last."""
    lid = _as_uuid(lot_id)
    lot = (await db.execute(select(Lot).where(Lot.id == lid))).scalar_one_or_none()
    if not lot:
        raise HTTPException(404, "Lot not found")

    # The modal lists every Parts Consumption row, including the ones no tag in
    # this lot needs, so the operator can see the full checklist rather than
    # wonder whether a missing part was overlooked. Zero rows carry a 0 and are
    # dropped when the estimate is saved.
    parts = await required_parts_for_lot(db, lid, include_zero=True)

    prev = (await db.execute(
        select(PartEstimate).where(PartEstimate.lot_id == lid)
        .order_by(desc(PartEstimate.created_at)).limit(1)
    )).scalar_one_or_none()
    prev_costs, prev_labour, prev_notes = {}, "0", ""
    if prev:
        prev_lines = (await db.execute(
            select(PartEstimateLine).where(PartEstimateLine.estimate_id == prev.id)
        )).scalars().all()
        prev_costs = {l.part_name: str(l.unit_cost) for l in prev_lines}
        prev_labour = str(prev.labour_cost or 0)
        prev_notes = prev.notes or ""

    for p in parts:
        p["unit_cost"] = prev_costs.get(p["part_name"], "")

    return JSONResponse({
        "ok": True, "lot_number": lot.lot_number, "parts": parts,
        "labour_cost": prev_labour, "notes": prev_notes,
    })


@router.get("/{lot_id}/model-matrix")
async def lot_model_matrix_json(
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Payload for the Part Estimate modal: the lot's Model Summary rows, each
    with a status breakdown per part, plus the last prices entered for this lot
    so a re-estimate starts where the previous one finished."""
    lid = _as_uuid(lot_id)
    lot = (await db.execute(select(Lot).where(Lot.id == lid))).scalar_one_or_none()
    if not lot:
        raise HTTPException(404, "Lot not found")

    models = await lot_model_matrix(db, lid)

    prev = (await db.execute(
        select(PartEstimate).where(PartEstimate.lot_id == lid)
        .order_by(desc(PartEstimate.created_at), desc(PartEstimate.id)).limit(1)
    )).scalar_one_or_none()
    prev_costs, prev_labour, prev_notes = {}, "0", ""
    if prev:
        prev_lines = (await db.execute(
            select(PartEstimateLine).where(PartEstimateLine.estimate_id == prev.id)
        )).scalars().all()
        # Lines from the per-model estimate are stored as "<model> — <part>";
        # keying on the whole label lets a re-estimate re-fill the exact cell,
        # and lines from a plain Generate Estimate simply never match. When a
        # batch wrote several grade files, they are byte-identical in content
        # (see generate_model_estimate), so it does not matter which one of
        # them "prev" resolves to.
        prev_costs = {l.part_name: str(l.unit_cost) for l in prev_lines}
        prev_labour = str(prev.labour_cost or 0)
        prev_notes = prev.notes or ""

    return JSONResponse({
        "ok": True, "lot_number": lot.lot_number, "groups": group_meta(),
        "models": models, "prev_costs": prev_costs,
        "labour_cost": prev_labour, "notes": prev_notes,
        "grade_options": grade_options(),
    })


def _line_label(model: str, part: str) -> str:
    """PartEstimateLine.part_name is 150 chars; a long model name must not
    cost us the part name at the end of it."""
    label = f"{model} — {part}"
    if len(label) <= 150:
        return label
    return f"{model[:150 - len(part) - 6]}… — {part}"


def _build_model_workbook(*, lot_number, generated_on, generated_by, grade, blocks,
                          unit_price_total, parts_total, labour_cost,
                          total_estimation, notes) -> bytes:
    """Workbook for the per-model estimate: Sheet 1 is the detailed breakdown
    (one block per model, one section per group — what the operator priced on
    screen); Sheet 2 is a compact roll-up, one row per model. Rows with
    Parts Quantity 0 are left out of both sheets — a status band that needs
    nothing costs nothing and would only pad the workbook.

    `grade` is stamped in the header of both sheets: when the operator picks
    more than one grade, every other field in this workbook is identical
    across the batch — grade is the one thing that tells the files apart.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Part Estimate"

    head_fill = PatternFill("solid", fgColor="1F3864")
    model_fill = PatternFill("solid", fgColor="DCE6F1")
    group_fill = PatternFill("solid", fgColor="F2F2F2")
    head_font = Font(bold=True, color="FFFFFF")
    bold = Font(bold=True)
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    money = '#,##0.00'

    def _header(sheet, title):
        sheet["A1"] = title
        sheet["A1"].font = Font(bold=True, size=14)
        sheet["A2"] = f"Lot Number: {lot_number}"
        sheet["A2"].font = bold
        sheet["A3"] = f"Grade: {grade or '—'}"
        sheet["A3"].font = bold
        sheet["A4"] = f"Generated On: {generated_on}"
        sheet["A5"] = f"Generated By: {generated_by}"

    _header(ws, "Part Estimate")

    headers = ["Part", "Note", "Parts Quantity", "Unit Part Price", "Total Part Value"]
    r = 7
    for col, title in enumerate(headers, start=1):
        c = ws.cell(row=r, column=col, value=title)
        c.fill, c.font, c.border = head_fill, head_font, border
        c.alignment = Alignment(horizontal="center")

    for block in blocks:
        # A model whose every group is costing 0 (nothing ticked, or every
        # group left on "Select") contributes nothing to either sheet.
        if not block["qty"]:
            continue
        r += 1
        c = ws.cell(row=r, column=1,
                    value=f"{block['model']} ({block['make']} · {block['device_type']})")
        c.font = bold
        ws.cell(row=r, column=2, value=f"Model Count: {block['model_count']}")
        ws.cell(row=r, column=3, value=block["qty"]).font = bold
        ws.cell(row=r, column=4, value=float(block["unit_price_sum"])).number_format = money
        cv = ws.cell(row=r, column=5, value=float(block["total"]))
        cv.font, cv.number_format = bold, money
        for col in range(1, 6):
            ws.cell(row=r, column=col).fill = model_fill
            ws.cell(row=r, column=col).border = border

        for grp in block["groups"]:
            lines = [l for l in grp["lines"] if l["qty"]]
            if not lines:
                continue
            r += 1
            g = ws.cell(row=r, column=1, value=f"    {grp['heading']}")
            g.font = bold
            ws.cell(row=r, column=2, value=f"{grp['filter_label']}: {grp['status'] or 'Select'}")
            for col in range(1, 6):
                ws.cell(row=r, column=col).fill = group_fill
                ws.cell(row=r, column=col).border = border
            for line in lines:
                r += 1
                ws.cell(row=r, column=1, value=f"        {line['part']}").border = border
                ws.cell(row=r, column=2, value=line["note"] or "").border = border
                ws.cell(row=r, column=3, value=line["qty"]).border = border
                c4 = ws.cell(row=r, column=4, value=float(line["unit_cost"]))
                c4.border, c4.number_format = border, money
                c5 = ws.cell(row=r, column=5, value=float(line["total_cost"]))
                c5.border, c5.number_format = border, money

    r += 2
    for label, value, fmt in (
        ("Total Unit Price", float(unit_price_total), money),
        ("Total Parts Price", float(parts_total), money),
        ("Total Labour Cost", float(labour_cost), money),
        ("Total Estimate Cost", float(total_estimation), money),
    ):
        ws.cell(row=r, column=1, value=label).font = bold
        c = ws.cell(row=r, column=5, value=value)
        c.font, c.number_format = bold, fmt
        r += 1

    if notes:
        r += 1
        ws.cell(row=r, column=1, value="Notes").font = bold
        nc = ws.cell(row=r, column=2, value=notes)
        nc.alignment = Alignment(wrap_text=True, vertical="top")

    for col, width in (("A", 40), ("B", 34), ("C", 16), ("D", 18), ("E", 20)):
        ws.column_dimensions[col].width = width

    # ── Sheet 2 — Model Summary: one row per priced model ─────────────────────
    ms = wb.create_sheet("Model Summary")
    _header(ms, "Model Summary")
    ms_headers = ["Model", "Make", "Device Type", "Model Count",
                  "Parts Quantity", "Unit Price", "Total Parts Price"]
    r2 = 7
    for col, title in enumerate(ms_headers, start=1):
        c = ms.cell(row=r2, column=col, value=title)
        c.fill, c.font, c.border = head_fill, head_font, border
        c.alignment = Alignment(horizontal="center")
    total_model_qty = 0
    for block in blocks:
        if not block["qty"]:
            continue
        r2 += 1
        total_model_qty += block["qty"]
        ms.cell(row=r2, column=1, value=block["model"]).border = border
        ms.cell(row=r2, column=2, value=block["make"]).border = border
        ms.cell(row=r2, column=3, value=block["device_type"]).border = border
        ms.cell(row=r2, column=4, value=block["model_count"]).border = border
        ms.cell(row=r2, column=5, value=block["qty"]).border = border
        c6 = ms.cell(row=r2, column=6, value=float(block["unit_price_sum"]))
        c6.border, c6.number_format = border, money
        c7 = ms.cell(row=r2, column=7, value=float(block["total"]))
        c7.border, c7.number_format = border, money
    r2 += 1
    ms.cell(row=r2, column=1, value="Total").font = bold
    ms.cell(row=r2, column=5, value=total_model_qty).font = bold
    tc = ms.cell(row=r2, column=7, value=float(parts_total))
    tc.font, tc.number_format = bold, money
    for col in range(1, 8):
        ms.cell(row=r2, column=col).border = border
    for col, width in (("A", 30), ("B", 18), ("C", 16), ("D", 14), ("E", 16), ("F", 16), ("G", 18)):
        ms.column_dimensions[col].width = width

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@router.post("/{lot_id}/generate-model-estimate")
async def generate_model_estimate(
    lot_id: str,
    request: Request,
    payload: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Part Estimate -> Generate Estimate.

    payload = {
      included: [model_key, ...],
      statuses: {model_key: {group: [status, ...]}},  # multi-select per group
      prices:   {model_key: {"<group>|<part>": {note, unit_cost}}},
      grade:    grade_value,
      labour_cost, notes
    }

    Quantities are recomputed here from IQC; the browser only gets to choose
    the status bands, the notes and the prices. Same rule as the plain
    Generate Estimate — a tampered or stale quantity can never reach the
    workbook or the sourcing request.

    One call writes one workbook for one grade. The modal stays open after a
    successful call specifically so the operator can change the grade (and
    re-price whatever that grade needs differently) and call this again —
    each call is its own independent PartEstimate + its own
    PartSourcingRequest, exactly like the plain (non-model) Generate Estimate
    already works. A lot ends up with as many files as the operator chose to
    make, in as many separate visits to this endpoint as that took.
    """
    import json

    lid = _as_uuid(lot_id)
    lot = (await db.execute(select(Lot).where(Lot.id == lid))).scalar_one_or_none()
    if not lot:
        raise HTTPException(404, "Lot not found")

    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Malformed estimate payload"}, status_code=400)

    matrix = await lot_model_matrix(db, lid)
    if not matrix:
        return JSONResponse({"ok": False, "error": "This lot has no tags to estimate"},
                            status_code=400)

    included = set(data.get("included") or [])
    statuses = data.get("statuses") or {}
    prices = data.get("prices") or {}
    grade = (data.get("grade") or "").strip()
    if not grade:
        return JSONResponse({"ok": False, "error": "Select a Grade"}, status_code=400)
    grade_labels = dict(grade_options())
    if grade not in grade_labels:
        return JSONResponse({"ok": False, "error": f"Unknown grade: {grade}"}, status_code=400)
    grade_label = grade_labels[grade]

    qtys = quantities_for(matrix, statuses)

    blocks, lines = [], []
    unit_price_total = Decimal("0")
    parts_total = Decimal("0")
    total_qty = 0

    for row in matrix:
        if row["key"] not in included:
            continue
        model_prices = prices.get(row["key"]) or {}
        block = {"model": row["model"], "make": row["make"],
                 "device_type": row["device_type"], "model_count": row["qty"],
                 "qty": 0, "unit_price_sum": Decimal("0"), "total": Decimal("0"),
                 "groups": []}

        for g, heading, filter_label, group_statuses, parts in PART_GROUPS:
            picked = [s for s in ((statuses.get(row["key"]) or {}).get(g) or [])
                     if s in group_statuses]
            grp = {"heading": heading, "filter_label": filter_label,
                   "status": ", ".join(picked) or None, "lines": []}
            for name, _fn in parts:
                cell = model_prices.get(f"{g}|{name}") or {}
                qty = qtys[row["key"]][g][name]
                unit = _money(cell.get("unit_cost", 0))
                total = _money(unit * qty)
                note = (str(cell.get("note") or "")).strip()[:255] or None
                grp["lines"].append({"part": name, "note": note, "qty": qty,
                                     "unit_cost": unit, "total_cost": total})
                block["qty"] += qty
                block["unit_price_sum"] += unit
                block["total"] += total
                unit_price_total += unit
                parts_total += total
                total_qty += qty
                if qty:
                    lines.append({"part_name": _line_label(row["model"], name),
                                  "qty": qty, "unit_cost": unit, "total_cost": total})
            block["groups"].append(grp)
        blocks.append(block)

    priced_blocks = [b for b in blocks if b["qty"]]
    if not blocks:
        return JSONResponse({"ok": False, "error": "Select at least one model to estimate"},
                            status_code=400)
    if not priced_blocks:
        return JSONResponse(
            {"ok": False, "error": "Every selected group is costing 0 parts — "
                                   "choose a part status before generating"},
            status_code=400)

    labour = _money(data.get("labour_cost"))
    total_estimation = _money(parts_total + labour)
    notes = (data.get("notes") or "").strip() or None

    total_products = (await device_counts_by_lot(db)).get(lid, 0)
    stamp = app_now()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_lot = "".join(ch for ch in (lot.lot_number or "lot") if ch.isalnum() or ch in "-_")

    xlsx = _build_model_workbook(
        lot_number=lot.lot_number, generated_on=stamp.strftime("%d %b %Y %H:%M"),
        generated_by=current_user.username, grade=grade_label, blocks=blocks,
        unit_price_total=unit_price_total, parts_total=parts_total,
        labour_cost=labour, total_estimation=total_estimation, notes=notes,
    )
    safe_grade = "".join(ch for ch in grade_label if ch.isalnum() or ch in "-_") or "grade"
    stored = f"{uuid.uuid4().hex}.xlsx"
    download_name = f"Part_Estimate_{safe_lot}_{safe_grade}_{stamp.strftime('%Y%m%d-%H%M')}.xlsx"
    with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
        fh.write(xlsx)

    est = PartEstimate(
        lot_id=lid, lot_number=lot.lot_number, total_products=total_products,
        total_qty=total_qty, parts_total_cost=parts_total, labour_cost=labour,
        total_estimation=total_estimation, notes=notes, grade=grade,
        file_path=stored, file_name=download_name,
        created_by=current_user.username, created_at=stamp,
    )
    db.add(est)
    await db.flush()
    for line in lines:
        db.add(PartEstimateLine(estimate_id=est.id, **line))

    db.add(PartSourcingRequest(
        source="production", part_code=None,
        part_name=f"Request for {lot.lot_number}",
        qty_requested=total_qty, raised_by=current_user.username,
        lot_id=lid, lot_number=lot.lot_number, estimate_id=est.id,
        created_at=stamp,
    ))

    await audit(db, user=current_user, action="PART_ESTIMATE_GENERATED",
                table_name="part_estimates", record_id=str(est.id),
                new_value={"lot": lot.lot_number, "mode": "per-model", "grade": grade,
                           "models": len(priced_blocks), "total_qty": total_qty,
                           "total_estimation": str(total_estimation)},
                request=request)
    await db.commit()

    return JSONResponse({"ok": True, "estimate_id": str(est.id),
                         "download_url": f"/part-estimation/download/{est.id}"})


@router.get("/{lot_id}/checklist-matrix")
async def lot_checklist_matrix_json(
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Payload for the Create Estimate modal: every Model in the lot with its
    full field-by-field defect-count breakdown (No/Yes/Minor/Major/Faulty),
    plus the last prices entered for this lot's checklist estimate."""
    lid = _as_uuid(lot_id)
    lot = (await db.execute(select(Lot).where(Lot.id == lid))).scalar_one_or_none()
    if not lot:
        raise HTTPException(404, "Lot not found")

    models = await checklist_svc.lot_checklist_matrix(db, lid)

    prev = (await db.execute(
        select(PartEstimate).where(PartEstimate.lot_id == lid, PartEstimate.estimate_type == "checklist")
        .order_by(desc(PartEstimate.created_at), desc(PartEstimate.id)).limit(1)
    )).scalar_one_or_none()
    prev_prices, prev_labour = {}, "0"
    if prev:
        prev_lines = (await db.execute(
            select(PartEstimateChecklistLine).where(PartEstimateChecklistLine.estimate_id == prev.id)
        )).scalars().all()
        prev_prices = {f"{l.model_name}||{l.field_group}|{l.field_name}": str(l.unit_price) for l in prev_lines}
        prev_labour = str(prev.labour_cost or 0)

    return JSONResponse({
        "ok": True, "lot_number": lot.lot_number, "groups": checklist_svc.group_meta(),
        "models": models, "prev_prices": prev_prices, "labour_cost": prev_labour,
        "grade_options": grade_options(),
    })


def _build_checklist_workbook(*, lot_number, generated_on, generated_by, grade, groups,
                              model_keys, model_labels, cells, row_totals, col_totals, grand_total,
                              labour_cost, total_estimation, severity_label, total_parts_count) -> bytes:
    """One sheet: Field Group heading rows, then one row per field with
    Count/Price per model plus a Row Total column, then a column-totals row,
    Total Part Price/Labour/Grade, and Total Estimate — matching the attached
    Part-Estimate-structure.xlsx template layout."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Create Estimate"

    head_fill = PatternFill("solid", fgColor="1F3864")
    group_fill = PatternFill("solid", fgColor="F2F2F2")
    total_fill = PatternFill("solid", fgColor="DCE6F1")
    head_font = Font(bold=True, color="FFFFFF")
    bold = Font(bold=True)
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    money = '#,##0.00'

    ws["A1"] = "Create Estimate"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Lot Number: {lot_number}"
    ws["A2"].font = bold
    ws["A3"] = f"Grade: {grade or '—'}"
    ws["A3"].font = bold
    ws["A4"] = f"Filters: {severity_label}"
    ws["A4"].alignment = Alignment(wrap_text=True)
    ws["A5"] = f"Generated On: {generated_on}"
    ws["A6"] = f"Generated By: {generated_by}"

    n_models = len(model_labels)
    header_row = 8
    ws.cell(row=header_row, column=1, value="Field").fill = head_fill
    ws.cell(row=header_row, column=1).font = head_font
    ws.cell(row=header_row, column=1).border = border
    for m_idx, model in enumerate(model_labels):
        col = 2 + m_idx * 2
        ws.merge_cells(start_row=header_row, start_column=col, end_row=header_row, end_column=col + 1)
        c = ws.cell(row=header_row, column=col, value=model)
        c.fill, c.font, c.border = head_fill, head_font, border
        c.alignment = Alignment(horizontal="center")
        ws.cell(row=header_row, column=col + 1).fill = head_fill
        ws.cell(row=header_row, column=col + 1).border = border
        sub_row = header_row + 1
        for j, label in enumerate(("Count", "Price")):
            sc = ws.cell(row=sub_row, column=col + j, value=label)
            sc.font, sc.border = bold, border
            sc.alignment = Alignment(horizontal="center")
    total_col = 2 + n_models * 2
    ws.merge_cells(start_row=header_row, start_column=total_col, end_row=header_row + 1, end_column=total_col)
    tc = ws.cell(row=header_row, column=total_col, value="Row Total")
    tc.fill, tc.font, tc.border = head_fill, head_font, border
    tc.alignment = Alignment(horizontal="center", vertical="center")

    r = header_row + 2
    for g_key, heading, fields in groups:
        rows_in_group = [name for name, _domain, _fn in fields if row_totals.get((g_key, name)) is not None]
        if not rows_in_group:
            continue
        ws.cell(row=r, column=1, value=heading).font = bold
        for col in range(1, total_col + 1):
            ws.cell(row=r, column=col).fill = group_fill
            ws.cell(row=r, column=col).border = border
        r += 1
        for name in rows_in_group:
            ws.cell(row=r, column=1, value=f"    {name}").border = border
            for m_idx, mkey in enumerate(model_keys):
                col = 2 + m_idx * 2
                cell = cells.get((g_key, name, mkey), {"count": 0, "price": 0, "total": 0})
                ws.cell(row=r, column=col, value=cell["count"]).border = border
                pc = ws.cell(row=r, column=col + 1, value=float(cell["price"]))
                pc.border, pc.number_format = border, money
            rt = ws.cell(row=r, column=total_col, value=float(row_totals[(g_key, name)]))
            rt.font, rt.number_format, rt.border = bold, money, border
            r += 1

    ws.cell(row=r, column=1, value="Column Total").font = bold
    for col in range(1, total_col + 1):
        ws.cell(row=r, column=col).fill = total_fill
        ws.cell(row=r, column=col).border = border
    for m_idx in range(n_models):
        col = 2 + m_idx * 2
        cv = ws.cell(row=r, column=col + 1, value=col_totals[m_idx])
        cv.font, cv.number_format = bold, money
    gt = ws.cell(row=r, column=total_col, value=float(grand_total))
    gt.font, gt.number_format = bold, money
    r += 2

    ws.cell(row=r, column=1, value="Total Part Price").font = bold
    tp = ws.cell(row=r, column=2, value=float(grand_total))
    tp.font, tp.number_format = bold, money
    ws.cell(row=r, column=3, value="Total Parts Count").font = bold
    ws.cell(row=r, column=4, value=total_parts_count).font = bold
    ws.cell(row=r, column=5, value="Total Labour Cost").font = bold
    lc = ws.cell(row=r, column=6, value=float(labour_cost))
    lc.font, lc.number_format = bold, money
    ws.cell(row=r, column=7, value="Grade").font = bold
    ws.cell(row=r, column=8, value=grade or "—").font = bold
    r += 1
    ws.cell(row=r, column=5, value="Total Estimate").font = bold
    te = ws.cell(row=r, column=6, value=float(total_estimation))
    te.font, te.number_format = bold, money

    ws.column_dimensions["A"].width = 26
    for m_idx in range(n_models):
        col = 2 + m_idx * 2
        ws.column_dimensions[get_column_letter(col)].width = 10
        ws.column_dimensions[get_column_letter(col + 1)].width = 12
    ws.column_dimensions[get_column_letter(total_col)].width = 14

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@router.post("/{lot_id}/generate-checklist-estimate")
async def generate_checklist_estimate(
    lot_id: str,
    request: Request,
    payload: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Create Estimate -> Generate.

    payload = {
      filters: {"critical": {"yes","no"}, "hardware": {"yes","no"},
                "cosmetic": {"yes","no","minor","major"}},  # bool per key
      prices: {"<model_key>||<group>|<field>": unit_price},
      grade: grade_value,
      labour_cost
    }

    Counts are always re-derived here from IQC/Device data — the browser only
    supplies the filter selections, the prices, and which grade to label the
    file with. Selecting more than one grade means the browser calls this
    endpoint once per grade (same pattern as generate-model-estimate); each
    call is its own independent PartEstimate + PartSourcingRequest.
    """
    import json

    lid = _as_uuid(lot_id)
    lot = (await db.execute(select(Lot).where(Lot.id == lid))).scalar_one_or_none()
    if not lot:
        raise HTTPException(404, "Lot not found")

    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Malformed estimate payload"}, status_code=400)

    matrix = await checklist_svc.lot_checklist_matrix(db, lid)
    if not matrix:
        return JSONResponse({"ok": False, "error": "This lot has no tags to estimate"}, status_code=400)

    raw_filters = data.get("filters") or {}

    def _scope_filter(scope, keys, defaults):
        picked = raw_filters.get(scope) or {}
        return {k: bool(picked.get(k, defaults[k])) for k in keys}

    filters = {
        "critical": _scope_filter("critical", ("yes", "no"), {"yes": True, "no": False}),
        "hardware": _scope_filter("hardware", ("yes", "no"), {"yes": True, "no": False}),
        "cosmetic": _scope_filter("cosmetic", ("yes", "no", "minor", "major"),
                                  {"yes": True, "no": False, "minor": True, "major": True}),
    }

    def _short_label(domain, scope_filters):
        if domain == "sev":
            picked = [c for c, k in (("Mi", "minor"), ("Ma", "major")) if scope_filters.get(k)]
        else:
            picked = [c for c, k in (("Y", "yes"), ("N", "no")) if scope_filters.get(k)]
        return "+".join(picked) or "None"

    def _readable(scope, keys):
        f = filters[scope]
        picked = [k.capitalize() for k in keys if f.get(k)]
        return ", ".join(picked) or "None"

    filters_summary = (
        f"Critical Part Added: {_readable('critical', ('yes', 'no'))} | "
        f"Hardware Damage: {_readable('hardware', ('yes', 'no'))} | "
        f"Cosmetic Damage: {_readable('cosmetic', ('yes', 'no', 'minor', 'major'))}"
    )

    prices = data.get("prices") or {}
    grade = (data.get("grade") or "").strip()
    if not grade:
        return JSONResponse({"ok": False, "error": "Select a Grade"}, status_code=400)
    grade_labels = dict(grade_options())
    if grade not in grade_labels:
        return JSONResponse({"ok": False, "error": f"Unknown grade: {grade}"}, status_code=400)
    grade_label = grade_labels[grade]

    qtys = checklist_svc.quantities_for(matrix, filters)
    # Keyed by the lot's unique model_key, not the display Model name — two
    # different Makes can legitimately share the same Model text within one
    # lot, and keying by name alone would silently collide their prices.
    model_keys = [row["key"] for row in matrix]
    model_labels = [row["model"] for row in matrix]

    cells, row_totals, checklist_lines = {}, {}, []
    col_totals = [0.0] * len(matrix)
    grand_total = Decimal("0")
    total_parts_count = 0   # sum of count, only where a price was actually entered

    for g_key, heading, fields in checklist_svc.CHECKLIST_GROUPS:
        scope = checklist_svc.GROUP_SCOPE[g_key]
        for name, domain, _fn in fields:
            row_total = Decimal("0")
            row_has_qty = False
            line_label = _short_label(domain, filters[scope])
            for m_idx, row in enumerate(matrix):
                mkey, model = row["key"], row["model"]
                count = qtys[mkey][g_key][name]
                price = _money(prices.get(f"{mkey}||{g_key}|{name}", 0))
                total = _money(price * count)
                cells[(g_key, name, mkey)] = {"count": count, "price": price, "total": total}
                col_totals[m_idx] += float(total)
                row_total += total
                grand_total += total
                if price:
                    total_parts_count += count
                if count:
                    row_has_qty = True
                if count or price:
                    checklist_lines.append(PartEstimateChecklistLine(
                        field_group=heading, field_name=name, model_name=model,
                        severity_scope=line_label, count=count,
                        unit_price=price, line_total=total,
                    ))
            if row_has_qty:
                row_totals[(g_key, name)] = row_total

    if not row_totals:
        return JSONResponse(
            {"ok": False, "error": "No fields in this lot have a defect to price — nothing to generate"},
            status_code=400)

    labour = _money(data.get("labour_cost"))
    total_estimation = _money(grand_total + labour)
    total_products = (await device_counts_by_lot(db)).get(lid, 0)
    # "Total Parts Count" — parts actually being priced/sourced, not every
    # defect on screen: a field with no price entered isn't being sourced yet.
    total_qty = total_parts_count
    stamp = app_now()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_lot = "".join(ch for ch in (lot.lot_number or "lot") if ch.isalnum() or ch in "-_")

    xlsx = _build_checklist_workbook(
        lot_number=lot.lot_number, generated_on=stamp.strftime("%d %b %Y %H:%M"),
        generated_by=current_user.username, grade=grade_label,
        groups=checklist_svc.CHECKLIST_GROUPS, model_keys=model_keys, model_labels=model_labels, cells=cells,
        row_totals=row_totals, col_totals=col_totals, grand_total=grand_total,
        labour_cost=labour, total_estimation=total_estimation, severity_label=filters_summary,
        total_parts_count=total_parts_count,
    )
    safe_grade = "".join(ch for ch in grade_label if ch.isalnum() or ch in "-_") or "grade"
    stored = f"{uuid.uuid4().hex}.xlsx"
    download_name = f"Create_Estimate_{safe_lot}_{safe_grade}_{stamp.strftime('%Y%m%d-%H%M')}.xlsx"
    with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
        fh.write(xlsx)

    est = PartEstimate(
        lot_id=lid, lot_number=lot.lot_number, total_products=total_products,
        total_qty=total_qty, parts_total_cost=grand_total, labour_cost=labour,
        total_estimation=total_estimation, grade=grade, estimate_type="checklist",
        notes=filters_summary, file_path=stored, file_name=download_name,
        created_by=current_user.username, created_at=stamp,
    )
    db.add(est)
    await db.flush()
    for line in checklist_lines:
        line.estimate_id = est.id
        db.add(line)

    db.add(PartSourcingRequest(
        source="production", part_code=None,
        part_name=f"Request for {lot.lot_number}",
        qty_requested=total_qty, raised_by=current_user.username,
        lot_id=lid, lot_number=lot.lot_number, estimate_id=est.id,
        created_at=stamp,
    ))

    await audit(db, user=current_user, action="PART_ESTIMATE_GENERATED",
                table_name="part_estimates", record_id=str(est.id),
                new_value={"lot": lot.lot_number, "mode": "checklist", "grade": grade,
                           "filters": filters_summary, "total_parts_count": total_qty,
                           "total_estimation": str(total_estimation)},
                request=request)
    await db.commit()

    return JSONResponse({"ok": True, "estimate_id": str(est.id),
                         "download_url": f"/part-estimation/download/{est.id}"})


@router.get("/{lot_id}/open-matrix")
async def lot_open_matrix_json(
    lot_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Payload for the Open Estimate modal: every part name (rows) x every
    Model in the lot (columns, with its tag count) plus, if this lot was
    open-estimated before, the cells and per-model Repair Charge that were
    entered last time."""
    lid = _as_uuid(lot_id)
    lot = (await db.execute(select(Lot).where(Lot.id == lid))).scalar_one_or_none()
    if not lot:
        raise HTTPException(404, "Lot not found")

    models = await lot_model_counts(db, lid)

    prev = (await db.execute(
        select(PartEstimate).where(PartEstimate.lot_id == lid, PartEstimate.estimate_type == "open")
        .order_by(desc(PartEstimate.created_at), desc(PartEstimate.id)).limit(1)
    )).scalar_one_or_none()
    prev_cells, prev_charges = {}, {}
    if prev:
        prev_lines = (await db.execute(
            select(PartEstimateOpenLine).where(PartEstimateOpenLine.estimate_id == prev.id)
        )).scalars().all()
        for l in prev_lines:
            prev_cells[f"{l.part_name}||{l.model_key}"] = str(l.qty)
            prev_charges[l.model_key] = str(l.repair_charge)

    return JSONResponse({
        "ok": True, "lot_number": lot.lot_number, "parts": PART_LABELS,
        "models": models, "prev_cells": prev_cells, "prev_charges": prev_charges,
        "grade_options": grade_options(),
    })


def _build_open_workbook(*, lot_number, generated_on, generated_by, grade, parts,
                         models, cells, charges, total_parts, model_estimates,
                         total_estimate) -> bytes:
    """One sheet: Part rows x Model columns (with each model's tag count in
    the header), a Repair Charge row under the grid, then Total Parts / Total
    Estimate Value. Cells are the operator's free entry, not IQC-derived, so
    every filled cell is written as-is."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Open Estimate"

    head_fill = PatternFill("solid", fgColor="1F3864")
    charge_fill = PatternFill("solid", fgColor="F2F2F2")
    total_fill = PatternFill("solid", fgColor="DCE6F1")
    head_font = Font(bold=True, color="FFFFFF")
    bold = Font(bold=True)
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    money = '#,##0.00'

    ws["A1"] = "Open Estimate"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Lot Number: {lot_number}"
    ws["A2"].font = bold
    ws["A3"] = f"Grade: {grade or '—'}"
    ws["A3"].font = bold
    ws["A4"] = f"Generated On: {generated_on}"
    ws["A5"] = f"Generated By: {generated_by}"

    header_row = 7
    ws.cell(row=header_row, column=1, value="Part").fill = head_fill
    ws.cell(row=header_row, column=1).font = head_font
    ws.cell(row=header_row, column=1).border = border
    for col, m in enumerate(models, start=2):
        c = ws.cell(row=header_row, column=col, value=f"{m['model']} ({m['qty']})")
        c.fill, c.font, c.border = head_fill, head_font, border
        c.alignment = Alignment(horizontal="center", wrap_text=True)

    r = header_row
    for part in parts:
        r += 1
        ws.cell(row=r, column=1, value=part).border = border
        for col, m in enumerate(models, start=2):
            val = cells.get((part, m["key"]))
            c = ws.cell(row=r, column=col, value=float(val) if val is not None else None)
            c.border = border
            if val is not None:
                c.alignment = Alignment(horizontal="center")

    r += 1
    ws.cell(row=r, column=1, value="Repair Charge").font = bold
    for col in range(1, len(models) + 2):
        ws.cell(row=r, column=col).fill = charge_fill
        ws.cell(row=r, column=col).border = border
    for col, m in enumerate(models, start=2):
        charge = charges.get(m["key"])
        if charge is not None:
            cc = ws.cell(row=r, column=col, value=float(charge))
            cc.number_format = money

    r += 1
    ws.cell(row=r, column=1, value="Estimate Value").font = bold
    for col in range(1, len(models) + 2):
        ws.cell(row=r, column=col).fill = charge_fill
        ws.cell(row=r, column=col).border = border
    for col, m in enumerate(models, start=2):
        val = model_estimates.get(m["key"])
        if val is not None:
            ec = ws.cell(row=r, column=col, value=float(val))
            ec.number_format = money

    r += 2
    for label, value, fmt in (
        ("Total Parts", total_parts, "#,##0.##"),
        ("Total Estimate Value", float(total_estimate), money),
    ):
        ws.cell(row=r, column=1, value=label).font = bold
        c = ws.cell(row=r, column=2, value=value)
        c.font, c.number_format = bold, fmt
        for col in range(1, 3):
            ws.cell(row=r, column=col).fill = total_fill
            ws.cell(row=r, column=col).border = border
        r += 1

    ws.column_dimensions["A"].width = 26
    for col in range(2, len(models) + 2):
        ws.column_dimensions[ws.cell(row=header_row, column=col).column_letter].width = 16

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@router.post("/{lot_id}/generate-open-estimate")
async def generate_open_estimate(
    lot_id: str,
    request: Request,
    payload: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Open Estimate -> Generate.

    payload = {
      cells:   {"<part>||<model_key>": qty, ...},   # only filled cells
      charges: {model_key: repair_charge, ...},      # one per model column
      grade:   grade_value
    }

    Cell values are the operator's own entry — nothing here is derived from
    IQC — but the Model Count multiplier behind Total Parts / Total Estimate
    Value is always recomputed from the lot's current tags, never taken from
    the browser, so that number can't be inflated by a tampered payload.
    Same one-call-per-grade pattern as the other three estimate types.
    """
    import json

    lid = _as_uuid(lot_id)
    lot = (await db.execute(select(Lot).where(Lot.id == lid))).scalar_one_or_none()
    if not lot:
        raise HTTPException(404, "Lot not found")

    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Malformed estimate payload"}, status_code=400)

    models = await lot_model_counts(db, lid)
    if not models:
        return JSONResponse({"ok": False, "error": "This lot has no tags to estimate"}, status_code=400)
    model_by_key = {m["key"]: m for m in models}
    parts_set = set(PART_LABELS)

    grade = (data.get("grade") or "").strip()
    if not grade:
        return JSONResponse({"ok": False, "error": "Select a Grade"}, status_code=400)
    grade_labels = dict(grade_options())
    if grade not in grade_labels:
        return JSONResponse({"ok": False, "error": f"Unknown grade: {grade}"}, status_code=400)
    grade_label = grade_labels[grade]

    raw_cells = data.get("cells") or {}
    raw_charges = data.get("charges") or {}

    cells, filled_models, open_lines = {}, set(), []
    total_parts = Decimal("0")
    for raw_key, raw_val in raw_cells.items():
        part, _, mkey = str(raw_key).partition("||")
        if part not in parts_set or mkey not in model_by_key:
            continue
        qty = _money(raw_val)
        if qty <= 0:
            continue
        m = model_by_key[mkey]
        cells[(part, mkey)] = qty
        filled_models.add(mkey)
        total_parts += qty * m["qty"]

    if not cells:
        return JSONResponse({"ok": False, "error": "No cells were filled in — nothing to generate"},
                            status_code=400)

    charges = {}
    for mkey, raw_val in raw_charges.items():
        if mkey not in model_by_key:
            continue
        charge = _money(raw_val)
        if charge > 0:
            charges[mkey] = charge

    model_estimates = {}
    total_estimate = Decimal("0")
    for mkey in filled_models:
        charge = charges.get(mkey)
        if not charge:
            continue
        m = model_by_key[mkey]
        value = _money(charge * m["qty"])
        model_estimates[mkey] = value
        total_estimate += value

    for (part, mkey), qty in cells.items():
        m = model_by_key[mkey]
        open_lines.append(PartEstimateOpenLine(
            part_name=part, model_key=mkey, model_name=m["model"], model_count=m["qty"],
            qty=qty, repair_charge=charges.get(mkey, Decimal("0")),
        ))

    total_products = (await device_counts_by_lot(db)).get(lid, 0)
    stamp = app_now()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_lot = "".join(ch for ch in (lot.lot_number or "lot") if ch.isalnum() or ch in "-_")

    total_qty_int = int(total_parts.to_integral_value(rounding="ROUND_HALF_UP"))

    xlsx = _build_open_workbook(
        lot_number=lot.lot_number, generated_on=stamp.strftime("%d %b %Y %H:%M"),
        generated_by=current_user.username, grade=grade_label, parts=PART_LABELS,
        models=[m for m in models if m["key"] in filled_models], cells=cells, charges=charges,
        total_parts=float(total_parts), model_estimates=model_estimates,
        total_estimate=total_estimate,
    )
    safe_grade = "".join(ch for ch in grade_label if ch.isalnum() or ch in "-_") or "grade"
    stored = f"{uuid.uuid4().hex}.xlsx"
    download_name = f"Open_Estimate_{safe_lot}_{safe_grade}_{stamp.strftime('%Y%m%d-%H%M')}.xlsx"
    with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
        fh.write(xlsx)

    est = PartEstimate(
        lot_id=lid, lot_number=lot.lot_number, total_products=total_products,
        total_qty=total_qty_int,
        parts_total_cost=total_estimate, labour_cost=Decimal("0"),
        total_estimation=total_estimate, grade=grade, estimate_type="open",
        file_path=stored, file_name=download_name,
        created_by=current_user.username, created_at=stamp,
    )
    db.add(est)
    await db.flush()
    for line in open_lines:
        line.estimate_id = est.id
        db.add(line)

    db.add(PartSourcingRequest(
        source="production", part_code=None,
        part_name=f"Request for {lot.lot_number}",
        qty_requested=est.total_qty, raised_by=current_user.username,
        lot_id=lid, lot_number=lot.lot_number, estimate_id=est.id,
        created_at=stamp,
    ))

    await audit(db, user=current_user, action="PART_ESTIMATE_GENERATED",
                table_name="part_estimates", record_id=str(est.id),
                new_value={"lot": lot.lot_number, "mode": "open", "grade": grade,
                           "models": len(filled_models), "total_parts": str(total_parts),
                           "total_estimation": str(total_estimate)},
                request=request)
    await db.commit()

    return JSONResponse({"ok": True, "estimate_id": str(est.id),
                         "download_url": f"/part-estimation/download/{est.id}"})


def _build_workbook(*, lot_number, generated_on, generated_by, lines,
                    total_qty, parts_total, labour_cost, total_estimation, notes) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Part Estimate"

    head_fill = PatternFill("solid", fgColor="1F3864")
    head_font = Font(bold=True, color="FFFFFF")
    bold = Font(bold=True)
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    money = '#,##0.00'

    ws["A1"] = "Part Estimation"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Lot Number: {lot_number}"
    ws["A2"].font = bold
    ws["A3"] = f"Generated On: {generated_on}"
    ws["A4"] = f"Generated By: {generated_by}"

    header_row = 6
    for col, title in enumerate(["Part Name", "Part Quantity", "Part Unit Cost", "Part Total Cost"], start=1):
        c = ws.cell(row=header_row, column=col, value=title)
        c.fill, c.font, c.border = head_fill, head_font, border
        c.alignment = Alignment(horizontal="center")

    r = header_row
    for line in lines:
        r += 1
        ws.cell(row=r, column=1, value=line["part_name"]).border = border
        ws.cell(row=r, column=2, value=line["qty"]).border = border
        c3 = ws.cell(row=r, column=3, value=float(line["unit_cost"]))
        c3.border, c3.number_format = border, money
        c4 = ws.cell(row=r, column=4, value=float(line["total_cost"]))
        c4.border, c4.number_format = border, money

    r += 1
    ws.cell(row=r, column=1, value="Total").font = bold
    ws.cell(row=r, column=2, value=total_qty).font = bold
    tc = ws.cell(row=r, column=4, value=float(parts_total))
    tc.font, tc.number_format = bold, money
    for col in range(1, 5):
        ws.cell(row=r, column=col).border = border

    r += 2
    ws.cell(row=r, column=1, value="Labour Cost").font = bold
    lc = ws.cell(row=r, column=4, value=float(labour_cost))
    lc.number_format = money
    r += 1
    ws.cell(row=r, column=1, value="Total Estimation").font = bold
    te = ws.cell(row=r, column=4, value=float(total_estimation))
    te.font, te.number_format = bold, money

    if notes:
        r += 2
        ws.cell(row=r, column=1, value="Notes").font = bold
        nc = ws.cell(row=r, column=2, value=notes)
        nc.alignment = Alignment(wrap_text=True, vertical="top")

    for col, width in (("A", 28), ("B", 16), ("C", 18), ("D", 18)):
        ws.column_dimensions[col].width = width

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@router.post("/{lot_id}/generate")
async def generate_estimate(
    lot_id: str,
    request: Request,
    payload: str = Form(...),          # JSON: {parts:[{part_name,qty,unit_cost}], labour_cost, notes}
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Download & Attach Estimate — writes the workbook, stores the estimate,
    and raises the production sourcing request in one transaction."""
    import json

    lid = _as_uuid(lot_id)
    lot = (await db.execute(select(Lot).where(Lot.id == lid))).scalar_one_or_none()
    if not lot:
        raise HTTPException(404, "Lot not found")

    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Malformed estimate payload"}, status_code=400)

    # Quantities are re-derived from IQC, never taken from the browser — only
    # the unit costs, labour and notes are user input.
    truth = {p["part_name"]: p["qty"] for p in await required_parts_for_lot(db, lid)}
    if not truth:
        return JSONResponse({"ok": False, "error": "No parts are required for this lot"}, status_code=400)

    submitted_costs = {str(p.get("part_name")): p.get("unit_cost") for p in (data.get("parts") or [])}

    lines, total_qty, parts_total = [], 0, Decimal("0")
    for part_name, qty in truth.items():
        unit = _money(submitted_costs.get(part_name, 0))
        total = _money(unit * qty)
        lines.append({"part_name": part_name, "qty": qty, "unit_cost": unit, "total_cost": total})
        total_qty += qty
        parts_total += total

    labour = _money(data.get("labour_cost"))
    total_estimation = _money(parts_total + labour)
    notes = (data.get("notes") or "").strip() or None

    total_products = (await device_counts_by_lot(db)).get(lid, 0)
    stamp = app_now()

    xlsx = _build_workbook(
        lot_number=lot.lot_number, generated_on=stamp.strftime("%d %b %Y %H:%M"),
        generated_by=current_user.username, lines=lines, total_qty=total_qty,
        parts_total=parts_total, labour_cost=labour,
        total_estimation=total_estimation, notes=notes,
    )

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_lot = "".join(ch for ch in (lot.lot_number or "lot") if ch.isalnum() or ch in "-_")
    stored = f"{uuid.uuid4().hex}.xlsx"
    download_name = f"Part_Estimate_{safe_lot}_{stamp.strftime('%Y%m%d-%H%M')}.xlsx"
    with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
        fh.write(xlsx)

    est = PartEstimate(
        lot_id=lid, lot_number=lot.lot_number, total_products=total_products,
        total_qty=total_qty, parts_total_cost=parts_total, labour_cost=labour,
        total_estimation=total_estimation, notes=notes,
        file_path=stored, file_name=download_name,
        created_by=current_user.username, created_at=stamp,
    )
    db.add(est)
    await db.flush()

    for line in lines:
        db.add(PartEstimateLine(estimate_id=est.id, **line))

    db.add(PartSourcingRequest(
        source="production", part_code=None,
        part_name=f"Request for {lot.lot_number}",
        qty_requested=total_qty, raised_by=current_user.username,
        lot_id=lid, lot_number=lot.lot_number, estimate_id=est.id,
        created_at=stamp,
    ))

    await audit(db, user=current_user, action="PART_ESTIMATE_GENERATED",
                table_name="part_estimates", record_id=str(est.id),
                new_value={"lot": lot.lot_number, "total_qty": total_qty,
                           "total_estimation": str(total_estimation)},
                request=request)
    await db.commit()

    return JSONResponse({"ok": True, "estimate_id": str(est.id),
                         "download_url": f"/part-estimation/download/{est.id}"})


@router.post("/{estimate_id}/delete-file")
async def delete_estimate_file(
    estimate_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Detach the estimate file and withdraw its Production sourcing request.

    The Download column goes back to empty and the request disappears from
    Part Master -> Sourcing Requests, which is what "Delete File" means to the
    user. Two things are deliberately kept:

    * The workbook itself stays under uploads/part_estimates. Removing the row
      that points at it is reversible; deleting the file is not, and nothing
      else on the page depends on the bytes being gone.
    * The PartEstimate row and its lines stay, so re-running Generate Estimate
      still pre-fills the unit costs and labour that were entered last time.

    Only the attachment fields are cleared, so file_name being empty is what
    hides the button.
    """
    est = (await db.execute(
        select(PartEstimate).where(PartEstimate.id == _as_uuid(estimate_id))
    )).scalar_one_or_none()
    if not est:
        return JSONResponse({"ok": False, "error": "Estimate not found"}, status_code=404)
    if not est.file_name:
        return JSONResponse({"ok": False, "error": "No file is attached to this estimate"},
                            status_code=409)

    srs = (await db.execute(
        select(PartSourcingRequest).where(PartSourcingRequest.estimate_id == est.id)
    )).scalars().all()

    # Snapshot enough to put this back by hand. file_path matters as much as
    # file_name: the stored name is a random hex, so without it the workbook
    # on disk cannot be matched back to the estimate. Same for the sourcing
    # request's own fields — logging only its id makes it unrecoverable.
    undo = {
        "file_name": est.file_name,
        "file_path": est.file_path,
        "sourcing_requests": [{
            "id": str(s.id), "part_name": s.part_name,
            "qty_requested": s.qty_requested, "qty_sourced": s.qty_sourced,
            "raised_by": s.raised_by, "status": s.status,
            "source_deal_id": s.source_deal_id,
            "confirmed": s.confirmed,
            "confirmed_by": s.confirmed_by,
            "confirmed_at": s.confirmed_at.isoformat() if s.confirmed_at else None,
            "closed_by": s.closed_by,
            "closed_at": s.closed_at.isoformat() if s.closed_at else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        } for s in srs],
    }
    removed_file = est.file_name

    for sr in srs:
        await db.delete(sr)
    est.file_path = None
    est.file_name = None

    await audit(db, user=current_user, action="PART_ESTIMATE_FILE_REMOVED",
                table_name="part_estimates", record_id=str(est.id),
                old_value=undo,
                new_value={"lot_number": est.lot_number},
                request=request)
    await db.commit()

    msg = f"Estimate file removed for {est.lot_number}"
    if srs:
        msg += f" and {len(srs)} sourcing request(s) withdrawn"
    return JSONResponse({"ok": True, "message": msg})


@router.get("/download/{estimate_id}")
async def download_estimate(
    estimate_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    from fastapi.responses import FileResponse

    est = (await db.execute(
        select(PartEstimate).where(PartEstimate.id == _as_uuid(estimate_id))
    )).scalar_one_or_none()
    if not est or not est.file_path:
        raise HTTPException(404, "Estimate file not found")
    path = os.path.join(UPLOAD_DIR, est.file_path)
    if not os.path.exists(path):
        raise HTTPException(404, "Estimate file is no longer on disk")
    return FileResponse(
        path, filename=est.file_name or "part_estimate.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
