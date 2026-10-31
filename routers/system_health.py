"""System Health for admins (2026-10-22).

Lets an admin — or Claude through the browser pane — see whether production's
database schema matches the code, without a terminal on the server.

  GET  /admin/system/health      read-only JSON:
       - what the startup schema step did (fixed / remaining / swallowed warning)
       - whether OXYPC_AUTO_FIX is on
       - a live CHECK-ONLY scan: missing tables, columns, enum values, stage rows
       - deploy stamp (ASSET_VERSION) and process uptime
  POST /admin/system/schema-fix  runs the same auto-fix startup runs
       (validate_and_fix(auto_fix=True)), refreshes the stage-transition cache,
       and writes an audit row. Admin only, CSRF-checked.

Why: db_validator swallows non-fatal errors at startup (prints a warning and
keeps serving), and with OXYPC_AUTO_FIX=0 a missing table is only logged —
so a new module's page 500s while every other page looks healthy.
"""
import os
import time
import traceback

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from auth.dependencies import require_roles, verify_csrf
from models.user import User, UserRole

router = APIRouter(prefix="/admin/system", tags=["system-health"], dependencies=[Depends(verify_csrf)])
admin_only = require_roles(UserRole.admin)

_PROCESS_START = time.time()

# Filled by main.py's startup_event with the outcome of validate_and_fix.
STARTUP_SCHEMA: dict = {"ran": False}


def record_startup(**info) -> None:
    STARTUP_SCHEMA.clear()
    STARTUP_SCHEMA.update({"ran": True, "at": time.strftime("%Y-%m-%d %H:%M:%S"), **info})


async def _missing_enum_values(engine) -> list[str]:
    """Read-only version of db_validator's Phase 0: Python enum values the
    Postgres enum type doesn't have yet (e.g. devicestage.'on_rent')."""
    from database import Base
    import models  # noqa: F401 — register every table on Base.metadata

    wanted: dict[str, set] = {}
    for table in Base.metadata.tables.values():
        for col in table.columns:
            t = col.type
            if hasattr(t, "enums") and getattr(t, "name", None):
                wanted.setdefault(t.name, set()).update(t.enums)
    out = []
    async with engine.connect() as conn:
        for enum_name, values in sorted(wanted.items()):
            rows = await conn.execute(text(
                "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = :n"), {"n": enum_name})
            existing = {r[0] for r in rows}
            if not existing:
                out.append(f"ENUM TYPE MISSING: {enum_name}")
                continue
            out += [f"ENUM VALUE MISSING: {enum_name}.'{v}'" for v in sorted(values - existing)]
    return out


@router.get("/health")
async def system_health(current_user: User = Depends(admin_only)):
    from database import engine
    from db_validator import validate_and_fix

    live: dict = {}
    try:
        summary = await validate_and_fix(engine, auto_fix=False)   # check-only: no DDL
        live = {"remaining": summary.get("remaining", []), "still_failing": summary.get("still_failing", [])}
    except Exception as exc:  # noqa: BLE001 — report, don't 500
        live = {"error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    try:
        live["enum_values_missing"] = await _missing_enum_values(engine)
    except Exception as exc:  # noqa: BLE001
        live["enum_check_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"

    try:
        from templates_config import ASSET_VERSION
    except Exception:  # noqa: BLE001
        ASSET_VERSION = None
    in_sync = not live.get("error") and not live.get("remaining") and not live.get("still_failing") \
        and not live.get("enum_values_missing")
    return JSONResponse({
        "schema_in_sync": in_sync,
        "live_check": live,
        "startup": STARTUP_SCHEMA,
        "auto_fix_env": os.environ.get("OXYPC_AUTO_FIX", "(unset → on)"),
        "asset_version": ASSET_VERSION,
        "process_uptime_seconds": int(time.time() - _PROCESS_START),
    })


@router.post("/schema-fix")
async def system_schema_fix(request: Request, current_user: User = Depends(admin_only)):
    """Run the startup schema auto-fix now (adds missing tables / columns / enum
    values / stage rows — additive only, never drops anything)."""
    from database import engine, AsyncSessionLocal
    from db_validator import validate_and_fix
    from services.audit_engine import audit

    result: dict
    try:
        summary = await validate_and_fix(engine, auto_fix=True)
        result = {"ok": True, "fixed": summary.get("fixed", []), "remaining": summary.get("remaining", [])}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:800]}",
                  "trace": traceback.format_exc()[-1500:]}
    try:
        from services.control_engine import invalidate_transitions_cache
        invalidate_transitions_cache()      # new stage transitions take effect without a restart
    except Exception:  # noqa: BLE001
        pass
    try:
        async with AsyncSessionLocal() as db:
            await audit(db, user=current_user, action="SCHEMA_FIX_RUN", table_name="(schema)",
                        new_value={"ok": result["ok"], "fixed": result.get("fixed", [])[:50],
                                   "error": result.get("error")}, request=request)
            await db.commit()
    except Exception:  # noqa: BLE001 — the fix itself already ran
        pass
    return JSONResponse(result, status_code=200 if result["ok"] else 500)


# ── Device Type backfill (2026-10-22) ────────────────────────────────────────
# Many tags have a blank Device Type but a Sub-Category (Laptop / Desktop /
# TFT). GET previews (read-only); POST copies Sub-Category into Device Type
# where it is blank and logs every changed tag id in the audit row, so the
# change can be reversed.

# Sub-Category spellings that aren't a Device Type value as written.
_SUBCAT_ALIASES = {"tft": "TFT Monitor", "sarvar": "Server", "all in one": "All-in-One"}


def _map_sub_category(value, device_types) -> str | None:
    """Device Type for a Sub-Category: the Device Type master value it matches
    (ignoring case), else a known alias; None if nothing matches (left blank)."""
    v = (value or "").strip()
    if not v:
        return None
    by_lower = {d.lower(): d for d in device_types}
    return by_lower.get(v.lower()) or _SUBCAT_ALIASES.get(v.lower())


async def _device_type_master(db) -> list:
    from sqlalchemy import select
    from models.master import MasterData
    return (await db.execute(
        select(MasterData.value).where(MasterData.category == "device_type", MasterData.is_active.isnot(False))
        .order_by(MasterData.display_order, MasterData.value))).scalars().all()


def _blank_device_type():
    from sqlalchemy import func, or_
    from models.device import Device
    return or_(Device.device_type.is_(None), func.trim(Device.device_type) == "")


@router.get("/device-type-backfill")
async def device_type_backfill_preview(current_user: User = Depends(admin_only)):
    from sqlalchemy import func, select
    from database import AsyncSessionLocal
    from models.device import Device
    from models.master import MasterData
    async with AsyncSessionLocal() as db:
        blanks = (await db.execute(
            select(Device.sub_category, func.count()).where(_blank_device_type())
            .group_by(Device.sub_category).order_by(func.count().desc()))).all()
        in_use = (await db.execute(
            select(Device.device_type, func.count())
            .where(Device.device_type.isnot(None), func.trim(Device.device_type) != "")
            .group_by(Device.device_type).order_by(func.count().desc()))).all()
        masters = {}
        for cat in ("device_type", "sub_category"):
            masters[cat] = (await db.execute(
                select(MasterData.value).where(MasterData.category == cat, MasterData.is_active.isnot(False))
                .order_by(MasterData.display_order, MasterData.value))).scalars().all()
    mapping = [{"sub_category": s, "tags": n, "device_type": _map_sub_category(s, masters["device_type"])}
               for s, n in blanks]
    # The reverse gap: Sub-Category blank while Device Type is set (read-only).
    async with AsyncSessionLocal() as db:
        from sqlalchemy import or_
        rev = (await db.execute(
            select(Device.device_type, func.count())
            .where(or_(Device.sub_category.is_(None), func.trim(Device.sub_category) == ""),
                   Device.device_type.isnot(None), func.trim(Device.device_type) != "")
            .group_by(Device.device_type).order_by(func.count().desc()))).all()
    return JSONResponse({
        "blank_device_type_by_sub_category": mapping,
        "would_fill": sum(m["tags"] for m in mapping if m["device_type"]),
        "blank_sub_category_by_device_type": [{"device_type": d, "tags": n} for d, n in rev],
        "blank_sub_category_total": sum(n for _d, n in rev),
        "left_blank": sum(m["tags"] for m in mapping if not m["device_type"]),
        "device_type_values_in_use": [{"device_type": d, "tags": n} for d, n in in_use],
        "device_type_master_list": masters["device_type"],
        "sub_category_master_list": masters["sub_category"],
    })


@router.post("/device-type-backfill")
async def device_type_backfill_apply(request: Request, current_user: User = Depends(admin_only)):
    """Copy Sub-Category into Device Type for every tag whose Device Type is
    blank and Sub-Category is set. Audited with the changed tag ids."""
    from sqlalchemy import func, select, update
    from database import AsyncSessionLocal
    from models.device import Device
    from services.audit_engine import audit
    from utils.timezone import app_now
    async with AsyncSessionLocal() as db:
        device_types = await _device_type_master(db)
        cond = (_blank_device_type(), Device.sub_category.isnot(None), func.trim(Device.sub_category) != "")
        rows = (await db.execute(select(Device.id, Device.sub_category).where(*cond))).all()
        groups: dict = {}      # target Device Type -> [device ids]
        skipped: dict = {}     # unmatched Sub-Category -> count
        for did, sc in rows:
            target = _map_sub_category(sc, device_types)
            if target:
                groups.setdefault(target, []).append(did)
            else:
                skipped[sc.strip()] = skipped.get(sc.strip(), 0) + 1
        if not groups:
            return JSONResponse({"ok": True, "updated": 0, "skipped": skipped})
        now = app_now()
        for target, ids in groups.items():
            for i in range(0, len(ids), 5000):
                await db.execute(update(Device).where(Device.id.in_(ids[i:i + 5000]), _blank_device_type())
                                 .values(device_type=target, updated_at=now)
                                 .execution_options(synchronize_session=False))
        total = sum(len(v) for v in groups.values())
        await audit(db, user=current_user, action="DEVICE_TYPE_BACKFILL_FROM_SUB_CATEGORY",
                    table_name="devices", record_id=f"{total} tags",
                    old_value={"device_type": None},
                    new_value={"count": total, "by_value": {k: len(v) for k, v in groups.items()},
                               "device_ids": {k: [str(x) for x in v] for k, v in groups.items()},
                               "skipped": skipped},
                    notes="Device Type was blank; set from the tag's Sub-Category. "
                          "Reverse: set device_type = NULL for these ids.",
                    request=request)
        await db.commit()
    return JSONResponse({"ok": True, "updated": total,
                         "by_value": {k: len(v) for k, v in groups.items()}, "skipped": skipped})



# ── RAM / Storage fill (2026-10-22) ──────────────────────────────────────────
# Fills each tag's blank RAM fields (RAM GB, RAM, Total RAM Count/Size) and
# Storage fields (SSD GB, Hard Drive, Total HDD Count/Size) from the others in
# the same set; HDD GB is the second drive, added in, never filled. Rules in
# utils/device_spec.fill_defaults. Blank fields only.
# GET previews (read-only); POST applies and audits the changed tag ids.

_SPEC_COLS = ("ram_gb", "ram_summary", "total_ram_count", "total_ram_size",
              "storage_gb", "hdd_summary", "total_hdd_count", "total_hdd_size")
