import csv
import io
import re
import uuid
from datetime import datetime
from utils.timezone import app_now
from templates_config import templates
from fastapi import APIRouter, Depends, File, Form, UploadFile, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from database import get_db
from utils.csv_decode import decode_csv_bytes
from utils.master_data import entity_values, master_values
from utils.device_category import pair_type_and_category
from utils.device_spec import upload_spec, looks_swapped, RAM_MAX_GB
from utils.grades import parse_grade
from models.user import User, UserRole
from models.device import Device, DeviceStage, StageMovement, STAGE_LABELS
from models.iqc_inspection import IQCInspection
from models.lot import Lot
from models.spare_parts import SparePart
from models.telecalling import TelecallingRecord
from auth.dependencies import get_current_user, require_roles, verify_csrf
from services.audit_engine import audit
from services.control_engine import validate_transition
from services.location_defaults import ensure_stage_location
from models.grn_import import GRNImport

router = APIRouter(prefix="/bulk-upload", tags=["bulk_upload"], dependencies=[Depends(verify_csrf)])

async def _remember_grn_stock(db, device_ids):
    """Before a tag's Invoice Number is overwritten, keep it counted under the
    GRN its current invoice belongs to (GRN Stocked is add-only). In a
    savepoint, so a failure here can never block the sale / edit itself."""
    try:
        from services.grn_stocked import remember_tags
        async with db.begin_nested():
            await remember_tags(db, list(device_ids))
    except Exception as e:  # noqa: BLE001
        print(f"  [GRNStocked] could not remember tags: {e}")
allowed = require_roles(UserRole.admin, UserRole.inventory_manager)

# ── Devices template column order — mirrors every field on the IQC entry
# page (templates/iqc/form.html) so a bulk upload can fully replace manual
# entry. First 9 columns per the fixed lead-in spec; everything else (the
# remaining Device columns, then the full physical inspection checklist)
# follows. Shared by both the template download and the upload parser so
# the two can never drift apart. ────────────────────────────────────────────
DEVICE_CORE_FIELDS = [
    "entity",
    "Tag No", "serial_no", "brand", "model", "cpu", "cpu_make", "generation", "RAM", "Storage",
    "category", "lot_number",
]
DEVICE_REST_FIELDS = [
    # RAM / Storage only (2026-10-22): ram_gb, storage_gb, storage_type,
    # hdd_capacity_gb, ram/hdd_summary and the four Total RAM/HDD columns are
    # no longer in the template (old files with them still import).
    "device_type",
    "invoice_number",
    "screen_size", "battery_health_pct", "bios_password", "color", "grade",
    "floor", "warehouse", "grn_number", "qty", "device_price", "notes",
]
IQC_INSPECTION_FIELDS = [
    "power_on", "all_ok", "status",
    "screen_dot", "screen_line", "screen_functional", "screen_discoloration",
    "screen_patch", "screen_broken", "screen_flickering", "screen_scratch",
    "screen_loose", "screen_missing", "screen_hinge_broken", "screen_colour_spread",
    "screen_keyboard_mark", "screen_hard_press",
    "panel_a_scratch", "panel_a_broken", "panel_a_missing", "panel_a_dent", "panel_a_colour_fade",
    "panel_b_scratch", "panel_b_colour_fade", "panel_b_rubber_cut", "panel_b_broken", "panel_b_missing",
    "panel_c_scratch", "panel_c_broken", "panel_c_missing", "panel_c_dent", "panel_c_colour_fade",
    "panel_d_dent", "panel_d_colour_fade", "panel_d_scratch", "panel_d_broken", "panel_d_missing",
    "keyboard_working", "keyboard_colour_fade", "keyboard_key_missing", "keyboard_hard_press",
    "speaker_status",
    "touchpad_working", "touchpad_click_working", "touchpad_scratch", "touchpad_colour_fade", "touchpad_missing",
    "port_hdmi", "port_usb_working", "port_audio_jack", "usb_a_ports", "usb_c_ports", "ethernet_ports",
    "wifi_status", "webcam_status", "hdd_connector", "hdd_casing", "battery_present", "battery_cable",
    "charging_port", "dvd_drive",
    "cover_ram", "cover_dvd", "cover_storage",
    "hinge_condition", "hinge_cover", "touchpad_logicboard",
    "storage_health_pct", "fan_sound_dba", "fan_working",
    "r2v3_grade_category", "remarks",
]
DEVICE_CSV_HEADERS = DEVICE_CORE_FIELDS + DEVICE_REST_FIELDS + IQC_INSPECTION_FIELDS

# Manage Lots (External Partner test data) "Bulk Upload IQC" template — same
# fields as the live devices template, minus "entity" (that page always
# force-sets entity server-side, see upload_devices' force_entity param) and
# reordered: lot_number first, then everything else in its usual order, with
# qty/warehouse/grn_number/invoice_number/device_price pushed to the very
# end. Parsing itself is header-name-based (_row_get), not positional, so
# this reordering needs no parser changes — same DEVICE_CSV_HEADERS fields,
# different column order.
_TEST_PARTNER_TRAILING_FIELDS = ["qty", "warehouse", "grn_number", "invoice_number", "device_price"]
DEVICE_CSV_HEADERS_TEST_PARTNER = (
    ["lot_number"]
    + [h for h in DEVICE_CSV_HEADERS if h not in (["entity", "lot_number"] + _TEST_PARTNER_TRAILING_FIELDS)]
    + _TEST_PARTNER_TRAILING_FIELDS
)

# Header aliases accepted for the tag-number column on IQC upload — the
# template ships "Tag No" but hand-built CSVs from other systems commonly
# use one of these instead.
_TAG_HEADER_ALIASES = ["Tag No", "Tag Number", "barcode", "tag_number"]


def _resolve_tag_header(row: dict) -> str | None:
    """Return the first tag-number value found under any accepted header
    alias (case-insensitive), or None if none of them are present/filled."""
    lower_map = {k.lower(): k for k in row.keys()}
    for alias in _TAG_HEADER_ALIASES:
        actual_key = lower_map.get(alias.lower())
        if actual_key is not None:
            val = (row.get(actual_key) or "").strip()
            if val:
                return val
    return None


