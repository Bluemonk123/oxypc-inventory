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

# ── Scrap column / Scrap Type filter (2026-10-23) ───────────────────────────
# An L3/L4 engineer's Normal Scrap / Replacement Scrap (routers/repair.py
# l3l4_scrap) closes their L3L4- WorkID and writes the Asset History note
# "L3/L4 <type> — returned to L1/L2" in the same request. A WorkID's scrap
# decision = that note on the same tag within 10 seconds of its close time.
SCRAP_TYPES = {"normal": "Normal Scrap", "replacement": "Replacement Scrap"}
_SCRAP_WINDOW = timedelta(seconds=10)


# The Asset History note each scrap step writes (2026-10-27): L3/L4's scrap
# decision closes the L3L4- WorkID, and L1/L2's "Back to Inventory" for that
# tag (routers/repair.py back_to_production) closes the L1/L2 WorkID. Both
# count as Scrap, apart from Completed.
_SCRAP_NOTE_L34 = "L3/L4 {}"
_SCRAP_NOTE_L12 = "Back to Inventory \u2014 {}"


def _is_l12_wo_clause():
    from sqlalchemy import and_, or_, not_
    return and_(or_(WorkOrder.work_id.like("L1L2-%"), WorkOrder.stage.in_(["l1", "l2"])),
                not_(WorkOrder.work_id.like("L3L4-%")), not_(WorkOrder.work_id.like("STRS-%")))


def _is_l12_wo(wo) -> bool:
    wid = wo.work_id or ""
    return ((wid.startswith("L1L2-") or (wo.stage or "") in ("l1", "l2"))
            and not wid.startswith(("L3L4-", "STRS-")))


def _scrap_exists(*labels):
    """SQL: this WorkOrder row was closed by a scrap step (any of `labels`):
    an L3L4- WorkID with an "L3/L4 <type>" note, or an L1/L2 WorkID with a
    "Back to Inventory — <type>" note, within _SCRAP_WINDOW of its close."""
    from sqlalchemy import and_, exists, or_

    def _near(prefix):
        return exists().where(
            StageMovement.device_id == WorkOrder.device_id,
            or_(*[StageMovement.notes.like(prefix.format(lb) + "%") for lb in labels]),
            StageMovement.moved_at >= WorkOrder.completed_at - _SCRAP_WINDOW,
            StageMovement.moved_at <= WorkOrder.completed_at + _SCRAP_WINDOW,
        )
    return and_(
        WorkOrder.completed_at.isnot(None),
        or_(and_(WorkOrder.work_id.like("L3L4-%"), _near(_SCRAP_NOTE_L34)),
            and_(_is_l12_wo_clause(), _near(_SCRAP_NOTE_L12))),
    )


def _fmt(dt, f="%d-%m-%Y %H:%M"):
    return dt.strftime(f) if dt else ""


# Stage tiles / Stage & Staff Summary (2026-10-27): distinct tags grouped by
# the table's own Stage column (stage_value), so the tiles, the export
# summaries and a Stage filter all describe the same rows. Stages outside
# these groups are left out of the tiles; the summary sheets list them under
# "Other". (When the table is capped, the tiles fall back to each WorkID's own
# stage in SQL — see _sql_stage_tile_counts.)
STAGE_GROUPS = [
    ("l1l2", "L1/L2", ("l1", "l2"), "#0d6efd", "bi-tools"),
    ("l3l4", "L3/L4", ("l3",), "#6f42c1", "bi-cpu"),
    ("stress", "Stress", ("qc_check",), "#fd7e14", "bi-speedometer2"),
    ("cosmetic", "Cosmetic", ("cosmetic_received", "cleaning", "putty", "dry_sanding", "masking",
                              "painting", "water_sanding", "cosmetic_completed"), "#0aa2c0", "bi-droplet"),
    ("final_qc", "Final QC", ("final_qc", "final_qc_pass_hold", "final_qc_fail_hold"), "#0F7A37", "bi-patch-check"),
]
_GROUP_BY_STAGE = {sv: key for key, _l, svs, _c, _i in STAGE_GROUPS for sv in svs}


def _wid_stage_value(work_id, code, code_stage) -> str:
    """Stage a WorkID was created for: the L3/L4 and Stress hand-off prefixes,
    else the stage code stored on the WorkID."""
    wid = work_id or ""
    if wid.startswith("L3L4-"):
        return "l3"
    if wid.startswith("STRS-"):
        return "qc_check"
    st = code_stage.get((code or "").strip())
    return st.value if st else ""


def _build_code_stage() -> dict:
    """WorkOrder.stage code -> DeviceStage (the stage stored ON a WorkID)."""
    from routers.cosmetic import MOVE_STAGE_CODE
    code_stage = {}
    for st in DeviceStage:
        code_stage.setdefault(st.value[:5], st)
    code_stage.update({code: st for st, code in MOVE_STAGE_CODE.items()})
    code_stage.update({"l1": DeviceStage.l1, "l2": DeviceStage.l2, "l3": DeviceStage.l3})
    # Stress Test WorkIDs (2026-10-27): "strs" (Stress Test page result,
    # routers/stress_api.py) and "qc" (STRS- hand-off / bucket assignment)
    # had no mapping, so they never matched Stress anywhere a WorkID's own
    # stage was read (stage tiles, SQL Stage filter).
    code_stage.update({"qc": DeviceStage.qc_check, "strs": DeviceStage.qc_check})
    return code_stage


# ── The table's Stage column, reproduced in SQL (2026-10-27) ────────────────
# The Stage column is worked out per row in Python (see the items loop in
# workid_status): a reassigned WorkID shows its own stage code, an L3L4- /
# STRS- hand-off shows L3 / Stress, and every other WorkID shows the From
# stage of the Asset History movement matched to it (nearest its completed_at,
# else the device's latest). The Stage filter used to run on that column only
# AFTER the MAIN_ROW_CAP cut, so a Stage-only view of a busy stage saw just the
# Stage rows among the 3,000 most recent WorkIDs, while the tiles counted all
# of them. The same three rules as SQL let the filter, the tiles and the export
# all work on every matching WorkID.
def _matched_from_stage_sq():
    """Correlated scalar subquery: From stage of the movement the page matches
    to this WorkOrder row (_movement_for_work_order), ignoring the L3/L4
    request movement (_is_l3l4_request_movement)."""
    from sqlalchemy import case, extract, or_
    nearest = func.abs(extract("epoch", StageMovement.moved_at - WorkOrder.completed_at))
    latest = -extract("epoch", StageMovement.moved_at)
    order = case((WorkOrder.completed_at.isnot(None), nearest), else_=latest)
    return (select(StageMovement.from_stage)
            .where(StageMovement.device_id == WorkOrder.device_id,
                   or_(StageMovement.notes.is_(None),
                       ~StageMovement.notes.like("Requested to L3/L4%")))
            .order_by(order.asc().nulls_last(), StageMovement.moved_at.asc())
            .limit(1)
            .correlate(WorkOrder)
            .scalar_subquery())