_SPEC_READ = _SPEC_COLS + ("hdd_capacity_gb",)


async def _spec_changes(db, with_rows=False):
    from sqlalchemy import select
    from types import SimpleNamespace
    from models.device import Device
    from utils.device_spec import fill_defaults
    cols = [Device.id, Device.barcode] + [getattr(Device, c) for c in _SPEC_READ]
    out = []
    for r in (await db.execute(select(*cols))).all():
        ch = fill_defaults(SimpleNamespace(**r._mapping))
        if ch:
            out.append((r.id, ch, r) if with_rows else (r.id, ch))
    return out


@router.get("/spec-fill")
async def spec_fill_preview(current_user: User = Depends(admin_only)):
    """Read-only. Counts per field, plus up to 12 example tags (before -> after),
    picked to cover different kinds of change."""
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        changes = await _spec_changes(db, with_rows=True)
    per_field = {c: sum(1 for _i, ch, _r in changes if c in ch) for c in _SPEC_COLS}
    examples, seen = [], set()
    for _i, ch, r in changes:
        kind = tuple(sorted(ch))
        if kind in seen:
            continue
        seen.add(kind)
        examples.append({"tag": r.barcode,
                         "before": {c: getattr(r, c) for c in _SPEC_READ if getattr(r, c) not in (None, "")},
                         "fills": ch})
        if len(examples) >= 12:
            break
    return JSONResponse({"tags_to_change": len(changes), "per_field": per_field,
                         "examples": examples})


def _back_to_page(request_form, msg):
    """POSTs from the Data Fixes page (hidden back=1) return there with a
    message instead of raw JSON."""
    from fastapi.responses import RedirectResponse
    from urllib.parse import quote
    if (request_form.get("back") or "") == "1":
        return RedirectResponse(f"/admin/system/data-fixes?done={quote(msg)}", status_code=303)
    return None


@router.post("/spec-fill")
async def spec_fill_apply(request: Request, current_user: User = Depends(admin_only)):
    from collections import defaultdict
    from sqlalchemy import update
    from database import AsyncSessionLocal
    from models.device import Device
    from services.audit_engine import audit
    async with AsyncSessionLocal() as db:
        changes = await _spec_changes(db)
        groups = defaultdict(list)          # same set of fields -> one statement
        for did, ch in changes:
            groups[tuple(sorted(ch))].append((did, ch))
        from sqlalchemy import bindparam
        conn = await db.connection()
        for keys, items in groups.items():
            stmt = (update(Device.__table__).where(Device.__table__.c.id == bindparam("b_id"))
                    .values({k: bindparam("b_" + k) for k in keys}))
            params = [{"b_id": did, **{"b_" + k: ch[k] for k in keys}} for did, ch in items]
            for i in range(0, len(params), 2000):
                await conn.execute(stmt, params[i:i + 2000])   # executemany
        per_field = {c: [str(d) for d, ch in changes if c in ch] for c in _SPEC_COLS}
        await audit(db, user=current_user, action="DEVICE_SPEC_FILL", table_name="devices",
                    record_id=f"{len(changes)} tags",
                    new_value={"count": len(changes), "device_ids_by_field": per_field},
                    notes="Blank RAM/Storage fields filled from the others in each set. "
                          "Reverse: set each listed field back to NULL for its ids.",
                    request=request)
        await db.commit()
    back = _back_to_page(await request.form(), f"RAM / Storage fill done: {len(changes):,} tags updated.")
    if back:
        return back
    return JSONResponse({"ok": True, "updated": len(changes),
                         "per_field": {c: len(v) for c, v in per_field.items()}})


# ── RAM / Storage size spacing (2026-10-27) ──────────────────────────────────
# Text sizes written without a space ("16GB", "256GB SSD", "1tb") become
# "16 GB", "256 GB SSD", "1 TB" — the form the Basic Specs edit modal saves.
# Only plain values are touched: packed multi-stick / multi-drive summaries
# ("16GB_DDR4_2300MHz_Samsung, 8GB_...") are left as they are, same rule as
# the modal. The numeric RAM GB / SSD GB columns are not involved.
_SPACE_FIELDS = ("total_ram_size", "ram_summary", "total_hdd_size", "hdd_summary")


def _space_size_text(v):
    import re
    if not v or "_" in v or "," in v:
        return v
    return re.sub(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(GB|TB)\b",
                  lambda m: f"{m.group(1)} {m.group(2).upper()}", v, flags=re.IGNORECASE)


async def _space_changes(db):
    """[(device_id, barcode, {field: (old, new)})] for tags with a size text
    that needs the space."""
    from sqlalchemy import or_, select
    from models.device import Device
    cols = [getattr(Device, f) for f in _SPACE_FIELDS]
    rows = (await db.execute(
        select(Device.id, Device.barcode, *cols)
        .where(or_(*[c.op("~*")(r"[0-9](gb|tb)") for c in cols]))
    )).all()
    out = []
    for r in rows:
        ch = {}
        for f in _SPACE_FIELDS:
            old = getattr(r, f)
            new = _space_size_text(old)
            if new != old:
                ch[f] = (old, new)
        if ch:
            out.append((r.id, r.barcode, ch))
    return out


@router.post("/size-space-fix")
async def size_space_fix_apply(request: Request, current_user: User = Depends(admin_only)):
    from collections import defaultdict
    from sqlalchemy import bindparam, update
    from database import AsyncSessionLocal
    from models.device import Device
    from services.audit_engine import audit
    async with AsyncSessionLocal() as db:
        changes = await _space_changes(db)
        groups = defaultdict(list)          # same set of fields -> one statement
        for did, _tag, ch in changes:
            groups[tuple(sorted(ch))].append((did, ch))
        conn = await db.connection()
        for keys, items in groups.items():
            stmt = (update(Device.__table__).where(Device.__table__.c.id == bindparam("b_id"))
                    .values({k: bindparam("b_" + k) for k in keys}))
            params = [{"b_id": did, **{"b_" + k: ch[k][1] for k in keys}} for did, ch in items]
            for i in range(0, len(params), 2000):
                await conn.execute(stmt, params[i:i + 2000])   # executemany
        per_field = {f: [str(d) for d, _t, ch in changes if f in ch] for f in _SPACE_FIELDS}
        await audit(db, user=current_user, action="DEVICE_SIZE_SPACE_FIX", table_name="devices",
                    record_id=f"{len(changes)} tags",
                    new_value={"count": len(changes), "device_ids_by_field": per_field},
                    notes='RAM / Storage text sizes given a space: "16GB" -> "16 GB", "1tb" -> "1 TB". '
                          "Reverse: remove the space between the number and GB/TB for the listed ids.",
                    request=request)
        await db.commit()
    back = _back_to_page(await request.form(), f"RAM / Storage spacing done: {len(changes):,} tags updated.")
    if back:
        return back
    return JSONResponse({"ok": True, "updated": len(changes),
                         "per_field": {f: len(v) for f, v in per_field.items()}})


# ── RAM / Storage swapped or "0" (2026-10-27) ────────────────────────────────
# Rules in utils/device_spec.spec_repair: "0" sizes cleared, RAM and Storage
# swapped back (RAM over 128 GB with a 4–64 GB drive), a drive size sitting in
# RAM moved to Storage, then blank fields filled from the rest of each set.
# The page previews; POST applies and audits every tag's before / after.
_REPAIR_COLS = ("ram_gb", "ram_summary", "total_ram_count", "total_ram_size",
                "storage_gb", "hdd_summary", "total_hdd_count", "total_hdd_size")
_REPAIR_KIND_LABELS = {
    "swapped": "RAM and Storage swapped back",
    "moved": "Drive size sitting in RAM moved to Storage",
    "summary": "Drive size in RAM Summary moved to Storage",
    "zero": '"0" size cleared',
}


async def _repair_changes(db):
    """[(device_id, barcode, kinds, {field: (old, new)})]."""
    from sqlalchemy import select
    from models.device import Device
    from utils.device_spec import spec_repair
    cols = [getattr(Device, c) for c in _REPAIR_COLS + ("hdd_capacity_gb",)]
    out = []
    for r in (await db.execute(select(Device.id, Device.barcode, *cols))).all():
        kinds, ch = spec_repair(r)
        if ch:
            out.append((r.id, r.barcode, kinds, {f: (getattr(r, f), v) for f, v in ch.items()}))
    return out


@router.post("/spec-repair")
async def spec_repair_apply(request: Request, current_user: User = Depends(admin_only)):
    from collections import defaultdict
    from sqlalchemy import bindparam, update
    from database import AsyncSessionLocal
    from models.device import Device
    from services.audit_engine import audit
    async with AsyncSessionLocal() as db:
        changes = await _repair_changes(db)
        groups = defaultdict(list)          # same set of fields -> one statement
        for did, _tag, _k, ch in changes:
            groups[tuple(sorted(ch))].append((did, ch))
        conn = await db.connection()
        for keys, items in groups.items():
            stmt = (update(Device.__table__).where(Device.__table__.c.id == bindparam("b_id"))
                    .values({k: bindparam("b_" + k) for k in keys}))
            params = [{"b_id": did, **{"b_" + k: ch[k][1] for k in keys}} for did, ch in items]
            for i in range(0, len(params), 2000):
                await conn.execute(stmt, params[i:i + 2000])   # executemany
        by_kind = {k: sum(1 for c in changes if k in c[2]) for k in _REPAIR_KIND_LABELS}
        await audit(db, user=current_user, action="DEVICE_SPEC_REPAIR", table_name="devices",
                    record_id=f"{len(changes)} tags",
                    old_value={str(did): {f: v[0] for f, v in ch.items()} for did, _t, _k, ch in changes},
                    new_value={"count": len(changes), "by_kind": by_kind,
                               "after": {str(did): {f: v[1] for f, v in ch.items()} for did, _t, _k, ch in changes}},
                    notes="RAM / Storage put right: swapped back, drive size moved out of RAM, "
                          '"0" cleared, blanks filled. Reverse: set each listed field back to its old_value.',
                    request=request)
        await db.commit()
    back = _back_to_page(await request.form(), f"RAM / Storage repair done: {len(changes):,} tags updated.")
    if back:
        return back
    return JSONResponse({"ok": True, "updated": len(changes), "by_kind": by_kind})


# ── Scrapped tags whose L1/L2 WorkID is still open (2026-10-27) ──────────────
# "Back to Inventory" (routers/repair.py back_to_production) sent a scrapped
# tag to Tags Scrapped without closing its L1/L2 WorkID, so it stayed open
# and kept counting as Assigned on WorkID Status. Now it closes there; this
# closes the ones left open before, stamped at that "Back to Inventory"
# movement's time — so WorkID Status counts them under Scrap.
async def _scrap_l12_open(db):
    """[(work_order_id, work_id, barcode, assigned_name, moved_at, note)]."""
    from sqlalchemy import and_, or_, not_, select, func
    from models.device import Device, StageMovement
    from models.work_order import WorkOrder
    from services.work_order_close import wo_open
    first_bti = (select(func.min(StageMovement.moved_at))
                 .where(StageMovement.device_id == WorkOrder.device_id,
                        StageMovement.notes.like("Back to Inventory \u2014 %"),
                        StageMovement.moved_at >= func.coalesce(WorkOrder.assigned_at, WorkOrder.created_at))
                 .correlate(WorkOrder).scalar_subquery())
    note = (select(StageMovement.notes)
            .where(StageMovement.device_id == WorkOrder.device_id, StageMovement.moved_at == first_bti,
                   StageMovement.notes.like("Back to Inventory \u2014 %"))
            .limit(1).correlate(WorkOrder).scalar_subquery())
    q = (select(WorkOrder.id, WorkOrder.work_id, Device.barcode, WorkOrder.assigned_name,
                first_bti.label("at"), note.label("note"))
         .join(Device, Device.id == WorkOrder.device_id)
         .where(or_(WorkOrder.work_id.like("L1L2-%"), WorkOrder.stage.in_(["l1", "l2"])),
                not_(WorkOrder.work_id.like("L3L4-%")), not_(WorkOrder.work_id.like("STRS-%")),
                wo_open(), first_bti.isnot(None))
         .order_by(first_bti.desc()))
    return (await db.execute(q)).all()


