"""Returns (/returns) — Option C dashboard (2026-11-14, Pankaj).

Return rows (models.sales.Return) with their device and sale. Board lanes:
Pending approval (same rule as /returns/pending, credit notes in their own lane) · Credit note in progress (cn_stage before "CN Complete") ·
Approved, CN complete and Rejected in the last 30 days. Return rate = returns
this month / tags sold this month. Days to resolve = approved_at - return_date
(last 90 days). Refunds need the Sales List Pricing tick.
"""
from datetime import datetime, timedelta

from sqlalchemy import select, func

from models.device import Device
from models.sales import Sale, Return
from services import dash_widgets as dw
from utils.timezone import app_now

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
PAL = ["red", "amber", "orange", "violet", "slate", "blue"]


def _reason(r):
    t = (r or "").strip().split("\n")[0]
    return (t[:22] + "…") if len(t) > 23 else (t or "Not given")


async def build(db, can_price=True):
    now = app_now()
    month0 = datetime(now.year, now.month, 1)
    d30, d90 = now - timedelta(days=30), now - timedelta(days=90)
    y0 = datetime(now.year - (1 if now.month < 12 else 0), now.month % 12 + 1, 1)
    rows = (await db.execute(select(Return.id, Return.return_date, Return.reason, Return.approval_status, Return.approved_at,
                                    Return.cn_stage, Return.refund_amount, Return.action_taken, Device.barcode, Device.brand,
                                    Device.model, Sale.customer_name)
                             .join(Device, Return.device_id == Device.id).outerjoin(Sale, Return.sale_id == Sale.id)
                             .where(Return.return_date >= min(y0, d90)))).all()
    pend_all = (await db.execute(select(Return.return_date, Device.barcode, Device.model, Sale.customer_name, Return.reason, Return.cn_stage)
                                 .join(Device, Return.device_id == Device.id).outerjoin(Sale, Return.sale_id == Sale.id)
                                 .where(func.coalesce(Return.approval_status, "pending") == "pending")
                                 .order_by(Return.return_date.asc()))).all()
    cn_open = (await db.execute(select(Return.return_date, Device.barcode, Device.model, Return.cn_stage)
                                .join(Device, Return.device_id == Device.id)
                                .where(Return.cn_stage.isnot(None), Return.cn_stage != "CN Complete")
                                .order_by(Return.return_date.asc()))).all()
    mtd = [r for r in rows if r[1] and r[1] >= month0]
    sold_m = (await db.execute(select(func.count(Sale.id)).where(Sale.sold_at >= month0))).scalar() or 0
    old7 = sum(1 for p in pend_all if p[0] and p[0] < now - timedelta(days=7))
    res = [(r[4] - r[1]).total_seconds() / 86400 for r in rows if r[4] and r[1] and r[1] >= d90 and r[4] >= r[1]]
    refunds = sum(float(r[6] or 0) for r in mtd)
    cust90 = {}
    for r in rows:
        if r[1] and r[1] >= d90 and r[11]:
            k = r[11].strip()
            cust90[k] = cust90.get(k, 0) + 1
    repeat = sum(1 for v in cust90.values() if v > 1)
    kpis = [dw.kpi("Returns this month", dw.num(len(mtd)), "tags", not mtd, "red"),
            dw.kpi("Return rate", ("%.1f%%" % (len(mtd) * 100 / sold_m)) if sold_m else "—", "of %s sold this month" % dw.num(sold_m),
                   not sold_m or len(mtd) * 100 / sold_m < 3, "orange"),
            dw.kpi("Pending approval", dw.num(len(pend_all)), "%d over 7 days" % old7, not old7, "amber"),
            dw.kpi("Avg days to resolve", ("%.1f" % (sum(res) / len(res))) if res else "—", "approved, last 90 days", True, "green"),
            dw.kpi("Refunds this month", dw.inr(refunds) if can_price else "—", "%d credit notes open" % len(cn_open), True, "violet"),
            dw.kpi("Repeat-return customers", dw.num(repeat), "2+ returns in 90 days", not repeat, "slate")]

    def chip(bc, model, sub):
        return ("%s · %s" % (bc or "—", (model or "")[:18]), sub)
    appr = [r for r in rows if (r[3] or "") == "approved" and r[4] and r[4] >= d30]
    cn_done = [r for r in rows if r[5] == "CN Complete" and r[1] and r[1] >= d30]
    rej = [r for r in rows if (r[3] or "") == "rejected" and r[1] and r[1] >= d30]
    pend_lane = [p for p in pend_all if not p[5]]      # credit-note returns sit in their own lane
    lanes = [("Pending approval", "red", len(pend_lane), [chip(p[1], p[2], "%s · %s" % ((p[3] or "—")[:16], _reason(p[4]))) for p in pend_lane[:2]]),
             ("Credit note in progress", "violet", len(cn_open), [chip(c[1], c[2], c[3] or "") for c in cn_open[:2]]),
             ("Approved", "green", len(appr), [chip(r[8], r[10], (r[7] or "approved").title()) for r in sorted(appr, key=lambda r: r[4], reverse=True)[:2]]),
             ("CN complete", "blue", len(cn_done), [chip(r[8], r[10], "Credit note closed") for r in cn_done[:2]]),
             ("Rejected", "slate", len(rej), [chip(r[8], r[10], _reason(r[2])) for r in rej[:2]])]
    board = dw.lanes("Return board", 12, lanes, sub="Open now · approved, completed and rejected in the last 30 days",
                     tag="%d open" % (len(pend_lane) + len(cn_open)), tagc="red")

    months = []
    y, m = y0.year, y0.month
    for _ in range(12):
        months.append((y, m)); m += 1
        if m > 12:
            y, m = y + 1, 1
    cnt = {k: 0 for k in months}
    for r in rows:
        if r[1]:
            k = (r[1].year, r[1].month)
            if k in cnt:
                cnt[k] += 1
    trend = (dw.area("Returns by month", 7, [MONTHS[mm - 1] for _, mm in months], [("Returns", [cnt[k] for k in months], "red")],
                     legv="sum", sub="Last 12 months")
             if any(cnt.values()) else dw.insight("Returns by month", 7, [("i", "No returns in the last 12 months.", "slate")]))
    q = [r for r in rows if r[1] and r[1] >= d90]
    rs = {}
    for r in q:
        rs[_reason(r[2])] = rs.get(_reason(r[2]), 0) + 1
    rl = sorted(rs.items(), key=lambda kv: -kv[1])
    dparts = [(k, v, PAL[i]) for i, (k, v) in enumerate(rl[:5])]
    if len(rl) > 5:
        dparts.append(("All other", sum(v for _, v in rl[5:]), "grey"))
    donut = (dw.donut("Return reasons", 5, dparts, dw.num(len(q)), "last 90 days")
             if dparts else dw.insight("Return reasons", 5, [("i", "No returns in the last 90 days.", "slate")]))
    mods = {}
    for r in q:
        mk = ("%s %s" % (r[9] or "", r[10] or "")).strip()[:18] or "—"
        mods[mk] = mods.get(mk, 0) + 1
    top_m = [k for k, _ in sorted(mods.items(), key=lambda kv: -kv[1])[:6]]
    top_r = [k for k, _ in rl[:5]]
    grid = {}
    for r in q:
        k = (("%s %s" % (r[9] or "", r[10] or "")).strip()[:18] or "—", _reason(r[2]))
        grid[k] = grid.get(k, 0) + 1
    heat = (dw.heat("Model × reason", 7, [t[:12] for t in top_r], [(mm, [grid.get((mm, t), 0) for t in top_r]) for mm in top_m],
                    color="red", lw="120px", sub="Returns, last 90 days")
            if top_m and top_r else dw.insight("Model × reason", 7, [("i", "No returns in the last 90 days.", "slate")]))
    cl = sorted(cust90.items(), key=lambda kv: -kv[1])[:6]
    by_c = (dw.hbars("Returns by customer", 5, [(k[:18], v) for k, v in cl], color="red", lw="120px", sub="Last 90 days")
            if cl else dw.insight("Returns by customer", 5, [("i", "No customer returns in the last 90 days.", "slate")]))
    widgets = dw.layout([(320, [board]), (380, [trend, donut]), (400, [heat, by_c])])
    return kpis, widgets
