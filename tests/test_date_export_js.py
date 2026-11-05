"""static/js/date-export.js (2026-11-04): the browser half of the global date /
export format — Global Tables show '04 Nov 2026 17:42', CSVs built in the page
leave with YYYY-MM-DD dates and plain-number amounts (same rules as
utils/export_format.py). Run with node; skipped where node isn't installed."""
import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "js" / "date-export.js").read_text(encoding="utf-8")


def _node(script):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = "global.window={Blob:function(p,o){this.p=p;this.o=o;}};\n" + JS + "\n" + script
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_display_format():
    cases = ["04-11-2026", "04-11-2026 17:42", "2026-11-04 17:42:09", "2026-11-04", "04 Nov 2026",
             "04-Nov-2026", "04/11/2026 9:05 PM", "31-02-2026", "LOT-042", "4561300426L0001", "1,17,900"]
    out = _node("console.log(JSON.stringify(%s.map(t => window.gtFormatDate(window.gtParseDate(t)))))" % json.dumps(cases))
    assert out == ["04 Nov 2026", "04 Nov 2026 17:42", "04 Nov 2026 17:42", "04 Nov 2026", "04 Nov 2026",
                   "04 Nov 2026", "04 Nov 2026 21:05", "", "", "", ""]


def test_page_csv_normalised_like_server():
    from utils.export_format import normalize_csv
    csv_in = ("Tag Number,Date Added,Updated On,Unit Price,Qty,Check-in Time,SKU,Notes\r\n"
              '4561300426L0001,04 Nov 2026,04 Nov 2026 17:42,"₹1,17,900","1,200",04 Nov 2026 09:05,="00123","a, b"\r\n'
              "X,—,04-11-2026,− ₹800,3,,=\"9\",ok")
    js = _node("const b=new window.Blob([%s],{type:'text/csv;charset=utf-8'}); console.log(JSON.stringify(b.p[0]))" % json.dumps(csv_in))
    py = normalize_csv(csv_in.encode()).decode("utf-8")
    assert js.splitlines() == py.splitlines(), (js, py)
    assert "2026-11-04,2026-11-04,117900,1200,2026-11-04 09:05" in js
    # binary / non-CSV blobs are left alone
    raw = _node("const b=new window.Blob(['04-11-2026'],{type:'text/plain'}); console.log(JSON.stringify(b.p[0]))")
    assert raw == "04-11-2026"
