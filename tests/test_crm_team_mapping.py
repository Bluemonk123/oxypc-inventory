from types import SimpleNamespace

import pytest
from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.crm_team import CRMContactTeamMember
from services.crm_team import ROLE_BUCKETS, BUCKET_LABELS, eligible_users_for_bucket, replace_team_buckets


@pytest.mark.asyncio
async def test_crm_contact_team_member_table_exists(db: AsyncSession):
    result = await db.execute(select(CRMContactTeamMember).limit(1))
    assert result.scalars().all() == []


def _u(designation=None, role="sales"):
    return SimpleNamespace(designation=designation, role=role)


def test_role_buckets_and_labels_cover_the_same_four_keys():
    # Five buckets since 2026-11-09 (Finance Manager added); name kept for history.
    assert set(ROLE_BUCKETS) == set(BUCKET_LABELS.keys())
    # 2026-11-11: Manager, Executive/Sr Executive, VP/AVP, DM/GM/CTO/CEO, Finance Manager
    assert ROLE_BUCKETS == ["manager", "executive", "vp_avp", "am_dm_gm", "finance"]
    assert BUCKET_LABELS["am_dm_gm"] == "DM/GM/CTO/CEO"
    assert BUCKET_LABELS["finance"] == "Finance Manager"


def test_finance_bucket_matches_finance_or_accounts_designation():
    users = [_u(designation="Finance Executive"), _u(designation="Accounts Head"),
             _u(designation="Account Manager"), _u(designation=None)]
    # Whole word "Finance" / "Accounts" only — "Account Manager" (sales) is not Finance.
    assert eligible_users_for_bucket(users, "finance") == [users[0], users[1]]


def test_eligible_users_matches_whole_word_designation():
    users = [_u(designation="Regional AVP"), _u(designation="Sales Executive"), _u(designation=None)]
    assert eligible_users_for_bucket(users, "vp_avp") == [users[0]]


def test_eligible_users_does_not_false_positive_on_substring():
    # 2026-11-11: the bucket is DM/GM/CTO/CEO (AM no longer matches)
    users = [_u(designation="Team Lead"), _u(designation="Samir's Assistant"), _u(designation="DM - North"),
             _u(designation="AM - North"), _u(designation="CTO"), _u(designation="Director")]
    assert eligible_users_for_bucket(users, "am_dm_gm") == [users[2], users[4]]


def test_eligible_users_can_match_multiple_buckets():
    u = _u(designation="Sr Executive / GM")
    assert eligible_users_for_bucket([u], "executive") == [u]
    assert eligible_users_for_bucket([u], "am_dm_gm") == [u]


def test_eligible_users_also_checks_role():
    u = _u(designation=None, role="manager")
    assert eligible_users_for_bucket([u], "manager") == [u]


def test_eligible_users_unknown_bucket_raises():
    with pytest.raises(ValueError):
        eligible_users_for_bucket([], "not_a_bucket")


@pytest.mark.asyncio
async def test_replace_team_buckets_round_trip(db: AsyncSession):
    from models.crm import CRMContact
    from models.user import User

    contact = (await db.execute(select(CRMContact).limit(1))).scalars().first()
    users = (await db.execute(select(User).limit(2))).scalars().all()
    assert contact and len(users) >= 2, "Need at least 1 CRMContact and 2 Users seeded in the dev DB"

    try:
        await replace_team_buckets(
            db, contact_id=contact.id,
            bucket_user_ids={"vp_avp": [users[0].id]},
            created_by=users[0].username,
        )
        await db.commit()
        rows = (await db.execute(
            select(CRMContactTeamMember).where(CRMContactTeamMember.contact_id == contact.id)
        )).scalars().all()
        assert {r.user_id for r in rows} == {users[0].id}
        assert rows[0].role_bucket == "vp_avp"

        await replace_team_buckets(
            db, contact_id=contact.id,
            bucket_user_ids={"vp_avp": [users[1].id]},
            created_by=users[0].username,
        )
        await db.commit()
        rows = (await db.execute(
            select(CRMContactTeamMember).where(CRMContactTeamMember.contact_id == contact.id)
        )).scalars().all()
        assert {r.user_id for r in rows} == {users[1].id}

        await replace_team_buckets(
            db, contact_id=contact.id,
            bucket_user_ids={"vp_avp": []},
            created_by=users[0].username,
        )
        await db.commit()
        rows = (await db.execute(
            select(CRMContactTeamMember).where(CRMContactTeamMember.contact_id == contact.id)
        )).scalars().all()
        assert rows == []
    finally:
        await db.execute(sa_delete(CRMContactTeamMember).where(CRMContactTeamMember.contact_id == contact.id))
        await db.commit()


