"""Main Dashboard — Option C "V1" analytics (2026-11-14, Pankaj).

The current dashboard is kept (Total Inventory, device-type tiles, Live Stage
Pipeline, Financial Summary, parts grid, weekly charts). This adds the KPI
strip and the analytics cards from the V1 design:

  Revenue and cost (FY by month) · Inflow vs outflow (8 weeks) · Stock health
  Stage ageing (days in stage) · Device type x stage · Needs attention

Same Entity / Device Type / Zone filters as the rest of the page (the
`device_where` list the dashboard route already builds).
"""
from datetime import datetime, timedelta

from sqlalchemy import select, func, case, and_, literal_column

from models.device import Device, DeviceStage, StageMovement
from models.sales import Sale
from services import dash_widgets as dw
from services.business_pl import compute_year_revenue, compute_year_device_cogs, compute_year_parts_labour_cogs
from utils.timezone import app_now

REPAIR = [DeviceStage.l1, DeviceStage.l2, DeviceStage.l3, DeviceStage.trc_production, DeviceStage.qc_check,
          DeviceStage.cosmetic_received, DeviceStage.cleaning, DeviceStage.putty, DeviceStage.dry_sanding,
          DeviceStage.masking, DeviceStage.painting, DeviceStage.water_sanding, DeviceStage.cosmetic_completed]
FINAL_QC = [DeviceStage.final_qc, DeviceStage.final_qc_pass_hold, DeviceStage.final_qc_fail_hold]
COSMETIC = REPAIR[5:]
GONE = [DeviceStage.sold, DeviceStage.returned, DeviceStage.scrapped, DeviceStage.scrap_for_sale, DeviceStage.on_rent]
FY_MONTHS = ['Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec', 'Jan', 'Feb', 'Mar']
AGE_GROUPS = [
    ("Stock In", [DeviceStage.stock_in]),
    ("L1/L2", [DeviceStage.l1, DeviceStage.l2]),
    ("L3/L4", [DeviceStage.l3]),
    ("Stress/QC", [DeviceStage.qc_check, DeviceStage.trc_production]),
    ("Cosmetic", COSMETIC),
    ("Final QC", FINAL_QC),
    ("Ready", [DeviceStage.ready_to_sale]),
]
BUCKETS = [("0-2d", 0, 3), ("3-7d", 3, 8), ("8-15d", 8, 16), ("16-30d", 16, 31), ("30d+", 31, 100000)]


def _sv(s):
    return getattr(s, "value", s)


