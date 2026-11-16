"""Floor Movement (/transfers) — Option C dashboard (2026-11-14, Pankaj).

Device moves between TRC Floor, 1st Floor and Showroom, and why: For sale /
For repair / Extra return (from StockTransfer.transfer_type). No in-transit or
acknowledgement — those are not part of this flow.

Location = where the move went (StockTransfer.to_warehouse), grouped:
anything with "showroom" -> Showroom, "1st" -> 1st Floor, "trc" -> TRC Floor.
"""
from datetime import datetime, timedelta

from sqlalchemy import select, func, desc

from models.device import Device, DeviceStage
from models.stock_transfer import StockTransfer
from services import dash_widgets as dw
from utils.timezone import app_now

LOCS = ["TRC Floor", "1st Floor", "Showroom"]
REASONS = [("For sale", "green"), ("For repair", "amber"), ("Extra return", "red")]


def loc_of(to_wh):
    t = (to_wh or "").lower()
    if "showroom" in t:
        return "Showroom"
    if "1st" in t or "first" in t:
        return "1st Floor"
    if "trc" in t:
        return "TRC Floor"
    return None


def reason_of(tt):
    t = (tt or "").lower()
    if "return" in t:
        return "Extra return"
    if "repair" in t or "trc" in t:
        return "For repair"
    if "sale" in t or "as_is" in t or "finish" in t:
        return "For sale"
    return None


async def build(db):
    now = app_now()
    day0 = datetime.combine(now.date(), datetime.min.time())
    since = now - timedelta(days=30)
    rows = (await db.execute(select(StockTransfer.transfer_date, StockTransfer.to_warehouse, StockTransfer.transfer_type,
                                    StockTransfer.transferred_by, StockTransfer.barcode, StockTransfer.model, StockTransfer.device_id)
                             .where(StockTransfer.move_kind == "device", StockTransfer.transfer_date >= since)
                             .order_by(desc(StockTransfer.transfer_date)))).all()
    moves = [(at, loc_of(wh), reason_of(tt), by or "—", bc, m, did, wh) for at, wh, tt, by, bc, m, did in rows if at]
    win = [x for x in moves if x[0] >= day0]
    label = "today"
    if not win:
        win = [x for x in moves if x[0] >= now - timedelta(days=7)]
        label = "last 7 days"
    cnt = lambda lst, r: sum(1 for x in lst if x[2] == r)   # noqa: E731
    kpis = [dw.kpi("Moves", dw.num(len(win)), label, True, "blue", style="spark"),
            dw.kpi("For sale", dw.num(cnt(win, "For sale")), label, True, "green", style="spark"),
            dw.kpi("For repair", dw.num(cnt(win, "For repair")), label, True, "amber", style="spark"),
            dw.kpi("Extra return", dw.num(cnt(win, "Extra return")), label, not cnt(win, "Extra return"), "red", style="spark"),
            dw.kpi("Into Showroom", dw.num(sum(1 for x in win if x[1] == "Showroom")), label, True, "indigo", style="spark"),
            dw.kpi("People moving", dw.num(len({x[3] for x in win})), label, True, "violet", style="spark")]

    grid = {(l, r): 0 for l in LOCS for r, _ in REASONS}
    for x in moves:
        if x[1] and x[2]:
            grid[(x[1], x[2])] += 1
    heat = dw.heat("Where moves went × why", 5, [r for r, _ in REASONS], [(l, [grid[(l, r)] for r, _ in REASONS]) for l in LOCS],
                   color="indigo", lw="80px", sub="Last 30 days")
    feed = dw.feed("Latest moves", 7, [("%s → %s" % (bc or "—", x_l or (wh or "—").split("→")[0].strip()),
                                         "%s · %s · %s" % (m or "", rs or "Other", by), at.strftime("%d %b %H:%M"),
                                         {"For sale": "green", "For repair": "amber", "Extra return": "red"}.get(rs, "slate"))
                                        for at, x_l, rs, by, bc, m, did, wh in moves[:6]] or [("No moves in 30 days", "", "", "slate")],
                   sub="Newest first")
    days = [now.date() - timedelta(days=13 - i) for i in range(14)]
    per = {(d, r): 0 for d in days for r, _ in REASONS}
    for x in moves:
        k = (x[0].date(), x[2])
        if k in per:
            per[k] += 1
    area = (dw.area("Moves per day", 8, [d.strftime("%d %b") for d in days],
                    [(r, [per[(d, r)] for d in days], col) for r, col in REASONS], legv="sum", sub="Last 14 days, by reason")
            if any(per.values()) else dw.insight("Moves per day", 8, [("i", "No moves in the last 14 days.", "slate")]))
    rc = [(r, sum(1 for x in moves if x[2] == r), col) for r, col in REASONS]
    other = sum(1 for x in moves if not x[2])
    if other:
        rc.append(("Other", other, "grey"))
    donut = (dw.donut("Move reason", 4, [p for p in rc if p[1]], dw.num(len(moves)), "moves · 30 days")
             if moves else dw.insight("Move reason", 4, [("i", "No moves in 30 days.", "slate")]))
    people = {}
    for x in moves:
        people[x[3]] = people.get(x[3], 0) + 1
    top = sorted(people.items(), key=lambda kv: -kv[1])[:6]
    by_person = (dw.hbars("Moves by person", 6, [(p[:18], c) for p, c in top], color="indigo", lw="120px", sub="Last 30 days")
                 if top else dw.insight("Moves by person", 6, [("i", "No moves in 30 days.", "slate")]))
    # Devices at each location now: each device's latest move, still in stock.
    latest = (select(StockTransfer.device_id, StockTransfer.to_warehouse)
              .where(StockTransfer.move_kind == "device", StockTransfer.device_id.isnot(None))
              .order_by(StockTransfer.device_id, desc(StockTransfer.transfer_date)).distinct(StockTransfer.device_id).subquery())
    at_loc = (await db.execute(select(latest.c.to_warehouse, func.count()).join(Device, Device.id == latest.c.device_id)
                               .where(Device.is_trashed == False, Device.current_stage.notin_(  # noqa: E712
                                   [DeviceStage.sold, DeviceStage.scrapped, DeviceStage.scrap_for_sale, DeviceStage.returned]))
                               .group_by(latest.c.to_warehouse))).all()
    loc_n = {l: 0 for l in LOCS}
    for wh, c in at_loc:
        l = loc_of(wh)
        if l:
            loc_n[l] += c
    mx = max(loc_n.values()) or 1
    prog = dw.prog("Devices at each location", 6, [(l, dw.num(loc_n[l]), loc_n[l] * 100 / mx, c)
                                                   for l, c in zip(LOCS, ["amber", "blue", "indigo"])], sub="In stock now, by their latest move")
    widgets = dw.layout([(360, [heat, feed]), (380, [area, donut]), (360, [by_person, prog])])
    return kpis, widgets
