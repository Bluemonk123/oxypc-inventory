"""CRM Sourcing (/crm/sourcing) and CRM Sales (/crm/sales) — Option C dashboards
(2026-11-14, Pankaj). No View Data on these pages: the deal list card sits on
the dashboard itself, below the deal board.

Sourcing: KPIs, deal board, (deal list), pipeline by owner / conversion /
deals created vs won in one row.
Sales: KPIs, deal board, (deal list), opportunity funnel + pipeline and wins,
then hot opportunities / dealer leaderboard / monthly target in one row.

Scope follows the list: sales (and telecaller, for Sales) users see only deals
assigned to them. Money is shown only with the module's Pricing tick; without
it, value cards fall back to deal counts. Monthly sales targets are kept in
app_settings (key "crm_sales_targets", JSON {username: amount}) and set from
the Set targets modal (POST /crm/sales/targets).
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import select, func, desc

from models.crm import (CRMSourcingDeal, CRMSourcingDealLineItem, CRMSalesOpportunity, CRMContact, CRMActivity, CRMQuote,
                        SOURCING_STAGES, SALES_STAGES)
from models.settings import AppSetting
from models.user import UserRole
from services import dash_widgets as dw
from utils.timezone import app_now

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
SALES_TARGET_KEY = "crm_sales_targets"

SRC_LANES = [("New", "slate", ["lead", "pricing", "contacted"]),
             ("Inspection", "cyan", ["inspection", "pricing_approval"]),
             ("Quote & negotiation", "amber", ["quote", "negotiation"]),
             ("PO & payment", "blue", ["purchase_order", "payment"]),
             ("Stock received", "indigo", ["received"]),
             ("Won", "green", ["won"])]
SAL_LANES = [("New", "slate", ["lead", "pricing", "contacted"]),
             ("Screening", "cyan", ["screening", "pricing_approval"]),
             ("Quote & negotiation", "amber", ["quote", "negotiation"]),
             ("Order & payment", "blue", ["sales_order", "payment"]),
             ("Handover", "indigo", ["received"]),
             ("Won", "green", ["won"])]


def _months(now, n=12):
    y, m = now.year, now.month
    out = []
    for _ in range(n):
        out.append((y, m)); m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


async def load_sales_targets(db) -> dict:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == SALES_TARGET_KEY))).scalar_one_or_none()
    try:
        data = json.loads(row.value) if row and row.value else {}
        return {str(k): float(v) for k, v in data.items() if str(v).replace(".", "", 1).isdigit() and float(v) > 0}
    except (ValueError, TypeError):
        return {}


async def _common(db, M, val, stages, lanes, scope, can_price, deal_type, no_col, link):
    """Shared pieces: KPIs-ready numbers, deal board, conversion steps, monthly series."""
    now = app_now()
    month0 = datetime(now.year, now.month, 1)
    d90 = now - timedelta(days=90)
    cond = [M.assigned_to == scope] if scope else []
    rows = (await db.execute(select(M.id, no_col, M.title, CRMContact.company_name, val, M.stage, M.assigned_to,
                                    M.created_by, M.created_at, M.updated_at)
                             .outerjoin(CRMContact, CRMContact.id == M.contact_id).where(*cond))).all()
    deals = [dict(id=r[0], no=r[1] or "—", title=r[2] or "", acct=r[3] or r[2] or "—", v=float(r[4] or 0), st=r[5] or "lead",
                  owner=r[6] or r[7] or "—", at=r[8], up=r[9]) for r in rows]
    open_ = [d for d in deals if d["st"] not in ("won", "lost")]
    won_m = [d for d in deals if d["st"] == "won" and d["up"] and d["up"] >= month0]
    closed90 = [d for d in deals if d["st"] in ("won", "lost") and d["up"] and d["up"] >= d90]
    win_rate = sum(1 for d in closed90 if d["st"] == "won") * 100 / len(closed90) if closed90 else None
    due = (await db.execute(select(func.count(CRMActivity.id), func.count(CRMActivity.id).filter(CRMActivity.next_followup < now))
                            .where(CRMActivity.deal_type == deal_type, CRMActivity.followup_done == False,  # noqa: E712
                                   CRMActivity.next_followup <= datetime.combine(now.date(), datetime.max.time()),
                                   *([CRMActivity.followup_assigned_to == scope] if scope else [])))).one()

    money = (lambda v: dw.inr(v)) if can_price else (lambda v: "")
    board = []
    for name, col, sts in lanes:
        ds = [d for d in deals if d["st"] in sts]
        if name == "Won":
            ds = [d for d in ds if d["up"] and d["up"] >= now - timedelta(days=30)]
        ds.sort(key=lambda d: -d["v"])
        chips = [((d["acct"] or "—")[:28], " · ".join(x for x in (d["no"], money(d["v"]) if d["v"] else "") if x)) for d in ds[:2]]
        board.append((name, col, len(ds), chips))
    tag = "%s open" % dw.num(len(open_))
    board_w = dw.lanes("Deal board", 12, board, sub="Biggest deals first in each stage · Won = last 30 days", tag=tag, tagc="green")

    # Conversion: deals created in the last 90 days that reached each board stage.
    order = [s for s, _ in stages if s not in ("won", "lost")] + ["won"]
    idx = {s: i for i, s in enumerate(order)}
    recent = [d for d in deals if d["at"] and d["at"] >= d90 and d["st"] in idx]
    steps = []
    for name, _c, sts in lanes:
        first = min(idx[s] for s in sts if s in idx)
        steps.append((name, sum(1 for d in recent if idx[d["st"]] >= first)))

    mon = _months(now)
    created = {k: 0 for k in mon}
    won = {k: 0 for k in mon}
    won_v = {k: 0.0 for k in mon}
    pipe_v = {k: 0.0 for k in mon}
    for d in deals:
        if d["at"]:
            k = (d["at"].year, d["at"].month)
            if k in created:
                created[k] += 1; pipe_v[k] += d["v"]
        if d["st"] == "won" and d["up"]:
            k = (d["up"].year, d["up"].month)
            if k in won:
                won[k] += 1; won_v[k] += d["v"]
    return dict(now=now, deals=deals, open=open_, won_m=won_m, win_rate=win_rate, due=due, board=board_w, steps=steps,
                mon=mon, created=created, won=won, won_v=won_v, pipe_v=pipe_v, money=money)


def _owner_widget(c, can_price, color, span=4):
    by = {}
    for d in c["open"]:
        by[d["owner"]] = by.get(d["owner"], 0) + (d["v"] if can_price else 1)
    top = sorted(by.items(), key=lambda kv: -kv[1])[:6]
    if not top:
        return dw.insight("Pipeline by owner", span, [("i", "No open deals.", "slate")])
    return dw.hbars("Pipeline by owner", span, [((u or "—")[:16], v) for u, v in top], kind="inr" if can_price else "n",
                    color=color, lw="100px", sub="Open deals, value" if can_price else "Open deals")


async def build_sourcing(db, user, can_price):
    scope = user.username if user.role in (UserRole.sales,) else None
    # Value = the deal's PO line items (what the list's Value column shows); deal price fields when it has none.
    lines = (select(func.sum(CRMSourcingDealLineItem.total_price))
             .where(CRMSourcingDealLineItem.deal_id == CRMSourcingDeal.id).correlate(CRMSourcingDeal).scalar_subquery())
    val = func.coalesce(lines, CRMSourcingDeal.final_price_total, CRMSourcingDeal.our_offer_total, CRMSourcingDeal.asking_price_total, 0)
    c = await _common(db, CRMSourcingDeal, val, SOURCING_STAGES, SRC_LANES, scope, can_price, "sourcing",
                      CRMSourcingDeal.deal_number, "/crm/sourcing")
    pipe = sum(d["v"] for d in c["open"])
    avg = pipe / len(c["open"]) if c["open"] else 0
    due_n, over_n = c["due"]
    kpis = [dw.kpi("Open deals", dw.num(len(c["open"])), "%s in total" % dw.num(len(c["deals"])), True, "green"),
            dw.kpi("Pipeline value", dw.inr(pipe) if can_price else "—", "open deals", True, "teal"),
            dw.kpi("Won this month", dw.num(len(c["won_m"])), dw.inr(sum(d["v"] for d in c["won_m"])) if can_price else "deals", True, "blue"),
            dw.kpi("Win rate", ("%d%%" % round(c["win_rate"])) if c["win_rate"] is not None else "—", "closed, last 90 days",
                   (c["win_rate"] or 0) >= 30, "indigo"),
            dw.kpi("Avg deal size", dw.inr(avg) if can_price and avg else "—", "open deals", True, "amber"),
            dw.kpi("Follow-ups due", dw.num(due_n), "%d overdue" % over_n, not over_n, "red")]
    steps = [s for s in c["steps"] if s[1]]
    conv = (dw.funnel("Conversion", 4, steps, color="green", lw="120px", sub="Deals created in the last 90 days")
            if steps and steps[0][1] else dw.insight("Conversion", 4, [("i", "No deals created in the last 90 days.", "slate")]))
    labels = [MONTHS[m - 1] for _, m in c["mon"]]
    trend = (dw.area("Deals created vs won", 4, labels, [("Created", [c["created"][k] for k in c["mon"]], "teal"),
                                                         ("Won", [c["won"][k] for k in c["mon"]], "green")], legv="sum", sub="Last 12 months")
             if any(c["created"].values()) or any(c["won"].values())
             else dw.insight("Deals created vs won", 4, [("i", "No deals in the last 12 months.", "slate")]))
    top = dw.layout([(330, [c["board"]])])
    bottom = dw.layout([(380, [_owner_widget(c, can_price, "green"), conv, trend])])
    return kpis, top, bottom


async def build_sales(db, user, can_price):
    scope = user.username if user.role in (UserRole.sales, UserRole.telecaller) else None
    val = func.coalesce(CRMSalesOpportunity.estimated_value, 0)
    c = await _common(db, CRMSalesOpportunity, val, SALES_STAGES, SAL_LANES, scope, can_price, "sales",
                      CRMSalesOpportunity.opp_number, "/crm/sales")
    now = c["now"]
    day0 = datetime.combine(now.date(), datetime.min.time())
    month0 = datetime(now.year, now.month, 1)
    calls = (await db.execute(select(func.count(CRMActivity.id)).where(
        CRMActivity.deal_type == "sales", CRMActivity.activity_type == "call", CRMActivity.activity_date >= day0,
        *([CRMActivity.performed_by == scope] if scope else [])))).scalar() or 0
    quotes = (await db.execute(select(func.count(CRMQuote.id)).where(
        CRMQuote.created_at >= month0, CRMQuote.status.in_(["sent", "negotiating", "accepted"]),
        *([CRMQuote.created_by == scope] if scope and hasattr(CRMQuote, "created_by") else [])))).scalar() or 0
    pipe = sum(d["v"] for d in c["open"])
    kpis = [dw.kpi("Open opportunities", dw.num(len(c["open"])), "%s in total" % dw.num(len(c["deals"])), True, "red"),
            dw.kpi("Pipeline", dw.inr(pipe) if can_price else "—", "open opportunities", True, "blue"),
            dw.kpi("Won this month", dw.inr(sum(d["v"] for d in c["won_m"])) if can_price else dw.num(len(c["won_m"])),
                   "%s deals" % dw.num(len(c["won_m"])), True, "green"),
            dw.kpi("Conversion", ("%d%%" % round(c["win_rate"])) if c["win_rate"] is not None else "—", "closed, last 90 days",
                   (c["win_rate"] or 0) >= 25, "indigo"),
            dw.kpi("Calls today", dw.num(calls), "logged on deals", True, "amber"),
            dw.kpi("Quotes sent", dw.num(quotes), "this month", True, "violet")]
    steps = [s for s in c["steps"] if s[1]]
    funnel = (dw.funnel("Opportunity funnel", 4, steps, color="red", lw="120px", sub="Created in the last 90 days")
              if steps and steps[0][1] else dw.insight("Opportunity funnel", 4, [("i", "No opportunities in the last 90 days.", "slate")]))
    labels = [MONTHS[m - 1] for _, m in c["mon"]]
    if can_price:
        series = [("New pipeline", [c["pipe_v"][k] for k in c["mon"]], "red"), ("Won", [c["won_v"][k] for k in c["mon"]], "green")]
        has = any(c["pipe_v"].values()) or any(c["won_v"].values())
    else:
        series = [("Created", [c["created"][k] for k in c["mon"]], "red"), ("Won", [c["won"][k] for k in c["mon"]], "green")]
        has = any(c["created"].values()) or any(c["won"].values())
    trend = (dw.area("Pipeline and wins", 8, labels, series, kind="inr" if can_price else "n", legv="sum",
                     sub="Value of opportunities created and won, last 12 months" if can_price else "Opportunities created and won, last 12 months")
             if has else dw.insight("Pipeline and wins", 8, [("i", "No opportunities in the last 12 months.", "slate")]))

    clabel = dict(SALES_STAGES)
    tone = {"quote": "blue", "negotiation": "amber", "sales_order": "green", "payment": "green", "received": "green"}
    hot = sorted(c["open"], key=lambda d: -d["v"])[:7]
    hot_t = dw.table("Hot opportunities", 5, [("Customer", 1.4), ("Value", 0.8), ("Stage", 1), ("Owner", 0.8)],
                     [[{"x": d["acct"][:26], "kind": "link", "href": "/crm/sales/%s" % d["id"]},
                       c["money"](d["v"]) or "—", (clabel.get(d["st"], d["st"]), tone.get(d["st"], "slate")), d["owner"][:14]]
                      for d in hot] or [["No open opportunities", "", "", ""]], sub="Most valuable first" if can_price else "Newest first")
    d90 = now - timedelta(days=90)
    lb = {}
    for d in c["deals"]:
        if d["st"] == "won" and d["up"] and d["up"] >= d90:
            lb[d["acct"]] = lb.get(d["acct"], 0) + (d["v"] if can_price else 1)
    top = sorted(lb.items(), key=lambda kv: -kv[1])[:6]
    leader = (dw.hbars("Dealer leaderboard", 4, [(a[:18], v) for a, v in top], kind="inr" if can_price else "n", color="red", lw="120px",
                       sub="Won value, last 90 days" if can_price else "Deals won, last 90 days")
              if top else dw.insight("Dealer leaderboard", 4, [("i", "No deals won in the last 90 days.", "slate")]))
    targets = await load_sales_targets(db)
    won_by = {}
    for d in c["won_m"]:
        won_by[d["owner"]] = won_by.get(d["owner"], 0) + d["v"]
    people = [u for u in targets if not scope or u == scope]
    if people and can_price:
        rings = dw.rings("Monthly target", 3, [(u[:10], min(100, won_by.get(u, 0) * 100 / targets[u]),
                                                "green" if won_by.get(u, 0) >= 0.85 * targets[u] else "amber" if won_by.get(u, 0) >= 0.5 * targets[u] else "red",
                                                "%d%%" % round(won_by.get(u, 0) * 100 / targets[u])) for u in sorted(people)[:6]],
                         cols=2, sub="Won this month vs target")
    else:
        rings = dw.insight("Monthly target", 3, [("i", "No monthly targets set yet." if can_price else "Targets need the Pricing tick.", "slate")])
    rings = dw.withbtn(rings, "Set targets", modal="#salesTargetsModal") if can_price else rings
    owners = sorted({d["owner"] for d in c["deals"] if d["owner"] != "—" and d["at"] and d["at"] >= d90} | set(targets))
    top_w = dw.layout([(330, [c["board"]])])
    bottom = dw.layout([(370, [funnel, trend]), (360, [hot_t, leader, rings])])
    return kpis, top_w, bottom, targets, owners