def _qc_next_stage_sq(from_value):
    """Correlated scalar subquery: To stage of the move out of `from_value`
    (qc_check = Stress Test, final_qc = Final QC) closest to this WorkOrder's
    completed_at, within 10 min — read by _qc_verdict (2026-10-28)."""
    from sqlalchemy import extract
    diff = func.abs(extract("epoch", StageMovement.moved_at - WorkOrder.completed_at))
    return (select(StageMovement.to_stage)
            .where(StageMovement.device_id == WorkOrder.device_id,
                   StageMovement.from_stage == DeviceStage(from_value),
                   WorkOrder.completed_at.isnot(None), diff <= 600)
            .order_by(diff.asc()).limit(1)
            .correlate(WorkOrder).scalar_subquery())


def _is_reassigned_row():
    from sqlalchemy import and_
    return and_(WorkOrder.status == "reassigned", WorkOrder.completed_at.isnot(None))


def _displayed_stage_clause(stage_vals, code_stage):
    """SQL WHERE: the row's Stage column is one of stage_vals."""
    from sqlalchemy import and_, or_, not_, false
    vals = set(stage_vals)
    codes = [c for c, st in code_stage.items() if st.value in vals]
    codes += [v for v in vals if v not in code_stage]      # unmapped code shown as-is
    handoff = or_(WorkOrder.work_id.like("L3L4-%"), WorkOrder.work_id.like("STRS-%"))
    handoff_match = []
    if "l3" in vals:
        handoff_match.append(WorkOrder.work_id.like("L3L4-%"))
    if "qc_check" in vals:
        handoff_match.append(and_(WorkOrder.work_id.like("STRS-%"),
                                  not_(WorkOrder.work_id.like("L3L4-%"))))
    enum_vals = [st for st in DeviceStage if st.value in vals]
    not_reassigned = func.coalesce(not_(_is_reassigned_row()), True)
    # Reassigned and still-open rows show the stage stored on the WorkID
    # (open ones since 2026-10-28 — see the items loop).
    known = list(code_stage)
    own_stage_row = or_(_is_reassigned_row(),
                        and_(WorkOrder.status.notin_(["completed", "reassigned"]), WorkOrder.stage.in_(known)))
    not_reassigned = not_(own_stage_row)
    return or_(
        and_(own_stage_row, WorkOrder.stage.in_(codes or ["__none__"])),
        and_(not_reassigned, or_(*handoff_match) if handoff_match else false()),
        and_(not_reassigned, not_(handoff),
             _matched_from_stage_sq().in_(enum_vals) if enum_vals else false()),
    )


def _displayed_stage_value(status, completed_at, work_id, code, from_stage, code_stage) -> str:
    """Python twin of _displayed_stage_clause for rows read back from SQL."""
    if status == "reassigned" and completed_at:
        st = code_stage.get((code or "").strip())
        return st.value if st else (code or "")
    if status not in ("completed", "reassigned") and (code or "").strip() in code_stage:
        return code_stage[(code or "").strip()].value       # open: its own stage (2026-10-28)
    wid = work_id or ""
    if wid.startswith("L3L4-"):
        return "l3"
    if wid.startswith("STRS-"):
        return "qc_check"
    return getattr(from_stage, "value", from_stage) or ""


def _stage_group(stage_value) -> str:
    return _GROUP_BY_STAGE.get(stage_value or "", "other")


def _count_status(it) -> str:
    """Status the tiles / summaries count a row under. Same as the WorkID's
    status, except under a Work Date range a WorkID completed OUTSIDE the
    range (it is listed because a PNA part was marked in the range) counts as
    in progress — so Completed is exactly what was completed in the range
    (2026-10-28)."""
    return it.get("count_status") or it.get("wo_status") or ""


def _is_done(status) -> bool:
    return status == "completed"


def _is_open(status) -> bool:
    return status not in ("completed", "reassigned")


_STRESS_FAIL_TO = ("l1", "l2", "l3")
_FQC_PASS_TO = ("final_qc_pass_hold", "ready_to_sale")
_QC_GROUP_FROM = {"stress": "qc_check", "final_qc": "final_qc"}


def _qc_verdict(group, to_stage) -> str:
    """Stress: back to L1/L2 or L3/L4 = Fail, anything else (Cosmetic) = Pass.
    Final QC: to Pass Hold / Ready to Sale = Pass, anything else = Fail."""
    to = getattr(to_stage, "value", to_stage) or ""
    if group == "stress":
        return "fail" if to in _STRESS_FAIL_TO else "pass"
    if group == "final_qc":
        return "pass" if to in _FQC_PASS_TO else "fail"
    return ""


def _qc_result(wo, dev_movements, group) -> str:
    """"pass" / "fail" for a completed Stress Test or Final QC WorkID
    (2026-10-28): where the tag went from that stage at the time the WorkID
    was closed (the move out within 10 minutes). "" if unknown."""
    src = _QC_GROUP_FROM.get(group)
    if not src or not wo.completed_at:
        return ""
    outs = [m for m in dev_movements
            if getattr(m.from_stage, "value", m.from_stage) == src and m.moved_at
            and abs((m.moved_at - wo.completed_at).total_seconds()) <= 600]
    if not outs:
        return ""
    m = min(outs, key=lambda m: abs((m.moved_at - wo.completed_at).total_seconds()))
    return _qc_verdict(group, m.to_stage)


