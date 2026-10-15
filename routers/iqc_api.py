"""
IQC Machine API
---------------
Machine-to-machine endpoints consumed by the OxyQC standalone app.
Authentication: static API key in X-OxyQC-Key header (configured in config.ini [oxyqc] api_key).

Endpoints:
  GET  /iqc/api/health           — connectivity check
  GET  /iqc/api/lot/{prefix}     — resolve lot_id from first-4-chars of barcode
  GET  /iqc/api/users            — active IQC-capable users for inspector dropdown
  POST /iqc/api/submit           — create Device + IQCInspection atomically
  GET  /iqc/api/check/{barcode}  — check if barcode already exists
"""
from __future__ import annotations

import uuid
from datetime import datetime
from utils.timezone import app_now
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from config import OXYQC_API_KEY
from database import get_db
from models.device import Device, DeviceStage, StageMovement
from models.iqc_inspection import IQCInspection
from models.lot import Lot
from models.user import User, UserRole

router = APIRouter(prefix="/iqc/api", tags=["iqc-machine-api"])


# ── Auth ──────────────────────────────────────────────────────────────────────

async def _require_api_key(x_oxyqc_key: str = Header(..., alias="X-OxyQC-Key")):
    if x_oxyqc_key != OXYQC_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid OxyQC API key")


# ── Health ────────────────────────────────────────────────────────────────────

@router.get("/health")
async def health(_: None = Depends(_require_api_key)):
    return {"status": "ok", "service": "OxyPC Inventory Lifecycle IQC API", "time": app_now().isoformat()}


# ── Lot lookup ────────────────────────────────────────────────────────────────

@router.get("/lot/{prefix}")
async def get_lot_by_prefix(
    prefix: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_api_key),
):
    """
    Resolve a lot from the first-4-character prefix of a device barcode.
    Tries exact match on lot_number first, then prefix (LIKE 'prefix%').
    """
    # 1. Exact match
    res = await db.execute(select(Lot).where(Lot.lot_number == prefix))
    lot = res.scalars().first()

    # 2. Prefix match (e.g. barcode "3772001" → prefix "3772" → lot_number "3772")
    if not lot:
        res = await db.execute(
            select(Lot).where(Lot.lot_number.ilike(f"{prefix}%")).limit(1)
        )
        lot = res.scalars().first()

    if not lot:
        raise HTTPException(status_code=404, detail=f"No lot found matching prefix '{prefix}'")

    return {
        "lot_id": str(lot.id),
        "lot_number": lot.lot_number,
        "supplier": lot.supplier_name or "",
        "qty": lot.qty,
        "buying_price": str(lot.buying_price),
    }


# ── User list ─────────────────────────────────────────────────────────────────

@router.get("/users")
async def get_iqc_users(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_api_key),
):
    """Return active users eligible to perform IQC (for inspector dropdown)."""
    res = await db.execute(
        select(User)
        .where(User.is_active == True)  # noqa: E712
        .where(User.role.in_([UserRole.iqc_inspector, UserRole.inventory_manager, UserRole.admin]))
        .order_by(User.full_name)
    )
    users = res.scalars().all()
    return [
        {"username": u.username, "full_name": u.full_name or u.username, "role": u.role.value}
        for u in users
    ]


# ── Barcode existence check ───────────────────────────────────────────────────

@router.get("/check/{barcode}")
async def check_barcode(
    barcode: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_api_key),
):
    # Case-folded — see the comment on the duplicate check in submit_iqc below.
    res = await db.execute(select(Device).where(func.upper(Device.barcode) == barcode.upper()))
    exists = res.scalar_one_or_none() is not None
    return {"barcode": barcode, "exists": exists}


# ── Submit IQC ────────────────────────────────────────────────────────────────

