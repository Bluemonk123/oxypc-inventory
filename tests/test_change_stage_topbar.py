"""Change Stage (2026-11-22, Pankaj): Production Manager's "Move to Stage" is
renamed "Change Stage", and the same modal opens from a global top-bar
button — order: bell, Raise Ticket, "+", Change Stage (no icon) — shown only
to Admin, Inventory Manager and Production Manager."""
from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


def _bar(html):
    return html.split('id="tbSearchWrap"', 1)[1].split('id="userMenuToggle"', 1)[0] if 'id="userMenuToggle"' in html \
        else html.split('id="tbSearchWrap"', 1)[1][:6000]


def test_topbar_change_stage_admin(app_client, make_user):  # noqa: F811
    u, p = make_user("admin")
    _login(app_client, u, p)
    html = app_client.get("/devices").text
    bar = _bar(html)
    assert bar.index("bi-bell") < bar.index("Raise Ticket") < bar.index('id="tbChangeStageBtn"')
    if 'id="quickAddBtn"' in bar:
        assert bar.index("Raise Ticket") < bar.index('id="quickAddBtn"') < bar.index('id="tbChangeStageBtn"')
    btn = bar.split('id="tbChangeStageBtn"', 1)[1].split("</button>", 1)[0]
    assert "Change Stage" in btn and "bi-" not in btn            # no icon
    assert html.count('id="changeEngineerModal"') == 1 and "Pre-Sale QC" in html
    pm = app_client.get("/trc-production").text
    assert pm.count('id="changeEngineerModal"') == 1               # one shared modal, not two
    assert ">Change Stage</button>" in pm and "Move to Stage</button>" not in pm


def test_topbar_change_stage_hidden_for_other_roles(app_client, make_user):  # noqa: F811
    for role, page in (("sales", "/sales/ready"), ("l1_engineer", "/repair/l1")):
        u, p = make_user(role)
        _login(app_client, u, p)
        html = app_client.get(page, follow_redirects=True).text
        assert 'id="tbSearchWrap"' in html, role                  # a real page with the top bar
        assert 'id="tbChangeStageBtn"' not in html and 'id="changeEngineerModal"' not in html, role


def test_topbar_change_stage_inventory_manager(app_client, make_user):  # noqa: F811
    u, p = make_user("inventory_manager")
    _login(app_client, u, p)
    html = app_client.get("/devices", follow_redirects=True).text
    assert 'id="tbChangeStageBtn"' in html and 'id="changeEngineerModal"' in html
