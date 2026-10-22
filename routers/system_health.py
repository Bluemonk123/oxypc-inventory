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
