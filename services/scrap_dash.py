"""Scrap Sale Report (/scrap-sale-report) — Option C dashboard (2026-11-14, Pankaj).

Built from the page's own rows (custom scrap sales, ScrapSale) plus the scrap
stock still waiting: devices in Scrapped / Scrap for Sale and open Scrap for
Sale batches. The page itself needs Scrap Sale Report Pricing, so amounts show.
"""
from datetime import date, timedelta

from sqlalchemy import select, func

from models.device import Device, DeviceStage
from models.scrap_for_sale import ScrapForSale
from services import dash_widgets as dw
from utils.timezone import app_now

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
PAL = ["slate", "blue", "teal", "amber", "violet", "indigo"]


async def build(db, rows):
    today = app_now().date()
    m0 = today.replace(day=1)
    sd = lambda r: date.fromisoformat(r["sale_date"])   # noqa: E731
    mtd = [r for r in rows if sd(r) >= m0]
    scrapped = (await db.execute(select(func.count(Device.id)).where(Device.current_stage == DeviceStage.scrapped,
                                                                     Device.is_trashed.isnot(True)))).scalar() or 0
    for_sale = (await db.execute(select(func.count(Device.id)).where(Device.current_stage == DeviceStage.scrap_for_sale,
                                                                     Device.is_trashed.isnot(True)))).scalar() or 0
    open_b = (await db.execute(select(func.count(ScrapForSale.id)).where(ScrapForSale.status == "open",
                                                                       ScrapForSale.is_trashed.isnot(True)))).scalar() or 0
    total = sum(r["total"] for r in rows)
    buyers = {(r["customer_company"] or "").strip().lower() for r in rows if r["customer_company"]}
    kpis = [dw.kpi("Scrap sold MTD", dw.inr(sum(r["total"] for r in mtd)), "%d sales" % len(mtd), True, "slate", style="spark"),
            dw.kpi("Scrap sold, all time", dw.inr(total), "%d sales" % len(rows), True, "green", style="spark"),
            dw.kpi("Avg per sale", dw.inr(total / len(rows)) if rows else "—", "all time", True, "blue", style="spark"),
            dw.kpi("Buyers", dw.num(len(buyers)), "all time", True, "indigo", style="spark"),
            dw.kpi("Tags sold", dw.num(sum(len(r["tags"]) for r in rows)), "with tag numbers", True, "cyan", style="spark"),
            dw.kpi("Waiting to sell", dw.num(for_sale), "%d open batches" % open_b, not for_sale, "amber", style="spark")]

    parts = [(l, n, c) for l, n, c in (("Scrapped", scrapped, "red"), ("Scrap for sale", for_sale, "amber"),
                                        ("Sold this month (tags)", sum(len(r["tags"]) for r in mtd), "green")) if n]
    stock = (dw.stack("Scrap stock", 12, parts, sub="Devices in Scrapped and Scrap for Sale now, and tags sold this month")
             if parts else dw.insight("Scrap stock", 12, [("✓", "No scrap waiting.", "green")]))
    months = []
    y, m = today.year, today.month
    for _ in range(12):
        months.append((y, m)); m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    val = {k: 0.0 for k in months}
    cnt = {k: 0 for k in months}
    for r in rows:
        d = sd(r); k = (d.year, d.month)
        if k in val:
            val[k] += r["total"]; cnt[k] += 1
    trend = (dw.area("Scrap sales by month", 8, [MONTHS[mm - 1] for _, mm in months], [("Value", [val[k] for k in months], "slate")],
                     kind="inr", legv="sum", sub="Last 12 months")
             if any(val.values()) else dw.insight("Scrap sales by month", 8, [("i", "No scrap sales in the last 12 months.", "slate")]))
    cat = {}
    for r in rows:
        for it in r["items"]:
            k = (it["category"] if it["category"] != "—" else it["name"] or "Other")[:16]
            cat[k] = cat.get(k, 0) + it["total"]
    cl = sorted(cat.items(), key=lambda kv: -kv[1])
    dparts = [(k, v, PAL[i]) for i, (k, v) in enumerate(cl[:5]) if v]
    if len(cl) > 5:
        dparts.append(("Other", sum(v for _, v in cl[5:]), "grey"))
    donut = (dw.donut("By item", 4, dparts, dw.inr(total), "all time", kind="inr")
             if dparts else dw.insight("By item", 4, [("i", "No items on scrap sales yet.", "slate")]))
    by_b = {}
    for r in rows:
        by_b[(r["customer_company"] or "—")[:18]] = by_b.get((r["customer_company"] or "—")[:18], 0) + r["total"]
    bl = sorted(by_b.items(), key=lambda kv: -kv[1])[:6]
    buyers_w = (dw.hbars("Buyers", 4, bl, kind="inr", color="slate", lw="110px", sub="Value bought, all time")
                if bl else dw.insight("Buyers", 4, [("i", "No scrap sales yet.", "slate")]))
    latest = dw.insight("Latest scrap sales", 8, [("i", "No scrap sales yet. Sales saved from Scrap for Sale → Custom Sale show here.", "slate")]) if not rows else dw.table("Latest scrap sales", 8, [("Sale", 0.8), ("Date", 0.7), ("Buyer", 1.4), ("Items", 0.5), ("Tags", 0.5), ("Value", 0.8)],
                      [[r["sale_number"], r["sale_date_label"], (r["customer_company"] or "—")[:24], dw.num(r["item_count"]),
                        dw.num(len(r["tags"])), dw.inr(r["total"])] for r in rows[:7]],
                      sub="Newest first")
    widgets = dw.layout([(170, [stock]), (360, [trend, donut]), (380, [buyers_w, latest])])
    return kpis, widgets
