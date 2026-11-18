"""Sales Coordinator (2026-11-14, Pankaj): one page with four cards that
follow a showroom tag from Floor Movement to the gate.

  1 Moved to Showroom    one row per Floor Movement (Move Device,
                         routers/transfers.py) that sent a tag to the
                         Showroom. Done once sold after that move — kept
                         even if the tag is later returned / repaired;
                         Pending while unsold, still there, and no stage
                         change since (2026-11-17). "Send to Pre-Sale"
                         (notes modal) queues the tag for Pre-Sale IQC through
                         /pre-sale-iqc/assign; while that WorkID is open the
                         button reads "Waiting at Pre-Sale".
  2 Sales Records        one row per invoice number; the Sales IDs count
                         opens the invoice's tags (/sales-coordinator/invoice).
  3 Pre-Sale QC          each tag's latest Pre-Sale IQC WorkID with the notes
                         sent with it (WorkOrder.notes): Pending / Pass / Fail.
                         "Add Tags" (2026-11-18): search + select Ready for Sale
                         tags that came without a Floor Movement and send them
                         to Pre-Sale QC; they then count as the user's tags here,
                         in Sales Records and in Waiting for Customer.
  4 Waiting for Customer the Gate Pass list: sold tags not yet exited
                         (Pending) / exited (Completed), with DC + Confirm Exit.

Who sees what (2026-11-14): a user sees only the tags Floor Movement
assigned to them (the movement's Assigned To employee, its "asgn" WorkID) —
those tags through Pre-Sale QC, Sales and Exits. Admin sees every tag.
Since 2026-11-17 any showroom move assigned to them counts, not only the
tag's latest move, so a later move elsewhere doesn't hide their past sales.

Filters (GET): entity (default: the user's profile Entity), date_from /
date_to, category, sales_person — multi-selects are comma-separated.
Export Report: the filtered sales in a modal (Delivery / Payment Status
dropdowns) downloaded as CSV in the browser.

Module key "sales_coordinator" — OFF by default for every role
(models/role_permissions.py DEFAULT_DISABLED_MODULES).
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, func, and_, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_module_perm
from database import get_db
from models.device import Device, DeviceStage, StageMovement
from models.location import StorageLocation, ZoneType
from models.sales import Sale
from models.stock_transfer import StockTransfer
from models.user import User, UserRole
from models.work_order import WorkOrder
from templates_config import templates
from utils.device_spec import gb_text_spaced
from utils.device_types import DEVICE_TYPES_12

router = APIRouter(tags=["sales_coordinator"])

MODULE = "sales_coordinator"
view_perm = require_module_perm(MODULE, "enable")

ROW_CAP = 500          # rows per pill tab — newest first
EXPORT_CAP = 5000

_category_expr = func.coalesce(func.nullif(func.trim(Device.device_type), ""), Device.sub_category)


def spec_line(d) -> str:
    """Model / CPU / Gen / RAM / Storage under a tag number."""
    parts = [d.model, d.cpu, d.generation, gb_text_spaced(d.ram_gb), gb_text_spaced(d.storage_gb)]
    return " / ".join(str(p) for p in parts if p) or "—"


def category_of(d) -> str:
    return ((d.device_type or "").strip() or d.sub_category or "—")


def _multi(v: str) -> list:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _is_admin(user) -> bool:
    return getattr(user.role, "value", user.role) == UserRole.admin.value


class Filters:
    """The page's filters, parsed once and applied per card."""
    def __init__(self, entity, date_from, date_to, category, sales_person):
        self.entity, self.category, self.sales_person = _multi(entity), _multi(category), _multi(sales_person)
        self.date_from = self._d(date_from)
        self.date_to = self._d(date_to)

    @staticmethod
    def _d(v):
        try:
            return datetime.strptime(v, "%Y-%m-%d") if v else None
        except ValueError:
            return None

    def device(self):
        """Clauses on Device (entity, category)."""
        out = []
        if self.entity:
            out.append(Device.entity.in_(self.entity))
        if self.category:
            out.append(_category_expr.in_(self.category))
        return out

    def dates(self, col):
        out = []
        if self.date_from:
            out.append(col >= self.date_from)
        if self.date_to:
            out.append(col < self.date_to + timedelta(days=1))
        return out

    def sale(self):
        return [Sale.sales_person.in_(self.sales_person)] if self.sales_person else []