def _row_get(row: dict, key: str) -> str:
    """Case-insensitive column read.

    Spreadsheets round-trip headers with inconsistent casing — a file whose
    first column reads "Entity" rather than "entity" used to import with the
    entity silently dropped, because every lookup was an exact dict hit. The
    tag-number column already tolerated casing; now every column does.
    """
    v = row.get(key)
    if v is None:
        for k in row.keys():
            if k is not None and k.lower() == key.lower():
                v = row[k]
                break
    return v or ""


# ── IQC inspection column shapes, read off the model ─────────────────────────
# The checklist used to be written straight through as text:
#     **{f: _s(row, f) for f in IQC_INSPECTION_FIELDS}
# which handed "Yes" to an Integer column and a 13-character value to a
# varchar(10). Both are rejected by Postgres at flush — i.e. at commit, outside
# the per-row try/except — so a single bad cell aborted the entire upload with
# an unhandled 500 and imported nothing. Coercing against the real column type
# turns those into ordinary per-row errors.
def _inspection_column_specs() -> dict:
    specs = {}
    for col in IQCInspection.__table__.columns:
        t = col.type.__class__.__name__
        specs[col.name] = (t, getattr(col.type, "length", None))
    return specs


_INSPECTION_SPECS = _inspection_column_specs()


def _coerce_inspection(row: dict, row_no: int, errors: list) -> dict:
    """Build the IQCInspection kwargs for one row, coercing each value to its
    column's type. Any value that cannot be represented is reported against
    the row and left out, so the rest of the row still imports."""
    out = {}
    for f in IQC_INSPECTION_FIELDS:
        raw = _row_get(row, f).strip()
        if not raw:
            continue
        kind, length = _INSPECTION_SPECS.get(f, ("String", None))

        if kind in ("Integer", "SmallInteger", "BigInteger"):
            # Pull the first integer out of whatever was typed ("2 ports" -> 2).
            # A cell with no digits at all ("Yes", "No") is left unset rather
            # than guessed at — inventing 1/0 for a dBA reading or a port count
            # would put a fabricated measurement into the inspection record.
            m = re.search(r"-?\d+", raw)
            if m:
                out[f] = int(m.group())
        elif kind in ("Float", "Numeric"):
            m = re.search(r"-?\d+(?:\.\d+)?", raw)
            if m:
                out[f] = float(m.group())
        else:
            if length and len(raw) > length:
                errors.append(
                    f"Row {row_no}: {f} is {len(raw)} characters, max {length} ('{raw}')")
            else:
                out[f] = raw
    return out

# Only a handful of illustrative inspection values in the sample row — the rest
# are left blank since the checklist is optional and 70+ filled columns would
# make the example row unreadable.
_IQC_EXAMPLE_MAP = {
    "power_on": "Yes", "all_ok": "Yes", "status": "Power On",
    "screen_functional": "Yes", "screen_scratch": "No",
    "keyboard_working": "Yes", "touchpad_working": "Yes",
    "port_hdmi": "Yes", "port_usb_working": "Yes", "port_audio_jack": "Yes",
    "usb_a_ports": "2", "usb_c_ports": "1", "ethernet_ports": "1",
    "wifi_status": "Working", "webcam_status": "Ok",
    "battery_present": "Yes", "fan_working": "Yes",
    "r2v3_grade_category": "C3",
}

# Sample values for the Device columns, keyed by header name rather than
# position. The example row used to be three hand-built lists concatenated in
# header order, which silently drifted every time a column was inserted — by
# the time `cpu_make` and `entity` were added it was 8 values short of 104
# headers, so the downloaded template showed lot_number under storage_gb and
# grade under total_ram_size. Building it from this map means the row is
# generated from DEVICE_CSV_HEADERS itself and cannot drift again.
_DEVICE_EXAMPLE_MAP = {
    "entity": "Deshwal",
    "Tag No": "OXY-00001", "serial_no": "SN123456",
    "brand": "HP", "model": "EliteBook 840 G6",
    "cpu": "Intel Core i5-8250U", "cpu_make": "Intel", "generation": "8th Gen",
    "RAM": "8", "Storage": "256",
    "category": "Laptop", "lot_number": "LOT-001",
    "device_type": "Laptop",
    "invoice_number": "INV-2024-001",
    "screen_size": "14.0 FHD", "battery_health_pct": "78", "bios_password": "no",
    "color": "Silver", "grade": "B",
    "floor": "Floor 1", "warehouse": "TRC 1st Floor", "grn_number": "GRN-001",
    "qty": "1", "device_price": "5500", "notes": "Minor scratch on lid",
}

