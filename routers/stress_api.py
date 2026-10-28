"""
Stress Test REST API
====================
POST /stress/{barcode}/run       — start stress tests on this server machine
GET  /stress/{barcode}/status    — live results poll (JSON)
POST /stress/{barcode}/stop      — stop running tests
POST /stress/{barcode}/save      — save results + generate PDF to DB
GET  /stress/{barcode}/report    — download latest PDF (or generate on-the-fly)
GET  /stress/results/{barcode}   — list all saved runs (JSON)
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import uuid as uuid_module

from fastapi import APIRouter, Depends, Form, HTTPException, Body
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse, Response
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user, verify_csrf
from database import get_db
from models.device import Device, DeviceStage, StageMovement
from models.user import User, UserRole
from models.stress import StressTestResult, STRESS_CHECKLIST_ITEMS, STRESS_OPTIONAL_ITEMS
from models.work_order import WorkOrder
from routers.transfers import _gen_work_id
from services.audit_engine import audit
from services.notifications import create_notification
import stress_runner as sr
from utils.timezone import app_now

router = APIRouter(prefix="/stress", tags=["stress"], dependencies=[Depends(verify_csrf)])

# ── Reports storage folder ────────────────────────────────────────────────────
_BASE_DIR  = Path(__file__).resolve().parent.parent
REPORTS_DIR = _BASE_DIR / "static" / "stress_reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ── Request/response schemas ──────────────────────────────────────────────────

class RunRequest(BaseModel):
    duration: str = "standard"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pdf_filename(barcode: str, run_id: int) -> str:
    return f"stress_{barcode}_{run_id}.pdf"


def _generate_pdf(record: StressTestResult) -> bytes:
    """Generate a PDF stress report.  Uses fpdf2 if available, else plain-text."""
    try:
        from fpdf import FPDF
        return _pdf_fpdf(record)
    except ImportError:
        return _pdf_text(record)


def _ascii(text) -> str:
    """fpdf2's core Helvetica font is latin-1 only — sanitize any text (static
    labels or arbitrary record data) before it reaches pdf.cell()/multi_cell()."""
    s = str(text)
    for uni, ascii_eq in (("—", "-"), ("–", "-"), ("‘", "'"), ("’", "'"),
                          ("“", '"'), ("”", '"'), ("…", "...")):
        s = s.replace(uni, ascii_eq)
    return s.encode("latin-1", "replace").decode("latin-1")


def _pdf_item_source(record: StressTestResult):
    """PDF generation supports two result_json shapes sharing this one table:
    the automated benchmark's {short_key: {status, summary, ...}} (stress_runner.py
    ALL_KEYS/TEST_NAMES), and the manual checklist's {item_name: "pass"/"fail"}
    (STRESS_CHECKLIST_ITEMS, models/stress.py). Detect which one this record
    used so old and new reports both render correctly from the same function."""
    results = record.results_json or {}
    if any(k in results for k in sr.ALL_KEYS):
        return sr.ALL_KEYS, sr.TEST_NAMES
    keys = ([k for k in STRESS_CHECKLIST_ITEMS if k in results]
            or [k for k in results.keys() if not str(k).startswith("_")])
    return keys, {k: k for k in keys}


def _result_status_summary(entry) -> tuple[str, str]:
    """Normalize one results_json entry to (status, summary) regardless of
    which of the two shapes above produced it."""
    if isinstance(entry, dict):
        return entry.get("status", "PENDING"), entry.get("summary", "—")
    if isinstance(entry, str):
        if entry.lower() == "na":
            return "N/A", "Not present on this device"
        # Summary wording (2026-10-28): "System Diagnose - Pass" (- Fail for a
        # failed item), was "Manually checked on the Stress Test page".
        return entry.upper(), f"System Diagnose - {entry.strip().title()}"
    return "PENDING", "—"


def _pdf_fpdf(record: StressTestResult) -> bytes:
    from fpdf import FPDF

    STATUS_COLORS = {
        "PASS":               (34,  139,  34),
        "FAIL":               (200,  30,  30),
        "WARN":               (200, 150,   0),
        "PASS_WITH_WARNINGS": (200, 150,   0),
        "SKIP":               (100, 100, 100),
        "IN_PROGRESS":        (60,  100, 200),
    }
    BADGE_BG = {
        "PASS":    (209, 231, 221),
        "FAIL":    (248, 215, 218),
        "WARN":    (255, 243, 205),
        "SKIP":    (226, 227, 229),
        "RUNNING": (207, 226, 255),
        "PENDING": (226, 227, 229),
        "N/A":     (226, 227, 229),
    }

    results = record.results_json or {}
    item_keys, NAMES = _pdf_item_source(record)

    # Always one page (2026-10-23): no automatic page break; the results
    # rows shrink to fit whatever space is left above the footer.
    pdf = FPDF()
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    page_h = pdf.h          # 297 mm on A4

    # ── Header band ───────────────────────────────────────────────────────────
    pdf.set_fill_color(30, 30, 30)
    pdf.rect(0, 0, 210, 22, "F")
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(255, 255, 255)
    pdf.set_y(7)
    pdf.cell(0, 8, "Stress or QC Test Report", align="C")
    pdf.set_y(28)

    # ── Device info table ─────────────────────────────────────────────────────
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Device Information", ln=True)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(2)

    info_rows = [
        ("Tag Number", record.barcode),
        ("Brand / Model", f"{record.brand or '—'} {record.model_name or ''}".strip()),
        ("Duration",      str(record.duration or "standard").capitalize()),
    ]
    for label, val in info_rows:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_fill_color(245, 245, 245)
        pdf.cell(45, 7, label, border=0, fill=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 7, _ascii(val), border=0, ln=True)

    pdf.ln(4)

    # ── Overall verdict ───────────────────────────────────────────────────────
    ov = record.overall_status or "UNKNOWN"
    ov_color = STATUS_COLORS.get(ov, (100, 100, 100))
    pdf.set_fill_color(*ov_color)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 10, _ascii(f"  Overall Result: {ov.replace('_', ' ')}"), fill=True, ln=True)
    pdf.ln(4)

    # ── Results table ─────────────────────────────────────────────────────────
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Test Results", ln=True)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(2)

    # Header row
    pdf.set_fill_color(40, 40, 40)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(25, 7, "Status",  border=0, fill=True)
    pdf.cell(48, 7, "Test",    border=0, fill=True)
    pdf.cell(0,  7, "Summary", border=0, fill=True, ln=True)

    # Row height / font from the space left (footer sits 12 mm from the bottom).
    n_rows = max(len(item_keys), 1)
    avail = page_h - 14 - 4 - pdf.get_y()
    row_h = max(3.0, min(7.0, avail / n_rows))
    fs = max(5.0, min(9.0, row_h * 1.3))

    pdf.set_text_color(0, 0, 0)
    for idx, key in enumerate(item_keys):
        st, summ = _result_status_summary(results.get(key))
        bg  = BADGE_BG.get(st, (240, 240, 240))
        name = NAMES.get(key, key)
        summ = summ[:80]

        row_bg = (250, 250, 250) if idx % 2 == 0 else (255, 255, 255)
        pdf.set_fill_color(*row_bg)

        # Status badge cell
        pdf.set_fill_color(*bg)
        pdf.set_font("Helvetica", "B", fs - 1)
        pdf.cell(25, row_h, st, border=0, fill=True, align="C")

        pdf.set_fill_color(*row_bg)
        pdf.set_font("Helvetica", "B", fs)
        pdf.cell(48, row_h, _ascii(name), border=0, fill=True)
        pdf.set_font("Helvetica", "", fs)
        pdf.cell(0, row_h, _ascii(summ), border=0, fill=True, ln=True)

    # ── Footer ────────────────────────────────────────────────────────────────
    pdf.set_y(page_h - 14)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, "Generated by OxyPC Inventory Lifecycle", align="C")

    return bytes(pdf.output())


def _pdf_text(record: StressTestResult) -> bytes:
    """Fallback: plain-text 'PDF' (actually a text file) if fpdf2 not installed."""
    results = record.results_json or {}
    lines = [
        "Stress or QC Test Report",
        "=" * 50,
        f"Tag Number  : {record.barcode}",
        f"Brand/Model : {record.brand or '—'} {record.model_name or ''}".strip(),
        f"Duration    : {record.duration}",
        f"Overall     : {record.overall_status}",
        "",
        "Test Results",
        "-" * 50,
    ]
    item_keys, names = _pdf_item_source(record)
    for key in item_keys:
        st, summ = _result_status_summary(results.get(key))
        lines.append(f"{names.get(key, key):20s}  {st:10s}  {summ}")
    return "\n".join(lines).encode("utf-8")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/{barcode}/run")
async def run_stress(
    barcode: str,
    body: RunRequest = Body(default=RunRequest()),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = sr.get_session(barcode)
    if existing and existing.running:
        raise HTTPException(409, "Tests already running for this device")

    # Fetch device info for the session
    dev = await db.execute(select(Device).where(Device.barcode == barcode))
    device = dev.scalar_one_or_none()
    brand = device.brand or "" if device else ""
    model = device.model or "" if device else ""

    sr.start_session(
        barcode=barcode,
        duration=body.duration,
        run_by=current_user.username,
        brand=brand,
        model=model,
    )
    return {"status": "started", "barcode": barcode, "duration": body.duration}


@router.get("/{barcode}/status")
async def get_status(barcode: str, current_user: User = Depends(get_current_user)):
    session = sr.get_session(barcode)
    if not session:
        return {"running": False, "overall_status": "NOT_RUN", "results": {}}
    return session.to_status_dict()


@router.post("/{barcode}/stop")
async def stop_stress(barcode: str, current_user: User = Depends(get_current_user)):
    sr.stop_session(barcode)
    return {"status": "stopping"}


@router.post("/{barcode}/save")
async def save_results(
    barcode: str,
    failed_items: str = Form(""),
    na_items: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist the current manual checklist state (without moving the device —
    that's what Fail/Complete do) and generate a downloadable PDF report.
    `failed_items` is the same comma-separated checklist-item convention as
    POST /{barcode}/fail."""
    dev = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not dev:
        raise HTTPException(404, f"Device {barcode} not found")

    try:
        record = await _record_checklist_result(
            db, dev, _parse_checklist_items(failed_items), _parse_checklist_items(na_items),
            current_user.username)

        await db.commit()
        return {
            "saved": True,
            "result_id": record.id,
            "overall_status": record.overall_status,
            "pdf_available": record.pdf_path is not None,
        }
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(500, f"Save failed: {exc}")