def _showroom_cond():
    """A Floor Movement whose destination is the Showroom."""
    return or_(StockTransfer.transfer_type == "trc_to_showroom",
               func.lower(StockTransfer.to_warehouse).contains("showroom"),
               StorageLocation.zone == ZoneType.showroom)


async def _showroom_moves(db, me: str | None):
    """{device_id: [(StockTransfer, assignee, next_move_at), ...]} — EVERY
    device Floor Movement that sent the tag to the Showroom (2026-11-17,
    Pankaj; was only the tag's latest move), kept for a non-admin only when
    it was assigned to `me`. next_move_at is when the tag's next Floor
    Movement (anywhere) happened, None while this one is still its latest.
    Each showroom move is its own record, so a tag sold, returned and moved
    again keeps the first coordinator's sale in their history. Simple
    queries instead of one big join: the join on
    work_orders.source_transfer_id took ~13 s on production-size data."""
    show_locs = set((await db.execute(
        select(StorageLocation.id).where(StorageLocation.zone == ZoneType.showroom))).scalars().all())
    show_cond = or_(StockTransfer.transfer_type == "trc_to_showroom",
                    func.lower(StockTransfer.to_warehouse).contains("showroom"),
                    *([StockTransfer.to_location_id.in_(show_locs)] if show_locs else []))
    shows = (await db.execute(
        select(StockTransfer)
        .where(StockTransfer.move_kind == "device", StockTransfer.device_id.isnot(None), show_cond))).scalars().all()
    if not shows:
        return {}
    assignee = {}
    ids = [t.id for t in shows]
    for k in range(0, len(ids), 5000):
        for tid, user in (await db.execute(
                select(WorkOrder.source_transfer_id, WorkOrder.assigned_username)
                .where(WorkOrder.source_transfer_id.in_(ids[k:k + 5000]), WorkOrder.stage == "asgn"))).all():
            assignee[tid] = user
    mine = [t for t in shows if me is None or assignee.get(t.id) == me]
    # Every Floor Movement time of those tags, to find each showroom move's next move.
    times: dict = {}
    dids = list({t.device_id for t in mine})
    for k in range(0, len(dids), 5000):
        for did, tid, at, ca in (await db.execute(
                select(StockTransfer.device_id, StockTransfer.id, StockTransfer.transfer_date, StockTransfer.created_at)
                .where(StockTransfer.move_kind == "device", StockTransfer.device_id.in_(dids[k:k + 5000])))).all():
            times.setdefault(did, []).append(((at or ca or datetime.min), (ca or at or datetime.min), tid))
    for v in times.values():
        v.sort()
    out: dict = {}
    for t in mine:
        key = ((t.transfer_date or t.created_at or datetime.min), (t.created_at or t.transfer_date or datetime.min), t.id)
        later = [x for x in times.get(t.device_id, ()) if x > key]
        out.setdefault(t.device_id, []).append((t, assignee.get(t.id), later[0][0] if later else None))
    return out


async def _psiqc_latest(db, device_ids) -> dict:
    """{device_id: latest Pre-Sale IQC WorkOrder} for the given tags."""
    from routers.pre_sale_iqc import STAGE_CODE
    out = {}
    ids = [i for i in device_ids if i]
    for k in range(0, len(ids), 5000):
        for wo in (await db.execute(
            select(WorkOrder).where(WorkOrder.device_id.in_(ids[k:k + 5000]), WorkOrder.stage == STAGE_CODE)
            .order_by(WorkOrder.created_at.asc()))).scalars().all():
            out[wo.device_id] = wo                   # latest wins (ascending)
    return out


def _in_scope(col, scope):
    """`col IN scope` (scope = None means every tag)."""
    if scope is None:
        return []
    return [col.in_(list(scope) or [None])]