class IQCSubmitPayload(BaseModel):
    # ── Device identity ───────────────────────────────────────────────────────
    barcode: str
    lot_id: str                          # UUID string
    sub_category: str = "Laptop"
    device_type: str = ""
    brand: str = ""
    model: str = ""
    serial_no: str = ""
    grn_number: str = ""
    cpu: str = ""
    generation: str = ""
    ram_gb: Optional[int] = None
    storage_gb: Optional[int] = None
    storage_type: str = ""
    hdd_capacity_gb: Optional[int] = None
    screen_size: str = ""
    battery_health_pct: Optional[int] = None
    bios_password: bool = False
    color: str = ""
    grade: str = ""
    floor: str = ""
    warehouse: str = ""
    notes: str = ""
    inspector_name: str = ""             # full name of the inspector

    # ── Functional status ─────────────────────────────────────────────────────
    power_on: bool = False
    status: str = ""
    all_ok: bool = False
    r2v3_grade_category: str = ""

    # ── Screen ────────────────────────────────────────────────────────────────
    screen_dot: bool = False
    screen_line: bool = False
    screen_functional: bool = False
    screen_discoloration: bool = False
    screen_patch: bool = False
    screen_broken: bool = False
    screen_flickering: bool = False
    screen_scratch: bool = False
    screen_loose: bool = False
    screen_missing: bool = False
    screen_hinge_broken: bool = False
    screen_colour_spread: bool = False
    screen_keyboard_mark: bool = False
    screen_hard_press: bool = False

    # ── Panel A (LCD Lid / Top Cover) ─────────────────────────────────────────
    panel_a_scratch: bool = False
    panel_a_broken: bool = False
    panel_a_missing: bool = False
    panel_a_dent: bool = False
    panel_a_colour_fade: bool = False

    # ── Panel B (Bottom Base) ─────────────────────────────────────────────────
    panel_b_scratch: bool = False
    panel_b_colour_fade: bool = False
    panel_b_rubber_cut: bool = False
    panel_b_broken: bool = False
    panel_b_missing: bool = False

    # ── Panel C (Bezel / Frame) ───────────────────────────────────────────────
    panel_c_scratch: bool = False
    panel_c_broken: bool = False
    panel_c_missing: bool = False
    panel_c_dent: bool = False
    panel_c_colour_fade: bool = False

    # ── Panel D (Palmrest) ────────────────────────────────────────────────────
    panel_d_dent: bool = False
    panel_d_colour_fade: bool = False
    panel_d_scratch: bool = False
    panel_d_broken: bool = False
    panel_d_missing: bool = False

    # ── Keyboard ──────────────────────────────────────────────────────────────
    keyboard_working: bool = False
    keyboard_colour_fade: bool = False
    keyboard_key_missing: bool = False
    keyboard_hard_press: bool = False

    # ── Speaker ───────────────────────────────────────────────────────────────
    speaker_status: bool = False          # legacy / fallback
    speaker_left:   bool = True           # left speaker working
    speaker_right:  bool = True           # right speaker working
    subwoofer:      str  = "N/A"          # N/A / Working / Faulty

    # ── Touchpad ──────────────────────────────────────────────────────────────
    touchpad_working: bool = False
    touchpad_click_working: bool = False
    touchpad_scratch: bool = False
    touchpad_colour_fade: bool = False
    touchpad_missing: bool = False

    # ── Ports ─────────────────────────────────────────────────────────────────
    port_hdmi: bool = False
    port_usb_working: bool = False
    port_audio_jack: bool = False
    # Detailed port counts (optional — from PortsTab spinboxes)
    usb_a2_count:    Optional[int] = None   # USB-A 2.0 port count
    usb_a3_count:    Optional[int] = None   # USB-A 3.0 port count
    usbc_count:      Optional[int] = None   # USB-C port count
    hdmi_count:      Optional[int] = None   # HDMI port count
    charging_port:   str = "OK"             # OK / Damaged / Missing
    charger_wattage: str = ""               # e.g. "65W"

    # ── Other components ──────────────────────────────────────────────────────
    wifi_status: bool = False
    webcam_status: bool = False
    hdd_connector: bool = False
    hdd_casing: bool = False
    battery_present: bool = False
    battery_cable: bool = False
    dvd_drive: bool = False

    # ── Stress test report (Final QC mode only) ───────────────────────────────
    # JSON string produced by StressOrchestrator.to_json() on the OxyQC app.
    # Stored verbatim in IQCInspection.stress_report for display on device page.
    stress_report: Optional[str] = None


def _v(s: str) -> Optional[str]:
    """Return None for empty strings."""
    if s is None:
        return None
    return str(s).strip() or None