# CSV template headers
TEMPLATES = {
    "lots": {
        "filename": "lots_template.csv",
        "headers": [
            # Required
            "lot_number", "supplier_name", "buying_price", "qty", "purchase_date",
            # GRN
            "grn_system_number", "grn_number_new", "grn_date",
            # Invoice / GST — invoice date is taken from purchase_date (GRN/Invoice)
            "invoice_no", "invoice_value",
            "taxable_amount", "sgst", "cgst", "igst",
            # PO / Logistics
            "po_number", "vehicle_number", "e_way_bill",
            # Misc
            "notes",
        ],
        "example": [
            "LOT-001", "ABC Traders", "50000", "10", "2024-01-15",
            "GRN-178", "178", "2024-01-14",
            "INV-2024-001", "59000",
            "50000", "4500", "4500", "0",
            "PO-2024-001", "MH04AB1234", "EWB-123456",
            "First lot - HP laptops",
        ],
    },
    "devices": {
        "filename": "devices_template.csv",
        "headers": DEVICE_CSV_HEADERS,
        # Generated from the header list itself — one value per header, always
        # in step with it. See _DEVICE_EXAMPLE_MAP.
        "example": [
            _DEVICE_EXAMPLE_MAP.get(h, _IQC_EXAMPLE_MAP.get(h, ""))
            for h in DEVICE_CSV_HEADERS
        ],
    },
    "devices_test_partner": {
        "filename": "devices_test_partner_template.csv",
        "headers": DEVICE_CSV_HEADERS_TEST_PARTNER,
        "example": [
            _DEVICE_EXAMPLE_MAP.get(h, _IQC_EXAMPLE_MAP.get(h, ""))
            for h in DEVICE_CSV_HEADERS_TEST_PARTNER
        ],
    },
    "spare_parts": {
        "filename": "spare_parts_template.csv",
        "headers": ["part_code", "name", "category", "unit_price", "min_stock_alert", "supplier", "notes"],
        "example": ["PART-0001", "DDR4 8GB RAM", "RAM", "500", "5", "ABC Electronics", "Samsung OEM"],
    },
    "leads": {
        "filename": "leads_template.csv",
        "headers": [
            "customer_name", "phone", "email", "customer_type",
            "city", "state",
            "category", "brand", "model", "generation",
            "processor", "ram", "hard_disk",
            "product_type", "grade", "qty_required", "budget",
            "lot_reference", "call_outcome", "next_followup", "notes",
        ],
        "example": [
            "Rajesh Kumar", "9876543210", "rajesh@example.com", "corporate",
            "Mumbai", "Maharashtra",
            "Laptop", "Dell", "Latitude 5490", "8th Gen",
            "Intel Core i5-8250U", "16GB", "512GB SSD",
            "Refurbished", "A", "5", "35000",
            "LOT-001", "interested", "2026-04-01", "Needs delivery by month end",
        ],
    },
}


@router.get("", response_class=HTMLResponse)
async def bulk_upload_page(
    request: Request,
    type: str = None,
    current_user: User = Depends(allowed)
):
    # ?type=lots → pre-select lots tab; ?type=devices → pre-select devices tab
    return templates.TemplateResponse("bulk_upload/index.html", {
        "request": request, "current_user": current_user,
        "preselect_type": type or "devices",
    })


@router.get("/template/{upload_type}")
async def download_template(upload_type: str, current_user: User = Depends(get_current_user)):
    if upload_type not in TEMPLATES:
        raise HTTPException(404, "Unknown template type")
    tmpl = TEMPLATES[upload_type]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(tmpl["headers"])
    writer.writerow(tmpl["example"])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={tmpl['filename']}"},
    )


