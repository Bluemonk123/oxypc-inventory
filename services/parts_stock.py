"""Spare parts stock — one definition for every page (2026-10-31).

Crate rows
----------
A Part Master row is one crate's stock of one part:
    Part Name + Make + Model + Type (New / Harvest) + Crate
Parts GRN and Add Harvest add to the matching row (or create it); Handover
takes from the matching row only; Update Count sets a row's count. Price is
per Part Name + Make + Model: the latest Parts GRN unit price is written to
every crate row of that part, New and Harvest.

Stock modes
-----------
"legacy" (until the Parts Data Fix is applied): qty_in_stock is a gross
figure that handovers never decremented, so live stock = qty_in_stock minus
the handed-over total of its name+make+model group minus approved part-sale
qty. That subtracted the group total from EVERY crate row of the group.

"row" (after the Data Fix): past handovers are baked into qty_in_stock once,
and each new handover decrements the crate row it was taken from, so live
stock = qty_in_stock minus approved part-sale qty, per row.

The Data Fix flips app_settings[parts_stock_mode] to "row".
"""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.part_request import PartRequest
from models.part_sales import PartSaleRequest
from models.spare_parts import SparePart

MODE_KEY = "parts_stock_mode"


def norm(v) -> str:
    return (v or "").strip().lower()


def group_key(name, make, model):
    return (norm(name), norm(make), norm(model))


def row_key(name, make, model, source, crate):
    return (norm(name), norm(make), norm(model), "harvest" if norm(source) == "harvest" else "new", norm(crate))


def part_row_key(p):
    return row_key(p.name, p.make, p.model, p.source, p.crate_number)


async def row_mode(db: AsyncSession) -> bool:
    from models.settings import AppSetting
    v = (await db.execute(select(AppSetting.value).where(AppSetting.key == MODE_KEY))).scalar()
    return (v or "").strip().lower() == "row"


async def legacy_consumed_by_group(db: AsyncSession) -> dict:
    """Handed-over qty per name+make+model (legacy mode only)."""
    out: dict = {}
    for name, make, model, total in (await db.execute(
            select(PartRequest.part_name, SparePart.make, SparePart.model, func.sum(PartRequest.qty_handed_over))
            .outerjoin(SparePart, PartRequest.part_id == SparePart.id)
            .group_by(PartRequest.part_name, SparePart.make, SparePart.model))).all():
        k = group_key(name, make, model)
        out[k] = out.get(k, 0) + int(total or 0)
    return out


async def sold_by_part(db: AsyncSession) -> dict:
    return {str(pid): int(n or 0) for pid, n in (await db.execute(
        select(PartSaleRequest.part_id, func.sum(PartSaleRequest.qty_requested))
        .where(PartSaleRequest.status == "approved", PartSaleRequest.part_id.isnot(None))
        .group_by(PartSaleRequest.part_id))).all()}


async def consumed_by_part(db: AsyncSession, parts) -> dict:
    """What live stock subtracts for handovers, per part id: the group total in
    legacy mode, nothing in row mode (already in qty_in_stock)."""
    if await row_mode(db):
        return {str(p.id): 0 for p in parts}
    g = await legacy_consumed_by_group(db)
    return {str(p.id): g.get(group_key(p.name, p.make, p.model), 0) for p in parts}


async def live_map(db: AsyncSession, parts) -> dict:
    cons = await consumed_by_part(db, parts)
    sold = await sold_by_part(db)
    return {str(p.id): max(0, int(p.qty_in_stock or 0) - cons.get(str(p.id), 0) - sold.get(str(p.id), 0))
            for p in parts}


async def find_row(db: AsyncSession, name, make, model, source, crate):
    """The live (not trashed) crate row for these five values, or None."""
    want = row_key(name, make, model, source, crate)
    cands = (await db.execute(select(SparePart).where(
        SparePart.is_trashed.isnot(True),
        func.lower(func.trim(SparePart.name)) == want[0]))).scalars().all()
    hits = [p for p in cands if part_row_key(p) == want]
    hits.sort(key=lambda p: (p.created_at is None, p.created_at))
    return hits[0] if hits else None


async def group_rows(db: AsyncSession, name, make, model):
    want = group_key(name, make, model)
    cands = (await db.execute(select(SparePart).where(
        SparePart.is_trashed.isnot(True),
        func.lower(func.trim(SparePart.name)) == want[0]))).scalars().all()
    return [p for p in cands if group_key(p.name, p.make, p.model) == want]


