"""All Inventory export: trailing "PNA" and "Stock" columns.

PNA   = "Yes" only when the tag is in the Production Manager PNA summary scope
        (active PNA part AND stage in l1/l2/l3 AND is_active AND not trashed).
Stock = ready_to_sale AND sold tags: sub-lot present -> "As-Is", else "Finish Good"
        (so sold items filter by what they were); every other stage -> "In Progress".

Calls routers.devices._export_rows directly with transient Device objects and
a stubbed session, so no database is touched.
"""
import asyncio
import csv
import io
import uuid
from types import SimpleNamespace

from models.device import Device, DeviceStage
from routers import devices as devices_router
from routers.devices import _EXPORT_HEADER, _export_rows, _pna_label, _stock_label


def _dev(barcode, stage, sub_lot=None, is_active=True, is_trashed=False):
    return Device(
        id=uuid.uuid4(), barcode=barcode, current_stage=stage,
        sub_lot_number=sub_lot, is_active=is_active, is_trashed=is_trashed,
    )


class _EmptyResult:
    def __init__(self, items=()):
        self._items = list(items)

    def scalars(self):
        return self

    def all(self):
        return self._items


class _StubDB:
    """Every lookup comes back empty except the active-PNA-part query, which
    returns pna_ids (the export queries device_pna_parts directly)."""
    def __init__(self, pna_ids=()):
        self.pna_ids = pna_ids

    async def execute(self, stmt, *_a, **_k):
        if "device_pna_parts" in str(stmt):     # (device_id, part_name) rows (Part Name column, 2026-11-23)
            return _EmptyResult([(i, n) for i in self.pna_ids for n in ("Camera", "Screen")])
        return _EmptyResult()


def _run_export(monkeypatch, devices, pna_ids):
    async def fake_pna(_db, ids):
        return {i: [SimpleNamespace()] for i in ids if i in pna_ids}

    async def fake_locmap(_db, _ids):
        return {}

    monkeypatch.setattr("services.pna_lookup.active_pna_parts", fake_pna)
    monkeypatch.setattr(devices_router, "_build_location_map", fake_locmap)

    async def go():
        resp = await _export_rows(_StubDB(pna_ids), [(d, "LOT-1") for d in devices])
        return resp.body

    import openpyxl   # the export is .xlsx (2026-11-18)
    wb = openpyxl.load_workbook(io.BytesIO(asyncio.run(go())))
    rows = [["" if v is None else v for v in r] for r in wb.active.iter_rows(values_only=True)]
    return rows[0], {r[0]: r for r in rows[1:]}


def test_header_sequence_matches_pankaj_sheet():
    # Column order of the export (2026-11-18, Pankaj's devices_export.xlsx).
    i = _EXPORT_HEADER.index("Device Price")
    assert _EXPORT_HEADER[i:i + 20] == [
        "Device Price", "PNA", "Part Name", "Stock", "Grade", "Stage", "Final QC Status",
        "L3/L4 Scrap Reason", "L3/L4 Scrap Notes", "Final QC Fail Reason", "Final QC Fail Notes",
        "Pre-Sale QC Fail Reason", "Pre-Sale QC Fail Notes",   # 2026-11-27
        "Location ID", "Bin Location", "Zone", "Notes", "Created", "Updated", "Stage History"]
    assert "Warehouse" not in _EXPORT_HEADER
    assert _EXPORT_HEADER[i + 20:] == [
        "Stock In (Date)", "Stock In (Ageing)", "L1/L2 (Date)", "L1/L2 (Ageing)", "L3/L4 (Date)", "L3/L4 (Ageing)",
        "Stress (Date)", "Stress (Ageing)", "Cosmetic (Date)", "Cosmetic (Ageing)", "Final QC (Date)", "Final QC (Ageing)",
        "Ready for Sale (Date)", "Ready for Sale (Ageing)"]
    assert len(_EXPORT_HEADER) == 81   # + 6 Scrap / Fail Reason + Notes (2026-11-27)   # + Part Name (2026-11-23), + GRN Date (2026-11-26)
    assert _EXPORT_HEADER[2:4] == ["GRN", "GRN Date"]