def _b(val: bool) -> str:
    """Convert bool to 'Yes'/'No' string for IQCInspection string fields."""
    return "Yes" if val else "No"


@router.post("/submit", status_code=201)
async def submit_iqc(
    payload: IQCSubmitPayload,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_api_key),
):
    """
    Create a Device record + IQCInspection in one atomic transaction.
    Returns 409 if barcode already exists.
    """
    # ── Derive computed fields ────────────────────────────────────────────────

    # Speaker status string
    if payload.speaker_left and payload.speaker_right:
        speaker_stat = "Both speakers working"
    elif not payload.speaker_left and not payload.speaker_right:
        speaker_stat = "Both speakers faulty"
    elif not payload.speaker_left:
        speaker_stat = "Left speaker faulty"
    else:
        speaker_stat = "Right speaker faulty"
    if payload.subwoofer not in ("N/A", ""):
        speaker_stat += f" | Subwoofer: {payload.subwoofer}"

    # Port booleans: prefer counts if provided
    if payload.usb_a2_count is not None or payload.usb_a3_count is not None or payload.usbc_count is not None:
        usb_working = (
            (payload.usb_a2_count or 0) > 0 or
            (payload.usb_a3_count or 0) > 0 or
            (payload.usbc_count   or 0) > 0
        )
    else:
        usb_working = payload.port_usb_working

    if payload.hdmi_count is not None:
        hdmi_ok = payload.hdmi_count > 0
    else:
        hdmi_ok = payload.port_hdmi

    # Extra remarks for port detail
    port_parts = []
    if payload.usb_a2_count is not None:
        port_parts.append(f"USB-A2.0:{payload.usb_a2_count}")
    if payload.usb_a3_count is not None:
        port_parts.append(f"USB-A3.0:{payload.usb_a3_count}")
    if payload.usbc_count is not None:
        port_parts.append(f"USB-C:{payload.usbc_count}")
    if payload.hdmi_count is not None:
        port_parts.append(f"HDMI:{payload.hdmi_count}")
    if payload.charging_port:
        port_parts.append(f"ChargingPort:{payload.charging_port}")
    if payload.charger_wattage:
        port_parts.append(f"Charger:{payload.charger_wattage}")
    extra_remarks = " | ".join(port_parts) if port_parts else None

    # ── Duplicate barcode check ───────────────────────────────────────────────
    # Case-folded: a tag scanned or typed with different capitalisation than
    # however it was first entered used to pass this check clean and register
    # as a second, fully independent device for the same physical unit. The
    # barcode column's UNIQUE constraint is case-sensitive at the database
    # level, so nothing there caught it either.
    res = await db.execute(
        select(Device).where(func.upper(Device.barcode) == payload.barcode.upper())
    )
    if res.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Barcode '{payload.barcode}' already registered")

    # Validate lot_id
    try:
        lot_uuid = uuid.UUID(payload.lot_id)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid lot_id UUID: {payload.lot_id!r}")

    lot_res = await db.execute(select(Lot).where(Lot.id == lot_uuid))
    if not lot_res.scalar_one_or_none():
        raise HTTPException(status_code=404, detail=f"Lot '{payload.lot_id}' not found")

    # Create Device
    device = Device(
        barcode=payload.barcode,
        lot_id=lot_uuid,
        sub_category=_v(payload.sub_category) or "Laptop",
        device_type=_v(payload.device_type),
        brand=_v(payload.brand),
        model=_v(payload.model),
        serial_no=_v(payload.serial_no),
        grn_number=_v(payload.grn_number),
        cpu=_v(payload.cpu),
        generation=_v(payload.generation),
        ram_gb=payload.ram_gb,
        storage_gb=payload.storage_gb,
        storage_type=_v(payload.storage_type),
        hdd_capacity_gb=payload.hdd_capacity_gb,
        screen_size=_v(payload.screen_size),
        battery_health_pct=payload.battery_health_pct,
        bios_password=payload.bios_password,
        color=_v(payload.color),
        grade=_v(payload.grade),
        floor=_v(payload.floor),
        warehouse=_v(payload.warehouse),
        notes=_v(payload.notes),
        current_stage=DeviceStage.iqc,
    )
    db.add(device)
    await db.flush()  # get device.id

    # Create IQCInspection
    inspection = IQCInspection(
        device_id=device.id,
        inspector_name=_v(payload.inspector_name),
        power_on=_b(payload.power_on),
        status=_v(payload.status),
        all_ok=_b(payload.all_ok),
        bios_password=_b(payload.bios_password),
        r2v3_grade_category=_v(payload.r2v3_grade_category),
        screen_dot=_b(payload.screen_dot),
        screen_line=_b(payload.screen_line),
        screen_functional=_b(payload.screen_functional),
        screen_discoloration=_b(payload.screen_discoloration),
        screen_patch=_b(payload.screen_patch),
        screen_broken=_b(payload.screen_broken),
        screen_flickering=_b(payload.screen_flickering),
        screen_scratch=_b(payload.screen_scratch),
        screen_loose=_b(payload.screen_loose),
        screen_missing=_b(payload.screen_missing),
        screen_hinge_broken=_b(payload.screen_hinge_broken),
        screen_colour_spread=_b(payload.screen_colour_spread),
        screen_keyboard_mark=_b(payload.screen_keyboard_mark),
        screen_hard_press=_b(payload.screen_hard_press),
        panel_a_scratch=_b(payload.panel_a_scratch),
        panel_a_broken=_b(payload.panel_a_broken),
        panel_a_missing=_b(payload.panel_a_missing),
        panel_a_dent=_b(payload.panel_a_dent),
        panel_a_colour_fade=_b(payload.panel_a_colour_fade),
        panel_b_scratch=_b(payload.panel_b_scratch),
        panel_b_colour_fade=_b(payload.panel_b_colour_fade),
        panel_b_rubber_cut=_b(payload.panel_b_rubber_cut),
        panel_b_broken=_b(payload.panel_b_broken),
        panel_b_missing=_b(payload.panel_b_missing),
        panel_c_scratch=_b(payload.panel_c_scratch),
        panel_c_broken=_b(payload.panel_c_broken),
        panel_c_missing=_b(payload.panel_c_missing),
        panel_c_dent=_b(payload.panel_c_dent),
        panel_c_colour_fade=_b(payload.panel_c_colour_fade),
        panel_d_dent=_b(payload.panel_d_dent),
        panel_d_colour_fade=_b(payload.panel_d_colour_fade),
        panel_d_scratch=_b(payload.panel_d_scratch),
        panel_d_broken=_b(payload.panel_d_broken),
        panel_d_missing=_b(payload.panel_d_missing),
        keyboard_working=_b(payload.keyboard_working),
        keyboard_colour_fade=_b(payload.keyboard_colour_fade),
        keyboard_key_missing=_b(payload.keyboard_key_missing),
        keyboard_hard_press=_b(payload.keyboard_hard_press),
        speaker_status=speaker_stat,
        touchpad_working=_b(payload.touchpad_working),
        touchpad_click_working=_b(payload.touchpad_click_working),
        touchpad_scratch=_b(payload.touchpad_scratch),
        touchpad_colour_fade=_b(payload.touchpad_colour_fade),
        touchpad_missing=_b(payload.touchpad_missing),
        port_hdmi=_b(hdmi_ok),
        port_usb_working=_b(usb_working),
        port_audio_jack=_b(payload.port_audio_jack),
        wifi_status="Working" if payload.wifi_status else "Not Working",
        webcam_status="Working" if payload.webcam_status else "Not Working",
        hdd_connector=_b(payload.hdd_connector),
        hdd_casing=_b(payload.hdd_casing),
        battery_present=_b(payload.battery_present),
        battery_cable=_b(payload.battery_cable),
        dvd_drive=_b(payload.dvd_drive),
        remarks=extra_remarks,
        stress_report=_v(payload.stress_report),
    )
    db.add(inspection)

    # Stage movement record
    db.add(StageMovement(
        device_id=device.id,
        from_stage=None,
        to_stage=DeviceStage.iqc,
        moved_by=payload.inspector_name or "OxyQC",
        notes="Device registered via OxyQC standalone app",
    ))

    await db.commit()

    return {
        "status": "created",
        "device_id": str(device.id),
        "barcode": payload.barcode,
        "lot_id": str(lot_uuid),
        "stage": "iqc",
    }
