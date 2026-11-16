"""Trade Partner → Manage Lots → Asset IQC upload (2026-11-13, Pankaj).

Reads the External Partner "IQC Formats" sheet (37 columns, see COLUMNS) and
creates test tags: Entity External Partner Test, stage IQC, lot EPT-<LOT
Number> (created automatically when missing, restricted from the live
catalog), plus each tag's IQC record with the part conditions word-for-word.
Kept apart from live GRN / Lot / IQC like the rest of Manage Lots.

2026-11-16 (Pankaj): uploaded tags go straight to Ready for Sale and their
lots are open to every partner (not Restricted), so an upload shows on the
partner Catalog at once. Optional GRN / Invoice columns: either value finds a
test GRN (GRN & Lot tab) — the lot is linked to it and the tag mapped to it.
"""
import csv
import io
import re
import uuid
from decimal import Decimal

from sqlalchemy import select, func

from models.device import Device, DeviceStage, DeviceGrade, StageMovement
from models.grn_import import GRNImport, GRNLot
from models.iqc_inspection import IQCInspection
from models.lot import Lot
from models.master import (EXTERNAL_PARTNER_TEST_ENTITY, EXTERNAL_PARTNER_TEST_LOT_PREFIX,
                           EXTERNAL_PARTNER_TEST_GRN_SOURCE)
from utils.timezone import app_now

# (Excel header, where it goes, kind). kind: device = Device column,
# iqc = IQCInspection column (text as written), special = handled in code.
COLUMNS = [
    ("Tag No", "barcode", "special"),
    ("Grade", "grade", "special"),
    ("LOT Number", "lot", "special"),
    ("Make", "brand", "device"),
    ("Model", "model", "device"),
    ("CPU", "cpu", "device"),
    ("Generation", "generation", "device"),
    ("RAM", "ram_flag", "special"),
    ("RAM Capacity", "ram_gb", "special"),
    ("HDD", "hdd_flag", "special"),
    ("HDD Capacity", "hdd_capacity_gb", "special"),
    ("SSD Capacity", "storage_gb", "special"),
    ("Front Panel(Bazel)", "bezel", "iqc"),
    ("Panel", "panel", "iqc"),
    ("Screen Size", "screen_size", "device"),
    ("Screen", "screen", "iqc"),
    ("Hinge", "hinge_condition", "iqc"),
    ("Hinge Cover", "hinge_cover", "iqc"),
    ("Power Panel", "power", "iqc"),
    ("Touch Pad", "touchpad_working", "iqc"),
    ("Base", "bottom_base", "iqc"),
    ("Keyboard", "keyboard_working", "iqc"),
    ("USB", "port_usb_working", "iqc"),
    ("HDD Connector", "hdd_connector", "iqc"),
    ("Sound", "speaker_status", "iqc"),
    ("HDD Casing", "hdd_casing", "iqc"),
    ("Web Cam", "webcam_status", "iqc"),
    ("HDD Cover", "cover_storage", "iqc"),
    ("RAM Cover", "cover_ram", "iqc"),
    ("WIFI", "wifi_status", "iqc"),
    ("EXT Battery", "external_battery", "iqc"),
    ("Int. Battery", "internal_battery", "iqc"),
    ("Battery Cable", "battery_cable", "iqc"),
    ("DVD", "dvd_drive", "iqc"),
    ("DVD Flap", "cover_dvd", "iqc"),
    ("Inspector", "inspector_name", "iqc"),
    ("Remarks", "remarks", "iqc"),
    ("GRN", "grn", "special"),          # optional: test GRN number (2026-11-16)
    ("Invoice", "invoice", "special"),  # optional: that GRN's invoice number
]
HEADERS = [c[0] for c in COLUMNS]

