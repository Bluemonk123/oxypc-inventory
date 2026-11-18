"""Inventory Board (/devices) — Option C dashboard (2026-11-14, Pankaj).

KPIs, where every device is, device types, top brands, As-Is lots, brand x
grade, and "Recently added" grouped by model + grade (View opens the tags —
/devices/recent-tags). Uses the page's own filters (the list passed in is
_device_search_filters(); queries join Lot like the page does).

As-Is: a device on a sub-lot (Device.sub_lot_number set) — sold in the same
condition it was bought, the same rule the Sales Coordinator export uses.
"""
from datetime import datetime, timedelta

from sqlalchemy import select, func, desc, literal_column

from models.device import Device, DeviceStage, STAGE_LABELS
from models.lot import Lot
from models.sales import Sale
from services import dash_widgets as dw
from utils.timezone import app_now

REPAIR = {"l1", "l2", "l3", "trc_production", "qc_check", "cosmetic_received", "cleaning", "putty", "dry_sanding",
          "masking", "painting", "water_sanding", "cosmetic_completed"}
FINAL_QC = {"final_qc", "final_qc_pass_hold", "final_qc_fail_hold"}
GONE = {"sold", "returned", "scrapped", "scrap_for_sale", "on_rent"}
AS_IS = func.coalesce(func.trim(Device.sub_lot_number), literal_column("''")) != literal_column("''")
CATEGORY = func.coalesce(func.nullif(func.trim(Device.device_type), literal_column("''")), Device.sub_category, literal_column("'Other'"))
BRAND = func.coalesce(func.nullif(func.upper(func.trim(Device.brand)), literal_column("''")), literal_column("'—'"))


def _sv(s):
    return getattr(s, "value", s) if s is not None else None


