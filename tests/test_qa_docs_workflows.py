"""QA page refresh (2026-11-04): Product & Architecture Handbook downloads on
the SRS card, change history read from qa_docs/changelog.txt when git is not
available (production Docker image), current New GRN / Re-GRN workflow and
Stage Workflows."""
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


def test_changelog_without_git(monkeypatch):
    import routers.qa_uat as q
    with_git = q._get_recent_commits()

    def no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(q.subprocess, "run", no_git)
    without = q._get_recent_commits()
    assert len(without) > len(q._HARDCODED_COMMITS)
    assert without[0]["date"] >= "2026-11-04"
    assert abs(len(without) - len(with_git)) <= 5


def test_handbook_card_and_downloads(app_client, make_user):  # noqa: F811
    u, pw = make_user("admin")
    _login(app_client, u, pw)
    html = app_client.get("/qa/").text
    from routers.qa_uat import SRS_HANDBOOK
    for key, _f, title, *_ in SRS_HANDBOOK:
        assert f"/qa/srs/handbook/{key}/download" in html
        r = app_client.get(f"/qa/srs/handbook/{key}/download")
        assert r.status_code == 200 and r.content[:4] == b"%PDF", key
    assert app_client.get("/qa/srs/handbook/nope/download").status_code == 404
    # 2026-11-06 set: 7 documents + handbook; Module Map and the separate
    # Design System row are gone (Design System is document 07)
    assert len(SRS_HANDBOOK) == 8 and SRS_HANDBOOK[-1][0] == "design-system"
    assert "/qa/srs/module-map/" not in html and "/qa/srs/design-system/" not in html
    assert app_client.get("/qa/srs/handbook/key-findings/download").status_code == 404


def test_handbook_download_needs_export(app_client, make_user):  # noqa: F811
    from models.role_permissions import set_cached_perms
    role = "itest_qadoc_" + uuid.uuid4().hex[:6]
    u, pw = make_user(role)
    _login(app_client, u, pw)
    try:
        set_cached_perms(role, {"qa": {"enable": True, "export": False}})
        assert "/qa/srs/handbook/" not in app_client.get("/qa/").text
        r = app_client.get("/qa/srs/handbook/product-overview/download", follow_redirects=False)
        assert r.status_code != 200 or r.content[:4] != b"%PDF"
    finally:
        set_cached_perms(role, {})


def test_workflow_and_stage_workflows(app_client, make_user):  # noqa: F811
    u, pw = make_user("admin")
    _login(app_client, u, pw)
    html = app_client.get("/qa/workflow").text
    assert "Flow 1 — New GRN" in html and "Flow 2 — Re-GRN" in html and "₹50,000" in html
    # Flow diagrams (2026-11-04): one swimlane SVG per flow, above the step journey
    # Built-in diagrams: New GRN, Re-GRN, Spare Parts (2026-11-09) + Supplier / Customer Deal (2026-11-11).
    assert html.count('class="wf-dg"') == 5 and "Flow 4 — Supplier Deal" in html and "Flow 5 — Customer Deal" in html and "Product IQC</text>" in html and "Move-to-GRN modal</text>" in html
    assert "Faulty &amp; Sourcing</text>" in html or "Faulty & Sourcing</text>" in html
    assert html.index('class="wf-dg"') < html.index('class="wf-steps"')
    # Stage Flows now a tab of Workflow (2026-11-04); the old page redirects there
    r = app_client.get("/qa/stage-workflows", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/qa/workflow?tab=stage"
    sw = app_client.get("/qa/workflow?tab=stage").text
    for t in ("Re-GRN (Item ReGRN)", "Final QC → Ready to Sale", "Customer Return → Return Stock"):
        assert t in sw
    assert 'href="/qa/stage-workflows"' not in sw            # tab removed from the QA menu
    assert app_client.get("/qa/stage-workflows/regrn/download").status_code == 200


def test_database_diagram_svgs_are_valid_xml():
    """2026-11-06: 7 module diagrams had a bare "&" in the title ("Sales &
    Dealers"), so the browser refused to draw them (broken image on
    /qa/database-diagram). Every diagram must parse as XML."""
    import glob
    import xml.etree.ElementTree as ET
    files = glob.glob("static/qa/journeys/*.svg")
    assert len(files) == 11
    for f in files:
        ET.parse(f)
