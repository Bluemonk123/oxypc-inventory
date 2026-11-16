"""Accounts (/accounts) — Option C dashboard (2026-11-14, Pankaj). No View Data:
the page's six Pending / Completed cards stay at the bottom of the dashboard.

Money in  = CustomerReceipt (receipt_date). Money out = SupplierPayment (payment_date).
Receivable = open Sales Orders (CRMSalesOrder, not cancelled) less the receipts
             recorded against them (CustomerReceipt.so_id); age from so_date.
Payable    = Purchase Orders (CRMPurchaseOrder, not cancelled, issued or verified by
             Accounts, payment
             not marked paid) less SupplierPayment against them (po_id).
Due date of a PO = po_date + the first number of days in its payment terms
("30 days" -> +30); without one, its expected delivery date, else po_date.

Rows: KPIs · cash in and out + collection efficiency · Ageing / Top debtors /
Payment due (next 8 weeks, View all) · Payment made / Payment received, last
7 days. Customer money needs Finance Customer Pricing, supplier money needs
Finance Supplier Pricing; without it the amounts show as "—".
"""
import re
from datetime import date, datetime, timedelta

from sqlalchemy import select, func, desc

from models.crm import CRMSalesOrder, CRMPurchaseOrder, CustomerReceipt, SupplierPayment, CRMContact
from services import dash_widgets as dw
from utils.timezone import app_now

BANDS = ["Not due", "0-15d", "16-30d", "31-60d", "60d+"]


def _days(terms):
    m = re.search(r"(\d{1,3})\s*(?:days?|d\b)", (terms or "").lower())
    return int(m.group(1)) if m else None


def _band(age, due_in=None):
    if due_in is not None and due_in > 0:
        return 0
    return 1 if age <= 15 else 2 if age <= 30 else 3 if age <= 60 else 4


