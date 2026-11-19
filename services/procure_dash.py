"""Procurement Board (/procure-dashboard) — Option C dashboard (2026-11-14, Pankaj).

Supplier Deals (CRM Sourcing, CRMSourcingDeal) and Customer Deals (CRM Sales,
CRMSalesOpportunity) side by side: open value, won this month, monthly value,
both funnels, latest deals of both types, owners and follow-ups due. Blue =
supplier deals, orange = customer deals.
"""
from datetime import datetime, timedelta

from sqlalchemy import select, func, desc

from models.crm import CRMSourcingDeal, CRMSalesOpportunity, CRMContact, SOURCING_STAGES, SALES_STAGES
from services import dash_widgets as dw
from utils.timezone import app_now

SD, CD = "blue", "orange"
SVAL = func.coalesce(CRMSourcingDeal.final_price_total, CRMSourcingDeal.our_offer_total, CRMSourcingDeal.asking_price_total, 0)
CVAL = func.coalesce(CRMSalesOpportunity.estimated_value, 0)
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def _cum(stages, deals_stage_counts):
    keys = [k for k, _ in stages if k not in ("won", "lost")] + ["won"]
    idx = {k: i for i, k in enumerate(keys)}
    labels = dict(stages)
    out = []
    for i, k in enumerate(keys):
        out.append((labels.get(k, k), sum(c for st, c in deals_stage_counts.items() if st in idx and idx[st] >= i)))
    return out


