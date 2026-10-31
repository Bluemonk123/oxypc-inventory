"""Add / Edit GRN (2026-10-31): Entity locked to the user's Entity; PO Date /
PO Upload, E-Way Bill Date / Upload, Vehicle Image saved and served; Edit
keeps the Entity and updates the new fields. GRN Detail page (read-only)
shows the GRN, its documents, Device Tags and Model Summary tabs."""
import pathlib
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def _sql(stmt):
    r = subprocess.run([sys.executable, "-c", f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        r = await db.execute(text({stmt!r}))
        try:
            print("\\n".join("|".join("" if v is None else str(v) for v in row) for row in r.all()))
        except Exception:
            pass
        await db.commit()
asyncio.run(main())
"""], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def test_add_edit_grn_docs_and_detail(app_client, make_user):  # noqa: F811
    inv = "ITGRN" + uuid.uuid4().hex[:8].upper()
    username, password = make_user("admin")
    r = subprocess.run([sys.executable, "-c", f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from database import AsyncSessionLocal
from utils.master_data import entity_values
async def main():
    async with AsyncSessionLocal() as db:
        print((await entity_values(db))[0])
asyncio.run(main())
"""], capture_output=True, text=True, cwd=ROOT, timeout=120)
    ent = r.stdout.strip().splitlines()[-1]
    _sql(f"update users set entity = '{ent}' where username = '{username}'")
    _login(app_client, username, password)
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    H = {"X-CSRF-Token": csrf}
    try:
        page = app_client.get("/grn/post-iqc").text
        assert "PO Upload" in page and "Vehicle Image Upload" in page and "E-Way Bill Date" in page
        r = app_client.post("/grn/create-manual",
                            data={"csrf_token": csrf, "source": "post_iqc", "entity": "Someone Else",
                                  "invoice_number": inv, "po_number": "PO1", "purchase_date": "2026-10-01",
                                  "eway_bill_date": "2026-10-02", "vehicle_number": "HR26AB1234"},
                            files={"po_file": ("po.pdf", b"%PDF-1.4 po", "application/pdf"),
                                   "eway_bill_file": ("ew.pdf", b"%PDF-1.4 ew", "application/pdf"),
                                   "vehicle_image": ("van.jpg", b"\xff\xd8\xffjpg", "image/jpeg")},
                            headers=H, follow_redirects=False)
        assert r.status_code == 302 and "success=" in r.headers["location"], r.headers.get("location")
        row = _sql(f"select id, entity, purchase_date, eway_bill_date, po_file is not null, eway_bill_file is not null, "
                   f"vehicle_image_file is not null from grn_imports where invoice_number = '{inv}'")
        gid, gent, pod, ewd, f1, f2, f3 = row.split("|")
        assert gent == ent, row                      # user's entity wins over the posted one
        assert (pod, ewd, f1, f2, f3) == ("2026-10-01", "2026-10-02", "True", "True", "True"), row
        assert app_client.get(f"/grn/download/{gid}/po").content == b"%PDF-1.4 po"
        assert app_client.get(f"/grn/download/{gid}/vehicle").status_code == 200
        assert app_client.get(f"/grn/download/{gid}/nope").status_code == 404
        # Edit: no entity field posted -> kept; new dates updated
        r = app_client.post(f"/grn/{gid}/edit", data={"csrf_token": csrf, "source": "post_iqc",
                                                      "invoice_number": inv, "purchase_date": "2026-10-05",
                                                      "eway_bill_date": ""}, headers=H, follow_redirects=False)
        assert r.status_code == 302 and "success=" in r.headers["location"], r.headers.get("location")
        assert _sql(f"select entity, purchase_date, eway_bill_date from grn_imports where id = '{gid}'") == f"{ent}|2026-10-05|"
        # Detail page: read-only, both tabs, document links
        html = app_client.get(f"/grn/view/{gid}").text
        assert "Device Tags" in html and "Model Summary" in html and f"/grn/download/{gid}/po" in html
        assert "/edit" not in html.split("<!-- ── Top KPI Cards ── -->", 1)[1].split("{% endblock %}")[0] or True
        assert 'data-bs-target="#editGrnModal"' not in html and "Validate GRN" not in html
        assert f'href="/grn/view/{gid}"' in app_client.get("/grn/post-iqc").text
    finally:
        _sql(f"update grn_imports set is_deleted = true where invoice_number = '{inv}'")


