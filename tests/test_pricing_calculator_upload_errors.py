"""Pricing Calculator upload (2026-11-04): new SKUs are 5 digits; rows left
out say why — which field and what is wrong — in the popup list and in an
"Error" column of the downloaded file."""
import asyncio
import base64
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)


def test_new_sku_is_five_digits():
    from models.product_master import new_sku
    taken = set()
    for _ in range(50):
        v = new_sku(taken)
        assert len(v) == 5 and v.isdigit() and v[0] != "0"
    assert len(taken) == 50


def _make_product():
    from database import AsyncSessionLocal
    from models.product_master import PMProduct, build_product_name

    async def go():
        async with AsyncSessionLocal() as s:
            model = "ITest " + uuid.uuid4().hex[:6]
            sku = str(10000 + int(uuid.uuid4().hex[:4], 16) % 89999)
            p = PMProduct(sku=sku, make="HP", model=model, cpu="i5", cpu_gen="8th",
                          ram_type="DDR4", ram_size="8 GB", storage_type="SSD", storage_size="256 GB",
                          product_name=build_product_name("HP", model, "i5", "8th"), created_by="itest")
            s.add(p)
            await s.commit()
            return str(p.id), sku, model
    return asyncio.run(go())


def _drop_product(pid):
    from database import AsyncSessionLocal
    from sqlalchemy import delete, select
    from models.product_master import PMProduct, PMCalcRow, PMCalcUpload

    async def go():
        async with AsyncSessionLocal() as s:
            p = (await s.execute(select(PMProduct).where(PMProduct.id == pid))).scalar_one_or_none()
            if p:
                ups = (await s.execute(select(PMCalcRow.upload_id).where(PMCalcRow.sku == p.sku))).scalars().all()
                await s.execute(delete(PMCalcRow).where(PMCalcRow.sku == p.sku))
                if ups:
                    await s.execute(delete(PMCalcUpload).where(PMCalcUpload.id.in_(set(ups))))
                await s.delete(p)
            await s.commit()
    asyncio.run(go())


def test_upload_reports_field_and_reason(app_client, make_user):  # noqa: F811
    try:
        pid, sku, model = _make_product()
    except Exception:   # product model needs fields this test does not know about
        import pytest
        pytest.skip("could not create a Product Master row")
    u, pw = make_user("admin")
    _login(app_client, u, pw)
    head = "SKU,Make,Model,CPU,CPU Gen,Ram Type,Ram Size,Storage Type,Storage Size,Bazel,Screen\r\n"
    rows = [
        f"{sku},HP,{model},i5,8th,DDR4,8 GB,SSD,256 GB,Ok,Ok",            # added
        f"{sku},HP,{model},i5,8th,DDR4,16 Gb,SSD,128GB,Ok,Damaged",      # other RAM / SSD: added too
        "2028549713742,HP,EliteBook 840 G6,i5,8th,DDR4,8 GB,SSD,256 GB,Ok,Ok",  # 13-digit SKU
        f",HP,{model},i5,8th,DDR4,8 GB,SSD,256 GB,Ok,Ok",                 # blank SKU
        f"{sku},HP,{model},i7,8th,DDR4,16 Gbx,SSD,,Ok,Ok",               # CPU differs, bad RAM, no SSD size
    ]
    data = (head + "\r\n".join(rows) + "\r\n").encode()
    try:
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post("/product-master/pricing-calculator/upload", data={"csrf_token": csrf},
                            files={"file": ("calc.csv", data, "text/csv")})
        j = r.json()
        assert j["ok"] and j["added"] == 2, j
        errs = {p["row"]: p["error"] for p in j["problems"]}
        assert 3 not in errs
        assert "SKU '2028549713742' is not in Product Master" in errs[4] and "13 digits" in errs[4]
        assert errs[5] == "SKU is empty"
        assert "CPU 'i7' does not match Product Master 'i5'" in errs[6]
        assert "Ram Size '16 Gbx' is not in Product Master → Dropdown Configuration" in errs[6]
        assert "Storage Size is empty" in errs[6]
        miss = base64.b64decode(j["missing_file_b64"]).decode("utf-8-sig")
        assert miss.splitlines()[0].endswith(",Error") and "13 digits" in miss
        mis = base64.b64decode(j["mismatch_file_b64"]).decode("utf-8-sig")
        assert "16 Gbx" in mis

        # A file without a configuration column is refused up front, naming it.
        bad = ("SKU,Make,Model,CPU,CPU Gen,Ram Type,Storage Type,Storage Size\r\n"
               f"{sku},HP,{model},i5,8th,DDR4,SSD,256 GB\r\n").encode()
        r = app_client.post("/product-master/pricing-calculator/upload", data={"csrf_token": csrf},
                            files={"file": ("calc.csv", bad, "text/csv")})
        j = r.json()
        assert not j["ok"] and "Ram Size" in j["error"], j
    finally:
        _drop_product(pid)


def test_part_deducted_unless_ok():
    """2026-11-04: OK / Okay (any case) or blank = no deduction; anything else deducts."""
    from routers.product_master import _is_damaged
    for v in ("OK", "ok", "Okay", "OKAY", " okay ", ""):
        assert not _is_damaged(v), v
    for v in ("Damaged", "Not Working", "Missing", "Scratch", "Broken"):
        assert _is_damaged(v), v
