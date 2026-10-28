"""Model Requested — a TRC/Inventory-facing READ-ONLY demand view.

Aggregates the same ModelRequest data that drives the Model Requests queue
(where Sales/Telecalling raise config+qty+grade requests), but rolls it up by
configuration so the production side can see, at a glance, what models are in
demand and how much outstanding quantity remains. No create/edit/fulfil actions
live here — those stay on the Model Requests page (/model-requests). This page
is purely a demand dashboard.
"""
from templates_config import templates
from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.model_requests import ModelRequest
from models.user import User
from auth.dependencies import get_current_user

router = APIRouter(prefix="/model-requested", tags=["model-requested"])


def _config_label(r: ModelRequest) -> str:
    """Human-readable one-line spec for a request row."""
    parts = []
    if r.brand:
        parts.append(r.brand)
    if r.model:
        parts.append(r.model)
    spec = []
    if r.cpu:
        spec.append(r.cpu)
    if r.ram_gb:
        spec.append(f"{r.ram_gb} GB")
    if r.storage_gb:
        spec.append(f"{r.storage_gb} GB {r.storage_type or ''}".strip())
    if r.screen_size:
        spec.append(str(r.screen_size))
    label = " ".join(parts) if parts else "(unspecified)"
    if spec:
        label += " · " + " / ".join(spec)
    return label


@router.get("", response_class=HTMLResponse)
async def model_requested_dashboard(
    request: Request,
    status: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read-only rollup of model/config demand. Groups non-cancelled requests
    by (sub_category, brand, model, cpu, ram, storage, storage_type, grade)."""
    q = select(ModelRequest).where(ModelRequest.status != "cancelled")
    if status:
        q = q.where(ModelRequest.status == status)
    q = q.order_by(ModelRequest.created_at.desc())
    rows = (await db.execute(q)).scalars().all()

    groups: dict = {}
    for r in rows:
        key = (
            r.sub_category or "", (r.brand or "").strip(), (r.model or "").strip(),
            (r.cpu or "").strip(), r.ram_gb or 0, r.storage_gb or 0,
            r.storage_type or "", r.grade or "Any",
        )
        g = groups.get(key)
        if not g:
            g = {
                "label": _config_label(r),
                "sub_category": r.sub_category or "—",
                "grade": r.grade or "Any",
                "qty_requested": 0,
                "qty_fulfilled": 0,
                "open_requests": 0,
                "requesters": set(),
            }
            groups[key] = g
        g["qty_requested"] += r.qty_requested or 0
        g["qty_fulfilled"] += r.qty_fulfilled or 0
        if r.status in ("open", "partially_fulfilled"):
            g["open_requests"] += 1
        if r.requested_by_name or r.requested_by:
            g["requesters"].add(r.requested_by_name or r.requested_by)

    demand = []
    for g in groups.values():
        outstanding = max(0, g["qty_requested"] - g["qty_fulfilled"])
        demand.append({**g, "outstanding": outstanding,
                       "requesters": ", ".join(sorted(g["requesters"])) or "—"})
    # Highest outstanding demand first.
    demand.sort(key=lambda d: (-d["outstanding"], -d["qty_requested"]))

    total_outstanding = sum(d["outstanding"] for d in demand)
    total_requested = sum(d["qty_requested"] for d in demand)
    active_configs = sum(1 for d in demand if d["outstanding"] > 0)

    return templates.TemplateResponse("model_requested/dashboard.html", {
        "request": request,
        "current_user": current_user,
        "demand": demand,
        "status": status,
        "total_outstanding": total_outstanding,
        "total_requested": total_requested,
        "active_configs": active_configs,
    })
