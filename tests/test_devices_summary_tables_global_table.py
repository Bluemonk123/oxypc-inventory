"""Model Based Summary and Lot Based Summary (templates/devices/list.html) —
the two client-side tables on the "All Inventory" page below the main
#devicesTable — moved onto the Global Table module 2026-09-03. #devicesTable
itself was already on it; this closes the "All Inventory (All tables)" gap.

Each table's old custom header search box is gone, replaced by
initGlobalTable's own toolbar search box:
  - Model Based Summary's old box only ever searched the Model Names column
    (dtModel.column(0).search(...)) — preserved via searchable:false on
    every other column, so the relocated box can't start matching
    Make/Device Type/counts too.
  - Lot Based Summary's old box searched every column (dtLot.search(...),
    a plain global search) — no restriction needed, initGlobalTable's own
    box reproduces that as-is. Its placeholder also carried a copy-pasted
    "Search Model Names…" string (should always have said "Search Lots…")
    — fixed in passing since the whole input was already being touched.
"""
import pathlib

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _read():
    return (pathlib.Path(ROOT) / "templates" / "devices" / "list.html").read_text(encoding="utf-8")


def test_model_summary_table_uses_global_table():
    src = _read()
    assert "initGlobalTable('#modelSummaryTable'" in src
    assert 'id="modelSearchBox"' not in src  # replaced by the module's own search box


def test_model_summary_search_covers_make_model_cpu_ram_storage():
    # 2026-10-10: the search box covers Make / Model / CPU / RAM / Storage
    # (cols 0,1,3,4,5). Still NOT targets:'_all' + a same-key override —
    # DataTables resolves '_all' columnDefs last regardless of array position
    # (found live 2026-09-03), so the not-searchable indices are listed.
    src = _read()
    block = src[src.index("initGlobalTable('#modelSummaryTable'"):][:700]
    assert "_all" not in block
    assert "MODEL_NOT_SEARCHABLE_COLS" in block
    assert "[2, 6, 7, 8, 9, 10, 11] : [2, 6, 7, 8, 9, 10]" in src
    assert "Search Make, Model, CPU, RAM, Storage…" in src


def test_model_summary_has_total_row_and_own_filters():
    src = _read()
    assert 'id="msTotalRow"' in src
    assert "rows({ search: 'applied' })" in src      # total follows the search
    for f in ("s_entity", "s_make", "s_device_type", "s_touch", "s_stock", "s_grade"):
        assert f"multiselect_inline('{f}'" in src
    assert "/devices/model-summary/data" in src
    assert "<th>Repaired</th>" not in src


def test_lot_summary_table_uses_global_table():
    src = _read()
    assert "initGlobalTable('#lotSummaryTable'" in src
    assert 'id="lotSearchBox"' not in src  # replaced by the module's own search box
    assert "Search Lots…" in src  # was mislabeled "Search Model Names…" before this batch


def test_lot_summary_checkbox_and_customise_button_still_wired():
    src = _read()
    assert 'id="lotSelectAllChk"' in src
    assert 'id="customiseLotBtn"' in src
    assert "updateLotSel" in src


def test_lot_summary_remarks_cell_is_no_longer_manually_pre_truncated():
    # A [:150]-sliced cell with its own title= tooltip fought
    # initGlobalTable's own clamp+tooltip (which reads the *rendered* cell
    # text, not the td's title attribute) — the tooltip would have shown
    # only the already-cut text instead of the real full note. Emitting the
    # full value lets the Global Table clamp+tooltip do the job correctly.
    src = _read()
    assert "l.notes[:150]" not in src
    assert 'class="small note-clamp"' not in src
    assert "{{ l.notes or '' }}" in src


def test_both_summary_tables_have_no_table_responsive_wrapper():
    src = _read()
    for table_id in ("modelSummaryTable", "lotSummaryTable"):
        table_pos = src.index(f'id="{table_id}"')
        preceding = src[:table_pos]
        assert '<div class="table-responsive">' not in preceding[-300:], table_id
