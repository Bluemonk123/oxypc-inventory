"""Render + behaviour checks for the five pages in this batch."""
import pytest

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

PAGES = [
    ("/dashboard", "admin"),
    ("/devices", "inventory_manager"),
    ("/repair/l1", "l1_engineer"),
    ("/grn/post-iqc", "inventory_manager"),
    ("/workid-status", "admin"),
]


@pytest.mark.parametrize("path,role", PAGES)
def test_pages_render(app_client, make_user, path, role):  # noqa: F811
    username, password = make_user(role)
    _login(app_client, username, password)
    r = app_client.get(path, follow_redirects=True)
    assert r.status_code == 200, f"{path}: {r.status_code}\n{r.text[:2000]}"


def test_dashboard_pipeline_steps_and_entity_filter(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/dashboard", follow_redirects=True).text

    # Scope to the pipeline card — "IQC" and "Stock" legitimately appear in
    # other cards on this page, so a whole-page search would prove nothing.
    card = html.split('<span class="fw-semibold">Device Stage Pipeline</span>', 1)[1]
    card = card.split('class="d-flex pipeline-row', 1)[1].split("</div>\n  </div>\n</div>", 1)[0]
    # Steps as redesigned (2026-10-15): IQC, Stock In and Production are back;
    # GRN (not a stage a tag sits in) is gone from the pipeline row.
    for label in ("IQC", "Stock In", "Production", "L1/L2", "L3/L4", "Stress/QC",
                  "Cosmetic", "Final QC", "Ready to Sale", "Sold"):
        assert ">" + label + "<" in card, f"{label} missing from the pipeline"
    assert ">GRN<" not in card
    assert 'id="ms_val_entity"' in html


def test_dashboard_entity_filter_applies(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    r = app_client.get("/dashboard?entity=Deshwal", follow_redirects=True)
    assert r.status_code == 200, r.text[:1500]


def test_l1_has_date_assigned_and_sorts_by_it(app_client, make_user):  # noqa: F811
    username, password = make_user("l1_engineer")
    _login(app_client, username, password)
    html = app_client.get("/repair/l1", follow_redirects=True).text

    assert "<th>Date Assigned</th>" in html
    head = html.split('id="l1Table"', 1)[1].split("</thead>", 1)[0]
    # Third column, i.e. index 2 — which is what the sort targets.
    assert head.index("Date Assigned") > head.index("Tag Number")
    assert "order: [[2, 'desc']]" in html


def test_grn_mapping_stays_on_the_page(app_client, make_user):  # noqa: F811
    username, password = make_user("inventory_manager")
    _login(app_client, username, password)
    html = app_client.get("/grn/post-iqc", follow_redirects=True).text

    assert "mapForm" in html
    assert "preventDefault" in html
    assert "'X-Requested-With': 'fetch'" in html
    assert 'id="mapNotice"' in html


def test_workid_status_filters_and_export(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    html = app_client.get("/workid-status", follow_redirects=True).text

    assert "/workid-status/export" in html
    # Buttons share the filter row rather than sitting in their own block.
    assert 'class="d-flex gap-2 mt-2"' not in html

    r = app_client.get("/workid-status/export", follow_redirects=True)
    assert r.status_code == 200, r.text[:800]
    # .xlsx workbook since 2026-10-27 (Tag Summary / Stage / Staff Summary).
    assert "spreadsheetml" in r.headers.get("content-type", "")
    from tests.workid_helpers import export_sheet
    # Export columns narrowed 2026-09-02 — WorkID is no longer one of them.
    assert "Tag Number" in export_sheet(r.content)[0]


def test_workid_export_honours_a_filter(app_client, make_user):  # noqa: F811
    username, password = make_user("admin")
    _login(app_client, username, password)
    r = app_client.get("/workid-status/export?workid=ZZZNOSUCH", follow_redirects=True)
    assert r.status_code == 200
    from tests.workid_helpers import export_sheet
    body = export_sheet(r.content)
    assert len(body) == 1, "a filter matching nothing must export only the header"
