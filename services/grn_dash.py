"""GRN (/grn/post-iqc) — Option C dashboard (2026-11-14, Pankaj).

Received vs invoiced, match rate, GRN Received table (one GRN can hold
several lots — the Lots count opens them: /grn/lots-of), Deals awaiting GRN
(won supplier deals with no GRN yet), an 8-week receipts calendar and
mismatches by supplier. Live GRNs only (source != Trade Partner test).

Received quantity: GRNImport.received_qty (validation), else physical_quantity.
"""
from datetime import datetime, timedelta

from sqlalchemy import select, func, desc, or_

from models.crm import CRMSourcingDeal, CRMContact
from models.grn_import import GRNImport, GRNLot
from services import dash_widgets as dw
from utils.timezone import app_now

RECV = func.coalesce(GRNImport.received_qty, GRNImport.physical_quantity)


def _live():
    from models.master import EXTERNAL_PARTNER_TEST_GRN_SOURCE
    return [GRNImport.is_deleted.isnot(True),
            or_(GRNImport.source.is_(None), GRNImport.source != EXTERNAL_PARTNER_TEST_GRN_SOURCE)]


async def build(db, can_price=True):
    """can_price: supplier deal values need GRN post IQC Pricing View."""
    now = app_now()
    today = now.date()
    month_start = datetime(now.year, now.month, 1)
    live = _live()
    m = (await db.execute(select(func.count(GRNImport.id), func.coalesce(func.sum(GRNImport.quantity), 0),
                                 func.coalesce(func.sum(RECV), 0),
                                 func.coalesce(func.sum(func.greatest(GRNImport.quantity - RECV, 0)), 0),
                                 func.coalesce(func.sum(func.greatest(RECV - GRNImport.quantity, 0)), 0))
                          .where(*live, GRNImport.grn_date >= month_start.date()))).one()
    n_grn, inv_q, rec_q, short, excess = [int(x or 0) for x in m]
    matched = min(rec_q, inv_q) / inv_q * 100 if inv_q else 0
    has_grn = select(GRNLot.lot_id).where(GRNLot.lot_id.isnot(None))
    awaiting = (await db.execute(
        select(CRMSourcingDeal, CRMContact.company_name)
        .outerjoin(CRMContact, CRMContact.id == CRMSourcingDeal.contact_id)
        .where(CRMSourcingDeal.stage.in_(["won", "received", "payment", "purchase_order"]),
               or_(CRMSourcingDeal.linked_lot_id.is_(None), CRMSourcingDeal.linked_lot_id.notin_(has_grn)))
        .order_by(desc(CRMSourcingDeal.updated_at)).limit(50))).all()

    kpis = [dw.kpi("GRNs this month", dw.num(n_grn), now.strftime("%B"), True, "teal"),
            dw.kpi("Devices received", dw.num(rec_q), "this month", True, "cyan"),
            dw.kpi("Matched to invoice", ("%.1f%%" % matched) if inv_q else "—", "%s invoiced" % dw.num(inv_q), True, "green"),
            dw.kpi("Short", dw.num(short), "this month", short == 0, "red"),
            dw.kpi("Excess", dw.num(excess), "this month", excess == 0, "amber"),
            dw.kpi("Deals awaiting GRN", dw.num(len(awaiting)), "won, not received", not awaiting, "slate")]

    days = [today - timedelta(days=13 - i) for i in range(14)]
    per_day = {d: (int(q or 0), int(r or 0)) for d, q, r in (await db.execute(
        select(GRNImport.grn_date, func.sum(GRNImport.quantity), func.sum(RECV))
        .where(*live, GRNImport.grn_date >= days[0]).group_by(GRNImport.grn_date))).all()}
    if any(per_day.values()):
        area = dw.area("Received vs invoiced", 8, [d.strftime("%d %b") for d in days],
                       [("Invoiced qty", [per_day.get(d, (0, 0))[0] for d in days], "slate"),
                        ("Received qty", [per_day.get(d, (0, 0))[1] for d in days], "teal")], legv="sum", sub="Devices per day, last 14 days")
    else:
        area = dw.insight("Received vs invoiced", 8, [("i", "No GRNs in the last 14 days.", "slate")])
    gauge = dw.gauge("Match rate", 4, matched, ("%.1f%%" % matched) if inv_q else "—", "this month", "teal",
                     [("Short", dw.num(short)), ("Excess", dw.num(excess)), ("GRNs", dw.num(n_grn))])

    lots_n = select(GRNLot.grn_id, func.count(GRNLot.id).label("n")).group_by(GRNLot.grn_id).subquery()
    grns = (await db.execute(select(GRNImport, lots_n.c.n).outerjoin(lots_n, lots_n.c.grn_id == GRNImport.id)
                             .where(*live).order_by(desc(GRNImport.created_at).nulls_last(), desc(GRNImport.grn_date).nulls_last()).limit(7))).all()
    rows = []
    for g, n in grns:
        q, r = g.quantity or 0, (g.received_qty if g.received_qty is not None else g.physical_quantity)
        if r is None:
            st = ("Pending", "slate")
        elif r < q:
            st = ("%d short" % (q - r), "red")
        elif r > q:
            st = ("%d excess" % (r - q), "amber")
        else:
            st = ("Matched", "green")
        rows.append([g.grn_number or "—", g.invoice_number or "—", g.grn_date.strftime("%Y-%m-%d") if g.grn_date else "—",
                     (g.invoice_date.strftime("%Y-%m-%d") if hasattr(g.invoice_date, "strftime") else str(g.invoice_date)[:10]) if g.invoice_date else "—", g.sender_name or "—",
                     {"x": str(n or 0), "kind": "link", "modal": "#grnLotsModal", "data": {"grn": str(g.id), "no": g.grn_number or ""}},
                     dw.num(q), dw.num(r) if r is not None else "—", st, g.created_by or "—"])
    grn_table = dw.table("GRN received", 12, [("GRN No", 0.8), ("Invoice No", 0.9), ("GRN date", 0.8), ("Invoice date", 0.8),
                                              ("Supplier", 1.4), ("Lots", 0.45), ("Invoiced", 0.6), ("Received", 0.6),
                                              ("Status", 0.8), ("Added by", 0.8)],
                         rows or [["No GRNs yet"] + [""] * 9], sub="Newest first · one GRN can cover several lots — click the count")

    deal_rows = []
    for d, contact in awaiting[:6]:
        name = d.title or (contact if isinstance(contact, str) else "") or d.deal_number or "—"
        st = {"won": ("Won", "green"), "received": ("Received", "blue"), "payment": ("Payment", "amber"),
              "purchase_order": ("PO sent", "slate")}.get(d.stage, (d.stage or "—", "slate"))
        val = d.final_price_total or d.our_offer_total
        deal_rows.append([d.deal_number or "—", name, dw.num(d.est_quantity or 0), (dw.inr(float(val)) if val and can_price else "—"), st])
    deals = dw.table("Deals awaiting GRN", 5, [("Deal", 0.8), ("Title", 1.5), ("Units", 0.5), ("Value", 0.7), ("Stage", 0.7)],
                     deal_rows or [["—", "No won deals waiting", "", "", ""]], sub="Won supplier deals with no GRN yet",
                     tag="%d deals" % len(awaiting), tagc="slate", href="/crm/sourcing")

    cal_start = today - timedelta(days=today.weekday()) - timedelta(weeks=7)
    by_day = dict((await db.execute(select(GRNImport.grn_date, func.sum(RECV)).where(*live, GRNImport.grn_date >= cal_start)
                                    .group_by(GRNImport.grn_date))).all())
    cal = dw.cal("Receipts calendar", 4, 8, 0, "teal", "Devices received per day, last 8 weeks")
    mx = max([float(v or 0) for v in by_day.values()] or [0]) or 1
    cells = []
    for i in range(56):
        d = cal_start + timedelta(days=(i // 7) * 7 + (i % 7))
        v = float(by_day.get(d) or 0)
        cells.append({"bg": "var(--dk-track)" if d > today else dw.tint(dw.C["teal"], 0.08 + 0.85 * (v / mx))})
    cal["cells"] = cells
    mism = (await db.execute(select(GRNImport.sender_name, func.sum(func.abs(GRNImport.quantity - RECV)))
                             .where(*live, RECV.isnot(None), GRNImport.quantity != RECV, GRNImport.grn_date >= today - timedelta(days=90))
                             .group_by(GRNImport.sender_name).order_by(desc(func.sum(func.abs(GRNImport.quantity - RECV)))).limit(5))).all()
    mism_w = (dw.hbars("Mismatches by supplier", 3, [((s or "—")[:18], int(v or 0)) for s, v in mism], color="red", lw="80px",
                       sub="Short + excess, 90 days") if mism else dw.insight("Mismatches by supplier", 3, [("✓", "No mismatches in 90 days.", "green")]))
    widgets = dw.layout([(330, [area, gauge]), (370, [grn_table]), (400, [deals, cal, mism_w])])
    return kpis, widgets