async def latest_grn_price(db: AsyncSession, name, make, model):
    """Unit price of the latest New Parts GRN line (price above 0) for this name+make+model."""
    from models.parts_grn import PartsGRN, PartsGRNLineItem
    want = group_key(name, make, model)
    rows = (await db.execute(
        select(PartsGRNLineItem.part_name, PartsGRNLineItem.part_brand, PartsGRNLineItem.part_model,
               PartsGRNLineItem.price)
        .join(PartsGRN, PartsGRNLineItem.grn_id == PartsGRN.id)
        .where(PartsGRNLineItem.is_harvest.isnot(True), PartsGRNLineItem.price > 0,
               func.lower(func.trim(PartsGRNLineItem.part_name)) == want[0])
        .order_by(PartsGRNLineItem.created_at.desc()))).all()
    for n, mk, md, price in rows:
        if group_key(n, mk, md) == want:
            return price
    return None


async def apply_group_price(db: AsyncSession, name, make, model, price) -> int:
    """Write the latest GRN unit price to every crate row of the part (a
    blank / zero price on a line changes nothing)."""
    if price is None or Decimal(str(price)) <= 0:
        return 0
    rows = await group_rows(db, name, make, model)
    for p in rows:
        p.unit_price = Decimal(str(price))
    return len(rows)


async def match_info(db: AsyncSession, name, make, model, source, crate) -> dict:
    """Matched / Not-Matched answer for the crate fields (GRN line, Add
    Harvest, Handover)."""
    if not norm(name) or not norm(crate):
        return {"matched": False, "ready": False}
    p = await find_row(db, name, make, model, source, crate)
    if not p:
        return {"matched": False, "ready": True}
    live = (await live_map(db, [p]))[str(p.id)]
    return {"matched": True, "ready": True, "part_id": str(p.id), "part_code": p.part_code,
            "qty": live, "crate": p.crate_number}


async def parts_stock_summary(db: AsyncSession) -> dict:
    """Dashboard Total Spare Parts card. Tiles are the 34 Device Detail Parts
    Consumption names (PARTS_MATRIX); parts whose name is not one of the 34
    still count in the totals."""
    from services.parts_required import PARTS_MATRIX
    labels = [row[0] for row in PARTS_MATRIX]
    by_lower = {l.lower(): l for l in labels}

    parts = (await db.execute(select(SparePart).where(SparePart.is_trashed.isnot(True)))).scalars().all()
    trashed = (await db.execute(select(func.count()).select_from(SparePart)
                                .where(SparePart.is_trashed.is_(True)))).scalar() or 0
    live = await live_map(db, parts)

    tiles = {l: 0 for l in labels}
    total = new = harvest = low = 0
    for p in parts:
        q = live[str(p.id)]
        total += q
        if (p.source or "new") == "harvest":
            harvest += q
        else:
            new += q
        if q <= int(p.min_stock_alert or 0):
            low += 1                                   # same rule as the Parts Manager "below min" tile
        lab = by_lower.get((p.name or "").strip().lower())
        if lab:
            tiles[lab] += q

    requested = (await db.execute(select(func.count()).select_from(PartRequest)
                                  .where(PartRequest.status == "requested"))).scalar() or 0
    handed = (await db.execute(select(func.coalesce(func.sum(PartRequest.qty_handed_over), 0))
                               .where(PartRequest.status == "handed_over"))).scalar() or 0
    ordered = sorted(tiles.items(), key=lambda kv: (-kv[1], labels.index(kv[0])))
    return {"total": total, "new": new, "harvest": harvest, "skus": len(parts), "low": low,
            "requested": int(requested), "handed": int(handed), "trashed": int(trashed),
            "tiles": ordered}


