"""Register Device — functional tests (2026-11-24, Pankaj):
Speaker Status next to Power Status, Storage Health next to Keyboard Working;
the Keyboard test has no click box any more (F1–F12 can be ticked by hand);
a Touchpad test (play button under Touchpad Working) fills Touchpad Working,
Click Function Working and Touchpad Missing."""
import pathlib
import re

_T = pathlib.Path(__file__).resolve().parent.parent / "templates"
FORM = "".join((_T / f).read_text() for f in ("iqc/form.html", "iqc/_spec_autofill.html", "iqc/_function_tests.html"))
EDIT = (_T / "devices/edit.html").read_text()


def test_field_order():
    names = re.findall(r'name="(status|speaker_status|keyboard_working|storage_health_pct|'
                       r'touchpad_click_working|touchpad_working|touchpad_missing)"', FORM)
    body = [n for n in names][:7]
    assert body == ["status", "speaker_status", "keyboard_working", "storage_health_pct",
                    "touchpad_working", "touchpad_missing", "touchpad_click_working"]


def test_keyboard_modal_keys_only():
    kb = FORM.split('id="kbClickTestModal"', 1)[1].split('id="tpTestModal"', 1)[0]
    assert "kbClickArea" not in FORM and "Left-click" not in kb
    assert "F1–F12" in kb and "kbExtraKeys" in kb
    assert "mark(code, 'hand')" in FORM                    # click a key to tick it by hand


def test_touchpad_modal_fills_three_fields():
    assert 'id="tpTestBtn"' in FORM and 'data-bs-target="#tpTestModal"' in FORM
    js = FORM.split("Touchpad & Click Test (2026-11-24", 1)[1].split("</script>", 1)[0]
    for n in ("touchpad_working", "touchpad_click_working", "touchpad_missing"):
        assert f"set('{n}'" in js
    assert "tpBtn.disabled = noPower" in FORM                # no-power gating covers it too


def test_device_edit_matches_register_device():
    names = re.findall(r"isel\('(status|speaker_status|keyboard_working|touchpad_working|touchpad_missing|"
                       r"touchpad_click_working)'|name=\"(storage_health_pct)\"", EDIT)
    order = [a or b for a, b in names]
    assert order == ["status", "speaker_status", "keyboard_working", "storage_health_pct",
                     "touchpad_working", "touchpad_missing", "touchpad_click_working"]
    assert 'id="kbClickTestBtn"' in EDIT and 'id="tpTestBtn"' in EDIT
    assert '{% include "iqc/_function_tests.html" %}' in EDIT
    assert '{% include "iqc/_spec_autofill.html" %}' in EDIT
    assert re.findall(r'data-spec="(\w+)"', EDIT) == [
        "ram_gb", "total_ram_size", "total_ram_count", "ram_summary",
        "storage_gb", "total_hdd_size", "total_hdd_count", "hdd_summary"]
    assert "device.total_ram_count or '0'" in EDIT and "device.total_hdd_count or '0'" in EDIT