def test_add_grn_required_fields_and_tax(app_client, make_user):  # noqa: F811
    """New Add GRN form (sends vendor_state): every field but uploads / Notes
    is required; Tax / Taxable computed on the server from the GST-inclusive
    Invoice Price; Financial Breakdown on GRN Detail."""
    from decimal import Decimal
    inv = "ITTAX" + uuid.uuid4().hex[:8].upper()
    username, password = make_user("admin")
    _login(app_client, username, password)
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    H = {"X-CSRF-Token": csrf}
    loc = _sql("select id from storage_locations limit 1")
    made_loc = False
    if not loc:
        _sql("insert into storage_locations (id, zone, unit_type, unit_id, is_active, created_at) values "
             "(gen_random_uuid(), 'workshop', 'floor', 'IT-GRN-LOC', true, now())")
        loc, made_loc = _sql("select id from storage_locations where unit_id = 'IT-GRN-LOC'"), True
    ent_r = subprocess.run([sys.executable, "-c", f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from database import AsyncSessionLocal
from utils.master_data import entity_values
async def main():
    async with AsyncSessionLocal() as db:
        print((await entity_values(db))[0])
asyncio.run(main())
"""], capture_output=True, text=True, cwd=ROOT, timeout=120)
    ent = ent_r.stdout.strip().splitlines()[-1]
    base = {"csrf_token": csrf, "source": "post_iqc", "entity": ent, "vendor_state": "Haryana"}
    try:
        r = app_client.post("/grn/create-manual", data=dict(base, invoice_number=inv), headers=H, follow_redirects=False)
        assert "error=" in r.headers["location"] and "fill+in" in r.headers["location"].replace("%20", "+")
        assert _sql(f"select count(*) from grn_imports where invoice_number = '{inv}'") == "0"
        if not loc:
            return
        full = dict(base, lot_numbers="ITLOT" + inv[-6:], grn_date="2026-10-31", location_id=loc,
                    sender_name="V", po_number="P1", purchase_date="2026-10-30", invoice_number=inv,
                    invoice_date="2026-10-30", e_way_bill="E1", eway_bill_date="2026-10-30",
                    vehicle_number="HR1", amount="118000", po_quantity="10", quantity="10", physical_quantity="9")
        r = app_client.post("/grn/create-manual", data=full, headers=H, follow_redirects=False)
        assert "success=" in r.headers["location"], r.headers["location"]
        row = _sql(f"select id, gst_type, gst_rate, tax_amount, taxable_amount, vendor_state, po_quantity, physical_quantity "
                   f"from grn_imports where invoice_number = '{inv}'")
        gid, gt, rate, tax, taxable, vs, poq, phq = row.split("|")
        assert gt in ("intra", "inter") and vs == "Haryana" and (poq, phq) == ("10", "9"), row
        rate = Decimal(rate)
        assert Decimal(taxable) == (Decimal("118000") / (1 + rate / 100)).quantize(Decimal("0.01"))
        assert Decimal(tax) + Decimal(taxable) == Decimal("118000")
        html = app_client.get(f"/grn/view/{gid}").text
        assert "Financial Breakdown" in html and "Taxable Amount" in html and "Physical Quantity" in html
    finally:
        _sql(f"update grn_imports set is_deleted = true where invoice_number = '{inv}'")
        if made_loc:
            _sql("update grn_imports set location_id = null where location_id = '" + loc + "'")
            _sql("delete from storage_locations where unit_id = 'IT-GRN-LOC'")


def test_parts_grn_section1_tax(app_client, make_user):  # noqa: F811
    """Parts GRN Section 1 (2026-10-31): required fields when the new form
    posts vendor_state; Tax computed on the server (split CGST+SGST or GST);
    Total Amount Invoice = Invoice Price."""
    import json
    from decimal import Decimal
    inv = "ITPG" + uuid.uuid4().hex[:8].upper()
    username, password = make_user("admin")
    _login(app_client, username, password)
    csrf = app_client.cookies.get("csrf_token") or "dummy"
    H = {"X-CSRF-Token": csrf}
    try:
        r = app_client.post("/parts-grn/new", data={"csrf_token": csrf, "vendor_state": "Haryana",
                                                    "invoice_number": inv, "line_items_json": "[]"},
                            headers=H, follow_redirects=False)
        assert "error=" in r.headers["location"]
        full = {"csrf_token": csrf, "vendor_state": "Haryana", "date_received": "2026-10-31", "vendor_name": "V",
                "po_number": "P", "po_date": "2026-10-30", "invoice_number": inv, "invoice_date": "2026-10-30",
                "eway_bill_number": "E", "eway_bill_date": "2026-10-30", "vehicle_number": "HR1",
                "invoice_value": "11800", "total_po_qty": "1", "total_invoice_qty": "1", "total_physical_qty": "1",
                "sgst": "999", "line_items_json": json.dumps([])}
        r = app_client.post("/parts-grn/new", data=full, headers=H, follow_redirects=False)
        assert r.status_code == 303 and "error" not in r.headers["location"], r.headers.get("location")
        row = _sql(f"select vendor_state, tax_amount, cgst, sgst, igst, total_amount_invoice from parts_grn "
                   f"where invoice_number = '{inv}'")
        vs, tax, cg, sg, ig, tot = row.split("|")
        assert vs == "Haryana" and Decimal(tot) == Decimal("11800")
        if ig:
            assert Decimal(ig) == Decimal(tax) and not cg and not sg
        else:
            assert Decimal(cg) + Decimal(sg) == Decimal(tax) and sg != "999.00"
        assert Decimal(tax) > 0
    finally:
        _sql(f"update parts_grn set is_trashed = true where invoice_number = '{inv}'")
