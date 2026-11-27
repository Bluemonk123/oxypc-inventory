"""One warranty rule everywhere (2026-11-27, Pankaj): in warranty only when a
warranty was picked on the sale and today is on/before its end date; no
warranty picked = No Warranty. Ready to Dispatch keeps the end date."""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS

from utils.timezone import app_now
from utils.warranty import effective_warranty_for_sale, warranty_status_for_sale


def _sale(wtype, days_from_now, sold_days_ago=5):
    now = app_now()
    return NS(warranty_type=wtype, sold_at=now - timedelta(days=sold_days_ago),
              warranty_expires_at=(now + timedelta(days=days_from_now)) if days_from_now is not None else None)


def test_rules_agree():
    cases = [(_sale("none", None), None, "no_warranty"),          # nothing picked: no 30-day default
             (_sale("30_days", 10), "active", "in_warranty"),
             (_sale("6_months", -3), "expired", "out_of_warranty"),
             (None, None, "no_warranty")]
    for sale, eff, rma in cases:
        w = effective_warranty_for_sale(sale)
        assert (w["status"] if w else None) == eff
        assert warranty_status_for_sale(sale) == rma


def test_ready_to_dispatch_keeps_end_date():
    src = Path("routers/stock.py").read_text(encoding="utf-8")
    assert "warranty_expires_at = orig_sale.warranty_expires_at" in src


def test_return_stock_warranty_column_before_stage():
    src = Path("templates/lots/stock_in.html").read_text(encoding="utf-8")
    assert "<th>Part Cost</th><th>Warranty</th><th>Current Stage</th><th>Action</th>" in src
    assert "targets: [0, 15]" in src


def test_no_page_uses_the_30_day_rule():
    for f in ("routers/sales.py", "routers/dispatch.py", "routers/devices.py"):
        body = Path(f).read_text(encoding="utf-8")
        assert "warranty_from_sold_at(" not in body, f