SAMPLE_ROWS = [
    ["4782241026L0001", "C", "4782", "LENOVO", "ThinkPad X13", "I5", "10TH", "Yes", "16 GB", "No", "", "",
     "Ok", "Colour Fade", '14.0"', "Colour Spread", "Ok", "Ok", "NA", "Minor Scratch", "Major Scratch", "Not Checked",
     "Ok", "Yes", "Not Checked", "Yes", "Not Checked", "Ok", "Ok", "Not Checked", "NA", "Yes", "Yes", "NA", "NA",
     "karamveer shiv", ""],
    ["4782241026L0002", "C", "4782", "LENOVO", "ThinkPad T490", "I7", "8TH", "Yes", "16 GB", "No", "", "",
     "Rubber Cut", "Minor Scratch", '14.0"', "Colour Spread", "Ok", "Ok", "NA", "Minor Broken", "Minor Broken",
     "Not Checked", "Ok", "Yes", "Not Checked", "Yes", "Not Checked", "Ok", "Ok", "Not Checked", "NA", "Yes", "Yes",
     "NA", "NA", "karamveer shiv", ""],
]


def _key(h) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


_BY_KEY = {_key(h): (h, f, k) for h, f, k in COLUMNS}


def sample_xlsx() -> bytes:
    """Sample file: the 37 columns in order, with the 2 example rows."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "IQC Formats"
    ws.append(HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0D3B66")
    for r in SAMPLE_ROWS:
        ws.append(r)
    for i, h in enumerate(HEADERS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(12, len(h) + 2)
    ws.freeze_panes = "B2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def read_rows(filename: str, data: bytes) -> tuple[list[dict], list[str]]:
    """Rows as {field: text} plus a list of problems with the file itself.
    .xlsx: the "IQC Formats" sheet (else the first sheet with a "Tag No"
    header). .csv: UTF-8, falling back to Windows-1252."""
    name = (filename or "").lower()
    table = []
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheets = ([wb["IQC Formats"]] if "IQC Formats" in wb.sheetnames else []) + list(wb.worksheets)
        for ws in sheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            if rows and any(_key(c) == "tagno" for c in rows[0]):
                table = rows
                break
    elif name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        table = [r for r in csv.reader(io.StringIO(text))]
    else:
        return [], ["Upload the .xlsx (or .csv) IQC format file."]
    if not table:
        return [], ["No sheet with a 'Tag No' column was found."]
    header = [_key(h) for h in table[0]]
    cols = {}
    for i, hk in enumerate(header):
        if hk in _BY_KEY and _BY_KEY[hk][1] not in cols:
            cols[_BY_KEY[hk][1]] = i
    problems = []
    if "barcode" not in cols:
        problems.append("The 'Tag No' column is missing.")
    unknown = [str(table[0][i]) for i, hk in enumerate(header) if hk and hk not in _BY_KEY]
    if unknown:
        problems.append("Columns not used: " + ", ".join(unknown))
    out = []
    for r in table[1:]:
        row = {}
        for f, i in cols.items():
            v = r[i] if i < len(r) else None
            row[f] = ("" if v is None else str(v)).strip()
        if any(row.values()):
            out.append(row)
    return out, problems


def _num(text) -> int | None:
    m = re.search(r"(\d+(?:\.\d+)?)", str(text or ""))
    return int(float(m.group(1))) if m else None


def _yes(text) -> bool:
    return str(text or "").strip().lower() in ("yes", "y", "true", "1")


async def _find_test_grn(db, cache: dict, grn_no: str, invoice: str):
    """The test GRN (Manage Lots → GRN & Lot) whose GRN number or invoice
    number matches; None when neither is filled or nothing matches."""
    from sqlalchemy import or_
    key = (grn_no.lower(), invoice.lower())
    if key in cache:
        return cache[key]
    conds = []
    if grn_no:
        conds.append(func.lower(GRNImport.grn_number) == grn_no.lower())
    if invoice:
        conds.append(func.lower(GRNImport.invoice_number) == invoice.lower())
    g = None
    if conds:
        g = (await db.execute(select(GRNImport).where(
            GRNImport.source == EXTERNAL_PARTNER_TEST_GRN_SOURCE, GRNImport.is_deleted == False,  # noqa: E712
            or_(*conds)).order_by(GRNImport.created_at.desc()))).scalars().first()
    cache[key] = g
    return g


async def import_rows(db, rows: list[dict], user) -> dict:
    """Create the test tags (straight to Ready for Sale). Returns counts, the
    lots created, the GRNs linked, and per-row errors / skips. Caller commits."""
    from services.location_defaults import ensure_stage_location
    created_lots, inserted, skipped, errors = [], 0, [], []
    grn_cache, linked, unmatched, touched_lots, new_devs, lot_grn = {}, set(), [], {}, [], {}
    seen = set()
    existing = set()
    tags = [r.get("barcode", "") for r in rows if r.get("barcode")]
    if tags:
        existing = {b.lower() for b in (await db.execute(
            select(Device.barcode).where(func.lower(Device.barcode).in_([t.lower() for t in tags])))).scalars()}
    lot_cache = {}
    now = app_now()
    pre = EXTERNAL_PARTNER_TEST_LOT_PREFIX
    for n, r in enumerate(rows, start=2):           # row 1 is the header
        tag = r.get("barcode", "")
        if not tag:
            errors.append(f"Row {n}: Tag No is blank")
            continue
        if tag.lower() in existing or tag.lower() in seen:
            skipped.append(f"Row {n}: {tag} already exists")
            continue
        raw_lot = r.get("lot", "")
        if not raw_lot:
            errors.append(f"Row {n}: {tag} has no LOT Number")
            continue
        lot_no = raw_lot if raw_lot.upper().startswith(pre.upper()) else pre + raw_lot
        grn_no, invoice = (r.get("grn") or "").strip(), (r.get("invoice") or "").strip()
        grn = await _find_test_grn(db, grn_cache, grn_no, invoice)
        if (grn_no or invoice) and grn is None:
            unmatched.append(f"Row {n}: {tag} — GRN / Invoice '{grn_no or invoice}' not found")
        lot = lot_cache.get(lot_no.lower())
        if lot is None:
            lot = (await db.execute(select(Lot).where(func.lower(Lot.lot_number) == lot_no.lower()))).scalars().first()
            if lot is not None and lot.is_trashed:
                errors.append(f"Row {n}: {tag} — lot {lot.lot_number} is in Trash")
                continue
            if lot is None:
                lot = Lot(lot_number=lot_no[:50], supplier_name="External Partner Test", buying_price=Decimal("0"),
                          qty=1, purchase_date=now, is_restricted=False, created_by=user.username,
                          notes="Created by Manage Lots → Asset IQC upload")
                db.add(lot)
                await db.flush()
                created_lots.append(lot.lot_number)
            lot.is_restricted = False          # open to every partner (2026-11-16)
            lot_cache[lot_no.lower()] = lot
        touched_lots[lot.id] = lot
        if grn is not None:
            if (grn.id, lot.id) not in linked:
                have = (await db.execute(select(GRNLot.id).where(GRNLot.grn_id == grn.id,
                                                                 GRNLot.lot_id == lot.id))).scalar_one_or_none()
                if have is None:
                    db.add(GRNLot(grn_id=grn.id, lot_id=lot.id, created_by=user.username))
                linked.add((grn.id, lot.id))
            lot_grn.setdefault(lot.id, grn.grn_number)
            if not lot.grn_system_number:
                lot.grn_system_number = grn.grn_number
            if grn.lot_id is None:
                grn.lot_id, grn.lot_number = lot.id, lot.lot_number[:60]
        g = (r.get("grade") or "").strip()
        grade = next((x for x in DeviceGrade if x.value.lower() == g.lower()), None)
        ssd = _num(r.get("storage_gb"))
        dev = Device(
            id=uuid.uuid4(), barcode=tag[:100], lot_id=lot.id, entity=EXTERNAL_PARTNER_TEST_ENTITY,
            brand=r.get("brand") or None, model=r.get("model") or None, cpu=r.get("cpu") or None,
            generation=r.get("generation") or None,
            ram_gb=_num(r.get("ram_gb")) if _yes(r.get("ram_flag")) or (not r.get("ram_flag") and r.get("ram_gb")) else None,
            hdd_capacity_gb=_num(r.get("hdd_capacity_gb")) if _yes(r.get("hdd_flag")) else None,
            storage_gb=ssd, storage_type="SSD" if ssd else None,
            screen_size=r.get("screen_size") or None, grade=grade, current_stage=DeviceStage.ready_to_sale, qty=1,
            grn_number=grn.grn_number if grn is not None else None,
        )
        db.add(dev)
        new_devs.append(dev)
        db.add(StageMovement(device_id=dev.id, from_stage=None, to_stage=DeviceStage.ready_to_sale,
                             moved_by=user.username, notes="Manage Lots — Asset IQC upload, straight to Ready for Sale"))
        try:
            await ensure_stage_location(db, dev, DeviceStage.ready_to_sale, user)
        except Exception:
            pass   # a missing default location must not block the upload
        iqc = {f: (r.get(f) or None) for h, f, k in COLUMNS if k == "iqc"}
        iqc = {f: (v[:40] if v and f not in ("remarks", "inspector_name") else v) for f, v in iqc.items()}
        if iqc.get("inspector_name"):
            iqc["inspector_name"] = iqc["inspector_name"][:100]
        db.add(IQCInspection(device_id=dev.id, inspected_at=now,
                             **{**iqc, "inspector_name": iqc.get("inspector_name") or (user.full_name or user.username)}))
        seen.add(tag.lower())
        inserted += 1
    # A row with GRN / Invoice blank, in a lot this upload linked to a GRN, is
    # mapped to that GRN too (one filled row per lot is enough).
    for dev in new_devs:
        if not dev.grn_number and dev.lot_id in lot_grn:
            dev.grn_number = lot_grn[dev.lot_id]
    # Lot quantity = its test tags, so a booking's per-unit price (lot price ÷
    # lot qty) is right; an auto-created lot starts at 1.
    if touched_lots:
        await db.flush()
        counts = dict((await db.execute(
            select(Device.lot_id, func.count(Device.id)).where(
                Device.lot_id.in_(list(touched_lots)), Device.is_active == True,  # noqa: E712
                Device.is_trashed.isnot(True)).group_by(Device.lot_id))).all())
        for lid, lot in touched_lots.items():
            if counts.get(lid):
                lot.qty = counts[lid]
    grns = sorted({g.grn_number for g in grn_cache.values() if g is not None})
    return {"inserted": inserted, "created_lots": created_lots, "skipped": skipped, "errors": errors,
            "grns": grns, "unmatched": unmatched}


# Human-readable "Saved as" for the Column mapping table on the Asset IQC tab.
_LABELS = {
    "barcode": "Tag number (required; existing tags are skipped)",
    "grade": "Grade (A / B / C / D / E / scrap)",
    "lot": f"Test lot — 4782 → {EXTERNAL_PARTNER_TEST_LOT_PREFIX}4782 (created if missing)",
    "brand": "Brand", "model": "Model", "cpu": "CPU", "generation": "Generation",
    "ram_flag": "RAM present? (Yes / No)", "ram_gb": "RAM (GB) — when RAM = Yes",
    "hdd_flag": "HDD present? (Yes / No)", "hdd_capacity_gb": "HDD (GB) — when HDD = Yes",
    "storage_gb": "SSD (GB), storage type SSD", "screen_size": "Screen size",
    "bezel": "IQC · Bezel", "panel": "IQC · Panel", "screen": "IQC · Screen", "power": "IQC · Power",
    "bottom_base": "IQC · Bottom Base", "external_battery": "IQC · External Battery",
    "internal_battery": "IQC · Internal Battery", "hinge_condition": "IQC · Hinge condition",
    "hinge_cover": "IQC · Hinge cover", "touchpad_working": "IQC · Touchpad", "keyboard_working": "IQC · Keyboard",
    "port_usb_working": "IQC · USB port", "hdd_connector": "IQC · HDD connector", "speaker_status": "IQC · Speaker",
    "hdd_casing": "IQC · HDD casing", "webcam_status": "IQC · Webcam", "cover_storage": "IQC · Storage cover",
    "cover_ram": "IQC · RAM cover", "wifi_status": "IQC · WiFi", "battery_cable": "IQC · Battery cable",
    "dvd_drive": "IQC · DVD drive", "cover_dvd": "IQC · DVD cover", "inspector_name": "IQC · Inspector",
    "remarks": "IQC · Remarks",
    "grn": "Optional — test GRN number: links the lot to that GRN and maps its tags (one filled row per lot is enough)",
    "invoice": "Optional — that GRN's invoice number (used when GRN is blank)",
}
MAPPING = [(h, _LABELS.get(f, f)) for h, f, k in COLUMNS]
