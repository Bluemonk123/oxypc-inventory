from __future__ import annotations

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, DateTime, JSON, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base

# The manual Stress Test checklist shown on the Stress Test page's right-hand
# panel (each item gets a Pass/Fail checkbox pair) — replaces the earlier
# automated 11-benchmark grid (stress_runner.py's TEST_NAMES/ALL_KEYS, still
# used by the separate on-server/on-device automated run + PDF pipeline).
# Persisted into StressTestResult.results_json as {item: "pass"|"fail"}.
STRESS_CHECKLIST_ITEMS = [
    "CPU", "RAM", "Hard Drive", "Screen", "Bezel", "Keyboard", "Touchpad",
    "Logic Card", "Camera", "Speaker", "Battery", "Wi-fi", "DC Jack",
    "LAN Port", "USB Ports", "Bluetooth", "Fingerprint", "Display & GPU",
    "Thermal", "Power Button",
    # Added 2026-10-10
    "HDMI Port", "Microphone", "Touch Screen", "Cosmetic Check",
    "Diagnostic Cleaning Test", "Startup/Critical Drivers", "Software/License Key",
]

# Items that may not exist on every device — the checklist gives these a
# third "N/A" choice (stored as "na", never counted as a failure).
STRESS_OPTIONAL_ITEMS = {"Touch Screen"}


class StressTestResult(Base):
    __tablename__ = "stress_test_results"

    id:             Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    barcode:        Mapped[str]      = mapped_column(String(100), nullable=False, index=True)
    brand:          Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    model_name:     Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    run_at:         Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration:       Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    overall_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    results_json:   Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    pdf_path:       Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    run_by:         Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
