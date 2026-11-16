"""External Partner Accounts (/trade-partner/partners) — Option C dashboard,
Variant 1 (2026-11-16, Pankaj): light KPI tiles, partner journey funnel,
partners by price segment and by sales owner. The Accounts as External
Partner table stays below, unchanged. Counts follow the page's filters.
"""
from datetime import timedelta

from sqlalchemy import select, func, or_

from services import dash_widgets as dw
from utils.timezone import app_now

SEG_COL = ["amber", "violet", "blue", "slate", "teal", "pink", "green"]


async def build(db, partners, scores, owner_names):
    from models.crm import CRMContact
    from models.partner import PartnerBid, LotBookingRequest, PartnerBooking
    now = app_now()
    ids = [p.id for p in partners]
    n = len(partners)
    active = [p for p in partners if p.portal_enabled]
    with_pw = [p for p in active if p.portal_password_hash]
    logged = [p for p in partners if p.portal_last_login_at]
    week = [p for p in logged if p.portal_last_login_at >= now - timedelta(days=7)]
    never = [p for p in active if not p.portal_last_login_at]

    async def dealers(stmt):
        return set((await db.execute(stmt)).scalars().all()) if ids else set()
    d30 = now - timedelta(days=30)
    act30 = (await dealers(select(PartnerBid.dealer_id).where(PartnerBid.dealer_id.in_(ids), PartnerBid.created_at >= d30)) |
             await dealers(select(LotBookingRequest.dealer_id).where(LotBookingRequest.dealer_id.in_(ids), LotBookingRequest.created_at >= d30)) |
             await dealers(select(PartnerBooking.dealer_id).where(PartnerBooking.dealer_id.in_(ids), PartnerBooking.created_at >= d30)))
    ever = (await dealers(select(PartnerBid.dealer_id).where(PartnerBid.dealer_id.in_(ids))) |
            await dealers(select(LotBookingRequest.dealer_id).where(LotBookingRequest.dealer_id.in_(ids))) |
            await dealers(select(PartnerBooking.dealer_id).where(PartnerBooking.dealer_id.in_(ids))))
    won = (await dealers(select(PartnerBid.dealer_id).where(PartnerBid.dealer_id.in_(ids), PartnerBid.status == "won")) |
           await dealers(select(LotBookingRequest.dealer_id).where(LotBookingRequest.dealer_id.in_(ids), LotBookingRequest.status == "approved")))
    sc = [v.get("score") for v in (scores or {}).values() if isinstance(v, dict) and v.get("score") is not None]
    avg = round(sum(sc) / len(sc)) if sc else None

    kpis = [dw.kpi("Partners", dw.num(n), "accounts as partner", True, "violet", style="flat"),
            dw.kpi("Portal active", dw.num(len(active)), "%d without login" % (n - len(active)) if n - len(active) else "all enabled", True, "green", style="flat"),
            dw.kpi("Logged in (7 days)", dw.num(len(week)), ("%d%% of active" % (len(week) * 100 // len(active))) if active else "", True, "blue", style="flat"),
            dw.kpi("Never logged in", dw.num(len(never)), "chase first login" if never else "none", not never, "amber", style="flat"),
            dw.kpi("Booked / bid (30 days)", dw.num(len(act30)), "partners", True, "indigo", style="flat"),
            dw.kpi("Avg score", str(avg) if avg is not None else "—", "0–100 behaviour score", True, "teal", style="flat")]

    eligible = (await db.execute(select(func.count(CRMContact.id)).where(
        CRMContact.status == "active", CRMContact.is_trashed.isnot(True),
        CRMContact.kyc_verified == True, CRMContact.credit_verified == True))).scalar() or 0  # noqa: E712
    steps = [("Eligible accounts", max(eligible, n)), ("Partner accounts", n), ("Login created", len(with_pw)),
             ("First login done", len(logged)), ("Booked or bid", len(ever)), ("Won / approved", len(won))]
    w_fun = dw.funnel("Partner journey", 5, [(l, max(v, 0.0001)) for l, v in steps], color="violet", lw="130px",
                      sub="Active + KYC + Credit verified → won")
    for i, (st, (l, v)) in enumerate(zip(w_fun["steps"], steps)):   # real counts; no ÷0
        prev = steps[i - 1][1] if i else 0
        st["v"] = dw.num(v)
        st["p"] = "" if not i else ("↓ %.0f%%" % (v * 100 / prev) if prev else "")
        st["tip"] = "%s: %s" % (l, dw.num(v))

    seg = {}
    for p in partners:
        k = (p.price_segment or "new_dealer").replace("_", " ").title()
        seg[k] = seg.get(k, 0) + 1
    parts = [(k, v, SEG_COL[i % len(SEG_COL)]) for i, (k, v) in enumerate(sorted(seg.items(), key=lambda x: -x[1]))]
    w_don = (dw.donut("Partners by price segment", 4, parts, dw.num(n), "partners") if parts else
             dw.donut("Partners by price segment", 4, [("None yet", 1, "slate")], "0", "partners"))
    if not parts:
        w_don["leg"] = [dict(l, v="0", p="—") for l in w_don["leg"]]

    own = {}
    for p in partners:
        k = owner_names.get(p.sales_owner_username, p.sales_owner_username) if p.sales_owner_username else "Unassigned"
        own[k] = own.get(k, 0) + 1
    top = sorted(own.items(), key=lambda x: -x[1])[:6] or [("—", 0.0001)]
    w_own = dw.hbars("Partners by sales owner", 3, [(k, v, dw.num(v)) for k, v in top], color="violet", lw="90px")
    if not own:
        for r in w_own["rows"]:
            r["w"], r["v"] = "0%", "0"

    return kpis, dw.layout([(300, [w_fun, w_don, w_own])])
