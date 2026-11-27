"""Manage Lots (2026-11-27, Pankaj): Bulk Set Price on the Lots and Tags tabs,
row Set Price (pencil icon) on both, tag price = lot price split over its tags
unless the tag has its own price."""
from pathlib import Path

from auth.route_actions import ROUTE_ACTIONS

SRC = Path("templates/trade_partner/manage_lots.html").read_text(encoding="utf-8")
PY = Path("routers/partner_admin.py").read_text(encoding="utf-8")


def test_buttons_and_modals():
    assert SRC.count('class="btn btn-sm btn-outline-success bulk-price-btn"') == 2
    assert 'data-scope="lots"' in SRC and 'data-scope="tags"' in SRC
    assert "tag-set-price-btn" in SRC and SRC.count("bi-pencil-square") >= 4
    assert 'id="bulkPriceModal"' in SRC and "Current Price" in SRC and "New Price" in SRC
    i = SRC.index('id="bulkPriceModal"'); j = SRC.index("</form>", i)
    assert "spCat" not in SRC[i:j] and "Sub-lot" not in SRC[i:j]   # only Current + New Price


def test_routes_mapped_and_need_pricing_add():
    for p in ("bulk-set-price", "tag-set-price", "tags-bulk-set-price"):
        assert ("POST", f"/trade-partner/manage-lots/{p}") in ROUTE_ACTIONS
    assert PY.count('can_add_prices(current_user, "trade_partner_manage_lots")') >= 4


def test_tag_price_is_lot_split_unless_own():
    assert 'row["price"], row["price_src"] = float(own), "tag"' in PY
    assert 'round(L["price"] / L["tags"], 2), "split"' in PY


def test_search_on_lots_and_tags_and_no_move_ready_button():
    assert 'data-target="#lotsTab"' in SRC and 'id="assetSearchInput"' in SRC
    assert "moveReadyForSaleBtn" not in SRC and "bulkReadyForm" not in SRC
    up = Path("services/partner_iqc_upload.py").read_text(encoding="utf-8")
    assert "current_stage=DeviceStage.ready_to_sale" in up     # uploads start in Ready for Sale