# ── Parts Data Fix: crate rows (2026-10-31) ─────────────────────────────────
async def crate_fix_plan(db: AsyncSession) -> dict:
    """What the Parts Data Fix will do, without changing anything.

    1. Past handovers are taken off stock once. Legacy mode subtracted each
       name+make+model group's handed-over total from EVERY row of the group;
       now each handover comes off one row: the crate row it is linked to, or
       (no link) the group's best-stocked rows in turn.
    2. Rows with the same Part Name + Make + Model + Type + Crate are merged
       into the oldest; requests, sales, ledger and GRN lines move with them.
    3. Unit Price of every row follows the latest Parts GRN unit price of its
       Part Name + Make + Model (when there is one).
    4. Rows with no crate are listed for the Parts Manager to assign.
    Then stock mode becomes "row" (live = row count - approved part sales)."""
    rows = (await db.execute(select(SparePart).where(SparePart.is_trashed.isnot(True))
                             .order_by(SparePart.created_at))).scalars().all()
    by_id = {str(p.id): p for p in rows}
    sold = await sold_by_part(db)
    groups: dict = {}
    for p in rows:
        groups.setdefault(group_key(p.name, p.make, p.model), []).append(p)

    # legacy figures (what the page shows today)
    cons_g = await legacy_consumed_by_group(db)
    shown_before = {str(p.id): max(0, int(p.qty_in_stock or 0) - cons_g.get(group_key(p.name, p.make, p.model), 0)
                                   - sold.get(str(p.id), 0)) for p in rows}

    # 1. bake handovers into rows
    new_qty = {str(p.id): int(p.qty_in_stock or 0) for p in rows}
    linked: dict = {}         # (group, row id) -> qty
    for name, pid, make, model, total in (await db.execute(
            select(PartRequest.part_name, PartRequest.part_id, SparePart.make, SparePart.model,
                   func.sum(PartRequest.qty_handed_over))
            .outerjoin(SparePart, PartRequest.part_id == SparePart.id)
            .where(PartRequest.qty_handed_over > 0)
            .group_by(PartRequest.part_name, PartRequest.part_id, SparePart.make, SparePart.model))).all():
        g = group_key(name, make, model)
        linked[(g, str(pid) if pid else "")] = linked.get((g, str(pid) if pid else ""), 0) + int(total or 0)
    short = []
    for (g, pid), q in linked.items():
        grp = groups.get(g)
        if not grp:
            continue                                     # never netted from any row; nothing to undo
        if pid and pid in by_id and group_key(by_id[pid].name, by_id[pid].make, by_id[pid].model) == g:
            take = min(q, new_qty[pid])
            new_qty[pid] -= take
            q -= take
        for p in sorted(grp, key=lambda x: -new_qty[str(x.id)]):
            if q <= 0:
                break
            take = min(q, new_qty[str(p.id)])
            new_qty[str(p.id)] -= take
            q -= take
        if q > 0:
            short.append({"part": " / ".join(x or "—" for x in g), "qty": q})

    # 2. merge same five values
    merges = []
    by_key: dict = {}
    for p in rows:
        by_key.setdefault(part_row_key(p), []).append(p)
    keep_of = {}
    for k, ps in by_key.items():
        if len(ps) > 1:
            keep = ps[0]
            for d in ps[1:]:
                keep_of[str(d.id)] = str(keep.id)
            merges.append({"keep": str(keep.id), "keep_code": keep.part_code, "name": keep.name,
                           "make": keep.make, "model": keep.model, "crate": keep.crate_number,
                           "type": "Harvest" if keep.source == "harvest" else "New",
                           "codes": [d.part_code for d in ps[1:]],
                           "qty": sum(new_qty[str(x.id)] for x in ps)})
    final_qty = {}
    for p in rows:
        pid = str(p.id)
        tgt = keep_of.get(pid, pid)
        final_qty[tgt] = final_qty.get(tgt, 0) + new_qty[pid]

    # 3. prices
    prices = []
    for g, grp in groups.items():
        pr = await latest_grn_price(db, *g)
        if pr is None:
            continue
        for p in grp:
            if str(p.id) in keep_of:
                continue
            if Decimal(str(p.unit_price or 0)) != Decimal(str(pr)):
                prices.append({"id": str(p.id), "code": p.part_code, "name": p.name, "crate": p.crate_number,
                               "old": str(p.unit_price or 0), "new": str(pr)})

    # per-part summary (before = today's page, after = row mode)
    summary = []
    for g, grp in groups.items():
        before = sum(shown_before[str(p.id)] for p in grp)
        after = sum(max(0, final_qty.get(str(p.id), 0) - sold.get(str(p.id), 0)) for p in grp if str(p.id) not in keep_of)
        n_after = sum(1 for p in grp if str(p.id) not in keep_of)
        if before != after or n_after != len(grp):
            summary.append({"part": f"{grp[0].name} / {grp[0].make or '—'} / {grp[0].model or '—'}",
                            "rows_before": len(grp), "rows_after": n_after, "before": before, "after": after})
    summary.sort(key=lambda r: -abs(r["after"] - r["before"]))
    blank = [{"code": p.part_code, "name": p.name, "make": p.make, "model": p.model,
              "type": "Harvest" if p.source == "harvest" else "New",
              "qty": max(0, final_qty.get(str(p.id), 0) - sold.get(str(p.id), 0))}
             for p in rows if not norm(p.crate_number) and str(p.id) not in keep_of]
    return {"row_mode": await row_mode(db), "summary": summary, "merges": merges, "prices": prices,
            "blank": blank, "short": short, "new_qty": new_qty, "final_qty": final_qty, "keep_of": keep_of,
            "rows": len(rows),
            "total_before": sum(shown_before.values()),
            "total_after": sum(max(0, final_qty.get(str(p.id), 0) - sold.get(str(p.id), 0))
                               for p in rows if str(p.id) not in keep_of)}


