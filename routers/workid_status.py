"""
WorkID Status — consolidated view of every WorkID (WorkOrder) with the tag's
Asset History (Stage/Completed Date/Assigned Engineer sourced from a
StageMovement — From/When/By respectively), an IQC→Final-QC timeline, and
filters (workid, tag number, engineer, date range).

Each WorkID is matched to ITS OWN StageMovement, not just the device's
overall latest one (2026-09-02 backfill): a WorkOrder is closed at almost
the same instant the movement that closes out its stage is recorded (see
routers/cosmetic.py advance_stage), so for a completed WorkOrder we pick the
StageMovement for that device whose moved_at is nearest to
WorkOrder.completed_at, out of ALL of that device's Asset History records —
not only its most recent one. A device with several WorkIDs across several
historical stages therefore shows each one's own correct Stage/Completed
Date/Engineer instead of every row collapsing onto the single latest
movement. A still-pending WorkOrder (no completed_at yet) has no closing
movement to match, so it falls back to the device's latest movement as the
best available "where things stand" hint.

2026-09-02 through 2026-09-24 this page also "backfilled" one row per
WorkOrder-less StageMovement (most StageMovements never get a WorkOrder —
bulk stage moves, IQC intake, Final QC pass/fail, etc.), so a tag that
completed a stage with no engineer assignment wasn't invisible here. Removed
2026-09-24 at the user's explicit request: matching a WorkOrder to "its own"
closing movement is inherently a single best-guess pick (closest StageMovement
by timestamp), and a WorkOrder that legitimately has TWO associated movements
(e.g. assignment-time + completion-time — repair.py's l1_pick "Picked by..."
vs. l1l2_complete_to_stress's closing movement) could only ever have one of
them matched, leaving the other to backfill in as a confusing "— No WorkID —"
duplicate of the real row right next to it. This page now shows exactly one
row per real WorkOrder — nothing else — trading away visibility into
WorkOrder-less transitions for never showing a phantom duplicate.

2026-09-19 — Aging/Completed Date redefined to be per-WorkID. Completed
Date only shows once the WorkID is genuinely done (WorkOrder.completed_at
set) -- a still-open WorkOrder (tag still with the same engineer, not yet
handed off to another stage/assignee) shows blank rather than falling back
to the device's latest, possibly-unrelated StageMovement as a "best guess".
Aging is a running day-count from Assigned Date: it keeps counting up every
day the WorkID stays open, then freezes at whatever it reached the moment
the WorkID is genuinely completed -- it is never blank.

"Exclude Admin" filter (2026-09-02) drops rows whose engineer (the
underlying StageMovement.moved_by / WorkOrder.assigned_username, not the
rendered display name) belongs to an admin-role User — resolved once per
request, not by string-matching a display name. CSV export (2026-09-02)
was narrowed to exactly Tag Number / Lot Number / Make / Model / Engineer
Name / Stage / Assigned Date / Completed Date; Lot Number is looked up once
across every device appearing in `items`.
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from templates_config import templates
from database import get_db
from utils.timezone import app_now
from models.user import User, UserRole
from models.device import Device, DeviceStage, StageMovement, STAGE_LABELS, DROPDOWN_STAGES
from models.work_order import WorkOrder
from models.lot import Lot
from utils.attendance_groups import managed_usernames
from services.pna_lookup import active_pna_parts, pna_history
from auth.dependencies import get_current_user

router = APIRouter(tags=["workid_status"])


def _parse_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except (ValueError, AttributeError):
            pass
    return None


def _pna_fields(parts) -> dict:
    """PNA columns for one tag from its ACTIVE DevicePNAPart rows (may be empty)."""
    parts = list(parts or [])
    marked_by = []
    for p in parts:
        mb = (getattr(p, "marked_by", None) or "").strip()
        if mb and mb not in marked_by:
            marked_by.append(mb)
    times = [p.marked_at for p in parts if getattr(p, "marked_at", None)]
    return {
        "pna": bool(parts),
        "pna_parts": [p.part_name for p in parts if getattr(p, "part_name", None)],
        "pna_marked_by": ", ".join(marked_by),
        "pna_marked_at": max(times).strftime("%d-%m-%Y %H:%M") if times else "",
    }


def _pna_history_lines(parts, start, end) -> list:
    """PNA mark/clear events that overlap one WorkID's working window.

    A part counts for a WorkID when it was PNA at any moment between the
    WorkID's Assigned Date and its Completed Date (open-ended while ongoing) --
    i.e. while that engineer held the tag. `parts` are ALL DevicePNAPart rows of
    the tag, active or cleared. Returns display-ready dicts, oldest mark first."""
    lines = []
    for p in parts or []:
        marked = getattr(p, "marked_at", None)
        cleared = getattr(p, "cleared_at", None) if not getattr(p, "is_active", True) else None
        if start and cleared and cleared < start:
            continue          # cleared before this WorkID began
        if end and marked and marked > end:
            continue          # marked after this WorkID finished
        src = "L3/L4" if getattr(p, "source", "") == "l3l4" else "L1/L2"
        lines.append({
            "part": p.part_name,
            "source": src,
            "marked_by": (getattr(p, "marked_by", None) or "").strip(),
            "marked_at": marked.strftime("%d-%m-%Y %H:%M") if marked else "",
            "cleared_by": (getattr(p, "cleared_by", None) or "").strip() if cleared else "",
            "cleared_at": cleared.strftime("%d-%m-%Y %H:%M") if cleared else "",
            "active": bool(getattr(p, "is_active", True)),
        })
    return lines


def _pna_history_text(lines) -> str:
    """One CSV/plain-text cell: 'Part (L1/L2): marked by X dd-mm-yyyy hh:mm; cleared by Y ...' joined by ' | '."""
    out = []
    for l in lines:
        s = f"{l['part']} ({l['source']}): marked by {l['marked_by'] or '—'} {l['marked_at']}".rstrip()
        if l["cleared_at"] or l["cleared_by"]:
            s += f"; cleared by {l['cleared_by'] or '—'} {l['cleared_at']}".rstrip()
        else:
            s += "; still PNA"
        out.append(s)
    return " | ".join(out)


def _multi(value) -> list:
    """Split a comma-separated multiselect filter value into a list — same
    convention as routers/devices.py:_multi(), so the Stage filter here posts
    one comma-joined value via templates/_multiselect_filter.html instead of
    repeating the parameter."""
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = str(value).split(",")
    return [v.strip() for v in raw if v and str(v).strip()]


def _is_l3l4_request_movement(mv) -> bool:
    """True for the synthetic StageMovement repair.py:request_l3l4 writes on
    the L1/L2 engineer's side purely to move current_stage to L3 (so the tag
    drops off /repair/l1) — not a genuine stage completion. Excluded from
    movements_by_device so the ordinary else-branch matching below (closest
    StageMovement to a WorkOrder's own completed_at) never mismatches it to
    some other WorkOrder's completion just because it happens to be the
    nearest-in-time candidate — the L3L4- WorkOrder already shows this
    request under the assigned L3/L4 engineer via the handoff-stage branch
    above, so this movement carries no information not already shown there.

    NOTE: l1l2_complete_to_stress's "L1/L2 completed —..." movement is
    deliberately NOT excluded here (tried 2026-09-24, reverted) — unlike the
    request-side movement above, it IS the correct match for the plain L1/L2
    WorkOrder's own completion (closest StageMovement to WorkOrder.completed_at,
    matched by the ordinary else-branch below), so excluding it from
    movements_by_device entirely starved that match of its correct candidate
    and made the L1/L2 row fall back to displaying its earlier "Picked by..."
    movement instead — a real regression, confirmed via
    _verify_workid_status_l3l4_duplicate.py-style reproduction."""
    return bool(mv.notes) and mv.notes.startswith("Requested to L3/L4")


MAIN_ROW_CAP = 3000


def _fmt(dt, f="%d-%m-%Y %H:%M"):
    return dt.strftime(f) if dt else ""


def _rows_json(items) -> str:
    """The table's rows as compact JSON for the page's DataTable (2026-10-15).

    The table used to be rendered as up to MAIN_ROW_CAP <tr>s of HTML (each
    with its PNA history), which the browser then had to parse and DataTables
    had to read cell by cell before showing page 1. Shipping the rows as data
    with deferRender means only the visible page's rows are ever built.
    Keys are short on purpose: this is up to 3000 rows. Sort keys (as/cs)
    make the date columns sort by real date, not dd-mm-yyyy text."""
    import json
    rows = []
    for it in items:
        a, c = it.get("assigned_date"), it.get("completed_at")
        pna_tip = ""
        if it.get("pna"):
            pna_tip = "Parts: " + ", ".join(it.get("pna_parts") or [])
            if it.get("pna_marked_by"):
                pna_tip += f", Marked by: {it['pna_marked_by']}"
            if it.get("pna_marked_at"):
                pna_tip += f", at: {it['pna_marked_at']}"
        rows.append({
            "k": it["row_key"], "w": it["work_id"], "b": it.get("barcode") or "—",
            "m": it.get("model") or "—", "s": it.get("stage_label") or "—",
            "a": _fmt(a), "ad": _fmt(a, "%d-%m-%Y"), "as": _fmt(a, "%Y%m%d%H%M"),
            "c": _fmt(c), "cd": _fmt(c, "%d-%m-%Y"), "cs": _fmt(c, "%Y%m%d%H%M"),
            "d": it.get("days"), "n": it.get("notes") or "", "e": it.get("engineer") or "",
            "p": bool(it.get("pna")), "pt": pna_tip, "h": it.get("pna_history") or [],
        })
    # "</" escaped so a value can never close the <script> block it sits in.
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


@router.get("/workid-status", response_class=HTMLResponse)
async def workid_status(request: Request, db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(get_current_user),
                        workid: str = Query(default=""),
                        tag: str = Query(default=""),
                        engineer: str = Query(default=""),
                        completed_from: str = Query(default=""),
                        completed_to: str = Query(default=""),
                        cosmetic_stage: str = Query(default=""),
                        exclude_admin: str = Query(default=""),
                        highlight: str = Query(default="")):
    # ── Base query: WorkOrders joined to their Device ──────────────────────────
    # stage="asgn" WorkOrders come from /transfers' "Assign To Employee" field
    # (routers/transfers.py) — plain assignment bookkeeping, not a repair/
    # cosmetic pipeline stage, so they have no Stage/StageMovement here to
    # show and only clutter this page. Left in place (not deleted) so All
    # Inventory's Employee filter/column and Device Detail's Work ID History,
    # which both join WorkOrder directly, keep working.
    stmt = (select(WorkOrder, Device)
            .join(Device, WorkOrder.device_id == Device.id, isouter=True)
            .where(WorkOrder.stage != "asgn")
            .order_by(WorkOrder.assigned_at.desc()))
    if workid:
        stmt = stmt.where(WorkOrder.work_id.ilike(f"%{workid.strip()}%"))
    if tag:
        stmt = stmt.where(WorkOrder.barcode.ilike(f"%{tag.strip()}%"))
    if engineer:
        stmt = stmt.where(WorkOrder.assigned_username == engineer)
    # Completed Date and Stage are both applied AFTER items are built below,
    # not here — the displayed Completed Date column and Stage column are
    # sourced from Asset History (the device's latest StageMovement), not
    # from WorkOrder.completed_at / Device.current_stage, so filtering
    # against those columns here filtered against a value the page no longer
    # showed (the reported "Completed Date filter not applying" bug).
    cf = _parse_date(completed_from)
    ct = _parse_date(completed_to)
    # The Completed From/To window also goes into the SQL BEFORE the row cap
    # below (2026-10-07: a WorkID completed on 6 Oct was "missing" because the
    # cap keeps only the MAIN_ROW_CAP most recently ASSIGNED WorkIDs, and the
    # date filter further down only ever saw those). WorkOrder.completed_at is
    # within seconds of the Completed Date shown (the matched movement), so the
    # SQL window is widened a little and the exact filter below still decides.
    if cf:
        stmt = stmt.where(WorkOrder.completed_at >= cf - timedelta(days=1))
    if ct:
        stmt = stmt.where(WorkOrder.completed_at <= ct.replace(hour=23, minute=59, second=59) + timedelta(days=2))

    is_admin = current_user.role.value == "admin"

    # ── Row visibility (item 33): admin sees everyone; a Group Config manager
    # sees every team member's WorkID records — transitively, so a manager of
    # managers sees every level down the chain (utils/attendance_groups.
    # managed_usernames), across every group they manage, not just one;
    # anyone else sees only their own. ───────────────────────────────────────
    if not is_admin:
        team = await managed_usernames(db, current_user.username)
        visible_usernames = set(team) | {current_user.username}
        stmt = stmt.where(WorkOrder.assigned_username.in_(visible_usernames))

    # ── Row cap on the base WorkOrder query (2026-09-15 — "table taking too
    # much time to load"): the unfiltered default view previously fetched
    # EVERY WorkOrder ever created (no LIMIT at all), then did per-device
    # Asset-History processing over all of them before Stage/Completed-Date
    # even get applied (those are Python-side filters, see below) — the
    # slowest page views were exactly the common "just open the page" case.
    # Capped to the most recent MAIN_ROW_CAP, with a visible truncation
    # notice rather than a silent cut.
    main_total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar() or 0
    main_truncated = main_total > MAIN_ROW_CAP
    stmt = stmt.limit(MAIN_ROW_CAP)

    rows = (await db.execute(stmt)).all()
    device_ids = [d.id for _, d in rows if d is not None]

    finalqc_date_map = {}
    # ── Asset History (Device Detail's "From"/"To"/"By"/"When" table) — Stage,
    # Completed Date and Assigned Engineer below read a StageMovement rather
    # than the device's live current_stage / WorkOrder.completed_at /
    # WorkOrder.assigned_name. movements_by_device holds EVERY movement per
    # device (not just the latest) so each WorkID below can be matched to its
    # own — see _movement_for_work_order. ────────────────────────────────────
    movements_by_device = {}
    display_name_by_username = {}
    if device_ids:
        # Date each device was sent to Final QC (latest movement to final_qc)
        fq_rows = (await db.execute(
            select(StageMovement.device_id, func.max(StageMovement.moved_at))
            .where(StageMovement.device_id.in_(device_ids),
                   StageMovement.to_stage == DeviceStage.final_qc)
            .group_by(StageMovement.device_id)
        )).all()
        for did, moved in fq_rows:
            finalqc_date_map[str(did)] = moved

        # Only the five columns this page reads (2026-10-15 — "page taking too
        # much time to load"): every movement of up to MAIN_ROW_CAP devices
        # used to be built as a full StageMovement ORM object, by far the
        # heaviest step of the request. Plain rows keep the same attribute
        # names (mv.from_stage, mv.moved_at, mv.moved_by, mv.notes), so the
        # matching logic below is unchanged.
        move_rows = (await db.execute(
            select(StageMovement.device_id, StageMovement.from_stage,
                   StageMovement.moved_at, StageMovement.moved_by, StageMovement.notes)
            .where(StageMovement.device_id.in_(device_ids))
            .order_by(StageMovement.moved_at.asc())
        )).all()
        for mv in move_rows:
            if _is_l3l4_request_movement(mv):
                continue
            movements_by_device.setdefault(str(mv.device_id), []).append(mv)

        usernames = ({mv.moved_by for mv in move_rows if mv.moved_by}
                     | {wo.assigned_username for wo, _d in rows if wo.assigned_username})
        if usernames:
            u_rows = (await db.execute(
                select(User.username, User.full_name).where(User.username.in_(usernames))
            )).all()
            display_name_by_username = {uname: full for uname, full in u_rows}

    # L3/L4 repair and the Stress-Test hand-off (routers/repair.py:
    # request_l3l4 / l1l2_complete_to_stress) create an "L3L4-"/"STRS-"
    # WorkOrder to make the assignment visible here. request_l3l4 does now
    # also write a StageMovement (added after this comment, to move
    # current_stage to L3 so the tag drops off /repair/l1) but it's filtered
    # out of movements_by_device above by _is_l3l4_request_movement — it's
    # L1/L2-side bookkeeping, not the L3/L4 engineer's own activity, and must
    # not be matched here as if it were one (2026-09-22: it was leaking
    # through as a phantom backfilled row attributed to the L1/L2 requester,
    # duplicate to the L3L4- WorkOrder row already showing the request under
    # the assigned L3/L4 engineer below). _movement_for_work_order below
    # therefore has no real movement to match and falls back to the device's overall
    # latest movement, which can be from long before this WorkID even
    # existed (2026-09-15 — reported as "L3/L4 WorkIDs not showing": a
    # completed L3L4- WorkOrder was rendering Stage/Completed/Engineer from
    # an unrelated stage the device passed through BEFORE reaching L1, so it
    # never matched a Stage-filtered search for "L3 Repair"). These two
    # prefixes carry their own accurate stage/completed_at/engineer directly
    # on the WorkOrder row, so read from there instead of Asset History.
    _HANDOFF_STAGE = {"L3L4-": DeviceStage.l3, "STRS-": DeviceStage.qc_check}

    def _handoff_stage(wo):
        for prefix, stage in _HANDOFF_STAGE.items():
            if wo.work_id and wo.work_id.startswith(prefix):
                return stage
        return None

    def _movement_for_work_order(wo, dev_movements):
        """Pick the StageMovement that actually corresponds to this WorkID,
        not just the device's overall-latest one. A completed WorkOrder is
        matched to the movement whose moved_at is nearest its completed_at
        (the two are stamped moments apart in the same request — see module
        docstring); a still-pending WorkOrder has no closing movement yet, so
        it falls back to the device's latest as a best-effort hint."""
        if not dev_movements:
            return None
        if wo.completed_at:
            return min(
                dev_movements,
                key=lambda m: abs((m.moved_at - wo.completed_at).total_seconds())
                if m.moved_at else float("inf"),
            )
        return dev_movements[-1]

    today = app_now()
    items = []
    for wo, dev in rows:
        did = str(wo.device_id)
        start = wo.assigned_at or wo.created_at
        finalqc_dt = finalqc_date_map.get(did)

        handoff_stage = _handoff_stage(wo)
        if handoff_stage:
            stage_value = handoff_stage.value
            stage_label = STAGE_LABELS.get(handoff_stage, handoff_stage.value)
            movement_completed_at = wo.completed_at
            movement_engineer = wo.assigned_name or wo.assigned_username or "—"
            engineer_username = wo.assigned_username
        else:
            mv = _movement_for_work_order(wo, movements_by_device.get(did, []))
            if mv:
                stage_value = mv.from_stage.value if mv.from_stage else ""
                stage_label = STAGE_LABELS.get(mv.from_stage, mv.from_stage.value if mv.from_stage else "—")
                movement_engineer = (display_name_by_username.get(mv.moved_by) or mv.moved_by) if mv.moved_by else "—"
                engineer_username = mv.moved_by
            else:
                stage_value, stage_label, movement_engineer = "", "—", "—"
                engineer_username = wo.assigned_username
            # Assigned Engineer is the user the WorkID was ASSIGNED TO. The
            # movement's "By" is only who clicked the move — often an admin
            # bulk-moving or reassigning someone else's tag (2026-10-07: a WorkID
            # assigned to Saroj showed Chandrabhan because an admin bulk-moved
            # the tag). Falls back to the movement's user for WorkIDs with no
            # assignee (receiving / unassigned queue rows).
            if wo.assigned_username:
                movement_engineer = (wo.assigned_name
                                     or display_name_by_username.get(wo.assigned_username)
                                     or wo.assigned_username)
                engineer_username = wo.assigned_username
            # Completed Date only reflects a genuine hand-off: the WorkOrder
            # itself must actually be completed (wo.completed_at set). A
            # still-open WorkOrder means the tag is still with THIS engineer
            # -- it hasn't moved to another stage or been reassigned yet --
            # so it stays blank rather than falling back to the device's
            # latest (possibly unrelated) StageMovement as a "best guess".
            movement_completed_at = mv.moved_at if (mv and wo.completed_at) else None
        # Aging = days from Assigned Date, counting up every day the WorkID
        # stays open -- and freezing at whatever it reached the moment the
        # WorkID is genuinely completed (2026-09-19 clarification: it's a
        # running counter, not blank, while still with this engineer; only
        # Completed Date itself stays blank until then).
        aging_end = movement_completed_at or today
        days = max(0, (aging_end.date() - start.date()).days) if start else None
        items.append({
            "row_key": f"wo-{wo.work_id}",
            "work_id": wo.work_id,
            "device_id": wo.device_id,
            "barcode": wo.barcode or (dev.barcode if dev else "—"),
            "model": (dev.model or dev.brand) if dev else "—",
            "brand": (dev.brand if dev else None),
            "stage_label": stage_label,
            "stage_value": stage_value,
            "wo_status": wo.status,
            "start": start,
            "assigned_date": start,
            "finalqc": finalqc_dt,
            "completed_at": movement_completed_at,
            "days": days,
            "ongoing": movement_completed_at is None,
            "notes": (dev.notes if dev else None),
            "engineer": movement_engineer,
            "engineer_username": engineer_username,
        })

    # Stage filter (applied below, after items are built) reads the same
    # cosmetic_stage query param the removed backfill block used to also
    # scope its own query by.
    stage_vals = _multi(cosmetic_stage)

    # Lot Number (export column) — one lookup covering every device in
    # `items`, rather than joining Lot into the WorkOrder query above.
    all_device_ids = {it["device_id"] for it in items if it.get("device_id")}
    lot_number_by_device = {}
    if all_device_ids:
        lot_rows = (await db.execute(
            select(Device.id, Lot.lot_number)
            .join(Lot, Device.lot_id == Lot.id)
            .where(Device.id.in_(all_device_ids))
        )).all()
        lot_number_by_device = {str(did): ln for did, ln in lot_rows}
    for it in items:
        it["lot_number"] = lot_number_by_device.get(str(it.get("device_id")), "—")

    # PNA (Part Not Available) — one lookup for every device in `items`.
    pna_by_device = await active_pna_parts(db, all_device_ids) if all_device_ids else {}
    pna_hist_by_device = await pna_history(db, all_device_ids) if all_device_ids else {}
    for it in items:
        it.update(_pna_fields(pna_by_device.get(it.get("device_id"), [])))
        it["pna_history"] = _pna_history_lines(
            pna_hist_by_device.get(it.get("device_id"), []),
            it.get("assigned_date"), it.get("completed_at"))
        it["pna_history_text"] = _pna_history_text(it["pna_history"])

    # ── Completed Date / Stage / Exclude Admin filters — applied here (not in
    # the SQL stmt above) so they narrow the SAME values the Completed Date
    # and Stage columns display (Asset History's When/From), not the
    # WorkOrder/Device columns those columns no longer read from. ─────────
    if stage_vals:
        items = [it for it in items if it["stage_value"] in stage_vals]
    if cf:
        items = [it for it in items if it["completed_at"] and it["completed_at"] >= cf]
    if ct:
        ct_end = ct.replace(hour=23, minute=59, second=59)
        items = [it for it in items if it["completed_at"] and it["completed_at"] <= ct_end]
    if exclude_admin:
        admin_usernames = set((await db.execute(
            select(User.username).where(User.role == UserRole.admin)
        )).scalars().all())
        items = [it for it in items if it.get("engineer_username") not in admin_usernames]

    # Sorted by Assigned Date (2026-09-18 — was completed_at-first, which put
    # completed rows out of Assigned Date order whenever their completed_at
    # diverged from assigned_date).
    items.sort(key=lambda it: it["assigned_date"] or datetime.min, reverse=True)

    # ── Card Count tiles — computed from the SAME filtered `items` list, so
    # every filter above (including Completed From/To and Stage) narrows the
    # tiles exactly as it narrows the table. "Assigned" and "Completed" read
    # WorkOrder.status directly (its own pending/in_progress/completed
    # values) rather than the page's separate Final-QC-movement-based
    # "ongoing" concept or completed_at, since those are three genuinely
    # different signals on this page. ──────────────────────────────────────
    # 2026-10-15: tiles by WorkOrder.status (pending = assigned, not started;
    # in_progress = engineer has started; completed). WorkID tiles count
    # rows; Tag tiles count distinct Tag Numbers with a WorkID in that status.
    def _tags(status):
        return len({it["barcode"] for it in items
                    if it["wo_status"] == status and it.get("barcode") and it["barcode"] != "—"})
    tile_counts = {
        "open_workids": sum(1 for it in items if it["wo_status"] != "completed"),
        "closed_workids": sum(1 for it in items if it["wo_status"] == "completed"),
        "open_tags": _tags("pending"),
        "ongoing_tags": _tags("in_progress"),
        "completed_tags": _tags("completed"),
        # Rows whose PNA column shows the PNA badge (active PNA part), after
        # the same filters as the table.
        "total_pna": sum(1 for it in items if it.get("pna")),
    }

    # Distinct engineers for the filter dropdown.
    #
    # Previously admin-only, which left an attendance-group manager — who can
    # see every member's rows — with no way to narrow to one of them. Admin
    # still draws from every work order; everyone else draws from the rows they
    # are allowed to see, so the dropdown can never widen someone's visibility
    # beyond what the row filter above already permits.
    engineers = []
    if is_admin:
        eng_rows = (await db.execute(
            select(WorkOrder.assigned_username, WorkOrder.assigned_name)
            .where(WorkOrder.assigned_username.isnot(None))
            .distinct()
        )).all()
    else:
        eng_rows = [(wo.assigned_username, wo.assigned_name)
                    for wo, _dev in rows if wo.assigned_username]
    seen = set()
    for uname, name in eng_rows:
        if uname and uname not in seen:
            seen.add(uname)
            engineers.append((uname, name or uname))
    engineers.sort(key=lambda kv: kv[1].lower())

    # "Stage" filter (renamed from "Cosmetic Stage") now offers every
    # DeviceStage, not just the cosmetic-line subset — it filters against
    # Asset History's From value (stage_value above), which can be any stage
    # a tag has ever moved off of, not only a cosmetic one.
    cosmetic_stage_choices = [(s.value, STAGE_LABELS.get(s, s.value)) for s in DROPDOWN_STAGES]

    return templates.TemplateResponse("workid_status/list.html", {
        "request": request, "current_user": current_user,
        "items": items, "rows_json": _rows_json(items),
        "engineers": engineers, "is_admin": is_admin,
        "tile_counts": tile_counts, "cosmetic_stage_choices": cosmetic_stage_choices,
        "f_workid": workid, "f_tag": tag, "f_engineer": engineer,
        "f_completed_from": completed_from, "f_completed_to": completed_to,
        "f_cosmetic_stage": cosmetic_stage, "f_exclude_admin": exclude_admin,
        "highlight": highlight,
        "main_truncated": main_truncated, "main_total": main_total,
        "main_cap": MAIN_ROW_CAP,
    })


