"""Supplier Deal documents, as the deal page's Documents card lists them
(2026-11-11, Pankaj): Inspection Report, Quotation, Purchase Order, Payment
Invoice, Product Data Sheet — each a deal step file. Uploads made before the
Documents card (the deal's old Purchase Invoice / Purchase Order / E-way Bill
/ Quote Received fields) still count: they fill Purchase Order, Payment
Invoice and Quotation when that slot is empty, and E-way Bill is listed when
present. Used by Parts Manager → Sourcing Requests (Documents column and the
Verify pop-up)."""
from sqlalchemy import select

SOURCING_DOCS = [("inspection_report", "Inspection Report", "bi-clipboard-check"),
                 ("quote_file", "Quotation", "bi-file-earmark-text"),
                 ("doc_purchase_order", "Purchase Order", "bi-file-earmark-ruled"),
                 ("payment_invoice", "Payment Invoice", "bi-receipt"),
                 ("doc_product_data_sheet", "Product Data Sheet", "bi-file-earmark-spreadsheet")]
_LEGACY = {"quote_file": "quote_received_path", "doc_purchase_order": "purchase_order_path",
           "payment_invoice": "purchase_invoice_path"}


async def sourcing_deal_docs(db, deals) -> dict:
    """{str(deal.id): [{key, label, icon, url}]} — url None when not uploaded."""
    from models.crm import CRMSourcingStep
    deals = [d for d in deals if d]
    if not deals:
        return {}
    keys = [k for k, _l, _i in SOURCING_DOCS]
    files = {}
    for st in (await db.execute(select(CRMSourcingStep).where(
            CRMSourcingStep.deal_id.in_([d.id for d in deals]), CRMSourcingStep.step_key.in_(keys)))).scalars().all():
        if st.file_path:
            files[(st.deal_id, st.step_key)] = st.file_path
    out = {}
    for d in deals:
        rows = []
        for k, label, icon in SOURCING_DOCS:
            fp = files.get((d.id, k)) or (getattr(d, _LEGACY[k], None) if k in _LEGACY else None)
            rows.append({"key": k, "label": label, "icon": icon, "url": f"/uploads/crm/{fp}" if fp else None})
        if getattr(d, "eway_bill_path", None):
            rows.append({"key": "eway_bill", "label": "E-way Bill", "icon": "bi-truck", "url": f"/uploads/crm/{d.eway_bill_path}"})
        out[str(d.id)] = rows
    return out