def _stage_tile_counts_from_items(items) -> dict:
    """{group: {"assigned", "completed"}} — distinct TAGS (2026-10-27, same
    unit as the dark tiles and the export summaries), grouped by the table's
    own Stage column (stage_value) so a Stage filter and these tiles always
    describe the same rows. Assigned = every tag with a WorkID in that stage
    (it was assigned, whatever its status now — every row has an Assigned
    Date); Completed = the tags whose WorkID there is completed, so
    Completed is always part of Assigned (2026-10-27, Pankaj)."""
    sets = {key: {"assigned": set(), "completed": set(), "scrap": set(), "waiting": set(),
                  "pass": set(), "fail": set()}
            for key, *_ in STAGE_GROUPS}
    for it in items:
        g = sets.get(_stage_group(it.get("stage_value")))
        tag = it.get("barcode")
        if g is None or not tag or tag == "—":
            continue
        g["assigned"].add(tag)              # every tag given out in this stage
        if it.get("waiting"):
            g["waiting"].add(tag)           # arrived, not picked yet
        if it.get("scrap"):
            g["scrap"].add(tag)             # scrap decision: counted apart from Completed
        elif _is_done(_count_status(it)):
            g["completed"].add(tag)
            if it.get("qc_result") in ("pass", "fail"):
                g[it["qc_result"]].add(tag)     # Stress / Final QC: Pass / Fail (2026-10-28)
    return {k: {n: len(v[n]) for n in v} for k, v in sets.items()}


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
            # Movement Date (2026-10-27): when the WorkID was opened — the
            # tag's arrival / hand-over. Assigned Date moves to the pick time.
            "mv": _fmt(it.get("moved_at")), "ms": _fmt(it.get("moved_at"), "%Y%m%d%H%M"),
            "wt": bool(it.get("waiting")),
            "c": _fmt(c), "cd": _fmt(c, "%d-%m-%Y"), "cs": _fmt(c, "%Y%m%d%H%M"),
            "d": it.get("days"), "n": it.get("notes") or "", "e": it.get("engineer") or "",
            "p": bool(it.get("pna")), "pt": pna_tip, "h": it.get("pna_history") or [],
            "r": _fmt(it.get("reassigned_at")), "rd": _fmt(it.get("reassigned_at"), "%d-%m-%Y"),
            "rt": it.get("reassigned_to") or "",
            "x": it.get("scrap") or "",
            # Tile filters (2026-10-24): status for Assigned / Ongoing /
            # Completed, and whether a customer ever returned the tag.
            "st": _count_status(it), "cr": bool(it.get("cust_return")),
            "sv": it.get("stage_value") or "", "sr": it.get("qc_result") or "",
        })
    # "</" escaped so a value can never close the <script> block it sits in.
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _sql_filtered(full_stmt, stage_vals, cf, ct, exclude_admin, code_stage):
    """full_stmt as (id, bc, did) rows with the Python-side filters re-applied
    in SQL — exact Completed From/To (completed WorkIDs only), Exclude Admin.
    The Stage filter is already part of full_stmt (_displayed_stage_clause,
    applied before the row cap). Same rules as _sql_tile_counts."""
    q = full_stmt.with_only_columns(
        WorkOrder.id.label("id"), WorkOrder.barcode.label("bc"), WorkOrder.device_id.label("did"),
    ).order_by(None)
    if cf or ct:
        q = q.where(WorkOrder.status == "completed")
        if cf:
            q = q.where(WorkOrder.completed_at >= cf)
        if ct:
            q = q.where(WorkOrder.completed_at <= ct.replace(hour=23, minute=59, second=59))
    if exclude_admin:
        admins = select(User.username).where(User.role == UserRole.admin)
        q = q.where(WorkOrder.assigned_username.notin_(admins))
    return q


async def _sql_tile_counts(db, full_stmt, *, stage_vals, cf, ct, exclude_admin, code_stage,
                           w_range=None) -> dict:
    """WorkID Status tiles counted in SQL over every WorkID matching the page's
    SQL filters (no row cap), with the Python-side filters re-applied as SQL:
    Stage (via the stage code stored on the WorkID), exact Completed From/To
    (completed WorkIDs only), Exclude Admin. Same definitions as the in-Python
    tiles, including the L1/L2 "Repair Started" = ongoing rule."""
    from sqlalchemy import and_, case, distinct, exists
    from models.pna_part import DevicePNAPart
    q = full_stmt.with_only_columns(
        WorkOrder.id.label("id"), WorkOrder.status.label("st"), WorkOrder.stage.label("sg"),
        WorkOrder.barcode.label("bc"), WorkOrder.device_id.label("did"),
        Device.l1l2_status.label("l12"),
        _scrap_exists(*SCRAP_TYPES.values()).label("scr"),
        WorkOrder.assigned_user_id.label("au"),
        WorkOrder.assigned_username.label("an"),
        WorkOrder.completed_at.label("ca"),
    ).order_by(None)
    # Stage: already in full_stmt (applied before the row cap).
    if cf or ct:
        q = q.where(WorkOrder.status == "completed")
        if cf:
            q = q.where(WorkOrder.completed_at >= cf)
        if ct:
            q = q.where(WorkOrder.completed_at <= ct.replace(hour=23, minute=59, second=59))
    if exclude_admin:
        admins = select(User.username).where(User.role == UserRole.admin)
        q = q.where(WorkOrder.assigned_username.notin_(admins))
    sub = q.subquery()
    eff = case((and_(sub.c.st == "pending", sub.c.sg.in_(["l1", "l2"]),
                     sub.c.l12 == "Repair Started"), "in_progress"), else_=sub.c.st)
    if w_range:
        # Work Date: a WorkID completed outside the range counts as in progress
        lo, hi = w_range
        eff = case((and_(sub.c.st == "completed",
                         ~func.coalesce(and_(sub.c.ca >= lo, sub.c.ca <= hi), False)), "in_progress"),
                   else_=eff)
    has_pna = exists().where(DevicePNAPart.device_id == sub.c.did, DevicePNAPart.is_active == True)  # noqa: E712
    r = (await db.execute(select(
        func.count().filter(eff.notin_(["completed", "reassigned"])),
        func.count().filter(eff.in_(["completed", "reassigned"])),
        func.count().filter(sub.c.st == "reassigned"),
        func.count(distinct(sub.c.bc)).filter(eff == "pending"),
        func.count(distinct(sub.c.bc)).filter(eff == "in_progress"),
        func.count(distinct(sub.c.bc)).filter(and_(eff == "completed", ~func.coalesce(sub.c.scr, False))),
        func.count(distinct(sub.c.bc)).filter(has_pna),
        func.count(distinct(sub.c.bc)),
        func.count(distinct(sub.c.bc)).filter(and_(sub.c.st.notin_(["completed", "reassigned"]),
                                                   sub.c.au.is_(None),
                                                   func.coalesce(func.trim(sub.c.an), "") == "")),
    ))).one()
    return {"open_workids": r[0], "closed_workids": r[1], "reassigned_workids": r[2],
            "open_tags": r[3], "ongoing_tags": r[4], "completed_tags": r[5], "total_pna": r[6],
            "assigned_tags": r[7], "waiting_tags": r[8]}


