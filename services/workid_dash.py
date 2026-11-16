"""WorkID Status (/workid-status) — Option C dashboard (2026-11-14, Pankaj).

Open WorkIDs by repair stage (L1/L2, L3/L4, Stress, Cosmetic, Final QC,
Pre-Sale QC) plus PNA, Scrap and Return; how old they are; overdue list and
on-time rate. "Overdue" = open longer than the stage's Max days in stage
(Production Board targets; 3 days when not set).
"""
from datetime import datetime, timedelta

from sqlalchemy import select, func, desc

from models.device import Device, DeviceStage
from models.pna_part import DevicePNAPart
from models.work_order import WorkOrder
from services import dash_widgets as dw
from services.production_dash import load_targets
from utils.timezone import app_now

GROUPS = [("L1/L2", ["l1", "l2"], "l1l2", "amber"), ("L3/L4", ["l3"], "l3l4", "orange"),
          ("Stress", ["qc", "strs", "qc_ch"], "stress", "pink"),
          ("Cosmetic", ["recv", "clean", "putty", "drysd", "mask", "paint", "water"], "cosmetic", "violet"),
          ("Final QC", ["fqc", "final"], "fqc", "blue"), ("Pre-Sale QC", ["psiqc"], "psqc", "green")]
OPEN = ["pending", "in_progress"]


