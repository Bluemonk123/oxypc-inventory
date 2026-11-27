"""WorkID Status (/workid-status) — Option C dashboard (2026-11-14, Pankaj).

2026-11-27 (Pankaj): the whole dashboard counts TAGS, each tag once, and uses
the same tags as the View Data cards:
  * Pending / Pending by stage — the cards' Pending (tags sitting on L1/L2,
    L3/L4, Stress, Cosmetic, Final QC on the last date of the filter)
  * In work — the tags sitting on those stages now; their age is how long
    they have been in their current stage (last Asset History move into it)
  * Overdue — in work longer than the stage's Max days in stage (Production
    Board targets; 3 days when not set)
  * PNA — in-work tags holding a PNA part
  * Scrap / Return — the cards' Scrap (L1/L2 + L3/L4) / Returned Pending
  * Completed today / On time — tags whose stage WorkID finished (each tag
    once per stage)
No External Partner Test, trashed (tag or lot) or inactive tags anywhere.
"""
from datetime import datetime, timedelta

from sqlalchemy import select, func

from models.device import Device, DeviceStage, StageMovement
from models.lot import Lot
from models.pna_part import DevicePNAPart
from models.work_order import WorkOrder
from services import dash_widgets as dw
from services.production_dash import load_targets
from utils.stage_page_filters import live_tag_filters
from utils.timezone import app_now

# (label, card key, Production Board target key, colour, device stages)
CARD_GROUPS = [
    ("L1/L2", "l1l2", "l1l2", "amber", ("l1",)),
    ("L3/L4", "l3l4", "l3l4", "orange", ("l3",)),
    ("Stress", "stress", "stress", "pink", ("qc_check",)),
    ("Cosmetic", "cosmetic", "cosmetic", "violet",
     ("cosmetic_received", "cleaning", "putty", "dry_sanding", "masking", "painting", "water_sanding",
      "cosmetic_completed")),
    ("Final QC", "final_qc", "fqc", "blue", ("final_qc",)),
]
# WorkID stage codes per card group (for Completed today / On time).
WO_STAGES = {"l1l2": ["l1", "l2"], "l3l4": ["l3"], "stress": ["qc", "strs", "qc_ch"],
             "cosmetic": ["recv", "clean", "putty", "drysd", "mask", "paint", "water"], "final_qc": ["fqc", "final"]}
OPEN = ["pending", "in_progress"]


def _live(q):
    """Only WorkIDs of tags the pages show: no External Partner Test, trashed
    (tag or lot) or inactive tags."""
    return (q.join(Device, WorkOrder.device_id == Device.id)
             .outerjoin(Lot, Device.lot_id == Lot.id).where(*live_tag_filters()))


def _chunks(ids, n=5000):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


