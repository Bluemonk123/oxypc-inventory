"""E-Way Bill Number / Date (2026-11-03): required on Add GRN (/grn/post-iqc)
and Parts GRN (/parts-grn/new) only when the Invoice Price is above 50,000."""
import pathlib

from routers.grn import eway_missing, _GRN_REQUIRED

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_rule():
    assert eway_missing("50000", "", "") == []
    assert eway_missing("", "", "") == []
    assert eway_missing("50000.01", "", "") == ["E-Way Bill Number", "E-Way Bill Date"]
    assert eway_missing("1,20,000", "EWB1", "") == ["E-Way Bill Date"]
    assert eway_missing("75000", "EWB1", "2026-11-03") == []
    assert eway_missing("75000", None, None) == []          # field not sent: not checked
    assert all(k not in ("e_way_bill", "eway_bill_date") for _l, k in _GRN_REQUIRED)


def test_forms_toggle_required():
    grn = (ROOT / "templates/grn/post_iqc.html").read_text(encoding="utf-8")
    parts = (ROOT / "templates/spare_parts/parts_grn_form.html").read_text(encoding="utf-8")
    assert 'name="e_way_bill" id="{{ p }}eway" class="form-control eway-in">' in grn      # no fixed required
    assert "amt > 50000" in grn and "eway-star d-none" in grn
    assert 'name="eway_bill_number" id="hdrEwayNo" class="form-control form-control-sm">' in parts
    assert "> 50000" in parts and "eway-star d-none" in parts
