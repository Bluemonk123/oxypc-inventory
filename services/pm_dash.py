"""Product Master (/product-master) — Option C dashboard, Variant 2 (2026-11-16, Pankaj).

KPIs = the page's old six Status tiles (light tiles). Row 1: Make x Model
pricing status (completed / pending, hover lists the models), selling price bands (pricing done). A pipeline strip (no card)
sits under the KPI tiles. Row 2: gaps to fix, Competitive Brands summary, recent price changes
(audit log of pm_products). The page's tabs stay below, unchanged.
Built from the same product rows the table shows (routers.product_master._product_rows).
"""
import re
from datetime import timedelta

from sqlalchemy import select, func

from services import dash_widgets as dw
from utils.timezone import app_now

ACTIONS = {"PM_PRODUCT_PRICE_SET": ("Product price set", "green"), "PM_PART_PRICES_SET": ("Parts prices set", "cyan"),
           "PM_DISCOUNT_SET": ("Discount changed", "blue"), "PM_PRODUCT_ADDED": ("Added", "amber"),
           "PM_PRODUCT_EDITED": ("Edited", "slate")}
BANDS = [("< 10k", 0, 10000), ("10–15k", 10000, 15000), ("15–20k", 15000, 20000), ("20–25k", 20000, 25000),
         ("25–35k", 25000, 35000), ("35k+", 35000, 10 ** 12)]


