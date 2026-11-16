"""Inventory Board (/stock, Stock Inwards) — Option C dashboard (2026-11-14, Pankaj).

What the stock is worth (Device.device_price), where it sits (warehouse),
how old it is, the slowest movers and model x grade. In-stock = not sold /
returned / scrapped / on rent / GRN receipt. Narrowed by the page's Entity,
Device Type and Lot filters.
"""
from datetime import timedelta

from sqlalchemy import select, func, desc, case, literal_column

from models.device import Device, DeviceStage
from models.lot import Lot
from models.sales import Sale
from services import dash_widgets as dw
from utils.timezone import app_now

GONE = [DeviceStage.sold, DeviceStage.returned, DeviceStage.scrapped, DeviceStage.scrap_for_sale, DeviceStage.on_rent, DeviceStage.grn]
CATEGORY = func.coalesce(func.nullif(func.trim(Device.device_type), literal_column("''")), Device.sub_category, literal_column("'Other'"))
WAREHOUSE = func.coalesce(func.nullif(func.trim(Device.warehouse), literal_column("''")), literal_column("'Not set'"))
AS_IS = func.coalesce(func.trim(Device.sub_lot_number), literal_column("''")) != literal_column("''")


async def build(db, entity="", device_type="", lot_number="", can_price=True):
    """can_price: Stock Pricing View tick — without it no money is shown (value charts fall back to units)."""
    now = app_now()
    where = [Device.is_trashed == False, Device.current_stage.notin_(GONE)]  # noqa: E712
    ents = [e.strip() for e in (entity or "").split(",") if e.strip()]
    if ents:
        where.append(Device.entity.in_(ents))
    dts = [d.strip() for d in (device_type or "").split(",") if d.strip()]
    if dts:
        where.append(Device.device_type.in_(dts))
    if lot_number:
        where.append(Device.lot_id.in_(select(Lot.id).where(Lot.lot_number.in_([x.strip() for x in lot_number.split(",") if x.strip()]))))
    price = func.coalesce(Device.device_price, 0) if can_price else literal_column("0")
    money = dw.inr if can_price else (lambda v: "—")
    age = func.extract("epoch", now - Device.created_at) / 86400

    units, value = (await db.execute(select(func.count(Device.id), func.sum(price)).where(*where))).one()
    units, value = units or 0, float(value or 0)
    r_units, r_value = (await db.execute(select(func.count(Device.id), func.sum(price))
                                         .where(*where, Device.current_stage == DeviceStage.ready_to_sale))).one()
    o_units, o_value = (await db.execute(select(func.count(Device.id), func.sum(price))
                                         .where(*where, Device.created_at < now - timedelta(days=60)))).one()
    wk = (await db.execute(select(func.count(Device.id)).where(Device.is_trashed == False,  # noqa: E712
                                                               Device.created_at >= now - timedelta(days=7)))).scalar() or 0
    sold_12m = (await db.execute(select(func.count(Sale.id)).where(Sale.sold_at >= now - timedelta(days=365)))).scalar() or 0
    turns = sold_12m / units if units else 0
    kpis = [dw.kpi("Stock value", money(value), "at device price", True, "green", style="spark"),
            dw.kpi("Units", dw.num(units), "in stock", True, "blue", style="spark"),
            dw.kpi("Ready to sell", dw.num(r_units or 0), money(float(r_value or 0)), True, "teal", style="spark"),
            dw.kpi("Ageing > 60 days", dw.num(o_units or 0), money(float(o_value or 0)), not o_units, "red", style="spark"),
            dw.kpi("Received this week", dw.num(wk), "new tags", True, "indigo", style="spark"),
            dw.kpi("Stock turns", "%.1f×" % turns, "sold last 12 months ÷ stock", True, "violet", style="spark")]

    wh = (await db.execute(select(WAREHOUSE, func.sum(price), func.count(Device.id)).where(*where)
                           .group_by(WAREHOUSE).order_by(desc(func.sum(price))))).all()
    cols = ["green", "amber", "blue", "indigo", "teal", "red", "violet", "slate"]
    parts = [(w, float(v or 0) or c, cols[i % 8]) for i, (w, v, c) in enumerate(wh[:7])]
    if len(wh) > 7:
        parts.append(("Other", sum(float(v or 0) or c for _, v, c in wh[7:]), "grey"))
    stack = (dw.stack("Stock value by warehouse", 12, parts, kind="inr" if value else "n",
                      sub="At device price" if value else "Units (no device prices set)")
             if parts else dw.insight("Stock value by warehouse", 12, [("i", "Nothing in stock.", "slate")]))

    band = case((age < 31, 0), (age < 61, 1), else_=2)
    inner = select(CATEGORY.label("cat"), band.label("b")).where(*where).subquery()
    ab = (await db.execute(select(inner.c.cat, inner.c.b, func.count()).group_by(inner.c.cat, inner.c.b))).all()
    cmap = {}
    for cat, b, c in ab:
        cmap.setdefault(cat, [0, 0, 0])[int(b)] += c
    cats = sorted(cmap, key=lambda k: -sum(cmap[k]))[:6]
    ageing = (dw.bars("Ageing by category", 7, cats, [("0-30d", [cmap[c][0] for c in cats], "green"),
                                                       ("31-60d", [cmap[c][1] for c in cats], "amber"),
                                                       ("60d+", [cmap[c][2] for c in cats], "red")],
                      stacked=True, bmax="60px", sub="Units by days since received")
              if cats else dw.insight("Ageing by category", 7, [("i", "Nothing in stock.", "slate")]))
    st = (await db.execute(select(AS_IS, func.sum(price), func.count(Device.id)).where(*where).group_by(AS_IS))).all()
    sparts = [("As-Is" if a else "Finish good / in repair", float(v or 0) or c, "amber" if a else "green") for a, v, c in st]
    donut = (dw.donut("Value by stock type", 5, sparts, money(value) if value else dw.num(units), "in stock",
                      kind="inr" if value else "n") if sparts else dw.insight("Value by stock type", 5, [("i", "Nothing in stock.", "slate")]))

    slow = (await db.execute(select(Device.model, func.avg(age), func.count(Device.id))
                             .where(*where, Device.current_stage == DeviceStage.ready_to_sale, Device.model.isnot(None))
                             .group_by(Device.model).having(func.count(Device.id) >= 3)
                             .order_by(desc(func.avg(age))).limit(6))).all()
    slow_w = (dw.hbars("Slowest movers", 4, [((m or "—")[:22], float(a or 0), "%dd · %d" % (a or 0, n)) for m, a, n in slow],
                       color="red", lw="120px", sub="Ready devices: average days in stock · units")
              if slow else dw.insight("Slowest movers", 4, [("i", "No ready models with 3+ units.", "slate")]))
    top_models = [m for (m,) in (await db.execute(select(Device.model).where(*where, Device.model.isnot(None))
                                                  .group_by(Device.model).order_by(desc(func.count(Device.id))).limit(6))).all()]
    grades = ["A", "B", "C", "D", "E"]
    mg = (await db.execute(select(Device.model, Device.grade, func.count(Device.id))
                           .where(*where, Device.model.in_(top_models or [""])).group_by(Device.model, Device.grade))).all()
    gmap = {}
    for m, g, c in mg:
        gmap.setdefault(m, {})[getattr(g, "value", g) or ""] = c
    heat = (dw.heat("Model × grade (units)", 5, grades, [(m[:18], [gmap.get(m, {}).get(g, 0) for g in grades]) for m in top_models],
                    color="green", lw="120px", sub="Top models in stock")
            if top_models else dw.insight("Model × grade (units)", 5, [("i", "Nothing in stock.", "slate")]))
    ins = []
    old_ready = (await db.execute(select(func.count(Device.id)).where(*where, Device.current_stage == DeviceStage.ready_to_sale,
                                                                      Device.created_at < now - timedelta(days=30)))).scalar() or 0
    if old_ready:
        ins.append(("↓", "%s ready devices are older than 30 days — consider a price drop" % dw.num(old_ready), "red"))
    if o_units:
        ins.append(("!", "%s units (%s) have been in stock over 60 days" % (dw.num(o_units), money(float(o_value or 0))), "amber"))
    nowh = next((c for w, _, c in wh if w == "Not set"), 0)
    if nowh:
        ins.append(("⌖", "%s units have no warehouse set" % dw.num(nowh), "blue"))
    if turns:
        ins.append(("✓", "Stock turns %.1f× a year at today's stock level" % turns, "green"))
    insight = dw.insight("What to do this week", 3, ins or [("✓", "Nothing stands out right now.", "green")])
    widgets = dw.layout([(170, [stack]), (390, [ageing, donut]), (420, [slow_w, heat, insight])])
    return kpis, widgets