@router.get("/workid-status/export")
async def workid_status_export(request: Request, db: AsyncSession = Depends(get_db),
                               current_user: User = Depends(get_current_user),
                               workid: str = Query(default=""),
                               tag: str = Query(default=""),
                               engineer: str = Query(default=""),
                               completed_from: str = Query(default=""),
                               completed_to: str = Query(default=""),
                               cosmetic_stage: str = Query(default=""),
                               exclude_admin: str = Query(default="")):
    """CSV of exactly the rows the page is showing.

    Delegates to the page handler rather than repeating its query, so the
    export cannot drift from the table — including the row-visibility rules,
    which are the part that would be damaging to get wrong: a non-admin must
    not be able to export rows the page would not show them.
    """
    import csv as _csv
    import io as _io
    from datetime import date as _date

    page = await workid_status(request=request, db=db, current_user=current_user,
                               workid=workid, tag=tag, engineer=engineer,
                               completed_from=completed_from, completed_to=completed_to,
                               cosmetic_stage=cosmetic_stage, exclude_admin=exclude_admin, highlight="")
    items = page.context["items"]

    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["Tag Number", "Lot Number", "Make", "Model", "Engineer Name", "Stage",
                "Assigned Date", "Completed Date", "PNA", "PNA History"])
    for it in items:
        w.writerow([
            it.get("barcode") or "",
            it.get("lot_number") or "",
            it.get("brand") or "",
            it.get("model") or "",
            it.get("engineer") or "",
            it.get("stage_label") or "",
            it["assigned_date"].strftime("%d-%m-%Y %H:%M") if it.get("assigned_date") else "",
            it["completed_at"].strftime("%d-%m-%Y %H:%M") if it.get("completed_at") else "",
            "Yes" if it.get("pna") else "No",
            it.get("pna_history_text") or "",
        ])
    # utf-8-sig so Excel opens it without mangling non-ASCII names.
    data = buf.getvalue().encode("utf-8-sig")
    fname = f"workid_status_{_date.today().isoformat()}.csv"
    return Response(
        content=data, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
