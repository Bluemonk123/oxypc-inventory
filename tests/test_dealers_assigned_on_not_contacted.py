"""Dealers table (2026-11-03): "Assigned On" column + Assign Date filter
(latest dealer_assignments row for the current assignee, else the date the
dealer was added), bulk assign records the date, and the "Not Contacted Yet"
checkbox (not_contacted=1) keeps dealers whose Status column is empty."""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_assigned_on_and_not_contacted(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:8].upper()
    a, b, c = (f"ZZAON{k}{sfx}" for k in "ABC")
    _run(f"""
import asyncio, sys, uuid
from datetime import datetime
sys.path.insert(0, r"{ROOT}")
from database import AsyncSessionLocal
from models.dealers import Dealer, DealerCall, DealerAssignment
async def main():
    async with AsyncSessionLocal() as db:
        mk = lambda n, code: Dealer(id=uuid.uuid4(), dealer_code=code, business_name=n, dealer_type="retail",
                                    assigned_to="aon_rep", created_at=datetime(2026, 9, 1, 10, 0))
        da, dbb, dc = mk("{a}", "ZA{sfx}"), mk("{b}", "ZB{sfx}"), mk("{c}", "ZC{sfx}")
        db.add_all([da, dbb, dc]); await db.flush()
        db.add(DealerAssignment(dealer_id=da.id, assigned_to="aon_rep", assigned_by="t",
                                assigned_at=datetime(2026, 10, 5, 12, 0)))
        db.add(DealerAssignment(dealer_id=da.id, assigned_to="someone_else", assigned_by="t",
                                assigned_at=datetime(2026, 10, 20, 12, 0)))      # not the current assignee
        db.add(DealerCall(dealer_id=dbb.id, called_by="aon_rep", call_date=datetime(2026, 10, 6), call_outcome="interested"))
        db.add(DealerCall(dealer_id=dc.id, called_by="aon_rep", call_date=datetime(2026, 10, 6), call_outcome=" "))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        html = app_client.get("/dealers").text
        assert "<th>Assigned On</th>" in html and 'name="assign_from"' in html and "dlNotContacted" in html
        # 2026-11-10: the Calling Date range is hidden from the filter bar (kept
        # only as a hidden input when a link already carries it)
        assert 'name="calling_date_from"' not in html
        assert 'name="quotes_shared"' in html

        def rows(q):
            j = app_client.get(f"/dealers/data?draw=1&start=0&length=200&search[value]=ZZAON&{q}").json()
            return {r[1].split(">")[-3].split("<")[0] if "gtable-clamp2" in r[1] else r[1].split(">")[1].split("<")[0]: r
                    for r in j["data"] if sfx in r[1]}
        allr = rows("")
        assert set(allr) == {a, b, c}
        assert "05-10-2026" in allr[a][-3] and "01-09-2026" in allr[b][-3]          # Assigned On before Assigned
        assert set(rows("not_contacted=1")) == {a, c}
        assert set(rows("assign_from=2026-10-01&assign_to=2026-10-31")) == {a}
        assert set(rows("assign_from=2026-09-01&assign_to=2026-09-01")) == {b, c}
        ids = app_client.get("/dealers/ids?search[value]=ZZAON&not_contacted=1").json()["ids"]
        assert len([i for i in ids]) >= 2
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from dealers where dealer_code in ('ZA{sfx}','ZB{sfx}','ZC{sfx}')"
        await db.execute(text(f"delete from dealer_assignments where dealer_id in ({{ids}})"))
        await db.execute(text(f"delete from dealer_calls where dealer_id in ({{ids}})"))
        await db.execute(text("delete from dealers where dealer_code in ('ZA{sfx}','ZB{sfx}','ZC{sfx}')"))
        await db.commit()
asyncio.run(main())
""")


def test_record_dealer_assignment_only_on_change():
    from types import SimpleNamespace
    from routers.dealers import record_dealer_assignment
    added = []
    db = SimpleNamespace(add=added.append)
    d = SimpleNamespace(id=uuid.uuid4(), assigned_to="x")
    record_dealer_assignment(db, d, "x", "admin")      # same owner: nothing
    record_dealer_assignment(db, d, None, "admin")     # unassign: nothing
    record_dealer_assignment(db, d, "y", "admin")
    assert len(added) == 1 and added[0].assigned_to == "y" and added[0].assigned_by == "admin"
