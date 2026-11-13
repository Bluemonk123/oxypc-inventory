"""
IQC Physical Inspection Model
Stores detailed physical condition data captured during IQC inspection.
Each device can have one IQC inspection record.
"""
import uuid
from datetime import datetime
from utils.timezone import app_now
from sqlalchemy import Column, String, DateTime, Boolean, ForeignKey, Text, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base


class IQCInspection(Base):
    """Detailed physical condition check performed during IQC."""
    __tablename__ = "iqc_inspections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, unique=True)
    inspector_name = Column(String(100), nullable=True)
    inspected_at = Column(DateTime, default=app_now)

    # ── Functional Status ─────────────────────────────────────────────────────
    power_on = Column(String(40), nullable=True)         # Yes / No
    bios_password = Column(String(40), nullable=True)    # Yes / No (override)
    all_ok = Column(String(40), nullable=True)           # Yes / No
    status = Column(String(50), nullable=True)          # Power On / No Display / No Power

    # ── Screen Condition ──────────────────────────────────────────────────────
    screen_dot = Column(String(40), nullable=True)           # Yes / No
    screen_line = Column(String(40), nullable=True)          # Yes / No
    screen_functional = Column(String(40), nullable=True)    # Yes / No
    screen_discoloration = Column(String(40), nullable=True) # Yes / No
    screen_patch = Column(String(40), nullable=True)         # No / Minor / Major
    touch_screen = Column(String(40), nullable=True)         # Yes / No / Yes - Faulty
    screen_broken = Column(String(40), nullable=True)        # Yes / No
    screen_flickering = Column(String(40), nullable=True)    # Yes / No
    screen_scratch = Column(String(40), nullable=True)       # No / Minor Scratch / Major Scratch
    screen_loose = Column(String(40), nullable=True)         # Yes / No
    screen_missing = Column(String(40), nullable=True)       # Yes / No
    screen_hinge_broken = Column(String(40), nullable=True)  # Yes / No
    screen_colour_spread = Column(String(40), nullable=True) # Yes / No
    screen_keyboard_mark = Column(String(40), nullable=True) # Yes / No
    screen_hard_press = Column(String(40), nullable=True)    # Yes / No

    # ── Panel A — LCD Lid / Top Cover ─────────────────────────────────────────
    panel_a_scratch = Column(String(40), nullable=True)      # No / Minor Scratch / Major Scratch
    panel_a_broken = Column(String(40), nullable=True)       # No / Yes / Major Broken
    panel_a_missing = Column(String(40), nullable=True)      # No / Yes
    panel_a_dent = Column(String(40), nullable=True)         # No / Yes / Major Dent
    panel_a_colour_fade = Column(String(40), nullable=True)  # No / Yes

    # ── Panel B — Bottom Base ─────────────────────────────────────────────────
    panel_b_scratch = Column(String(40), nullable=True)      # No / Minor Scratch / Major Scratch
    panel_b_colour_fade = Column(String(40), nullable=True)  # No / Yes
    panel_b_rubber_cut = Column(String(40), nullable=True)   # No / Yes
    panel_b_broken = Column(String(40), nullable=True)       # No / Yes / Major Broken
    panel_b_missing = Column(String(40), nullable=True)      # No / Yes

    # ── Panel C — Bezel / Frame ───────────────────────────────────────────────
    panel_c_scratch = Column(String(40), nullable=True)      # No / Minor Scratch / Major Scratch
    panel_c_broken = Column(String(40), nullable=True)       # No / Yes / Major Broken
    panel_c_missing = Column(String(40), nullable=True)      # No / Yes
    panel_c_dent = Column(String(40), nullable=True)         # No / Yes / Major Dent
    panel_c_colour_fade = Column(String(40), nullable=True)  # No / Yes

    # ── Panel D — Palmrest ────────────────────────────────────────────────────
    panel_d_dent = Column(String(40), nullable=True)         # No / Yes / Major Dent
    panel_d_colour_fade = Column(String(40), nullable=True)  # No / Yes
    panel_d_scratch = Column(String(40), nullable=True)      # No / Minor Scratch / Major Scratch
    panel_d_broken = Column(String(40), nullable=True)       # No / Yes / Major Broken
    panel_d_missing = Column(String(40), nullable=True)      # No / Yes

    # ── Keyboard ──────────────────────────────────────────────────────────────
    keyboard_working = Column(String(40), nullable=True)     # Yes / Not Working / No
    keyboard_colour_fade = Column(String(40), nullable=True) # No / Yes
    keyboard_key_missing = Column(String(40), nullable=True) # No / Yes
    keyboard_hard_press = Column(String(40), nullable=True)  # No / Yes

    # ── Speaker ───────────────────────────────────────────────────────────────
    speaker_status = Column(String(50), nullable=True)
    # Both speakers working / Both speakers faulty / Left speaker faulty / Right speaker faulty

    # ── Touchpad ──────────────────────────────────────────────────────────────
    touchpad_working = Column(String(40), nullable=True)     # Yes / Not Working / No
    touchpad_click_working = Column(String(40), nullable=True) # Yes / No
    touchpad_scratch = Column(String(40), nullable=True)     # No / Minor Scratch / Major Scratch
    touchpad_colour_fade = Column(String(40), nullable=True) # No / Yes
    touchpad_missing = Column(String(40), nullable=True)     # No / Yes

    # ── Ports ─────────────────────────────────────────────────────────────────
    port_hdmi = Column(String(40), nullable=True)            # Yes / Not Working / No
    port_usb_working = Column(String(40), nullable=True)     # Yes / Not Working / No
    port_audio_jack = Column(String(40), nullable=True)      # Yes / Not Working / No
    usb_a_ports = Column(Integer, nullable=True)             # count (agent-fillable)
    usb_c_ports = Column(Integer, nullable=True)             # count (manual)
    ethernet_ports = Column(Integer, nullable=True)          # count (agent-fillable)

    # ── Other Components ──────────────────────────────────────────────────────
    wifi_status = Column(String(40), nullable=True)          # Working / Faulty / Not Checked
    webcam_status = Column(String(40), nullable=True)        # Ok / Faulty / Not Checked
    hdd_connector = Column(String(40), nullable=True)        # Yes / Not Working / No
    hdd_casing = Column(String(40), nullable=True)           # Yes / Not Working / No
    battery_present = Column(String(40), nullable=True)      # Yes / No
    battery_cable = Column(String(40), nullable=True)        # Yes / No
    charging_port = Column(String(40), nullable=True)        # Yes / No (works if charging)
    dvd_drive = Column(String(40), nullable=True)            # Yes / No / NA

    # ── Covers and Casing ─────────────────────────────────────────────────────
    cover_ram = Column(String(40), nullable=True)              # Yes / No
    cover_dvd = Column(String(40), nullable=True)              # Yes / No
    cover_storage = Column(String(40), nullable=True)          # Yes / No

    # ── Hinge ─────────────────────────────────────────────────────────────────
    hinge_condition = Column(String(40), nullable=True)       # Yes / No
    hinge_cover = Column(String(40), nullable=True)           # Yes / No
    touchpad_logicboard = Column(String(40), nullable=True)   # Yes / No

    # ── Storage / Fan ─────────────────────────────────────────────────────────
    storage_health_pct = Column(Integer, nullable=True)
    fan_sound_dba = Column(Integer, nullable=True)
    fan_working = Column(String(40), nullable=True)            # Yes / No

    # ── Part conditions from the External Partner IQC format (2026-11-13,
    # Pankaj — approved in chat). One condition word per part, as written in
    # the sheet (Ok / Minor Scratch / Rubber Cut / NA / Not Checked …).
    # Filled by Trade Partner → Manage Lots → Asset IQC upload. ──
    bezel = Column(String(40), nullable=True)              # "Front Panel(Bazel)"
    panel = Column(String(40), nullable=True)              # "Panel"
    screen = Column(String(40), nullable=True)             # "Screen"
    power = Column(String(40), nullable=True)              # "Power Panel"
    bottom_base = Column(String(40), nullable=True)        # "Base"
    external_battery = Column(String(40), nullable=True)   # "EXT Battery"
    internal_battery = Column(String(40), nullable=True)   # "Int. Battery"

    # ── Overall Assessment ────────────────────────────────────────────────────
    r2v3_grade_category = Column(String(40), nullable=True)  # C0 / C3 / C4 / C5
    remarks = Column(Text, nullable=True)

    # ── Stress Test Report (Final QC mode, submitted from OxyQC standalone) ──
    # Stores JSON string produced by StressOrchestrator.to_json()
    stress_report = Column(Text, nullable=True)

    device = relationship("Device", backref="iqc_inspection", uselist=False)