async def _stage_changes(db, device_ids) -> dict:
    """{device_id: latest stage change time} — a real stage change
    (from != to) that isn't a sale. Used so a tag sent back to repair / QC /
    cosmetic after its showroom move (no new Floor Movement) leaves Pending
    (2026-11-17, Pankaj)."""
    out = {}
    ids = list(device_ids)
    for k in range(0, len(ids), 5000):
        for did, last in (await db.execute(
                select(StageMovement.device_id, func.max(StageMovement.moved_at))
                .where(StageMovement.device_id.in_(ids[k:k + 5000]),
                       StageMovement.from_stage.isnot(None),
                       StageMovement.from_stage != StageMovement.to_stage,
                       StageMovement.to_stage != DeviceStage.sold)
                .group_by(StageMovement.device_id))).all():
            out[did] = last
    return out


async def _sold_times(db, device_ids) -> dict:
    """{device_id: [sold_at, ...]} from the Sale rows (a return keeps them)."""
    out = {}
    ids = list(device_ids)
    for k in range(0, len(ids), 5000):
        for did, at in (await db.execute(
                select(Sale.device_id, Sale.sold_at).where(Sale.device_id.in_(ids[k:k + 5000])))).all():
            if at:
                out.setdefault(did, []).append(at)
    return out


async def _showroom_rows(db, moves, f: Filters):
    """Card 1, one row per showroom move (2026-11-17, Pankaj):
      Done    — the tag was sold after this move (before its next Floor
                Movement). Kept for good: a later return / repair doesn't
                undo the coordinator's sale.
      Pending — not sold since, still the tag's latest Floor Movement, and
                no stage change since (else it has left the showroom)."""
    if not moves:
        return [], []
    devs = {}
    ids = list(moves)
    for k in range(0, len(ids), 5000):
        for d in (await db.execute(select(Device).where(Device.id.in_(ids[k:k + 5000]), *f.device()))).scalars():
            devs[d.id] = d
    changed = await _stage_changes(db, devs)
    sold = await _sold_times(db, devs)
    pending, done = [], []
    for did, recs in moves.items():
        d = devs.get(did)
        if not d:
            continue
        for t, assignee, next_at in recs:
            at = t.transfer_date or t.created_at
            if f.date_from and (not at or at < f.date_from):
                continue
            if f.date_to and (not at or at >= f.date_to + timedelta(days=1)):
                continue
            row = {"d": d, "spec": spec_line(d), "category": category_of(d),
                   "location": t.to_warehouse if t.to_warehouse and t.to_warehouse != "—" else "",
                   "moved_at": t.transfer_date, "moved_by": t.transferred_by or t.created_by,
                   "assignee": assignee}
            sales = [x for x in sold.get(did, ()) if at and x >= at and (next_at is None or x < next_at)]
            if sales:
                row["sold_at"] = min(sales)
                done.append(row)
            elif (next_at is None and d.current_stage != DeviceStage.sold
                  and not (changed.get(did) and at and changed[did] > at)):
                pending.append(row)
    pending.sort(key=lambda r: r["moved_at"] or datetime.min, reverse=True)
    done.sort(key=lambda r: r.get("sold_at") or r["moved_at"] or datetime.min, reverse=True)
    _showroom_rows.last_counts = (len(pending), len(done))      # dashboard tiles (before the row cap)
    pending, done = pending[:ROW_CAP], done[:ROW_CAP]
    wos = await _psiqc_latest(db, [r["d"].id for r in pending])
    for r in pending:
        wo = wos.get(r["d"].id)
        r["psiqc"] = wo.status if wo else ""
    return pending, done


async def _sales_rows(db, scope, f: Filters):
    """Card 2: one row per invoice number, newest first."""
    rows = (await db.execute(
        select(Sale.invoice_no, func.min(Sale.sold_at), func.max(Sale.customer_name),
               func.max(Sale.sales_person), func.count(Sale.id))
        .join(Device, Device.id == Sale.device_id)
        .where(Sale.invoice_no.isnot(None), Sale.invoice_no != "", *_in_scope(Sale.device_id, scope),
               *f.device(), *f.sale(), *f.dates(Sale.sold_at))
        .group_by(Sale.invoice_no)
        .order_by(desc(func.min(Sale.sold_at)))
        .limit(ROW_CAP)
    )).all()
    return [{"invoice": inv, "date": at, "customer": cust, "person": person, "count": n}
            for inv, at, cust, person, n in rows]


