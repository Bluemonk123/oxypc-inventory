"""Register Device — RAM / Hard Drive rows (2026-11-24, Pankaj):
rows read RAM · Total RAM Size · Total RAM Count and Hard Drive · Total Hard
Drive Size · Total Hard Drive Count; filling RAM / Hard Drive, or the Total
Size (one size, or parts split by "," / "+"), fills the other two — on the
page (JS) and on save (fill_defaults) when a field is still blank."""
import pathlib
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from utils.device_spec import fill_defaults

ROOT = pathlib.Path(__file__).resolve().parent.parent
_T = ROOT / "templates"
FORM = "".join((_T / f).read_text() for f in ("iqc/form.html", "iqc/_spec_autofill.html", "iqc/_function_tests.html"))
EDIT = (_T / "devices/edit.html").read_text()


def test_row_order():
    names = re.findall(r'data-spec="(\w+)"', FORM)
    assert names == ["ram_summary", "total_ram_size", "total_ram_count",
                     "hdd_summary", "total_hdd_size", "total_hdd_count"]
    # counts start at 0 (2026-11-24) and a 0 count is still filled from the sizes
    for n in ("total_ram_count", "total_hdd_count"):
        assert f"device.{n} if device and device.{n} else '0' }}}}\"{{% if not (device and device.{n}) %}} data-auto=\"1\"" in FORM
    assert "f.value.trim() !== '0'" in FORM and "put(count, '0')" in FORM


def _dev(**kw):
    base = dict(ram_gb=None, ram_summary=None, total_ram_size=None, total_ram_count=None,
                storage_gb=None, hdd_capacity_gb=None, hdd_summary=None, total_hdd_size=None, total_hdd_count=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_save_fills_from_total_size_with_commas():
    ch = fill_defaults(_dev(total_ram_size="8GB DDR4, 8GB DDR4", total_hdd_size="256GB SSD, 1TB HDD"))
    assert ch["ram_summary"] == "16GB" and ch["total_ram_count"] == "2"
    assert ch["hdd_summary"] == "1280GB" and ch["total_hdd_count"] == "2"


def test_save_fills_from_ram_and_hard_drive():
    ch = fill_defaults(_dev(ram_summary="16GB", hdd_summary="512GB"))
    assert ch["total_ram_size"] == "16GB" and ch["total_ram_count"] == "1"
    assert ch["total_hdd_size"] == "512GB" and ch["total_hdd_count"] == "1"


def test_save_keeps_typed_values():
    ch = fill_defaults(_dev(ram_summary="16GB", total_ram_size="8GB, 8GB", total_ram_count="2"))
    assert "ram_summary" not in ch and "total_ram_size" not in ch and "total_ram_count" not in ch


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_page_js_parts_and_totals():
    js = FORM.split("RAM / Hard Drive autofill", 1)[1]
    body = js[js.index("var SIZED"):js.index("function el(")]
    src = body + """
const t = (v) => { const p = parts(v); return [gbText(p.reduce((a,b)=>a+b,0)), p.length]; };
console.log(JSON.stringify([t('8GB, 8GB'), t('8GB+8GB'), t('16GB_DDR4_2300_Samsung, 8GB_DDR4_2133_Crucial'),
  t('512GB SSD, 512GB SSD'), t('16'), t('1TB'), spaced('16GB'), spaced('16'), t('')]));"""
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ('[["16 GB",2],["16 GB",2],["24 GB",2],["1 TB",2],["16 GB",1],'
                                  '["1 TB",1],"16 GB","16 GB",["",0]]')