def test_stock_label_rules():
    assert _stock_label(_dev("A", DeviceStage.ready_to_sale)) == "Finish Good"
    assert _stock_label(_dev("B", DeviceStage.ready_to_sale, sub_lot="  ")) == "Finish Good"
    assert _stock_label(_dev("C", DeviceStage.ready_to_sale, sub_lot="SL-9")) == "As-Is"
    assert _stock_label(_dev("D", DeviceStage.l2, sub_lot="SL-9")) == "As-Is"   # a sub-lot is As-Is at any stage (stock_type_key)
    assert _stock_label(_dev("E", DeviceStage.iqc)) == "In Progress"
    # sold items carry the same Finish Good / As-Is label (never "In Progress")
    assert _stock_label(_dev("F", DeviceStage.sold)) == "Finish Good"
    assert _stock_label(_dev("G", DeviceStage.sold, sub_lot="SL-9")) == "As-Is"


def test_pna_label_scope():
    ok = _dev("A", DeviceStage.l1)
    assert _pna_label(ok, {ok.id}) == "Yes"
    for stage in (DeviceStage.l2, DeviceStage.l3):
        d = _dev("S", stage)
        assert _pna_label(d, {d.id}) == "Yes"
    other_stage = _dev("B", DeviceStage.ready_to_sale)
    assert _pna_label(other_stage, {other_stage.id}) == "No"
    no_part = _dev("C", DeviceStage.l1)
    assert _pna_label(no_part, set()) == "No"
    inactive = _dev("D", DeviceStage.l1, is_active=False)
    assert _pna_label(inactive, {inactive.id}) == "No"
    trashed = _dev("E", DeviceStage.l1, is_trashed=True)
    assert _pna_label(trashed, {trashed.id}) == "No"


def test_export_rows_end_to_end_values(monkeypatch):
    pna_l2 = _dev("T-PNA-L2", DeviceStage.l2)
    l2_no_pna = _dev("T-L2-CLEAN", DeviceStage.l2)
    pna_other_stage = _dev("T-PNA-RTS", DeviceStage.ready_to_sale)          # PNA row, wrong stage
    rts_plain = _dev("T-RTS", DeviceStage.ready_to_sale)
    rts_sublot = _dev("T-RTS-SUB", DeviceStage.ready_to_sale, sub_lot="SL-1")
    iqc_dev = _dev("T-IQC", DeviceStage.iqc)
    sold_dev = _dev("T-SOLD", DeviceStage.sold)
    # l2_no_pna stands in for a device whose only PNA row is inactive: the
    # lookup (active_pna_parts) never returns it, so it is absent from pna_ids.
    pna_ids = {pna_l2.id, pna_other_stage.id}

    header, by_barcode = _run_export(
        monkeypatch,
        [pna_l2, l2_no_pna, pna_other_stage, rts_plain, rts_sublot, iqc_dev, sold_dev],
        pna_ids,
    )
    assert header == _EXPORT_HEADER
    pna_i, stock_i = header.index("PNA"), header.index("Stock")

    def cells(b):
        return by_barcode[b][pna_i], by_barcode[b][stock_i]

    assert cells("T-PNA-L2") == ("Yes", "In Progress")
    assert cells("T-L2-CLEAN") == ("No", "In Progress")
    assert cells("T-PNA-RTS") == ("No", "Finish Good")
    assert cells("T-RTS") == ("No", "Finish Good")
    assert cells("T-RTS-SUB") == ("No", "As-Is")
    assert cells("T-IQC") == ("No", "In Progress")
    assert cells("T-SOLD") == ("No", "Finish Good")
    assert all(len(r) == len(header) for r in by_barcode.values())
    # Part Name right after PNA: the PNA parts " / " joined, only when PNA = Yes (like the PNA Summary)
    part_i = header.index("Part Name")
    assert part_i == pna_i + 1
    assert by_barcode["T-PNA-L2"][part_i] == "Camera / Screen"
    assert by_barcode["T-PNA-RTS"][part_i] == "" and by_barcode["T-L2-CLEAN"][part_i] == ""


def test_stock_in_date_is_registered_date():
    # 2026-11-26 (Pankaj): Stock In (Date) = the tag's Registered (created)
    # date, even when the stage log has an older Stock In entry.
    from datetime import datetime
    from routers.devices import _ageing_cols
    d = _dev("R1", DeviceStage.ready_to_sale)
    d.created_at = datetime(2026, 8, 10, 12, 32)
    moves = [(DeviceStage.stock_in, datetime(2025, 1, 5)), (DeviceStage.ready_to_sale, datetime(2026, 9, 1))]
    cols = _ageing_cols(d, moves, datetime(2026, 11, 26))
    assert cols[0] == datetime(2026, 8, 10).date()
    assert cols[1] == (datetime(2026, 11, 26) - datetime(2026, 8, 10)).days
    # no created date: old order (Stock In step) still applies
    d.created_at = None
    assert _ageing_cols(d, moves, datetime(2026, 11, 26))[0] == datetime(2025, 1, 5).date()
