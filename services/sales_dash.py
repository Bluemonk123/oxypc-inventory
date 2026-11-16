"""Sales (/sales) — Option C dashboard (2026-11-14, Pankaj).

Built from Sale joined to Device and Lot, with the page's own filters (Sale #,
customer, grade, lot, sales person, stock / sale type) applied. The date range
sets the KPI period; without one the period is this month. The per-day chart
always compares this month with last month.

Margin = (sale price - Device.device_price) over paid sales (price > 0) whose
tag has a buying price. Returns = Return rows dated in the period. Money needs the Sales List
Pricing tick; without it the money cards show counts.
"""
from datetime import datetime, timedelta

from sqlalchemy import select, func, desc

from models.device import Device
from models.lot import Lot
from models.sales import Sale, Return
from services import dash_widgets as dw
from utils.timezone import app_now

CH = {"procurement": ("Procurement", "blue"), "telecaller": ("Telesales", "indigo"), "showroom": ("Showroom", "cyan")}


def _cat(sub, dtype):
    return (sub or dtype or "Other").strip().title()[:14] or "Other"


async def build(db, filters, d_from=None, d_to=None, can_price=True):
    now = app_now()
    month0 = datetime(now.year, now.month, 1)
    p0 = d_from or month0
    p1 = (d_to.replace(hour=23, minute=59, second=59) if d_to else now)
    plabel = ("%s – %s" % (p0.strftime("%d %b"), p1.strftime("%d %b"))) if (d_from or d_to) else "this month"
    last0 = (month0 - timedelta(days=1)).replace(day=1)
    since = min(p0, last0)
    base = (select(Sale.sold_at, Sale.sale_price, Sale.customer_name, Sale.customer_state, Sale.sale_channel,
                   Sale.sales_person, Sale.sales_person_username, Sale.sale_number, Sale.invoice_no, Sale.payment_mode,
                   Device.device_price, Device.sub_category, Device.device_type)
            .join(Device, Sale.device_id == Device.id).join(Lot, Device.lot_id == Lot.id)
            .where(*filters, Sale.sold_at >= since, Sale.sold_at <= max(p1, now)))
    rows = (await db.execute(base)).all()
    per = [r for r in rows if r[0] and p0 <= r[0] <= p1]
    rev = sum(float(r[1] or 0) for r in per)
    units = len(per)
    with_cost = [(float(r[1] or 0), float(r[10])) for r in per if r[10] and float(r[1] or 0) > 0]   # ₹0 sales (replaced) left out
    mrev = sum(a for a, _ in with_cost)
    margin = (mrev - sum(b for _, b in with_cost)) * 100 / mrev if mrev else None
    custs = {(r[2] or "").strip().lower() for r in per if r[2]}
    rets = (await db.execute(select(func.count(Return.id)).where(Return.return_date >= p0, Return.return_date <= p1))).scalar() or 0
    money = dw.inr if can_price else (lambda v: "—")
    kpis = [dw.kpi("Revenue", money(rev), plabel, True, "blue", style="spark"),
            dw.kpi("Units sold", dw.num(units), plabel, True, "indigo", style="spark"),
            dw.kpi("Avg selling price", money(rev / units) if units else "—", "per tag", True, "cyan", style="spark"),
            dw.kpi("Gross margin", ("%.1f%%" % margin) if margin is not None and can_price else "—",
                   "%s paid tags with a buy price" % dw.num(len(with_cost)), (margin or 0) >= 10, "amber", style="spark"),
            dw.kpi("Customers", dw.num(len(custs)), plabel, True, "green", style="spark"),
            dw.kpi("Returns", ("%.1f%%" % (rets * 100 / units)) if units else dw.num(rets), "%s returns" % dw.num(rets),
                   not rets or (units and rets * 100 / units < 3), "violet", style="spark")]

    # This month vs last month, per day of month.
    nd = now.day
    cur = [0.0] * nd
    prev = [0.0] * nd
    for r in rows:
        at = r[0]
        if not at or at.day > nd:
            continue
        v = float(r[1] or 0) if can_price else 1
        if at >= month0:
            cur[at.day - 1] += v
        elif last0 <= at < month0:
            prev[at.day - 1] += v
    labels = ["%d %s" % (i + 1, now.strftime("%b")) for i in range(nd)]
    trend = (dw.area("This month vs last", 8, labels, [("This month", cur, "blue"), ("Last month", prev, "slate")],
                     kind="inr" if can_price else "n", dashed=True, legv="sum",
                     sub=("Revenue" if can_price else "Tags sold") + " per day, same days of last month")
             if any(cur) or any(prev) else dw.insight("This month vs last", 8, [("i", "No sales this month or last month.", "slate")]))
    # Channel when sales carry one, else how customers paid.
    use_ch = any((r[4] or "").strip() for r in per)
    pal = ["blue", "indigo", "cyan", "teal", "amber", "violet"]
    chans = {}
    for r in per:
        k = (r[4] or "").lower() if use_ch else ((r[9] or "").strip() or "Not set")
        chans[k] = chans.get(k, 0) + (float(r[1] or 0) if can_price else 1)
    ordered = sorted(chans.items(), key=lambda kv: -kv[1])
    if use_ch:
        parts = [(CH.get(k, ("Other", "grey"))[0], v, CH.get(k, ("Other", "grey"))[1]) for k, v in ordered if v]
    else:
        parts = [(k[:16], v, pal[i]) for i, (k, v) in enumerate(ordered[:5]) if v]
        rest = sum(v for _, v in ordered[5:])
        if rest:
            parts.append(("Other", rest, "grey"))
    dtitle = "Sales channel" if use_ch else "Payment mode"
    donut = (dw.donut(dtitle, 4, parts, money(rev) if can_price else dw.num(units), plabel, kind="inr" if can_price else "n")
             if parts else dw.insight(dtitle, 4, [("i", "No sales in %s." % plabel, "slate")]))

    def agg(key):
        out = {}
        for r in per:
            k = key(r)
            if k:
                out[k] = out.get(k, 0) + (float(r[1] or 0) if can_price else 1)
        return sorted(out.items(), key=lambda kv: -kv[1])
    kind = "inr" if can_price else "n"
    tc = agg(lambda r: (r[2] or "").strip()[:20] or None)[:6]
    top_c = (dw.hbars("Top customers", 4, tc, kind=kind, color="blue", lw="120px", sub=plabel.capitalize())
             if tc else dw.insight("Top customers", 4, [("i", "No sales in %s." % plabel, "slate")]))
    cats = agg(lambda r: _cat(r[11], r[12]))[:6]
    by_cat = (dw.bars("By category", 4, [c for c, _ in cats], [("Revenue" if can_price else "Tags", [v for _, v in cats], "indigo")],
                      kind=kind, bmax="40px", legv="", sub=plabel.capitalize())
              if cats else dw.insight("By category", 4, [("i", "No sales in %s." % plabel, "slate")]))
    sp = agg(lambda r: (r[5] or r[6] or "").strip()[:16] or None)[:6]
    by_sp = (dw.hbars("By sales person", 4, sp, kind=kind, color="cyan", lw="110px", sub=plabel.capitalize())
             if sp else dw.insight("By sales person", 4, [("i", "No sales person on sales in %s." % plabel, "slate")]))
    states = [s for s, _ in agg(lambda r: (r[3] or "").strip().title()[:14] or None)[:6]]
    cnames = [c for c, _ in cats][:5]
    grid = {}
    for r in per:
        k = ((r[3] or "").strip().title()[:14], _cat(r[11], r[12]))
        grid[k] = grid.get(k, 0) + 1
    heat = (dw.heat("State × category", 7, cnames, [(s, [grid.get((s, c), 0) for c in cnames]) for s in states], color="blue", lw="90px",
                    sub="Tags sold, %s" % plabel)
            if states and cnames else dw.insight("State × category", 7, [("i", "No customer state on sales in %s." % plabel, "slate")]))
    latest = sorted(per, key=lambda r: r[0], reverse=True)[:7]
    tone = lambda m: ("Credit note", "violet") if m == "Credit Note" else ((m or "—")[:12], "green" if m else "slate")   # noqa: E731
    inv = dw.table("Latest sales", 5, [("Invoice", 1), ("Customer", 1.4), ("Value", 0.8), ("Paid by", 0.8)],
                   [[(r[8] or "").strip() or r[7], (r[2] or "—")[:22], money(float(r[1] or 0)), tone(r[9])] for r in latest]
                   or [["No sales", "", "", ""]], sub="Newest first")
    widgets = dw.layout([(360, [trend, donut]), (380, [top_c, by_cat, by_sp]), (400, [heat, inv])])
    return kpis, widgets
