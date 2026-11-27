"""Customer-return badges (2026-11-27, Pankaj) — All Inventory (next to the
entity name) and Product IQC (under the Tag Number).

  RMA  the tag was returned by a customer (repaired and sent back)
       hover: Return Date, Return By, Sales Person, Sale Date, Warranty
  RR   the returned tag was replaced — Tag Return Status "Replaced by <tag>"
       hover: Replaced with <replacement tag>
  RS   the tag was sold as a replacement — "Replaced with <tag>" /
       RMA's "Replacement for <tag>"
       hover: Sold and Replaced for <customer tag>
"""
from html import escape


def _after(text, prefix):
    t = (text or "").strip()
    return t[len(prefix):].strip() if t.startswith(prefix) else None


async def return_badge_map(db, devices) -> dict:
    """{str(device id): (code, hover text)} for the given Device rows."""
    from sqlalchemy import select, func
    from models.sales import Return, Sale
    from utils.sales_person import linked_name_map, sales_person_display
    from utils.warranty import effective_warranty_for_sale

    devices = list(devices or [])
    out, need = {}, []
    for d in devices:
        rr = _after(d.tag_return_status, "Replaced by")
        if rr:
            out[str(d.id)] = ("RR", f"Replaced with {rr}")
        else:
            need.append(d)
    if not need:
        return out
    ids = [d.id for d in need]
    rets = {}
    for i in range(0, len(ids), 5000):
        for r in (await db.execute(
                select(Return).where(Return.device_id.in_(ids[i:i + 5000]),
                                     func.coalesce(Return.approval_status, "pending") != "rejected")
                .order_by(Return.device_id, Return.return_date.desc().nullslast())
                .distinct(Return.device_id))).scalars().all():
            rets[r.device_id] = r
    sale_ids = [r.sale_id for r in rets.values() if r.sale_id]
    sales = {s.id: s for s in (await db.execute(select(Sale).where(Sale.id.in_(sale_ids)))).scalars().all()} \
        if sale_ids else {}
    names = await linked_name_map(db) if rets else {}
    fmt = lambda x: x.strftime("%d-%m-%Y") if x else "—"   # noqa: E731
    for d in need:
        rs = _after(d.tag_return_status, "Replacement for") or None
        r = rets.get(d.id)
        if r is not None and not rs:
            s = sales.get(r.sale_id)
            w = effective_warranty_for_sale(s)
            wt = (("In Warranty till " if w["status"] == "active" else "Out of Warranty, ended ")
                  + w["expiry"].strftime("%d-%m-%Y")) if w else "No Warranty"
            out[str(d.id)] = ("RMA", "\n".join([
                f"Return Date: {fmt(r.return_date)}", f"Return By: {r.processed_by or '—'}",
                f"Sales Person: {(sales_person_display(s, names) if s else '') or '—'}",
                f"Sale Date: {fmt(s.sold_at if s else None)}", f"Warranty: {wt}"]))
            continue
        rs = rs or _after(d.replaced, "Replaced with")
        if rs:
            out[str(d.id)] = ("RS", f"Sold and Replaced for {rs}")
    return out


_CLS = {"RMA": "bg-warning text-dark", "RR": "bg-danger", "RS": "bg-info text-dark"}


def return_badge_html(badges: dict, device_id, label=None) -> str:
    """Small badge with the hover text; label overrides the code (IQC shows
    "Customer Return")."""
    b = badges.get(str(device_id))
    if not b:
        return ""
    code, tip = b
    return (f'<span class="badge {_CLS[code]} ms-1 align-text-top" style="font-size:.62rem;cursor:help" '
            f'title="{escape(tip)}">{escape(label or code)}</span>')
