"""Download column on the CRM listing pages (2026-11-10, Pankaj).

One file -> a direct download button; several -> a "Download" dropdown
(templates/_download_cell.html). A record only offers documents once it is
approved:

  Deal Quotations       status Approved                 -> Quote (print), Generated Quote PDF
  Purchase Orders       its Supplier Deal's PO Level 1  -> Generated PO, Supplier Invoice, Payment Snapshot
  Sales Orders          always (an SO exists only after Quote Level 2) -> Sales Order (print)
  Supplier Deals        each file once its stage is approved:
                          Inspection Report (no approval), Quote (Quote L2), Updated Quote
                          (Negotiation L1), Generated PO (PO L1), Invoice (Payment L1);
                          Deal Info uploads always
  Customer Deals        Screening Report (no approval), Quote (Quote L2), Updated Quote
                          (Negotiation L1), Sales Order (exists), Customer PO (SO L2),
                          Invoice (Payment L1); Deal Info uploads + Product Records always

Printed documents use the Company Setting company of the record's From Entity
(routers/settings.get_company_settings(entity=...)).
"""
import os

from sqlalchemy import select

from models.crm import (CRMSourcingStep, CRMSalesStep, CRMPurchaseOrder, CRMQuote,
                        CRMSalesOrder)


def _f(label, url, newtab=True):
    return {"label": label, "url": url, "newtab": newtab}


def _crm(path):
    return f"/uploads/crm/{path}"


def _acc(path):
    return f"/uploads/accounts/{path}"


async def _steps(db, model, deal_ids):
    out = {}
    if not deal_ids:
        return out
    for s in (await db.execute(select(model).where(model.deal_id.in_(deal_ids)))).scalars().all():
        out.setdefault(str(s.deal_id), {})[s.step_key] = s
    return out


def _ok(steps, key):
    s = steps.get(key)
    return bool(s and s.status == "approved")


def _file(steps, key):
    s = steps.get(key)
    return s.file_path if s and s.file_path else None


async def quote_downloads(db, quotes) -> dict:
    out = {}
    for q in quotes:
        # The quote document exists from the moment the quote is added (2026-11-11)
        out[str(q.id)] = [_f("Quote", f"/crm/quotes/{q.id}/download", False)]
    return out


async def po_downloads(db, pos) -> dict:
    steps = await _steps(db, CRMSourcingStep, list({p.deal_id for p in pos if p.deal_id}))
    out = {}
    for p in pos:
        files = []
        if p.deal_id and _ok(steps.get(str(p.deal_id), {}), "po_l1"):
            files.append(_f("Generated PO", f"/crm/purchase-orders/{p.id}/print"))
            if p.invoice_path:
                files.append(_f("Supplier Invoice", _acc(p.invoice_path)))
            if p.payment_snapshot_path:
                files.append(_f("Payment Snapshot", _acc(p.payment_snapshot_path)))
        out[str(p.id)] = files
    return out


async def so_downloads(db, orders) -> dict:
    return {str(o.id): [_f("Sales Order", f"/crm/sales-orders/{o.id}/print")] for o in orders}


async def sourcing_downloads(db, deals) -> dict:
    ids = [d.id for d in deals]
    steps = await _steps(db, CRMSourcingStep, ids)
    has_po = set()
    if ids:
        has_po = {str(x) for x in (await db.execute(
            select(CRMPurchaseOrder.deal_id).where(CRMPurchaseOrder.deal_id.in_(ids)))).scalars().all()}
    out = {}
    for d in deals:
        st, files = steps.get(str(d.id), {}), []
        if _file(st, "inspection_report"):
            files.append(_f("Inspection Report", _crm(_file(st, "inspection_report"))))
        if _file(st, "quote_file") and _ok(st, "quote_l1") and _ok(st, "quote_l2"):   # both levels, any order (2026-11-11)
            files.append(_f("Quote", _crm(_file(st, "quote_file"))))
        if _file(st, "negotiation_quote") and _ok(st, "negotiation_l1"):
            files.append(_f("Updated Quote", _crm(_file(st, "negotiation_quote"))))
        if str(d.id) in has_po and _ok(st, "po_l1"):
            files.append(_f("Generated PO", f"/crm/sourcing/{d.id}/download-po", False))
        if _file(st, "payment_invoice") and _ok(st, "payment_l1"):
            files.append(_f("Invoice", _crm(_file(st, "payment_invoice"))))
        for attr, label in (("purchase_invoice_path", "Purchase Invoice"), ("purchase_order_path", "Purchase Order"),
                            ("eway_bill_path", "E-way Bill"), ("quote_received_path", "Quote Received")):
            if getattr(d, attr, None):
                files.append(_f(label, _crm(getattr(d, attr))))
        out[str(d.id)] = files
    return out


async def sales_downloads(db, opps) -> dict:
    ids = [o.id for o in opps]
    steps = await _steps(db, CRMSalesStep, ids)
    latest_quote, so_by_opp = {}, {}
    if ids:
        for q in (await db.execute(select(CRMQuote).where(CRMQuote.opportunity_id.in_(ids))
                                   .order_by(CRMQuote.created_at.desc()))).scalars().all():
            latest_quote.setdefault(str(q.opportunity_id), q)
        for so in (await db.execute(select(CRMSalesOrder).where(CRMSalesOrder.deal_id.in_(ids))
                                    .order_by(CRMSalesOrder.created_at.desc()))).scalars().all():
            so_by_opp.setdefault(str(so.deal_id), so)
    out = {}
    for o in opps:
        st, files = steps.get(str(o.id), {}), []
        if _file(st, "screening_report"):
            files.append(_f("Screening Report", _crm(_file(st, "screening_report"))))
        q = latest_quote.get(str(o.id))
        if q and _ok(st, "quote_l1") and _ok(st, "quote_l2"):   # both levels, any order (2026-11-11)
            files.append(_f("Quote", f"/crm/quotes/{q.id}/print"))
        if _file(st, "negotiation_quote") and _ok(st, "negotiation_l1"):
            files.append(_f("Updated Quote", _crm(_file(st, "negotiation_quote"))))
        so = so_by_opp.get(str(o.id))
        if so:
            files.append(_f("Sales Order", f"/crm/sales-orders/{so.id}/print"))
        if _file(st, "so_file") and _ok(st, "so_l1"):
            files.append(_f("Customer PO", _crm(_file(st, "so_file"))))
        if _file(st, "payment_invoice") and _ok(st, "payment_l1"):
            files.append(_f("Invoice", _crm(_file(st, "payment_invoice"))))
        for key, label in (("doc_sales_invoice", "Sales Invoice"), ("doc_purchase_order", "Purchase Order"),
                           ("doc_eway_bill", "E-way Bill"), ("doc_quote", "Quote (uploaded)")):
            if _file(st, key):
                files.append(_f(label, _crm(_file(st, key))))
        if getattr(o, "product_records_file", None):
            files.append(_f("Product Records", _crm(o.product_records_file), False))
        out[str(o.id)] = files
    return out
