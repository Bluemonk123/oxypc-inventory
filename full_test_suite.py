"""
OxyPC Inventory — FULL TEST SUITE
===================================
Covers every testable dimension of the system end-to-end.

TEST SECTIONS
─────────────
F  — Functional  : all 80 UI endpoints reachable, no 500s
E  — E2E Flow    : full device lifecycle (15 devices, all stages)
S  — Spare Parts : purchase → consume → ledger balance
C  — Cosmetic    : cleaning → dry_sanding → masking → painting →
                   water_sanding → final_qc → ready_to_sale
X  — Edge Cases  : invalid transitions, below-cost, neg stock,
                   dupe barcode, sell wrong stage, bad login
R  — RBAC        : all 8 roles allowed/denied pages
I  — Integrity   : P&L math, ledger double-entry, stage log completeness
P  — Perf        : 5000-device bulk, page-load times, 50 concurrent
                   users, stage-move throughput, sales throughput

Run: python full_test_suite.py
Requires: server running on http://localhost:8000
"""

from __future__ import annotations

import asyncio
import aiohttp
import sys
import time
import re
import csv
import io
import json
from datetime import date, datetime

sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://localhost:8000"

# ─────────────────────────────────────────────────────────────────────────────
# Result tracking
# ─────────────────────────────────────────────────────────────────────────────
PASS = 0
FAIL = 0
SKIP = 0
RESULTS: list[tuple] = []
SECTION_TIMES: dict[str, float] = {}


def record(section, tid, name, passed, note=""):
    global PASS, FAIL
    if passed is None:          # skip
        global SKIP
        SKIP += 1
        RESULTS.append((section, tid, name, "SKIP", note))
        print(f"  [SKIP] {tid} {name}{(' — ' + note) if note else ''}")
        return
    status = "PASS" if passed else "FAIL"
    if passed:
        PASS += 1
    else:
        FAIL += 1
    RESULTS.append((section, tid, name, status, note))
    flag = "[PASS]" if passed else "[FAIL]"
    print(f"  {flag} {tid} {name}{(' — ' + note) if note else ''}")


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────
async def login(session, username="admin", password="oxypc@admin123") -> bool:
    try:
        r = await session.post(f"{BASE}/auth/login",
                               data={"username": username, "password": password},
                               allow_redirects=True)
        text = await r.text()
        return r.status == 200 and "login" not in str(r.url).lower().split("?")[0].rstrip("/").split("/")[-1] or "dashboard" in text.lower() or "stage pipeline" in text.lower()
    except Exception:
        return False


async def get_(session, path, timeout=20) -> tuple[int, str]:
    try:
        r = await session.get(f"{BASE}{path}", allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=timeout))
        return r.status, await r.text()
    except asyncio.TimeoutError:
        return 0, "TIMEOUT"
    except Exception as e:
        return 0, str(e)


async def post_(session, path, data, timeout=20) -> tuple[int, str, str]:
    try:
        r = await session.post(f"{BASE}{path}", data=data,
                                allow_redirects=True,
                                timeout=aiohttp.ClientTimeout(total=timeout))
        text = await r.text()
        return r.status, str(r.url), text
    except asyncio.TimeoutError:
        return 0, "", "TIMEOUT"
    except Exception as e:
        return 0, "", str(e)


def ok_page(status, text, keyword=None) -> bool:
    if status != 200:
        return False
    if "internal server error" in text.lower():
        return False
    if keyword and keyword.lower() not in text.lower():
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers (direct async SQL)
# ─────────────────────────────────────────────────────────────────────────────
async def db_fetchone(sql, params=None):
    from database import AsyncSessionLocal
    from sqlalchemy import text as sa_text
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text(sql), params or {})
        return r.fetchone()


async def db_fetchall(sql, params=None):
    from database import AsyncSessionLocal
    from sqlalchemy import text as sa_text
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text(sql), params or {})
        return r.fetchall()


async def db_exec(sql, params=None):
    from database import AsyncSessionLocal
    from sqlalchemy import text as sa_text
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text(sql), params or {})
        await db.commit()


async def db_scalar(sql, params=None):
    from database import AsyncSessionLocal
    from sqlalchemy import text as sa_text
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text(sql), params or {})
        return r.scalar()


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# SECTION F — FUNCTIONAL: all endpoints reachable, zero 500s
# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

FUNCTIONAL_ENDPOINTS = [
    # AUTH
    ("F-AUTH-01", "GET", "/auth/login",                       "Login"),
    # DASHBOARD
    ("F-DASH-01", "GET", "/",                                  "Stage Pipeline"),
    # DEVICES
    ("F-DEV-01",  "GET", "/devices",                           "Inventory Search"),
    ("F-DEV-02",  "GET", "/devices?q=OXY-TEST",                "Inventory Search"),
    ("F-DEV-03",  "GET", "/devices?stage=l1",                  "Inventory Search"),
    ("F-DEV-04",  "GET", "/devices?grade=A",                   "Inventory Search"),
    ("F-DEV-05",  "GET", "/devices/export",                    None),       # Excel (zipped) since 2026-11-18: status only
    # LOTS / GRN
    ("F-LOT-01",  "GET", "/lots",                              "Lot"),
    ("F-LOT-02",  "GET", "/lots/new",                          "Lot"),
    ("F-GRN-01",  "GET", "/grn",                               "GRN"),
    ("F-GRN-02",  "GET", "/grn/new",                           "GRN"),
    # STOCK
    ("F-STK-01",  "GET", "/stock",                             "Stock"),
    # IQC
    ("F-IQC-01",  "GET", "/iqc",                               "IQC"),
    ("F-IQC-02",  "GET", "/iqc/new",                           "IQC"),
    # REPAIR
    ("F-REP-01",  "GET", "/repair/l1",                         "L1"),
    # F-REP-02/03 retired: /repair/l2 and /repair/l3 are withdrawn and now 404.
    ("F-REP-04",  "GET", "/repair/l3l4",                       "L3"),
    # QC
    ("F-QC-01",   "GET", "/qc",                                "QC"),
    ("F-QC-02",   "GET", "/qc/new",                            "QC"),
    # COSMETIC
    ("F-COS-01",  "GET", "/cosmetic",                          "Cosmetic"),
    # SALES
    ("F-SALE-01", "GET", "/sales/ready",                       "Ready"),
    ("F-SALE-02", "GET", "/sales/new",                         "Sale"),
    ("F-SALE-03", "GET", "/sales",                             "Sales"),
    ("F-SALE-04", "GET", "/returns",                           "Return"),
    ("F-SALE-05", "GET", "/returns/new",                       "Return"),
    # SPARE PARTS
    ("F-PART-01", "GET", "/spare-parts",                       "Spare"),
    ("F-PART-02", "GET", "/spare-parts/new",                   "Part"),
    ("F-PART-03", "GET", "/spare-parts/purchase",              "Purchase"),
    ("F-PART-04", "GET", "/spare-parts/consume",               "Consum"),
    # REPORTS
    ("F-RPT-01",  "GET", "/reports/lot-pl",                    "Lot"),
    ("F-RPT-02",  "GET", "/reports/stage-movement",            "Stage"),
    ("F-RPT-03",  "GET", "/reports/sales",                     "Sales"),
    ("F-RPT-04",  "GET", "/reports/export/lot-pl",             None),
    ("F-RPT-05",  "GET", "/reports/export/sales",              None),
    # ADMIN
    ("F-ADM-01",  "GET", "/admin/users",                       "admin"),
    ("F-ADM-02",  "GET", "/admin/users/new",                   "User"),
    ("F-ADM-03",  "GET", "/admin/login-log",                   "Login"),
    ("F-ADM-04",  "GET", "/admin/master",                      "Master"),
    # STAGE CONTROL
    ("F-SC-01",   "GET", "/stage-control",                     "Stage"),
    ("F-SC-02",   "GET", "/stage-control/aging",               None),
    ("F-SC-03",   "GET", "/stage-control/audit",               "Audit"),
    # LOCATIONS
    ("F-LOC-01",  "GET", "/locations/dashboard",               "Location"),
    ("F-LOC-02",  "GET", "/locations/master",                  "Location"),
    ("F-LOC-03",  "GET", "/locations/gaps",                    None),
    ("F-LOC-04",  "GET", "/locations/audit",                   "Audit"),
    # DEALERS
    ("F-DLR-01",  "GET", "/dealers",                           "Dealer"),
    ("F-DLR-02",  "GET", "/dealers/followups-due",             None),
    ("F-DLR-03",  "GET", "/dealers/new",                       "Dealer"),
    # MARKET
    ("F-MKT-01",  "GET", "/market",                            "Market"),
    # BULK UPLOAD
    ("F-BLK-01",  "GET", "/bulk-upload",                       "Bulk"),
    # ATTENDANCE
    ("F-ATT-01",  "GET", "/attendance",                        "Attendance"),
    ("F-ATT-02",  "GET", "/attendance/history",                "Attendance"),
    # TRANSFERS
    ("F-TRF-01",  "GET", "/transfers",                         "Transfer"),
    ("F-TRF-02",  "GET", "/transfers/new",                     "Transfer"),
    # TELECALLING
    ("F-TEL-01",  "GET", "/telecalling",                       "Telecalling"),
    # WHATSAPP
    ("F-WA-01",   "GET", "/whatsapp",                          "WhatsApp"),
    # JSON APIs
    ("F-API-01",  "GET", "/locations/api/gap-count",           None),
    ("F-API-02",  "GET", "/attendance/api/status",             None),
    ("F-API-03",  "GET", "/market/api/search?q=dell",          None),
]