async def build(db, filters, sold_filters):
    """filters: the page's filter list; sold_filters: the same without "exclude sold"."""
    base = lambda *cols: select(*cols).select_from(Device).join(Lot, Device.lot_id == Lot.id).where(  # noqa: E731
        Device.is_trashed == False, *filters)  # noqa: E712
    now = app_now()
    month_start = datetime(now.year, now.month, 1)

    stage_rows = (await db.execute(base(Device.current_stage, AS_IS, func.count()).group_by(Device.current_stage, AS_IS))).all()
    total = sum(c for _, _, c in stage_rows)
    by_stage, as_is_stock = {}, 0
    for st, asis, c in stage_rows:
        k = _sv(st) or "none"
        by_stage[k] = by_stage.get(k, 0) + c
        if asis and k not in GONE:
            as_is_stock += c
    in_stock = sum(c for k, c in by_stage.items() if k not in GONE and k != "grn")
    ready = by_stage.get("ready_to_sale", 0)
    scrapped = by_stage.get("scrapped", 0) + by_stage.get("scrap_for_sale", 0)
    sold_mtd = (await db.execute(select(func.count(Sale.id)).select_from(Sale).join(Device, Device.id == Sale.device_id)
                                 .join(Lot, Device.lot_id == Lot.id)
                                 .where(Device.is_trashed == False, Sale.sold_at >= month_start, *sold_filters))).scalar() or 0  # noqa: E712
    as_is_lots_n = (await db.execute(base(func.count(func.distinct(Device.lot_id))).where(AS_IS))).scalar() or 0

    kpis = [dw.kpi("Total devices", dw.num(total), "with these filters", True, "blue"),
            dw.kpi("In stock", dw.num(in_stock), "not sold / scrapped", True, "cyan"),
            dw.kpi("Ready", dw.num(ready), ("%d%% of stock" % round(ready * 100 / in_stock)) if in_stock else "", True, "green"),
            dw.kpi("Sold this month", dw.num(sold_mtd), now.strftime("%B"), True, "indigo"),
            dw.kpi("As-Is stock", dw.num(as_is_stock), "%d lots" % as_is_lots_n, True, "amber"),
            dw.kpi("Scrapped", dw.num(scrapped), "incl. scrap for sale", False, "red")]

    groups = [("IQC", ["grn", "iqc"], "cyan"), ("Stock In", ["stock_in"], "slate"), ("Repair", sorted(REPAIR), "violet"),
              ("Final QC", sorted(FINAL_QC), "indigo"), ("Ready", ["ready_to_sale"], "green"), ("On rent", ["on_rent"], "teal")]
    parts = [(lbl, sum(by_stage.get(k, 0) for k in ks), col) for lbl, ks, col in groups]
    parts.append(("As-Is (no repair)", as_is_stock, "amber"))
    parts = [p for p in parts if p[1]]
    stack = (dw.stack("Where every device in stock is", 12, parts, sub="In-stock devices by stage · As-Is also counted in its stage")
             if parts else dw.insight("Where every device in stock is", 12, [("i", "No devices for these filters.", "slate")]))

    in_stock_where = [Device.current_stage.notin_([DeviceStage(s) for s in GONE])]
    # By device type / Brand x grade cards hidden (2026-11-18, Pankaj).
    brands = (await db.execute(base(BRAND, func.count()).where(*in_stock_where)
                               .group_by(BRAND).order_by(desc(func.count())).limit(6))).all()
    brand_w = (dw.hbars("Top brands", 3, [(b, c) for b, c in brands], color="blue", lw="80px", sub="In stock")
               if brands else dw.insight("Top brands", 3, [("i", "Nothing in stock.", "slate")]))

    # One row per As-Is sub-lot (Device.sub_lot_number), newest first.
    base_all = lambda *cols: select(*cols).select_from(Device).join(Lot, Device.lot_id == Lot.id).where(  # noqa: E731
        Device.is_trashed == False, *sold_filters)  # noqa: E712  (sold kept, so Sold counts)
    asis_rows = (await db.execute(
        base_all(Device.sub_lot_number, func.max(Lot.supplier_name), func.count(Device.id),
             func.count().filter(Device.current_stage == DeviceStage.sold))
        .where(AS_IS).group_by(Device.sub_lot_number).order_by(desc(func.max(Device.created_at))).limit(7))).all()
    asis_table = dw.table("As-Is lots", 4, [("As-Is lot", 0.9), ("Supplier", 1.4), ("Units", 0.6), ("Sold", 0.6), ("Status", 0.9)],
                          [[ln or "—", sup or "—", dw.num(n), dw.num(s or 0),
                            ("Sold out", "green") if (s or 0) >= n else (("Selling", "blue") if s else ("Not sold yet", "amber"))]
                           for ln, sup, n, s in asis_rows] or [["—", "No As-Is lots", "", "", ""]],
                          sub="Sold in the same condition as bought — no repair", tag="As-Is", tagc="amber")

    since = now - timedelta(days=1)
    recent = (await db.execute(
        base(Device.model, Device.grade, func.count(), func.max(Device.current_stage), func.max(Device.created_at))
        .where(Device.created_at >= since)
        .group_by(Device.model, Device.grade).order_by(desc(func.max(Device.created_at))).limit(8))).all()
    if not recent:      # nothing in 24h: the latest day that had additions
        last = (await db.execute(base(func.max(Device.created_at)))).scalar()
        if last:
            since = last - timedelta(days=1)
            recent = (await db.execute(
                base(Device.model, Device.grade, func.count(), func.max(Device.current_stage), func.max(Device.created_at))
                .where(Device.created_at >= since)
                .group_by(Device.model, Device.grade).order_by(desc(func.max(Device.created_at))).limit(8))).all()
    gcol = {"A": "green", "B": "blue", "C": "amber", "D": "red", "E": "red", "scrap": "slate"}
    rows = []
    for model, g, c, st, _at in recent:
        gv = _sv(g) or ""
        rows.append([model or "—", dw.num(c), (gv or "—", gcol.get(gv, "slate")),
                     STAGE_LABELS.get(st, _sv(st) or "—") if st is not None else "—",
                     {"x": "View", "kind": "btn", "modal": "#recentTagsModal",
                      "data": {"model": model or "", "grade": gv, "since": since.strftime("%Y-%m-%dT%H:%M:%S")}}])
    recent_w = dw.table("Recently added", 5, [("Model", 1.7), ("Tags", 0.6), ("Grade", 0.6), ("Stage", 0.9), ("", 0.7)],
                        rows or [["Nothing added recently", "", "", "", ""]],
                        sub="Since %s · one row per model + grade" % since.strftime("%d %b %H:%M"))
    # One row: Top brands | As-Is lots | Recently added (2026-11-18).
    widgets = dw.layout([(170, [stack]), (430, [brand_w, asis_table, recent_w])])
    return kpis, widgets