@router.post("/scrap-l12-close")
async def scrap_l12_close_apply(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import bindparam, update
    from database import AsyncSessionLocal
    from models.work_order import WorkOrder
    from services.audit_engine import audit
    async with AsyncSessionLocal() as db:
        rows = await _scrap_l12_open(db)
        if rows:
            conn = await db.connection()
            stmt = (update(WorkOrder.__table__).where(WorkOrder.__table__.c.id == bindparam("b_id"))
                    .values(status="completed", completed_at=bindparam("b_at")))
            params = [{"b_id": r.id, "b_at": r.at} for r in rows]
            for i in range(0, len(params), 2000):
                await conn.execute(stmt, params[i:i + 2000])
        await audit(db, user=current_user, action="WORKID_SCRAP_L12_CLOSE", table_name="work_orders",
                    record_id=f"{len(rows)} WorkIDs",
                    new_value={"count": len(rows),
                               "work_orders": {str(r.id): {"work_id": r.work_id, "tag": r.barcode,
                                                           "completed_at": r.at.isoformat() if r.at else None}
                                               for r in rows}},
                    notes="Open L1/L2 WorkIDs of tags already sent Back to Inventory (scrap) closed at "
                          "that movement's time. Reverse: set status back to 'pending' and completed_at "
                          "NULL for the listed ids.",
                    request=request)
        await db.commit()
    back = _back_to_page(await request.form(), f"Scrap WorkIDs closed: {len(rows):,}.")
    if back:
        return back
    return JSONResponse({"ok": True, "closed": len(rows)})


# ── WorkIDs still open on tags already at Ready to Sale / Sold (2026-10-28) ──
# Tags that reached Ready to Sale (and some later sold) with a WorkID never
# closed — mostly old L1/L2 WorkIDs from before one-WorkID-per-visit. They
# showed as "ongoing" on WorkID Status. Each closes as completed at the time
# the tag first reached Ready to Sale after the WorkID started (now, if no
# such move is recorded).
async def _rts_open(db):
    """[(id, work_id, barcode, assigned_name, stage, start, at, current_stage)]."""
    from sqlalchemy import func, select
    from models.device import Device, DeviceStage, StageMovement
    from models.work_order import WorkOrder
    from services.work_order_close import wo_open
    start = func.coalesce(WorkOrder.assigned_at, WorkOrder.created_at)
    reached = (select(func.min(StageMovement.moved_at))
               .where(StageMovement.device_id == WorkOrder.device_id,
                      StageMovement.to_stage == DeviceStage.ready_to_sale,
                      StageMovement.moved_at >= start)
               .correlate(WorkOrder).scalar_subquery())
    q = (select(WorkOrder.id, WorkOrder.work_id, Device.barcode, WorkOrder.assigned_name,
                WorkOrder.stage, start.label("start"), reached.label("at"), Device.current_stage)
         .join(Device, Device.id == WorkOrder.device_id)
         .where(wo_open(), Device.current_stage.in_([DeviceStage.ready_to_sale, DeviceStage.sold]),
                Device.is_trashed.isnot(True),
                # "asgn" = Inventory Request "Assign To Employee" bookkeeping —
                # live work on Ready to Sale tags, never closed here (2026-10-28).
                # "psiqc" = Pre-Sale IQC (2026-10-29): work done ON Ready for
                # Sale tags, open on purpose — not left over either.
                WorkOrder.stage.notin_(["asgn", "psiqc"]))
         .order_by(start.desc()))
    return (await db.execute(q)).all()       # at None = no Ready to Sale move recorded -> closed now


@router.post("/rts-wo-close")
async def rts_wo_close_apply(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import bindparam, update
    from database import AsyncSessionLocal
    from models.work_order import WorkOrder
    from services.audit_engine import audit
    from utils.timezone import app_now
    async with AsyncSessionLocal() as db:
        rows = await _rts_open(db)
        now = app_now()
        if rows:
            conn = await db.connection()
            stmt = (update(WorkOrder.__table__).where(WorkOrder.__table__.c.id == bindparam("b_id"))
                    .values(status="completed", completed_at=bindparam("b_at")))
            params = [{"b_id": r.id, "b_at": r.at or now} for r in rows]
            for i in range(0, len(params), 2000):
                await conn.execute(stmt, params[i:i + 2000])
        await audit(db, user=current_user, action="WORKID_RTS_CLOSE", table_name="work_orders",
                    record_id=f"{len(rows)} WorkIDs",
                    new_value={"count": len(rows),
                               "work_orders": {str(r.id): {"work_id": r.work_id, "tag": r.barcode,
                                                           "completed_at": (r.at or now).isoformat()}
                                               for r in rows}},
                    notes="WorkIDs still open on tags already at Ready to Sale / Sold closed at the time the "
                          "tag reached Ready to Sale. Reverse: set status back to 'pending' and completed_at "
                          "NULL for the listed ids.",
                    request=request)
        await db.commit()
    back = _back_to_page(await request.form(), f"Ready to Sale WorkIDs closed: {len(rows):,}.")
    if back:
        return back
    return JSONResponse({"ok": True, "closed": len(rows)})


# ── Undo for the above on "asgn" WorkIDs (2026-10-28) ────────────────────────
# The first version of the card also closed Inventory Request "Assign To
# Employee" WorkIDs (stage "asgn") — live assignments of Ready to Sale tags.
# This reopens exactly those: listed in a WORKID_RTS_CLOSE audit, stage
# "asgn", still completed at the very time that fix stamped. They were all
# "pending" before (transfers.py creates them pending), so that is restored.
async def _rts_asgn_closed(db):
    """[(id, work_id, barcode, assigned_name, completed_at)]."""
    import json
    import uuid as _uuid
    from sqlalchemy import select
    from models.device import Device
    from models.engines import AuditLog
    from models.work_order import WorkOrder
    stamped = {}
    for (nv,) in (await db.execute(
            select(AuditLog.new_value).where(AuditLog.action == "WORKID_RTS_CLOSE"))).all():
        try:
            data = json.loads(nv or "{}")
        except ValueError:
            continue
        for wid_id, info in (data.get("work_orders") or {}).items():
            stamped[wid_id] = (info or {}).get("completed_at")
    if not stamped:
        return []
    ids = []
    for k in stamped:
        try:
            ids.append(_uuid.UUID(k))
        except ValueError:
            pass
    out = []
    for i in range(0, len(ids), 5000):
        rows = (await db.execute(
            select(WorkOrder.id, WorkOrder.work_id, Device.barcode, WorkOrder.assigned_name,
                   WorkOrder.completed_at)
            .join(Device, Device.id == WorkOrder.device_id)
            .where(WorkOrder.id.in_(ids[i:i + 5000]), WorkOrder.stage == "asgn",
                   WorkOrder.status == "completed")
        )).all()
        for r in rows:
            at = stamped.get(str(r.id))
            if at and r.completed_at and r.completed_at.isoformat() == at:
                out.append(r)
    return sorted(out, key=lambda r: r.work_id or "")


@router.post("/rts-asgn-reopen")
async def rts_asgn_reopen_apply(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import update
    from database import AsyncSessionLocal
    from models.work_order import WorkOrder
    from services.audit_engine import audit
    async with AsyncSessionLocal() as db:
        rows = await _rts_asgn_closed(db)
        ids = [r.id for r in rows]
        for i in range(0, len(ids), 5000):
            await db.execute(update(WorkOrder).where(WorkOrder.id.in_(ids[i:i + 5000]))
                             .values(status="pending", completed_at=None))
        await audit(db, user=current_user, action="WORKID_RTS_ASGN_REOPEN", table_name="work_orders",
                    record_id=f"{len(rows)} WorkIDs",
                    new_value={"count": len(rows),
                               "work_orders": {str(r.id): {"work_id": r.work_id, "tag": r.barcode,
                                                           "was_completed_at": r.completed_at.isoformat()}
                                               for r in rows}},
                    notes="Inventory Request assignment (asgn) WorkIDs wrongly closed by the Ready to Sale "
                          "WorkID fix reopened as pending.",
                    request=request)
        await db.commit()
    back = _back_to_page(await request.form(), f"Assignment WorkIDs reopened: {len(rows):,}.")
    if back:
        return back
    return JSONResponse({"ok": True, "reopened": len(rows)})


# ── A size typed into Storage Type (2026-10-29) ──────────────────────────────
# Some uploads put the drive size ("256 GB", "512 GB", "0") into Storage Type,
# which should only hold SSD / HDD / NVMe. Summaries showed it twice ("256 GB
# 256 GB"). Rules: "0" -> cleared. A size and the tag has no other storage ->
# the size moves into the storage fields (GB, size text, summary; count only
# if blank), Storage Type cleared. Same size already in the storage fields ->
# Storage Type cleared. A DIFFERENT size already there -> left alone, listed.
# No GB / TB correction (Pankaj, 2026-10-29).
_STYPE_COLS = ("storage_type", "storage_gb", "total_hdd_size", "hdd_summary", "total_hdd_count")


async def _stype_changes(db):
    """(changes, conflicts): changes = [(id, barcode, {field: (old, new)})],
    conflicts = [(barcode, storage_gb, total_hdd_size, storage_type)]."""
    from sqlalchemy import select
    from models.device import Device
    from utils.device_spec import _is_zero, size_to_gb, upload_spec
    changes, conflicts = [], []
    rows = (await db.execute(
        select(Device.id, Device.barcode, *[getattr(Device, c) for c in _STYPE_COLS])
        .where(Device.storage_type.op("~")("[0-9]"))
    )).all()
    for r in rows:
        st = (r.storage_type or "").strip()
        ch = {"storage_type": (r.storage_type, None)}
        if not _is_zero(st):
            have = 0
            for v in (r.total_hdd_size, r.storage_gb, r.hdd_summary):
                if v not in (None, "") and not _is_zero(v):
                    have = size_to_gb(str(v)) if not isinstance(v, int) else v
                    if have:
                        break
            want = size_to_gb(st)
            if have and want and have != want:
                conflicts.append((r.barcode, r.storage_gb, r.total_hdd_size, r.storage_type))
                continue
            if not have:
                for f, v in upload_spec(None, st).items():
                    old = getattr(r, f)
                    if f == "total_hdd_count" and (old or "").strip() and not _is_zero(old):
                        continue
                    if old != v:
                        ch[f] = (old, v)
        changes.append((r.id, r.barcode, ch))
    return changes, conflicts


# ── Device Type: the 12 categories (2026-10-30, Pankaj) ──────────────────────
# Laptop, Desktop, TFT Monitor, Tablet, Mini PC, Server, Router, Printer,
# Switch, Projector, All-in-One, Mobile. Tags: other spellings / case of these
# become the exact name; Tiny and Thinclient become Mini PC; Mobile Phone
# becomes Mobile. Master Data "Device Form Factors": Projector added (and any
# missing one of the 12), Workstation switched off, the 12 switched on.
from utils.device_types import DEVICE_TYPES_12, canonical_device_type
_DT_OFF = ("Workstation",)


def _dt12_target(value):
    """New Device Type for a tag value, or None when it is already exact /
    not one of the 12."""
    target = canonical_device_type(value)
    return target if target and value != target else None


async def _dt12_changes(db):
    """([(old value, new value, tag count)], master-list changes) — preview."""
    from sqlalchemy import func, select
    from models.device import Device
    from models.master import MasterData
    rows = (await db.execute(select(Device.device_type, func.count()).where(Device.device_type.isnot(None))
                             .group_by(Device.device_type))).all()
    tags = sorted(((old, _dt12_target(old), n) for old, n in rows if _dt12_target(old)), key=lambda x: -x[2])
    master = {m.value: m for m in (await db.execute(
        select(MasterData).where(MasterData.category == "device_type"))).scalars().all()}
    low = {k.lower(): k for k in master}
    add = [n for n in DEVICE_TYPES_12 if n.lower() not in low]
    turn_on = [low[n.lower()] for n in DEVICE_TYPES_12 if n.lower() in low and master[low[n.lower()]].is_active is False]
    turn_off = [k for k in master if k in _DT_OFF and master[k].is_active is not False]
    return tags, {"add": add, "turn_on": turn_on, "turn_off": turn_off}


@router.post("/device-type-12-fix")
async def device_type_12_fix(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import select, update
    from database import AsyncSessionLocal
    from models.device import Device
    from models.master import MasterData
    from services.audit_engine import audit
    from utils.master_data import refresh_master_cache
    from utils.timezone import app_now
    async with AsyncSessionLocal() as db:
        tags, master = await _dt12_changes(db)
        now, ids_by = app_now(), {}
        for old, new, _n in tags:
            ids = (await db.execute(select(Device.id).where(Device.device_type == old))).scalars().all()
            ids_by[f"{old} -> {new}"] = [str(i) for i in ids]
            await db.execute(update(Device).where(Device.device_type == old)
                             .values(device_type=new, updated_at=now).execution_options(synchronize_session=False))
        order = max([m.display_order or 0 for m in (await db.execute(
            select(MasterData).where(MasterData.category == "device_type"))).scalars().all()] or [0])
        for name in master["add"]:
            order += 1
            db.add(MasterData(category="device_type", value=name, display_order=order, is_active=True))
        if master["turn_on"]:
            await db.execute(update(MasterData).where(MasterData.category == "device_type",
                                                      MasterData.value.in_(master["turn_on"])).values(is_active=True))
        if master["turn_off"]:
            await db.execute(update(MasterData).where(MasterData.category == "device_type",
                                                      MasterData.value.in_(master["turn_off"])).values(is_active=False))
        total = sum(n for _o, _n2, n in tags)
        await audit(db, user=current_user, action="DEVICE_TYPE_12_FIX", table_name="devices",
                    record_id=f"{total} tags",
                    new_value={"count": total, "device_ids": ids_by, "master": master},
                    notes="Device Type set to the 12 categories (spelling / Tiny / Thinclient / Mobile Phone). "
                          "Reverse: put each group's old value back for the listed ids.",
                    request=request)
        await db.commit()
        await refresh_master_cache(db)
    msg = (f"Device Type tidied on {total:,} tags"
           + (f"; added {', '.join(master['add'])}" if master["add"] else "")
           + (f"; switched off {', '.join(master['turn_off'])}" if master["turn_off"] else "") + ".")
    back = _back_to_page(await request.form(), msg)
    return back or JSONResponse({"ok": True, "updated": total, "master": master})


@router.post("/storage-type-fix")
async def storage_type_fix_apply(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import update
    from database import AsyncSessionLocal
    from models.device import Device
    from services.audit_engine import audit
    async with AsyncSessionLocal() as db:
        changes, conflicts = await _stype_changes(db)
        for did, _tag, ch in changes:
            await db.execute(update(Device).where(Device.id == did).values({f: v[1] for f, v in ch.items()}))
        await audit(db, user=current_user, action="DEVICE_STORAGE_TYPE_FIX", table_name="devices",
                    record_id=f"{len(changes)} tags",
                    new_value={"count": len(changes),
                               "before_after": {str(d): {f: [str(v[0]) if v[0] is not None else None,
                                                             str(v[1]) if v[1] is not None else None]
                                                         for f, v in ch.items()} for d, _t, ch in changes},
                               "left_alone_different_size": [c[0] for c in conflicts]},
                    notes="Sizes typed into Storage Type moved to the storage fields; Storage Type cleared. "
                          "Reverse: put each field's 'before' value back for the listed ids.",
                    request=request)
        await db.commit()
    back = _back_to_page(await request.form(),
                         f"Storage Type fixed on {len(changes):,} tags"
                         + (f"; {len(conflicts)} with a different size left alone." if conflicts else "."))
    if back:
        return back
    return JSONResponse({"ok": True, "updated": len(changes), "left_alone": len(conflicts)})


# ── Error log viewer (2026-10-28) ─────────────────────────────────────────────
# Production has no terminal access from here; unhandled 500s are written to
# errors.log next to main.py (main.py generic_exception_handler /
# _log_unhandled). Admin-only, read-only: the newest entries first.
@router.get("/error-log", response_class=HTMLResponse)
async def error_log_page(request: Request, kb: int = 256, q: str = "",
                         current_user: User = Depends(admin_only)):
    import os
    from html import escape
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "errors.log")
    kb = max(16, min(kb, 4096))
    text, size = "", 0
    if os.path.exists(path):
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - kb * 1024))
            text = f.read().decode("utf-8", "replace")
    # one entry per logged line that starts with a timestamp
    import re
    parts = re.split(r"(?m)^(?=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[)", text)
    entries = [p.strip() for p in parts if p.strip()]
    if q:
        entries = [e for e in entries if q.lower() in e.lower()]
    entries = entries[::-1][:200]
    body = "".join(f'<pre class="border rounded bg-light p-2 small mb-3" style="white-space:pre-wrap">{escape(e)}</pre>'
                   for e in entries) or '<div class="text-muted">No errors logged.</div>'
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Error Log — OxyPC</title>
<link rel="stylesheet" href="/static/vendor/bootstrap/css/bootstrap.min.css"></head>
<body class="p-3"><h5>Error Log <small class="text-muted">errors.log, {size/1024:,.0f} KB — newest first, last {kb} KB</small></h5>
<form class="d-flex gap-2 mb-3" method="get"><input class="form-control form-control-sm" style="max-width:320px" name="q" value="{escape(q)}" placeholder="Filter, e.g. credit-note">
<input type="hidden" name="kb" value="{kb}"><button class="btn btn-sm btn-primary">Filter</button>
<a class="btn btn-sm btn-outline-secondary" href="?kb={min(kb * 4, 4096)}">Load more</a></form>{body}</body></html>"""
    return HTMLResponse(html)


# ── Stress WorkIDs left open after the tag left Stress (2026-10-27) ──────────
# Submitting the Stress result added a separate completed "strs" WorkID and
# never closed the tag's Stress WorkID (STRS- / bucket / Change Engineer), so
# it stayed open forever. Now the result closes it (services/work_order_close
# complete_open_stress_work_order); this closes the old ones, at the moment
# the tag left Stress (its first Asset History move out of Stress Test after
# the WorkID started). Where a "strs" result WorkID was recorded at that same
# moment, the Stress step is already counted there, so the old one is closed
# as "reassigned" (hidden by default, not counted as Completed) instead of
# "completed" — no Stress visit is counted twice.
_STRESS_DUP_WINDOW_SECONDS = 120


async def _stress_open_left(db):
    """[(wo_id, work_id, barcode, assigned_name, left_at, status_to_set)]."""
    from sqlalchemy import select, func
    from models.device import Device, DeviceStage, StageMovement
    from models.work_order import WorkOrder
    from services.work_order_close import wo_open, stress_step_clause
    left_at = (select(func.min(StageMovement.moved_at))
               .where(StageMovement.device_id == WorkOrder.device_id,
                      StageMovement.from_stage == DeviceStage.qc_check,
                      StageMovement.moved_at >= func.coalesce(WorkOrder.assigned_at, WorkOrder.created_at))
               .correlate(WorkOrder).scalar_subquery())
    rows = (await db.execute(
        select(WorkOrder.id, WorkOrder.work_id, Device.barcode, WorkOrder.assigned_name,
               WorkOrder.device_id, left_at.label("at"))
        .join(Device, Device.id == WorkOrder.device_id)
        .where(wo_open(), WorkOrder.stage != "asgn", stress_step_clause(), left_at.isnot(None))
        .order_by(left_at.desc())
    )).all()
    if not rows:
        return []
    dev_ids = list({r.device_id for r in rows})
    strs = {}
    for i in range(0, len(dev_ids), 20000):
        for did, done in (await db.execute(
            select(WorkOrder.device_id, WorkOrder.completed_at)
            .where(WorkOrder.device_id.in_(dev_ids[i:i + 20000]), WorkOrder.stage == "strs",
                   WorkOrder.completed_at.isnot(None))
        )).all():
            strs.setdefault(did, []).append(done)
    out = []
    for r in rows:
        dup = any(abs((d - r.at).total_seconds()) <= _STRESS_DUP_WINDOW_SECONDS for d in strs.get(r.device_id, ()))
        out.append((r.id, r.work_id, r.barcode, r.assigned_name, r.at, "reassigned" if dup else "completed"))
    return out


@router.post("/stress-wo-close")
async def stress_wo_close_apply(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import bindparam, update
    from database import AsyncSessionLocal
    from models.work_order import WorkOrder
    from services.audit_engine import audit
    async with AsyncSessionLocal() as db:
        rows = await _stress_open_left(db)
        if rows:
            conn = await db.connection()
            stmt = (update(WorkOrder.__table__).where(WorkOrder.__table__.c.id == bindparam("b_id"))
                    .values(status=bindparam("b_st"), completed_at=bindparam("b_at")))
            params = [{"b_id": r[0], "b_st": r[5], "b_at": r[4]} for r in rows]
            for i in range(0, len(params), 2000):
                await conn.execute(stmt, params[i:i + 2000])
        await audit(db, user=current_user, action="WORKID_STRESS_CLOSE", table_name="work_orders",
                    record_id=f"{len(rows)} WorkIDs",
                    new_value={"count": len(rows),
                               "completed": sum(1 for r in rows if r[5] == "completed"),
                               "reassigned": sum(1 for r in rows if r[5] == "reassigned"),
                               "work_orders": {str(r[0]): {"work_id": r[1], "tag": r[2], "status": r[5],
                                                           "completed_at": r[4].isoformat()}
                                               for r in rows}},
                    notes="Open Stress WorkIDs of tags that had already left Stress Test closed at "
                          "that move's time. Reverse: set status back to 'pending' and completed_at "
                          "NULL for the listed ids.",
                    request=request)
        await db.commit()
    back = _back_to_page(await request.form(), f"Stress WorkIDs closed: {len(rows):,}.")
    if back:
        return back
    return JSONResponse({"ok": True, "closed": len(rows)})


# ── Cosmetic WorkIDs left open by bulk Assign (2026-10-27) ───────────────────
# Cosmetic bulk Assign (routers/cosmetic.py bulk_assign) used to leave the
# tag's WorkID for its stage open: moving a batch from Cosmetic Received to
# Cleaning left the Cosmetic Received WorkID open forever, and reassigning
# at Cleaning..Cosmetic Completed left the previous person's WorkID open next
# to the new one. bulk Assign now closes them; this closes the old ones:
#   - a newer WorkID for the same tag and stage exists -> "reassigned" at the
#     moment the newer one was created (taken off that person);
#   - otherwise, the tag has since left that stage -> "completed" at its
#     first Asset History move out of the stage after the WorkID started.
# A WorkID for the stage the tag is on now, with no newer one, stays open.
async def _cosmetic_open_left(db):
    """[(wo_id, work_id, barcode, assigned_name, stage_label, at, status_to_set)]."""
    from sqlalchemy import select, func
    from sqlalchemy.orm import aliased
    from models.device import Device, StageMovement, STAGE_LABELS
    from models.work_order import WorkOrder
    from routers.cosmetic import MOVE_STAGE_CODE
    from services.work_order_close import wo_open
    from models.device import DeviceStage
    code_stage = {c: st for st, c in MOVE_STAGE_CODE.items() if st != DeviceStage.final_qc}
    out = []
    newer = aliased(WorkOrder)
    start = func.coalesce(WorkOrder.assigned_at, WorkOrder.created_at)
    for code, stage in code_stage.items():
        next_at = (select(func.min(newer.created_at))
                   .where(newer.device_id == WorkOrder.device_id, newer.stage == code,
                          newer.id != WorkOrder.id, newer.created_at > WorkOrder.created_at)
                   .correlate(WorkOrder).scalar_subquery())
        left_at = (select(func.min(StageMovement.moved_at))
                   .where(StageMovement.device_id == WorkOrder.device_id,
                          StageMovement.from_stage == stage, StageMovement.moved_at >= start)
                   .correlate(WorkOrder).scalar_subquery())
        rows = (await db.execute(
            select(WorkOrder.id, WorkOrder.work_id, Device.barcode, WorkOrder.assigned_name,
                   next_at.label("nx"), left_at.label("lf"))
            .join(Device, Device.id == WorkOrder.device_id)
            .where(WorkOrder.stage == code, wo_open())
        )).all()
        for r in rows:
            # Whichever happened first: handed to someone else, or left the stage.
            if r.nx is not None and (r.lf is None or r.nx <= r.lf):
                out.append((r.id, r.work_id, r.barcode, r.assigned_name, STAGE_LABELS.get(stage, code),
                            r.nx, "reassigned"))
            elif r.lf is not None:
                out.append((r.id, r.work_id, r.barcode, r.assigned_name, STAGE_LABELS.get(stage, code),
                            r.lf, "completed"))
    out.sort(key=lambda r: r[5], reverse=True)
    return out


@router.post("/cosmetic-wo-close")
async def cosmetic_wo_close_apply(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import bindparam, update
    from database import AsyncSessionLocal
    from models.work_order import WorkOrder
    from services.audit_engine import audit
    async with AsyncSessionLocal() as db:
        rows = await _cosmetic_open_left(db)
        if rows:
            conn = await db.connection()
            stmt = (update(WorkOrder.__table__).where(WorkOrder.__table__.c.id == bindparam("b_id"))
                    .values(status=bindparam("b_st"), completed_at=bindparam("b_at")))
            params = [{"b_id": r[0], "b_st": r[6], "b_at": r[5]} for r in rows]
            for i in range(0, len(params), 2000):
                await conn.execute(stmt, params[i:i + 2000])
        await audit(db, user=current_user, action="WORKID_COSMETIC_CLOSE", table_name="work_orders",
                    record_id=f"{len(rows)} WorkIDs",
                    new_value={"count": len(rows),
                               "completed": sum(1 for r in rows if r[6] == "completed"),
                               "reassigned": sum(1 for r in rows if r[6] == "reassigned"),
                               "work_orders": {str(r[0]): {"work_id": r[1], "tag": r[2], "stage": r[4],
                                                           "status": r[6], "completed_at": r[5].isoformat()}
                                               for r in rows}},
                    notes="Cosmetic WorkIDs left open by bulk Assign closed (reassigned when a newer "
                          "WorkID took over, else completed when the tag left the stage). Reverse: set "
                          "status back to 'pending' and completed_at NULL for the listed ids.",
                    request=request)
        await db.commit()
    back = _back_to_page(await request.form(), f"Cosmetic WorkIDs closed: {len(rows):,}.")
    if back:
        return back
    return JSONResponse({"ok": True, "closed": len(rows)})


# ── Sub-Category fill from Device Type (2026-10-22) ──────────────────────────
# The reverse of the Device Type backfill: Sub-Category blank, Device Type set.
# Uses the same pairing as upload (utils/device_category): the Sub-Category
# master value it matches, else a known spelling, else Device Type as typed.

async def _subcat_changes(db):
    from sqlalchemy import func, or_, select
    from models.device import Device
    from models.master import MasterData
    from utils.device_category import pair_type_and_category
    sc_master = (await db.execute(
        select(MasterData.value).where(MasterData.category == "sub_category", MasterData.is_active.isnot(False))
    )).scalars().all()
    rows = (await db.execute(
        select(Device.id, Device.barcode, Device.device_type)
        .where(or_(Device.sub_category.is_(None), func.trim(Device.sub_category) == ""),
               Device.device_type.isnot(None), func.trim(Device.device_type) != "")
    )).all()
    out = []
    for did, tag, dt in rows:
        _dt, sc = pair_type_and_category(dt, None, [], sc_master)
        if sc:
            out.append((did, tag, dt, sc))
    return out


@router.post("/subcategory-fill")
async def subcategory_fill_apply(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import func, or_, update
    from database import AsyncSessionLocal
    from models.device import Device
    from services.audit_engine import audit
    from utils.timezone import app_now
    async with AsyncSessionLocal() as db:
        changes = await _subcat_changes(db)
        groups: dict = {}
        for did, _tag, _dt, sc in changes:
            groups.setdefault(sc, []).append(did)
        now = app_now()
        for sc, ids in groups.items():
            await db.execute(update(Device)
                             .where(Device.id.in_(ids),
                                    or_(Device.sub_category.is_(None), func.trim(Device.sub_category) == ""))
                             .values(sub_category=sc, updated_at=now)
                             .execution_options(synchronize_session=False))
        if changes:
            await audit(db, user=current_user, action="SUB_CATEGORY_FILL_FROM_DEVICE_TYPE",
                        table_name="devices", record_id=f"{len(changes)} tags",
                        old_value={"sub_category": None},
                        new_value={"count": len(changes), "by_value": {k: len(v) for k, v in groups.items()},
                                   "device_ids": {k: [str(x) for x in v] for k, v in groups.items()}},
                        notes="Sub-Category was blank; set from the tag's Device Type. "
                              "Reverse: set sub_category = NULL for these ids.",
                        request=request)
        await db.commit()
    back = _back_to_page(await request.form(), f"Sub-Category fill done: {len(changes):,} tags updated.")
    if back:
        return back
    return JSONResponse({"ok": True, "updated": len(changes),
                         "by_value": {k: len(v) for k, v in groups.items()}})


# ── RAM / Storage data check (2026-10-22) ────────────────────────────────────
# Read-only. Tags whose values look entered wrong, for a person to correct:
# sizes no laptop RAM has, RAM and storage likely swapped, and summary/total
# pairs that disagree. The fill above never changes a value already there.

def _spec_issues_for(r) -> list:
    from utils.device_spec import size_to_gb
    issues = []
    ram_txt = [size_to_gb(r.total_ram_size), size_to_gb(r.ram_summary)]
    if r.ram_gb and r.ram_gb > 128:
        issues.append(f"RAM GB {r.ram_gb} is over 128")
    elif any(g and g > 128 for g in ram_txt):
        issues.append("RAM text is over 128GB")
    if r.storage_gb and r.storage_gb < 32:
        issues.append(f"SSD GB {r.storage_gb} is under 32")
    if r.ram_gb and r.storage_gb and r.ram_gb >= r.storage_gb:
        issues.append("RAM is not smaller than storage (swapped?)")

    def differs(a, b):
        return a and b and max(a, b) > min(a, b) * 1.25
    if differs(*ram_txt):
        issues.append("RAM summary and Total RAM Size disagree")
    elif differs(r.ram_gb, ram_txt[0] or ram_txt[1]):
        issues.append("RAM GB and RAM text disagree")
    hdd_txt = [size_to_gb(r.total_hdd_size), size_to_gb(r.hdd_summary)]
    if differs(*hdd_txt):
        issues.append("Hard Drive and Total HDD Size disagree")
    else:
        drives = (r.storage_gb or 0) + (r.hdd_capacity_gb or 0)
        if differs(drives, hdd_txt[0] or hdd_txt[1]) and differs(r.storage_gb, hdd_txt[0] or hdd_txt[1]):
            issues.append("SSD/HDD GB and storage text disagree")
    return issues


def _issue_counts(rows) -> list:
    """[(issue, tags)] most common first; "RAM GB 256 is over 128" counts as
    "RAM GB is over 128"."""
    import re
    counts: dict = {}
    for r in rows:
        for i in r["issues"]:
            k = re.sub(r"^(RAM|SSD) GB \d+ ", r"\1 GB ", i)
            counts[k] = counts.get(k, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])


async def _spec_issues(db):
    from sqlalchemy import select
    from models.device import Device
    cols = [Device.barcode, Device.brand, Device.model, Device.current_stage] + \
        [getattr(Device, c) for c in _SPEC_READ]
    out = []
    for r in (await db.execute(select(*cols).where(Device.is_trashed.isnot(True))
                               .order_by(Device.barcode))).all():
        issues = _spec_issues_for(r)
        if issues:
            out.append({"tag": r.barcode, "brand": r.brand or "", "model": r.model or "",
                        "stage": getattr(r.current_stage, "value", r.current_stage) or "",
                        "issues": issues,
                        **{c: getattr(r, c) for c in _SPEC_READ}})
    return out


@router.get("/spec-check")
async def spec_check(csv: int = 0, current_user: User = Depends(admin_only)):
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        rows = await _spec_issues(db)
    if not csv:
        return JSONResponse({"tags": len(rows), "by_issue": dict(_issue_counts(rows)), "first_50": rows[:50]})
    import csv as _csv
    import io
    from fastapi.responses import Response
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["Tag", "Brand", "Model", "Stage", "Issues", "RAM GB", "RAM", "Total RAM Count",
                "Total RAM Size", "SSD GB", "Hard Drive", "Total HDD Count", "Total HDD Size", "HDD GB"])
    for r in rows:
        w.writerow([r["tag"], r["brand"], r["model"], r["stage"], "; ".join(r["issues"]),
                    *[r[c] if r[c] is not None else "" for c in _SPEC_READ]])
    return Response("﻿" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ram_storage_check.csv"})


# ── Tag numbers that differ only by letter case (2026-10-22) ─────────────────
# Read-only. e.g. 4430110426L0274 and 4430110426l0274 are two separate tags
# in the database but the same number to a person. Lists each group with
# what hangs off every copy (sales, returns), so someone can decide which
# copy is real. Trashed copies are listed too, marked.

async def _case_duplicates(db):
    from sqlalchemy import func, select
    from models.device import Device
    from models.lot import Lot
    from models.sales import Sale, Return
    keys = select(func.upper(Device.barcode).label("k")).group_by(func.upper(Device.barcode)) \
        .having(func.count(Device.id) > 1).subquery()
    sales_n = select(func.count(Sale.id)).where(Sale.device_id == Device.id).correlate(Device).scalar_subquery()
    ret_n = select(func.count(Return.id)).where(Return.device_id == Device.id).correlate(Device).scalar_subquery()
    rows = (await db.execute(
        select(Device.barcode, Device.current_stage, Device.entity, Device.brand, Device.model,
               Device.is_trashed, Device.created_at, Lot.lot_number, sales_n.label("sales"), ret_n.label("returns"))
        .join(keys, keys.c.k == func.upper(Device.barcode))
        .outerjoin(Lot, Lot.id == Device.lot_id)
        .order_by(func.upper(Device.barcode), Device.created_at)
    )).all()
    groups: dict = {}
    for r in rows:
        groups.setdefault(r.barcode.upper(), []).append({
            "tag": r.barcode, "stage": getattr(r.current_stage, "value", r.current_stage),
            "entity": r.entity or "", "model": f"{r.brand or ''} {r.model or ''}".strip(),
            "lot": r.lot_number or "", "trashed": bool(r.is_trashed),
            "created": r.created_at.strftime("%d-%m-%Y") if r.created_at else "",
            "sales": int(r.sales or 0), "returns": int(r.returns or 0)})
    return [{"key": k, "tags": v} for k, v in groups.items()]


@router.get("/tag-case-duplicates")
async def tag_case_duplicates(current_user: User = Depends(admin_only)):
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        groups = await _case_duplicates(db)
    live = [g for g in groups if sum(1 for t in g["tags"] if not t["trashed"]) > 1]
    return JSONResponse({"groups": len(groups), "groups_with_2_live_copies": len(live), "list": groups})


# ── Past reassignments recorded as "completed" (2026-10-23) ──────────────────
# Before 2026-10-23, Pick This / Change Engineer / Customise or Manual Move
# closed the previous engineer's WorkID as "completed", so it counted as their
# finished work. Those WorkIDs are found by the action's own Asset History
# entry on the same tag within 10 seconds of the WorkID's completed time, and
# only when the WorkID already existed before that action (so the new
# engineer's own WorkID is never caught). Apply sets them to "reassigned";
# completed_at (the close time) is kept. Audited with every id.
_REASSIGN_NOTES = ("Picked by %", "Changed engineer to %", "Bulk Customise modal — bulk Move to Stage%",
                   "Manual move%")


async def _reassign_candidates(db):
    from datetime import timedelta
    from sqlalchemy import and_, or_, select
    from models.device import Device, StageMovement
    from models.work_order import WorkOrder
    win = timedelta(seconds=10)
    rows = (await db.execute(
        select(WorkOrder.id, WorkOrder.work_id, WorkOrder.barcode, WorkOrder.stage,
               WorkOrder.assigned_name, WorkOrder.assigned_username, WorkOrder.assigned_at,
               WorkOrder.completed_at, StageMovement.notes, StageMovement.moved_by, StageMovement.moved_at)
        .join(StageMovement, and_(
            StageMovement.device_id == WorkOrder.device_id,
            StageMovement.moved_at >= WorkOrder.completed_at - win,
            StageMovement.moved_at <= WorkOrder.completed_at + win,
            or_(*[StageMovement.notes.like(n) for n in _REASSIGN_NOTES])))
        .where(WorkOrder.status == "completed", WorkOrder.completed_at.isnot(None),
               WorkOrder.stage != "asgn", WorkOrder.assigned_at < StageMovement.moved_at - timedelta(seconds=5))
        .order_by(WorkOrder.completed_at.desc())
    )).all()
    seen, out = set(), []
    for r in rows:
        if r.id in seen:
            continue
        seen.add(r.id)
        out.append({"id": r.id, "work_id": r.work_id, "tag": r.barcode or "", "stage": r.stage,
                    "engineer": r.assigned_name or r.assigned_username or "",
                    "assigned": r.assigned_at.strftime("%d-%m-%Y %H:%M") if r.assigned_at else "",
                    "closed": r.completed_at.strftime("%d-%m-%Y %H:%M") if r.completed_at else "",
                    "action": (r.notes or "")[:80], "by": r.moved_by or ""})
    return out


@router.get("/reassign-fix")
async def reassign_fix_preview(current_user: User = Depends(admin_only)):
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        c = await _reassign_candidates(db)
    for r in c:
        r["id"] = str(r["id"])
    return JSONResponse({"workids_to_change": len(c), "list": c})


@router.post("/reassign-fix")
async def reassign_fix_apply(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import update
    from database import AsyncSessionLocal
    from models.work_order import WorkOrder
    from services.audit_engine import audit
    async with AsyncSessionLocal() as db:
        c = await _reassign_candidates(db)
        ids = [r["id"] for r in c]
        for i in range(0, len(ids), 5000):
            await db.execute(update(WorkOrder)
                             .where(WorkOrder.id.in_(ids[i:i + 5000]), WorkOrder.status == "completed")
                             .values(status="reassigned")
                             .execution_options(synchronize_session=False))
        if ids:
            await audit(db, user=current_user, action="WORKID_REASSIGNED_BACKFILL", table_name="work_orders",
                        record_id=f"{len(ids)} workids",
                        old_value={"status": "completed"},
                        new_value={"status": "reassigned", "count": len(ids),
                                   "work_order_ids": [str(i) for i in ids],
                                   "work_ids": [r["work_id"] for r in c]},
                        notes="Past Pick This / Change Engineer / stage-move closes changed from completed "
                              "to reassigned. Reverse: set status = 'completed' for these ids.",
                        request=request)
        await db.commit()
    back = _back_to_page(await request.form(), f"Reassigned fix done: {len(ids):,} WorkIDs changed to Reassigned.")
    if back:
        return back
    return JSONResponse({"ok": True, "updated": len(ids)})


# ── Physical Audit: split one-per-date rows (2026-10-24) ─────────────────────
# Rows are per Report Date + Audit/Physical Stage + Location again. Rows made
# under the short-lived one-row-per-date rule (stage / location / category
# unset) are split: the tags of each stage + location scanned into such a row
# go to their own row, and every resulting row's Current Count is recounted
# as the tags the system has at that stage + location now. Audited.

async def _audit_split_candidates(db):
    from sqlalchemy import select
    from models.audit_record import AuditRecord, AuditRecordItem
    recs = (await db.execute(
        select(AuditRecord).where(AuditRecord.my_stage.is_(None), AuditRecord.my_location_id.is_(None),
                                  AuditRecord.category.is_(None))
        .order_by(AuditRecord.report_date.desc())
    )).scalars().all()
    out = []
    for r in recs:
        items = (await db.execute(
            select(AuditRecordItem).where(AuditRecordItem.audit_record_id == r.id)
            .order_by(AuditRecordItem.created_at.asc())
        )).scalars().all()
        groups = {}
        for it in items:
            groups.setdefault((it.my_stage, it.my_location_id), []).append(it)
        if groups:
            out.append({"record": r, "groups": groups})
    return out


async def _audit_split_preview_rows(db, cands):
    from sqlalchemy import select
    from models.location import StorageLocation
    from models.device import STAGE_LABELS
    loc_ids = {loc for c in cands for (_st, loc) in c["groups"] if loc}
    names = {}
    if loc_ids:
        names = {l.id: (l.unit_id or str(l.id)) for l in (await db.execute(
            select(StorageLocation).where(StorageLocation.id.in_(loc_ids)))).scalars().all()}
    return [{"date": c["record"].report_date.strftime("%d-%m-%Y"),
             "rows": [{"stage": str(STAGE_LABELS.get(st, getattr(st, "value", st) or "—")),
                       "location": names.get(loc, "No Location") if loc else "No Location",
                       "tags": len(its)} for (st, loc), its in c["groups"].items()]}
            for c in cands]


@router.get("/audit-split-fix")
async def audit_split_preview(current_user: User = Depends(admin_only)):
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        rows = await _audit_split_preview_rows(db, await _audit_split_candidates(db))
    return JSONResponse({"rows_to_split": len(rows), "list": rows})


@router.post("/audit-split-fix")
async def audit_split_apply(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import update
    from database import AsyncSessionLocal
    from models.audit_record import AuditRecord, AuditRecordItem
    from routers.physical_audits import _count_devices_at, _ANY
    from services.audit_engine import audit
    made = 0
    async with AsyncSessionLocal() as db:
        for c in await _audit_split_candidates(db):
            r = c["record"]
            before = {"current_count": r.current_count, "physical_count": r.physical_count}
            result = []
            for n, ((st, loc), its) in enumerate(c["groups"].items()):
                if n == 0:
                    target = r
                    target.my_stage, target.my_location_id = st, loc
                else:
                    target = AuditRecord(report_date=r.report_date, my_stage=st, my_location_id=loc,
                                         current_count=0, physical_count=0)
                    db.add(target)
                    await db.flush()
                    await db.execute(update(AuditRecordItem)
                                     .where(AuditRecordItem.id.in_([it.id for it in its]))
                                     .values(audit_record_id=target.id)
                                     .execution_options(synchronize_session=False))
                    made += 1
                target.current_count = await _count_devices_at(db, st, loc, _ANY) if st else 0
                target.physical_count = sum(1 for it in its if it.device_id is not None)
                result.append({"id": str(target.id), "stage": getattr(st, "value", st),
                               "location_id": str(loc) if loc else None, "tags": len(its),
                               "current_count": target.current_count})
            await audit(db, user=current_user, action="PHYSICAL_AUDIT_ROW_SPLIT", table_name="audit_records",
                        record_id=str(r.id), old_value={"report_date": r.report_date.isoformat(), **before},
                        new_value={"rows": result},
                        notes="One-per-date audit row split into one row per Stage + Location; "
                              "Current Count recounted at each stage + location.",
                        request=request)
        await db.commit()
    back = _back_to_page(await request.form(), f"Physical Audit rows split: {made} new row(s).")
    if back:
        return back
    return JSONResponse({"ok": True, "new_rows": made})


# ── Physical Audit: Scrapped + Scrap for Sale rows (2026-10-24) ───────────────
# Scrapped and Scrap for Sale are one audit stage now. Rows saved before that
# counted only the stage picked (e.g. Current 0 for "Scrapped" while the tags
# sit in Scrap for Sale). Fix: per Report Date + Location, join the two scrap
# rows into one and recount Current Count across both stages. Audited.

async def _audit_scrap_candidates(db):
    from sqlalchemy import select
    from models.audit_record import AuditRecord
    from routers.physical_audits import SCRAP_GROUP, _count_devices_at, _ANY
    recs = (await db.execute(
        select(AuditRecord).where(AuditRecord.my_stage.in_(SCRAP_GROUP))
        .order_by(AuditRecord.report_date.desc(), AuditRecord.created_at.asc())
    )).scalars().all()
    groups = {}
    for r in recs:
        groups.setdefault((r.report_date, r.my_location_id), []).append(r)
    out = []
    for (d, loc), rows in groups.items():
        now = await _count_devices_at(db, SCRAP_GROUP[0], loc, _ANY)
        if len(rows) > 1 or rows[0].current_count != now:
            out.append({"date": d, "location_id": loc, "rows": rows, "current_now": now})
    return out


async def _audit_scrap_preview(db, cands):
    from sqlalchemy import select
    from models.location import StorageLocation
    ids = {c["location_id"] for c in cands if c["location_id"]}
    names = {}
    if ids:
        names = {l.id: l.unit_id for l in (await db.execute(
            select(StorageLocation).where(StorageLocation.id.in_(ids)))).scalars().all()}
    return [{"date": c["date"].strftime("%d-%m-%Y"),
             "location": names.get(c["location_id"], "No Location") if c["location_id"] else "No Location",
             "rows": len(c["rows"]), "current_before": sum(r.current_count or 0 for r in c["rows"]),
             "current_now": c["current_now"]} for c in cands]


@router.get("/audit-scrap-fix")
async def audit_scrap_preview(current_user: User = Depends(admin_only)):
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        rows = await _audit_scrap_preview(db, await _audit_scrap_candidates(db))
    return JSONResponse({"groups": len(rows), "list": rows})


@router.post("/audit-scrap-fix")
async def audit_scrap_apply(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import update, delete, select, func
    from database import AsyncSessionLocal
    from models.audit_record import AuditRecord, AuditRecordItem
    from services.audit_engine import audit
    fixed = 0
    async with AsyncSessionLocal() as db:
        for c in await _audit_scrap_candidates(db):
            keep, others = c["rows"][0], c["rows"][1:]
            old = [{"id": str(r.id), "my_stage": r.my_stage.value, "current_count": r.current_count,
                    "physical_count": r.physical_count, "notes": r.notes} for r in c["rows"]]
            if others:
                oids = [r.id for r in others]
                await db.execute(update(AuditRecordItem).where(AuditRecordItem.audit_record_id.in_(oids))
                                 .values(audit_record_id=keep.id).execution_options(synchronize_session=False))
                notes = [n for n in [keep.notes] + [r.notes for r in others] if (n or "").strip()]
                keep.notes = " | ".join(notes) if notes else None
            keep.current_count = c["current_now"]
            keep.physical_count = (await db.execute(
                select(func.count(AuditRecordItem.id)).where(AuditRecordItem.audit_record_id == keep.id,
                                                             AuditRecordItem.device_id.isnot(None))
            )).scalar() or 0
            await audit(db, user=current_user, action="PHYSICAL_AUDIT_SCRAP_ROWS_FIXED", table_name="audit_records",
                        record_id=str(keep.id), old_value={"rows": old},
                        new_value={"kept_row": str(keep.id), "current_count": keep.current_count,
                                   "physical_count": keep.physical_count},
                        notes="Scrapped + Scrap for Sale audit rows joined; Current Count recounted across both.",
                        request=request)
            if others:
                await db.execute(delete(AuditRecord).where(AuditRecord.id.in_([r.id for r in others]))
                                 .execution_options(synchronize_session=False))
            fixed += 1
        await db.commit()
    back = _back_to_page(await request.form(), f"Physical Audit scrap rows fixed: {fixed}.")
    if back:
        return back
    return JSONResponse({"ok": True, "fixed": fixed})


# ── Physical Audit: split plain L1/L2 + L3/L4 rows into PNA / Line (2026-10-24)
# Rows scanned before the PNA checkbox existed carry no PNA / Line. Each
# scanned tag goes by its PNA mark in the system: an active PNA part ->
# "<stage> - PNA" row, otherwise (and tag numbers not in the system) ->
# "<stage> - Line" row. Joins an existing PNA / Line row of the same date +
# stage + location; Current Count is recounted for each half. Audited.

async def _audit_pna_candidates(db):
    from sqlalchemy import select
    from models.audit_record import AuditRecord, AuditRecordItem
    from routers.physical_audits import PNA_STAGES
    from services.pna_lookup import active_pna_parts
    recs = (await db.execute(
        select(AuditRecord).where(AuditRecord.my_stage.in_(list(PNA_STAGES)), AuditRecord.pna_group.is_(None))
        .order_by(AuditRecord.report_date.desc(), AuditRecord.created_at.asc())
    )).scalars().all()
    out = []
    for r in recs:
        items = (await db.execute(
            select(AuditRecordItem).where(AuditRecordItem.audit_record_id == r.id))).scalars().all()
        ids = {it.device_id for it in items if it.device_id}
        with_pna = set(await active_pna_parts(db, ids)) if ids else set()
        pna_items = [it for it in items if it.device_id in with_pna]
        line_items = [it for it in items if it.device_id not in with_pna]
        out.append({"record": r, "pna": pna_items, "line": line_items})
    return out


async def _audit_pna_preview(db, cands):
    from sqlalchemy import select
    from models.location import StorageLocation
    from models.device import STAGE_LABELS
    ids = {c["record"].my_location_id for c in cands if c["record"].my_location_id}
    names = {}
    if ids:
        names = {l.id: l.unit_id for l in (await db.execute(
            select(StorageLocation).where(StorageLocation.id.in_(ids)))).scalars().all()}
    return [{"date": c["record"].report_date.strftime("%d-%m-%Y"),
             "stage": str(STAGE_LABELS.get(c["record"].my_stage, c["record"].my_stage)),
             "location": names.get(c["record"].my_location_id, "No Location") if c["record"].my_location_id
             else "No Location",
             "pna_tags": len(c["pna"]), "line_tags": len(c["line"])} for c in cands]


@router.get("/audit-pna-fix")
async def audit_pna_preview(current_user: User = Depends(admin_only)):
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        rows = await _audit_pna_preview(db, await _audit_pna_candidates(db))
    return JSONResponse({"rows": len(rows), "list": rows})


@router.post("/audit-pna-fix")
async def audit_pna_apply(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import update, delete, select, func
    from database import AsyncSessionLocal
    from models.audit_record import AuditRecord, AuditRecordItem
    from routers.physical_audits import _count_devices_at, _ANY
    from services.audit_engine import audit
    done = 0
    async with AsyncSessionLocal() as db:
        for c in await _audit_pna_candidates(db):
            r = c["record"]
            before = {"current_count": r.current_count, "physical_count": r.physical_count}
            loc_match = (AuditRecord.my_location_id == r.my_location_id) if r.my_location_id \
                else AuditRecord.my_location_id.is_(None)
            existing = {x.pna_group: x for x in (await db.execute(
                select(AuditRecord).where(AuditRecord.report_date == r.report_date,
                                          AuditRecord.my_stage == r.my_stage, loc_match,
                                          AuditRecord.pna_group.in_(["PNA", "Line"]))
            )).scalars().all()}
            # Only the halves that get tags (no empty PNA / Line rows). The plain
            # row itself becomes the first half without an existing row.
            needed = [g for g, its in (("Line", c["line"]), ("PNA", c["pna"])) if its] or ["Line"]
            targets, reused = {}, False
            for grp in needed:
                if grp in existing:
                    targets[grp] = existing[grp]
                elif not reused:
                    r.pna_group = grp
                    targets[grp] = r
                    reused = True
                else:
                    t = AuditRecord(report_date=r.report_date, my_stage=r.my_stage,
                                    my_location_id=r.my_location_id, pna_group=grp,
                                    current_count=0, physical_count=0)
                    db.add(t)
                    await db.flush()
                    targets[grp] = t
            for grp, its in (("PNA", c["pna"]), ("Line", c["line"])):
                if grp not in targets:
                    continue
                tid = targets[grp].id
                ids = [it.id for it in its if it.audit_record_id != tid]
                if ids:
                    await db.execute(update(AuditRecordItem).where(AuditRecordItem.id.in_(ids))
                                     .values(audit_record_id=tid).execution_options(synchronize_session=False))
            result = {}
            for grp, t in targets.items():
                t.current_count = await _count_devices_at(db, t.my_stage, t.my_location_id, _ANY, grp)
                t.physical_count = (await db.execute(
                    select(func.count(AuditRecordItem.id)).where(AuditRecordItem.audit_record_id == t.id,
                                                                 AuditRecordItem.device_id.isnot(None))
                )).scalar() or 0
                result[grp] = {"id": str(t.id), "current_count": t.current_count, "physical_count": t.physical_count}
            await audit(db, user=current_user, action="PHYSICAL_AUDIT_PNA_SPLIT", table_name="audit_records",
                        record_id=str(r.id),
                        old_value={"report_date": r.report_date.isoformat(), "stage": r.my_stage.value, **before},
                        new_value={"rows": result, "pna_tags": len(c["pna"]), "line_tags": len(c["line"])},
                        notes="Plain L1/L2 / L3/L4 audit row split into PNA / Line rows by each tag's "
                              "PNA mark in the system.", request=request)
            if not reused:          # all tags went into existing rows: the plain row is empty now
                await db.execute(delete(AuditRecord).where(AuditRecord.id == r.id)
                                 .execution_options(synchronize_session=False))
            done += 1
        await db.commit()
    back = _back_to_page(await request.form(), f"Physical Audit rows split into PNA / Line: {done}.")
    if back:
        return back
    return JSONResponse({"ok": True, "split": done})


# ── Hold for Selling: released tags still assigned (2026-10-24) ─────────────
# Release used to clear only Device.is_blocked, leaving assigned_to_user_id =
# the block's Block For user, so Ready for Sale kept showing Blocked. Fix:
# clear that assignment on tags with a released (not sold) hold, no open hold,
# and still assigned to that hold's Block For user. Audited.

async def _hold_release_candidates(db):
    from sqlalchemy import select, and_, or_
    from models.device import Device, DeviceStage
    from models.user import User
    from models.block_tags import BlockRecord, BlockRecordItem, BlockItemStatus, SOLD_NOTE_PREFIX
    from sqlalchemy.orm import aliased
    open_item = aliased(BlockRecordItem)   # own alias: the outer query joins BlockRecordItem too
    still_blocked = (select(open_item.id)
                     .where(open_item.device_id == Device.id,
                            open_item.status == BlockItemStatus.blocked)
                     .correlate(Device).exists())
    rows = (await db.execute(
        select(Device.id, Device.barcode, Device.current_stage, BlockRecord.block_id,
               User.full_name, User.username, BlockRecordItem.status_changed_at)
        .join(BlockRecordItem, BlockRecordItem.device_id == Device.id)
        .join(BlockRecord, BlockRecord.id == BlockRecordItem.block_record_id)
        .outerjoin(User, User.id == Device.assigned_to_user_id)
        .where(BlockRecordItem.status == BlockItemStatus.released,
               or_(BlockRecordItem.notes.is_(None), ~BlockRecordItem.notes.like(SOLD_NOTE_PREFIX + "%")),
               BlockRecord.block_for_user_id.isnot(None),
               Device.assigned_to_user_id == BlockRecord.block_for_user_id,
               Device.is_blocked.isnot(True),
               Device.current_stage != DeviceStage.sold,
               ~still_blocked)
        .order_by(BlockRecordItem.status_changed_at.desc().nullslast())
    )).all()
    seen, out = set(), []
    for r in rows:
        if r.id in seen:
            continue
        seen.add(r.id)
        out.append({"id": r.id, "tag": r.barcode or "", "block_id": r.block_id or "",
                    "assigned": r.full_name or r.username or "",
                    "stage": getattr(r.current_stage, "value", r.current_stage) or "",
                    "released": r.status_changed_at.strftime("%d-%m-%Y %H:%M") if r.status_changed_at else ""})
    return out


@router.get("/hold-release-fix")
async def hold_release_preview(current_user: User = Depends(admin_only)):
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        c = await _hold_release_candidates(db)
    for r in c:
        r["id"] = str(r["id"])
    return JSONResponse({"tags": len(c), "list": c})


@router.post("/hold-release-fix")
async def hold_release_apply(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import update
    from database import AsyncSessionLocal
    from models.device import Device
    from services.audit_engine import audit
    async with AsyncSessionLocal() as db:
        c = await _hold_release_candidates(db)
        ids = [r["id"] for r in c]
        for i in range(0, len(ids), 5000):
            await db.execute(update(Device).where(Device.id.in_(ids[i:i + 5000]))
                             .values(assigned_to_user_id=None)
                             .execution_options(synchronize_session=False))
        if ids:
            await audit(db, user=current_user, action="HOLD_RELEASE_UNASSIGN_BACKFILL", table_name="devices",
                        record_id=f"{len(ids)} tags",
                        old_value={"tags": [{"tag": r["tag"], "block_id": r["block_id"], "assigned": r["assigned"]}
                                            for r in c]},
                        new_value={"assigned_to_user_id": None, "count": len(ids)},
                        notes="Released Hold for Selling tags still assigned to the Block For user "
                              "(showed Blocked on Ready for Sale). Assignment cleared.",
                        request=request)
        await db.commit()
    back = _back_to_page(await request.form(), f"Released holds fixed: {len(ids):,} tags now Open.")
    if back:
        return back
    return JSONResponse({"ok": True, "updated": len(ids)})


# ── Physical Audit: remove listed Missing tags (2026-10-24) ─────────────────
# Missing = a scanned Tag Number not in the system (AuditRecordItem with no
# device). Wrong scans (two tags read as one, typed names) are removed by
# listing them. Every removed row is written to the audit log first.

def _norm_tag(t: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9-]", "", t or "").upper()


def _split_tag_list(text: str) -> list:
    import re
    seen, out = set(), []
    for t in re.split(r"[\s,;]+", text or ""):
        t = t.strip()
        if t and t.upper() not in seen:
            seen.add(t.upper())
            out.append(t)
    return out


async def _audit_missing_matches(db, tags: list):
    from sqlalchemy import select, func
    from models.audit_record import AuditRecord, AuditRecordItem
    from routers.physical_audits import _stage_label, _loc_short
    from models.location import StorageLocation
    if not tags:
        return []
    rows = (await db.execute(
        select(AuditRecordItem, AuditRecord.report_date, AuditRecord.my_stage, AuditRecord.pna_group,
               StorageLocation)
        .join(AuditRecord, AuditRecordItem.audit_record_id == AuditRecord.id)
        .outerjoin(StorageLocation, StorageLocation.id == AuditRecord.my_location_id)
        .where(AuditRecordItem.device_id.is_(None),
               # Compare letters, digits and "-" only: a scan can carry hidden
               # characters (Enter / Tab / scanner separators) between two tags.
               func.upper(func.regexp_replace(AuditRecordItem.barcode, "[^A-Za-z0-9-]", "", "g"))
               .in_([_norm_tag(t) for t in tags]))
        .order_by(AuditRecord.report_date.desc(), AuditRecordItem.barcode)
    )).all()
    return [{"item": it, "tag": it.barcode, "date": d.strftime("%d-%m-%Y") if d else "",
             "stage": _stage_label(st, grp), "location": _loc_short(loc) if loc else "—"}
            for it, d, st, grp, loc in rows]


@router.post("/audit-missing-remove")
async def audit_missing_remove(request: Request, current_user: User = Depends(admin_only)):
    from sqlalchemy import delete
    from database import AsyncSessionLocal
    from models.audit_record import AuditRecordItem
    from services.audit_engine import audit
    form = await request.form()
    tags = _split_tag_list(form.get("tags") or "")
    async with AsyncSessionLocal() as db:
        found = await _audit_missing_matches(db, tags)
        ids = [f["item"].id for f in found]
        if ids:
            await audit(db, user=current_user, action="AUDIT_MISSING_TAGS_REMOVED",
                        table_name="audit_record_items", record_id=f"{len(ids)} items",
                        old_value={"items": [{"id": str(f["item"].id),
                                              "audit_record_id": str(f["item"].audit_record_id),
                                              "barcode": f["tag"], "report_date": f["date"],
                                              "stage": f["stage"], "location": f["location"],
                                              "my_stage": getattr(f["item"].my_stage, "value", f["item"].my_stage),
                                              "my_location_id": str(f["item"].my_location_id or ""),
                                              "audited_by_user_id": str(f["item"].audited_by_user_id or ""),
                                              "created_at": str(f["item"].created_at)} for f in found]},
                        new_value={"removed": len(ids)},
                        notes="Physical Audit Missing tags removed from Data Fixes (wrong scans).",
                        request=request)
            await db.execute(delete(AuditRecordItem).where(AuditRecordItem.id.in_(ids),
                                                           AuditRecordItem.device_id.is_(None)))
        await db.commit()
    back = _back_to_page(form, f"Removed {len(ids)} Missing tag(s) from Physical Audit.")
    if back:
        return back
    return JSONResponse({"ok": True, "removed": len(ids)})


# ── Data Fixes page (2026-10-22) ─────────────────────────────────────────────
# One admin page: preview + Apply button for each fill, and the data check.
# The Apply buttons are ordinary form posts the admin clicks.

@router.get("/data-fixes")
async def data_fixes_page(request: Request, done: str = "", missing_tags: str = "",
                          current_user: User = Depends(admin_only)):
    from templates_config import templates
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        spec = await _spec_changes(db, with_rows=True)
        space = await _space_changes(db)
        repair = await _repair_changes(db)
        scrap_l12 = await _scrap_l12_open(db)
        stress_open = await _stress_open_left(db)
        cosm_open = await _cosmetic_open_left(db)
        rts_open = await _rts_open(db)
        rts_asgn = await _rts_asgn_closed(db)
        stype, stype_conf = await _stype_changes(db)
        subcat = await _subcat_changes(db)
        dt12_tags, dt12_master = await _dt12_changes(db)
        issues = await _spec_issues(db)
        case_dupes = await _case_duplicates(db)
        # "Reassigned but marked Completed" card removed (2026-10-28): since
        # 2026-10-27 a reassignment closes the old WorkID as Completed on purpose.
        audit_split = await _audit_split_preview_rows(db, await _audit_split_candidates(db))
        audit_scrap = await _audit_scrap_preview(db, await _audit_scrap_candidates(db))
        audit_pna = await _audit_pna_preview(db, await _audit_pna_candidates(db))
        hold_rel = await _hold_release_candidates(db)
        miss_list = _split_tag_list(missing_tags)
        miss_found = [{k: v for k, v in f.items() if k != "item"}
                      for f in await _audit_missing_matches(db, miss_list)]
    spec_fields = {c: sum(1 for _i, ch, _r in spec if c in ch) for c in _SPEC_COLS}
    spec_examples, seen = [], set()
    for _i, ch, r in spec:
        kind = tuple(sorted(ch))
        if kind not in seen:
            seen.add(kind)
            spec_examples.append({"tag": r.barcode,
                                  "before": {c: getattr(r, c) for c in _SPEC_READ if getattr(r, c) not in (None, "")},
                                  "fills": ch})
        if len(spec_examples) >= 12:
            break
    subcat_by: dict = {}
    for _id, _tag, dt, sc in subcat:
        k = (dt.strip(), sc)
        subcat_by[k] = subcat_by.get(k, 0) + 1
    return templates.TemplateResponse("admin/data_fixes.html", {
        "request": request, "current_user": current_user, "done": done,
        "spec_total": len(spec), "spec_fields": spec_fields, "spec_examples": spec_examples,
        "space_total": len(space),
        "space_fields": {f: sum(1 for _d, _t, ch in space if f in ch) for f in _SPACE_FIELDS},
        "space_examples": [{"tag": t, "ch": ch} for _d, t, ch in space[:12]],
        "scrap_l12": scrap_l12[:200], "scrap_l12_total": len(scrap_l12),
        "stress_open": stress_open[:200], "stress_open_total": len(stress_open),
        "cosm_open": cosm_open[:200], "cosm_open_total": len(cosm_open),
        "rts_open": rts_open[:200], "rts_open_total": len(rts_open),
        "rts_asgn": rts_asgn[:200], "rts_asgn_total": len(rts_asgn),
        "stype_total": len(stype), "stype_examples": [{"tag": t, "ch": ch} for _d, t, ch in stype[:15]],
        "stype_conf": stype_conf[:100], "stype_conf_total": len(stype_conf),
        "cosm_open_re": sum(1 for r in cosm_open if r[6] == "reassigned"),
        "stress_open_dup": sum(1 for r in stress_open if r[5] == "reassigned"),
        "repair_total": len(repair),
        "repair_kinds": [(lbl, sum(1 for c in repair if k in c[2])) for k, lbl in _REPAIR_KIND_LABELS.items()],
        "repair_examples": list({t: {"tag": t, "kinds": ", ".join(_REPAIR_KIND_LABELS[k] for k in ks), "ch": ch}
                                 for k in _REPAIR_KIND_LABELS
                                 for _d, t, ks, ch in [c for c in repair if k in c[2]][:4]}.values()),
        "dt12_tags": [{"old": o, "new": n, "count": c} for o, n, c in dt12_tags],
        "dt12_total": sum(c for _o, _n, c in dt12_tags), "dt12_master": dt12_master,
        "dt12_list": DEVICE_TYPES_12,
        "subcat_total": len(subcat), "subcat_by": [{"dt": k[0], "sc": k[1], "n": n} for k, n in sorted(subcat_by.items(), key=lambda x: -x[1])],
        "issues": issues[:500], "issues_total": len(issues),
        "issue_counts": _issue_counts(issues),
        # Only numbers with 2+ copies NOT in Trash need a decision; the rest
        # (one copy already trashed) are counted, not listed.
        "audit_split": audit_split, "audit_scrap": audit_scrap, "audit_pna": audit_pna, "hold_rel": hold_rel[:300], "hold_rel_total": len(hold_rel),
        "missing_tags": missing_tags, "miss_list": miss_list, "miss_found": miss_found,
        "miss_not_found": [t for t in miss_list
                           if _norm_tag(t) not in {_norm_tag(f["tag"]) for f in miss_found}],
        "case_dupes": [g for g in case_dupes if sum(1 for t in g["tags"] if not t["trashed"]) > 1],
        "case_dupes_handled": sum(1 for g in case_dupes if sum(1 for t in g["tags"] if not t["trashed"]) <= 1),
        "field_labels": {"ram_gb": "RAM GB", "ram_summary": "RAM", "total_ram_count": "Total RAM Count",
                         "total_ram_size": "Total RAM Size", "storage_gb": "SSD GB",
                         "hdd_summary": "Hard Drive", "total_hdd_count": "Total HDD Count",
                         "total_hdd_size": "Total HDD Size", "hdd_capacity_gb": "HDD GB"},
    })