async def build(db, overdue_count, today_count, all_due, pending_parts, sp=True, cp=True):
    """sp / cp: Pricing View on Supplier Deals (crm_sourcing) / Customer Deals (crm_sales_opp)."""
    now = app_now()
    sm = dw.inr if sp else (lambda v: "—")
    cm = dw.inr if cp else (lambda v: "—")
    month0 = datetime(now.year, now.month, 1)
    s_open, s_open_v = (await db.execute(select(func.count(CRMSourcingDeal.id), func.sum(SVAL))
                                         .where(CRMSourcingDeal.stage.notin_(["won", "lost"])))).one()
    c_open, c_open_v = (await db.execute(select(func.count(CRMSalesOpportunity.id), func.sum(CVAL))
                                         .where(CRMSalesOpportunity.stage.notin_(["won", "lost"])))).one()
    s_won, s_won_v = (await db.execute(select(func.count(CRMSourcingDeal.id), func.sum(SVAL))
                                       .where(CRMSourcingDeal.stage == "won", CRMSourcingDeal.updated_at >= month0))).one()
    c_won, c_won_v = (await db.execute(select(func.count(CRMSalesOpportunity.id), func.sum(CVAL))
                                       .where(CRMSalesOpportunity.stage == "won", CRMSalesOpportunity.updated_at >= month0))).one()
    kpis = [dw.kpi("Supplier deals open", dw.num(s_open or 0), (sm(float(s_open_v or 0)) + " pipeline") if sp else "open", True, "blue"),
            dw.kpi("Customer deals open", dw.num(c_open or 0), (cm(float(c_open_v or 0)) + " pipeline") if cp else "open", True, "orange"),
            dw.kpi("Supplier deals won", dw.num(s_won or 0), (sm(float(s_won_v or 0)) + " this month") if sp else "this month", True, "indigo"),
            dw.kpi("Customer deals won", dw.num(c_won or 0), (cm(float(c_won_v or 0)) + " this month") if cp else "this month", True, "green"),
            dw.kpi("Follow-ups due", dw.num(overdue_count + today_count), "%d overdue" % overdue_count, not overdue_count, "red"),
            dw.kpi("Part requests", dw.num(pending_parts), "waiting for procurement", not pending_parts, "violet")]

    m_start = datetime(now.year - (1 if now.month < 12 else 0), (now.month % 12) + 1, 1)
    months = []
    y, m = m_start.year, m_start.month
    for _ in range(12):
        months.append((y, m)); m += 1
        if m > 12:
            y, m = y + 1, 1
    smv = {(int(a), int(b)): float(v or 0) for a, b, v in (await db.execute(
        select(func.extract("year", CRMSourcingDeal.created_at), func.extract("month", CRMSourcingDeal.created_at), func.sum(SVAL))
        .where(CRMSourcingDeal.created_at >= m_start)
        .group_by(func.extract("year", CRMSourcingDeal.created_at), func.extract("month", CRMSourcingDeal.created_at)))).all()}
    cmv = {(int(a), int(b)): float(v or 0) for a, b, v in (await db.execute(
        select(func.extract("year", CRMSalesOpportunity.created_at), func.extract("month", CRMSalesOpportunity.created_at), func.sum(CVAL))
        .where(CRMSalesOpportunity.created_at >= m_start)
        .group_by(func.extract("year", CRMSalesOpportunity.created_at), func.extract("month", CRMSalesOpportunity.created_at)))).all()}
    ser = ([("Supplier deals", [smv.get(k, 0) for k in months], SD)] if sp else []) + \
          ([("Customer deals", [cmv.get(k, 0) for k in months], CD)] if cp else [])
    if ser and any(any(x[1]) for x in ser):
        trend = dw.area("Deal value by month", 8, [MONTHS[mm - 1] for _, mm in months], ser,
                        kind="inr", legv="sum", sub="Value of deals created, last 12 months")
    else:
        trend = dw.insight("Deal value by month", 8, [("i", "No deals created in the last 12 months." if ser else "Deal values need the Pricing View tick.", "slate")])
    split = dw.donut("Open deals", 4, [p for p in [("Supplier deals", s_open or 0, SD), ("Customer deals", c_open or 0, CD)] if p[1]]
                     or [("None", 1, "grey")], dw.num((s_open or 0) + (c_open or 0)), "open")

    sc = dict((await db.execute(select(CRMSourcingDeal.stage, func.count(CRMSourcingDeal.id)).group_by(CRMSourcingDeal.stage))).all())
    cc = dict((await db.execute(select(CRMSalesOpportunity.stage, func.count(CRMSalesOpportunity.id)).group_by(CRMSalesOpportunity.stage))).all())
    sf = [s for s in _cum(SOURCING_STAGES, sc) if s[1]][:6]
    cf = [s for s in _cum(SALES_STAGES, cc) if s[1]][:6]
    s_funnel = (dw.funnel("Supplier deals", 3, sf, color=SD, lw="90px", sub="Deals that reached each stage")
                if sf else dw.insight("Supplier deals", 3, [("i", "No supplier deals yet.", "slate")]))
    c_funnel = (dw.funnel("Customer deals", 3, cf, color=CD, lw="90px", sub="Deals that reached each stage")
                if cf else dw.insight("Customer deals", 3, [("i", "No customer deals yet.", "slate")]))
    slabel, clabel = dict(SOURCING_STAGES), dict(SALES_STAGES)
    srows = (await db.execute(select(CRMSourcingDeal.deal_number, CRMSourcingDeal.title, CRMContact.company_name, SVAL,
                                     CRMSourcingDeal.stage, CRMSourcingDeal.created_at)
                              .outerjoin(CRMContact, CRMContact.id == CRMSourcingDeal.contact_id)
                              .order_by(desc(CRMSourcingDeal.created_at)).limit(5))).all()
    crows = (await db.execute(select(CRMSalesOpportunity.opp_number, CRMSalesOpportunity.title, CRMContact.company_name, CVAL,
                                     CRMSalesOpportunity.stage, CRMSalesOpportunity.created_at)
                              .outerjoin(CRMContact, CRMContact.id == CRMSalesOpportunity.contact_id)
                              .order_by(desc(CRMSalesOpportunity.created_at)).limit(5))).all()
    mixed = sorted([("Supplier", r) for r in srows] + [("Customer", r) for r in crows], key=lambda t: t[1][5] or now, reverse=True)[:7]
    latest = dw.table("Latest deals", 6, [("Deal", 0.9), ("Account", 1.4), ("Type", 0.8), ("Value", 0.7), ("Stage", 0.9)],
                      [[no or "—", (acct or title or "—"), (typ, SD if typ == "Supplier" else CD), (sm if typ == "Supplier" else cm)(float(v or 0)),
                        (slabel if typ == "Supplier" else clabel).get(st, st or "—")]
                       for typ, (no, title, acct, v, st, _at) in mixed] or [["No deals yet", "", "", "", ""]], sub="Both deal types, newest first",
                      # Won = dark green row, Lost = maroon row, white text (2026-11-19, Pankaj).
                      row_colors=[{"won": ("#14532d", "#ffffff"), "lost": ("#7b1f2b", "#ffffff")}.get(st) for _t, (_n, _ti, _a, _v, st, _x) in mixed])

    so = (await db.execute(select(func.coalesce(CRMSourcingDeal.assigned_to, CRMSourcingDeal.created_by), (func.sum(SVAL) if sp else func.count(CRMSourcingDeal.id)))
                           .where(CRMSourcingDeal.stage.notin_(["lost"]))
                           .group_by(func.coalesce(CRMSourcingDeal.assigned_to, CRMSourcingDeal.created_by))
                           .order_by(desc(func.sum(SVAL) if sp else func.count(CRMSourcingDeal.id))).limit(5))).all()
    co = (await db.execute(select(func.coalesce(CRMSalesOpportunity.assigned_to, CRMSalesOpportunity.created_by), (func.sum(CVAL) if cp else func.count(CRMSalesOpportunity.id)))
                           .where(CRMSalesOpportunity.stage.notin_(["lost"]))
                           .group_by(func.coalesce(CRMSalesOpportunity.assigned_to, CRMSalesOpportunity.created_by))
                           .order_by(desc(func.sum(CVAL) if cp else func.count(CRMSalesOpportunity.id))).limit(5))).all()
    s_own = (dw.hbars("Supplier deals by owner", 4, [((u or "—")[:16], float(v or 0)) for u, v in so], kind="inr" if sp else "n", color=SD, lw="100px",
                      sub="Pipeline + won") if so else dw.insight("Supplier deals by owner", 4, [("i", "No supplier deals yet.", "slate")]))
    c_own = (dw.hbars("Customer deals by owner", 4, [((u or "—")[:16], float(v or 0)) for u, v in co], kind="inr" if cp else "n", color=CD, lw="100px",
                      sub="Pipeline + won") if co else dw.insight("Customer deals by owner", 4, [("i", "No customer deals yet.", "slate")]))
    fu = []
    for a in all_due[:6]:
        when = a.next_followup
        fu.append(((getattr(a, "subject", None) or getattr(a, "activity_type", None) or "Follow-up")[:40],
                   (getattr(a, "notes", None) or "")[:50], when.strftime("%d %b") if when else "",
                   "red" if when and when <= now else "amber"))
    feed = dw.feed("Follow-ups due", 4, fu or [("Nothing due", "No follow-ups overdue or due today", "", "green")], sub="Overdue first")
    widgets = dw.layout([(360, [trend, split]), (380, [s_funnel, c_funnel, latest]), (360, [s_own, c_own, feed])])
    return kpis, widgets
