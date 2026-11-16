"""Business P&L (/reports/business-pl) — Option C dashboard (2026-11-14, Pankaj).

Uses the page's own monthly rows (revenue, device / parts / labour cost, with
the admin overrides already applied), so the dashboard always agrees with the
Monthly Breakdown table. The category table and heat map come from the FY's
sales: sale price against the tag's buying price (Device.device_price), by
category (Device.sub_category, else device type) — tags without a buying price
are left out of those two.
"""
from datetime import datetime

from sqlalchemy import select

from models.device import Device
from models.sales import Sale
from services import dash_widgets as dw
from utils.timezone import app_now

PAL = ["blue", "indigo", "cyan", "violet", "teal", "amber"]


def _cat(sub, dtype):
    return (sub or dtype or "Other").strip().title()[:14] or "Other"


async def build(db, fy, month_rows, rev, dev, parts, labour, inv_value):
    now = app_now()
    total_rev, total_dev, total_parts, total_lab = sum(rev), sum(dev), sum(parts), sum(labour)
    gp = total_rev - total_dev - total_parts - total_lab
    gm = gp * 100 / total_rev if total_rev else 0
    kpis = [dw.kpi("Revenue", dw.inr(total_rev), "FY %d-%02d" % (fy, (fy + 1) % 100), True, "blue"),
            dw.kpi("Device cost", dw.inr(total_dev), ("%.0f%% of revenue" % (total_dev * 100 / total_rev)) if total_rev else "", True, "slate"),
            dw.kpi("Gross profit", dw.inr(gp), "after device, parts, labour", gp >= 0, "green"),
            dw.kpi("Gross margin", "%.1f%%" % gm, "FY so far", gm >= 15, "amber"),
            dw.kpi("Repair & parts", dw.inr(total_parts + total_lab), ("%.1f%% of revenue" % ((total_parts + total_lab) * 100 / total_rev)) if total_rev else "",
                   True, "violet"),
            dw.kpi("Stock at cost", dw.inr(inv_value), "unsold tags", True, "teal")]
    upto = [i for i, r in enumerate(month_rows) if datetime(r["year"], r["month"], 1) <= now]
    labels = [month_rows[i]["label"][:3] for i in upto]
    if total_rev:
        fall = dw.waterfall("Revenue to profit", 7, [("Revenue", total_rev, "total"), ("Device cost", total_dev, "down"),
                                                     ("Parts", total_parts, "down"), ("Labour", total_lab, "down"),
                                                     ("Gross profit", max(gp, 0), "total")], sub="FY so far")
        trend = dw.area("Revenue and profit", 5, labels, [("Revenue", [rev[i] for i in upto], "blue"),
                                                          ("Gross profit", [rev[i] - dev[i] - parts[i] - labour[i] for i in upto], "green")],
                        kind="inr", legv="sum", sub="By month")
    else:
        fall = dw.insight("Revenue to profit", 7, [("i", "No revenue in this financial year yet.", "slate")])
        trend = dw.insight("Revenue and profit", 5, [("i", "No revenue in this financial year yet.", "slate")])

    fy0, fy1 = datetime(fy, 4, 1), datetime(fy + 1, 4, 1)
    rows = (await db.execute(select(Sale.sold_at, Sale.sale_price, Device.device_price, Device.sub_category, Device.device_type)
                             .join(Device, Sale.device_id == Device.id)
                             .where(Sale.sold_at >= fy0, Sale.sold_at < fy1))).all()
    cat, cm = {}, {}
    for at, sp, cost, sub, dt in rows:
        k = _cat(sub, dt)
        a = cat.setdefault(k, [0.0, 0.0, 0.0])
        a[0] += float(sp or 0)
        if cost and float(sp or 0) > 0:
            a[1] += float(sp or 0); a[2] += float(cost)
            mk = (k, at.year, at.month)
            b = cm.setdefault(mk, [0.0, 0.0]); b[0] += float(sp or 0); b[1] += float(cost)
    cl = sorted(cat.items(), key=lambda kv: -kv[1][0])
    tone = lambda m: "green" if m >= 15 else "amber" if m >= 5 else "red"   # noqa: E731
    table = dw.table("P&L by category", 7, [("Category", 1.2), ("Revenue", 0.9), ("Device cost", 0.9), ("Profit", 0.9), ("Margin", 0.7)],
                     [[k, dw.inr(a[0]), dw.inr(a[2]) if a[1] else "—", dw.inr(a[1] - a[2]) if a[1] else "—",
                       (("%.1f%%" % ((a[1] - a[2]) * 100 / a[1])), tone((a[1] - a[2]) * 100 / a[1])) if a[1] else ("—", "slate")]
                      for k, a in cl[:7]] or [["No sales this FY", "", "", "", ""]],
                     sub="Device sales; cost and margin on tags with a buying price")
    dparts = [(k, a[0], PAL[i]) for i, (k, a) in enumerate(cl[:5]) if a[0]]
    if len(cl) > 5:
        dparts.append(("Other", sum(a[0] for _, a in cl[5:]), "grey"))
    donut = (dw.donut("Revenue mix", 5, dparts, dw.inr(sum(v for _, v, _ in dparts)), "device sales", kind="inr")
             if dparts else dw.insight("Revenue mix", 5, [("i", "No sales this FY.", "slate")]))
    hm = [(month_rows[i]["year"], month_rows[i]["month"]) for i in upto][-8:]
    hcats = [k for k, _ in cl[:5]]
    heat = (dw.heat("Margin % — month × category", 8, labels[-8:],
                    [(k, [round((cm[(k, y, m)][0] - cm[(k, y, m)][1]) * 100 / cm[(k, y, m)][0]) if (k, y, m) in cm else 0 for y, m in hm])
                     for k in hcats], color="green", kind="n", lw="90px", sub="Sale price vs buying price, %")
            if hcats and cm and hm else dw.insight("Margin % — month × category", 8, [("i", "No tags with a buying price sold this FY.", "slate")]))
    mm = [(month_rows[i]["label"][:3], (rev[i] - dev[i] - parts[i] - labour[i]) * 100 / rev[i]) for i in upto if rev[i]]
    best = max(mm, key=lambda t: t[1]) if mm else None
    worst = min(mm, key=lambda t: t[1]) if mm else None
    gauge = dw.gauge("Gross margin", 4, max(0, min(100, gm / 30 * 100)), "%.1f%%" % gm, "FY so far · dial to 30%", "green" if gm >= 15 else "amber",
                     [("Best month", ("%s %.0f%%" % best) if best else "—"), ("Worst month", ("%s %.0f%%" % worst) if worst else "—")])
    widgets = dw.layout([(380, [fall, trend]), (380, [table, donut]), (360, [heat, gauge])])
    return kpis, widgets
