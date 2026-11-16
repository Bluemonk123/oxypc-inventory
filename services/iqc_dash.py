"""IQC (/iqc) — Option C dashboard (2026-11-14, Pankaj).

No pass / fail at IQC: every incoming item (laptops, desktops, monitors,
accessories…) is checked and given a grade. Shows items graded, the grade
split, inspector x hour, what pulled grades down (defects noted on C / D / E
items), grade by category and the IQC queue.

Window: the last 7 days of inspections (30 days when the week had fewer than
20; the latest day with any when there were none). "—" = graded later.
"""
from datetime import datetime, timedelta

from sqlalchemy import select, func, desc, literal_column

from models.device import Device, DeviceStage
from models.iqc_inspection import IQCInspection
from models.lot import Lot
from services import dash_widgets as dw
from utils.timezone import app_now

GRADES = [("A", "green"), ("B", "blue"), ("C", "amber"), ("D", "red"), ("E", "violet")]
DEFECTS = [("Screen lines", "screen_line"), ("Screen dots", "screen_dot"), ("Screen scratch", "screen_scratch"),
           ("Screen broken", "screen_broken"), ("Lid scratch", "panel_a_scratch"), ("Lid dent", "panel_a_dent"),
           ("Palm rest scratch", "panel_c_scratch"), ("Base broken", "panel_d_broken"), ("Key missing", "keyboard_key_missing"),
           ("Touchpad scratch", "touchpad_scratch"), ("Hinge broken", "screen_hinge_broken"), ("Colour fade", "panel_a_colour_fade")]
CATEGORY = func.coalesce(func.nullif(func.trim(Device.device_type), literal_column("''")), Device.sub_category, literal_column("'Other'"))
HOURS = list(range(9, 20))


def _g(v):
    return getattr(v, "value", v) if v is not None else None