async def _psiqc_rows(db, scope, f: Filters):
    """Card 3: every tag's latest Pre-Sale IQC WorkID — Pending / Pass / Fail."""
    from routers.pre_sale_iqc import STAGE_CODE, psiqc_result_map
    wos = (await db.execute(
        select(WorkOrder).join(Device, Device.id == WorkOrder.device_id)
        .where(WorkOrder.stage == STAGE_CODE, *_in_scope(WorkOrder.device_id, scope), *f.device())
        .order_by(WorkOrder.created_at.asc()))).scalars().all()
    latest, notes = {}, {}
    for wo in wos:
        latest[wo.device_id] = wo
        if wo.notes:            # "Start again" opens a WorkID without notes
            notes[wo.device_id] = wo.notes
    latest = {k: v for k, v in latest.items()
              if not ((f.date_from and (v.assigned_at or v.created_at) < f.date_from) or
                      (f.date_to and (v.assigned_at or v.created_at) >= f.date_to + timedelta(days=1)))}
    devs = {}
    ids = list(latest)
    for k in range(0, len(ids), 5000):
        for d in (await db.execute(select(Device).where(Device.id.in_(ids[k:k + 5000])))).scalars().all():
            devs[d.id] = d
    results = await psiqc_result_map(db, latest)
    out = {"pending": [], "pass": [], "fail": []}
    for did, wo in sorted(latest.items(), key=lambda kv: kv[1].created_at or kv[1].assigned_at, reverse=True):
        d = devs.get(did)
        if not d:
            continue
        res = results.get(str(did))
        r = {"d": d, "spec": spec_line(d), "notes": notes.get(did, ""), "wo": wo, "result": res}
        if wo.status in ("pending", "in_progress"):
            out["pending"].append(r)
        elif res == "fail":
            out["fail"].append(r)
        else:
            out["pass"].append(r)        # completed: Pass (or no result picked)
    capped = {k: v[:ROW_CAP] for k, v in out.items()}
    capped["counts"] = {k: len(v) for k, v in out.items()}       # dashboard (before the row cap)
    return capped


_EXITED = ("dispatched", "delivered")


async def _gate_rows(db, scope, f: Filters):
    """Card 4: Gate Pass rows — not yet exited / exited."""
    base = (select(Sale, Device.barcode, Device.brand, Device.model)
            .join(Device, Device.id == Sale.device_id)
            .where(*_in_scope(Sale.device_id, scope), *f.device(), *f.sale(), *f.dates(Sale.sold_at)))
    exited = Sale.delivery_status.in_(_EXITED)
    pending = (await db.execute(base.where(or_(~exited, Sale.delivery_status.is_(None)))
                                .order_by(desc(Sale.sold_at)).limit(ROW_CAP))).all()
    done = (await db.execute(base.where(exited)
                             .order_by(desc(func.coalesce(Sale.dispatch_date, Sale.sold_at))).limit(ROW_CAP))).all()
    pack = lambda rows: [{"sale": s, "barcode": bc, "device": f"{b or ''} {m or ''}".strip() or "—"}  # noqa: E731
                         for s, bc, b, m in rows]
    return pack(pending), pack(done)


async def _export_rows(db, scope, f: Filters):
    """Export Report: one row per sold tag (filters applied)."""
    rows = (await db.execute(
        select(Sale, Device).join(Device, Device.id == Sale.device_id)
        .where(*_in_scope(Sale.device_id, scope), *f.device(), *f.sale(), *f.dates(Sale.sold_at))
        .order_by(desc(Sale.sold_at)).limit(EXPORT_CAP))).all()
    out = []
    for s, d in rows:
        exit_at = (s.dispatch_date or s.sold_at) if s.delivery_status in _EXITED else None
        out.append({
            "person": s.sales_person or "", "invoice": s.invoice_no or "",
            "invoice_date": s.sold_at.strftime("%d-%m-%Y") if s.sold_at else "",
            "entity": d.entity or "", "make": d.brand or "", "model": d.model or "", "cpu": d.cpu or "",
            "gen": d.generation or "", "ram": gb_text_spaced(d.ram_gb) or "",
            "storage": gb_text_spaced(d.storage_gb) or "",
            "grade": (getattr(d.grade, "value", d.grade) or "") if d.grade else "",
            "stock_type": "As-Is" if (d.sub_lot_number or "").strip() else "Finish Good",
            "qty": 1, "exit_date": exit_at.strftime("%d-%m-%Y") if exit_at else "",
            # Defaults (Pankaj): Done when the invoice number / exit date is there.
            "delivery_done": bool(exit_at), "payment_done": bool((s.invoice_no or "").strip()),
        })
    return out


