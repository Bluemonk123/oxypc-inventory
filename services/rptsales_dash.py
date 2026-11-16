"""Sales Report (/reports/sales) — Option C dashboard (2026-11-14, Pankaj).

Period = the page's From / To dates (default last 90 days). Revenue per day,
state × month, top customers, payment mode, invoice summary (sales grouped by
invoice number, else Sale #), and a daily-revenue calendar for the last 8
weeks whose View all opens the financial year (April to this month) day by
day. The page itself needs Sales Report Pricing, so amounts show.
"""
import calendar
from datetime import datetime, timedelta, date

from sqlalchemy import select

from models.sales import Sale
from services import dash_widgets as dw
from utils.timezone import app_now

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
PAL = ["cyan", "green", "amber", "blue", "violet", "indigo"]


async def build(db, from_dt, to_dt):
    now = app_now()
    today = now.date()
    fy = today.year if today.month >= 4 else today.year - 1
    fy0 = datetime(fy, 4, 1)
    m6 = datetime(today.year, today.month, 1)
    for _ in range(5):
        m6 = (m6 - timedelta(days=1)).replace(day=1)
    since = min(from_dt, fy0, m6, datetime.combine(today - timedelta(weeks=8), datetime.min.time()))
    rows = (await db.execute(select(Sale.sold_at, Sale.sale_price, Sale.customer_name, Sale.customer_state, Sale.payment_mode,
                                    Sale.invoice_no, Sale.sale_number)
                             .where(Sale.sold_at >= since, Sale.sold_at <= max(to_dt, now)))).all()
    per = [r for r in rows if r[0] and from_dt <= r[0] <= to_dt]
    rev = sum(float(r[1] or 0) for r in per)
    inv = {}
    for r in per:
        k = (r[5] or "").strip() or r[6]
        a = inv.setdefault(k, {"d": r[0], "c": r[2], "s": r[3], "v": 0.0, "n": 0, "m": r[4]})
        a["v"] += float(r[1] or 0); a["n"] += 1
        if r[0] > a["d"]:
            a["d"] = r[0]
    custs = {(r[2] or "").strip().lower() for r in per if r[2]}
    days_with = len({r[0].date() for r in per})
    plabel = "%s – %s" % (from_dt.strftime("%d %b"), to_dt.strftime("%d %b %Y"))
    kpis = [dw.kpi("Revenue", dw.inr(rev), plabel, True, "cyan", style="spark"),
            dw.kpi("Units", dw.num(len(per)), "tags sold", True, "blue", style="spark"),
            dw.kpi("Invoices", dw.num(len(inv)), "invoice no., else Sale #", True, "indigo", style="spark"),
            dw.kpi("Customers", dw.num(len(custs)), "in the period", True, "green", style="spark"),
            dw.kpi("Avg invoice", dw.inr(rev / len(inv)) if inv else "—", "per invoice", True, "violet", style="spark"),
            dw.kpi("Days with sales", dw.num(days_with), "of %d days" % ((to_dt.date() - from_dt.date()).days + 1), True, "amber", style="spark")]

    nd = (to_dt.date() - from_dt.date()).days + 1
    dlist = [from_dt.date() + timedelta(days=i) for i in range(nd)]
    by_d = {}
    for r in per:
        by_d[r[0].date()] = by_d.get(r[0].date(), 0) + float(r[1] or 0)
    area = (dw.area("Revenue per day", 12, [d.strftime("%d %b") for d in dlist], [("Revenue", [by_d.get(d, 0) for d in dlist], "cyan")],
                    kind="inr", legv="sum", sub=plabel)
            if by_d else dw.insight("Revenue per day", 12, [("i", "No sales in %s." % plabel, "slate")]))
    months6 = []
    y, m = m6.year, m6.month
    for _ in range(6):
        months6.append((y, m)); m += 1
        if m > 12:
            y, m = y + 1, 1
    st = {}
    for r in rows:
        if r[0] and r[0] >= m6:
            s = (r[3] or "").strip().title()[:14] or "Not given"
            st.setdefault(s, {})[(r[0].year, r[0].month)] = st.get(s, {}).get((r[0].year, r[0].month), 0) + float(r[1] or 0) / 1e5
    top_s = sorted(st, key=lambda s: -sum(st[s].values()))[:6]
    heat = (dw.heat("State × month", 5, [MONTHS[mm - 1] for _, mm in months6], [(s, [round(st[s].get(k, 0), 1) for k in months6]) for s in top_s],
                    color="cyan", lw="90px", sub="Revenue ₹ lakh, last 6 months")
            if top_s else dw.insight("State × month", 5, [("i", "No sales in the last 6 months.", "slate")]))
    tc = {}
    for r in per:
        if r[2]:
            tc[r[2].strip()[:18]] = tc.get(r[2].strip()[:18], 0) + float(r[1] or 0)
    tcl = sorted(tc.items(), key=lambda kv: -kv[1])[:5]
    top_c = (dw.hbars("Top customers", 3, tcl, kind="inr", color="cyan", lw="100px", sub=plabel)
             if tcl else dw.insight("Top customers", 3, [("i", "No sales in the period.", "slate")]))
    pm = {}
    for r in per:
        k = (r[4] or "Not set").strip()[:16]
        pm[k] = pm.get(k, 0) + float(r[1] or 0)
    pml = sorted(pm.items(), key=lambda kv: -kv[1])
    dparts = [(k, v, PAL[i]) for i, (k, v) in enumerate(pml[:5]) if v]
    if len(pml) > 5:
        dparts.append(("Other", sum(v for _, v in pml[5:]), "grey"))
    donut = (dw.donut("Payment mode", 4, dparts, dw.inr(rev), "billed", kind="inr")
             if dparts else dw.insight("Payment mode", 4, [("i", "No sales in the period.", "slate")]))
    il = sorted(inv.items(), key=lambda kv: kv[1]["d"], reverse=True)[:8]
    table = dw.table("Invoice summary", 8, [("Invoice", 1), ("Date", 0.8), ("Customer", 1.4), ("State", 0.8), ("Tags", 0.4), ("Value", 0.8), ("Mode", 0.8)],
                     [[k, a["d"].strftime("%Y-%m-%d"), (a["c"] or "—")[:22], (a["s"] or "—")[:12], dw.num(a["n"]), dw.inr(a["v"]),
                       ((a["m"] or "—")[:12], "cyan")] for k, a in il] or [["No sales in the period", "", "", "", "", "", ""]],
                     sub="Newest first · dates in YYYY-MM-DD, as in exports")
    daily = {}
    for r in rows:
        if r[0]:
            daily[r[0].date()] = daily.get(r[0].date(), 0) + float(r[1] or 0)
    wk0 = today - timedelta(days=today.weekday()) - timedelta(weeks=7)
    mx8 = max([daily.get(wk0 + timedelta(days=i), 0) for i in range(56)] or [0]) or 1
    cal = dw.cal("Daily revenue", 4, 8, 0, "cyan", "Last 8 weeks")
    cells = []
    for i in range(56):
        d = wk0 + timedelta(days=(i // 7) * 7 + (i % 7))
        v = daily.get(d, 0)
        cells.append({"bg": "var(--dk-track)" if d > today else dw.tint(dw.C["cyan"], 0.08 + 0.85 * (v / mx8)),
                      "tip": "%s: %s" % (d.strftime("%d %b"), dw.inr(v))})
    cal["cells"] = cells
    cal = dw.withbtn(cal, "View all", modal="#srDailyModal")
    # Financial year, month by month (Monday first), for the View all modal.
    fy_days = {d: v for d, v in daily.items() if d >= fy0.date()}
    mxf = max(fy_days.values() or [0]) or 1
    fy_months = []
    y, m = fy, 4
    while (y, m) <= (today.year, today.month):
        first_dow, ndays = calendar.monthrange(y, m)
        cells = [None] * first_dow
        tot = 0.0
        for dd in range(1, ndays + 1):
            d = date(y, m, dd)
            v = fy_days.get(d, 0)
            tot += v
            cells.append({"n": dd, "future": d > today, "v": v, "tip": "%s: %s" % (d.strftime("%d %b %Y"), dw.inr(v)),
                          "bg": "var(--dk-track)" if d > today else (dw.tint(dw.C["cyan"], 0.1 + 0.85 * (v / mxf)) if v else "var(--dk-card)")})
        fy_months.append({"label": "%s %d" % (calendar.month_name[m], y), "cells": cells, "total": dw.inr(tot)})
        m += 1
        if m > 12:
            y, m = y + 1, 1
    fy_info = {"label": "FY %d-%02d" % (fy, (fy + 1) % 100), "total": dw.inr(sum(fy_days.values())), "months": fy_months}
    widgets = dw.layout([(320, [area]), (380, [heat, top_c, donut]), (420, [table, cal])])
    return kpis, widgets, fy_info
