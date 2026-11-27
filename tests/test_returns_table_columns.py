"""Returns table (2026-11-27, Pankaj): new column set, no Type column, tag badge
Returned / Replaced with …, filter All / Replaced / Returned."""
import re
from pathlib import Path

SRC = Path("templates/sales/returns_list.html").read_text(encoding="utf-8")


def test_columns_in_order():
    head = SRC[SRC.index('<table id="returnsTable"'):SRC.index("</thead>")]
    assert re.findall(r"<th>([^<]+)</th>", head) == [
        "Return Date", "Tag No", "Serial No", "Make", "Model", "CPU", "Gen", "RAM", "Storage",
        "Sale Date", "Sale No", "Sales Person", "Return By", "Paid Repair", "Reason", "Condition", "Receipt"]
    assert "<th>Type</th>" not in SRC


def test_badges_and_filter():
    assert '>Returned</span>' in SRC and "{{ d.replaced }}" in SRC
    assert re.search(r'<option value="all" selected>All</option>\s*<option value="yes">Replaced</option>\s*<option value="no">Returned</option>', SRC)
