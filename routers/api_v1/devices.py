"""
JSON API — Devices
GET   /api/v1/devices                  list with filters + pagination
GET   /api/v1/devices/{barcode}        single device detail
PATCH /api/v1/devices/{barcode}/stage  move stage (FSM-validated via AllowedTransition table)
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.device import Device, DeviceStage, StageMovement
from models.api_key import APIKey
from models.stage_control import AllowedTransition
from auth.api_key import require_scope
from schemas.device import DeviceOut, DeviceListItem, DeviceStageMoveRequest
from schemas.common import PaginatedResponse
from datetime import datetime
from services.event_bus import EventType, publish

router = APIRouter(prefix="/devices", tags=["api-v1-devices"])


@router.get("", response_model=PaginatedResponse[DeviceListItem])
async def list_devices(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    stage: Optional[str] = Query(default=None),
    sub_category: Optional[str] = Query(default=None),
    brand: Optional[str] = Query(default=None),
    lot_id: Optional[str] = Query(default=None),
    grade: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_scope("devices:read")),
):
    query = select(Device).where(Device.is_trashed == False)
    if stage:
        try:
            query = query.where(Device.current_stage == DeviceStage(stage))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid stage: {stage}")
    if sub_category:
        query = query.where(Device.sub_category == sub_category)
    if brand:
        query = query.where(Device.brand.ilike(f"%{brand}%"))
    if lot_id:
        query = query.where(Device.lot_id == lot_id)
    if grade:
        query = query.where(Device.grade == grade)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    result = await db.execute(
        query.order_by(Device.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    devices = result.scalars().all()
    total_pages = max(1, (total + page_size - 1) // page_size)

    return PaginatedResponse[DeviceListItem](
        items=[DeviceListItem.model_validate(d) for d in devices],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/{barcode}", response_model=DeviceOut)
async def get_device(
    barcode: str,
    db: AsyncSession = Depends(get_db),
    _key: APIKey = Depends(require_scope("devices:read")),
):
    result = await db.execute(select(Device).where(Device.barcode == barcode))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail=f"Device '{barcode}' not found")
    return DeviceOut.model_validate(device)


@router.patch("/{barcode}/stage", response_model=DeviceOut)
async def move_device_stage(
    barcode: str,
    body: DeviceStageMoveRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    api_key: APIKey = Depends(require_scope("devices:write")),
):
    result = await db.execute(select(Device).where(Device.barcode == barcode))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail=f"Device '{barcode}' not found")

    current = device.current_stage.value if hasattr(device.current_stage, "value") else str(device.current_stage)

    # FSM validation via AllowedTransition table
    t_result = await db.execute(
        select(AllowedTransition).where(
            AllowedTransition.from_stage == current,
            AllowedTransition.to_stage == body.to_stage,
        )
    )
    if not t_result.scalar_one_or_none():
        raise HTTPException(
            status_code=422,
            detail=f"Transition '{current}' → '{body.to_stage}' is not allowed by the FSM",
        )

    try:
        new_stage = DeviceStage(body.to_stage)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown stage: {body.to_stage}")

    # Into Pass Hold / Ready to Sale without the Final QC Pass button: write
    # verified RAM/Hard Drive changes onto the spec (2026-10-15).
    from services.parts_changed import apply_spec_changes_if_past_final_qc
    await apply_spec_changes_if_past_final_qc(db, device, current, new_stage)
    device.current_stage = new_stage
    # Arrived at L1/L2, L3/L4, Stress or Final QC: one pending WorkID (2026-10-27).
    from services.stage_work_orders import open_for_stage as _open_for_stage
    await _open_for_stage(db, device, new_stage, created_by=f"api_key:{api_key.name}")
    movement = StageMovement(
        device_id=device.id,
        from_stage=current,
        to_stage=body.to_stage,
        moved_by=f"api_key:{api_key.name}",
        notes=body.notes or f"API stage move via {api_key.key_prefix}",
    )
    db.add(movement)
    await db.commit()
    await db.refresh(device)
    publish(EventType.STAGE_MOVED, {
        "barcode": barcode,
        "from_stage": current,
        "to_stage": body.to_stage,
        "api_key_name": api_key.name,
        "_source": "devices_api",
    }, background_tasks)
    return DeviceOut.model_validate(device)
