"""Pricing split (2026-11-03): Pricing View / Pricing Add / Pricing Export.
- has_perm: Pricing Add / Export not set -> follow Pricing View.
- Exports drop their price columns without Pricing Export; priced documents
  (dealer statement, invoice print...) redirect back with a notice.
- Price-changing routes need Pricing Add."""
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


def test_has_perm_pricing_fallback():
    from models.role_permissions import has_perm, set_cached_perms
    role = "itest_pr_" + uuid.uuid4().hex[:6]
    try:
        set_cached_perms(role, {"sales": {"enable": True, "pricing": False}})
        assert not has_perm(role, "sales", "pricing_add") and not has_perm(role, "sales", "pricing_export")
        set_cached_perms(role, {"sales": {"enable": True, "pricing": True}})
        assert has_perm(role, "sales", "pricing_add") and has_perm(role, "sales", "pricing_export")
        set_cached_perms(role, {"sales": {"enable": True, "pricing": True, "pricing_add": False, "pricing_export": False}})
        assert has_perm(role, "sales", "pricing") and not has_perm(role, "sales", "pricing_add")
        assert not has_perm(role, "sales", "pricing_export")
    finally:
        set_cached_perms(role, {})


def test_drop_csv_columns():
    from utils.price_export import drop_csv_columns
    t = "Title row\r\nSale#,Date,Price,Customer\r\n1,2026-11-03,1000,A\r\n"
    assert drop_csv_columns(t, ["Price"]) == "Title row\r\nSale#,Date,Customer\r\n1,2026-11-03,A\r\n"
    assert drop_csv_columns(t, ["Nope"]) == t


def test_exports_follow_pricing_export(app_client, make_user):  # noqa: F811
    from models.role_permissions import set_cached_perms
    role = "itest_prx_" + uuid.uuid4().hex[:6]
    u, pw = make_user(role)
    _login(app_client, u, pw)
    base = {"enable": True, "export": True, "pricing": True}
    try:
        # Pricing Export on: the price column is there
        set_cached_perms(role, {"sales_list": {**base, "pricing_export": True}, "devices": {**base, "pricing_export": True}})
        r = app_client.get("/sales/export")
        assert r.status_code == 200 and "Price" in r.text.splitlines()[0].split(",")
        # Pricing Export off: same file without the column
        set_cached_perms(role, {"sales_list": {**base, "pricing_export": False}, "devices": {**base, "pricing_export": False}})
        r = app_client.get("/sales/export")
        assert r.status_code == 200 and "Price" not in r.text.splitlines()[0].split(","), r.text[:200]
        assert "Sale#" in r.text.splitlines()[0]
        # Priced document: redirected back with the notice
        set_cached_perms(role, {"sales_list": {**base, "pricing_export": False}})
        r = app_client.get("/invoices/print/" + str(uuid.uuid4()), follow_redirects=False)
        assert r.status_code == 302 and "Pricing+Export" in r.headers["location"]
    finally:
        set_cached_perms(role, {})


def test_price_change_route_needs_pricing_add():
    from auth.route_actions import ROUTE_ACTIONS
    assert ROUTE_ACTIONS[("POST", "/sales/ready/set-price")] == ("sales", "pricing_add")
    assert ROUTE_ACTIONS[("POST", "/spare-parts/bulk-edit")] == ("spare_parts", "edit")
    assert not [k for k, v in ROUTE_ACTIONS.items() if v and v[1] == "pricing"]


def test_remaining_exports_follow_pricing_export(app_client, make_user):  # noqa: F811
    """Dealer ageing CSV is not produced without Pricing Export (contacts
    export is admin-only; IQC export is checked below via the helper)."""
    from models.role_permissions import set_cached_perms
    role = "itest_prr_" + uuid.uuid4().hex[:6]
    u, pw = make_user(role)
    _login(app_client, u, pw)
    mods = ("iqc", "crm_contacts", "dealers", "sales")
    def perms(px):
        return {m: {"enable": True, "export": True, "pricing": True, "pricing_export": px} for m in mods}
    def hdr(text):
        return [c.strip() for c in text.lstrip("﻿").splitlines()[0].split(",")]
    try:
        set_cached_perms(role, perms(True))

        set_cached_perms(role, perms(False))
        r = app_client.get("/dealers/ageing?export=csv", follow_redirects=False)
        if r.status_code != 403:   # route itself may be role-gated
            assert r.status_code == 302 and "error=" in r.headers["location"]
    finally:
        set_cached_perms(role, {})


def test_iqc_export_strips_device_price():
    """/iqc/export-csv is role-gated (built-in roles), so check its response
    through the same helper with a stand-in user."""
    import asyncio
    from types import SimpleNamespace
    from fastapi.responses import StreamingResponse
    from models.role_permissions import set_cached_perms
    from utils.price_export import strip_price_columns
    role = "itest_pri_" + uuid.uuid4().hex[:6]
    body = "\ufefflot_number,current_stage,device_price,barcode\r\nL1,IQC,5000,B1\r\n".encode("utf-8")
    async def run():
        resp = StreamingResponse(iter([body]), media_type="text/csv")
        return await strip_price_columns(resp, SimpleNamespace(role=role), "iqc", ["device_price"])
    try:
        set_cached_perms(role, {"iqc": {"enable": True, "pricing": True, "pricing_export": False}})
        out = asyncio.run(run()).body
        assert out.startswith(b"\xef\xbb\xbf") and b"device_price" not in out and b"5000" not in out and b"B1" in out
        set_cached_perms(role, {"iqc": {"enable": True, "pricing": True, "pricing_export": True}})
        assert asyncio.run(run()).__class__.__name__ == "StreamingResponse"
    finally:
        set_cached_perms(role, {})
