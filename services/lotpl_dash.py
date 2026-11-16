"""Lot P&L (/reports/lot-pl) — Option C dashboard (2026-11-14, Pankaj).

Built from the page's own per-lot rows (routers/reports.py lot_pl_report), so
it agrees with the table. "Return" of a lot = Profit on Sold Units / Cost of
Sold Units. Best lot and the league table go by profit amount, so a tiny lot
with a big % does not top them. A lot is closed when every device in it is sold. Money locked =
Total Cost - Cost of Sold Units on lots that still have unsold devices.
"""
from services import dash_widgets as dw


def build(lot_pl):
    lots = [r for r in lot_pl if r["devices"]]
    selling = [r for r in lots if r["sold"]]
    ret = lambda r: (r["profit_sold"] * 100 / r["cost_sold"]) if r["cost_sold"] else 0   # noqa: E731
    closed = lambda r: r["sold"] >= r["devices"]   # noqa: E731
    in_profit = [r for r in selling if r["profit_sold"] > 0]
    tot_cost_sold = sum(r["cost_sold"] for r in selling)
    avg_ret = sum(r["profit_sold"] for r in selling) * 100 / tot_cost_sold if tot_cost_sold else 0
    best = max(selling, key=lambda r: r["profit_sold"]) if selling else None   # by profit, so tiny lots with a big % do not win
    open_ = [r for r in lots if not closed(r)]
    locked = sum(max(0, r["total_cost"] - r["cost_sold"]) for r in open_)
    dev = sum(r["devices"] for r in lots)
    sold = sum(r["sold"] for r in lots)
    st = sold * 100 / dev if dev else 0
    kpis = [dw.kpi("Lots", dw.num(len(lots)), "%d still selling" % len(open_), True, "violet"),
            dw.kpi("Lots in profit", dw.num(len(in_profit)), ("%d%% of lots with sales" % round(len(in_profit) * 100 / len(selling))) if selling else "",
                   True, "green"),
            dw.kpi("Average return", "%.1f%%" % avg_ret, "on sold units", avg_ret >= 10, "blue"),
            dw.kpi("Best lot", best["lot_number"] if best else "—", (dw.inr(best["profit_sold"]) + " profit") if best else "", True, "teal"),
            dw.kpi("Money locked in stock", dw.inr(locked), "%d lots" % len(open_), True, "amber"),
            dw.kpi("Sell-through", "%d%%" % round(st), "of devices sold", st >= 70, "indigo")]

    recent = sorted(selling, key=lambda r: str(r["purchase_date"] or ""))[-12:]
    bars = (dw.bars("Profit by lot", 8, [(r["lot_number"] or "—")[-8:] for r in recent],
                    [("Profit on sold units", [max(0, r["profit_sold"]) for r in recent], "violet")], kind="inr", bmax="30px", legv="",
                    sub="Latest 12 lots with sales · a lot at a loss shows no bar")
            if recent else dw.insight("Profit by lot", 8, [("i", "No lot has sales yet.", "slate")]))
    gauge = dw.gauge("Sell-through", 4, st, "%d%%" % round(st), "of devices bought are sold", "violet",
                     [("Sold", dw.num(sold)), ("In stock", dw.num(dev - sold))])
    league = sorted(selling, key=lambda r: r["profit_sold"], reverse=True)
    pick = league[:4] + [r for r in league[-3:] if r not in league[:4]]
    tone = lambda v: "green" if v >= 10 else "amber" if v >= 0 else "red"   # noqa: E731
    table = dw.table("Lot league table", 8, [("Lot", 0.8), ("Supplier", 1.4), ("Cost sold", 0.8), ("Sold for", 0.8), ("Return", 0.6), ("Status", 0.7)],
                     [[r["lot_number"] or "—", (r["supplier"] or "—")[:22], dw.inr(r["cost_sold"]), dw.inr(r["revenue"]),
                       ("%+.0f%%" % ret(r), tone(ret(r))), ("Closed", "slate") if closed(r) else ("Selling", "blue")] for r in pick]
                     or [["No lot has sales yet", "", "", "", "", ""]], sub="Most and least profit on sold units")
    groups = [("Closed in profit", sum(1 for r in lots if closed(r) and r["profit_sold"] > 0), "green"),
              ("Selling, in profit", sum(1 for r in lots if not closed(r) and r["sold"] and r["profit_sold"] > 0), "blue"),
              ("Selling, below cost", sum(1 for r in lots if not closed(r) and r["sold"] and r["profit_sold"] <= 0), "amber"),
              ("Nothing sold yet", sum(1 for r in lots if not r["sold"]), "slate"),
              ("Closed at loss", sum(1 for r in lots if closed(r) and r["profit_sold"] <= 0), "red")]
    groups = [g for g in groups if g[1]]
    donut = (dw.donut("Lot status", 4, groups, dw.num(len(lots)), "lots")
             if groups else dw.insight("Lot status", 4, [("i", "No lots yet.", "slate")]))
    sup = {}
    for r in selling:
        a = sup.setdefault((r["supplier"] or "—")[:20], [0.0, 0.0]); a[0] += r["profit_sold"]; a[1] += r["cost_sold"]
    sl = sorted(((k, a[0] * 100 / a[1]) for k, a in sup.items() if a[1] >= 100000), key=lambda kv: -kv[1])[:6]
    by_sup = (dw.hbars("Return by supplier", 6, [(k, round(v, 1), "%.0f%%" % v) for k, v in sl],
                       colors=["green" if v >= 10 else "amber" if v >= 0 else "red" for _, v in sl], lw="140px", sub="Profit on sold units / their cost · suppliers with ₹1L+ sold")
              if sl else dw.insight("Return by supplier", 6, [("i", "No lot has sales yet.", "slate")]))
    lk = sorted(((r["lot_number"] or "—", max(0, r["total_cost"] - r["cost_sold"])) for r in open_), key=lambda kv: -kv[1])[:6]
    lk = [x for x in lk if x[1]]
    by_lock = (dw.hbars("Money locked by lot", 6, lk, kind="inr", color="amber", lw="120px", sub="Cost of devices not sold yet")
               if lk else dw.insight("Money locked by lot", 6, [("✓", "No money locked in unsold devices.", "green")]))
    return kpis, dw.layout([(370, [bars, gauge]), (400, [table, donut]), (370, [by_sup, by_lock])])