from httpx import AsyncClient, ASGITransport

from main import app
from database import get_db, AsyncSessionLocal, engine
from auth.dependencies import get_current_user
from models.user import UserRole


class _FakeAdmin:
    id = None
    username = "verify_admin"
    role = UserRole.admin
    status = True
    full_name = "Verify Admin"


_CURRENT_USER = {"user": None}


async def _override_user():
    return _CURRENT_USER["user"]


async def _override_db():
    # Each async test in this module runs in its own event loop
    # (pytest.ini's asyncio_mode = auto), but AsyncSessionLocal's pooled
    # asyncpg connections are bound to whichever loop opened them — reusing
    # one across a loop boundary fails with a bare "'NoneType' object has no
    # attribute 'send'" that looks like a real fault and isn't one (same
    # issue conftest.py's own `db` fixture documents and works around).
    # Disposing before each request-scoped session keeps every test's
    # connections native to its own loop.
    await engine.dispose()
    async with AsyncSessionLocal() as db:
        yield db


@pytest.mark.asyncio
async def test_list_contacts_renders_team_column_and_filters():
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    _CURRENT_USER["user"] = _FakeAdmin()

    transport = ASGITransport(app=app, raise_app_exceptions=True)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/crm/contacts")
            assert r.status_code == 200, r.text[:500]
            assert "Team</th>" in r.text or ">Team<" in r.text
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_save_team_mapping_single_and_bulk_modes():
    from models.crm import CRMContact
    from models.user import User

    # See _override_db's own comment — dispose before this test's first
    # direct AsyncSessionLocal() use too, not just inside the override,
    # since this loop-bound-connection issue bites there just as easily.
    await engine.dispose()
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db

    async with AsyncSessionLocal() as db:
        contacts = (await db.execute(select(CRMContact).limit(2))).scalars().all()
        users = (await db.execute(select(User).limit(2))).scalars().all()
        assert len(contacts) >= 2 and len(users) >= 2, "Need 2 CRMContacts and 2 Users seeded"
        c1, c2 = contacts[0], contacts[1]
        u1, u2 = users[0], users[1]

    # created_by has a real FK to users.username, so the fake current_user
    # must carry an actual seeded username, not a literal placeholder.
    fake_admin = _FakeAdmin()
    fake_admin.username = u1.username
    _CURRENT_USER["user"] = fake_admin

    transport = ASGITransport(app=app, raise_app_exceptions=True)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            c.cookies.set("csrf_token", "smoketest-csrf")

            r = await c.post(
                "/crm/contacts/team-mapping",
                data={
                    "contact_ids": [str(c1.id)],
                    "mode": "single",
                    "bucket_vp_avp": str(u1.id),
                    "bucket_manager": "", "bucket_am_dm_gm": "", "bucket_executive": "",
                    "csrf_token": "smoketest-csrf",
                },
                follow_redirects=False,
            )
            assert r.status_code in (302, 303, 307), f"single save returned {r.status_code}: {r.text[:300]}"

            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(CRMContactTeamMember).where(CRMContactTeamMember.contact_id == c1.id)
                )).scalars().all()
                assert {(row.user_id, row.role_bucket) for row in rows} == {(u1.id, "vp_avp")}

            r = await c.post(
                "/crm/contacts/team-mapping",
                data={
                    "contact_ids": [str(c1.id), str(c2.id)],
                    "mode": "bulk",
                    "bucket_vp_avp": "",
                    "bucket_manager": str(u2.id),
                    "bucket_am_dm_gm": "", "bucket_executive": "",
                    "csrf_token": "smoketest-csrf",
                },
                follow_redirects=False,
            )
            assert r.status_code in (302, 303, 307), f"bulk save returned {r.status_code}: {r.text[:300]}"

            async with AsyncSessionLocal() as db:
                rows1 = (await db.execute(
                    select(CRMContactTeamMember).where(CRMContactTeamMember.contact_id == c1.id)
                )).scalars().all()
                rows2 = (await db.execute(
                    select(CRMContactTeamMember).where(CRMContactTeamMember.contact_id == c2.id)
                )).scalars().all()
                assert {(row.user_id, row.role_bucket) for row in rows1} == {(u1.id, "vp_avp"), (u2.id, "manager")}
                assert {(row.user_id, row.role_bucket) for row in rows2} == {(u2.id, "manager")}
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(sa_delete(CRMContactTeamMember).where(
                CRMContactTeamMember.contact_id.in_([c1.id, c2.id])
            ))
            await db.commit()
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_team_mapping_sample_csv_lists_accounts():
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    _CURRENT_USER["user"] = _FakeAdmin()

    transport = ASGITransport(app=app, raise_app_exceptions=True)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/crm/contacts/team-mapping/sample-csv")
            assert r.status_code == 200, r.text[:300]
            body = r.content.decode("utf-8-sig")
            # Row count depends on how many non-trashed accounts exist in
            # whatever DB this runs against (the local dev DB currently has
            # none active) — the header contract is what this test locks in.
            assert body.splitlines()[0] == "gstin,company_name,manager,executive,vp_avp,am_dm_gm,finance"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_team_mapping_upload_page_loads():
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    _CURRENT_USER["user"] = _FakeAdmin()

    transport = ASGITransport(app=app, raise_app_exceptions=True)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/crm/contacts/team-mapping/upload")
            assert r.status_code == 200, r.text[:300]
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_team_mapping_upload_apply_matches_by_gstin_and_reports_errors():
    from models.crm import CRMContact
    from models.user import User

    await engine.dispose()
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db

    async with AsyncSessionLocal() as db:
        contact = (await db.execute(
            select(CRMContact).limit(1)
        )).scalars().first()
        assert contact, "Need at least one CRMContact seeded"
        gstin_was_blank = not contact.gstin
        if gstin_was_blank:
            contact.gstin = "TESTGSTIN0001"
            await db.commit()
        user = (await db.execute(select(User).limit(1))).scalars().first()
        assert user

    fake_admin = _FakeAdmin()
    fake_admin.username = user.username
    _CURRENT_USER["user"] = fake_admin

    csv_body = (
        "gstin,company_name,vp_avp,manager,am_dm_gm,executive\n"
        f"{contact.gstin},{contact.company_name},{user.full_name},,,\n"
        ",Missing GSTIN Row,SomeoneNotReal,,,\n"
        f"{contact.gstin.lower()},dup case,,NoSuchPerson,,\n"
    ).encode("utf-8")

    transport = ASGITransport(app=app, raise_app_exceptions=True)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            c.cookies.set("csrf_token", "smoketest-csrf")
            r = await c.post(
                "/crm/contacts/team-mapping/upload",
                data={"csrf_token": "smoketest-csrf"},
                files={"file": ("team_mapping.csv", csv_body, "text/csv")},
            )
            assert r.status_code == 200, r.text[:500]
            assert "1" in r.text
            assert "missing gstin" in r.text.lower()
            assert "NoSuchPerson" in r.text

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(CRMContactTeamMember).where(CRMContactTeamMember.contact_id == contact.id)
            )).scalars().all()
            assert {(row.user_id, row.role_bucket) for row in rows} == {(user.id, "vp_avp")}
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(sa_delete(CRMContactTeamMember).where(
                CRMContactTeamMember.contact_id == contact.id
            ))
            if gstin_was_blank:
                c = await db.get(CRMContact, contact.id)
                if c:
                    c.gstin = None
            await db.commit()
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
