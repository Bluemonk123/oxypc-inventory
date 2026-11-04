"""/spare-parts tiles (2026-11-04): each tile split New / Harvest, worked out
from the same rows and numbers as the Part Master table; the value tile and
the per-row value the browser recounts from need Pricing View."""
import re
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


def _tiles(html):
    return dict(re.findall(r'data-tile="([a-z]+-(?:new|harvest))">\s*([^<]*?)\s*<', html))


def test_tiles_split_and_match_rows(app_client, make_user):  # noqa: F811
    u, pw = make_user("admin")
    _login(app_client, u, pw)
    html = app_client.get("/spare-parts").text
    t = _tiles(html)
    for k in ("qty", "types", "low", "out", "consumed", "sold", "value"):
        assert f"{k}-new" in t and f"{k}-harvest" in t, k
    rows = re.findall(r'data-addedas="(New|Harvest)"[^>]*?data-live="(-?\d+)" data-min="(-?\d+)" '
                      r'data-cons="(\d+)" data-sold="(\d+)"', html, re.S)
    for src, key in (("New", "new"), ("Harvest", "harvest")):
        mine = [r for r in rows if r[0] == src]
        assert int(t[f"types-{key}"]) == len(mine)
        assert int(t[f"qty-{key}"]) == sum(int(r[1]) for r in mine)
        assert int(t[f"low-{key}"]) == sum(1 for r in mine if int(r[1]) <= int(r[2]))
        assert int(t[f"out-{key}"]) == sum(1 for r in mine if int(r[1]) <= 0)
        assert int(t[f"consumed-{key}"]) == sum(int(r[3]) for r in mine)
        assert int(t[f"sold-{key}"]) == sum(int(r[4]) for r in mine)


def test_value_tile_needs_pricing_view(app_client, make_user):  # noqa: F811
    from models.role_permissions import set_cached_perms
    role = "itest_spt_" + uuid.uuid4().hex[:6]
    u, pw = make_user(role)
    _login(app_client, u, pw)
    try:
        set_cached_perms(role, {"spare_parts": {"enable": True, "pricing": False}})
        html = app_client.get("/spare-parts").text
        assert 'data-tile="qty-new"' in html
        assert 'data-tile="value-new"' not in html and "data-val=" not in html
        set_cached_perms(role, {"spare_parts": {"enable": True, "pricing": True}})
        assert 'data-tile="value-new"' in app_client.get("/spare-parts").text
    finally:
        set_cached_perms(role, {})