@router.post("/{barcode}/save-agent")
async def save_agent_results(
    barcode: str,
    payload: dict = Body(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist an ON-DEVICE stress run produced by the local OxyQC agent
    (PXE station mode). Unlike /save there is no server-side session — the
    browser forwards the agent's results JSON directly. Result shape matches
    stress_runner.TestResult.to_dict(), so PDF generation works unchanged."""
    results = payload.get("results") or {}
    if not isinstance(results, dict) or not results:
        raise HTTPException(422, "No results in payload")
    dev = (await db.execute(
        select(Device).where(Device.barcode == barcode, Device.is_active == True)
    )).scalar_one_or_none()
    if not dev:
        raise HTTPException(404, "Device not found")

    overall = str(payload.get("overall_status") or "UNKNOWN")[:30]
    try:
        record = StressTestResult(
            barcode=barcode,
            brand=payload.get("brand") or dev.brand,
            model_name=payload.get("model") or dev.model,
            run_at=datetime.now(timezone.utc),
            duration=int(payload.get("duration") or 0),
            overall_status=overall,
            results_json=results,
            run_by=f"{current_user.username} (on-device agent)",
        )
        db.add(record)
        await db.flush()
        try:
            pdf_bytes = _generate_pdf(record)
            fname = _pdf_filename(barcode, record.id)
            fpath = REPORTS_DIR / fname
            fpath.write_bytes(pdf_bytes)
            record.pdf_path = str(fpath)
        except Exception:
            record.pdf_path = None
        await audit(db, action="STRESS_AGENT_SAVED", user=current_user,
                    table_name="stress_test_results", record_id=str(record.id),
                    new_value={"barcode": barcode, "overall": overall,
                               "agent_version": payload.get("version")})
        await db.commit()
        return {"saved": True, "result_id": record.id, "overall_status": overall,
                "pdf_available": record.pdf_path is not None}
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(500, f"Save failed: {exc}")


@router.get("/{barcode}/report")
async def download_report(
    barcode: str,
    result_id: Optional[int] = None,
    inline: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """inline=1 opens the PDF in the browser (Ready to Sale's QC Report
    preview popup) instead of downloading it."""
    if result_id:
        q = select(StressTestResult).where(
            StressTestResult.id == result_id,
            StressTestResult.barcode == barcode,
        )
    else:
        q = (
            select(StressTestResult)
            .where(StressTestResult.barcode == barcode)
            .order_by(desc(StressTestResult.run_at))
            .limit(1)
        )

    res    = await db.execute(q)
    record = res.scalar_one_or_none()
    if not record:
        raise HTTPException(404, "No saved stress report found for this device")

    # Always built fresh from the saved results (2026-10-23) so every report
    # uses the current layout; a stored file is only the fallback.
    try:
        try:
            pdf_bytes = _generate_pdf(record)
        except Exception:
            if record.pdf_path and Path(record.pdf_path).exists():
                pdf_bytes = Path(record.pdf_path).read_bytes()
            else:
                raise
    except Exception as exc:
        raise HTTPException(500, f"Report generation failed: {exc}")

    filename = f"stress_report_{barcode}.pdf"
    try:
        from fpdf import FPDF  # noqa
        media_type = "application/pdf"
        cd_name    = filename
    except ImportError:
        media_type = "text/plain"
        cd_name    = filename.replace(".pdf", ".txt")

    return Response(
        content=pdf_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{cd_name}"'},
    )


@router.get("/results/{barcode}")
async def list_results(
    barcode: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        select(StressTestResult)
        .where(StressTestResult.barcode == barcode)
        .order_by(desc(StressTestResult.run_at))
        .limit(20)
    )
    records = rows.scalars().all()
    return [
        {
            "id":             r.id,
            "run_at":         r.run_at.isoformat() if r.run_at else None,
            "duration":       r.duration,
            "overall_status": r.overall_status,
            "run_by":         r.run_by,
            "has_pdf":        r.pdf_path is not None,
        }
        for r in records
    ]


async def _record_checklist_result(db: AsyncSession, device: Device, failed_list: list[str],
                                   na_list: list[str], run_by: str,
                                   overall: str | None = None) -> StressTestResult:
    """Save the manual checklist for this device and attach its PDF report.

    One report per visit to the Stress Test stage: Save / Fail / Complete in
    the same visit update the same StressTestResult (so the attached report
    always matches the latest checklist), while a tag that comes back to
    Stress Test again gets a fresh one — and since every viewer (Stress Test
    page, Final QC, Device Detail) shows the latest report, that new one
    replaces the old one everywhere. The visit is identified by the
    StageMovement that brought the tag into qc_check, stored in results_json
    under "_visit" (keys starting with "_" are metadata, never checklist items).
    """
    results = {}
    for item in STRESS_CHECKLIST_ITEMS:
        if item in STRESS_OPTIONAL_ITEMS and item in na_list:
            results[item] = "na"
        else:
            results[item] = "fail" if item in failed_list else "pass"
    overall = overall or ("FAIL" if failed_list else "PASS")

    visit = (await db.execute(
        select(StageMovement.id).where(StageMovement.device_id == device.id,
                                       StageMovement.to_stage == DeviceStage.qc_check)
        .order_by(StageMovement.moved_at.desc()).limit(1)
    )).scalar_one_or_none()
    visit_key = str(visit) if visit else "none"
    results["_visit"] = visit_key

    latest = (await db.execute(
        select(StressTestResult).where(StressTestResult.barcode == device.barcode)
        .order_by(desc(StressTestResult.id)).limit(1)
    )).scalars().first()
    record = latest if (latest is not None and isinstance(latest.results_json, dict)
                        and latest.results_json.get("_visit") == visit_key) else None
    if record is None:
        record = StressTestResult(barcode=device.barcode)
        db.add(record)
    record.brand = device.brand
    record.model_name = device.model
    record.run_at = app_now()
    record.overall_status = overall
    record.results_json = results
    record.run_by = run_by
    await db.flush()

    # PDF (failure is non-fatal — the checklist is still saved and the
    # download endpoint regenerates on the fly when no file is attached).
    try:
        fpath = REPORTS_DIR / _pdf_filename(device.barcode, record.id)
        fpath.write_bytes(_generate_pdf(record))
        record.pdf_path = str(fpath)
    except Exception:
        record.pdf_path = None
    return record


def _parse_checklist_items(raw: str) -> list[str]:
    """Comma-separated item names from the checklist form -> the subset that
    actually match STRESS_CHECKLIST_ITEMS, in canonical order. Anything else
    (stale client, tampered request) is silently dropped rather than trusted."""
    if not raw:
        return []
    posted = {s.strip() for s in raw.split(",") if s.strip()}
    return [item for item in STRESS_CHECKLIST_ITEMS if item in posted]


@router.post("/{barcode}/fail")
async def stress_fail_assign(
    barcode: str,
    engineer_user_id: str = Form(...),
    notes: str = Form(""),
    failed_items: str = Form(""),
    na_items: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """'Fail' — assign the device to an L1/L2 engineer.

    Replicates the exact WorkOrder + stage-move mechanism Stock Transfer uses
    when assigning a device to an L1/L2 engineer (see routers/transfers.py
    `_move_devices_bulk`), so the device shows up on that engineer's L1/L2
    Repair "assigned to me" table via the existing WorkOrder query in
    routers/repair.py (`WorkOrder.stage == stage, status != completed`).
    """
    dev_res = await db.execute(select(Device).where(Device.barcode == barcode))
    device = dev_res.scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"Device {barcode} not found")

    try:
        eng_uuid = uuid_module.UUID(engineer_user_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, "Invalid engineer selected")

    engineer = (await db.execute(select(User).where(User.id == eng_uuid))).scalar_one_or_none()
    if not engineer or engineer.role not in (UserRole.l1_engineer, UserRole.l2_engineer):
        raise HTTPException(400, "Selected user is not an active L1/L2 engineer")

    # Both L1 and L2 engineers work the same /repair/l1 queue, and the l2 stage is retired,
    # so a stress failure always lands in l1 regardless of who it is assigned to. Routing an
    # L2 engineer's device to DeviceStage.l2 would strand it in a stage with no page.
    target_stage_str = "l1"
    new_stage = DeviceStage.l1

    prev_stage = device.current_stage
    prev_mv = (await db.execute(
        select(StageMovement).where(
            StageMovement.device_id == device.id,
            StageMovement.to_stage == prev_stage,
            StageMovement.exited_at == None,
        ).order_by(StageMovement.moved_at.desc())
    )).scalars().first()
    if prev_mv:
        prev_mv.exited_at = app_now()

    device.current_stage = new_stage
    # Reset the L1/L2 workflow statuses — a device returning from a stress fail
    # starts a fresh repair cycle; stale "Completed"/"Repair Started" badges on
    # the L1/L2 page were contradictory (found in the 2026-07-15 UX audit).
    device.l1l2_status = "New"
    device.l34_status = None
    device.updated_at = app_now()
    move_notes = f"Stress Test Failed — assigned to {engineer.full_name or engineer.username}"
    if notes:
        move_notes += f". {notes}"

    failed_list = _parse_checklist_items(failed_items)
    # device.stress_notes is what the L1/L2 Repair "Stress Notes" column and
    # Device Detail's Notes section read — built from whichever checklist
    # items were marked Fail, plus any free-text note, so both the engineer's
    # queue and the device history show WHAT failed, not just THAT it failed.
    stress_note = f"Failed: {', '.join(failed_list)}" if failed_list else "Failed"
    if notes:
        stress_note += f" — {notes}"
    device.stress_notes = stress_note

    db.add(StageMovement(device_id=device.id, from_stage=prev_stage, to_stage=new_stage,
                         moved_by=current_user.username, notes=move_notes))

    # Historical record of the manual checklist itself (results_json), reusing
    # StressTestResult rather than a new table — it was already a flexible
    # per-run JSON blob, just previously only ever populated by the automated
    # benchmark's Save button. Same table now also serves the manual result.
    # Checklist + attached PDF for this visit (see _record_checklist_result).
    # Recorded before the stage move below so the visit lookup still sees
    # the movement that brought the tag INTO Stress Test.
    await _record_checklist_result(db, device, failed_list, _parse_checklist_items(na_items),
                                   current_user.username, overall="FAIL")

    # Self-attribution (2026-09-02, same pattern as Final QC's decision WorkID
    # in routers/cosmetic.py): whoever submits this Stress Test result gets
    # their own completed "strs" WorkID for it, visible on /workid-status —
    # distinct from the "l1" WorkOrder below, which tracks the downstream
    # engineer assignment rather than who ran the test.
    # One Stress WorkID per visit (2026-10-27, services/stage_work_orders):
    # the tag's open Stress WorkID closes now as completed. Unpicked, it is
    # credited to whoever submitted the result; picked, it stays with that
    # engineer. With none open, a completed one is recorded for the submitter.
    from services.stage_work_orders import finish as _finish_stage
    await _finish_stage(db, device, DeviceStage.qc_check, current_user)

    # Back at L1/L2 for the chosen engineer: one pending WorkID for this visit.
    from services.stage_work_orders import open_for_stage as _open_for_stage
    work_id = (await _open_for_stage(db, device, DeviceStage.l1, assignee=engineer,
                                     created_by=current_user.username)).work_id

    await create_notification(
        db, user_id=engineer.id, title="Device Assigned to You",
        message=(f"{device.barcode} failed Stress Test and has been assigned to you for "
                 f"{target_stage_str.upper()} repair (WorkID: {work_id})."),
        notification_type="warning",
        barcode=device.barcode, brand=device.brand, model=device.model,
        stage=new_stage.value,
    )

    await audit(db, user=current_user, action="STRESS_FAIL_ASSIGNED",
                table_name="devices", record_id=str(device.id),
                new_value={"assigned_to": engineer.username, "stage": target_stage_str,
                           "work_id": work_id, "notes": notes},
                request=None)

    await db.commit()
    return RedirectResponse(url="/qc?success=Device+failed+%26+assigned+to+" +
                             (engineer.full_name or engineer.username).replace(" ", "+"),
                             status_code=302)


@router.post("/{barcode}/pick")
async def stress_pick(
    barcode: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pick This on the Stress Test queue (2026-10-27): the clicking user
    takes the tag's Stress WorkID — the pending one it arrived with is
    claimed (assigned date = now, movement date kept); one held by someone
    else closes as completed and a new one opens for the picker."""
    from services.stage_work_orders import pick as _pick
    device = (await db.execute(select(Device).where(Device.barcode == barcode))).scalar_one_or_none()
    if not device:
        return JSONResponse({"ok": False, "error": f"Tag {barcode} not found"}, status_code=404)
    if device.current_stage != DeviceStage.qc_check:
        return JSONResponse({"ok": False, "error": "This tag is no longer in Stress Test."}, status_code=400)
    wo, previous = await _pick(db, device, DeviceStage.qc_check, current_user)
    if wo is None:
        return JSONResponse({"ok": True, "already_yours": True})
    picker = current_user.full_name or current_user.username
    for _w, uid, _n in previous:
        if uid and uid != current_user.id:
            await create_notification(
                db, user_id=uid, title="Tag picked by another engineer",
                message=f"{device.barcode} was picked by {picker} (Stress WorkID: {wo.work_id}).",
                notification_type="info", barcode=device.barcode,
                brand=device.brand, model=device.model, stage="qc_check",
            )
    await audit(db, user=current_user, action="STRESS_PICKED", table_name="devices",
                record_id=str(device.id),
                notes=f"{device.barcode} picked by {picker} (WorkID: {wo.work_id})"
                      + (", closed " + ", ".join(f"{w} ({n})" for w, _u, n in previous) if previous else ""),
                request=None)
    await db.commit()
    return JSONResponse({"ok": True, "work_id": wo.work_id})


@router.post("/{barcode}/complete-to-paint")
async def stress_complete_to_paint(
    barcode: str,
    engineer_user_id: str = Form(""),
    notes: str = Form(""),
    failed_items: str = Form(""),
    na_items: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """'Complete' — send the device to the Cosmetic Received stage (the
    holding stage before cosmetic work starts — see routers/cosmetic.py's
    COSMETIC_PIPELINE).

    Reuses the exact stage-set logic from routers/cosmetic.py's
    `send_to_cosmetic` (QC Check -> Cosmetic Received), and additionally
    records a WorkOrder (same mechanism used for L1/L2/L3 assignment) so the
    movement is traceable, since Device has no assigned_username field of
    its own. That WorkOrder is also what the Cosmetic Received / Cosmetic
    Completed pages read for their "Assigned to" / "Assigned Date" columns.

    `engineer_user_id` is optional: the QC 'Complete' button sends the tag
    straight to Cosmetic Received with an unassigned WorkOrder, while callers
    that do pass an engineer still get validation, assignment and a
    notification.
    """
    dev_res = await db.execute(select(Device).where(Device.barcode == barcode))
    device = dev_res.scalar_one_or_none()
    if not device:
        raise HTTPException(404, f"Device {barcode} not found")

    engineer = None
    if engineer_user_id:
        try:
            eng_uuid = uuid_module.UUID(engineer_user_id)
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(400, "Invalid engineer selected")

        engineer = (await db.execute(select(User).where(User.id == eng_uuid))).scalar_one_or_none()
        allowed_paint_roles = (UserRole.qc_inspector, UserRole.inventory_manager, UserRole.sales_manager,
                               UserRole.cosmetic_manager)
        if not engineer or engineer.role not in allowed_paint_roles:
            raise HTTPException(400, "Selected user is not an active Paint/Cleaning engineer")

    prev_stage = device.current_stage
    device.current_stage = DeviceStage.cosmetic_received
    device.updated_at = app_now()
    # Complete means the checklist came back all-Pass — clear any earlier
    # failure note so Device Detail / a future L1/L2 view doesn't show a
    # stale "Failed: …" note for a device that has since passed.
    device.stress_notes = None
    move_notes = "Sent to Cosmetic Refurbishment (Cosmetic Received)"
    if engineer:
        move_notes += f" — assigned to {engineer.full_name or engineer.username}"
    if notes:
        move_notes += f". {notes}"
    db.add(StageMovement(device_id=device.id, from_stage=prev_stage, to_stage=DeviceStage.cosmetic_received,
                         moved_by=current_user.username, notes=move_notes))

    # The checklist exactly as ticked on the panel (it used to record every
    # item as Pass regardless), plus its attached PDF — see
    # _record_checklist_result for the one-report-per-visit rule.
    await _record_checklist_result(db, device, _parse_checklist_items(failed_items),
                                   _parse_checklist_items(na_items), current_user.username)

    # Self-attribution (2026-09-02, same pattern as Final QC's decision WorkID
    # in routers/cosmetic.py): whoever submits this Stress Test result gets
    # their own completed "strs" WorkID for it, visible on /workid-status —
    # distinct from the "recv" WorkOrder below, which tracks the downstream
    # engineer assignment rather than who ran the test.
    # One Stress WorkID per visit (2026-10-27, services/stage_work_orders):
    # the tag's open Stress WorkID closes now as completed. Unpicked, it is
    # credited to whoever submitted the result; picked, it stays with that
    # engineer. With none open, a completed one is recorded for the submitter.
    from services.stage_work_orders import finish as _finish_stage
    await _finish_stage(db, device, DeviceStage.qc_check, current_user)

    work_id = await _gen_work_id(db)
    db.add(WorkOrder(
        # WorkOrder.stage is String(5) — "recv" is routers/cosmetic.py's
        # MOVE_STAGE_CODE for Cosmetic Received, the stage this hand-off
        # actually lands the device on. "clean" is reserved for the Cleaning
        # stage's own entry WorkOrder, created later by Received's own "Move
        # to Cleaning" modal, not by this endpoint.
        work_id=work_id, device_id=device.id, barcode=device.barcode,
        stage="recv", assigned_role=engineer.role.value if engineer else None,
        assigned_user_id=engineer.id if engineer else None,
        assigned_username=engineer.username if engineer else None,
        assigned_name=engineer.full_name if engineer else None, status="pending",
        created_by=current_user.username,
    ))

    if engineer:
        await create_notification(
            db, user_id=engineer.id, title="Device Assigned to You",
            message=(f"{device.barcode} completed Stress Test and has been sent to Cosmetic Received, "
                     f"assigned to you (WorkID: {work_id})."),
            notification_type="info",
            barcode=device.barcode, brand=device.brand, model=device.model,
            stage=DeviceStage.cosmetic_received.value,
        )

    await audit(db, user=current_user, action="STRESS_COMPLETE_TO_PAINT",
                table_name="devices", record_id=str(device.id),
                new_value={"assigned_to": engineer.username if engineer else None,
                           "stage": "cosmetic_received", "work_id": work_id, "notes": notes},
                request=None)

    await db.commit()
    if engineer:
        return RedirectResponse(url="/qc?success=Device+sent+to+Cosmetic+Received+%26+assigned+to+" +
                                 (engineer.full_name or engineer.username).replace(" ", "+"),
                                 status_code=302)
    return RedirectResponse(url="/qc?success=Device+sent+to+Cosmetic+Received", status_code=302)


@router.get("/has-results")
async def bulk_has_results(
    barcodes: str,   # comma-separated
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns set of barcodes that have at least one saved stress result."""
    bc_list = [b.strip() for b in barcodes.split(",") if b.strip()]
    if not bc_list:
        return {}
    rows = await db.execute(
        select(StressTestResult.barcode, StressTestResult.id,
               StressTestResult.overall_status, StressTestResult.run_at)
        .where(StressTestResult.barcode.in_(bc_list))
        .order_by(desc(StressTestResult.run_at))
    )
    seen = {}
    for barcode, rid, status, run_at in rows.all():
        if barcode not in seen:
            seen[barcode] = {
                "result_id":     rid,
                "overall_status": status,
                "run_at":        run_at.isoformat() if run_at else None,
            }
    return seen