async def build(db, stage_tiles=None):
    """stage_tiles: the View Data cards (routers/workid_status.py)."""
    now = app_now()
    day0 = datetime.combine(now.date(), datetime.min.time())
    targets = await load_targets(db)
    group_of = {}
    for lbl, key, tkey, col, stages in CARD_GROUPS:
        for st in stages:
            group_of[st] = key
    label = {key: lbl for lbl, key, *_ in CARD_GROUPS}
    maxd = {key: ((targets.get(tkey) or {}).get("max_days") or 3) for _, key, tkey, *_ in CARD_GROUPS}

    # Tags in work now: sitting on a card stage (same rule as the cards / pages).
    devs = (await db.execute(
        select(Device.id, Device.barcode, Device.current_stage)
        .outerjoin(Lot, Device.lot_id == Lot.id)
        .where(Device.current_stage.in_([DeviceStage(s) for s in group_of]), *live_tag_filters())
    )).all()
    ids = [d.id for d in devs]
    since, wo, pna_ids = {}, {}, set()
    for part in _chunks(ids):
        # when each tag last moved into the stage it is in now
        for did, st, at in (await db.execute(
                select(StageMovement.device_id, StageMovement.to_stage, func.max(StageMovement.moved_at))
                .where(StageMovement.device_id.in_(part))
                .group_by(StageMovement.device_id, StageMovement.to_stage))).all():
            since[(did, st)] = at
        # its current (latest) open WorkID: Work ID + owner for the Overdue list
        for did, wid, nm, un, at in (await db.execute(
                select(WorkOrder.device_id, WorkOrder.work_id, WorkOrder.assigned_name, WorkOrder.assigned_username,
                       func.coalesce(WorkOrder.assigned_at, WorkOrder.created_at))
                .where(WorkOrder.device_id.in_(part), WorkOrder.status.in_(OPEN), WorkOrder.stage != "asgn"))).all():
            if did not in wo or (at and (wo[did][3] is None or at > wo[did][3])):
                wo[did] = (wid, nm, un, at)
        pna_ids.update((await db.execute(select(DevicePNAPart.device_id).where(
            DevicePNAPart.device_id.in_(part), DevicePNAPart.is_active == True))).scalars().all())  # noqa: E712

    items = []
    for did, bc, st in devs:
        key = group_of[st.value]
        at = since.get((did, st))
        age = (now - at).total_seconds() / 86400 if at else 0
        w = wo.get(did) or (None, None, None, None)
        items.append(dict(bc=bc, key=key, lbl=label[key], age=age, wid=w[0], who=w[1] or w[2] or "—",
                          pna=did in pna_ids, over=age > maxd[key]))
    n_open = len(items)
    n_over = sum(1 for x in items if x["over"])
    n_pna = sum(1 for x in items if x["pna"])

    # Finished: each tag once (today) / once per stage (last 7 days).
    wo_key = {code: key for key, codes in WO_STAGES.items() for code in codes}
    all_codes = list(wo_key)
    done_today = (await db.execute(_live(select(func.count(func.distinct(WorkOrder.device_id))))
                                   .where(WorkOrder.completed_at >= day0, WorkOrder.stage.in_(all_codes)))).scalar() or 0
    started = func.coalesce(WorkOrder.assigned_at, WorkOrder.created_at)
    recent = (await db.execute(_live(select(WorkOrder.device_id, WorkOrder.stage, started, WorkOrder.completed_at))
                               .where(WorkOrder.completed_at >= now - timedelta(days=7),
                                      WorkOrder.stage.in_(all_codes)))).all()
    last = {}
    for did, code, at, done in recent:
        k = (did, wo_key[code])
        if k not in last or (done and done > last[k][1]):
            last[k] = (at, done)
    ontime = {key: [0, 0] for _, key, *_ in CARD_GROUPS}
    for (did, key), (at, done) in last.items():
        ontime[key][1] += 1
        if at and done and (done - at).total_seconds() / 86400 <= maxd[key]:
            ontime[key][0] += 1
    tot_ok, tot_n = sum(v[0] for v in ontime.values()), sum(v[1] for v in ontime.values())
    pct = tot_ok * 100 / tot_n if tot_n else 0

    # From the cards: Pending, Scrap, Returned.
    card = lambda k, f: int(((stage_tiles or {}).get(k) or {}).get(f, 0) or 0)   # noqa: E731
    pend = {key: card(key, "pending") for _, key, *_ in CARD_GROUPS}
    n_pend = sum(pend.values())
    n_scrap = card("l1l2", "scrap") + card("l3l4", "scrap")
    n_ret = card("returned", "pending")

    kpis = [dw.kpi("Pending", dw.num(n_pend), "tags on the repair stages", True, "pink"),
            dw.kpi("Overdue", dw.num(n_over), ("%d%% of %s tags in work" % (round(n_over * 100 / n_open), dw.num(n_open)))
                   if n_open else "", not n_over, "red"),
            dw.kpi("Completed today", dw.num(done_today), "tags", True, "green"),
            dw.kpi("PNA", dw.num(n_pna), "tags waiting for parts", not n_pna, "violet"),
            dw.kpi("Scrap / Return", "%s / %s" % (dw.num(n_scrap), dw.num(n_ret)), "scrapped / returned tags",
                   not (n_scrap or n_ret), "slate"),
            dw.kpi("On time", ("%d%%" % round(pct)) if tot_n else "—", "tags finished last 7 days", pct >= 85, "blue")]

    parts = [(lbl, pend[key], col) for lbl, key, _t, col, _s in CARD_GROUPS if pend[key]]
    stack = (dw.stack("Pending by stage", 12, parts, sub="Same as the cards in View Data: tags sitting on each stage on the last date")
             if parts else dw.insight("Pending by stage", 12, [("✓", "Nothing pending.", "green")]))
    cats = [g[0] for g in CARD_GROUPS] + ["PNA"]
    in_cat = lambda x, c: x["pna"] if c == "PNA" else x["lbl"] == c            # noqa: E731
    band = lambda x: 0 if x["age"] < 1 else (1 if x["age"] <= 3 else 2)         # noqa: E731
    ages = [[sum(1 for x in items if in_cat(x, c) and band(x) == b) for c in cats] for b in range(3)]
    age_w = dw.bars("Age by stage", 8, cats, [("< 1 day", ages[0], "green"), ("1-3 days", ages[1], "amber"),
                                              ("> 3 days", ages[2], "red")],
                    stacked=True, bmax="40px", sub="Tags in work, by how long they have been in their stage")
    gauge = dw.gauge("On-time", 4, pct, ("%d%%" % round(pct)) if tot_n else "—", "tags finished within max days", "pink",
                     [("Finished 7d", dw.num(tot_n)), ("Overdue now", dw.num(n_over)), ("In work", dw.num(n_open))])
    over = sorted([x for x in items if x["over"]], key=lambda x: -x["age"])[:7]
    over_t = dw.table("Overdue tags", 7, [("Tag", 1.1), ("Work ID", 1), ("Stage", 0.8), ("Owner", 0.9), ("In stage", 0.7)],
                      [[{"x": x["bc"] or "—", "kind": "link", "href": "/devices/%s" % (x["bc"] or "")}, x["wid"] or "—",
                        x["lbl"] + (" · PNA" if x["pna"] else ""), x["who"],
                        ("%dd" % x["age"], "red" if x["age"] > 2 * maxd[x["key"]] else "amber")] for x in over]
                      or [["No overdue tags", "", "", "", ""]], sub="Longest in stage first, each tag once")
    rings = dw.rings("On time by stage", 5, [(lbl, (ontime[key][0] * 100 / ontime[key][1]) if ontime[key][1] else 0, col,
                                             ("%d%%" % round(ontime[key][0] * 100 / ontime[key][1])) if ontime[key][1] else "—")
                                            for lbl, key, _t, col, _s in CARD_GROUPS], sub="Tags finished in the last 7 days")
    widgets = dw.layout([(170, [stack]), (400, [age_w, gauge]), (400, [over_t, rings])])
    return kpis, widgets