async def build(db, pv_cus, pv_sup):
    now = app_now()
    today = now.date()
    month0 = today.replace(day=1)
    cm = (lambda v: dw.inr(v)) if pv_cus else (lambda v: "—")
    sm = (lambda v: dw.inr(v)) if pv_sup else (lambda v: "—")

    # ── Receivables: open sales orders less receipts against them ──
    rec_by_so = dict((await db.execute(select(CustomerReceipt.so_id, func.sum(CustomerReceipt.amount))
                                       .where(CustomerReceipt.so_id.isnot(None)).group_by(CustomerReceipt.so_id))).all())
    sos = (await db.execute(select(CRMSalesOrder.id, CRMSalesOrder.so_number, CRMSalesOrder.so_date, CRMSalesOrder.total_amount,
                                   CRMSalesOrder.payment_terms, CRMContact.company_name)
                            .outerjoin(CRMContact, CRMContact.id == CRMSalesOrder.contact_id)
                            .where(CRMSalesOrder.status != "cancelled"))).all()
    recv = []
    for sid, no, d, tot, terms, acct in sos:
        bal = float(tot or 0) - float(rec_by_so.get(sid) or 0)
        if bal > 0.5:
            age = (today - d).days if d else 0
            n = _days(terms)
            recv.append(dict(no=no, acct=acct or "—", bal=bal, age=age, band=_band(age, (n - age) if n is not None else None)))
    # ── Payables: POs less supplier payments against them ──
    paid_by_po = dict((await db.execute(select(SupplierPayment.po_id, func.sum(SupplierPayment.amount))
                                        .where(SupplierPayment.po_id.isnot(None)).group_by(SupplierPayment.po_id))).all())
    pos = (await db.execute(select(CRMPurchaseOrder.id, CRMPurchaseOrder.po_number, CRMPurchaseOrder.po_date,
                                   CRMPurchaseOrder.expected_delivery_date, CRMPurchaseOrder.payment_terms,
                                   func.coalesce(CRMPurchaseOrder.total_amount, CRMPurchaseOrder.offer_total, 0), CRMContact.company_name)
                            .outerjoin(CRMContact, CRMContact.id == CRMPurchaseOrder.contact_id)
                            .where(CRMPurchaseOrder.status != "cancelled",
                                   (CRMPurchaseOrder.status != "draft") | (CRMPurchaseOrder.verified.is_(True)),
                                   func.coalesce(CRMPurchaseOrder.payment_status, "pending") != "paid"))).all()
    pay = []
    for pid, no, d, exp, terms, tot, acct in pos:
        paid = float(paid_by_po.get(pid) or 0)
        bal = float(tot or 0) - paid
        if bal > 0.5:
            n = _days(terms)
            due = (d + timedelta(days=n)) if (d and n is not None) else (exp or d or today)
            age = (today - d).days if d else 0
            pay.append(dict(no=no, acct=acct or "—", tot=float(tot or 0), paid=paid, bal=bal, due=due, age=age,
                            band=_band((today - due).days, (due - today).days)))
    pay.sort(key=lambda x: x["due"])

    receivable = sum(x["bal"] for x in recv)
    payable = sum(x["bal"] for x in pay)
    over30 = [x for x in recv if x["age"] > 30]
    coll_mtd = float((await db.execute(select(func.coalesce(func.sum(CustomerReceipt.amount), 0))
                                       .where(CustomerReceipt.receipt_date >= month0))).scalar() or 0)
    paid_mtd = float((await db.execute(select(func.coalesce(func.sum(SupplierPayment.amount), 0))
                                       .where(SupplierPayment.payment_date >= month0))).scalar() or 0)
    # Days to collect: receipts in the last 90 days against a sales order, days after the SO date.
    dts = (await db.execute(select(CustomerReceipt.receipt_date, CRMSalesOrder.so_date)
                            .join(CRMSalesOrder, CRMSalesOrder.id == CustomerReceipt.so_id)
                            .where(CustomerReceipt.receipt_date >= today - timedelta(days=90)))).all()
    gaps = [(r - s).days for r, s in dts if r and s and r >= s]
    dso = sum(gaps) / len(gaps) if gaps else None
    due7 = [x for x in pay if x["due"] <= today + timedelta(days=7)]
    kpis = [dw.kpi("Receivables", cm(receivable), "%d open sales orders" % len(recv), True, "blue"),
            dw.kpi("Payables", sm(payable), "%d unpaid POs" % len(pay), True, "violet"),
            dw.kpi("Collected MTD", cm(coll_mtd), "from customers", True, "green"),
            dw.kpi("Overdue > 30 days", cm(sum(x["bal"] for x in over30)), "%d sales orders" % len(over30), not over30, "red"),
            dw.kpi("Paid out MTD", sm(paid_mtd), "to suppliers", True, "teal"),
            dw.kpi("Payments due 7 days", sm(sum(x["bal"] for x in due7)), ("%d POs" % len(due7)) + (" · days to collect %d" % round(dso) if dso is not None else ""),
                   not due7, "amber")]

    # ── Cash in and out, 14 days ──
    d14 = today - timedelta(days=13)
    din = dict((await db.execute(select(CustomerReceipt.receipt_date, func.sum(CustomerReceipt.amount))
                                 .where(CustomerReceipt.receipt_date >= d14).group_by(CustomerReceipt.receipt_date))).all())
    dout = dict((await db.execute(select(SupplierPayment.payment_date, func.sum(SupplierPayment.amount))
                                  .where(SupplierPayment.payment_date >= d14).group_by(SupplierPayment.payment_date))).all())
    days = [d14 + timedelta(days=i) for i in range(14)]
    series = []
    if pv_cus:
        series.append(("Money in", [float(din.get(d) or 0) for d in days], "green"))
    if pv_sup:
        series.append(("Money out", [float(dout.get(d) or 0) for d in days], "red"))
    if series and any(any(s[1]) for s in series):
        cash = dw.area("Cash in and out", 8, [d.strftime("%d %b") for d in days], series, kind="inr", legv="sum",
                       sub="Customer receipts and supplier payments per day, last 14 days")
    else:
        cash = dw.insight("Cash in and out", 8, [("i", "No receipts or payments in the last 14 days." if series
                                                         else "Amounts need the Finance Pricing tick.", "slate")])
    # Collection efficiency: sales orders of the last 90 days, share already received.
    d90 = today - timedelta(days=90)
    so90 = [(sid, float(tot or 0), d) for sid, no, d, tot, terms, acct in sos if d and d >= d90]
    billed = sum(t for _, t, _ in so90)
    got = sum(min(t, float(rec_by_so.get(sid) or 0)) for sid, t, _ in so90)
    pct = got * 100 / billed if billed else 0
    gauge = dw.insight("Collection efficiency", 4, [("i", "No sales orders in the last 90 days." if pv_cus else
                                                     "Amounts need the Finance Customer Pricing tick.", "slate")]) if not (billed and pv_cus) else dw.gauge("Collection efficiency", 4, pct, ("%d%%" % round(pct)) if billed and pv_cus else "—",
                     "of sales orders (90 days) collected", "green",
                     [("Billed", cm(billed)), ("Collected", cm(got)), ("Days to collect", ("%d" % round(dso)) if dso is not None else "—")])

    # ── Ageing / Top debtors / Payment due ──
    rb = [sum(x["bal"] for x in recv if x["band"] == i) for i in range(5)]
    pb = [sum(x["bal"] for x in pay if x["band"] == i) for i in range(5)]
    ser = ([("Receivable", rb, "blue")] if pv_cus else []) + ([("Payable", pb, "violet")] if pv_sup else [])
    ageing = (dw.bars("Ageing", 4, BANDS, ser, kind="inr", bmax="26px", sub="Open balances by days past due")
              if ser and any(any(s[1]) for s in ser)
              else dw.insight("Ageing", 4, [("✓" if ser else "i", "Nothing outstanding." if ser else "Amounts need the Finance Pricing tick.",
                                             "green" if ser else "slate")]))
    deb = {}
    for x in recv:
        e = deb.setdefault(x["acct"], [0, 0, 0])
        e[0] += x["bal"]; e[1] += x["bal"] if x["age"] > 30 else 0; e[2] = max(e[2], x["age"])
    top = sorted(deb.items(), key=lambda kv: -kv[1][0])[:6]
    debtors = dw.insight("Top debtors", 4, [("✓", "No money owed on open sales orders.", "green")],
                         sub="Open sales orders less receipts") if not top else dw.table("Top debtors", 4, [("Customer", 1.5), ("Due", 0.9), ("Overdue", 0.9), ("Oldest", 0.6)],
                       [[a[:22], cm(v[0]), cm(v[1]) if v[1] else "₹0" if pv_cus else "—",
                         ("%dd" % v[2], "red" if v[2] > 30 else "amber" if v[2] > 15 else "green")] for a, v in top]
                       , sub="Open sales orders less receipts")
    wk0 = today - timedelta(days=today.weekday())
    end = wk0 + timedelta(weeks=8)
    by_day = {}
    for x in pay:
        k = max(x["due"], today) if x["due"] < end else None   # overdue bills sit on today
        if k:
            by_day.setdefault(k, []).append(x)
    cal = dw.cal("Payment due", 4, 8, 0, "violet", "Supplier payments due per day, next 8 weeks")
    mx = max([sum(i["bal"] for i in v) for v in by_day.values()] or [0]) or 1
    cells = []
    for i in range(56):
        d = wk0 + timedelta(days=(i // 7) * 7 + (i % 7))
        items = by_day.get(d, [])
        v = sum(it["bal"] for it in items)
        tip = "%s: %d bill%s%s" % (d.strftime("%d %b"), len(items), "" if len(items) == 1 else "s", (" · " + sm(v)) if pv_sup and items else "")
        cells.append({"bg": "var(--dk-track)" if d < today else dw.tint(dw.C["violet"], 0.08 + 0.85 * (v / mx)), "tip": tip})
    cal["cells"] = cells
    cal = dw.withbtn(cal, "View all", modal="#accDuesModal")
    cal["sub"] = "%d unpaid POs · %s" % (len(pay), sm(payable)) if pay else "No unpaid POs"

    # ── Payment made / received, last 7 days ──
    d7 = today - timedelta(days=6)
    made = (await db.execute(select(SupplierPayment.payment_date, CRMContact.company_name, SupplierPayment.amount,
                                    SupplierPayment.payment_mode, SupplierPayment.verified)
                             .outerjoin(CRMContact, CRMContact.id == SupplierPayment.contact_id)
                             .where(SupplierPayment.payment_date >= d7)
                             .order_by(desc(SupplierPayment.payment_date), desc(SupplierPayment.created_at)).limit(8))).all()
    got7 = (await db.execute(select(CustomerReceipt.receipt_date, CRMContact.company_name, CustomerReceipt.amount,
                                    CustomerReceipt.payment_mode, CustomerReceipt.verified)
                             .outerjoin(CRMContact, CRMContact.id == CustomerReceipt.contact_id)
                             .where(CustomerReceipt.receipt_date >= d7)
                             .order_by(desc(CustomerReceipt.receipt_date), desc(CustomerReceipt.created_at)).limit(8))).all()
    cols = [("Date", 0.7), ("Party", 1.5), ("Amount", 0.9), ("Mode", 0.6), ("Status", 0.8)]

    def rows(rs, money):
        return [[d.strftime("%d %b") if d else "—", (p or "—")[:24], money(float(a or 0)), (m or "—").upper(),
                 ("Verified", "green") if v else ("Pending", "amber")] for d, p, a, m, v in rs]
    made_t = (dw.table("Payment made", 6, cols, rows(made, sm), sub="Supplier payments, last 7 days", href="/accounts/supplier-payments")
              if made else dw.insight("Payment made", 6, [("i", "No supplier payments in the last 7 days.", "slate")],
                                      sub="Supplier payments, last 7 days", href="/accounts/supplier-payments"))
    got_t = (dw.table("Payment received", 6, cols, rows(got7, cm), sub="Customer receipts, last 7 days", href="/accounts/customer-receipts")
             if got7 else dw.insight("Payment received", 6, [("i", "No customer receipts in the last 7 days.", "slate")],
                                     sub="Customer receipts, last 7 days", href="/accounts/customer-receipts"))

    widgets = dw.layout([(340, [cash, gauge]), (380, [ageing, debtors, cal]), (340 if (made or got7) else 200, [made_t, got_t])])
    return kpis, widgets, pay