async def _context(db, user, entity, date_from, date_to, category, sales_person):
    """Filters + who-sees-what, shared by the page and the export feed.
    Entity: the user's profile Entity until they pick another (an empty
    value in the URL means all entities)."""
    if entity is None:
        entity = ",".join(getattr(user, "all_entities", None) or [user.entity or ""])   # all profile entities (2026-11-17)
    f = Filters(entity, date_from, date_to, category, sales_person)
    admin = _is_admin(user)
    moves = await _showroom_moves(db, None if admin else user.username)
    return entity, f, admin, moves, (None if admin else set(moves) | await _direct_ids(db, user.username))


async def _direct_ids(db, username: str) -> set:
    """Tags this user sent to Pre-Sale QC themselves (2026-11-18, Pankaj):
    tags that reach the coordinator without a Floor Movement are added with
    "Add Tags" on the Pre-Sale QC card (/pre-sale-iqc/assign, which records
    created_by), so they count as theirs in Pre-Sale QC, Sales and Waiting
    for Customer."""
    from routers.pre_sale_iqc import STAGE_CODE
    return set((await db.execute(
        select(WorkOrder.device_id).where(WorkOrder.stage == STAGE_CODE, WorkOrder.created_by == username)
        .distinct())).scalars().all())


async def _dashboard(db, scope, f: Filters, show_pending, show_done, psqc, gate_pending, sales):
    """Option C dashboard above the cards (2026-11-14, Pankaj) — real numbers,
    same filters and who-sees-what as the cards."""
    from services import dash_widgets as dw
    from utils.timezone import app_now
    n_show, n_sold = getattr(_showroom_rows, "last_counts", (len(show_pending), len(show_done)))
    qc = psqc.get("counts", {})
    n_qc_pending, n_pass, n_fail = qc.get("pending", 0), qc.get("pass", 0), qc.get("fail", 0)
    base = [*_in_scope(Sale.device_id, scope), *f.device(), *f.sale()]
    exited = Sale.delivery_status.in_(_EXITED)
    today = app_now().date()
    # Old sales never got a gate status, so with no date filter "waiting"
    # counts only the last 30 days (otherwise it reads as tens of thousands).
    wait_dates = f.dates(Sale.sold_at) if (f.date_from or f.date_to) else [
        Sale.sold_at >= datetime.combine(today - timedelta(days=30), datetime.min.time())]
    n_wait = (await db.execute(select(func.count(Sale.id)).join(Device, Device.id == Sale.device_id)
                               .where(*base, *wait_dates, or_(~exited, Sale.delivery_status.is_(None))))).scalar() or 0
    n_invoices = (await db.execute(select(func.count(func.distinct(Sale.invoice_no))).join(Device, Device.id == Sale.device_id)
                                   .where(*base, *f.dates(Sale.sold_at), Sale.invoice_no.isnot(None), Sale.invoice_no != ""))).scalar() or 0
    days = [today - timedelta(days=13 - i) for i in range(14)]
    start = datetime.combine(days[0], datetime.min.time())
    inv_by_day = dict((await db.execute(
        select(func.date(Sale.sold_at), func.count(Sale.id)).join(Device, Device.id == Sale.device_id)
        .where(*base, Sale.sold_at >= start).group_by(func.date(Sale.sold_at)))).all())
    exit_by_day = dict((await db.execute(
        select(func.date(func.coalesce(Sale.dispatch_date, Sale.sold_at)), func.count(Sale.id))
        .join(Device, Device.id == Sale.device_id)
        .where(*base, exited, func.coalesce(Sale.dispatch_date, Sale.sold_at) >= start)
        .group_by(func.date(func.coalesce(Sale.dispatch_date, Sale.sold_at))))).all())
    move_by_day = {}
    for r in show_pending + show_done:
        if r["moved_at"] and r["moved_at"] >= start:
            k = r["moved_at"].date(); move_by_day[k] = move_by_day.get(k, 0) + 1
    inv_today = inv_by_day.get(today, 0)
    persons = (await db.execute(
        select(Sale.sales_person, func.count(Sale.id)).join(Device, Device.id == Sale.device_id)
        .where(*base, *f.dates(Sale.sold_at), Sale.sales_person.isnot(None), Sale.sales_person != "")
        .group_by(Sale.sales_person).order_by(desc(func.count(Sale.id))).limit(6))).all()
    # Showroom -> exit: days from the showroom move to the gate exit (tags that exited).
    moved_at = {r["d"].id: r["moved_at"] for r in show_done if r["moved_at"]}
    spans = []
    if moved_at:
        for did, ex in (await db.execute(
                select(Sale.device_id, func.coalesce(Sale.dispatch_date, Sale.sold_at))
                .where(Sale.device_id.in_(list(moved_at)[:5000]), exited))).all():
            if ex and moved_at.get(did) and ex >= moved_at[did]:
                spans.append((ex - moved_at[did]).total_seconds() / 86400)
    avg_span = sum(spans) / len(spans) if spans else None
    checked = n_pass + n_fail
    lbl = [d.strftime("%d %b") for d in days]
    fmt_d = lambda dt: dt.strftime("%d %b %H:%M") if dt else ""       # noqa: E731
    ago = lambda dt: fmt_d(dt)                                        # noqa: E731

    kpis = [
        dw.kpi("In showroom", dw.num(n_show), "not sold yet", True, "indigo", [move_by_day.get(d, 0) for d in days]),
        dw.kpi("Awaiting pre-sale QC", dw.num(n_qc_pending), "open WorkIDs", n_qc_pending == 0, "amber"),
        dw.kpi("QC pass rate", ("%d%%" % round(n_pass * 100 / checked)) if checked else "—",
               "%s checked" % dw.num(checked), True, "green"),
        dw.kpi("Invoiced today", dw.num(inv_today), "tags", True, "blue", [inv_by_day.get(d, 0) for d in days]),
        dw.kpi("Waiting for customer", dw.num(n_wait), "sold, not exited" + ("" if (f.date_from or f.date_to) else " · 30 days"), n_wait == 0, "red"),
        dw.kpi("Showroom → exit", ("%.1f days" % avg_span) if avg_span is not None else "—",
               "average", True, "violet"),
    ]
    chip_tag = lambda d: "%s · %s" % (d.barcode, d.model or d.brand or "")   # noqa: E731
    lanes = dw.lanes("Tag journey", 12, [
        ("Moved to showroom", "indigo", n_show, [(chip_tag(r["d"]), r["spec"]) for r in show_pending[:2]]),
        ("Pre-sale QC", "amber", n_qc_pending, [(chip_tag(r["d"]), (r["notes"] or "Pending")[:60]) for r in psqc.get("pending", [])[:2]]),
        ("Sales records", "blue", n_invoices, [("%s · %s" % (r["invoice"], r["customer"] or ""), "%d tags · %s" % (r["count"], r["person"] or ""))
                                               for r in sales[:2]]),
        ("Waiting for customer", "red", n_wait, [("%s · %s" % (g["barcode"], g["sale"].customer_name or ""), g["sale"].invoice_no or "")
                                                 for g in gate_pending[:2]]),
        ("Exited", "green", n_sold, [(chip_tag(r["d"]), "Sold") for r in show_done[:2]]),
    ], sub="Floor movement › Pre-sale QC › Sales › Exit — newest first", tag="Live", tagc="indigo")
    flow = dw.area("Daily flow", 8, lbl, [("Moved in", [move_by_day.get(d, 0) for d in days], "indigo"),
                                         ("Invoiced", [inv_by_day.get(d, 0) for d in days], "blue"),
                                         ("Exited", [exit_by_day.get(d, 0) for d in days], "green")],
                   legv="sum", sub="Tags per day, last 14 days") if any(
        move_by_day.get(d) or inv_by_day.get(d) or exit_by_day.get(d) for d in days) else dw.insight(
        "Daily flow", 8, [("i", "No moves, invoices or exits in the last 14 days for these filters.", "slate")])
    qc = (dw.donut("Pre-sale QC result", 4, [("Pass", n_pass, "green"), ("Fail", n_fail, "red"), ("Pending", n_qc_pending, "amber")],
                   dw.num(n_pass + n_fail + n_qc_pending), "WorkIDs") if (n_pass + n_fail + n_qc_pending)
          else dw.insight("Pre-sale QC result", 4, [("i", "No Pre-Sale QC WorkIDs yet.", "slate")]))
    by_person = (dw.hbars("By sales person", 4, [(p, n) for p, n in persons], color="indigo", lw="90px", sub="Tags sold")
                 if persons else dw.insight("By sales person", 4, [("i", "No sales for these filters.", "slate")]))
    wait_rows = [[g["sale"].invoice_no or g["sale"].sale_number or "—", g["sale"].customer_name or "—", g["barcode"],
                  ("Over 3 days", "red") if g["sale"].sold_at and (app_now() - g["sale"].sold_at).days > 3 else ("Waiting", "amber")]
                 for g in gate_pending[:7]]
    waiting = dw.table("Waiting for customer", 5, [("Invoice", 1.1), ("Customer", 1.5), ("Tag", 1), ("Status", 0.9)],
                       wait_rows or [["—", "Nothing waiting", "", ""]], sub="Newest first")
    events = []
    for r in show_pending[:6]:
        events.append((r["moved_at"], "%s moved to showroom" % r["d"].barcode, "by %s" % (r["moved_by"] or "—"), "indigo"))
    for r in sales[:6]:
        events.append((r["date"], "%s invoiced" % r["invoice"], "%d tags · %s" % (r["count"], r["customer"] or ""), "blue"))
    for k, col in (("fail", "red"), ("pass", "green")):
        for r in psqc.get(k, [])[:4]:
            wo = r["wo"]
            events.append((wo.completed_at if getattr(wo, "completed_at", None) else wo.created_at,
                           "%s %s pre-sale QC" % (r["d"].barcode, "failed" if k == "fail" else "passed"), r["notes"][:50] or "", col))
    events = sorted([e for e in events if e[0]], key=lambda e: e[0], reverse=True)[:6]
    feed = dw.feed("Recent activity", 3, [(t, s_, ago(at), c) for at, t, s_, c in events] or [("Nothing yet", "", "", "slate")])
    widgets = dw.layout([(330, [lanes]), (380, [flow, qc]), (400, [by_person, waiting, feed])])
    return kpis, widgets


