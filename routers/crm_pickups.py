"""Pickup Process page (2026-11-11, Pankaj) — /crm/pickups.

Tabs Supplier Deal / Customer Deal; each with status pill tabs. A row lands
here once a user is assigned on the deal's Stock Received / Stock Handover
stage. Permission module `crm_pickups` (default disabled for every role).
Admin, or a role with Pickup Process → Edit, sees every pickup; anyone else
sees the ones assigned to them. The assignee (or those who see all) moves a
pickup one step at a time. services/pickups.py has the flows."""
from urllib.parse import quote_plus
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from templates_config import templates
from database import get_db
from auth.dependencies import get_current_user, verify_csrf
from models.user import User

router = APIRouter(prefix="/crm/pickups", tags=["crm-pickups"], dependencies=[Depends(verify_csrf)])
MODULE = "crm_pickups"


def _role(user):
    return getattr(user.role, "value", user.role)


def _see_all(user) -> bool:
    from models.role_permissions import has_perm
    r = _role(user)
    return r == "admin" or has_perm(r, MODULE, "edit")


def _back(kind, msg, ok=True, pill=""):
    return RedirectResponse(url=f"/crm/pickups?tab={kind}" + (f"&pill={pill}" if pill else "")
                                + f"&{'success' if ok else 'error'}={quote_plus(msg)}", status_code=302)


@router.get("", response_class=HTMLResponse)
async def pickups_page(request: Request, db: AsyncSession = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    from sqlalchemy import select
    from models.role_permissions import has_perm
    from services.pickups import pickup_rows, FLOWS, SHORT, COLORS, TILES
    if not has_perm(_role(current_user), MODULE, "enable"):
        return RedirectResponse(url="/dashboard?error=Pickup+Process+is+not+enabled+for+your+role", status_code=302)
    see_all = _see_all(current_user)
    rows = {k: await pickup_rows(db, k, current_user, see_all) for k in ("supplier", "customer")}
    counts = {k: {s: sum(1 for r in rows[k] if r["status"] == s) for s, _ in FLOWS[k]} for k in rows}
    names = {u.username: (u.full_name or u.username) for u in (await db.execute(select(User))).scalars().all()}
    return templates.TemplateResponse("crm/pickups/index.html", {
        "request": request, "current_user": current_user, "rows": rows, "counts": counts,
        "flows": FLOWS, "short": SHORT, "colors": COLORS, "tiles": TILES,
        "names": names, "see_all": see_all, "tab": request.query_params.get("tab", "supplier"),
        "pill": request.query_params.get("pill", ""),
    })


@router.post("/{kind}/{deal_id}/advance")
async def advance(kind: str, deal_id: str, request: Request, note: str = Form(default=""),
                  db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    from sqlalchemy import select
    from models.role_permissions import has_perm
    from services.pickups import advance_pickup, _models, ASSIGN_KEY, FLOWS
    if kind not in FLOWS:
        return RedirectResponse(url="/crm/pickups?error=Unknown+pickup", status_code=302)
    if not has_perm(_role(current_user), MODULE, "enable"):
        return RedirectResponse(url="/dashboard?error=Pickup+Process+is+not+enabled+for+your+role", status_code=302)
    Deal, Step, *_ = _models(kind)
    try:
        a = (await db.execute(select(Step).where(Step.deal_id == deal_id, Step.step_key == ASSIGN_KEY))).scalar_one_or_none()
    except Exception:
        a = None
    if not a:
        return _back(kind, "Pickup not found", ok=False)
    if a.assigned_to != current_user.username and not _see_all(current_user):
        return _back(kind, "Only the assigned person can update this pickup", ok=False)
    res = await advance_pickup(db, kind, a.deal_id, current_user, note, request)
    if isinstance(res, str):
        return _back(kind, res, ok=False)
    await db.commit()
    return _back(kind, f"Pickup moved to {dict(FLOWS[kind])[res[1]]}", pill=res[1])
