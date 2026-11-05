"""Product Management (2026-11-05, Pankaj): sidebar group with QA Dashboard /
UAT Analysis / UX Journeys, one Permission Matrix row each ("qa", "qa_uat",
"qa_ux"). Admin always; any other role only with Enable ticked on that row
(a role with no row gets nothing). The group is hidden when none is allowed;
each page shows only its own tabs."""
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


def test_pages_follow_their_own_matrix_row(app_client, make_user):  # noqa: F811
    from models.role_permissions import set_cached_perms
    role = "itest_pm_" + uuid.uuid4().hex[:6]
    u, pw = make_user(role)
    _login(app_client, u, pw)
    try:
        # nothing ticked: no group, every page refused
        set_cached_perms(role, {"devices": {"enable": True}})
        html = app_client.get("/settings", follow_redirects=True).text
        assert "productMgmtCollapse" not in html
        for url in ("/qa/", "/qa/uat", "/qa/workflow", "/qa/journeys", "/qa/requirements"):
            assert app_client.get(url, follow_redirects=False).status_code == 403, url

        # only UAT Analysis ticked
        set_cached_perms(role, {"devices": {"enable": True}, "qa_uat": {"enable": True}})
        for url in ("/qa/uat", "/qa/requirements", "/qa/test-cases", "/qa/defects"):
            assert app_client.get(url).status_code == 200, url
        for url in ("/qa/", "/qa/releases", "/qa/workflow", "/qa/database-diagram"):
            assert app_client.get(url, follow_redirects=False).status_code == 403, url
        page = app_client.get("/qa/uat").text
        side = page.split('id="productMgmtCollapse"', 1)[1].split("</ul>", 1)[0]
        assert 'href="/qa/uat"' in side and 'href="/qa/"' not in side and 'href="/qa/workflow"' not in side
        tabs = page.split('qa-tabs">', 1)[1].split("</ul>", 1)[0]
        assert "UAT Board" in tabs and "Requirements" in tabs and "Releases" not in tabs and "Workflows" not in tabs
    finally:
        set_cached_perms(role, {})


def test_admin_sees_all_three(app_client, make_user):  # noqa: F811
    u, pw = make_user("admin")
    _login(app_client, u, pw)
    page = app_client.get("/qa/").text
    side = page.split('id="productMgmtCollapse"', 1)[1].split("</ul>", 1)[0]
    assert all(h in side for h in ('href="/qa/"', 'href="/qa/uat"', 'href="/qa/workflow"'))
    for url in ("/qa/", "/qa/uat", "/qa/workflow", "/qa/journeys", "/qa/database-diagram"):
        assert app_client.get(url).status_code == 200, url
