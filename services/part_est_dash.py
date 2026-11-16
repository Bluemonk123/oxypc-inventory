"""Part Estimation (/part-estimation) — Option C dashboard (2026-11-14, Pankaj).

Estimates raised (PartEstimate), what they cost, which parts come up most
(PartEstimateLine), the biggest estimates, and the parts tags are waiting for
right now (active DevicePNAPart) by part and by model. Pricing page — the
route already blocks users without Pricing View.
"""
from datetime import datetime, timedelta

from sqlalchemy import select, func, desc

from models.device import Device, DeviceStage
from models.part_estimate import PartEstimate, PartEstimateLine
from models.part_request import PartSourcingRequest
from models.pna_part import DevicePNAPart
from services import dash_widgets as dw
from utils.timezone import app_now


async def build(db, pna_tag_count):
    now = app_now()
    d90 = now - timedelta(days=90)
    month0 = datetime(now.year, now.month, 1)
    n_month = (await db.execute(select(func.count(PartEstimate.id)).where(PartEstimate.created_at >= month0))).scalar() or 0
    n90, v90, prod90 = (await db.execute(select(func.count(PartEstimate.id), func.sum(PartEstimate.total_estimation),
                                                func.sum(PartEstimate.total_products)).where(PartEstimate.created_at >= d90))).one()
    v90, prod90 = float(v90 or 0), int(prod90 or 0)
    open_req = (await db.execute(select(func.count(PartSourcingRequest.id)).where(
        func.lower(PartSourcingRequest.status).in_(["pending", "open", "requested", "approved"])))).scalar() or 0
    lines90 = (await db.execute(select(func.coalesce(func.sum(PartEstimateLine.qty), 0)).join(
        PartEstimate, PartEstimate.id == PartEstimateLine.estimate_id).where(PartEstimate.created_at >= d90))).scalar() or 0
    kpis = [dw.kpi("Estimates this month", dw.num(n_month), "%s in 90 days" % dw.num(n90 or 0), True, "violet"),
            dw.kpi("Estimated value", dw.inr(v90), "last 90 days", True, "indigo"),
            dw.kpi("Avg per device", dw.inr(v90 / prod90) if prod90 else "—", "%s devices" % dw.num(prod90), True, "blue"),
            dw.kpi("Parts estimated", dw.num(lines90), "units, 90 days", True, "cyan"),
            dw.kpi("Tags in PNA", dw.num(pna_tag_count), "waiting for parts", not pna_tag_count, "amber"),
            dw.kpi("Open part requests", dw.num(open_req), "not handed over", not open_req, "red")]

    wk0 = datetime.combine((now - timedelta(days=now.weekday())).date(), datetime.min.time()) - timedelta(weeks=11)
    weeks = [wk0 + timedelta(weeks=i) for i in range(12)]
    by_wk = {}
    for at, v in (await db.execute(select(PartEstimate.created_at, PartEstimate.total_estimation).where(PartEstimate.created_at >= wk0))).all():
        if at:
            i = min(11, (at - wk0).days // 7); by_wk[i] = by_wk.get(i, 0) + float(v or 0)
    trend = (dw.area("Estimate value by week", 8, [w.strftime("%d %b") for w in weeks], [("Estimated", [by_wk.get(i, 0) for i in range(12)], "violet")],
                     kind="inr", legv="sum", sub="Last 12 weeks") if by_wk
             else dw.insight("Estimate value by week", 8, [("i", "No estimates in the last 12 weeks.", "slate")]))
    parts = (await db.execute(select(PartEstimateLine.part_name, func.sum(PartEstimateLine.qty), func.sum(PartEstimateLine.total_cost))
                              .join(PartEstimate, PartEstimate.id == PartEstimateLine.estimate_id)
                              .where(PartEstimateLine.part_name.isnot(None))
                              .group_by(PartEstimateLine.part_name).order_by(desc(func.sum(PartEstimateLine.qty))).limit(12))).all()
    top = dw.hbars("Most estimated parts", 4, [((p or "—")[:20], int(q or 0)) for p, q, _ in parts[:6]], color="violet", lw="110px",
                   sub="Units, all estimates") if parts else dw.insight("Most estimated parts", 4, [("i", "No priced part lines on estimates yet.", "slate")])
    by_cost = sorted(parts, key=lambda r: -float(r[2] or 0))
    cols = ["violet", "indigo", "blue", "cyan", "teal"]
    dparts = [((p or "—")[:18], float(c or 0), cols[i]) for i, (p, _, c) in enumerate(by_cost[:5]) if c]
    rest = sum(float(c or 0) for _, _, c in by_cost[5:])
    if rest:
        dparts.append(("Other", rest, "grey"))
    donut = (dw.donut("Value by part", 4, dparts, dw.inr(sum(v for _, v, _ in dparts)), "estimated", kind="inr")
             if dparts else dw.insight("Value by part", 4, [("i", "No priced part lines on estimates yet.", "slate")]))
    big = (await db.execute(select(PartEstimate.lot_number, PartEstimate.grade, PartEstimate.total_products, PartEstimate.total_estimation)
                            .where(PartEstimate.created_at >= d90).order_by(desc(PartEstimate.total_estimation)).limit(7))).all()
    big_t = dw.table("Biggest estimates", 5, [("Lot", 0.9), ("Grade", 0.6), ("Devices", 0.6), ("Value", 0.8)],
                     [[ln or "—", g or "—", dw.num(n or 0), dw.inr(float(v or 0))] for ln, g, n, v in big] or [["No estimates in 90 days", "", "", ""]],
                     sub="Last 90 days")
    pna_where = [DevicePNAPart.is_active == True, Device.is_trashed == False,  # noqa: E712
                 Device.current_stage.in_([DeviceStage.l1, DeviceStage.l2, DeviceStage.l3])]
    pna_parts = (await db.execute(select(DevicePNAPart.part_name, func.count(func.distinct(DevicePNAPart.device_id)))
                                  .join(Device, Device.id == DevicePNAPart.device_id).where(*pna_where)
                                  .group_by(DevicePNAPart.part_name).order_by(desc(func.count(func.distinct(DevicePNAPart.device_id)))).limit(6))).all()
    pna_w = (dw.hbars("Parts tags are waiting for", 3, [((p or "—")[:16], c) for p, c in pna_parts], color="amber", lw="90px", sub="Active PNA, tags")
             if pna_parts else dw.insight("Parts tags are waiting for", 3, [("✓", "No tags waiting for parts.", "green")]))
    pm = (await db.execute(select(DevicePNAPart.part_name, Device.model, func.count(func.distinct(Device.id)))
                           .join(Device, Device.id == DevicePNAPart.device_id).where(*pna_where)
                           .group_by(DevicePNAPart.part_name, Device.model))).all()
    models_ = {}
    for _, m, c in pm:
        models_[m or "—"] = models_.get(m or "—", 0) + c
    top_models = sorted(models_, key=lambda k: -models_[k])[:7]
    grid = {}
    for p, m, c in pm:
        grid[(p, m or "—")] = c
    top_parts = [p for p, _ in pna_parts][:6]
    heat = (dw.heat("PNA part × model", 12, [m[:16] for m in top_models], [((p or "—")[:16], [grid.get((p, m), 0) for m in top_models]) for p in top_parts],
                    color="violet", lw="110px", sub="Tags waiting for each part, by model")
            if top_parts and top_models else dw.insight("PNA part × model", 12, [("✓", "No tags waiting for parts.", "green")]))
    widgets = dw.layout([(360, [trend, top]), (380, [donut, big_t, pna_w]), (360, [heat])])
    return kpis, widgets
