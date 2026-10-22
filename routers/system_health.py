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
from fastapi.responses import JSONResponse
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
    return JSONResponse({"ok": True, "updated": len(changes),
                         "per_field": {c: len(v) for c, v in per_field.items()}})
