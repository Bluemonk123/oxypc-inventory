"""Parts dropdown cards on Parts GRN (2026-11-23, Pankaj): Spare Part Brands /
Spare Part Names / Parts: Part Category shown under the Parts GRN table, edited
by the Parts Manager too; same Master Data rows, so a change on either page
shows on both. Other Master Data lists can't be changed through these routes."""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2500:]
    return r.stdout.strip()


def _q(sql):
    return _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        r = await db.execute(text({sql!r}))
        try:
            print("|".join(str(x) for x in r.first() or []))
        except Exception:
            pass
        await db.commit()
asyncio.run(main())
""")


def test_parts_lists_on_grn(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:6].upper()
    val, val2, val3 = f"ZZBrand{sfx}", f"ZZBrandB{sfx}", f"ZZName{sfx}"
    try:
        u, p = make_user("spare_parts_manager")
        _login(app_client, u, p)
        csrf = app_client.cookies.get("csrf_token", "")
        h = {"x-csrf-token": csrf}
        page = app_client.get("/parts-grn").text
        assert 'id="partsLists"' in page
        cards = page.split('id="partsLists"', 1)[1].split('id="plValueModal"', 1)[0]
        assert "pl-upload" not in cards and "/toggle" not in cards     # Upload + Hide/Show hidden here (2026-11-23)
        assert "pl-add" in cards and "/export/" in cards
        for lab in ("Spare Part Brands", "Spare Part Names", "Parts: Part Category"):
            assert lab in page
        # Parts Manager adds on Parts GRN -> it is a Master Data value
        r = app_client.post("/parts-grn/master/add", headers=h, follow_redirects=False,
                            data={"csrf_token": csrf, "category": "spare_part_brand", "value": val})
        assert r.status_code == 302 and "success=" in r.headers["location"], r.headers["location"]
        row = _q(f"select id, category, is_active from master_data where value = '{val}'").split("|")
        cards = app_client.get("/parts-grn").text.split('id="partsLists"', 1)[1].split('id="plValueModal"', 1)[0]
        assert val in cards and "pl-edit" in cards and "/delete" in cards and "/toggle" not in cards
        assert row[1] == "spare_part_brand" and row[2] == "True"
        r = app_client.post("/parts-grn/master/add", headers=h, follow_redirects=False,
                            data={"csrf_token": csrf, "category": "spare_part_brand", "value": val.lower()})
        assert "error=" in r.headers["location"]                        # no duplicates
        # Edit, hide, other lists refused
        app_client.post(f"/parts-grn/master/{row[0]}/edit", headers=h, data={"csrf_token": csrf, "value": val2})
        assert _q(f"select value from master_data where id = '{row[0]}'") == val2
        app_client.post(f"/parts-grn/master/{row[0]}/toggle", headers=h, data={"csrf_token": csrf})
        assert _q(f"select is_active from master_data where id = '{row[0]}'") == "False"
        other = _q("select id from master_data where category not in ('spare_part_brand','part_category','iqc_part_category') limit 1")
        if other:
            assert app_client.post(f"/parts-grn/master/{other}/delete", headers=h,
                                   data={"csrf_token": csrf}).status_code == 404
        assert app_client.post("/parts-grn/master/add", headers=h, data={
            "csrf_token": csrf, "category": "brand", "value": "X"}).status_code == 400
        x = app_client.get("/parts-grn/master/export/spare_part_brand")
        assert x.status_code == 200 and val2 in x.text
        r = app_client.post("/parts-grn/master/bulk-upload/part_category", headers=h, follow_redirects=False,
                            data={"csrf_token": csrf}, files={"file": ("p.csv", f"value\n{val3}\n".encode(), "text/csv")})
        assert "1+added" in r.headers["location"] or "1%20added" in r.headers["location"], r.headers["location"]
        # Admin's Master Data page shows the Parts Manager's changes; an admin rename shows on Parts GRN
        ua, pa = make_user("admin")
        _login(app_client, ua, pa)
        csrf = app_client.cookies.get("csrf_token", "")
        m = app_client.get("/admin/master").text
        assert val2 in m and val3 in m
        nid = _q(f"select id from master_data where value = '{val3}'")
        app_client.post(f"/admin/master/{nid}/edit", headers={"x-csrf-token": csrf},
                        data={"csrf_token": csrf, "value": val3 + "X"})
        assert (val3 + "X") in app_client.get("/parts-grn").text
        # Delete on Parts GRN removes it from Master Data
        app_client.post(f"/parts-grn/master/{row[0]}/delete", headers={"x-csrf-token": csrf}, data={"csrf_token": csrf})
        assert _q(f"select count(*) from master_data where id = '{row[0]}'") == "0"
    finally:
        _q(f"delete from master_data where value in ('{val}', '{val2}', '{val3}', '{val3}X')")
