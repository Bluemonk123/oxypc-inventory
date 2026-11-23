"""Returns (/returns) — Option C dashboard (2026-11-14, Pankaj).

Return rows (models.sales.Return) with their device and sale. 2026-11-21
(Pankaj): returns no longer need approval (approved when logged), so the
approval tiles and lanes are gone and rejected returns (old data) are left
out everywhere. Board lanes: Returned in the last 30 days (no credit note) ·
Credit note in progress (cn_stage before "CN Complete") · CN complete in the
last 30 days. Return rate = returns this month / tags sold this month.
Refunds need the Sales List Pricing tick.
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
                             .where(Return.return_date >= min(y0, d90),
                                    func.coalesce(Return.approval_status, "pending") != "rejected"))).all()
    cn_open = (await db.execute(select(Return.return_date, Device.barcode, Device.model, Return.cn_stage)
                                .join(Device, Return.device_id == Device.id)
                                .where(Return.cn_stage.isnot(None), Return.cn_stage != "CN Complete",
                                       func.coalesce(Return.approval_status, "pending") != "rejected")
                                .order_by(Return.return_date.asc()))).all()
    mtd = [r for r in rows if r[1] and r[1] >= month0]
    sold_m = (await db.execute(select(func.count(Sale.id)).where(Sale.sold_at >= month0))).scalar() or 0
    r90 = [r for r in rows if r[1] and r[1] >= d90]
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
            dw.kpi("Returns last 90 days", dw.num(len(r90)), "tags", not r90, "amber"),
            dw.kpi("Credit notes open", dw.num(len(cn_open)), "not yet CN Complete", not cn_open, "violet"),
            dw.kpi("Refunds this month", dw.inr(refunds) if can_price else "—", "on returns logged this month", True, "green"),
            dw.kpi("Repeat-return customers", dw.num(repeat), "2+ returns in 90 days", not repeat, "slate")]

    def chip(bc, model, sub):
        return ("%s · %s" % (bc or "—", (model or "")[:18]), sub)
    recent = sorted([r for r in rows if not r[5] and r[1] and r[1] >= d30], key=lambda r: r[1], reverse=True)
    cn_done = [r for r in rows if r[5] == "CN Complete" and r[1] and r[1] >= d30]
    lanes = [("Returned", "red", len(recent), [chip(r[8], r[10], "%s · %s" % ((r[11] or "—")[:16], _reason(r[2]))) for r in recent[:2]]),
             ("Credit note in progress", "violet", len(cn_open), [chip(c[1], c[2], c[3] or "") for c in cn_open[:2]]),
             ("CN complete", "blue", len(cn_done), [chip(r[8], r[10], "Credit note closed") for r in cn_done[:2]])]
    board = dw.lanes("Return board", 12, lanes, sub="Returned and CN complete: last 30 days · credit notes: open now",
                     tag="%d credit notes open" % len(cn_open), tagc="violet")

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
