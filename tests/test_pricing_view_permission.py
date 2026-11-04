"""Pricing View sweep (2026-11-03): amounts hidden on screen and in JSON feeds
without the module's Pricing View tick; pages that are nothing but amounts
redirect back with a notice."""
import uuid
from types import SimpleNamespace

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


def _role():
    return "itest_pv_" + uuid.uuid4().hex[:6]


def test_blank_prices_nested():
    from models.role_permissions import set_cached_perms
    from utils.price_export import blank_prices
    role = _role()
    u = SimpleNamespace(role=role)
    try:
        set_cached_perms(role, {"lots": {"enable": True, "pricing": False}})
        data = {"items": [{"id": 1, "unit_price": 50.0, "qty": 2}], "unit_price": 9}
        assert blank_prices(u, "lots", data, ["unit_price"]) == {"items": [{"id": 1, "unit_price": None, "qty": 2}],
                                                                  "unit_price": None}
        set_cached_perms(role, {"lots": {"enable": True, "pricing": True}})
        assert blank_prices(u, "lots", {"unit_price": 9}, ["unit_price"]) == {"unit_price": 9}
    finally:
        set_cached_perms(role, {})


def test_price_only_pages_redirect_without_view(app_client, make_user):  # noqa: F811
    from models.role_permissions import set_cached_perms
    role = _role()
    u, pw = make_user(role)
    _login(app_client, u, pw)
    try:
        set_cached_perms(role, {"report_receivables": {"enable": True, "pricing": False},
                                "cost_config": {"enable": True, "pricing": False}})
        for url in ("/reports/receivables", "/admin/cost-config"):
            r = app_client.get(url, follow_redirects=False)
            if r.status_code in (302, 303) and "error=" in r.headers.get("location", ""):
                assert "Pricing" in r.headers["location"] or "pricing" in r.headers["location"].lower()
            else:
                assert r.status_code in (302, 303, 403), (url, r.status_code)   # route's own role gate
    finally:
        set_cached_perms(role, {})


def test_lot_line_items_feed_hides_unit_price(app_client, make_user):  # noqa: F811
    """GET /lots/{id}/line-items returns unit_price None without Pricing View."""
    import asyncio
    from models.role_permissions import set_cached_perms
    from database import AsyncSessionLocal
    from sqlalchemy import select
    from models.lot import Lot

    async def any_lot():
        async with AsyncSessionLocal() as s:
            return (await s.execute(select(Lot.id).limit(1))).scalar_one_or_none()
    lot_id = asyncio.run(any_lot())
    if lot_id is None:
        return
    role = _role()
    u, pw = make_user(role)
    _login(app_client, u, pw)
    try:
        set_cached_perms(role, {"lots": {"enable": True, "pricing": False}})
        r = app_client.get(f"/lots/{lot_id}/line-items")
        if r.status_code == 200:
            for it in r.json().get("items", []):
                assert it.get("unit_price") is None
    finally:
        set_cached_perms(role, {})
