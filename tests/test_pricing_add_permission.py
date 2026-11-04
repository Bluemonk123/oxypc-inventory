"""Pricing Add sweep (2026-11-03).
- Transactions whose amount is the point (sale, parts sale, rental, payment,
  quote, PO...) need Add AND Pricing Add: route_actions holds both and the
  action guard checks each; their buttons follow automatically via can().
- Records where a price is one field among many (device, lot, GRN, part,
  contact credit limit...) still save without Pricing Add; the price field is
  ignored (stored value kept) — price_unchanged()."""
import uuid
from types import SimpleNamespace

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


def _role():
    return "itest_pa_" + uuid.uuid4().hex[:6]


def test_route_actions_need_pricing_add_for_money_transactions():
    from auth.route_actions import ROUTE_ACTIONS
    for key in [("POST", "/sales/new"), ("POST", "/part-sales/new"), ("POST", "/rentals/new"),
                ("POST", "/accounts/supplier-payments/new"), ("POST", "/accounts/customer-receipts/new"),
                ("POST", "/dealers/{dealer_id}/orders/{order_id}/pay"), ("POST", "/crm/quotes/new"),
                ("POST", "/crm/purchase-orders/new"), ("POST", "/scrap-for-sale/custom-sale"),
                ("POST", "/spare-parts/purchase"), ("POST", "/deals/new")]:
        mod, acts = ROUTE_ACTIONS[key]
        assert isinstance(acts, tuple) and "pricing_add" in acts, key


def test_can_follows_price_bound_actions():
    from models.role_permissions import set_cached_perms
    from templates_config import _can, _price_bound_actions
    assert "add" in _price_bound_actions("part_sales")
    assert "add" not in _price_bound_actions("sales")      # Dispatch Request is also sales/add
    role = _role()
    u = SimpleNamespace(role=role)
    try:
        set_cached_perms(role, {"part_sales": {"enable": True, "add": True, "pricing": True, "pricing_add": False}})
        assert not _can(u, "part_sales", "add")
        set_cached_perms(role, {"part_sales": {"enable": True, "add": True, "pricing": True, "pricing_add": True}})
        assert _can(u, "part_sales", "add")
    finally:
        set_cached_perms(role, {})


def test_price_unchanged_rules():
    from models.role_permissions import set_cached_perms
    from utils.price_export import price_unchanged, credit_edit, strip_price_keys
    role = _role()
    u = SimpleNamespace(role=role)
    try:
        set_cached_perms(role, {"lots": {"enable": True, "pricing": True, "pricing_add": True}})
        assert not price_unchanged(u, "lots", "100") and not price_unchanged(u, "lots", "")   # may clear
        set_cached_perms(role, {"lots": {"enable": True, "pricing": False, "pricing_add": True}})
        assert not price_unchanged(u, "lots", "100") and price_unchanged(u, "lots", "")       # blank = keep
        set_cached_perms(role, {"lots": {"enable": True, "pricing": True, "pricing_add": False}})
        assert price_unchanged(u, "lots", "100")
        set_cached_perms(role, {"dealers": {"enable": True, "pricing": True, "pricing_add": False}})
        assert credit_edit(u, "dealers", "99999", 5000) == 5000
        row = {"phone": "1", "gst_number": "X", "asking_price": "9", "quote_given": "8", "sgst": "1"}
        assert strip_price_keys(row, u, "dealers") == {"phone": "1", "gst_number": "X"}
    finally:
        set_cached_perms(role, {})


def test_guard_refuses_sale_without_pricing_add(app_client, make_user):  # noqa: F811
    from models.role_permissions import set_cached_perms
    role = _role()
    u, pw = make_user(role)
    _login(app_client, u, pw)
    try:
        set_cached_perms(role, {"sales": {"enable": True, "add": True, "pricing": True, "pricing_add": False}})
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post("/sales/new", data={"csrf_token": csrf}, headers={"accept": "application/json"},
                            follow_redirects=False)
        assert r.status_code == 403 and "Pricing Add" in r.text, r.text[:200]
    finally:
        set_cached_perms(role, {})


def test_price_input_attrs():
    from models.role_permissions import set_cached_perms
    from templates_config import _price_input_attrs, _price_field_shown
    role = _role()
    u = SimpleNamespace(role=role)
    try:
        set_cached_perms(role, {"devices": {"enable": True, "pricing": False, "pricing_add": True}})
        a = str(_price_input_attrs(u, "devices", 4500))
        assert 'value=""' in a and "4500" not in a and _price_field_shown(u, "devices")
        set_cached_perms(role, {"devices": {"enable": True, "pricing": True, "pricing_add": False}})
        a = str(_price_input_attrs(u, "devices", 4500))
        assert "4500" in a and "readonly" in a
        set_cached_perms(role, {"devices": {"enable": True, "pricing": False, "pricing_add": False}})
        assert not _price_field_shown(u, "devices")
    finally:
        set_cached_perms(role, {})