async def _sql_stage_tile_counts(db, full_stmt, *, stage_vals, cf, ct, exclude_admin, code_stage,
                                 w_range=None) -> dict:
    """Stage tiles over EVERY matching WorkID when the table is capped (more
    than MAIN_ROW_CAP rows). Distinct tags, like the in-page count, grouped by
    the table's own Stage column reproduced in SQL (_matched_from_stage_sq /
    _displayed_stage_value), so a capped view counts exactly like the table
    would if it held every row."""
    q = _sql_filtered(full_stmt, stage_vals, cf, ct, exclude_admin, code_stage).with_only_columns(
        WorkOrder.stage.label("sg"), WorkOrder.status.label("st"), WorkOrder.work_id.label("wid"),
        WorkOrder.barcode.label("bc"), WorkOrder.completed_at.isnot(None).label("done_at"),
        _matched_from_stage_sq().label("fs"),
        _scrap_exists(*SCRAP_TYPES.values()).label("scr"),
        WorkOrder.assigned_user_id.label("au"),
        WorkOrder.assigned_username.label("an"),
        WorkOrder.completed_at.label("ca"),
        _qc_next_stage_sq("qc_check").label("nxt"),
        _qc_next_stage_sq("final_qc").label("nxf"),
    )
    sub = q.subquery()
    rows = (await db.execute(
        select(sub.c.sg, sub.c.st, sub.c.wid, sub.c.bc, sub.c.done_at, sub.c.fs, sub.c.scr, sub.c.au, sub.c.an,
               sub.c.ca, sub.c.nxt, sub.c.nxf)
    )).all()
    sets = {key: {"assigned": set(), "completed": set(), "scrap": set(), "waiting": set(),
                  "pass": set(), "fail": set()}
            for key, *_ in STAGE_GROUPS}
    for code, st, wid, bc, done_at, fs, scr, au, an, ca, nxt, nxf in rows:
        g = sets.get(_stage_group(_displayed_stage_value(st, done_at, wid, code, fs, code_stage)))
        if g is None or not bc:
            continue
        g["assigned"].add(bc)
        if au is None and not (an or "").strip() and st not in ("completed", "reassigned"):
            g["waiting"].add(bc)
        if scr:
            g["scrap"].add(bc)              # scrap decision: counted apart from Completed
        elif _is_done(st) and not (w_range and not (ca and w_range[0] <= ca <= w_range[1])):
            g["completed"].add(bc)
            if nxt is not None and g is sets.get("stress"):
                g[_qc_verdict("stress", nxt)].add(bc)
            elif nxf is not None and g is sets.get("final_qc"):
                g[_qc_verdict("final_qc", nxf)].add(bc)
    return {k: {n: len(v[n]) for n in v} for k, v in sets.items()}


@router.get("/workid-status", response_class=HTMLResponse)
async def workid_status(request: Request, db: AsyncSession = Depends(get_db),
                        current_user: User = Depends(get_current_user),
                        workid: str = Query(default=""),
                        tag: str = Query(default=""),
                        engineer: str = Query(default=""),
                        completed_from: str = Query(default=""),
                        completed_to: str = Query(default=""),
                        work_from: str = Query(default=""),
                        work_to: str = Query(default=""),
                        cosmetic_stage: str = Query(default=""),
                        exclude_admin: str = Query(default=""),
                        show_reassigned: str = Query(default=""),
                        scrap_type: str = Query(default="all"),
                        highlight: str = Query(default="")):
    return await _workid_status_page(
        request=request, db=db, current_user=current_user, workid=workid, tag=tag,
        engineer=engineer, completed_from=completed_from, completed_to=completed_to,
        work_from=work_from, work_to=work_to,
        cosmetic_stage=cosmetic_stage, exclude_admin=exclude_admin,
        show_reassigned=show_reassigned, scrap_type=scrap_type, highlight=highlight,
        row_cap=MAIN_ROW_CAP)


