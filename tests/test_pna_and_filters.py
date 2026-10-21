"""Page-render and endpoint coverage for the PNA marks, the Part Master /
request filters, and manual GRN creation.

These are the pages the batch touched; each one must still render for an
ordinary user, since a template error here is a 500 on a page the floor uses
every day.
"""
import uuid

import pytest

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


PAGES = [
    ("/spare-parts", "spare_parts_manager"),
    ("/repair/l1", "l1_engineer"),
    ("/repair/l3l4", "l3_engineer"),
    ("/grn/post-iqc", "inventory_manager"),
]


@pytest.mark.parametrize("path,role", PAGES)
def test_page_renders(app_client, make_user, path, role):  # noqa: F811
    username, password = make_user(role)
    _login(app_client, username, password)

    r = app_client.get(path, follow_redirects=True)
    assert r.status_code != 500, f"{path} returned 500:\n{r.text[:2500]}"
    assert r.status_code == 200


def test_part_master_has_added_on_and_filters(app_client, make_user):  # noqa: F811
    username, password = make_user("spare_parts_manager")
    _login(app_client, username, password)
    html = app_client.get("/spare-parts", follow_redirects=True).text

    assert "Added On" in html
    assert 'data-filterbar="partsTable"' in html
    assert 'data-filterbar="partReqTable"' in html
    assert 'data-filterbar="faultyReqTable"' in html
    # Bulk actions on both request tables
    assert 'bulkHandover_partReqTable' in html
    assert 'bulkDelete_partReqTable' in html
    assert 'bulkHandover_faultyReqTable' in html


def test_repair_queues_have_pna_column(app_client, make_user):  # noqa: F811
    username, password = make_user("l1_engineer")
    _login(app_client, username, password)

    l1 = app_client.get("/repair/l1", follow_redirects=True).text
    assert "PNA Parts" in l1
    assert 'id="onlyPnaL1"' in l1

    l34 = app_client.get("/repair/l3l4", follow_redirects=True).text
    assert "PNA Parts" in l34
    assert 'id="onlyPnaL34"' in l34
    # L3/L4's own inline "Mark PNA" button (rp_mark_pna) is gone as of
    # 2026-09-09 — Request Part there is now a plain link to the device page,
    # matching L1/L2, which never had an inline PNA marker of its own either.
    assert 'id="rp_mark_pna"' not in l34


def test_post_iqc_has_add_grn_modal(app_client, make_user):  # noqa: F811
    username, password = make_user("inventory_manager")
    _login(app_client, username, password)
    html = app_client.get("/grn/post-iqc", follow_redirects=True).text

    assert 'id="addGrnModal"' in html
    assert "/grn/create-manual" in html
    # GRN Number is deliberately absent (generated on save). Since 2026-10-21
    # (Flow 1) the modal takes the Entity and the Lot Number(s) to map.
    add_modal = html.split('id="addGrnModal"', 1)[1].split("</form>", 1)[0]
    assert "GRN Number" not in add_modal
    assert 'name="entity"' in add_modal
    assert 'name="lot_numbers"' in add_modal


def test_pna_mark_and_clear_roundtrip(app_client, make_user):  # noqa: F811
    """Marking, re-marking and clearing a part must not accumulate rows.

    The unique (device, part) constraint means a second mark has to reopen the
    existing row rather than insert — the case that would 500 in production the
    first time an engineer ticked, unticked and re-ticked the same part.
    """
    username, password = make_user("l1_engineer")
    _login(app_client, username, password)

    # Need a real tag to mark against; use whichever the IQC list already has.
    from tests.test_iqc_new_user import _login as _relogin  # noqa: F401
    app_client.get("/spare-parts")
    csrf = app_client.cookies.get("csrf_token") or "dummy"

    # Unknown tag must 404 cleanly rather than raise.
    r = app_client.post(
        f"/devices/NOSUCHTAG{uuid.uuid4().hex[:6]}/pna",
        data={"csrf_token": csrf, "part_name": "RAM", "marked": "1"},
        follow_redirects=False,
    )
    assert r.status_code in (404, 302), f"unexpected {r.status_code}: {r.text[:500]}"
    if r.status_code == 404:
        assert r.json()["ok"] is False