@router.post("/lots")
async def upload_lots(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    content = await file.read()
    text = content.decode("utf-8-sig")  # handle BOM
    reader = csv.DictReader(io.StringIO(text))
    inserted, errors = 0, []

    def _parse_date(s):
        s = (s or "").strip()
        if not s:
            return None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    def _parse_decimal(s, default=None):
        s = (s or "").strip()
        if not s:
            return default
        try:
            return float(s.replace(",", ""))
        except ValueError:
            return default

    def _parse_int(s, default=None):
        s = (s or "").strip()
        if not s:
            return default
        try:
            return int(s)
        except ValueError:
            return default

    for i, row in enumerate(reader, start=2):
        try:
            lot_number = row.get("lot_number", "").strip()
            if not lot_number:
                errors.append(f"Row {i}: lot_number is required")
                continue
            existing = await db.execute(select(Lot).where(Lot.lot_number == lot_number))
            if existing.scalar_one_or_none():
                errors.append(f"Row {i}: lot_number '{lot_number}' already exists")
                continue

            buying_price = _parse_decimal(row.get("buying_price"), 0)
            qty          = _parse_int(row.get("qty"), 1)
            purchase_date = _parse_date(row.get("purchase_date")) or app_now()

            lot = Lot(
                lot_number    = lot_number,
                supplier_name = row.get("supplier_name", "").strip() or "Unknown",
                buying_price  = buying_price,
                qty           = qty,
                purchase_date = purchase_date,
                # GRN
                grn_system_number = row.get("grn_system_number", "").strip() or None,
                grn_number_new    = _parse_int(row.get("grn_number_new")),
                grn_date          = _parse_date(row.get("grn_date")),
                # Invoice / GST — GRN/Invoice (purchase_date) doubles as invoice date
                invoice_no     = row.get("invoice_no", "").strip() or None,
                invoice_date   = purchase_date,
                invoice_value  = _parse_decimal(row.get("invoice_value")),
                taxable_amount = _parse_decimal(row.get("taxable_amount")),
                sgst           = _parse_decimal(row.get("sgst")),
                cgst           = _parse_decimal(row.get("cgst")),
                igst           = _parse_decimal(row.get("igst")),
                # PO / Logistics
                po_number      = row.get("po_number", "").strip() or None,
                vehicle_number = row.get("vehicle_number", "").strip() or None,
                e_way_bill     = row.get("e_way_bill", "").strip() or None,
                # Misc
                notes          = row.get("notes", "").strip() or None,
                created_by     = current_user.username,
            )
            db.add(lot)
            inserted += 1
        except Exception as e:
            errors.append(f"Row {i}: {str(e)}")

    await db.commit()
    return templates.TemplateResponse("bulk_upload/result.html", {
        "request": request, "current_user": current_user,
        "upload_type": "Lots", "inserted": inserted, "errors": errors,
        "back_url": "/lots",
    })


@router.post("/devices")
async def upload_devices(
    request: Request,
    file: UploadFile = File(...),
    force_entity: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    content = await file.read()
    text = decode_csv_bytes(content)
    reader = csv.DictReader(io.StringIO(text))
    inserted, errors = 0, []
    # Rows held back for the duplicate-review modal instead of hard-erroring —
    # a duplicate Tag No (barcode already exists elsewhere) or a lot_number the
    # file references but which doesn't exist yet in Lot Management. Both used
    # to be silently skipped as an error row; now they're surfaced so the user
    # can resolve them in bulk (Approve Move / Add Lot) instead of fixing the
    # CSV and re-uploading one row at a time.
    tag_duplicates: list[dict] = []
    lot_duplicates: dict[str, dict] = {}   # lot_number -> row, de-duped

    # Build lot_number → lot_id map
    lots_result = await db.execute(select(Lot))
    lot_map = {lot.lot_number: lot.id for lot in lots_result.scalars().all()}

    # Existing barcodes, fetched once up front (not per row) so a large CSV
    # doesn't do a DB round-trip per row — this is what was slow enough to
    # trip a reverse-proxy timeout ("upstream error"). Also tracks barcodes
    # added earlier in this same batch (no longer flushed row-by-row) so
    # in-batch duplicates are still caught instead of failing the whole
    # commit with a unique-constraint error.
    # Case-folded to uppercase for membership testing. A tag re-typed, re-scanned,
    # or pulled off a different label with different capitalisation than however
    # it was first entered used to pass this check clean and create a second,
    # fully independent Device row for the same physical unit — the barcode
    # column's UNIQUE constraint is case-sensitive at the database level, so
    # nothing there caught it either. 1,495 such pairs exist in production
    # today, most traced to this exact gap.
    existing_barcodes = {
        (b or "").upper() for b in (await db.execute(select(Device.barcode))).scalars().all()
    }
    # Existing devices by barcode (upper-cased key), for the duplicate-tag
    # review table (current stage). Fetched lazily below only if a duplicate
    # is actually found.
    existing_devices_by_barcode: dict[str, Device] = {}

    def _s(row, key):
        return _row_get(row, key).strip() or None

    def _i(row, key):
        # Accept any value without restriction — pull the first integer out of
        # whatever was typed (e.g. "256GB" -> 256, "8 GB" -> 8) instead of
        # silently discarding the whole cell just because it wasn't a bare digit.
        m = re.search(r"-?\d+", _row_get(row, key).strip())
        return int(m.group()) if m else None

    def _grade(row, key="grade"):
        return _parse_grade(_row_get(row, key))

    # Entity is matched case-insensitively against the Dropdown-configuration
    # list and stored in its canonical spelling, so "deshwal" and "DESHWAL"
    # both land as "Deshwal". An unrecognised value fails the row rather than
    # being written through: a typo'd entity is invisible in the UI but
    # silently corrupts the Entity Movement counts and the per-entity
    # breakdown on All Inventory. Blank is allowed — unassigned is a valid
    # state, and entity can still be set later via Customise or Entity Movement.
    entity_lookup = {e.lower(): e for e in await entity_values(db)}
    # Device Type / Sub-Category: either column fills both (2026-10-22).
    dt_master = await master_values(db, "device_type")
    sc_master = await master_values(db, "sub_category")
    # Manage Lots' "Bulk Upload IQC" form (External Partner test data) posts
    # this so every row lands on that entity regardless of whether the file
    # even has an "entity" column — the test-partner template intentionally
    # drops that column (see DEVICE_CSV_HEADERS_TEST_PARTNER).
    forced_entity_val = entity_lookup.get(force_entity.strip().lower()) if force_entity.strip() else None

    # ── Flow 1 GRN check (2026-10-21) ────────────────────────────────────
    # A row that carries a GRN number is validated against that GRN: the GRN
    # must exist (not deleted) and the row's Invoice Number must equal the
    # GRN's invoice. Lot-vs-GRN mapping is deliberately NOT checked (a lot can
    # be split across GRNs). Valid rows take grn_number, invoice_number and
    # entity from the GRN and go straight to Stock In; invalid rows are
    # skipped and handed back as a downloadable file. Rows with no GRN keep
    # the old behaviour (land in IQC, mapped later via "Map this GRN").
    _live_grns = (await db.execute(
        select(GRNImport).where(GRNImport.is_deleted.isnot(True))
    )).scalars().all()
    grn_by_number = {g.grn_number.strip().upper(): g for g in _live_grns if g.grn_number}
    # GRN <-> Invoice Number is one-to-one (2026-10-21), so a row may give
    # either one. An invoice that legacy data has on 2+ GRNs is ambiguous and
    # is reported rather than guessed.
    grns_by_invoice: dict[str, list] = {}
    for _g in _live_grns:
        if _g.invoice_number and _g.invoice_number.strip():
            grns_by_invoice.setdefault(_g.invoice_number.strip().upper(), []).append(_g)
    # Location master rows used by those GRNs — Location ID, zone and
    # warehouse on the tag come from here, never from the file.
    from models.location import StorageLocation
    _loc_ids = {g.location_id for g in _live_grns if g.location_id}
    loc_by_id = {l.id: l for l in (await db.execute(
        select(StorageLocation).where(StorageLocation.id.in_(_loc_ids))
    )).scalars().all()} if _loc_ids else {}
    file_header = list(reader.fieldnames or [])
    grn_skipped: list[list] = []
    to_stock_in = 0
    now = app_now()

    for i, row in enumerate(reader, start=2):
        try:
            barcode = _resolve_tag_header(row)
            if not barcode:
                errors.append(f"Row {i}: Tag No is required")
                continue
            barcode_key = barcode.upper()
            if barcode_key in existing_barcodes:
                if barcode_key not in existing_devices_by_barcode:
                    # func.upper() on both sides: the stored barcode may carry
                    # whatever case it was originally entered under.
                    ed = (await db.execute(
                        select(Device).where(func.upper(Device.barcode) == barcode_key)
                    )).scalar_one_or_none()
                    if ed:
                        existing_devices_by_barcode[barcode_key] = ed
                ed = existing_devices_by_barcode.get(barcode_key)
                tag_duplicates.append({
                    "tag_number": barcode,
                    "grade": _s(row, "grade") or "—",
                    "current_stage": ed.current_stage.value if ed else None,
                    "current_stage_label": STAGE_LABELS.get(ed.current_stage, ed.current_stage) if ed else "—",
                    # In Trash (2026-10-17): a trashed tag still blocks the
                    # upload as a duplicate but is hidden from Inventory
                    # Search, so the result page warns and points to Trash.
                    "in_trash": bool(ed and ed.is_trashed),
                    "file_stage": DeviceStage.iqc.value,
                    "file_stage_label": STAGE_LABELS.get(DeviceStage.iqc, "IQC"),
                    # Full CSV row, round-tripped through the modal so "Update"/
                    # "Update All" can apply this row's values without needing
                    # the original file re-uploaded.
                    "row": {k: (v or "") for k, v in row.items()},
                })
                continue
            if forced_entity_val:
                entity_val = forced_entity_val
            else:
                entity_raw = _s(row, "entity")
                if entity_raw and entity_raw.lower() not in entity_lookup:
                    errors.append(
                        f"Row {i}: entity '{entity_raw}' not recognised — "
                        f"expected one of: {', '.join(entity_lookup.values()) or '(none configured)'}"
                    )
                    continue
                entity_val = entity_lookup.get(entity_raw.lower()) if entity_raw else None

            # GRN check — before the lot lookup, so a bad GRN/invoice is reported
            # as such rather than hidden behind a lot error.
            grn_obj = None
            grn_raw = (_s(row, "grn_number") or "").strip()
            inv_raw = (_s(row, "invoice_number") or "").strip()
            if grn_raw or inv_raw:
                reason = None
                if grn_raw:
                    grn_obj = grn_by_number.get(grn_raw.upper())
                    if grn_obj is None:
                        reason = f"GRN {grn_raw} not found"
                    elif inv_raw and (grn_obj.invoice_number or "").strip().upper() != inv_raw.upper():
                        reason = (f"Invoice Number '{inv_raw}' does not match "
                                  f"GRN {grn_obj.grn_number}'s invoice '{grn_obj.invoice_number or '(blank)'}'")
                else:
                    # Invoice only: a match fills everything from its GRN. No
                    # match keeps the pre-2026-10-21 behaviour (tag lands in IQC
                    # with the file's invoice) — External Partner test uploads
                    # and invoice-only intakes rely on it.
                    matches = grns_by_invoice.get(inv_raw.upper(), [])
                    if not matches:
                        pass  # no GRN for this invoice -> old path (IQC, file's invoice)
                    elif len(matches) > 1:
                        reason = (f"Invoice Number {inv_raw} is on {len(matches)} GRNs "
                                  f"({', '.join(m.grn_number for m in matches)}) — add the GRN number")
                    else:
                        grn_obj = matches[0]
                if reason:
                    grn_obj = None
                    grn_skipped.append([row.get(h) or "" for h in file_header] + [reason])
                    errors.append(f"Row {i}: {reason} — skipped")
                    continue
                if grn_obj is not None and grn_obj.entity and not forced_entity_val:
                    entity_val = grn_obj.entity

            # RAM / Storage swapped in the file (2026-10-27): RAM over 128 GB
            # with a 4-64 GB drive means the two columns were typed into each
            # other (whole lots of EliteBook "RAM 256GB, Storage 16GB" came in
            # this way). Skipped with the reason, like a bad GRN, so the sheet
            # is fixed rather than wrong specs saved.
            _chk = upload_spec(_s(row, "RAM") or _s(row, "total_ram_size") or _s(row, "ram_gb"),
                               _s(row, "Storage") or _s(row, "total_hdd_size") or _s(row, "storage_gb"))
            if looks_swapped(_chk.get("ram_gb"), _chk.get("storage_gb")):
                reason = (f"RAM {_chk.get('total_ram_size')} and Storage {_chk.get('total_hdd_size')} "
                          f"look swapped (RAM over {RAM_MAX_GB} GB) — check the RAM / Storage columns")
                grn_skipped.append([row.get(h) or "" for h in file_header] + [reason])
                errors.append(f"Row {i}: {reason} — skipped")
                continue

            lot_number = _s(row, "lot_number")
            lot_id = lot_map.get(lot_number)
            if not lot_id:
                if lot_number and lot_number not in lot_duplicates:
                    lot_duplicates[lot_number] = {
                        "lot_number": lot_number,
                        "purchase_date": app_now().strftime("%Y-%m-%d"),
                        "vendor_name": "Unknown",
                    }
                errors.append(f"Row {i}: lot_number '{lot_number}' not found")
                continue
            bios_pwd_raw = (row.get("bios_password") or "").strip().lower()
            qty_raw = _i(row, "qty")
            dev_price_raw = (row.get("device_price") or "").strip()
            dev_price = float(dev_price_raw) if dev_price_raw else None
            # Generate the PK explicitly rather than relying on the column
            # default — that default only fires on flush, so device.id would
            # still be None when building the StageMovement below now that
            # rows are no longer flushed one at a time.
            dev_id = uuid.uuid4()
            row_dt, row_sc = pair_type_and_category(_s(row, "device_type"), _s(row, "category"),
                                                    dt_master, sc_master)
            # RAM / Storage (2026-10-22). Old files without them fall back to
            # their previous columns, so they import as before.
            spec = upload_spec(_s(row, "RAM") or _s(row, "total_ram_size") or _s(row, "ram_gb"),
                               _s(row, "Storage") or _s(row, "total_hdd_size") or _s(row, "storage_gb"))
            device = Device(
                id=dev_id, barcode=barcode, lot_id=lot_id,
                entity=entity_val,
                sub_category=row_sc,
                brand=_s(row, "brand"), model=_s(row, "model"),
                device_type=row_dt,
                serial_no=_s(row, "serial_no"),
                grn_number=_s(row, "grn_number"),
                cpu=_s(row, "cpu"), cpu_make=_s(row, "cpu_make"), generation=_s(row, "generation"),
                ram_gb=spec.get("ram_gb"), storage_gb=spec.get("storage_gb"),
                storage_type=_s(row, "storage_type"),
                hdd_capacity_gb=_i(row, "hdd_capacity_gb"),
                ram_summary=_s(row, "ram_summary") or spec.get("ram_summary"),
                hdd_summary=_s(row, "hdd_summary") or spec.get("hdd_summary"),
                total_ram_count=_s(row, "total_ram_count") or spec.get("total_ram_count"),
                total_ram_size=spec.get("total_ram_size"),
                total_hdd_count=_s(row, "total_hdd_count") or spec.get("total_hdd_count"),
                total_hdd_size=spec.get("total_hdd_size"),
                invoice_number=_s(row, "invoice_number"),
                screen_size=_s(row, "screen_size"),
                battery_health_pct=_i(row, "battery_health_pct"),
                bios_password=(bios_pwd_raw == "yes"),
                color=_s(row, "color"), grade=_grade(row),
                floor=_s(row, "floor"), warehouse=_s(row, "warehouse"),
                qty=qty_raw or 1,
                device_price=dev_price,
                notes=_s(row, "notes"),
                current_stage=DeviceStage.iqc,
            )
            db.add(device)
            existing_barcodes.add(barcode_key)
            movement = StageMovement(
                device_id=device.id, from_stage=None, to_stage=DeviceStage.iqc,
                moved_by=current_user.username, notes="Bulk Upload - IQC Entry"
            )
            db.add(movement)
            if grn_obj is not None:
                # GRN-validated row: details come from the GRN, stage -> Stock In.
                device.grn_number = grn_obj.grn_number
                device.invoice_number = grn_obj.invoice_number
                # Location from the GRN overrides the file's floor/warehouse
                # (2026-10-21): Location ID, zone and warehouse all come from
                # Location master.
                _gloc = loc_by_id.get(grn_obj.location_id) if grn_obj.location_id else None
                if _gloc is not None:
                    device.location_id = _gloc.id
                    device.floor = _gloc.zone_label
                    device.warehouse = _gloc.display_name
                movement.exited_at = now
                db.add(StageMovement(
                    device_id=device.id, from_stage=DeviceStage.iqc, to_stage=DeviceStage.stock_in,
                    moved_by=current_user.username,
                    notes=f"Bulk Upload IQC — GRN {grn_obj.grn_number} verified, moved to Stock Inward"))
                device.current_stage = DeviceStage.stock_in
                await ensure_stage_location(db, device, DeviceStage.stock_in, current_user,
                                            preferred_location_id=(_gloc.id if _gloc is not None else None))
                to_stock_in += 1

            # ── Physical inspection checklist (mirrors the manual IQC entry
            # form's IQCInspection creation) — entirely optional per-row. ──
            # Every checklist value is coerced to its column's real type and
            # length-checked here, before anything is staged — see
            # _coerce_inspection. Overflows land in `errors` as a row message
            # instead of blowing up the shared commit later.
            inspection = IQCInspection(
                device_id=device.id,
                inspector_name=current_user.full_name,
                bios_password=("Yes" if bios_pwd_raw == "yes" else "No" if bios_pwd_raw == "no" else None),
                **_coerce_inspection(row, i, errors),
            )
            db.add(inspection)
            inserted += 1
        except Exception as e:
            errors.append(f"Row {i}: {str(e)}")

    # Single flush/commit for the whole batch — a flush per row was causing a
    # DB round-trip per device, slow enough on larger CSVs to trip a reverse
    # proxy's upstream timeout ("upstream error") before the response returned.
    #
    # Batching means a value the row-level checks did not catch is rejected
    # here, for the whole batch at once, far from the row that caused it. That
    # used to surface as an unhandled 500 with no indication of which file or
    # cell was at fault. Report it on the results page instead, so the upload
    # always ends somewhere the user can act on.
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        inserted = 0
        errors.append(
            "The batch could not be saved and no rows were imported. "
            "Fix the value below and re-upload: " + str(e).split("\n")[0]
        )
    if inserted == 0:
        to_stock_in = 0
    return templates.TemplateResponse("bulk_upload/result.html", {
        "request": request, "current_user": current_user,
        "upload_type": "Devices", "inserted": inserted, "errors": errors,
        "back_url": "/iqc",
        # Flow 1 GRN check: rows skipped for a bad GRN / invoice, as the
        # uploaded file's own rows plus a Reason column (downloadable).
        "grn_skipped": {"header": file_header + ["Reason"], "rows": grn_skipped} if grn_skipped else None,
        "to_stock_in": to_stock_in,
        "tag_duplicates": tag_duplicates,
        "lot_duplicates": list(lot_duplicates.values()),
    })


@router.post("/duplicates/approve-move")
async def approve_duplicate_move(
    request: Request,
    tag_number: str = Form(...),
    to_stage: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Approve-Move button on the duplicate-review modal's Tag Number table —
    moves a single already-existing device from its current stage to the
    stage requested by the uploaded file. Reuses the same validated-transition
    engine as every other stage-move flow in the app (services/control_engine)
    so bulk corrections can't skip stages the rest of the system assumes are
    sequential. Returns JSON (not a redirect) — the modal must stay open."""
    result = await db.execute(select(Device).where(Device.barcode == tag_number))
    device = result.scalar_one_or_none()
    if not device:
        return JSONResponse({"success": False, "error": f"Tag Number '{tag_number}' not found"}, status_code=404)

    is_admin = current_user.role.value == "admin"
    try:
        new_stage = DeviceStage(to_stage)
    except ValueError:
        return JSONResponse({"success": False, "error": f"Invalid stage '{to_stage}'"}, status_code=400)
    try:
        await validate_transition(device, new_stage, db, override_admin=is_admin)
    except HTTPException as e:
        return JSONResponse({"success": False, "error": e.detail}, status_code=e.status_code)

    prev = device.current_stage
    device.current_stage = new_stage
    device.updated_at = app_now()
    db.add(StageMovement(device_id=device.id, from_stage=prev, to_stage=new_stage,
                          moved_by=current_user.username, notes="Bulk Upload — duplicate review, Approve Move"))
    await audit(db, action="STAGE_MOVED", user=current_user,
                table_name="devices", record_id=str(device.id),
                old_value={"stage": str(prev)}, new_value={"stage": to_stage}, request=request)
    await db.commit()
    return JSONResponse({"success": True, "new_stage": new_stage.value,
                          "new_stage_label": STAGE_LABELS.get(new_stage, new_stage.value)})


# devicegrade is a real Postgres enum but GRN-style exports commonly write
# "B Grade", "Grade B", etc. Normalisation lives in utils/grades so the CSV
# path and the Customise-modal dropdowns accept exactly the same set of grades —
# they drifted before, which is how "E" worked in one place and not the other.
_parse_grade = parse_grade


def _apply_row_to_device(device: Device, row: dict, entity_lookup: dict | None = None,
                         dt_master: list | None = None, sc_master: list | None = None) -> None:
    """Apply one CSV row's field values onto an already-existing device —
    shared by the single-row Update and bulk Update All actions on the
    duplicate-review modal. Never touches barcode/lot_number (identity).

    entity_lookup maps lowercased entity name -> canonical spelling. An
    unrecognised entity is left alone rather than written through, matching
    the upload path's refusal to store a typo."""
    def s(key):
        return (row.get(key) or "").strip() or None

    def i(key):
        v = (row.get(key) or "").strip()
        m = re.search(r"-?\d+", v)
        return int(m.group()) if m else None

    if s("brand"): device.brand = s("brand")
    if s("model"): device.model = s("model")
    # Either column fills both (2026-10-22): a value in the file wins; a blank
    # one is taken from the other column (only if the tag has none yet).
    if s("device_type") or s("category"):
        dt, sc = pair_type_and_category(s("device_type") or device.device_type,
                                        s("category") or device.sub_category,
                                        dt_master or [], sc_master or [])
        if s("device_type") or not (device.device_type or "").strip():
            device.device_type = dt
        if s("category") or not (device.sub_category or "").strip():
            device.sub_category = sc
    if s("serial_no"): device.serial_no = s("serial_no")
    if s("cpu"): device.cpu = s("cpu")
    if s("cpu_make"): device.cpu_make = s("cpu_make")
    if s("generation"): device.generation = s("generation")
    if i("ram_gb") is not None: device.ram_gb = i("ram_gb")
    if i("storage_gb") is not None: device.storage_gb = i("storage_gb")
    if s("ram_summary"): device.ram_summary = s("ram_summary")
    if s("hdd_summary"): device.hdd_summary = s("hdd_summary")
    if s("storage_type"): device.storage_type = s("storage_type")
    if i("hdd_capacity_gb") is not None: device.hdd_capacity_gb = i("hdd_capacity_gb")
    if s("total_ram_count"): device.total_ram_count = s("total_ram_count")
    if s("total_ram_size"): device.total_ram_size = s("total_ram_size")
    if s("total_hdd_count"): device.total_hdd_count = s("total_hdd_count")
    if s("total_hdd_size"): device.total_hdd_size = s("total_hdd_size")
    # RAM / Storage columns (2026-10-22) — fill size, GB and count together.
    for k, v in upload_spec(_row_get(row, "RAM"), _row_get(row, "Storage")).items():
        if k.endswith("_count") and (getattr(device, k) or "").strip():
            continue   # keep an existing stick/drive count
        setattr(device, k, v)
    if s("invoice_number"): device.invoice_number = s("invoice_number")
    if s("screen_size"): device.screen_size = s("screen_size")
    if i("battery_health_pct") is not None: device.battery_health_pct = i("battery_health_pct")
    if s("color"): device.color = s("color")
    if _parse_grade(row.get("grade")) is not None: device.grade = _parse_grade(row.get("grade"))
    if s("grn_number"): device.grn_number = s("grn_number")
    if s("entity") and entity_lookup:
        canonical = entity_lookup.get(s("entity").lower())
        if canonical: device.entity = canonical
    device.updated_at = app_now()


@router.post("/duplicates/update")
async def update_duplicate_device(
    request: Request,
    tag_number: str = Form(...),
    row_json: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Update button on the duplicate-review modal's Tag Number table — applies
    this row's uploaded field values onto the existing device. Returns JSON so
    the modal stays open."""
    import json
    try:
        row = json.loads(row_json)
    except (ValueError, TypeError):
        return JSONResponse({"success": False, "error": "Malformed row data"}, status_code=400)
    device = (await db.execute(select(Device).where(Device.barcode == tag_number))).scalar_one_or_none()
    if not device:
        return JSONResponse({"success": False, "error": f"Tag Number '{tag_number}' not found"}, status_code=404)
    await _remember_grn_stock(db, [device.id])   # before the row may change its invoice
    _apply_row_to_device(device, row, {e.lower(): e for e in await entity_values(db)},
                         await master_values(db, "device_type"), await master_values(db, "sub_category"))
    await audit(db, action="DEVICE_UPDATED", user=current_user,
                table_name="devices", record_id=str(device.id),
                new_value={"source": "bulk_upload_duplicate_update"}, request=request)
    await db.commit()
    return JSONResponse({"success": True})


@router.post("/duplicates/update-all")
async def update_all_duplicate_devices(
    request: Request,
    rows_json: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Update All button — applies every listed duplicate row's uploaded
    values to its matching existing device in one pass."""
    import json
    try:
        rows = json.loads(rows_json)
    except (ValueError, TypeError):
        return JSONResponse({"success": False, "error": "Malformed row data"}, status_code=400)
    if not isinstance(rows, list):
        return JSONResponse({"success": False, "error": "Expected a list of rows"}, status_code=400)

    barcodes = [r.get("tag_number") for r in rows if r.get("tag_number")]
    devices = {d.barcode: d for d in (await db.execute(
        select(Device).where(Device.barcode.in_(barcodes))
    )).scalars().all()}

    # Fetched once for the whole batch rather than per row.
    entity_lookup = {e.lower(): e for e in await entity_values(db)}
    dt_master = await master_values(db, "device_type")
    sc_master = await master_values(db, "sub_category")

    updated, missing = [], []
    # GRN Stocked is add-only: remember these tags under their current
    # invoice's GRN before the rows below may change it.
    await _remember_grn_stock(db, [d.id for d in devices.values()])
    for r in rows:
        tag = r.get("tag_number")
        device = devices.get(tag)
        if not device:
            missing.append(tag)
            continue
        _apply_row_to_device(device, r.get("row") or {}, entity_lookup, dt_master, sc_master)
        updated.append(tag)

    if updated:
        await audit(db, action="DEVICE_UPDATED", user=current_user,
                    table_name="devices", record_id=None,
                    new_value={"source": "bulk_upload_duplicate_update_all", "tags": updated}, request=request)
        await db.commit()
    return JSONResponse({"success": True, "updated": updated, "missing": missing})


@router.post("/duplicates/add-lot")
async def add_duplicate_lot(
    request: Request,
    lot_number: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    """Add Lot button on the duplicate-review modal's Lot table — registers
    the lot shell (lot_number + today's purchase date + Unknown vendor) in
    Lot Management so a later normal upload/GRN can attach devices to it.
    Returns JSON (not a redirect) — the modal must stay open."""
    lot_number = (lot_number or "").strip()
    if not lot_number:
        return JSONResponse({"success": False, "error": "Lot Number is required"}, status_code=400)
    existing = (await db.execute(select(Lot).where(Lot.lot_number == lot_number))).scalar_one_or_none()
    if existing:
        return JSONResponse({"success": False, "error": f"Lot '{lot_number}' already exists"}, status_code=409)

    lot = Lot(
        lot_number=lot_number,
        supplier_name="Unknown",
        vendor_name="Unknown",
        buying_price=0,
        qty=1,
        purchase_date=app_now(),
        created_by=current_user.username if current_user else None,
    )
    db.add(lot)
    await db.flush()
    await audit(db, action="LOT_CREATED_FROM_DUPLICATE_REVIEW", user=current_user,
                table_name="lots", record_id=str(lot.id),
                new_value={"lot_number": lot.lot_number}, request=request)
    await db.commit()
    return JSONResponse({"success": True, "lot_id": str(lot.id)})


@router.post("/spare-parts")
async def upload_spare_parts(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allowed),
):
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    inserted, errors = 0, []

    for i, row in enumerate(reader, start=2):
        try:
            part_code = row.get("part_code", "").strip()
            name = row.get("name", "").strip()
            if not part_code or not name:
                errors.append(f"Row {i}: part_code and name are required")
                continue
            existing = await db.execute(select(SparePart).where(SparePart.part_code == part_code))
            if existing.scalar_one_or_none():
                errors.append(f"Row {i}: part_code '{part_code}' already exists")
                continue
            unit_price = row.get("unit_price", "0").strip()
            min_alert = row.get("min_stock_alert", "5").strip()
            part = SparePart(
                part_code=part_code, name=name,
                category=row.get("category", "Other").strip(),
                unit_price=float(unit_price) if unit_price else 0,
                min_stock_alert=int(min_alert) if min_alert else 5,
                supplier=row.get("supplier", "").strip() or None,
                notes=row.get("notes", "").strip() or None,
            )
            db.add(part)
            inserted += 1
        except Exception as e:
            errors.append(f"Row {i}: {str(e)}")

    await db.commit()
    return templates.TemplateResponse("bulk_upload/result.html", {
        "request": request, "current_user": current_user,
        "upload_type": "Spare Parts", "inserted": inserted, "errors": errors,
        "back_url": "/spare-parts",
    })


@router.post("/leads")
async def upload_leads(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bulk upload telecalling leads / lead-gen data from CSV."""
    content = await file.read()
    text    = content.decode("utf-8-sig")
    reader  = csv.DictReader(io.StringIO(text))
    inserted, errors = 0, []

    VALID_OUTCOMES = {"interested", "not_interested", "callback", "order_placed", "no_answer", "do_not_call", ""}
    VALID_CUST_TYPES = {"end_customer", "corporate", "individual", "dealer", "reseller", ""}

    for i, row in enumerate(reader, start=2):
        try:
            phone = row.get("phone", "").strip().replace(" ", "").replace("-", "")
            if not phone:
                errors.append(f"Row {i}: phone is required")
                continue

            # Parse optional numeric fields
            qty_raw    = row.get("qty_required", "").strip()
            budget_raw = row.get("budget", "").strip()
            qty    = int(qty_raw)    if qty_raw    else None
            budget = float(budget_raw) if budget_raw else None

            # Parse next_followup date
            followup_str = row.get("next_followup", "").strip()
            next_followup = None
            if followup_str:
                try:
                    next_followup = datetime.strptime(followup_str, "%Y-%m-%d")
                except ValueError:
                    errors.append(f"Row {i}: next_followup must be YYYY-MM-DD, got '{followup_str}'")
                    continue

            outcome    = row.get("call_outcome", "").strip().lower()
            cust_type  = row.get("customer_type", "").strip().lower()

            record = TelecallingRecord(
                customer_name  = row.get("customer_name", "").strip() or None,
                phone          = phone,
                email          = row.get("email", "").strip() or None,
                customer_type  = cust_type or None,
                city           = row.get("city", "").strip() or None,
                state          = row.get("state", "").strip() or None,
                category       = row.get("category", "").strip() or None,
                brand          = row.get("brand", "").strip() or None,
                model          = row.get("model", "").strip() or None,
                generation     = row.get("generation", "").strip() or None,
                processor      = row.get("processor", "").strip() or None,
                ram            = row.get("ram", "").strip() or None,
                hard_disk      = row.get("hard_disk", "").strip() or None,
                product_type   = row.get("product_type", "").strip() or None,
                grade          = row.get("grade", "").strip() or None,
                lot_reference  = row.get("lot_reference", "").strip() or None,
                quantity_required = qty,
                budget         = budget,
                call_outcome   = outcome or None,
                next_followup  = next_followup,
                notes          = row.get("notes", "").strip() or None,
                called_by      = current_user.username,
                product_interest = row.get("category", "").strip() or None,
            )
            db.add(record)
            inserted += 1
        except Exception as e:
            errors.append(f"Row {i}: {str(e)}")

    await db.commit()
    return templates.TemplateResponse("bulk_upload/result.html", {
        "request": request, "current_user": current_user,
        "upload_type": "Telecalling Leads", "inserted": inserted, "errors": errors,
        "back_url": "/telecalling",
    })
