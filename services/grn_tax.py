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
