"""Production Board (/trc-production) — Option C dashboard (2026-11-14, Pankaj).

Repair board by stage — L1/L2, L3/L4, Stress, Cosmetic, Final QC, Pre-Sale QC
— each with today's output against its daily target, plus PNA / Scrap /
Return, output vs target, engineer leaderboard and turnaround by stage.

Stage counts use the page's own tile rules (routers/stock.py
_prod_tile_criteria) so the numbers match its tiles. "Done today" = tags that
left the stage today (StageMovement.from_stage). Targets are kept in the
app_settings table (key "prod_targets", JSON) and set from the Set targets
modal (POST /trc-production/targets).
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import select, func, desc

from models.device import Device, DeviceStage, StageMovement
from models.pna_part import DevicePNAPart
from models.settings import AppSetting
from models.work_order import WorkOrder
from services import dash_widgets as dw
from utils.timezone import app_now

COSMETIC = [DeviceStage.cosmetic_received, DeviceStage.cleaning, DeviceStage.putty, DeviceStage.dry_sanding,
            DeviceStage.masking, DeviceStage.painting, DeviceStage.water_sanding, DeviceStage.cosmetic_completed]
FINAL_QC = [DeviceStage.final_qc, DeviceStage.final_qc_pass_hold, DeviceStage.final_qc_fail_hold]
STAGES = [  # code, label, colour, tile key, stages a tag leaves when it is "done" here
    ("l1l2", "L1/L2", "amber", "l1-l2", [DeviceStage.l1, DeviceStage.l2]),
    ("l3l4", "L3/L4", "orange", "l3-l4", [DeviceStage.l3]),
    ("stress", "Stress", "pink", "stress", [DeviceStage.qc_check]),
    ("cosmetic", "Cosmetic", "violet", "cosmetic", COSMETIC),
    ("fqc", "Final QC", "blue", "final-qc", FINAL_QC),
    ("psqc", "Pre-Sale QC", "green", None, []),
]
DEFAULT_TARGETS = {"l1l2": 140, "l3l4": 40, "stress": 70, "cosmetic": 60, "fqc": 110, "psqc": 45}
TARGET_KEY = "prod_targets"


async def load_targets(db) -> dict:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == TARGET_KEY))).scalar_one_or_none()
    out = {k: {"daily": v, "weekly": v * 6, "per_engineer": 0, "max_days": 0} for k, v in DEFAULT_TARGETS.items()}
    if row and row.value:
        try:
            for k, v in (json.loads(row.value) or {}).items():
                if k in out and isinstance(v, dict):
                    out[k].update({kk: int(vv) for kk, vv in v.items() if str(vv).strip().lstrip("-").isdigit()})
        except (ValueError, TypeError):
            pass
    return out


async def build(db, tile_criteria, pna_count, returned_count):
    from routers.pre_sale_iqc import STAGE_CODE
    now = app_now()
    day0 = datetime.combine(now.date(), datetime.min.time())
    targets = await load_targets(db)

    counts, chips, done = {}, {}, {}
    for code, label, col, key, left in STAGES:
        if key:
            crit = tile_criteria[key][1]
            counts[code] = (await db.execute(select(func.count(Device.id)).where(crit))).scalar() or 0
            rows = (await db.execute(select(Device.barcode, Device.model, Device.updated_at).where(crit)
                                     .order_by(Device.updated_at.asc()).limit(2))).all()
            chips[code] = [("%s · %s" % (b, m or ""), "since %s" % (u.strftime("%d %b") if u else "—")) for b, m, u in rows]
            done[code] = (await db.execute(select(func.count(func.distinct(StageMovement.device_id)))
                                           .where(StageMovement.from_stage.in_(left), StageMovement.moved_at >= day0))).scalar() or 0
        else:   # Pre-Sale QC: open WorkIDs / completed today
            counts[code] = (await db.execute(select(func.count(WorkOrder.id)).where(
                WorkOrder.stage == STAGE_CODE, WorkOrder.status.in_(["pending", "in_progress"])))).scalar() or 0
            rows = (await db.execute(select(Device.barcode, Device.model, WorkOrder.notes).join(Device, Device.id == WorkOrder.device_id)
                                     .where(WorkOrder.stage == STAGE_CODE, WorkOrder.status.in_(["pending", "in_progress"]))
                                     .order_by(WorkOrder.created_at.asc()).limit(2))).all()
            chips[code] = [("%s · %s" % (b, m or ""), (n or "Pending")[:50]) for b, m, n in rows]
            done[code] = (await db.execute(select(func.count(WorkOrder.id)).where(
                WorkOrder.stage == STAGE_CODE, WorkOrder.completed_at >= day0))).scalar() or 0
    total_done = sum(done.values())
    total_target = sum(targets[c]["daily"] for c, *_ in STAGES)
    wip = sum(counts[c] for c in ("l1l2", "l3l4", "stress", "cosmetic", "fqc"))
    week0 = now - timedelta(days=7)
    scrap_n = (await db.execute(select(func.count(func.distinct(StageMovement.device_id))).where(
        StageMovement.to_stage.in_([DeviceStage.scrapped, DeviceStage.scrap_for_sale]), StageMovement.moved_at >= week0))).scalar() or 0
    engineers = (await db.execute(select(func.count(func.distinct(StageMovement.moved_by))).where(
        StageMovement.moved_at >= day0, StageMovement.from_stage.in_([s for *_, l in STAGES for s in l])))).scalar() or 0

    pct = total_done * 100 / total_target if total_target else 0
    kpis = [dw.kpi("Work in progress", dw.num(wip), "L1 → Final QC", True, "amber"),
            dw.kpi("Completed today", dw.num(total_done), "target %s" % dw.num(total_target), True, "green"),
            dw.kpi("Target reached", "%d%%" % round(pct), "today", pct >= 85, "blue"),
            dw.kpi("PNA (parts not available)", dw.num(pna_count), "in L1/L2", not pna_count, "violet"),
            dw.kpi("Scrap", dw.num(scrap_n), "last 7 days", not scrap_n, "red"),
            dw.kpi("Return", dw.num(returned_count), "back for repair", not returned_count, "slate")]

    board = dw.lanes("Repair board", 12, [(label, col, counts[code], chips[code], (done[code], targets[code]["daily"]))
                                          for code, label, col, key, left in STAGES],
                     sub="Tags in each stage, oldest first · bar = today's output against target", tag="Live", tagc="amber")
    out_vs = dw.bars("Output vs target by stage", 8, [s[1] for s in STAGES],
                     [("Done today", [done[c] for c, *_ in STAGES], "amber"), ("Target", [targets[c]["daily"] for c, *_ in STAGES], "slate")],
                     bmax="38px", sub="Tags today")
    pna_rows = (await db.execute(select(Device.barcode, Device.model).where(
        Device.current_stage.in_([DeviceStage.l1, DeviceStage.l2]), Device.is_trashed == False,  # noqa: E712
        Device.id.in_(select(DevicePNAPart.device_id).where(DevicePNAPart.is_active == True)))  # noqa: E712
        .order_by(Device.updated_at.asc()).limit(2))).all() if pna_count else []
    scrap_rows = (await db.execute(select(Device.barcode, Device.model).join(StageMovement, StageMovement.device_id == Device.id)
                                   .where(StageMovement.to_stage.in_([DeviceStage.scrapped, DeviceStage.scrap_for_sale]),
                                          StageMovement.moved_at >= week0).order_by(desc(StageMovement.moved_at)).limit(2))).all()
    ret_rows = (await db.execute(select(Device.barcode, Device.model).where(tile_criteria["returned"][1])
                                 .order_by(Device.updated_at.desc()).limit(2))).all()
    off = dw.lanes("Out of the repair flow", 4, [
        ("PNA", "violet", pna_count, [("%s · %s" % (b, m or ""), "waiting for parts") for b, m in pna_rows]),
        ("Scrap", "red", scrap_n, [("%s · %s" % (b, m or ""), "scrapped this week") for b, m in scrap_rows]),
        ("Return", "slate", returned_count, [("%s · %s" % (b, m or ""), "returned for repair") for b, m in ret_rows])],
        sub="Parts not available · scrapped · sent back")

    eng = (await db.execute(select(StageMovement.moved_by, func.count(func.distinct(StageMovement.device_id)))
                            .where(StageMovement.moved_at >= day0, StageMovement.moved_by.isnot(None),
                                   StageMovement.from_stage.in_([s for *_, l in STAGES for s in l]))
                            .group_by(StageMovement.moved_by).order_by(desc(func.count(func.distinct(StageMovement.device_id)))).limit(6))).all()
    if not eng:     # quiet day: last 7 days
        eng = (await db.execute(select(StageMovement.moved_by, func.count(func.distinct(StageMovement.device_id)))
                                .where(StageMovement.moved_at >= week0, StageMovement.moved_by.isnot(None),
                                       StageMovement.from_stage.in_([s for *_, l in STAGES for s in l]))
                                .group_by(StageMovement.moved_by).order_by(desc(func.count(func.distinct(StageMovement.device_id)))).limit(6))).all()
        eng_sub = "Tags moved on, last 7 days"
    else:
        eng_sub = "Tags moved on today"
    leader = (dw.hbars("Engineer leaderboard", 5, [((u or "—")[:16], c) for u, c in eng], color="amber", lw="110px", sub=eng_sub)
              if eng else dw.insight("Engineer leaderboard", 5, [("i", "No repair moves in the last 7 days.", "slate")]))
    # Turnaround: average days a tag spent in each stage (movements that left it, 30 days).
    tat = []
    for code, label, col, key, left in STAGES[:5]:
        v = (await db.execute(select(func.avg(func.extract("epoch", StageMovement.exited_at - StageMovement.moved_at) / 86400))
                              .where(StageMovement.to_stage.in_(left), StageMovement.exited_at.isnot(None),
                                     StageMovement.exited_at >= now - timedelta(days=30)))).scalar()
        tat.append(float(v or 0))
    tat_w = (dw.bars("Turnaround by stage", 4, [s[1] for s in STAGES[:5]], [("Days", tat, "orange")], kind="d", bmax="28px", legv="",
                     sub="Average days in stage, last 30 days") if any(tat)
             else dw.insight("Turnaround by stage", 4, [("i", "No stage exits recorded in 30 days.", "slate")]))
    tgt = dw.withbtn(dw.prog("Targets today", 3, [(label, "%d / %d" % (done[c], targets[c]["daily"]),
                                                   min(100, done[c] * 100 / targets[c]["daily"]) if targets[c]["daily"] else 0,
                                                   "green" if targets[c]["daily"] and done[c] >= 0.85 * targets[c]["daily"] else "amber")
                                                  for c, label, *_ in STAGES]), "Set targets", modal="#prodTargetsModal")
    widgets = dw.layout([(340, [board]), (380, [out_vs, off]), (400, [leader, tat_w, tgt])])
    return kpis, widgets, targets, STAGES