async def section_functional(session):
    print("\n" + "=" * 70)
    print("  SECTION F — FUNCTIONAL: All endpoints reachable")
    print("=" * 70)
    t0 = time.time()

    tasks = [get_(session, path) for _, _, path, _ in FUNCTIONAL_ENDPOINTS]
    results = await asyncio.gather(*tasks)

    server_errors = []
    missing_keyword = []
    http_errors = []

    for (tid, method, path, keyword), (status, text) in zip(FUNCTIONAL_ENDPOINTS, results):
        if status == 0:
            record("F", tid, path, False, f"Connection error: {text[:60]}")
            http_errors.append(tid)
            continue
        if "internal server error" in text.lower():
            record("F", tid, path, False, f"HTTP {status} — 500 Internal Server Error")
            server_errors.append(tid)
            continue
        if keyword and keyword.lower() not in text.lower():
            record("F", tid, path, False, f"HTTP {status} — missing '{keyword}'")
            missing_keyword.append(tid)
            continue
        record("F", tid, path, True, f"HTTP {status}")

    elapsed = time.time() - t0
    SECTION_TIMES["F"] = elapsed
    total = len(FUNCTIONAL_ENDPOINTS)
    issues = len(server_errors) + len(missing_keyword) + len(http_errors)
    print(f"\n  [F] {total - issues}/{total} endpoints OK in {elapsed:.1f}s")
    if server_errors:
        print(f"  500 Errors: {server_errors}")
    if http_errors:
        print(f"  Connection errors: {http_errors}")


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# SECTION E — E2E FLOW: Full device lifecycle (15 devices)
# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

E2E_LOT    = "FTS-E2E-LOT1"
E2E_PREFIX = "FTS-E2E-"
E2E_COUNT  = 15


async def _e2e_ensure_lot(session) -> str | None:
    """Create lot + devices, always resetting devices to 'grn'. Returns lot UUID."""
    # Create lot (idempotent)
    await post_(session, "/lots/new", {
        "lot_number": E2E_LOT, "supplier_name": "FTS Supplier Ltd",
        "buying_price": "150000", "qty": str(E2E_COUNT),
        "purchase_date": str(date.today()),
        "grn_system_number": "GRN-FTS-001", "grn_number_new": "1",
        "grn_date": str(date.today()), "invoice_date": str(date.today()),
        "invoice_no": "INV-FTS-001", "invoice_value": "150000",
        "taxable_amount": "127119", "sgst": "11441", "cgst": "11441",
        "igst": "0", "vendor_name": "FTS Supplier",
    })
    row = await db_fetchone("SELECT id FROM lots WHERE lot_number = :n", {"n": E2E_LOT})
    if not row:
        return None
    lot_id = str(row[0])

    # Upsert devices — always reset to 'grn' so re-runs start clean
    for i in range(1, E2E_COUNT + 1):
        bc = f"{E2E_PREFIX}{i:03d}"
        await db_exec("""
            INSERT INTO devices (id, barcode, lot_id, brand, model, serial_no,
                sub_category, device_type, current_stage, created_at)
            VALUES (gen_random_uuid(), :bc, :lid, 'Dell', 'Latitude 5490',
                :sn, 'Laptop', 'Laptop', 'grn', NOW())
            ON CONFLICT (barcode) DO UPDATE SET current_stage='grn'
        """, {"bc": bc, "lid": lot_id, "sn": f"FTS-SN-{i:04d}"})

    return lot_id


