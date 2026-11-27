"""Data Fixes → Tags in the old L2 stage (2026-11-26): card + admin route."""
import re
from datetime import datetime
from pathlib import Path

from jinja2 import Environment

from auth.route_actions import ROUTE_ACTIONS


def _card():
    src = Path("templates/admin/data_fixes.html").read_text(encoding="utf-8")
    m = re.search(r"\{# ── Tags in the old L2 stage.*?(?=\s*\{# ── GRNs with no GRN Date)", src, re.S)
    assert m
    return Environment().from_string(m.group(0))


def test_route_mapped_admin_only():
    assert ("POST", "/admin/system/l2-to-stock-in") in ROUTE_ACTIONS
    src = Path("routers/system_health.py").read_text(encoding="utf-8")
    i = src.index('@router.post("/l2-to-stock-in")')
    assert "Depends(admin_only)" in src[i:i + 300]
    assert "reassign_open_work_orders" in src[i:i + 2500]
    assert "DeviceStage.stock_in" in src[i:i + 2500]


def test_card_lists_tags_and_button():
    html = _card().render(l2_total=1, csrf="c", l2_tags=[
        {"barcode": "T1", "entity": "OxyPC Computers", "since": datetime(2026, 5, 1), "wos": 1}])
    assert "Move 1 to Stock In" in html and "T1" in html and "01-05-2026" in html
    assert "Nothing to fix" in _card().render(l2_total=0, l2_tags=[], csrf="c")