def _ago(t, now):
    s = (now - t).total_seconds()
    if s < 3600:
        return "%dm" % max(1, s // 60)
    if s < 86400:
        return "%dh" % (s // 3600)
    return "yday" if s < 172800 else t.strftime("%d %b")


def _gen(v):
    m = re.search(r"(\d+)", str(v or ""))
    if not m:
        return None
    n = int(m.group(1))
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


async def build(db, products, comp_lists, can_price=True):
    from models.engines import AuditLog
    from models.product_master import PMCompetitorPrice
    now = app_now()
    n_all = len(products)
    n_pp = sum(1 for p in products if not p["price_complete"])
    n_parts = sum(1 for p in products if not p["parts_done"])
    done = [p for p in products if p["price_complete"] and p["parts_done"]]
    n_done = len(done)
    pct = (n_done * 100 // n_all) if n_all else 0
    kpis = [dw.kpi("All Records", dw.num(n_all), "", True, "slate", style="flat"),
            dw.kpi("Product Price Pending", dw.num(n_pp), ("%d%% of catalogue" % (n_pp * 100 // n_all)) if n_all else "", not n_pp, "amber", style="flat"),
            dw.kpi("Parts Price Pending", dw.num(n_parts), "", not n_parts, "cyan", style="flat"),
            dw.kpi("Pricing Done", dw.num(n_done), "%d%% complete" % pct, True, "green", style="flat"),
            dw.kpi("Active", dw.num(n_done), "on price lists", True, "blue", style="flat"),
            dw.kpi("Inactive", dw.num(n_all - n_done), "need pricing", n_all == n_done, "slate", style="flat")]

    # Make x Model pricing status (2026-11-16, Pankaj): per Make, the models
    # with every price done vs still pending; hover lists the models.
    mk = {}
    for p in products:
        m = (p["make"] or "").strip() or "(no Make)"
        mk[m] = mk.get(m, 0) + 1
    makes = [m for m, _ in sorted(mk.items(), key=lambda x: (-x[1], x[0].lower()))][:6]
    cells, missing = {}, {}
    for p in products:
        m = (p["make"] or "").strip() or "(no Make)"
        m = m if m in makes else "Others"
        st = "done" if (p["price_complete"] and p["parts_done"]) else "pend"
        model = (p["model"] or p["product_name"] or "—").strip()
        cells.setdefault((m, st), {}).setdefault(model, 0)
        cells[(m, st)][model] += 1
        if st == "pend":   # what is still missing for this model
            miss = missing.setdefault((m, model), set())
            if p["ram_price"] is None or p["storage_price"] is None:
                miss.add("Hardware")
            if not p["parts_done"]:
                miss.add("Parts")
            if p["product_price"] is None:
                miss.add("Product price")
    rows_m = makes + (["Others"] if len(mk) > len(makes) else [])
    cols = [("done", "Pricing Completed", "green"), ("pend", "Pricing Pending", "amber")]
    heat_rows = [(m, [len(cells.get((m, k), {})) for k, _l, _c in cols]) for m in rows_m] or [("—", [0, 0])]
    w_heat = dw.heat("Make × Model pricing", 7, [c[1] for c in cols], heat_rows, color="teal", lw="90px",
                     sub="Models per Make · hover a cell for the list")
    mx = max([v for r in heat_rows for v in r[1]] or [0]) or 1
    for r, (m, vals) in zip(w_heat["hrows"], heat_rows):
        for ci, (c, v) in enumerate(zip(r["cells"], vals)):
            key, label, col = cols[ci]
            models = cells.get((m, key), {})
            a = 0.10 + 0.80 * (v / mx) if v else 0.06
            c["bg"] = dw.tint(dw.c(col), a)
            c["fg"] = "#ffffff" if a > 0.55 else "var(--dk-text)"
            n_items = sum(models.values())
            c["v"] = ("%s model%s" % (dw.num(v), "" if v == 1 else "s")) if v else "0"
            lines = ["%s · %s — %s model%s, %s product%s" % (m, label, dw.num(v), "" if v == 1 else "s",
                                                             dw.num(n_items), "" if n_items == 1 else "s")]
            top = sorted(models.items(), key=lambda x: (-x[1], x[0].lower()))
            order = ("Hardware", "Parts", "Product price")
            lines += ["• %s%s%s" % (name, " (%d)" % n if n > 1 else "",
                                    (" — needs " + ", ".join(x for x in order if x in missing.get((m, name), ())))
                                    if key == "pend" and missing.get((m, name)) else "")
                      for name, n in top[:18]]
            if len(top) > 18:
                lines.append("+ %d more" % (len(top) - 18))
            c["tip"] = "\n".join(lines) if v else "%s · %s — none" % (m, label)

    # Price bands (selling price, pricing done)
    sp = [float(p["selling_price"]) for p in done if p["selling_price"] is not None]
    band = [sum(1 for v in sp if lo <= v < hi) for _, lo, hi in BANDS]
    if can_price:
        w_bars = dw.bars("Selling price bands", 5, [b[0] for b in BANDS], [("Products", band if any(band) else [1] * 6, "teal")],
                         sub="Pricing done products")
        if any(band) and max(band) < 4:   # few products: whole-number axis 0-4, not 0, 0, 1, 1
            for g, v in zip(w_bars["groups"], band):
                for st in g["stacks"]:
                    st["h"] = "%.1f%%" % (v * 100 / 4)
            w_bars["yt"] = [{"x": str(4 - i)} for i in range(5)]
        if not any(band):   # no priced products yet: empty bars, zero axis
            for g in w_bars["groups"]:
                for st in g["stacks"]:
                    st["h"] = "0%"
            w_bars["yt"] = [{"x": "0"} for _ in w_bars["yt"]]
            w_bars["leg"] = [dict(l, v="0") for l in w_bars.get("leg", [])]
    else:
        w_bars = dw.hbars("Products by make", 5, [(m, mk[m]) for m in makes] or [("—", 0.0001, "0")], color="teal", lw="90px",
                          sub="All records")

    # Pipeline strip (2026-11-16, Pankaj): no card, under the KPI tiles, in
    # this order — Added > Hardware > Parts > Product price > Done > Active.
    n_hw = sum(1 for p in products if p["ram_price"] is not None and p["storage_price"] is not None)
    n_prodp = sum(1 for p in products if p["product_price"] is not None)
    n_partsset = sum(1 for p in products if p["parts_done"])

    def _pend(k):
        return "%s pending" % dw.num(n_all - k) if n_all - k else "all set"
    pipeline = dw.slim("", 12, [
        ("Product Added", dw.num(n_all), "slate", "", "All product records"),
        ("Hardware Price set", dw.num(n_hw), "violet", _pend(n_hw), "RAM and Storage both priced in Hardware"),
        ("Parts Price set", dw.num(n_partsset), "cyan", _pend(n_partsset), "Every spare-part price filled"),
        ("Product Price set", dw.num(n_prodp), "amber", _pend(n_prodp), "Product price entered"),
        ("Pricing done", dw.num(n_done), "green", "%d%%" % pct, "Product, RAM, Storage and parts all priced"),
        ("Active on Price list", dw.num(n_done), "blue", "live", "Shown on price lists")])["slim"]

    # Gaps
    stale = 0
    for p in done:
        try:
            d = p["updated_at"] and __import__("datetime").datetime.strptime(p["updated_at"], "%d-%m-%Y")
        except ValueError:
            d = None
        if d and d < now - timedelta(days=90):
            stale += 1
    gaps = []
    if n_pp:
        gaps.append(("!", "%s product%s have no complete product price" % (dw.num(n_pp), "" if n_pp == 1 else "s"), "amber"))
    if n_parts:
        gaps.append(("!", "%s product%s missing parts prices" % (dw.num(n_parts), " is" if n_parts == 1 else "s are"), "amber"))
    if stale:
        gaps.append(("⏱", "%s price%s unchanged for 90+ days" % (dw.num(stale), "" if stale == 1 else "s"), "red"))
    gaps.append(("✓", "%s product%s live on price lists" % (dw.num(n_done), "" if n_done == 1 else "s"), "green"))
    w_gaps = dw.insight("Gaps to fix", 4, gaps[:4])

    # Competitive brands
    ids = {p["id"]: p for p in done if p["selling_price"] is not None}
    cheaper = within = 0
    last = None
    cps = (await db.execute(select(PMCompetitorPrice.product_id, func.min(PMCompetitorPrice.price), func.max(PMCompetitorPrice.updated_at))
                            .group_by(PMCompetitorPrice.product_id))).all()
    for pid, mn, up in cps:
        if up and (last is None or up > last):
            last = up
        p = ids.get(str(pid))
        if not p or mn is None:
            continue
        ours = float(p["selling_price"])
        if float(mn) < ours * 0.95:
            cheaper += 1
        elif ours and abs(float(mn) - ours) <= ours * 0.05:
            within += 1
    rows = [("Tracked brands", dw.num(len(comp_lists)), "blue")]
    if can_price:
        rows += [("Competitor cheaper (>5%)", "%s model%s" % (dw.num(cheaper), "" if cheaper == 1 else "s"), "red", "red" if cheaper else ""),
                 ("Within ±5%", "%s model%s" % (dw.num(within), "" if within == 1 else "s"), "green")]
    rows.append(("Last price update", _ago(last, now) + (" ago" if _ago(last, now)[-1] in "mh" else "") if last else "—", ""))
    w_comp = dw.kv("Competitive brands", 4, rows, foot="From the Competitive Brands tab")

    # Recent price changes
    logs = (await db.execute(select(AuditLog.action, AuditLog.username, AuditLog.record_id, AuditLog.timestamp)
                             .where(AuditLog.table_name == "pm_products", AuditLog.action.in_(list(ACTIONS)))
                             .order_by(AuditLog.timestamp.desc()).limit(5))).all()
    by_id = {p["id"]: p for p in products}
    items = []
    for act, user, rid, ts in logs:
        lab, col = ACTIONS[act]
        p = by_id.get(rid or "")
        name = (p["model"] or p["product_name"]) if p else "Deleted product"
        extra = (" · ₹{:,.0f}".format(float(p["selling_price"])) if (p and can_price and p["selling_price"] is not None
                                                                        and act != "PM_PRODUCT_ADDED") else "")
        items.append((name + extra, "%s · by %s" % (lab, user or "system"), _ago(ts, now), col))
    if not items:
        items = [("No price changes yet", "Changes made on this page show here", "", "slate")]
    w_feed = dw.feed("Recent price changes", 4, items)

    return kpis, dw.layout([(300, [w_heat, w_bars]), (260, [w_gaps, w_comp, w_feed])]), pipeline
