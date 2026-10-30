"""
Audit Engine
-----------
Writes an immutable AuditLog entry for every significant system action.

Usage:
    from services.audit_engine import audit
    await audit(db, user=current_user, action="STAGE_MOVED",
                table_name="devices", record_id=str(device.id),
                notes=f"{from_stage} → {to_stage}", request=request)
"""
from __future__ import annotations
import json
from datetime import datetime
from utils.timezone import app_now
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from models.engines import AuditLog
from models.user import User


async def audit(
    db: AsyncSession,
    action: str,
    user: User | None = None,
    table_name: str | None = None,
    record_id: str | None = None,
    old_value: dict | None = None,
    new_value: dict | None = None,
    notes: str | None = None,
    request: Request | None = None,
) -> AuditLog:
    """
    Create and persist an AuditLog entry.
    Does NOT call db.commit() — caller is responsible for committing.
    """
    ip = None
    if request:
        forwarded = request.headers.get("X-Forwarded-For")
        ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else None
        )

    # audit_logs.record_id is VARCHAR(50). A bulk action that joins several
    # ids (e.g. a Credit Note for 6 tags, 95 chars) made the INSERT fail and
    # the whole request 500 (2026-10-30). Keep the first 50 characters there
    # and the full value in notes, so nothing is lost.
    record_id = str(record_id) if record_id else None
    if record_id and len(record_id) > 50:
        notes = (f"{notes} | " if notes else "") + f"record_id: {record_id}"
        record_id = record_id[:50]

    log = AuditLog(
        user_id   = user.id if user else None,
        username  = user.username if user else "system",
        action    = action,
        table_name= table_name,
        record_id = record_id,
        old_value = json.dumps(old_value, default=str) if old_value else None,
        new_value = json.dumps(new_value, default=str) if new_value else None,
        notes     = notes,
        timestamp = app_now(),
        ip_address= ip,
    )
    db.add(log)
    return log