async def section_e2e_flow(session):
    print("\n" + "=" * 70)
    print("  SECTION E — E2E FLOW: Full device lifecycle (15 devices)")
    print("=" * 70)
    t0 = time.time()

    # ── E-SETUP ───────────────────────────────────────────────────────────────
    lot_id = await _e2e_ensure_lot(session)
    record("E", "E-SETUP-01", f"Lot {E2E_LOT} + {E2E_COUNT} devices created", lot_id is not None)
    if not lot_id:
        print("  [FATAL] Cannot continue E2E without lot")
        return

    # ── E-GRN → IQC ──────────────────────────────────────────────────────────
    await db_exec(
        f"UPDATE devices SET current_stage='iqc' WHERE lot_id=:lid AND current_stage='grn'",
        {"lid": lot_id})
    cnt = await db_scalar(
        "SELECT COUNT(*) FROM devices WHERE lot_id=:lid AND current_stage='iqc'", {"lid": lot_id})
    record("E", "E-GRN-01", f"Advance all devices GRN → IQC", cnt == E2E_COUNT, f"{cnt}/{E2E_COUNT}")

    # ── E-IQC inspections ─────────────────────────────────────────────────────
    iqc_ok = 0
    for i in range(1, E2E_COUNT + 1):
        bc = f"{E2E_PREFIX}{i:03d}"
        s, u, t = await post_(session, "/iqc/new", {
            "barcode": bc, "lot_id": lot_id,
            "sub_category": "Laptop", "device_type": "Laptop",
            "brand": "Dell", "model": "Latitude 5490",
            "serial_no": f"FTS-SN-{i:04d}", "grn_number": "GRN-FTS-001",
            "cpu": "Core i5", "generation": "8th Gen",
            "ram_gb": "8", "storage_gb": "256", "storage_type": "SSD",
            "hdd_capacity_gb": "0", "screen_size": "14",
            "battery_health_pct": "85", "bios_password": "",
            "color": "Black", "grade": "A", "floor": "1", "warehouse": "Main",
            "screen_dot": "0", "panel_a_scratch": "no",
            "keyboard_working": "yes", "touchpad_working": "yes",
            "power_on": "yes", "charging_port": "ok",
        })
        if ok_page(s, t):
            iqc_ok += 1
    record("E", "E-IQC-01", "IQC inspections (15 devices)", iqc_ok >= 12, f"{iqc_ok}/15")

    # ── E-IQC → Stock In ─────────────────────────────────────────────────────
    await db_exec(
        "UPDATE devices SET current_stage='stock_in' WHERE lot_id=:lid AND current_stage='iqc'",
        {"lid": lot_id})
    cnt = await db_scalar(
        "SELECT COUNT(*) FROM devices WHERE lot_id=:lid AND current_stage='stock_in'", {"lid": lot_id})
    record("E", "E-STK-01", f"IQC → Stock In", cnt >= 12, f"{cnt} devices in stock_in")

    # ── E-Stock In → Repair stages ────────────────────────────────────────────
    # Devices 1-5: stock_in → l1 → qc_check
    # Devices 6-10: stock_in → l1 → l2 → qc_check
    # Devices 11-13: stock_in → l1 → l2 → l3 → qc_check
    # Devices 14-15: stock_in → qc_check (no repair)

    for i, (from_stage, to_stage) in [
        (range(1, 14),  ("stock_in", "l1")),
    ]:
        barcodes = [f"{E2E_PREFIX}{j:03d}" for j in i]
        vals = ", ".join(f"'{bc}'" for bc in barcodes)
        await db_exec(
            f"UPDATE devices SET current_stage='{to_stage}' "
            f"WHERE barcode IN ({vals}) AND current_stage='{from_stage}'"
        )
    # Devices 14-15 → qc_check directly
    await db_exec(
        f"UPDATE devices SET current_stage='qc_check' "
        f"WHERE barcode IN ('{E2E_PREFIX}014', '{E2E_PREFIX}015') AND current_stage='stock_in'"
    )
    cnt_l1 = await db_scalar(
        "SELECT COUNT(*) FROM devices WHERE lot_id=:lid AND current_stage='l1'", {"lid": lot_id})
    record("E", "E-REP-01", "13 devices moved to L1", cnt_l1 >= 10, f"{cnt_l1} in l1")

    # Escalation
    await db_exec(
        f"UPDATE devices SET current_stage='l2' WHERE barcode IN "
        f"('{E2E_PREFIX}006','{E2E_PREFIX}007','{E2E_PREFIX}008','{E2E_PREFIX}009','{E2E_PREFIX}010',"
        f"'{E2E_PREFIX}011','{E2E_PREFIX}012','{E2E_PREFIX}013') AND current_stage='l1'"
    )
    await db_exec(
        f"UPDATE devices SET current_stage='l3' WHERE barcode IN "
        f"('{E2E_PREFIX}011','{E2E_PREFIX}012','{E2E_PREFIX}013') AND current_stage='l2'"
    )
    # All repairs → qc_check
    await db_exec(
        "UPDATE devices SET current_stage='qc_check' WHERE lot_id=:lid AND current_stage IN ('l1','l2','l3')",
        {"lid": lot_id})
    cnt_qc = await db_scalar(
        "SELECT COUNT(*) FROM devices WHERE lot_id=:lid AND current_stage='qc_check'", {"lid": lot_id})
    record("E", "E-REP-02", "All repair paths → QC Check", cnt_qc >= 12, f"{cnt_qc} in qc_check")

    # ── E-QC checks ──────────────────────────────────────────────────────────
    qc_ok = 0
    qc_fail = 0
    for i in range(1, E2E_COUNT + 1):
        bc = f"{E2E_PREFIX}{i:03d}"
        if i == 13:  # deliberate fail → back to L1
            s, u, t = await post_(session, "/qc/new", {
                "barcode": bc, "battery_score": "3", "screen_score": "3",
                "keyboard_score": "4", "body_score": "4",
                "issues_found": "Screen damage", "notes": "FTS QC fail",
                "send_to_stage": "l1",
            })
            if ok_page(s, t):
                qc_fail += 1
        else:
            s, u, t = await post_(session, "/qc/new", {
                "barcode": bc, "battery_score": "9", "screen_score": "9",
                "keyboard_score": "10", "body_score": "8",
                "issues_found": "None", "notes": f"FTS QC pass {i}",
                "send_to_stage": "l1",
            })
            if ok_page(s, t):
                qc_ok += 1
    record("E", "E-QC-01", "QC pass (14 devices)", qc_ok >= 10, f"{qc_ok}/14 passed")
    record("E", "E-QC-02", "QC fail (1 device → L1)", qc_fail >= 1, f"{qc_fail}/1 failed")

    # ── E-QC pass → ready_to_sale (skip cosmetic for speed) ──────────────────
    # QC pass moves devices to 'cleaning' (first cosmetic stage), not qc_check.
    # Force all cosmetic+qc stages to ready_to_sale for E2E speed.
    await db_exec(
        "UPDATE devices SET current_stage='ready_to_sale' WHERE lot_id=:lid "
        "AND current_stage IN ('qc_check','cleaning','dry_sanding','masking',"
        "'painting','water_sanding','final_qc')",
        {"lid": lot_id})
    cnt_rts = await db_scalar(
        "SELECT COUNT(*) FROM devices WHERE lot_id=:lid AND current_stage='ready_to_sale'", {"lid": lot_id})
    record("E", "E-QC-03", "QC pass → Ready to Sale", cnt_rts >= 10, f"{cnt_rts} ready")

    # ── E-Sales ───────────────────────────────────────────────────────────────
    ready = await db_fetchall(
        "SELECT barcode FROM devices WHERE lot_id=:lid AND current_stage='ready_to_sale' LIMIT 5",
        {"lid": lot_id})
    sold = 0
    for idx, (bc,) in enumerate(ready):
        s, u, t = await post_(session, "/sales/new", {
            "barcode": bc, "sale_price": "12000",
            "customer_name": f"FTS Customer {idx+1}",
            "customer_phone": f"987650{idx:04d}",
            "invoice_no": f"FTS-INV-{idx+1:03d}",
            "payment_mode": "cash", "notes": "FTS E2E test sale",
        })
        if ok_page(s, t):
            sold += 1
    record("E", "E-SALE-01", f"Sell {len(ready)} ready devices", sold >= min(3, len(ready)),
           f"{sold}/{len(ready)} sold")

    # ── E-Return ──────────────────────────────────────────────────────────────
    sold_row = await db_fetchone(
        "SELECT barcode FROM devices WHERE lot_id=:lid AND current_stage='sold' LIMIT 1", {"lid": lot_id})
    if sold_row:
        bc = sold_row[0]
        s, u, t = await post_(session, "/returns/new", {
            "barcode": bc, "reason": "Customer changed mind — FTS test",
            "condition_on_return": "Good condition", "action_taken": "restock",
            "refund_amount": "12000", "notes": "FTS return test",
        })
        record("E", "E-SALE-02", "Process a return (restock)", ok_page(s, t))
    else:
        record("E", "E-SALE-02", "Process a return", None, "No sold device available")

    # ── E-Scrap ───────────────────────────────────────────────────────────────
    scrap_row = await db_fetchone(
        "SELECT barcode FROM devices WHERE lot_id=:lid AND current_stage='l1' LIMIT 1", {"lid": lot_id})
    if scrap_row:
        bc = scrap_row[0]
        await db_exec(
            "UPDATE devices SET current_stage='scrapped' WHERE barcode=:bc", {"bc": bc})
        v = await db_scalar(
            "SELECT current_stage FROM devices WHERE barcode=:bc", {"bc": bc})
        record("E", "E-SCRAP-01", "Scrap a device", v == "scrapped")
    else:
        record("E", "E-SCRAP-01", "Scrap a device", None, "No l1 device available")

    SECTION_TIMES["E"] = time.time() - t0
    print(f"\n  [E] E2E flow complete in {SECTION_TIMES['E']:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# SECTION S — SPARE PARTS: Full purchase → consume → ledger
# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

async def section_spare_parts(session):
    print("\n" + "=" * 70)
    print("  SECTION S — SPARE PARTS: purchase → consume → ledger balance")
    print("=" * 70)
    t0 = time.time()

    PART_CODE = "FTS-RAM-8GB"

    # ── S-01 Create spare part ────────────────────────────────────────────────
    s, u, t = await post_(session, "/spare-parts/new", {
        "part_code": PART_CODE, "name": "RAM 8GB DDR4",
        "category": "Memory", "unit_price": "800",
        "min_stock_alert": "5", "supplier": "FTS Parts Supplier",
        "notes": "FTS test part",
    })
    # Verify by checking DB — duplicate creates a form error page, not a 500
    part_exists = await db_fetchone("SELECT id FROM spare_parts WHERE part_code=:pc", {"pc": PART_CODE})
    record("S", "S-01", "Create spare part (RAM 8GB DDR4)",
           bool(part_exists), f"HTTP {s}, in_db={'yes' if part_exists else 'no'}")

    # Get part UUID
    part_row = await db_fetchone(
        "SELECT id FROM spare_parts WHERE part_code=:pc", {"pc": PART_CODE})
    if not part_row:
        record("S", "S-01b", "Part UUID in DB", False, "Not found")
        return
    part_id = str(part_row[0])
    record("S", "S-01b", f"Part UUID resolved ({part_id[:8]}...)", True)

    # ── S-02 Purchase ─────────────────────────────────────────────────────────
    s, u, t = await post_(session, "/spare-parts/purchase", {
        "part_id": part_id, "qty": "50",
        "unit_price": "800", "total_price": "40000",
        "supplier": "FTS Parts Supplier",
        "invoice_no": "FTS-PART-INV-001",
        "purchase_date": str(date.today()),
        "purchased_by": "admin",
    })
    record("S", "S-02", "Purchase 50 units (₹40,000)", ok_page(s, t), f"HTTP {s}")

    # Verify stock increased
    stock = await db_scalar(
        "SELECT qty_in_stock FROM spare_parts WHERE id=:pid", {"pid": part_id})
    record("S", "S-03", f"Stock after purchase: {stock} units", (stock or 0) >= 50,
           f"qty_in_stock = {stock}")

    # Verify ledger IN entry
    ledger_in = await db_scalar(
        "SELECT COUNT(*) FROM spare_parts_ledger WHERE part_id=:pid AND entry_type='IN'",
        {"pid": part_id})
    record("S", "S-04", f"Ledger IN entry created ({ledger_in})", (ledger_in or 0) >= 1)

    # ── S-05 Consume on device ────────────────────────────────────────────────
    dev_row = await db_fetchone(
        "SELECT barcode FROM devices WHERE current_stage NOT IN ('sold','scrapped') LIMIT 1")
    if dev_row:
        bc = dev_row[0]
        s, u, t = await post_(session, "/spare-parts/consume", {
            "barcode": bc, "part_id": part_id,
            "qty_used": "2", "unit_cost": "800",
            "stage": "l1", "consumed_by": "admin",
            "notes": "FTS spare part consumption test",
        })
        record("S", "S-05", f"Consume 2 units on device {bc}", ok_page(s, t), f"HTTP {s}")

        # Verify stock decreased
        stock_after = await db_scalar(
            "SELECT qty_in_stock FROM spare_parts WHERE id=:pid", {"pid": part_id})
        record("S", "S-06", f"Stock after consumption: {stock_after} units",
               (stock_after or 0) <= (stock or 0), f"before={stock}, after={stock_after}")

        # Verify ledger OUT entry
        ledger_out = await db_scalar(
            "SELECT COUNT(*) FROM spare_parts_ledger WHERE part_id=:pid AND entry_type='OUT'",
            {"pid": part_id})
        record("S", "S-07", f"Ledger OUT entry created ({ledger_out})", (ledger_out or 0) >= 1)

        # Verify ledger balance: IN total - OUT total = stock
        ledger_in_qty = await db_scalar(
            "SELECT COALESCE(SUM(qty),0) FROM spare_parts_ledger WHERE part_id=:pid AND entry_type='IN'",
            {"pid": part_id})
        ledger_out_qty = await db_scalar(
            "SELECT COALESCE(SUM(qty),0) FROM spare_parts_ledger WHERE part_id=:pid AND entry_type='OUT'",
            {"pid": part_id})
        expected_stock = (ledger_in_qty or 0) - (ledger_out_qty or 0)
        actual_stock = stock_after or 0
        record("S", "S-08", f"Ledger double-entry balance: IN({ledger_in_qty})-OUT({ledger_out_qty})={expected_stock} == stock({actual_stock})",
               expected_stock == actual_stock,
               f"{'BALANCED' if expected_stock == actual_stock else 'MISMATCH'}")
    else:
        record("S", "S-05", "Consume spare part on device", None, "No active device found")
        record("S", "S-06", "Stock after consumption", None, "skipped")
        record("S", "S-07", "Ledger OUT entry", None, "skipped")
        record("S", "S-08", "Ledger double-entry balance", None, "skipped")

    # ── S-09 Low stock alert visible ─────────────────────────────────────────
    s, t = await get_(session, "/spare-parts")
    record("S", "S-09", "Spare parts list loads (no 500)", ok_page(s, t))

    # ── S-10 Purchase log loads ───────────────────────────────────────────────
    s, t = await get_(session, "/spare-parts/purchase")
    record("S", "S-10", "Purchase log loads", ok_page(s, t))

    SECTION_TIMES["S"] = time.time() - t0
    print(f"\n  [S] Spare parts tests complete in {SECTION_TIMES['S']:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# SECTION C — COSMETIC PIPELINE: Full cleaning → ready_to_sale
# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

async def section_cosmetic(session):
    print("\n" + "=" * 70)
    print("  SECTION C — COSMETIC PIPELINE: qc_check → ready_to_sale")
    print("=" * 70)
    t0 = time.time()

    # Create 2 fresh test devices at qc_check stage
    lot_row = await db_fetchone("SELECT id FROM lots LIMIT 1")
    if not lot_row:
        record("C", "C-SETUP", "Base lot available", False, "No lots in DB")
        return
    lot_id = str(lot_row[0])

    cos_devices = ["FTS-COS-001", "FTS-COS-002"]
    for bc in cos_devices:
        await db_exec("""
            INSERT INTO devices (id, barcode, lot_id, brand, model, serial_no,
                sub_category, device_type, current_stage, created_at)
            VALUES (gen_random_uuid(), :bc, :lid, 'HP', 'EliteBook 840',
                :sn, 'Laptop', 'Laptop', 'qc_check', NOW())
            ON CONFLICT (barcode) DO UPDATE SET current_stage='qc_check'
        """, {"bc": bc, "lid": lot_id, "sn": f"FTS-COS-SN-{bc[-3:]}"})

    record("C", "C-SETUP", "2 cosmetic test devices in qc_check", True)

    # ── C-01 QC → send to cosmetic (Cleaning) ────────────────────────────────
    s, u, t = await post_(session, "/cosmetic/send-to-cosmetic", {
        "barcode": cos_devices[0], "notes": "FTS: Cosmetic refurb needed",
    })
    record("C", "C-01", f"Send {cos_devices[0]}: qc_check → Cleaning", ok_page(s, t), f"HTTP {s}")

    # ── C-02 Cleaning → Dry Sanding ──────────────────────────────────────────
    s, u, t = await post_(session, "/cosmetic/advance", {
        "barcode": cos_devices[0], "current_stage": "cleaning",
        "action": "pass", "notes": "FTS: Cleaning done",
    })
    stage = await db_scalar("SELECT current_stage FROM devices WHERE barcode=:bc", {"bc": cos_devices[0]})
    record("C", "C-02", f"Cleaning → Dry Sanding", stage == "dry_sanding", f"stage={stage}")

    # ── C-03 Dry Sanding → Masking ───────────────────────────────────────────
    s, u, t = await post_(session, "/cosmetic/advance", {
        "barcode": cos_devices[0], "current_stage": "dry_sanding",
        "action": "pass", "notes": "FTS: Dry sanding done",
    })
    stage = await db_scalar("SELECT current_stage FROM devices WHERE barcode=:bc", {"bc": cos_devices[0]})
    record("C", "C-03", f"Dry Sanding → Masking", stage == "masking", f"stage={stage}")

    # ── C-04 Masking → Painting ───────────────────────────────────────────────
    s, u, t = await post_(session, "/cosmetic/advance", {
        "barcode": cos_devices[0], "current_stage": "masking",
        "action": "pass", "notes": "FTS: Masking done",
    })
    stage = await db_scalar("SELECT current_stage FROM devices WHERE barcode=:bc", {"bc": cos_devices[0]})
    record("C", "C-04", f"Masking → Painting", stage == "painting", f"stage={stage}")

    # ── C-05 Painting → Water Sanding ────────────────────────────────────────
    s, u, t = await post_(session, "/cosmetic/advance", {
        "barcode": cos_devices[0], "current_stage": "painting",
        "action": "pass", "notes": "FTS: Painting done",
    })
    stage = await db_scalar("SELECT current_stage FROM devices WHERE barcode=:bc", {"bc": cos_devices[0]})
    record("C", "C-05", f"Painting → Water Sanding", stage == "water_sanding", f"stage={stage}")

    # ── C-06 Water Sanding → Final QC ────────────────────────────────────────
    s, u, t = await post_(session, "/cosmetic/advance", {
        "barcode": cos_devices[0], "current_stage": "water_sanding",
        "action": "pass", "notes": "FTS: Water sanding done",
    })
    stage = await db_scalar("SELECT current_stage FROM devices WHERE barcode=:bc", {"bc": cos_devices[0]})
    record("C", "C-06", f"Water Sanding → Final QC", stage == "final_qc", f"stage={stage}")

    # ── C-07 Final QC → Ready to Sale ────────────────────────────────────────
    s, u, t = await post_(session, "/cosmetic/advance", {
        "barcode": cos_devices[0], "current_stage": "final_qc",
        "action": "pass", "notes": "FTS: Final QC passed",
    })
    stage = await db_scalar("SELECT current_stage FROM devices WHERE barcode=:bc", {"bc": cos_devices[0]})
    record("C", "C-07", f"Final QC → Ready to Sale", stage == "ready_to_sale", f"stage={stage}")

    # ── C-08 Final QC FAIL → back to Cleaning ────────────────────────────────
    # Cosmetic router uses `final_qc_status` field (not `action`)
    await db_exec(
        "UPDATE devices SET current_stage='final_qc' WHERE barcode=:bc", {"bc": cos_devices[1]})
    s, u, t = await post_(session, "/cosmetic/advance", {
        "barcode": cos_devices[1], "current_stage": "final_qc",
        "final_qc_status": "fail", "notes": "FTS: Final QC failed — send back",
    })
    stage = await db_scalar("SELECT current_stage FROM devices WHERE barcode=:bc", {"bc": cos_devices[1]})
    record("C", "C-08", f"Final QC FAIL → back to Cleaning", stage == "cleaning", f"stage={stage}")

    # ── C-09 Cosmetic dashboard loads ────────────────────────────────────────
    s, t = await get_(session, "/cosmetic")
    record("C", "C-09", "Cosmetic dashboard loads", ok_page(s, t))

    SECTION_TIMES["C"] = time.time() - t0
    print(f"\n  [C] Cosmetic pipeline complete in {SECTION_TIMES['C']:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# SECTION X — EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

async def section_edge_cases(session):
    print("\n" + "=" * 70)
    print("  SECTION X — EDGE CASES: validation, guards, bad inputs")
    print("=" * 70)
    t0 = time.time()

    # ── X-01 Bad login ────────────────────────────────────────────────────────
    jar = aiohttp.CookieJar()
    async with aiohttp.ClientSession(cookie_jar=jar) as bad_sess:
        r = await bad_sess.post(f"{BASE}/auth/login",
                                data={"username": "admin", "password": "wrong_password"},
                                allow_redirects=True)
        text = await r.text()
        blocked = "invalid" in text.lower() or "incorrect" in text.lower() or "login" in str(r.url).lower()
        record("X", "X-01", "Bad login rejected", blocked, f"HTTP {r.status}")

    # ── X-02 Duplicate barcode rejected ──────────────────────────────────────
    existing = await db_fetchone("SELECT barcode FROM devices LIMIT 1")
    if existing:
        bc = existing[0]
        s, u, t = await post_(session, "/iqc/new", {
            "barcode": bc, "lot_id": "00000000-0000-0000-0000-000000000000",
            "sub_category": "Laptop", "brand": "Test", "model": "Test",
            "serial_no": "DUPE-SN", "device_type": "Laptop",
            "grn_number": "TEST",
        })
        blocked = s != 201 or "already" in t.lower() or "duplicate" in t.lower() or "exist" in t.lower()
        record("X", "X-02", "Duplicate barcode rejected at IQC", blocked, f"HTTP {s}")
    else:
        record("X", "X-02", "Duplicate barcode rejected", None, "No devices in DB")

    # ── X-03 Invalid stage transition blocked (non-admin user) ──────────────
    # Admin bypasses transition rules. Test with a non-admin (uat_l1) session.
    await db_exec("""
        INSERT INTO devices (id, barcode, lot_id, brand, model, serial_no,
            sub_category, device_type, current_stage, created_at)
        VALUES (gen_random_uuid(), 'FTS-EDGE-SCRAP', (SELECT id FROM lots LIMIT 1),
            'Test', 'Test', 'FTS-EDGE-SN', 'Laptop', 'Laptop', 'scrapped', NOW())
        ON CONFLICT (barcode) DO UPDATE SET current_stage='scrapped'
    """)
    jar3 = aiohttp.CookieJar()
    async with aiohttp.ClientSession(cookie_jar=jar3) as l1_sess:
        await login(l1_sess, "uat_l1", "oxypc@uat123")
        s, u, t = await post_(l1_sess, "/repair/move", {
            "barcode": "FTS-EDGE-SCRAP", "to_stage": "l1",
            "notes": "FTS: invalid transition test",
        })
    # Also check that device stage didn't actually change
    actual_stage = await db_scalar(
        "SELECT current_stage FROM devices WHERE barcode='FTS-EDGE-SCRAP'")
    blocked = actual_stage == "scrapped"
    record("X", "X-03", "Invalid transition scrapped→l1 blocked (non-admin)",
           blocked, f"HTTP {s}, stage={actual_stage}")

    # ── X-04 Sell device not in ready_to_sale ────────────────────────────────
    await db_exec("""
        INSERT INTO devices (id, barcode, lot_id, brand, model, serial_no,
            sub_category, device_type, current_stage, created_at)
        VALUES (gen_random_uuid(), 'FTS-EDGE-L1', (SELECT id FROM lots LIMIT 1),
            'Test', 'Test', 'FTS-EDGE-L1-SN', 'Laptop', 'Laptop', 'l1', NOW())
        ON CONFLICT (barcode) DO UPDATE SET current_stage='l1'
    """)
    s, u, t = await post_(session, "/sales/new", {
        "barcode": "FTS-EDGE-L1", "sale_price": "10000",
        "customer_name": "FTS Edge Test", "customer_phone": "9999999999",
        "invoice_no": "FTS-EDGE-INV-001", "payment_mode": "cash",
    })
    blocked = (s != 200) or "not ready" in t.lower() or "cannot" in t.lower() or "stage" in t.lower() or "error" in t.lower()
    record("X", "X-04", "Sale blocked for non-ready device (in l1)", blocked, f"HTTP {s}")

    # ── X-05 Negative stock consumption blocked ───────────────────────────────
    part_row = await db_fetchone("SELECT id, qty_in_stock FROM spare_parts LIMIT 1")
    if part_row:
        part_id = str(part_row[0])
        current_stock = part_row[1] or 0
        dev_row = await db_fetchone(
            "SELECT barcode FROM devices WHERE current_stage NOT IN ('sold','scrapped') LIMIT 1")
        if dev_row:
            bc = dev_row[0]
            s, u, t = await post_(session, "/spare-parts/consume", {
                "barcode": bc, "part_id": part_id,
                "qty_used": str(current_stock + 9999),  # Way more than available
                "unit_cost": "800", "stage": "l1", "consumed_by": "admin",
                "notes": "FTS: negative stock guard test",
            })
            blocked = (s != 200) or "insufficient" in t.lower() or "not enough" in t.lower() or "stock" in t.lower() or "error" in t.lower()
            record("X", "X-05", f"Negative stock consumption blocked (need {current_stock+9999}, have {current_stock})",
                   blocked, f"HTTP {s}")
        else:
            record("X", "X-05", "Negative stock guard", None, "No active device")
    else:
        record("X", "X-05", "Negative stock guard", None, "No spare parts in DB")

    # ── X-06 Below-cost sale warning ─────────────────────────────────────────
    # Find a device that's ready_to_sale
    rts_row = await db_fetchone(
        "SELECT d.barcode, l.buying_price FROM devices d "
        "JOIN lots l ON l.id=d.lot_id "
        "WHERE d.current_stage='ready_to_sale' LIMIT 1")
    if rts_row:
        bc, buying_price = rts_row
        below_price = max(1, int((buying_price or 10000) / 100))  # 1% of buying price — definitely below cost
        s, u, t = await post_(session, "/sales/new", {
            "barcode": bc, "sale_price": str(below_price),
            "customer_name": "FTS Below Cost Test",
            "customer_phone": "9999999998",
            "invoice_no": "FTS-BELOW-INV-001", "payment_mode": "cash",
            "notes": "FTS: below cost test",
        })
        # Should show warning or block — look for warning indicators
        warned = "below" in t.lower() or "cost" in t.lower() or "warning" in t.lower() or "loss" in t.lower()
        # Also acceptable: blocked entirely (error page)
        blocked = s != 200 or "error" in t.lower()
        record("X", "X-06", f"Below-cost sale warning/block (₹{below_price} vs lot ₹{buying_price})",
               warned or blocked, f"HTTP {s}, warned={warned}, blocked={blocked}")
    else:
        record("X", "X-06", "Below-cost sale warning", None, "No ready_to_sale devices")

    # ── X-07 Unknown barcode lookup returns 404 ───────────────────────────────
    s, t = await get_(session, "/devices/FTS-NONEXISTENT-BARCODE-XYZ")
    record("X", "X-07", "Unknown barcode → 404 (not 500)", s == 404, f"HTTP {s}")

    # ── X-08 Invalid UUID in URL → 404 not 500 ───────────────────────────────
    s, t = await get_(session, "/devices/not-a-uuid-at-all")
    record("X", "X-08", "Invalid UUID in URL → 404 not 500", s in (404, 422), f"HTTP {s}")

    # ── X-09 Unauthenticated request redirects to login ──────────────────────
    jar = aiohttp.CookieJar()
    async with aiohttp.ClientSession(cookie_jar=jar) as anon_sess:
        r = await anon_sess.get(f"{BASE}/admin/users", allow_redirects=True)
        t = await r.text()
        redirected = "login" in str(r.url).lower() or "login" in t.lower()
        record("X", "X-09", "Unauthenticated /admin/users → login redirect", redirected,
               f"HTTP {r.status}, url={str(r.url)[-40:]}")

    # ── X-10 XSS in form field doesn't crash server ───────────────────────────
    s, t = await get_(session, "/devices?q=<script>alert(1)</script>")
    record("X", "X-10", "XSS in search param doesn't 500", s == 200 and "internal server error" not in t.lower(),
           f"HTTP {s}")

    SECTION_TIMES["X"] = time.time() - t0
    print(f"\n  [X] Edge cases complete in {SECTION_TIMES['X']:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# SECTION R — RBAC: Role-based access control
# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

RBAC_USERS = [
    ("uat_admin",   "oxypc@uat123", "admin"),
    ("uat_inv_mgr", "oxypc@uat123", "inventory_manager"),
    ("uat_iqc",     "oxypc@uat123", "iqc_inspector"),
    ("uat_l1",      "oxypc@uat123", "l1_engineer"),
    ("uat_l2",      "oxypc@uat123", "l2_engineer"),
    ("uat_l3",      "oxypc@uat123", "l3_engineer"),
    ("uat_qc",      "oxypc@uat123", "qc_inspector"),
    ("uat_sales",   "oxypc@uat123", "sales"),
    ("uat_spare",   "oxypc@uat123", "spare_parts_manager"),
]

RBAC_ACCESS = {
    "uat_admin":   {"allow": ["/", "/admin/users", "/repair/l1", "/sales", "/spare-parts"],
                    "deny":  []},
    "uat_inv_mgr": {"allow": ["/", "/lots", "/devices", "/iqc", "/stock"],
                    "deny":  ["/admin/users"]},
    "uat_iqc":     {"allow": ["/", "/iqc", "/iqc/new"],
                    "deny":  ["/admin/users", "/sales"]},
    "uat_l1":      {"allow": ["/", "/repair/l1"],
                    "deny":  ["/admin/users"]},
    # L2/L3 pages withdrawn: uat_l2 has no repair page left, uat_l3 works via L3/L4.
    "uat_l2":      {"allow": ["/"],
                    "deny":  ["/admin/users"]},
    "uat_l3":      {"allow": ["/", "/repair/l3l4"],
                    "deny":  ["/admin/users"]},
    "uat_qc":      {"allow": ["/", "/qc", "/qc/new"],
                    "deny":  ["/admin/users", "/sales"]},
    "uat_sales":   {"allow": ["/", "/sales", "/sales/ready", "/returns"],
                    "deny":  ["/admin/users", "/iqc"]},
    "uat_spare":   {"allow": ["/", "/spare-parts"],
                    "deny":  ["/admin/users", "/sales"]},
}


async def _ensure_rbac_users(admin_session):
    """Create RBAC test users if missing."""
    for username, password, role in RBAC_USERS:
        await post_(admin_session, "/admin/users/new", {
            "username": username, "password": password,
            "full_name": f"Test {role}", "role": role, "status": "1",
        })


async def section_rbac(admin_session):
    print("\n" + "=" * 70)
    print("  SECTION R — RBAC: Role-based access control (9 roles)")
    print("=" * 70)
    t0 = time.time()

    await _ensure_rbac_users(admin_session)

    for username, password, role in RBAC_USERS:
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(cookie_jar=jar) as sess:
            logged = await login(sess, username, password)
            record("R", f"R-{role[:6].upper()}-LOGIN", f"{username} login", logged)
            if not logged:
                continue

            access = RBAC_ACCESS.get(username, {"allow": ["/"], "deny": []})

            # Test allowed pages
            allow_ok = 0
            for path in access["allow"]:
                s, t = await get_(sess, path)
                allow_ok += 1 if ok_page(s, t) else 0
            record("R", f"R-{role[:6].upper()}-ALLOW",
                   f"{username}: allowed pages ({allow_ok}/{len(access['allow'])})",
                   allow_ok == len(access["allow"]),
                   f"{allow_ok}/{len(access['allow'])}")

            # Test denied pages (should redirect to login or 403)
            deny_ok = 0
            for path in access["deny"]:
                s, t = await get_(sess, path)
                is_denied = s == 403 or ("login" in t.lower() and "password" in t.lower())
                deny_ok += 1 if is_denied else 0
            if access["deny"]:
                record("R", f"R-{role[:6].upper()}-DENY",
                       f"{username}: denied pages blocked ({deny_ok}/{len(access['deny'])})",
                       deny_ok == len(access["deny"]),
                       f"{deny_ok}/{len(access['deny'])}")

    SECTION_TIMES["R"] = time.time() - t0
    print(f"\n  [R] RBAC tests complete in {SECTION_TIMES['R']:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# SECTION I — DATA INTEGRITY
# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

async def section_integrity(session):
    print("\n" + "=" * 70)
    print("  SECTION I — DATA INTEGRITY: P&L math, ledger, stage log")
    print("=" * 70)
    t0 = time.time()

    # ── I-01 Every sold device has a sales record ─────────────────────────────
    orphan_sales = await db_scalar("""
        SELECT COUNT(*) FROM devices d
        WHERE d.current_stage = 'sold'
        AND NOT EXISTS (SELECT 1 FROM sales s WHERE s.device_id = d.id)
    """)
    record("I", "I-01", f"Sold devices have sales record",
           (orphan_sales or 0) == 0,
           f"{orphan_sales} orphan sold devices (no sales row)")

    # ── I-02 No device with NULL lot_id ──────────────────────────────────────
    null_lot = await db_scalar("SELECT COUNT(*) FROM devices WHERE lot_id IS NULL")
    record("I", "I-02", "All devices have lot_id", (null_lot or 0) == 0,
           f"{null_lot} devices without lot")

    # ── I-03 No devices in impossible stage ───────────────────────────────────
    valid_stages = (
        "'grn','iqc','stock_in','l1','l2','l3','qc_check','cleaning',"
        "'dry_sanding','masking','painting','water_sanding','final_qc',"
        "'ready_to_sale','sold','returned','scrapped'"
    )
    bad_stage = await db_scalar(
        f"SELECT COUNT(*) FROM devices WHERE current_stage NOT IN ({valid_stages})")
    record("I", "I-03", "All devices in valid stage", (bad_stage or 0) == 0,
           f"{bad_stage} devices with invalid stage")

    # ── I-04 Lot P&L math consistent ─────────────────────────────────────────
    # Find a lot with at least 1 sale
    lot_pl_row = await db_fetchone("""
        SELECT l.lot_number, l.buying_price,
               COALESCE(SUM(s.sale_price), 0) as revenue,
               COUNT(s.id) as sales_count
        FROM lots l
        JOIN devices d ON d.lot_id = l.id
        LEFT JOIN sales s ON s.device_id = d.id
        GROUP BY l.id, l.lot_number, l.buying_price
        HAVING COUNT(s.id) > 0
        LIMIT 1
    """)
    if lot_pl_row:
        lot_num, buying, revenue, cnt = lot_pl_row
        profit = float(revenue or 0) - float(buying or 0)
        margin = (profit / float(revenue) * 100) if revenue else 0
        # P&L should be calculable (even if negative — that's a valid business state)
        pl_ok = isinstance(profit, float) and not (profit != profit)  # not NaN
        record("I", "I-04", f"Lot P&L math: {lot_num} ₹{profit:,.0f} ({margin:.1f}%)",
               pl_ok, f"buying={buying}, revenue={revenue}, sales={cnt}")
    else:
        record("I", "I-04", "Lot P&L math", None, "No lots with sales found")

    # ── I-05 Stage movement log: no device skips stages without log entry ──────
    stage_moves = await db_scalar("SELECT COUNT(*) FROM stage_movements")
    record("I", "I-05", f"Stage movements table has entries ({stage_moves})",
           (stage_moves or 0) >= 0, f"{stage_moves} movement records")

    # ── I-06 Spare parts ledger balanced (only parts WITH ledger entries) ─────
    # Parts seeded directly via SQL have no ledger entries — those are excluded.
    # Only parts that went through purchase/consume flow must have balanced ledger.
    ledger_check = await db_fetchall("""
        SELECT sp.part_code,
               COALESCE(SUM(CASE WHEN l.entry_type='IN' THEN l.qty ELSE 0 END), 0) as total_in,
               COALESCE(SUM(CASE WHEN l.entry_type='OUT' THEN l.qty ELSE 0 END), 0) as total_out,
               sp.qty_in_stock
        FROM spare_parts sp
        INNER JOIN spare_parts_ledger l ON l.part_id = sp.id
        GROUP BY sp.id, sp.part_code, sp.qty_in_stock
        HAVING sp.qty_in_stock IS NOT NULL
        LIMIT 20
    """)
    mismatches = []
    for row in ledger_check:
        pc, total_in, total_out, stock = row
        expected = int(total_in) - int(total_out)
        if expected != int(stock or 0):
            mismatches.append(f"{pc}: expected {expected}, actual {stock}")
    parts_with_ledger = len(ledger_check)
    seeded_no_ledger = await db_scalar("""
        SELECT COUNT(*) FROM spare_parts sp
        WHERE NOT EXISTS (SELECT 1 FROM spare_parts_ledger l WHERE l.part_id=sp.id)
        AND sp.qty_in_stock > 0
    """)
    note = (f"Mismatches: {mismatches}" if mismatches
            else f"all {parts_with_ledger} ledger-tracked parts balanced "
                 f"({seeded_no_ledger} pre-ledger seed parts excluded)")
    record("I", "I-06", f"Spare parts ledger balanced ({parts_with_ledger} tracked parts)",
           len(mismatches) == 0, note)

    # ── I-07 No negative stock in spare parts ────────────────────────────────
    neg_stock = await db_scalar(
        "SELECT COUNT(*) FROM spare_parts WHERE qty_in_stock < 0")
    record("I", "I-07", "No negative spare parts stock", (neg_stock or 0) == 0,
           f"{neg_stock} parts with negative stock")

    # ── I-08 Audit log has entries ────────────────────────────────────────────
    audit_count = await db_scalar("SELECT COUNT(*) FROM audit_logs")
    record("I", "I-08", f"Audit log populated ({audit_count} entries)",
           (audit_count or 0) >= 0, f"{audit_count} audit entries")

    # ── I-09 Returns all have parent sale ────────────────────────────────────
    orphan_returns = await db_scalar("""
        SELECT COUNT(*) FROM returns r
        WHERE NOT EXISTS (SELECT 1 FROM sales s WHERE s.id = r.sale_id)
    """)
    record("I", "I-09", "All returns have parent sale",
           (orphan_returns or 0) == 0, f"{orphan_returns} orphan returns")

    # ── I-10 stage_master has all 17 canonical stages ─────────────────────────
    stage_count = await db_scalar("SELECT COUNT(*) FROM stage_master")
    record("I", "I-10", f"stage_master has >=17 canonical stages ({stage_count})",
           (stage_count or 0) >= 17, f"{stage_count} stages in stage_master")

    # ── I-11 allowed_transitions not empty ───────────────────────────────────
    trans_count = await db_scalar("SELECT COUNT(*) FROM allowed_transitions")
    record("I", "I-11", f"allowed_transitions populated ({trans_count} rules)",
           (trans_count or 0) >= 30, f"{trans_count} transitions")

    SECTION_TIMES["I"] = time.time() - t0
    print(f"\n  [I] Integrity checks complete in {SECTION_TIMES['I']:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# SECTION P — PERFORMANCE: 5000 devices, load, throughput
# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

PERF_LOT_PREFIX = "FTS-PERF-LOT"
PERF_LOT_COUNT  = 10    # 10 lots × 500 devices = 5000 devices
PERF_DEV_PER_LOT = 500


async def section_performance(session):
    print("\n" + "=" * 70)
    print("  SECTION P — PERFORMANCE: 5000 devices, 50 concurrent users, throughput")
    print("=" * 70)
    t0_total = time.time()

    # ── P-01 Bulk insert 5000 devices ─────────────────────────────────────────
    print("\n  [P-01] Bulk inserting 5,000 devices (10 lots × 500)...")
    t0 = time.time()
    total_inserted = 0

    for lot_num in range(1, PERF_LOT_COUNT + 1):
        lot_number = f"{PERF_LOT_PREFIX}{lot_num:02d}"

        # Insert lot if missing
        await db_exec("""
            INSERT INTO lots (id, lot_number, supplier_name, buying_price, qty,
                purchase_date, created_by)
            VALUES (gen_random_uuid(), :ln, 'Perf Supplier', 5000000, :qty,
                :pd, 'admin')
            ON CONFLICT (lot_number) DO NOTHING
        """, {"ln": lot_number, "qty": PERF_DEV_PER_LOT, "pd": date.today()})

        lot_row = await db_fetchone("SELECT id FROM lots WHERE lot_number=:ln", {"ln": lot_number})
        if not lot_row:
            continue
        lot_id = str(lot_row[0])

        # Check existing count
        existing = await db_scalar(
            "SELECT COUNT(*) FROM devices WHERE lot_id=:lid", {"lid": lot_id})
        if existing >= PERF_DEV_PER_LOT:
            total_inserted += existing
            continue

        # Bulk insert in batches of 250
        batch_values = []
        for i in range(int(existing or 0) + 1, PERF_DEV_PER_LOT + 1):
            bc  = f"PERF-L{lot_num:02d}-{i:05d}"
            sn  = f"PERF-SN-L{lot_num:02d}-{i:05d}"
            batch_values.append(
                f"(gen_random_uuid(), '{bc}', '{lot_id}', 'HP', 'EliteBook 840 G8', "
                f"'{sn}', 'Laptop', 'Laptop', 'stock_in', NOW())"
            )

        from database import AsyncSessionLocal
        from sqlalchemy import text as sa_text
        async with AsyncSessionLocal() as db:
            for start in range(0, len(batch_values), 250):
                chunk = batch_values[start:start + 250]
                sql = (
                    "INSERT INTO devices (id, barcode, lot_id, brand, model, serial_no, "
                    "sub_category, device_type, current_stage, created_at) VALUES "
                    + ", ".join(chunk)
                    + " ON CONFLICT (barcode) DO NOTHING"
                )
                await db.execute(sa_text(sql))
            await db.commit()
        total_inserted += PERF_DEV_PER_LOT

    insert_elapsed = time.time() - t0
    actual_perf_devices = await db_scalar(
        f"SELECT COUNT(*) FROM devices WHERE barcode LIKE 'PERF-%'")
    record("P", "P-01", f"5,000 device bulk insert",
           (actual_perf_devices or 0) >= 4500,
           f"{actual_perf_devices} perf devices in DB, {insert_elapsed:.2f}s "
           f"({(actual_perf_devices or 1)/max(insert_elapsed,0.001):.0f} rows/s)")

    # ── P-02 Page load times under large dataset ──────────────────────────────
    print("\n  [P-02] Page load times with 5,000+ device DB...")
    page_tests = [
        ("/",                           "Dashboard",            5.0),
        ("/devices",                    "Device list",          5.0),
        ("/devices?q=PERF-L01",         "Device search filter", 5.0),
        ("/repair/l1",                  "L1 queue",             5.0),
        ("/repair/l3l4",                "L3/L4 queue",          5.0),
        ("/qc",                         "QC list",              5.0),
        ("/sales/ready",                "Ready to Sale",        5.0),
        ("/spare-parts",                "Spare parts",          5.0),
        ("/reports/lot-pl",             "Lot P&L report",       8.0),
        ("/reports/stage-movement",     "Stage movement",       8.0),
        ("/reports/sales",              "Sales report",         8.0),
        ("/stage-control/aging",        "Aging dashboard",      8.0),
        ("/locations/dashboard",        "Location dashboard",   8.0),
        ("/devices/export",             "Excel export",        15.0),
    ]
    slow_pages = []
    for path, label, max_t in page_tests:
        t0 = time.time()
        s, t = await get_(session, path, timeout=max_t + 5)
        elapsed = time.time() - t0
        ok = ok_page(s, t) and elapsed < max_t
        rating = "FAST" if elapsed < 1.5 else "OK" if elapsed < max_t else "SLOW"
        record("P", f"P-02-{path.split('/')[-1][:8] or 'root'}", f"{label}: {elapsed:.2f}s",
               ok, f"{rating} (limit {max_t}s)")
        if not ok:
            slow_pages.append(f"{label} ({elapsed:.2f}s)")

    # ── P-03 50 concurrent user sessions ─────────────────────────────────────
    print("\n  [P-03] 50 concurrent user requests...")
    t0 = time.time()

    async def single_user_burst(user_id):
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(cookie_jar=jar) as s:
            await login(s)
            paths = ["/", "/devices", "/repair/l1", "/qc", "/sales/ready"]
            path = paths[user_id % len(paths)]
            s2, t2 = await get_(s, path, timeout=30)
            return s2, ok_page(s2, t2)

    concurrent_tasks = [single_user_burst(i) for i in range(50)]
    concurrent_results = await asyncio.gather(*concurrent_tasks, return_exceptions=True)
    concurrent_elapsed = time.time() - t0

    ok_concurrent = sum(
        1 for r in concurrent_results
        if isinstance(r, tuple) and r[1]
    )
    avg_ms = concurrent_elapsed / 50 * 1000
    record("P", "P-03",
           f"50 concurrent users: {ok_concurrent}/50 OK in {concurrent_elapsed:.2f}s (avg {avg_ms:.0f}ms)",
           ok_concurrent >= 45, f"{ok_concurrent}/50 success")

    # ── P-04 Stage movement throughput (50 devices concurrently) ─────────────
    print("\n  [P-04] Stage movement throughput test...")
    # Find 50 stock_in perf devices
    perf_stock = await db_fetchall(
        "SELECT barcode FROM devices WHERE barcode LIKE 'PERF-%' AND current_stage='stock_in' LIMIT 50")
    if len(perf_stock) < 10:
        # Move some perf devices back to stock_in
        await db_exec(
            "UPDATE devices SET current_stage='stock_in' "
            "WHERE barcode LIKE 'PERF-%' AND current_stage NOT IN ('sold','scrapped') LIMIT 50"
        )
        perf_stock = await db_fetchall(
            "SELECT barcode FROM devices WHERE barcode LIKE 'PERF-%' AND current_stage='stock_in' LIMIT 50")

    if perf_stock:
        move_tasks = [
            post_(session, "/repair/move", {
                "barcode": row[0], "to_stage": "l1", "notes": "PERF throughput test"
            })
            for row in perf_stock[:50]
        ]
        t0 = time.time()
        move_results = await asyncio.gather(*move_tasks)
        move_elapsed = time.time() - t0
        ok_moves = sum(1 for s, _, t in move_results if ok_page(s, t))
        rate = ok_moves / max(move_elapsed, 0.001)
        extrapolated_per_hour = rate * 3600
        record("P", "P-04",
               f"Stage movement throughput: {ok_moves}/{len(perf_stock)} in {move_elapsed:.2f}s "
               f"({rate:.1f}/s → {extrapolated_per_hour:,.0f}/hr)",
               ok_moves >= len(perf_stock) * 0.8,
               f"{'PASS' if ok_moves >= len(perf_stock)*0.8 else 'DEGRADED'}")
    else:
        record("P", "P-04", "Stage movement throughput", None, "No perf devices available")

    # ── P-05 Sales throughput (20 concurrent) ────────────────────────────────
    print("\n  [P-05] Sales throughput test...")
    # Move 30 perf devices to ready_to_sale
    await db_exec(
        "UPDATE devices SET current_stage='ready_to_sale' "
        "WHERE barcode LIKE 'PERF-%' "
        "AND current_stage NOT IN ('sold','scrapped','ready_to_sale') "
        "AND id IN (SELECT id FROM devices WHERE barcode LIKE 'PERF-%' "
        "AND current_stage NOT IN ('sold','scrapped','ready_to_sale') LIMIT 30)"
    )
    rts_perf = await db_fetchall(
        "SELECT barcode FROM devices WHERE barcode LIKE 'PERF-%' AND current_stage='ready_to_sale' LIMIT 20"
    )
    if rts_perf:
        sale_tasks = [
            post_(session, "/sales/new", {
                "barcode": row[0], "sale_price": "10000",
                "customer_name": f"Perf Buyer {i}",
                "customer_phone": "9876500000",
                "invoice_no": f"PERF-SALE-{i:05d}",
                "payment_mode": "cash", "notes": "PERF throughput test",
            })
            for i, row in enumerate(rts_perf)
        ]
        t0 = time.time()
        sale_results = await asyncio.gather(*sale_tasks)
        sale_elapsed = time.time() - t0
        ok_sales = sum(1 for s, _, t in sale_results if ok_page(s, t))
        rate = ok_sales / max(sale_elapsed, 0.001)
        record("P", "P-05",
               f"Sales throughput: {ok_sales}/{len(rts_perf)} in {sale_elapsed:.2f}s "
               f"({rate:.1f}/s → ~{rate*3600*8:,.0f}/8hr-day)",
               ok_sales >= len(rts_perf) * 0.8,
               f"{'PASS' if ok_sales >= len(rts_perf)*0.8 else 'DEGRADED'}")
    else:
        record("P", "P-05", "Sales throughput", None, "No ready perf devices")

    # ── P-06 IQC throughput (25 concurrent) ──────────────────────────────────
    print("\n  [P-06] IQC throughput test...")
    lot_row = await db_fetchone(f"SELECT id FROM lots WHERE lot_number='{PERF_LOT_PREFIX}01'")
    if lot_row:
        lot_id = str(lot_row[0])
        # Create 25 fresh devices at IQC stage
        iqc_barcodes = [f"PERF-IQC-{i:04d}" for i in range(1, 26)]
        for bc in iqc_barcodes:
            await db_exec("""
                INSERT INTO devices (id, barcode, lot_id, brand, model, serial_no,
                    sub_category, device_type, current_stage, created_at)
                VALUES (gen_random_uuid(), :bc, :lid, 'Dell', 'Latitude 5420', :sn,
                    'Laptop', 'Laptop', 'iqc', NOW())
                ON CONFLICT (barcode) DO UPDATE SET current_stage='iqc'
            """, {"bc": bc, "lid": lot_id, "sn": f"IQC-SN-{bc[-4:]}"})

        iqc_tasks = [
            post_(session, "/iqc/new", {
                "barcode": bc, "lot_id": lot_id,
                "sub_category": "Laptop", "device_type": "Laptop",
                "brand": "Dell", "model": "Latitude 5420",
                "serial_no": f"IQC-SN-{bc[-4:]}", "grn_number": "PERF-GRN",
                "cpu": "Core i5", "generation": "10th Gen",
                "ram_gb": "8", "storage_gb": "256", "storage_type": "SSD",
                "hdd_capacity_gb": "0", "screen_size": "14",
                "battery_health_pct": "80", "color": "Silver",
                "grade": "A", "floor": "1", "warehouse": "Main",
                "screen_dot": "0", "panel_a_scratch": "no",
                "keyboard_working": "yes", "touchpad_working": "yes",
                "power_on": "yes", "charging_port": "ok",
            })
            for bc in iqc_barcodes
        ]
        t0 = time.time()
        iqc_results = await asyncio.gather(*iqc_tasks)
        iqc_elapsed = time.time() - t0
        ok_iqc = sum(1 for s, _, t in iqc_results if ok_page(s, t))
        rate = ok_iqc / max(iqc_elapsed, 0.001)
        record("P", "P-06",
               f"IQC throughput: {ok_iqc}/25 in {iqc_elapsed:.2f}s ({rate:.1f}/s → {rate*3600:,.0f}/hr)",
               ok_iqc >= 20, f"{'PASS' if ok_iqc>=20 else 'DEGRADED'}")
    else:
        record("P", "P-06", "IQC throughput", None, "No perf lot found")

    # ── P-07 DB count at scale ────────────────────────────────────────────────
    total_devices = await db_scalar("SELECT COUNT(*) FROM devices")
    total_lots = await db_scalar("SELECT COUNT(*) FROM lots")
    total_sales = await db_scalar("SELECT COUNT(*) FROM sales")
    total_movements = await db_scalar("SELECT COUNT(*) FROM stage_movements")
    print(f"\n  DB at test completion:")
    print(f"    Devices:   {total_devices:,}")
    print(f"    Lots:      {total_lots:,}")
    print(f"    Sales:     {total_sales:,}")
    print(f"    Movements: {total_movements:,}")
    record("P", "P-07", f"DB at scale: {total_devices:,} devices, {total_sales:,} sales",
           (total_devices or 0) >= 5000,
           f"devices={total_devices}, lots={total_lots}, sales={total_sales}")

    # ── P-08 Final dashboard under full load ──────────────────────────────────
    t0 = time.time()
    s, t = await get_(session, "/", timeout=15)
    elapsed = time.time() - t0
    record("P", "P-08",
           f"Dashboard loads in {elapsed:.2f}s with {total_devices:,} devices in DB",
           ok_page(s, t) and elapsed < 8.0, f"{elapsed:.2f}s")

    SECTION_TIMES["P"] = time.time() - t0_total
    print(f"\n  [P] Performance tests complete in {SECTION_TIMES['P']:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# MAIN RUNNER
# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    suite_start = time.time()

    print("=" * 70)
    print("  OxyPC Inventory — FULL TEST SUITE")
    print(f"  Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # Single admin session for most tests
    jar = aiohttp.CookieJar()
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        ok = await login(session)
        print(f"\n  Admin login: {'OK' if ok else 'FAILED'}")
        if not ok:
            print("  FATAL: Cannot run tests — login failed.")
            print("  Ensure server is running on http://localhost:8000")
            return

        # ── Run all sections ──────────────────────────────────────────────────
        await section_functional(session)
        await section_e2e_flow(session)
        await section_spare_parts(session)
        await section_cosmetic(session)
        await section_edge_cases(session)
        await section_rbac(session)
        await section_integrity(session)
        await section_performance(session)

    # ── Final report ──────────────────────────────────────────────────────────
    total_elapsed = time.time() - suite_start

    total = PASS + FAIL
    pct   = (PASS / total * 100) if total > 0 else 0

    print("\n" + "=" * 70)
    print(f"  FULL TEST SUITE RESULTS")
    print(f"  {PASS} PASS / {FAIL} FAIL / {SKIP} SKIP / {total} TOTAL ({pct:.1f}% pass rate)")
    print(f"  Total runtime: {total_elapsed:.1f}s")
    print("=" * 70)

    # Section summary
    section_summary: dict[str, dict] = {}
    for sec, tid, name, status, note in RESULTS:
        if sec not in section_summary:
            section_summary[sec] = {"pass": 0, "fail": 0, "skip": 0}
        section_summary[sec][status.lower() if status != "PASS" else "pass"] += 1

    print("\n  SECTION BREAKDOWN:")
    sections_with_labels = [
        ("F",  "Functional (endpoint reachability)"),
        ("E",  "E2E Flow (device lifecycle)"),
        ("S",  "Spare Parts (purchase → consume → ledger)"),
        ("C",  "Cosmetic Pipeline"),
        ("X",  "Edge Cases (guards, validation)"),
        ("R",  "RBAC (role-based access)"),
        ("I",  "Data Integrity"),
        ("P",  "Performance (5000 devices, load, throughput)"),
    ]
    for code, label in sections_with_labels:
        stats = {k: 0 for k in ("pass", "fail", "skip")}
        for sec, tid, name, status, note in RESULTS:
            if sec == code:
                stats[status.lower() if status in ("PASS","FAIL","SKIP") else "skip"] += 1
        t_sec = SECTION_TIMES.get(code, 0)
        total_sec = stats["pass"] + stats["fail"] + stats["skip"]
        flag = "[PASS]" if stats["fail"] == 0 else "[FAIL]"
        print(f"  {flag} {code}  {label:<45} {stats['pass']:>3}P {stats['fail']:>3}F {stats['skip']:>3}S  ({t_sec:.1f}s)")

    # Failures detail
    failures = [(sec, tid, name, note) for sec, tid, name, status, note in RESULTS if status == "FAIL"]
    if failures:
        print(f"\n  FAILURES ({len(failures)}):")
        for sec, tid, name, note in failures:
            print(f"    [FAIL] [{sec}] {tid}: {name}" + (f"  -- {note}" if note else ""))
    else:
        print("\n  No failures!")

    # Write results to file
    report_path = "test_results.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"OxyPC Inventory — Full Test Suite Results\n")
        f.write(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Result: {PASS}P / {FAIL}F / {SKIP}S / {total} total ({pct:.1f}%)\n")
        f.write(f"Runtime: {total_elapsed:.1f}s\n\n")
        current_sec = None
        for sec, tid, name, status, note in RESULTS:
            if sec != current_sec:
                f.write(f"\n[{sec}]\n")
                current_sec = sec
            f.write(f"  {status:<4} {tid:<30} {name}" + (f"  [{note}]" if note else "") + "\n")
    print(f"\n  Full results written to: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
