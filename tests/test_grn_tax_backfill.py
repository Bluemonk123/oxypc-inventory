"""Data Fix (2026-11-02): old GRNs with an Invoice Amount but no Tax get the
picked Vendor State and Tax / Taxable computed like a new GRN; blank picks
are skipped; Parts GRNs get the CGST+SGST / IGST split."""
import pathlib
import subprocess
import sys
import uuid

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


def test_grn_tax_backfill():
    tag = uuid.uuid4().hex[:6].upper()
    r = subprocess.run([sys.executable, "-c", f"""
import asyncio, sys, uuid
from decimal import Decimal
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import select, text
from database import AsyncSessionLocal
from models.grn_import import GRNImport
from models.parts_grn import PartsGRN
from models.user import User
from services.grn_tax import tax_backfill_rows, tax_backfill_apply, gst_rates
async def main():
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).limit(1))).scalar()
        g1 = GRNImport(id=uuid.uuid4(), grn_number="9{tag}01", invoice_number="ITTB{tag}A", amount=Decimal("11800"))
        g2 = GRNImport(id=uuid.uuid4(), grn_number="9{tag}02", invoice_number="ITTB{tag}B", amount=Decimal("5900"))
        p1 = PartsGRN(id=uuid.uuid4(), grn_number="PT{tag}", invoice_number="ITTB{tag}P", invoice_value=Decimal("1180"))
        db.add_all([g1, g2, p1]); await db.commit()
        keys = {{r["key"] for r in (lambda x: x["device"] + x["parts"])(await tax_backfill_rows(db))}}
        listed = all(k in keys for k in (f"d:{{g1.id}}", f"d:{{g2.id}}", f"p:{{p1.id}}"))
        res = await tax_backfill_apply(db, user, {{f"d:{{g1.id}}": "Haryana", f"d:{{g2.id}}": "", f"p:{{p1.id}}": "Haryana"}})
        await db.refresh(g1); await db.refresh(g2); await db.refresh(p1)
        intra, inter = await gst_rates(db)
        rate = Decimal(str(g1.gst_rate))
        ok1 = g1.vendor_state == "Haryana" and g1.tax_amount + g1.taxable_amount == Decimal("11800") \\
              and g1.taxable_amount == (Decimal("11800") / (1 + rate / 100)).quantize(Decimal("0.01"))
        ok2 = g2.tax_amount is None and g2.vendor_state is None
        split = (p1.igst == p1.tax_amount) if p1.igst is not None else (p1.cgst + p1.sgst == p1.tax_amount)
        again = f"d:{{g1.id}}" in {{r["key"] for r in (await tax_backfill_rows(db))["device"]}}
        print(listed, res["updated"] == 2, ok1, ok2, split and p1.tax_amount > 0, again)
        for t, i in (("grn_imports", g1.id), ("grn_imports", g2.id), ("parts_grn", p1.id)):
            await db.execute(text(f"delete from {{t}} where id = :i"), {{"i": i}})
        await db.commit()
asyncio.run(main())
"""], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip().splitlines()[-1] == "True True True True True False", r.stdout