async def crate_fix_apply(db: AsyncSession, user=None, request=None) -> dict:
    from sqlalchemy import text, update
    from models.parts_grn import PartsGRNLineItem
    from models.settings import AppSetting
    from services.audit_engine import audit
    from utils.timezone import app_now
    plan = await crate_fix_plan(db)
    if plan["row_mode"]:
        return {"ok": False, "detail": "already applied"}
    rows = {str(p.id): p for p in (await db.execute(
        select(SparePart).where(SparePart.is_trashed.isnot(True)))).scalars().all()}
    before = {pid: int(p.qty_in_stock or 0) for pid, p in rows.items()}
    # tables pointing at spare_parts.id
    fks = (await db.execute(text(
        "SELECT c.conrelid::regclass::text, a.attname FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
        "WHERE c.contype = 'f' AND c.confrelid = 'spare_parts'::regclass"))).all()
    import uuid as _uuid
    for dup, keep in plan["keep_of"].items():
        for tbl, col in fks:
            await db.execute(text(f'UPDATE {tbl} SET "{col}" = :k WHERE "{col}" = :d'),
                             {"k": _uuid.UUID(keep), "d": _uuid.UUID(dup)})
        await db.execute(update(PartsGRNLineItem).where(PartsGRNLineItem.part_id == rows[dup].part_code)
                         .values(part_id=rows[keep].part_code))
        d = rows[dup]
        d.qty_in_stock = 0
        d.is_trashed = True
        d.trashed_at = app_now()
        d.notes = ((d.notes or "") + f" [Merged into {rows[keep].part_code} by Parts Data Fix]").strip()
    for pid, q in plan["final_qty"].items():
        if pid in rows and pid not in plan["keep_of"]:
            rows[pid].qty_in_stock = q
    for pr in plan["prices"]:
        if pr["id"] in rows:
            rows[pr["id"]].unit_price = Decimal(pr["new"])
    st = (await db.execute(select(AppSetting).where(AppSetting.key == MODE_KEY))).scalar_one_or_none()
    if st is None:
        db.add(AppSetting(key=MODE_KEY, value="row", description="Spare parts stock = crate row count",
                          updated_by=getattr(user, "username", "system")))
    else:
        st.value = "row"
        st.updated_by = getattr(user, "username", "system")
    await audit(db, user=user, action="PARTS_CRATE_FIX", table_name="spare_parts",
                record_id=f"{len(rows)} parts",
                old_value={"qty_in_stock": before},
                new_value={"qty_in_stock": {pid: rows[pid].qty_in_stock for pid in rows},
                           "merged": plan["keep_of"], "prices": plan["prices"],
                           "total_before": plan["total_before"], "total_after": plan["total_after"]},
                notes="Parts Data Fix: past handovers taken off crate rows once, same-crate duplicates merged "
                      "(merged rows trashed, references moved), prices = latest GRN, stock mode -> row. "
                      "Reverse: restore old_value qty_in_stock, untrash merged ids, set parts_stock_mode to legacy.",
                request=request)
    await db.commit()
    return {"ok": True, "merged": len(plan["keep_of"]), "prices": len(plan["prices"]),
            "total_before": plan["total_before"], "total_after": plan["total_after"]}
