"""Part Sales (/part-sales) — Option C dashboard (2026-11-14, Pankaj).

Spare parts sold (PartSale), by part category (SparePart.category), customer
and month. The page's date range sets the KPI period; without one it is this
month (last 90 days, then 12 months, when the month has no part sales). Margin = PartSale.margin over sales with a stock cost (stock_unit_price).
Category = SparePart.category, else the type in the part name ("Laptop-Adapter"). Money needs the Part
Sales Pricing tick; without it the money cards show units.
"""
from datetime import datetime, timedelta

from sqlalchemy import select

from models.part_sales import PartSale
from models.spare_parts import SparePart
from services import dash_widgets as dw
from utils.timezone import app_now

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
def _cat(r):
    """Part category: the spare part's category, else the part name's type (\"Laptop-Adapter\" -> Adapter)."""
    if r[8] and r[8].strip().lower() not in ("other", "others", "misc"):
        return r[8].strip().title()[:16]
    n = (r[4] or "").strip()
    return (n.split("-")[-1].strip() or n or "Other").title()[:16]


PAL = ["orange", "amber", "blue", "violet", "teal", "pink"]


async def build(db, d_from=None, d_to=None, can_price=True):
    now = app_now()
    month0 = datetime(now.year, now.month, 1)
    p0 = d_from or month0
    p1 = d_to.replace(hour=23, minute=59, second=59) if d_to else now
    plabel = ("%s – %s" % (p0.strftime("%d %b"), p1.strftime("%d %b"))) if (d_from or d_to) else "this month"
    y0 = datetime(now.year - (1 if now.month < 12 else 0), now.month % 12 + 1, 1)
    rows = (await db.execute(select(PartSale.sold_at, PartSale.qty, PartSale.total_sale_price, PartSale.margin,
                                    PartSale.part_name, PartSale.customer_name, PartSale.sale_number, PartSale.invoice_no,
                                    SparePart.category, PartSale.stock_unit_price)
                             .outerjoin(SparePart, SparePart.id == PartSale.part_id)
                             .where(PartSale.sold_at >= min(p0, y0)))).all()
    per = [r for r in rows if r[0] and p0 <= r[0] <= p1]
    if not per and not (d_from or d_to):      # quiet month: widen to 90 days, then 12 months
        for days, lbl in ((90, "last 90 days"), (365, "last 12 months")):
            p0, plabel = now - timedelta(days=days), lbl
            per = [r for r in rows if r[0] and r[0] >= p0]
            if per:
                break
    rev = sum(float(r[2] or 0) for r in per)
    units = sum(int(r[1] or 0) for r in per)
    orders = len(per)
    mg = [(float(r[2] or 0), float(r[3])) for r in per if r[3] is not None and r[9]]   # only sales with a cost basis
    mpct = sum(m for _, m in mg) * 100 / sum(v for v, _ in mg) if mg and sum(v for v, _ in mg) else None
    money = dw.inr if can_price else (lambda v: "—")
    val = (lambda r: float(r[2] or 0)) if can_price else (lambda r: int(r[1] or 0))
    kind = "inr" if can_price else "n"

    def agg(key, rs=per):
        out = {}
        for r in rs:
            k = key(r)
            if k:
                out[k] = out.get(k, 0) + val(r)
        return sorted(out.items(), key=lambda kv: -kv[1])
    cats = agg(_cat)
    custs = {(r[5] or "").strip().lower() for r in per if r[5]}
    rep = {}
    for r in rows:
        if r[5]:
            rep[(r[5] or "").strip().lower()] = rep.get((r[5] or "").strip().lower(), 0) + 1
    repeat = sum(1 for c in custs if rep.get(c, 0) > 1)
    kpis = [dw.kpi("Part revenue", money(rev), plabel, True, "orange"),
            dw.kpi("Orders", dw.num(orders), "%s units" % dw.num(units), True, "amber"),
            dw.kpi("Margin", ("%d%%" % round(mpct)) if mpct is not None and can_price else "—", plabel, (mpct or 0) >= 20, "green"),
            dw.kpi("Avg order", money(rev / orders) if orders else "—", "per sale", True, "blue"),
            dw.kpi("Top category", cats[0][0] if cats else "—", money(cats[0][1]) if cats and can_price else "", True, "violet"),
            dw.kpi("Repeat buyers", ("%d%%" % round(repeat * 100 / len(custs))) if custs else "—", "of %s customers" % dw.num(len(custs)), True, "teal")]

    months = []
    y, m = y0.year, y0.month
    for _ in range(12):
        months.append((y, m)); m += 1
        if m > 12:
            y, m = y + 1, 1
    rv = {k: 0.0 for k in months}
    mv = {k: 0.0 for k in months}
    for r in rows:
        if r[0]:
            k = (r[0].year, r[0].month)
            if k in rv:
                rv[k] += val(r); mv[k] += float(r[3] or 0) if r[9] else 0
    series = [("Revenue" if can_price else "Units", [rv[k] for k in months], "orange")]
    if can_price:
        series.append(("Margin", [mv[k] for k in months], "green"))
    trend = (dw.area("Revenue and margin" if can_price else "Units sold", 8, [MONTHS[mm - 1] for _, mm in months], series, kind=kind, legv="sum",
                     sub="Last 12 months")
             if any(rv.values()) else dw.insight("Revenue and margin", 8, [("i", "No part sales in the last 12 months.", "slate")]))
    dparts = [(c, v, PAL[i]) for i, (c, v) in enumerate(cats[:5])]
    if len(cats) > 5:
        dparts.append(("Other", sum(v for _, v in cats[5:]), "grey"))
    donut = (dw.donut("By category", 4, dparts, money(rev) if can_price else dw.num(units), plabel, kind=kind)
             if dparts else dw.insight("By category", 4, [("i", "No part sales in %s." % plabel, "slate")]))
    best = {}
    for r in per:
        best[(r[4] or "—")[:22]] = best.get((r[4] or "—")[:22], 0) + int(r[1] or 0)
    best = sorted(best.items(), key=lambda kv: -kv[1])[:6]
    best_w = (dw.hbars("Best-selling parts", 6, best, color="orange", lw="140px", sub="Units, %s" % plabel)
              if best else dw.insight("Best-selling parts", 6, [("i", "No part sales in %s." % plabel, "slate")]))
    latest = sorted([r for r in rows if r[0]], key=lambda r: r[0], reverse=True)[:7]
    recent = dw.table("Recent sales", 6, [("Sale", 0.9), ("Customer", 1.4), ("Part", 1.3), ("Qty", 0.4), ("Value", 0.7)],
                      [[(r[7] or "").strip() or r[6], (r[5] or "—")[:20], (r[4] or "—")[:22], dw.num(r[1] or 0), money(float(r[2] or 0))]
                       for r in latest] or [["No part sales", "", "", "", ""]], sub="Newest first")
    # Margin % by category (period).
    cm = {}
    for r in per:
        if r[3] is not None and r[9] and float(r[2] or 0):
            k = _cat(r)
            a = cm.setdefault(k, [0.0, 0.0]); a[0] += float(r[3]); a[1] += float(r[2] or 0)
    cml = sorted(cm.items(), key=lambda kv: -kv[1][1])[:6]
    mbars = (dw.bars("Margin by category", 6, [k for k, _ in cml], [("Margin %", [round(a[0] * 100 / a[1], 1) for _, a in cml], "green")],
                     kind="pct", bmax="40px", legv="", sub=plabel.capitalize())
             if cml and can_price else dw.insight("Margin by category", 6, [("i", "No margin recorded in %s." % plabel if can_price
                                                                              else "Margin needs the Pricing tick.", "slate")]))
    tc = agg(lambda r: (r[5] or "").strip()[:20] or None)[:6]
    top_c = (dw.hbars("Top customers", 6, tc, kind=kind, color="amber", lw="140px", sub=plabel.capitalize())
             if tc else dw.insight("Top customers", 6, [("i", "No part sales in %s." % plabel, "slate")]))
    widgets = dw.layout([(360, [trend, donut]), (380, [best_w, recent]), (380, [mbars, top_c])])
    return kpis, widgets
