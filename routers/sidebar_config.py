"""
Sidebar Config — admin tool to rename sidebar nav items.

Renaming a module here changes:
  1. The label shown in the left sidebar (templates/base.html, via the
     sidebar_label() Jinja global).
  2. The "Module" column in the Module Permission Matrix
     (templates/admin/master.html Tab 2), via the same helper.

Storage: reuses the AppSetting key/value table (same convention as Landing
Pages' page_title_<module_key>), key = f"sidebar_label_{module_key}".
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import get_db
from models.user import User, UserRole
from models.settings import AppSetting
from models.role_permissions import set_cached_sidebar_label, _SIDEBAR_LABEL_CACHE
from auth.dependencies import get_current_user, require_roles, verify_csrf
from routers.master import PERM_MODULES

# Sidebar defaults that differ from the Permission Matrix row label: the
# matrix row "Parts GRN / Parts Purchased" (2026-10-30) is one key covering
# two pages, but its own sidebar entry still reads "Parts Purchased".
_SIDEBAR_DEFAULTS = {"spare_parts_purchase": "Parts Purchased"}
from templates_config import templates

router = APIRouter(
    prefix="/admin/sidebar-config",
    tags=["sidebar_config"],
    dependencies=[Depends(verify_csrf)],
)
admin_only = require_roles(UserRole.admin)

SETTING_PREFIX = "sidebar_label_"


async def load_sidebar_labels_to_cache(db: AsyncSession) -> None:
    """Populate the in-memory sidebar-label cache from AppSetting rows.
    Called at app startup and after every save."""
    rows = (await db.execute(
        select(AppSetting).where(AppSetting.key.like(f"{SETTING_PREFIX}%"))
    )).scalars().all()
    _SIDEBAR_LABEL_CACHE.clear()
    for r in rows:
        module_key = r.key[len(SETTING_PREFIX):]
        if r.value:
            _SIDEBAR_LABEL_CACHE[module_key] = r.value


@router.get("/", response_class=HTMLResponse)
async def sidebar_config_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    rows = (await db.execute(
        select(AppSetting).where(AppSetting.key.like(f"{SETTING_PREFIX}%"))
    )).scalars().all()
    custom_labels = {r.key[len(SETTING_PREFIX):]: r.value for r in rows}

    items = [
        {
            "key": mod_key,
            "default_label": default_label,
            "current_label": custom_labels.get(mod_key, default_label),
            "is_custom": mod_key in custom_labels,
        }
        for mod_key, default_label in (
            (k, _SIDEBAR_DEFAULTS.get(k, lbl)) for k, lbl in PERM_MODULES)
    ]

    return templates.TemplateResponse("admin/sidebar_config.html", {
        "request": request,
        "current_user": current_user,
        "items": items,
        "success": request.query_params.get("success"),
        "error": request.query_params.get("error"),
    })


@router.post("/save")
async def save_sidebar_label(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    form = await request.form()
    module_key = (form.get("module_key") or "").strip()
    new_label = (form.get("label") or "").strip()

    if not module_key or not new_label:
        return RedirectResponse(
            url="/admin/sidebar-config/?error=Module+key+and+label+are+required",
            status_code=302,
        )

    setting_key = f"{SETTING_PREFIX}{module_key}"
    existing = (await db.execute(
        select(AppSetting).where(AppSetting.key == setting_key)
    )).scalar_one_or_none()

    if existing:
        existing.value = new_label
        existing.updated_by = current_user.username
    else:
        db.add(AppSetting(
            key=setting_key,
            value=new_label,
            description=f"Custom sidebar label for {module_key}",
            updated_by=current_user.username,
        ))

    await db.commit()
    set_cached_sidebar_label(module_key, new_label)

    return RedirectResponse(
        url=f"/admin/sidebar-config/?success=Label+saved+for+{module_key}",
        status_code=302,
    )


@router.post("/reset")
async def reset_sidebar_label(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    form = await request.form()
    module_key = (form.get("module_key") or "").strip()
    if not module_key:
        return RedirectResponse(url="/admin/sidebar-config/?error=Module+key+required", status_code=302)

    setting_key = f"{SETTING_PREFIX}{module_key}"
    existing = (await db.execute(
        select(AppSetting).where(AppSetting.key == setting_key)
    )).scalar_one_or_none()
    if existing:
        await db.delete(existing)
        await db.commit()
    _SIDEBAR_LABEL_CACHE.pop(module_key, None)

    return RedirectResponse(
        url=f"/admin/sidebar-config/?success=Label+reset+for+{module_key}",
        status_code=302,
    )