async def _workid_status_page(*, request, db, current_user, workid="", tag="", engineer="",
                              completed_from="", completed_to="", work_from="", work_to="",
                              cosmetic_stage="",
                              exclude_admin="", show_reassigned="", scrap_type="all",
                              highlight="", row_cap=MAIN_ROW_CAP):
    """The page's data. row_cap=None loads every matching WorkID (Export,
    2026-10-27 — the workbook covers all matching rows, not just the 3,000
    the page table loads)."""
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
    # "Show Reassigned" (2026-10-23, off by default): WorkIDs taken off an
    # engineer by Pick This / Change Engineer / a stage move are hidden from the
    # table, tiles and Export unless ticked. Filtered here, before the row cap.
    # A link to one WorkID (?highlight=) always finds it, reassigned or not.
    if not show_reassigned and not highlight:
        stmt = stmt.where(WorkOrder.status != "reassigned")
    # Scrap Type filter (default "All Scrap Types" since 2026-10-28; was
    # "No Scrap"): in SQL before the row cap, so the tiles and Export follow
    # it too. ?highlight= always finds its WorkID.
    scrap_type = (scrap_type or "all").strip().lower()
    if scrap_type not in ("all", "none", *SCRAP_TYPES):
        scrap_type = "all"
    scrap_clause = None
    if not highlight and scrap_type != "all":
        from sqlalchemy import not_
        if scrap_type == "none":
            scrap_clause = not_(_scrap_exists(*SCRAP_TYPES.values()))
        else:
            scrap_clause = _scrap_exists(SCRAP_TYPES[scrap_type])
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

    # Work Date From/To (2026-10-28): a day's work = WorkIDs COMPLETED in the
    # range, plus WorkIDs on which their engineer marked a PNA part in the
    # range while the WorkID was open (assigned .. completed) — so an engineer's "worked on"
    # tags include the ones still waiting for parts. In SQL before the row
    # cap, so the table, tiles and Export all follow it.
    wf, wt = _parse_date(work_from), _parse_date(work_to)
    if wf or wt:
        from sqlalchemy import and_, exists, or_
        from models.pna_part import DevicePNAPart
        w_end = wt.replace(hour=23, minute=59, second=59) if wt else None
        done_in = [WorkOrder.status == "completed", WorkOrder.completed_at.isnot(None)]
        pna_in = [DevicePNAPart.device_id == WorkOrder.device_id,
                  DevicePNAPart.marked_by == WorkOrder.assigned_username,   # marked by that WorkID's engineer
                  DevicePNAPart.marked_at >= func.coalesce(WorkOrder.assigned_at, WorkOrder.created_at)
                  - timedelta(minutes=1),
                  or_(WorkOrder.completed_at.is_(None),
                      DevicePNAPart.marked_at <= WorkOrder.completed_at + timedelta(minutes=1))]
        if wf:
            done_in.append(WorkOrder.completed_at >= wf)
            pna_in.append(DevicePNAPart.marked_at >= wf)
        if w_end:
            done_in.append(WorkOrder.completed_at <= w_end)
            pna_in.append(DevicePNAPart.marked_at <= w_end)
        stmt = stmt.where(or_(and_(*done_in), exists().where(*pna_in)))

    # Stage filter in SQL, BEFORE the row cap (2026-10-27): same rule as the
    # table's Stage column (_displayed_stage_clause), so a Stage-only view
    # loads every row of that stage, not just those among the latest 3,000.
    # The exact Python filter further down still runs and agrees with it.
    _code_stage = _build_code_stage()
    stage_vals = _multi(cosmetic_stage)
    if stage_vals:
        stmt = stmt.where(_displayed_stage_clause(stage_vals, _code_stage))

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
    # Every filter except Scrap Type — the Total Scrap tile counts from this
    # (2026-10-24), so it isn't stuck at 0 under the default "No Scrap".
    stmt_any_scrap = stmt
    if scrap_clause is not None:
        stmt = stmt.where(scrap_clause)

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
    main_truncated = row_cap is not None and main_total > row_cap
    full_stmt = stmt            # every matching WorkID — the tiles count these (see below)
    if row_cap is not None:
        stmt = stmt.limit(row_cap)

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
            select(StageMovement.device_id, StageMovement.from_stage, StageMovement.to_stage,
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

    # ── Reassigned WorkIDs (2026-10-23) ─────────────────────────────────────
    # Pick This / Change Engineer / a stage move take a tag off its engineer:
    # their WorkID is closed as "reassigned", not completed. Such a row shows
    # the stage stored ON the WorkID (the nearest Asset History movement is the
    # pick/move itself, which mislabelled e.g. a Masking WorkID "L1/L2
    # Repair"), no Completed Date, and who took it over: the WorkID opened
    # for the same tag within a minute, else the stage move at that moment.
    # (_code_stage is built above, before the row cap — _build_code_stage.)
    def _wo_stage(wo):
        st = _code_stage.get((wo.stage or "").strip())
        return (st.value, STAGE_LABELS.get(st, st.value)) if st else (wo.stage or "", (wo.stage or "—").upper())

    successor_by_wo = {}
    _re_devs = {wo.device_id for wo, _d in rows if wo.status == "reassigned" and wo.completed_at}
    if _re_devs:
        _others = (await db.execute(
            select(WorkOrder.id, WorkOrder.device_id, WorkOrder.assigned_at,
                   WorkOrder.assigned_name, WorkOrder.assigned_username)
            .where(WorkOrder.device_id.in_(_re_devs), WorkOrder.stage != "asgn")
        )).all()
        for wo, _d in rows:
            if wo.status != "reassigned" or not wo.completed_at:
                continue
            near = [o for o in _others if o.device_id == wo.device_id and o.id != wo.id and o.assigned_at
                    and abs((o.assigned_at - wo.completed_at).total_seconds()) <= 60]
            if near:
                o = min(near, key=lambda o: abs((o.assigned_at - wo.completed_at).total_seconds()))
                successor_by_wo[wo.id] = "to " + (o.assigned_name or o.assigned_username or "unassigned")
            else:
                mvs = [m for m in movements_by_device.get(str(wo.device_id), []) if m.moved_at
                       and abs((m.moved_at - wo.completed_at).total_seconds()) <= 60]
                if mvs:
                    m = min(mvs, key=lambda m: abs((m.moved_at - wo.completed_at).total_seconds()))
                    who = display_name_by_username.get(m.moved_by) or m.moved_by or ""
                    successor_by_wo[wo.id] = ("moved to " + STAGE_LABELS.get(m.to_stage, getattr(m.to_stage, "value", "") or "")
                                              + (f" by {who}" if who else ""))

    # Scrap column: the scrap note matched to each closed L3L4- WorkID, and
    # (2026-10-27) the "Back to Inventory" note matched to each closed L1/L2
    # WorkID of a scrapped tag — same rule as _scrap_exists.
    scrap_by_wo = {}
    _closed = [(wo, _SCRAP_NOTE_L34) for wo, _d in rows
               if (wo.work_id or "").startswith("L3L4-") and wo.completed_at]
    _closed += [(wo, _SCRAP_NOTE_L12) for wo, _d in rows if _is_l12_wo(wo) and wo.completed_at]
    if _closed:
        from sqlalchemy import or_ as _or
        _notes = (await db.execute(
            select(StageMovement.device_id, StageMovement.moved_at, StageMovement.notes)
            .where(StageMovement.device_id.in_({wo.device_id for wo, _p in _closed}),
                   _or(*[StageMovement.notes.like(p.format(lb) + "%")
                         for p in (_SCRAP_NOTE_L34, _SCRAP_NOTE_L12) for lb in SCRAP_TYPES.values()]))
        )).all()
        for wo, prefix in _closed:
            near = [n for n in _notes if n.device_id == wo.device_id and n.moved_at
                    and n.notes.startswith(prefix.format(""))
                    and abs(n.moved_at - wo.completed_at) <= _SCRAP_WINDOW]
            if near:
                n = min(near, key=lambda n: abs((n.moved_at - wo.completed_at).total_seconds()))
                scrap_by_wo[wo.id] = next((lb for lb in SCRAP_TYPES.values()
                                           if n.notes.startswith(prefix.format(lb))), "")

    today = app_now()
    items = []
    for wo, dev in rows:
        did = str(wo.device_id)
        start = wo.assigned_at or wo.created_at
        finalqc_dt = finalqc_date_map.get(did)

        handoff_stage = _handoff_stage(wo)
        reassigned_at = wo.completed_at if wo.status == "reassigned" else None
        if reassigned_at:
            stage_value, stage_label = _wo_stage(wo)
            movement_completed_at = None
            movement_engineer = (wo.assigned_name or display_name_by_username.get(wo.assigned_username)
                                 or wo.assigned_username or "—")
            engineer_username = wo.assigned_username
        elif handoff_stage:
            stage_value = handoff_stage.value
            stage_label = STAGE_LABELS.get(handoff_stage, handoff_stage.value)
            movement_completed_at = wo.completed_at
            movement_engineer = wo.assigned_name or wo.assigned_username or "—"
            engineer_username = wo.assigned_username
        else:
            mv = _movement_for_work_order(wo, movements_by_device.get(did, []))
            own = _code_stage.get((wo.stage or "").strip()) if _is_open(wo.status) else None
            if own is not None:
                # Still-open WorkID (2026-10-28): the work is at the stage stored
                # ON it. Since WorkIDs open when a tag ARRIVES, the device's
                # latest movement is that arrival, whose From stage is the stage
                # it came from (an L1/L2 WorkID after a Stress Fail showed
                # "Stress Test").
                stage_value, stage_label = own.value, STAGE_LABELS.get(own, own.value)
                movement_engineer, engineer_username = "—", wo.assigned_username
            elif mv:
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
        aging_end = movement_completed_at or reassigned_at or today
        days = max(0, (aging_end.date() - start.date()).days) if start else None
        # Waiting = an open WorkID nobody has picked yet (2026-10-27, one
        # WorkID per visit: a tag arrives at L1/L2, L3/L4, Stress or Final QC
        # with a pending, unassigned WorkID). Shown as "Not picked", never
        # under the person who happened to move the tag.
        # Held = a user id OR a username (older WorkIDs carry only the name).
        waiting = (wo.status not in ("completed", "reassigned") and wo.assigned_user_id is None
                   and not (wo.assigned_username or "").strip())
        if waiting:
            movement_engineer, engineer_username = "Not picked", None
        items.append({
            "row_key": f"wo-{wo.work_id}",
            "qc_result": (_qc_result(wo, movements_by_device.get(did, []), _stage_group(stage_value))
                          if wo.status == "completed" else ""),
            "moved_at": wo.created_at,          # Movement Date: when the tag arrived / was handed over
            "waiting": waiting,
            "work_id": wo.work_id,
            "device_id": wo.device_id,
            "barcode": wo.barcode or (dev.barcode if dev else "—"),
            "model": (dev.model or dev.brand) if dev else "—",
            "brand": (dev.brand if dev else None),
            "stage_label": stage_label,
            "stage_value": stage_value,
            # The stage the WorkID itself was created for (stage tiles and
            # the export summaries, 2026-10-27) — same rule as the SQL count.
            "wid_stage": _wid_stage_value(wo.work_id, wo.stage, _code_stage),
            # Effective status for the tiles: an open L1/L2 WorkID whose tag
            # shows "Repair Started" is ongoing even if the WorkID itself was
            # left "pending" (L1/L2 Start didn't update it before 2026-10-23).
            "wo_status": ("in_progress" if (wo.status == "pending" and (wo.stage or "") in ("l1", "l2")
                                            and dev is not None and dev.l1l2_status == "Repair Started")
                          else wo.status),
            "start": start,
            "assigned_date": start,
            "finalqc": finalqc_dt,
            "completed_at": movement_completed_at,
            "wo_completed_at": wo.completed_at,
            "days": days,
            "ongoing": movement_completed_at is None and reassigned_at is None,
            "reassigned_at": reassigned_at,
            "reassigned_to": successor_by_wo.get(wo.id, ""),
            "scrap": scrap_by_wo.get(wo.id, ""),
            "notes": (dev.notes if dev else None),
            "engineer": movement_engineer,
            "engineer_username": engineer_username,
        })

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
    _w_range = ((wf or datetime(1900, 1, 1)),
                (wt.replace(hour=23, minute=59, second=59) if wt else datetime(2999, 1, 1))) if (wf or wt) else None
    if wf or wt:
        w_lo = wf or datetime.min
        w_hi = wt.replace(hour=23, minute=59, second=59) if wt else datetime.max
        for it in items:
            if it.get("wo_status") == "completed":
                done_at = it.get("completed_at") or it.get("wo_completed_at")
                if not (done_at and w_lo <= done_at <= w_hi):
                    it["count_status"] = "in_progress"
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
        # A WorkID closed by an L3/L4 scrap decision is stored as "completed"
        # but counted under Scrap, not Completed (2026-10-27).
        return len({it["barcode"] for it in items
                    if _count_status(it) == status and it.get("barcode") and it["barcode"] != "—"
                    and not (status == "completed" and it.get("scrap"))})
    if main_truncated:
        # More WorkIDs match than the table loads (MAIN_ROW_CAP): count the
        # tiles in SQL over ALL of them, with the same filters, instead of
        # just the loaded rows (2026-10-23 — "Ongoing tile not calculating":
        # older open WorkIDs fell outside the loaded rows and were missed).
        tile_counts = await _sql_tile_counts(
            db, full_stmt, stage_vals=stage_vals, cf=cf, ct=ct,
            exclude_admin=bool(exclude_admin), code_stage=_code_stage, w_range=_w_range)
    else:
        tile_counts = None
    # Total Scrap (WorkIDs with an L3/L4 scrap decision, Scrap Type filter
    # ignored) and Total Customer Return (distinct tags a customer returned),
    # 2026-10-24 — both over every matching WorkID, not just loaded rows.
    from models.sales import Return
    scrap_q = _sql_filtered(stmt_any_scrap.where(_scrap_exists(*SCRAP_TYPES.values())),
                            stage_vals, cf, ct, bool(exclude_admin), _code_stage).subquery()
    # Distinct tags (2026-10-27): a scrapped tag can now have two scrap
    # WorkIDs (L3/L4's decision and L1/L2's Back to Inventory).
    total_scrap = (await db.execute(select(func.count(func.distinct(scrap_q.c.bc))))).scalar() or 0
    if main_truncated:
        cr_q = _sql_filtered(full_stmt, stage_vals, cf, ct, bool(exclude_admin), _code_stage).subquery()
        total_cust_return = (await db.execute(
            select(func.count(func.distinct(cr_q.c.bc)))
            .where(select(Return.id).where(Return.device_id == cr_q.c.did).exists())
        )).scalar() or 0
    _dids = {it["device_id"] for it in items if it.get("device_id")}
    _ret = set((await db.execute(select(Return.device_id).where(Return.device_id.in_(_dids)).distinct())
                ).scalars().all()) if _dids else set()
    for it in items:
        it["cust_return"] = it.get("device_id") in _ret
    if not main_truncated:
        total_cust_return = len({it["barcode"] for it in items
                                 if it.get("device_id") in _ret and it.get("barcode") not in (None, "—")})

    tile_counts = tile_counts or {
        "open_workids": sum(1 for it in items if _count_status(it) not in ("completed", "reassigned")),
        "closed_workids": sum(1 for it in items if _count_status(it) in ("completed", "reassigned")),
        "reassigned_workids": sum(1 for it in items if it["wo_status"] == "reassigned"),
        "open_tags": _tags("pending"),
        "waiting_tags": len({it["barcode"] for it in items
                             if it.get("waiting") and it.get("barcode") and it["barcode"] != "—"}),
        # Assigned Tags tile (2026-10-27): every tag given a WorkID in this
        # view, whatever its status now — same rule as the stage tiles. It
        # used to count only not-yet-started ("pending") WorkIDs, which read
        # 0 under a Completed date range while the stage tiles showed hundreds.
        "assigned_tags": len({it["barcode"] for it in items if it.get("barcode") and it["barcode"] != "—"}),
        "ongoing_tags": _tags("in_progress"),
        "completed_tags": _tags("completed"),
        # Tags whose PNA column shows the PNA badge (active PNA part), after
        # the same filters as the table — tags, like every other tile
        # (2026-10-28; was rows, so it could exceed Assigned Tags).
        "total_pna": len({it["barcode"] for it in items
                          if it.get("pna") and it.get("barcode") and it["barcode"] != "—"}),
    }
    tile_counts["total_scrap"] = total_scrap
    tile_counts["total_cust_return"] = total_cust_return
    # Stage tiles (2026-10-27): L1/L2, L3/L4, Stress, Cosmetic, Final QC.
    if main_truncated:
        stage_tiles = await _sql_stage_tile_counts(
            db, full_stmt, stage_vals=stage_vals, cf=cf, ct=ct,
            exclude_admin=bool(exclude_admin), code_stage=_code_stage, w_range=_w_range)
    else:
        stage_tiles = _stage_tile_counts_from_items(items)

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
    # Only stages a WorkID can show under (2026-10-28): no work is assigned
    # at GRN, IQC, Returned, Scrap, Scrap for Sale or On Rent, so those
    # choices always came back empty. Ready to Sale stays — WorkIDs left open
    # when a tag moved on show there.
    _no_workid = {"grn", "iqc", "returned", "scrapped", "scrap_for_sale", "on_rent"}
    cosmetic_stage_choices = [(s.value, STAGE_LABELS.get(s, s.value)) for s in DROPDOWN_STAGES
                              if s.value not in _no_workid]

    return templates.TemplateResponse("workid_status/list.html", {
        "request": request, "current_user": current_user,
        "items": items, "rows_json": _rows_json(items),
        "engineers": engineers, "is_admin": is_admin,
        "tile_counts": tile_counts, "cosmetic_stage_choices": cosmetic_stage_choices,
        "stage_tiles": stage_tiles, "stage_groups": STAGE_GROUPS,
        "f_workid": workid, "f_tag": tag, "f_engineer": engineer,
        "f_completed_from": completed_from, "f_completed_to": completed_to,
        "f_work_from": work_from, "f_work_to": work_to,
        "f_cosmetic_stage": cosmetic_stage, "f_exclude_admin": exclude_admin,
        "f_show_reassigned": show_reassigned,
        "f_scrap_type": scrap_type,
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
                               work_from: str = Query(default=""),
                               work_to: str = Query(default=""),
                               cosmetic_stage: str = Query(default=""),
                               exclude_admin: str = Query(default=""),
                               show_reassigned: str = Query(default=""),
                               scrap_type: str = Query(default="all")):
    """Excel workbook of exactly the rows the page is showing (2026-10-27;
    was a single CSV): Tag Summary (the former CSV columns, one row per
    WorkID), Stage Summary and Staff Summary.

    Delegates to the page handler rather than repeating its query, so the
    export cannot drift from the table — including the row-visibility rules,
    which are the part that would be damaging to get wrong: a non-admin must
    not be able to export rows the page would not show them.
    """
    import io as _io
    from datetime import date as _date
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    # row_cap=None: every matching WorkID, not only the 3,000 the page table
    # loads (2026-10-27), so the summary sheets agree with the tiles.
    page = await _workid_status_page(request=request, db=db, current_user=current_user,
                                     workid=workid, tag=tag, engineer=engineer,
                                     completed_from=completed_from, completed_to=completed_to,
                                     work_from=work_from, work_to=work_to,
                                     cosmetic_stage=cosmetic_stage, exclude_admin=exclude_admin,
                                     show_reassigned=show_reassigned, scrap_type=scrap_type,
                                     highlight="", row_cap=None)
    items = page.context["items"]

    hdr_font = Font(bold=True, color="FFFFFF")
    hdr_fill = PatternFill("solid", fgColor="0E3A7A")
    bold = Font(bold=True)

    def _sheet(ws, header, rows, total_row=None):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = hdr_font, hdr_fill
            c.alignment = Alignment(vertical="center")
        for r in rows:
            ws.append(r)
        if total_row:
            ws.append(total_row)
            for c in ws[ws.max_row]:
                c.font = bold
        ws.freeze_panes = "A2"
        for i, col in enumerate(ws.iter_cols(min_row=1, max_row=min(ws.max_row, 500)), start=1):
            width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 10), 60)

    def _status(it):
        if it.get("reassigned_at"):
            return "Reassigned"
        if it.get("scrap"):
            return "Scrap"          # closed by an L3/L4 scrap decision — apart from Completed
        return "Completed" if _count_status(it) == "completed" else "Open"

    wb = Workbook()
    # ── Sheet 1: Tag Summary (the former CSV) ──
    ws = wb.active
    ws.title = "Tag Summary"
    tag_rows = []
    for it in items:
        tag_rows.append([
            it.get("barcode") or "",
            it.get("lot_number") or "",
            it.get("brand") or "",
            it.get("model") or "",
            it.get("engineer") or "",
            it.get("stage_label") or "",
            it.get("moved_at"),
            it.get("assigned_date"),
            it.get("completed_at"),
            "Waiting (not picked)" if it.get("waiting") else
            (("Reassigned " + it["reassigned_at"].strftime("%Y-%m-%d %H:%M")
              + (f" ({it['reassigned_to']})" if it.get("reassigned_to") else ""))
             if it.get("reassigned_at") else ("Scrap" if it.get("scrap")
                                              else "Completed" if it.get("completed_at") else "Open")),
            it.get("scrap") or "",
            {"pass": "Pass", "fail": "Fail"}.get(it.get("qc_result") or "", ""),
            "Yes" if it.get("pna") else "No",
            it.get("pna_history_text") or "",
        ])
    _sheet(ws, ["Tag Number", "Lot Number", "Make", "Model", "Engineer Name", "Stage",
                "Movement Date", "Assigned Date", "Completed Date", "Status", "Scrap", "QC Result", "PNA",
                "PNA History"], tag_rows)
    for row in ws.iter_rows(min_row=2, min_col=7, max_col=9):
        for c in row:
            c.number_format = "yyyy-mm-dd hh:mm"

    # Both summaries count distinct Tags, not WorkIDs (2026-10-27): a tag
    # with several WorkIDs in one stage counts once there. A tag can still
    # appear under both Assigned and Completed (e.g. an old completed WorkID
    # and a newer open one), so Assigned + Completed can exceed Total Tags.
    def _bucket():
        return {"total": set(), "assigned": set(), "completed": set(), "reassigned": set(),
                "scrap": set(), "pna": set(), "days": [], "groups": {},
                "stress_pass": set(), "stress_fail": set(), "fqc_pass": set(), "fqc_fail": set()}

    def _add(b, it):
        tag = it.get("barcode")
        if not tag or tag == "—":
            return
        st = _status(it)
        b["total"].add(tag)
        b["assigned" if st == "Open" else st.lower()].add(tag)
        if it.get("pna"):
            b["pna"].add(tag)
        if st == "Completed" and it.get("qc_result") in ("pass", "fail"):
            grp = _stage_group(it.get("stage_value"))
            if grp in ("stress", "final_qc"):
                b[("stress_" if grp == "stress" else "fqc_") + it["qc_result"]].add(tag)
        if st == "Completed" and it.get("days") is not None:
            b["days"].append(it["days"])
        b["groups"].setdefault(_stage_group(it.get("stage_value")), set()).add(tag)

    def _avg(days):
        return round(sum(days) / len(days), 1) if days else ""

    def _n(b, k):
        return len(b[k])

    group_label = {key: lbl for key, lbl, *_ in STAGE_GROUPS}
    group_label["other"] = "Other"
    group_order = [key for key, *_ in STAGE_GROUPS] + ["other"]

    # ── Sheet 2: Stage Summary — one row per stage, grouped ──
    by_stage = {}
    for it in items:
        # Grouped by the table's Stage column (2026-10-27), same as the tiles.
        key = (_stage_group(it.get("stage_value")), it.get("stage_label") or "—")
        _add(by_stage.setdefault(key, _bucket()), it)
    total_b = _bucket()
    for it in items:
        _add(total_b, it)
    def _pf(b, g):
        if g == "stress":
            return [_n(b, "stress_pass"), _n(b, "stress_fail")]
        if g == "final_qc":
            return [_n(b, "fqc_pass"), _n(b, "fqc_fail")]
        return ["", ""]

    stage_rows = [[group_label[g], lbl, _n(b, "total"), _n(b, "assigned"), _n(b, "completed")]
                  + _pf(b, g) + [_n(b, "scrap"), _n(b, "reassigned"), _n(b, "pna"), _avg(b["days"])]
                  for (g, lbl), b in sorted(by_stage.items(),
                                             key=lambda kv: (group_order.index(kv[0][0]), kv[0][1]))]
    _sheet(wb.create_sheet("Stage Summary"),
           ["Stage Group", "Stage", "Total Tags", "Assigned Tags (Open)", "Completed Tags",
            "Pass Tags", "Fail Tags",
            "Scrap Tags", "Reassigned Tags", "Tags with PNA", "Avg Days to Complete"],
           stage_rows,
           ["Total", "", _n(total_b, "total"), _n(total_b, "assigned"), _n(total_b, "completed"),
            _n(total_b, "stress_pass") + _n(total_b, "fqc_pass"), _n(total_b, "stress_fail") + _n(total_b, "fqc_fail"),
            _n(total_b, "scrap"), _n(total_b, "reassigned"), _n(total_b, "pna"), _avg(total_b["days"])])

    # ── Sheet 3: Staff Summary — one row per engineer ──
    by_eng = {}
    for it in items:
        _add(by_eng.setdefault(it.get("engineer") or "—", _bucket()), it)
    staff_rows = [[eng, _n(b, "total"), _n(b, "assigned"), _n(b, "completed"), _n(b, "scrap"), _n(b, "reassigned")]
                  + [len(b["groups"].get(g, ())) for g in group_order]
                  + [_n(b, "stress_pass"), _n(b, "stress_fail"), _n(b, "fqc_pass"), _n(b, "fqc_fail")]
                  + [_n(b, "pna"), _avg(b["days"])]
                  for eng, b in sorted(by_eng.items(), key=lambda kv: (-len(kv[1]["total"]), kv[0].lower()))]
    _sheet(wb.create_sheet("Staff Summary"),
           ["Engineer", "Total Tags", "Assigned Tags (Open)", "Completed Tags", "Scrap Tags", "Reassigned Tags"]
           + [group_label[g] + " Tags" for g in group_order]
           + ["Stress Pass", "Stress Fail", "Final QC Pass", "Final QC Fail"]
           + ["Tags with PNA", "Avg Days to Complete"],
           staff_rows,
           ["Total", _n(total_b, "total"), _n(total_b, "assigned"), _n(total_b, "completed"),
            _n(total_b, "scrap"), _n(total_b, "reassigned")]
           + [len(total_b["groups"].get(g, ())) for g in group_order]
           + [_n(total_b, "stress_pass"), _n(total_b, "stress_fail"), _n(total_b, "fqc_pass"), _n(total_b, "fqc_fail")]
           + [_n(total_b, "pna"), _avg(total_b["days"])])

    out = _io.BytesIO()
    wb.save(out)
    fname = f"workid_status_{_date.today().isoformat()}.xlsx"
    return Response(
        content=out.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
