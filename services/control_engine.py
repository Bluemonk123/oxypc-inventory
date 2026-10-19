"""
Control Engine
--------------
Enforces:
  1. Stage transition validation (allowed_transitions table)
  2. Sale block (device must be ready_to_sale)
  3. Repair level escalation order (L1 before L2 before L3)

All rejections raise HTTPException(403) with a structured message.
Every decision is logged via AuditEngine.
"""
from __future__ import annotations
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models.device import Device, DeviceStage
from models.stage_control import AllowedTransition


class ControlEngineError(Exception):
    """Raised when a control rule is violated."""
    pass


def assert_device_in_stage(device: Device, expected: DeviceStage) -> None:
    """
    Verify the device is currently in `expected` stage before performing any
    stage-scoped action (start repair, complete QC, etc.).

    Raises HTTPException(409) if the device has already moved to a different stage.
    Call this immediately after loading the device from the DB, before any writes.
    """
    current = device.current_stage.value if hasattr(device.current_stage, "value") else str(device.current_stage)
    target  = expected.value if hasattr(expected, "value") else str(expected)
    if current != target:
        raise HTTPException(
            status_code=409,
            detail=(
                f"STAGE CONFLICT: '{device.barcode}' is currently in stage '{current}', "
                f"not '{target}'. The device may have been moved. "
                f"Please refresh the page before retrying."
            ),
        )


# ── AllowedTransitions in-memory cache ────────────────────────────────────────
# Populated on first call; call invalidate_transitions_cache() after any
# INSERT / UPDATE / DELETE on the allowed_transitions table.
_transitions_cache: dict | None = None


async def get_transitions(db: AsyncSession) -> dict:
    """Return full AllowedTransitions map, using in-memory cache."""
    global _transitions_cache
    if _transitions_cache is not None:
        return _transitions_cache
    result = await db.execute(select(AllowedTransition))
    rows = result.scalars().all()
    _transitions_cache = {(r.from_stage, r.to_stage): r for r in rows}
    return _transitions_cache


def invalidate_transitions_cache() -> None:
    """Call after any admin INSERT/UPDATE/DELETE on allowed_transitions."""
    global _transitions_cache
    _transitions_cache = None


async def validate_transition(
    device: Device,
    to_stage: str,
    db: AsyncSession,
    override_admin: bool = False,
) -> None:
    """
    Checks whether moving `device` from its current stage to `to_stage` is permitted.
    Raises HTTPException(403) if not allowed.
    Admins may override by passing override_admin=True.
    """
    if override_admin:
        return  # admin bypasses table check

    from_stage = device.current_stage.value if hasattr(device.current_stage, "value") else str(device.current_stage)
    to_stage_val = to_stage.value if hasattr(to_stage, "value") else str(to_stage)

    if from_stage == to_stage_val:
        return  # staying in the same stage is a no-op, not a transition to validate
                # (e.g. Start Repair on a device already in the L1/L2/L3 stage)

    result = await db.execute(
        select(AllowedTransition).where(
            AllowedTransition.from_stage == from_stage,
            AllowedTransition.to_stage   == to_stage_val,
        )
    )
    transition = result.scalar_one_or_none()

    if not transition:
        raise HTTPException(
            status_code=403,
            detail=(
                f"CONTROL ENGINE: Transition from '{from_stage}' to '{to_stage_val}' "
                f"is not permitted. Check allowed_transitions table."
            ),
        )


async def validate_sale_allowed(device: Device) -> None:
    """
    Sale hold check. Since 2026-10-19 a sale is allowed from ANY stage
    (director's decision: tags can be marked sold from Ready to Sale no
    matter where they sit); the entity rule (tag entity must match the
    seller's entity) is enforced by create_sale, which knows the user.
    Still refused: a tag held under Block Tags.
    """
    if device.is_blocked:
        raise HTTPException(
            status_code=403,
            detail=(
                f"CONTROL ENGINE: Sale blocked — tag {device.barcode} is held under "
                f"Block Tags. Release it from the Block Tags page before it can be sold."
            ),
        )


async def validate_repair_level(device: Device, requested_level: int, db: AsyncSession) -> None:
    """
    Enforce L1 before L2 before L3 repair escalation order.
    L2 can only start if L1 has been attempted.
    L3 can only start if L2 has been attempted.
    """
    from models.repair import RepairJob
    if requested_level == 1:
        return  # L1 always allowed

    # L3→L3 self-transition: device already in L3, allow starting a new L3 job
    from models.device import DeviceStage as _DS
    current_stage_val = device.current_stage.value if hasattr(device.current_stage, "value") else str(device.current_stage)
    if requested_level == 3 and current_stage_val == _DS.l3.value:
        return

    required_prev = requested_level - 1
    result = await db.execute(
        select(RepairJob).where(
            RepairJob.device_id == device.id,
            RepairJob.stage == f"L{required_prev}",
        )
    )
    prev_job = result.scalars().first()
    if not prev_job:
        raise HTTPException(
            status_code=403,
            detail=(
                f"CONTROL ENGINE: L{requested_level} repair requires L{required_prev} "
                f"to have been attempted first."
            ),
        )


async def get_allowed_next_stages(device: Device, db: AsyncSession) -> list:
    """Return list of stage names this device is allowed to move to."""
    from_stage = device.current_stage.value if hasattr(device.current_stage, "value") else str(device.current_stage)
    result = await db.execute(
        select(AllowedTransition.to_stage).where(AllowedTransition.from_stage == from_stage)
    )
    return [row[0] for row in result.all()]