@router.get("/sales-coordinator", response_class=HTMLResponse)
async def sales_coordinator(request: Request,
                            entity: str | None = Query(default=None), date_from: str = "", date_to: str = "",
                            category: str = "", sales_person: str = "",
                            db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(view_perm)):
    from utils.master_data import entity_values
    entity, f, admin, moves, scope = await _context(db, current_user, entity, date_from, date_to,
                                                    category, sales_person)
    show_pending, show_done = await _showroom_rows(db, moves, f)
    gate_pending, gate_done = await _gate_rows(db, scope, f)
    persons = [p for (p,) in (await db.execute(
        select(Sale.sales_person).where(Sale.sales_person.isnot(None), Sale.sales_person != "",
                                        *_in_scope(Sale.device_id, scope))
        .distinct().order_by(Sale.sales_person))).all()]
    sales = await _sales_rows(db, scope, f)
    psqc = await _psiqc_rows(db, scope, f)
    dash_kpis, dash_widgets = await _dashboard(db, scope, f, show_pending, show_done, psqc, gate_pending, sales)
    return templates.TemplateResponse("sales_coordinator/index.html", {
        "request": request, "current_user": current_user, "is_admin": admin,
        "dash_kpis": dash_kpis, "dash_widgets": dash_widgets,
        "show_pending": show_pending, "show_done": show_done,
        "sales": sales,
        "psqc": psqc,
        "gate_pending": gate_pending, "gate_done": gate_done,
        "moved_ids": set(moves),        # Pre-Sale QC rows outside it show "No floor movement"
        "entity_opts": await entity_values(db), "category_opts": DEVICE_TYPES_12, "person_opts": persons,
        "f_entity": entity, "f_category": category, "f_person": sales_person,
        "f_from": date_from, "f_to": date_to,
    })