async def build(db):
    now = app_now()
    start = now - timedelta(days=7)
    n7 = (await db.execute(select(func.count(IQCInspection.id)).where(IQCInspection.inspected_at >= start))).scalar() or 0
    label = "last 7 days"
    if n7 < 20:          # quiet week: look at 30 days so the charts mean something
        n30 = (await db.execute(select(func.count(IQCInspection.id)).where(IQCInspection.inspected_at >= now - timedelta(days=30)))).scalar() or 0
        if n30 > n7:
            start, label, n7 = now - timedelta(days=30), "last 30 days", n30
    if not n7:
        last = (await db.execute(select(func.max(IQCInspection.inspected_at)))).scalar()
        if last:
            start = datetime.combine(last.date(), datetime.min.time())
            label = last.strftime("%d %b")
    win = [IQCInspection.inspected_at >= start]

    grade_rows = (await db.execute(select(Device.grade, func.count(IQCInspection.id)).join(Device, Device.id == IQCInspection.device_id)
                                   .where(*win).group_by(Device.grade))).all()
    gc = {}
    for g, c in grade_rows:
        gc[_g(g) or "—"] = gc.get(_g(g) or "—", 0) + c
    total = sum(gc.values())
    share = lambda *ks: (sum(gc.get(k, 0) for k in ks) * 100 / total) if total else 0   # noqa: E731
    inspectors = (await db.execute(select(func.count(func.distinct(IQCInspection.inspector_name))).where(*win))).scalar() or 0
    queue_n = (await db.execute(select(func.count(Device.id)).where(Device.is_trashed == False,  # noqa: E712
                                                                     Device.current_stage.in_([DeviceStage.grn, DeviceStage.iqc])))).scalar() or 0
    oldest = (await db.execute(select(func.min(Device.created_at)).where(Device.is_trashed == False,  # noqa: E712
                                                                           Device.current_stage.in_([DeviceStage.grn, DeviceStage.iqc])))).scalar()
    kpis = [dw.kpi("Graded", dw.num(total), label, True, "cyan"),
            dw.kpi("Grade A", "%d%%" % round(share("A")), dw.num(gc.get("A", 0)) + " items", True, "green"),
            dw.kpi("Grade B", "%d%%" % round(share("B")), dw.num(gc.get("B", 0)) + " items", True, "blue"),
            dw.kpi("Grade C / D / E", "%d%%" % round(share("C", "D", "E")), dw.num(sum(gc.get(k, 0) for k in "CDE")) + " items", False, "amber"),
            dw.kpi("Inspectors", dw.num(inspectors), label, True, "violet"),
            dw.kpi("Queue", dw.num(queue_n), ("oldest %d days" % (now - oldest).days) if oldest else "waiting for IQC", queue_n == 0, "red")]

    hour = func.extract("hour", IQCInspection.inspected_at)
    hg = (await db.execute(select(hour, Device.grade, func.count(IQCInspection.id)).join(Device, Device.id == IQCInspection.device_id)
                           .where(*win).group_by(hour, Device.grade))).all()
    by = {}
    for h, g, c in hg:
        by[(int(h), _g(g) or "—")] = by.get((int(h), _g(g) or "—"), 0) + c
    hrs = HOURS
    if total:
        bars = dw.bars("Items graded by hour", 8, ["%d%s" % ((h - 1) % 12 + 1, "a" if h < 12 else "p") for h in hrs],
                       [(g, [by.get((h, g), 0) for h in hrs], col) for g, col in GRADES + [("—", "grey")]], stacked=True, bmax="40px",
                       sub="By grade · %s" % label)
        donut = dw.donut("Grade split", 4, [(g, gc.get(g, 0), col) for g, col in GRADES if gc.get(g)] +
                         ([("No grade", gc["—"], "grey")] if gc.get("—") else []), dw.num(total), "graded")
    else:
        bars = dw.insight("Items graded by hour", 8, [("i", "No inspections yet.", "slate")])
        donut = dw.insight("Grade split", 4, [("i", "No inspections yet.", "slate")])

    ih = (await db.execute(select(func.coalesce(IQCInspection.inspector_name, literal_column("'—'")), hour, func.count(IQCInspection.id))
                           .where(*win).group_by(func.coalesce(IQCInspection.inspector_name, literal_column("'—'")), hour))).all()
    insp = {}
    for name, h, c in ih:
        insp.setdefault(name, {})[int(h)] = c
    top_insp = sorted(insp, key=lambda k: -sum(insp[k].values()))[:6]
    heat = (dw.heat("Inspector × hour", 7, ["%d%s" % ((h - 1) % 12 + 1, "a" if h < 12 else "p") for h in hrs[:9]],
                    [(n[:14], [insp[n].get(h, 0) for h in hrs[:9]]) for n in top_insp], color="cyan", lw="100px",
                    sub="Items graded · %s" % label) if top_insp else dw.insight("Inspector × hour", 7, [("i", "No inspections yet.", "slate")]))

    low = [Device.grade.in_(["C", "D", "E"])]
    dq = select(*[func.count().filter(getattr(IQCInspection, col).in_(["Yes", "Minor", "Major"])) for _, col in DEFECTS]).join(
        Device, Device.id == IQCInspection.device_id).where(IQCInspection.inspected_at >= now - timedelta(days=30), *low)
    dvals = (await db.execute(dq)).one()
    defects = sorted([(lbl, int(v or 0)) for (lbl, _), v in zip(DEFECTS, dvals) if v], key=lambda x: -x[1])[:6]
    defects_w = (dw.hbars("What pulled grades down", 5, defects, color="amber", lw="120px", sub="Defects noted on C / D / E items, 30 days")
                 if defects else dw.insight("What pulled grades down", 5, [("i", "No defects noted on C / D / E items in 30 days.", "slate")]))

    cg = (await db.execute(select(CATEGORY, Device.grade, func.count(IQCInspection.id)).join(Device, Device.id == IQCInspection.device_id)
                           .where(IQCInspection.inspected_at >= now - timedelta(days=30)).group_by(CATEGORY, Device.grade))).all()
    cmap = {}
    for cat, g, c in cg:
        cmap.setdefault(cat or "Other", {})[_g(g) or "—"] = c
    cats = sorted(cmap, key=lambda k: -sum(cmap[k].values()))[:6]
    cat_heat = (dw.heat("Grade by category", 5, [g for g, _ in GRADES], [(c[:14], [cmap[c].get(g, 0) for g, _ in GRADES]) for c in cats],
                        color="cyan", lw="100px", sub="Every item type is graded · 30 days")
                if cats else dw.insight("Grade by category", 5, [("i", "No inspections in 30 days.", "slate")]))

    qrows = (await db.execute(select(Device.barcode, CATEGORY, Device.model, Lot.lot_number, Device.created_at)
                              .join(Lot, Lot.id == Device.lot_id)
                              .where(Device.is_trashed == False, Device.current_stage.in_([DeviceStage.grn, DeviceStage.iqc]))  # noqa: E712
                              .order_by(Device.created_at.asc()).limit(7))).all()
    def waited(at):
        if not at:
            return "—", ("—", "slate")
        d = (now - at).days
        return ("%dd" % d if d else "%dh" % max(1, (now - at).seconds // 3600)), (("Old", "red") if d >= 2 else (("Today", "blue") if d == 0 else ("Waiting", "amber")))
    rows = []
    for tag, cat, model, lot, at in qrows:
        w, pr = waited(at)
        rows.append([{"x": tag, "kind": "link", "href": "/devices/%s" % tag}, cat or "—", model or "—", lot or "—", w, pr])
    queue = dw.table("Queue", 7, [("Tag", 1.1), ("Category", 0.8), ("Model", 1.4), ("Lot", 0.7), ("Waiting", 0.6), ("Status", 0.7)],
                     rows or [["Nothing waiting for IQC", "", "", "", "", ""]], sub="Oldest first", tag="%s waiting" % dw.num(queue_n), tagc="red")
    widgets = dw.layout([(360, [bars, donut]), (390, [heat, defects_w]), (390, [cat_heat, queue])])
    return kpis, widgets