async def build(db):
    now = app_now()
    day0 = datetime.combine(now.date(), datetime.min.time())
    targets = await load_targets(db)
    maxd = {code: (targets.get(code, {}).get("max_days") or 3) for _, _, code, _ in GROUPS}
    stage_of = {s: (lbl, code) for lbl, ss, code, _ in GROUPS for s in ss}
    all_stages = list(stage_of)
    started = func.coalesce(WorkOrder.assigned_at, WorkOrder.created_at)
    rows = (await db.execute(select(WorkOrder.work_id, WorkOrder.barcode, WorkOrder.stage, WorkOrder.assigned_name,
                                    WorkOrder.assigned_username, started, WorkOrder.device_id)
                             .where(WorkOrder.status.in_(OPEN), WorkOrder.stage.in_(all_stages)))).all()
    pna_ids = set((await db.execute(select(DevicePNAPart.device_id).where(DevicePNAPart.is_active == True)))  # noqa: E712
                  .scalars().all())
    ret_ids = set((await db.execute(select(Device.id).where(Device.tag_return_status == "Return for Repair",
                                                            Device.is_trashed == False))).scalars().all())  # noqa: E712
    scrap_ids = set((await db.execute(select(Device.id).where(Device.current_stage.in_(
        [DeviceStage.scrapped, DeviceStage.scrap_for_sale])))).scalars().all())
    items = []
    for wid, bc, st, nm, un, at, did in rows:
        lbl, code = stage_of[st]
        age = (now - at).total_seconds() / 86400 if at else 0
        items.append(dict(wid=wid, bc=bc, lbl=lbl, code=code, who=nm or un or "—", age=age, did=did,
                          pna=did in pna_ids, ret=did in ret_ids, scrap=did in scrap_ids, over=age > maxd[code]))
    n_open = len(items)
    n_over = sum(1 for x in items if x["over"])
    n_pna = sum(1 for x in items if x["pna"])
    n_ret = sum(1 for x in items if x["ret"])
    n_scrap = sum(1 for x in items if x["scrap"])
    done_today = (await db.execute(select(func.count(WorkOrder.id)).where(WorkOrder.completed_at >= day0,
                                                                          WorkOrder.stage.in_(all_stages)))).scalar() or 0
    # On time: WorkIDs completed in the last 7 days within their stage's max days.
    recent = (await db.execute(select(WorkOrder.stage, started, WorkOrder.completed_at)
                               .where(WorkOrder.completed_at >= now - timedelta(days=7), WorkOrder.stage.in_(all_stages)))).all()
    ontime = {code: [0, 0] for _, _, code, _ in GROUPS}
    for st, at, done in recent:
        code = stage_of[st][1]
        ontime[code][1] += 1
        if at and done and (done - at).total_seconds() / 86400 <= maxd[code]:
            ontime[code][0] += 1
    tot_ok, tot_n = sum(v[0] for v in ontime.values()), sum(v[1] for v in ontime.values())
    pct = tot_ok * 100 / tot_n if tot_n else 0
    kpis = [dw.kpi("Open WorkIDs", dw.num(n_open), "all repair stages", True, "pink"),
            dw.kpi("Overdue", dw.num(n_over), ("%d%% of open" % round(n_over * 100 / n_open)) if n_open else "", not n_over, "red"),
            dw.kpi("Completed today", dw.num(done_today), "WorkIDs", True, "green"),
            dw.kpi("PNA", dw.num(n_pna), "waiting for parts", not n_pna, "violet"),
            dw.kpi("Scrap / Return", "%s / %s" % (dw.num(n_scrap), dw.num(n_ret)), "open WorkIDs", not (n_scrap or n_ret), "slate"),
            dw.kpi("On time", ("%d%%" % round(pct)) if tot_n else "—", "closed last 7 days", pct >= 85, "blue")]

    by = {lbl: 0 for lbl, *_ in GROUPS}
    for x in items:
        by[x["lbl"]] += 1
    parts = [(lbl, by[lbl], col) for lbl, _, _, col in GROUPS if by[lbl]]
    for lbl, n, col in (("PNA", n_pna, "indigo"), ("Scrap", n_scrap, "red"), ("Return", n_ret, "slate")):
        if n:
            parts.append((lbl, n, col))
    stack = (dw.stack("Open WorkIDs by stage", 12, parts, sub="PNA / Scrap / Return are also counted in their repair stage")
             if parts else dw.insight("Open WorkIDs by stage", 12, [("✓", "No open WorkIDs.", "green")]))
    cats = [g[0] for g in GROUPS] + ["PNA", "Scrap", "Return"]
    def in_cat(x, c):
        return x["pna"] if c == "PNA" else x["scrap"] if c == "Scrap" else x["ret"] if c == "Return" else x["lbl"] == c
    band = lambda x: 0 if x["age"] < 1 else (1 if x["age"] <= 3 else 2)   # noqa: E731
    ages = [[sum(1 for x in items if in_cat(x, c) and band(x) == b) for c in cats] for b in range(3)]
    age_w = dw.bars("Age by stage", 8, cats, [("< 1 day", ages[0], "green"), ("1-3 days", ages[1], "amber"), ("> 3 days", ages[2], "red")],
                    stacked=True, bmax="40px", sub="Open WorkIDs")
    gauge = dw.gauge("On-time", 4, pct, ("%d%%" % round(pct)) if tot_n else "—", "closed within max days", "pink",
                     [("Closed 7d", dw.num(tot_n)), ("Overdue now", dw.num(n_over)), ("Open", dw.num(n_open))])
    over = sorted([x for x in items if x["over"]], key=lambda x: -x["age"])[:7]
    over_t = dw.table("Overdue WorkIDs", 7, [("Work ID", 1), ("Tag", 1.1), ("Stage", 0.8), ("Owner", 0.9), ("Open for", 0.7)],
                      [[x["wid"] or "—", {"x": x["bc"] or "—", "kind": "link", "href": "/devices/%s" % (x["bc"] or "")},
                        x["lbl"] + (" · PNA" if x["pna"] else ""), x["who"],
                        ("%dd" % x["age"], "red" if x["age"] > 2 * maxd[x["code"]] else "amber")] for x in over]
                      or [["No overdue WorkIDs", "", "", "", ""]], sub="Longest open first")
    rings = dw.rings("On time by stage", 5, [(lbl, (ontime[code][0] * 100 / ontime[code][1]) if ontime[code][1] else 0, col,
                                             ("%d%%" % round(ontime[code][0] * 100 / ontime[code][1])) if ontime[code][1] else "—")
                                            for lbl, _, code, col in GROUPS], sub="Closed in the last 7 days")
    widgets = dw.layout([(170, [stack]), (400, [age_w, gauge]), (400, [over_t, rings])])
    return kpis, widgets