@router.get("/sales-coordinator/invoice")
async def sales_coordinator_invoice(no: str = Query(...), db: AsyncSession = Depends(get_db),
                                    current_user: User = Depends(view_perm)):
    """Sales IDs modal: every sale on one invoice number (a non-admin sees
    only their own Floor Movement tags on it)."""
    q = select(Sale, Device).join(Device, Device.id == Sale.device_id).where(Sale.invoice_no == no)
    if not _is_admin(current_user):
        mine = set(await _showroom_moves(db, current_user.username)) | await _direct_ids(db, current_user.username)
        q = q.where(Sale.device_id.in_(list(mine) or [None]))
    rows = (await db.execute(q.order_by(Sale.sale_number))).all()
    return JSONResponse({"invoice": no, "rows": [{
        "sale_id": s.sale_number, "customer": s.customer_name or "", "category": category_of(d),
        "tag": d.barcode, "make": d.brand or "", "model": d.model or "", "cpu": d.cpu or "",
        "gen": d.generation or "", "ram": gb_text_spaced(d.ram_gb) or "",
        "storage": gb_text_spaced(d.storage_gb) or "",
        "grade": (getattr(d.grade, "value", d.grade) or "") if d.grade else "",
    } for s, d in rows]})


@router.get("/sales-coordinator/search-tags")
async def sales_coordinator_search_tags(q: str = Query(default=""), db: AsyncSession = Depends(get_db),
                                        current_user: User = Depends(view_perm)):
    """Add Tags modal (2026-11-18): Ready for Sale tags to send straight to
    Pre-Sale QC when they reached the coordinator without a Floor Movement.
    A non-admin sees tags of their own entities. Exact tag number first."""
    from routers.pre_sale_iqc import STAGE_CODE
    term = q.strip()
    if not term:
        return JSONResponse({"data": []})
    like = f"%{term}%"
    exact = func.lower(Device.barcode) == term.lower()
    w = [Device.current_stage == DeviceStage.ready_to_sale, Device.is_trashed.isnot(True),
         or_(Device.barcode.ilike(like), Device.model.ilike(like), Device.brand.ilike(like))]
    ents = [e for e in (getattr(current_user, "all_entities", None) or [current_user.entity or ""]) if e]
    if not _is_admin(current_user) and ents:
        w.append(Device.entity.in_(ents))
    devs = (await db.execute(select(Device).where(*w).order_by(exact.desc(), Device.barcode).limit(30))).scalars().all()
    queued = set((await db.execute(select(WorkOrder.device_id).where(
        WorkOrder.device_id.in_([d.id for d in devs] or [None]), WorkOrder.stage == STAGE_CODE,
        WorkOrder.status.in_(["pending", "in_progress"])))).scalars().all()) if devs else set()
    note = None
    if not any((d.barcode or "").lower() == term.lower() for d in devs):
        dev = (await db.execute(select(Device).where(exact).limit(1))).scalar_one_or_none()
        if dev is not None:
            stage = getattr(dev.current_stage, "value", dev.current_stage) or "unknown"
            note = ("is in trash" if dev.is_trashed else
                    f"is not in Ready for Sale (stage: {str(stage).replace('_', ' ')})"
                    if dev.current_stage != DeviceStage.ready_to_sale else "belongs to another entity")
    return JSONResponse({"data": [{
        "barcode": d.barcode, "spec": spec_line(d), "entity": d.entity or "",
        "queued": d.id in queued, "blocked": bool(d.is_blocked),
    } for d in devs], "exact_unavailable": note})


@router.get("/sales-coordinator/export-data")
async def sales_coordinator_export_data(entity: str | None = Query(default=None), date_from: str = "",
                                        date_to: str = "", category: str = "", sales_person: str = "",
                                        db: AsyncSession = Depends(get_db),
                                        current_user: User = Depends(require_module_perm(MODULE, "export"))):
    """Export Report rows, loaded when its modal opens (the page used to
    carry all of them, 6.7 MB on production-size data)."""
    _e, f, _a, _m, scope = await _context(db, current_user, entity, date_from, date_to, category, sales_person)
    rows = await _export_rows(db, scope, f)
    return JSONResponse({"rows": rows, "capped": len(rows) >= EXPORT_CAP})