async def build(db, device_where, stage_counts, category_counts, categories, *, admin, fy,
                month_revenue, month_profit, fy_net_profit, fy_revenue, parts_card):
    now = app_now()
    today = now.date()
    sc = {_sv(k): v for k, v in (stage_counts or {}).items()}
    n = lambda *st: sum(sc.get(_sv(s), 0) for s in st)          # noqa: E731
    in_repair = n(*REPAIR)
    ready = n(DeviceStage.ready_to_sale)
    active = sum(v for k, v in sc.items() if k not in {_sv(s) for s in GONE} and k != "grn")

    # ── Sold this FY + 8-week inflow / outflow ───────────────────────────
    fy_start = datetime(fy, 4, 1)
    sold_fy = (await db.execute(select(func.count(Sale.id)).join(Device, Device.id == Sale.device_id)
                                .where(Sale.sold_at >= fy_start, *device_where))).scalar() or 0
    wk0 = datetime.combine(today - timedelta(days=today.weekday()) - timedelta(weeks=7), datetime.min.time())
    weeks = [wk0 + timedelta(weeks=i) for i in range(8)]
    wk_idx = lambda dt: min(7, max(0, (dt - wk0).days // 7))      # noqa: E731
    recv, sold, scrap = [0] * 8, [0] * 8, [0] * 8
    for dt, c in (await db.execute(select(func.date_trunc(literal_column("'week'"), Device.created_at), func.count(Device.id))
                                   .where(Device.is_trashed == False, Device.created_at >= wk0, *device_where)  # noqa: E712
                                   .group_by(func.date_trunc(literal_column("'week'"), Device.created_at)))).all():
        recv[wk_idx(dt)] += c
    for dt, c in (await db.execute(select(func.date_trunc(literal_column("'week'"), Sale.sold_at), func.count(Sale.id))
                                   .join(Device, Device.id == Sale.device_id)
                                   .where(Sale.sold_at >= wk0, *device_where)
                                   .group_by(func.date_trunc(literal_column("'week'"), Sale.sold_at)))).all():
        sold[wk_idx(dt)] += c
    for dt, c in (await db.execute(select(func.date_trunc(literal_column("'week'"), StageMovement.moved_at), func.count(StageMovement.id))
                                   .join(Device, Device.id == StageMovement.device_id)
                                   .where(StageMovement.moved_at >= wk0,
                                          StageMovement.to_stage.in_([DeviceStage.scrapped, DeviceStage.scrap_for_sale]), *device_where)
                                   .group_by(func.date_trunc(literal_column("'week'"), StageMovement.moved_at)))).all():
        scrap[wk_idx(dt)] += c
    sold_12m = (await db.execute(select(func.count(Sale.id)).join(Device, Device.id == Sale.device_id)
                                 .where(Sale.sold_at >= now - timedelta(days=365), *device_where))).scalar() or 0

    # ── Days in current stage (latest movement into it) ──────────────────
    live = [s for _, g in AGE_GROUPS for s in g]
    # Only the live devices' movements (2026-11-15): grouping the whole
    # stage_movements history on every load was the slowest query here.
    live_ids = select(Device.id).where(Device.is_trashed == False, Device.current_stage.in_(live), *device_where)  # noqa: E712
    last_move = (select(StageMovement.device_id, func.max(StageMovement.moved_at).label("at"))
                 .where(StageMovement.device_id.in_(live_ids))
                 .group_by(StageMovement.device_id).subquery())
    age_days = func.extract('epoch', now - func.coalesce(last_move.c.at, Device.created_at)) / 86400
    bucket = case(*[(and_(age_days >= lo, age_days < hi), i) for i, (_, lo, hi) in enumerate(BUCKETS)], else_=len(BUCKETS) - 1)
    grid = {}
    inner = (select(Device.current_stage.label("st"), bucket.label("b"))
             .outerjoin(last_move, last_move.c.device_id == Device.id)
             .where(Device.is_trashed == False, Device.current_stage.in_(live), *device_where)  # noqa: E712
             .subquery())
    for stage, b, c in (await db.execute(
            select(inner.c.st, inner.c.b, func.count()).group_by(inner.c.st, inner.c.b))).all():
        grid[(_sv(stage), int(b))] = grid.get((_sv(stage), int(b)), 0) + c
    age_rows = [(label, [sum(grid.get((_sv(s), i), 0) for s in g) for i in range(len(BUCKETS))]) for label, g in AGE_GROUPS]
    old_ready = sum(age_rows[-1][1][4:])
    aged_60 = (await db.execute(select(func.count(Device.id)).where(
        Device.is_trashed == False, Device.current_stage.notin_(GONE + [DeviceStage.grn]),  # noqa: E712
        Device.created_at < now - timedelta(days=60), *device_where))).scalar() or 0

    ready_share = ready / active if active else 0
    aged_share = aged_60 / active if active else 0
    turns = sold_12m / active if active else 0
    health = round(100 * (0.4 * ready_share + 0.4 * (1 - aged_share) + 0.2 * min(turns / 6, 1))) if active else 0

    # ── KPI strip ────────────────────────────────────────────────────────
    kpis = [dw.kpi("In repair", dw.num(in_repair), "L1 → cosmetic", True, "amber", recv, style="spark"),
            dw.kpi("Ready for sale", dw.num(ready), ("%d%% of stock" % round(ready_share * 100)) if active else "", True, "green", style="spark"),
            dw.kpi("Sold this FY", dw.num(sold_fy), "FY %d-%02d" % (fy, (fy + 1) % 100), True, "indigo", sold, style="spark")]
    if admin:
        kpis += [dw.kpi("Revenue this month", dw.inr(month_revenue), "", True, "cyan", style="spark"),
                 dw.kpi("Profit this month", ("−" if month_profit < 0 else "") + dw.inr(abs(month_profit)),
                        "below cost" if month_profit < 0 else "", month_profit >= 0, "red" if month_profit < 0 else "green", style="spark"),
                 dw.kpi("Net profit FY", dw.inr(fy_net_profit),
                        ("%.1f%% margin" % (fy_net_profit * 100 / fy_revenue)) if fy_revenue else "", fy_net_profit >= 0, "blue", style="spark")]
    else:
        kpis += [dw.kpi("Final QC", dw.num(n(*FINAL_QC)), "", True, "cyan", style="spark"),
                 dw.kpi("Stock In", dw.num(n(DeviceStage.stock_in)), "waiting for production", True, "blue", style="spark"),
                 dw.kpi("Scrap for sale", dw.num(n(DeviceStage.scrap_for_sale)), "", False, "red", style="spark")]

    # ── Cards ────────────────────────────────────────────────────────────
    nmonths = (today.month - 4) % 12 + 1          # Apr .. this month
    row1 = []
    if admin:
        r1, r2 = await compute_year_revenue(db, fy), await compute_year_revenue(db, fy + 1)
        d1, d2 = await compute_year_device_cogs(db, fy), await compute_year_device_cogs(db, fy + 1)
        (p1, l1), (p2, l2) = await compute_year_parts_labour_cogs(db, fy), await compute_year_parts_labour_cogs(db, fy + 1)
        rev = (r1[3:] + r2[:3])[:nmonths]
        cost = [a + b + c for a, b, c in zip((d1[3:] + d2[:3]), (p1[3:] + p2[:3]), (l1[3:] + l2[:3]))][:nmonths]
        if nmonths >= 2 and (any(rev) or any(cost)):
            row1.append(dw.area("Revenue and cost", 5, FY_MONTHS[:nmonths], [("Revenue", rev, "blue"), ("Cost", cost, "slate")],
                                kind="inr", dashed=True, legv="sum", sub="FY %d-%02d by month · device + parts + labour" % (fy, (fy + 1) % 100),
                                href="/reports/business-pl"))
        else:
            row1.append(dw.insight("Revenue and cost", 5, [("i", "Not enough months in this FY yet.", "slate")]))
    wl = [w.strftime("%d %b") for w in weeks]
    row1.append(dw.bars("Inflow vs outflow", 4 if admin else 9, wl, [("Received", recv, "blue"), ("Sold", sold, "green"), ("Scrapped", scrap, "red")],
                        sub="Devices per week"))
    row1.append(dw.gauge("Stock health", 3, health, str(health), "out of 100", "green" if health >= 60 else ("amber" if health >= 40 else "red"),
                         [("Ageing >60d", "%d%%" % round(aged_share * 100)), ("Ready", "%d%%" % round(ready_share * 100)), ("Turns", "%.1f×" % turns)],
                         sub="Readiness, age and turnover"))

    cats = [c for c in categories if (category_counts.get(c) or {}).get("total")][:7]
    cat_cols = [("IQC", ["iqc"]), ("Stock In", ["stock_in"]), ("Repair", [_sv(s) for s in REPAIR]),
                ("Final QC", [_sv(s) for s in FINAL_QC]), ("Ready", ["ready_to_sale"])]
    cat_rows = [(c, [sum((category_counts.get(c) or {}).get(k, 0) for k in ks) for _, ks in cat_cols]) for c in cats]
    pc = parts_card or {}
    attn = []
    if old_ready:
        attn.append(("%s ready devices older than 30 days" % dw.num(old_ready), "Ready to Sale · consider a price drop", "now", "red"))
    if n(DeviceStage.scrap_for_sale):
        attn.append(("%s devices in Scrap for Sale" % dw.num(n(DeviceStage.scrap_for_sale)), "List them for scrap buyers", "today", "amber"))
    if pc.get("low"):
        attn.append(("%s part SKUs below minimum" % dw.num(pc.get("low")), "Parts · reorder", "today", "amber"))
    l3_old = sum(age_rows[2][1][1:])
    if l3_old:
        attn.append(("%s devices in L3/L4 over 2 days" % dw.num(l3_old), "Check board-level repairs", "today", "red"))
    if admin and month_profit < 0:
        attn.append(("Profit this month −%s" % dw.inr(abs(month_profit)), "Revenue %s so far" % dw.inr(month_revenue), "today", "red"))
    if n(DeviceStage.stock_in):
        attn.append(("%s devices in Stock In" % dw.num(n(DeviceStage.stock_in)), "Waiting to start production", "today", "blue"))
    row2 = [dw.heat("Stage ageing", 5, [b[0] for b in BUCKETS], age_rows, color="blue", lw="70px", sub="Devices by days in their current stage"),
            (dw.heat("Device type × stage", 4, [c[0] for c in cat_cols], cat_rows, color="indigo", lw="90px", sub="Active devices")
             if cat_rows else dw.insight("Device type × stage", 4, [("i", "No devices for these filters.", "slate")])),
            dw.feed("Needs attention", 3, attn[:6] or [("All clear", "Nothing needs attention right now", "", "green")], sub="Alerts")]
    widgets = dw.layout([(340, row1), (340, row2)])
    return kpis, widgets
