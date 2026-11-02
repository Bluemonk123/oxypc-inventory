"""GRN tax (2026-10-31): Tax Amount / Taxable Amount from the GST-inclusive
Invoice Price.

GST rate comes from Cost Config ("Additional Rates"): Intra-State (CGST +
SGST, gst_rate_intra) when the Vendor State equals the company state of the
GRN's Entity (Company Setting, company_entity), otherwise Inter-State (IGST,
gst_rate_inter). Taxable = Price / (1 + rate%); Tax = Price - Taxable.
Server-side Decimal maths; the Add / Edit GRN modal shows the same figures
live, but what is saved is computed here."""
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def gst_rates(db: AsyncSession) -> tuple[Decimal, Decimal]:
    from models.cost_config import CostConfig
    rows = {k: v for k, v in (await db.execute(
        select(CostConfig.key, CostConfig.value).where(
            CostConfig.key.in_(["gst_rate_intra", "gst_rate_inter"])))).all()}
    return (Decimal(str(rows.get("gst_rate_intra", 18))), Decimal(str(rows.get("gst_rate_inter", 18))))


async def company_states(db: AsyncSession) -> dict:
    """{entity: company state}, plus "" -> the first active company's state."""
    from models.company import Company
    out: dict = {}
    for ent, st in (await db.execute(
            select(Company.company_entity, Company.company_state)
            .where(Company.is_active == True).order_by(Company.created_at))).all():  # noqa: E712
        if st:
            out.setdefault((ent or "").strip(), st.strip())
            out.setdefault("", st.strip())
    return out


async def compute_grn_tax(db: AsyncSession, entity, vendor_state, price):
    """(gst_type, gst_rate, tax_amount, taxable_amount); all None without a
    price or a vendor state."""
    if price is None or not (vendor_state or "").strip():
        return None, None, None, None
    price = Decimal(str(price))
    intra_rate, inter_rate = await gst_rates(db)
    states = await company_states(db)
    home = states.get((entity or "").strip()) or states.get("", "")
    intra = bool(home) and home.strip().lower() == vendor_state.strip().lower()
    rate = intra_rate if intra else inter_rate
    q = Decimal("0.01")
    taxable = (price / (Decimal(1) + rate / Decimal(100))).quantize(q, rounding=ROUND_HALF_UP)
    tax = (price - taxable).quantize(q, rounding=ROUND_HALF_UP)
    return ("intra" if intra else "inter"), rate, tax, taxable


# ── Data Fix (2026-11-02): tax on old GRNs that already have an Invoice Amount ──
# GRNs made before the new Add / Edit GRN have a price but no Vendor State, so
# Tax / Taxable were never worked out. The admin picks each GRN's Vendor State
# on the Data Fixes page; Apply saves it and computes the tax exactly as a new
# GRN would (same rates, same intra / inter rule).

async def tax_backfill_rows(db: AsyncSession) -> dict:
    from models.grn_import import GRNImport
    from models.parts_grn import PartsGRN
    from sqlalchemy import func
    dev = (await db.execute(
        select(GRNImport.id, GRNImport.grn_number, GRNImport.sender_name, GRNImport.entity,
               GRNImport.amount, GRNImport.vendor_state, GRNImport.grn_date, GRNImport.created_at)
        .where(GRNImport.is_deleted.isnot(True), GRNImport.amount > 0, GRNImport.tax_amount.is_(None))
        .order_by(GRNImport.created_at.desc()))).all()
    parts = (await db.execute(
        select(PartsGRN.id, PartsGRN.grn_number, PartsGRN.vendor_name, PartsGRN.created_by,
               PartsGRN.invoice_value, PartsGRN.vendor_state, PartsGRN.created_at)
        .where(PartsGRN.is_trashed.isnot(True), PartsGRN.invoice_value > 0,
               func.coalesce(PartsGRN.tax_amount, 0) == 0)
        .order_by(PartsGRN.created_at.desc()))).all()
    return {
        "device": [{"key": f"d:{r.id}", "grn": r.grn_number, "vendor": r.sender_name or "",
                    "entity": r.entity or "", "amount": r.amount, "state": r.vendor_state or "",
                    "date": r.grn_date or (r.created_at.date() if r.created_at else None)} for r in dev],
        "parts": [{"key": f"p:{r.id}", "grn": r.grn_number, "vendor": r.vendor_name or "",
                   "by": r.created_by or "", "amount": r.invoice_value, "state": r.vendor_state or "",
                   "date": r.created_at.date() if r.created_at else None} for r in parts],
    }


async def tax_backfill_apply(db: AsyncSession, user, picks: dict, request=None) -> dict:
    """picks = {"d:<grn id>" | "p:<parts grn id>": vendor state}. Only GRNs
    still listed (price set, no tax yet) are touched; blank picks are skipped."""
    import uuid as _uuid
    from models.grn_import import GRNImport
    from models.parts_grn import PartsGRN
    from models.user import User
    from services.audit_engine import audit
    listed = await tax_backfill_rows(db)
    open_keys = {r["key"] for r in listed["device"] + listed["parts"]}
    done, before_after = 0, {}
    q = Decimal("0.01")
    users = {}
    for key, state in picks.items():
        state = (state or "").strip()
        if not state or key not in open_keys:
            continue
        kind, rid = key.split(":", 1)
        rid = _uuid.UUID(rid)
        if kind == "d":
            g = await db.get(GRNImport, rid)
            gt, rate, tax, taxable = await compute_grn_tax(db, g.entity, state, g.amount)
            if tax is None:
                continue
            before_after[g.grn_number] = {"vendor_state": [g.vendor_state, state], "gst_type": gt,
                                          "gst_rate": str(rate), "tax": str(tax), "taxable": str(taxable)}
            g.vendor_state, g.gst_type, g.gst_rate, g.tax_amount, g.taxable_amount = state, gt, rate, tax, taxable
        else:
            g = await db.get(PartsGRN, rid)
            if g.created_by and g.created_by not in users:
                users[g.created_by] = (await db.execute(
                    select(User.entity).where(User.username == g.created_by))).scalar()
            gt, rate, tax, taxable = await compute_grn_tax(db, users.get(g.created_by), state, g.invoice_value)
            if tax is None:
                continue
            half = (tax / 2).quantize(q, rounding=ROUND_HALF_UP)
            before_after["P:" + g.grn_number] = {
                "vendor_state": [g.vendor_state, state], "gst_type": gt, "gst_rate": str(rate),
                "tax": str(tax), "before": {"tax_amount": str(g.tax_amount) if g.tax_amount is not None else None,
                                            "cgst": str(g.cgst) if g.cgst is not None else None,
                                            "sgst": str(g.sgst) if g.sgst is not None else None,
                                            "igst": str(g.igst) if g.igst is not None else None}}
            g.vendor_state, g.tax_amount = state, tax
            g.cgst = half if gt == "intra" else None
            g.sgst = (tax - half) if gt == "intra" else None
            g.igst = tax if gt == "inter" else None
        done += 1
    if done:
        await audit(db, user=user, action="GRN_TAX_BACKFILL", table_name="grn_imports/parts_grn",
                    record_id=f"{done} GRNs", new_value={"count": done, "changes": before_after},
                    notes="Old GRNs: Vendor State picked on Data Fixes; Tax / Taxable computed from the "
                          "Invoice Amount with the Cost Config GST rates.", request=request)
        await db.commit()
    return {"ok": True, "updated": done}
