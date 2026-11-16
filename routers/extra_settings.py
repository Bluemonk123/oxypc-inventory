"""Extra Settings (2026-11-16, Pankaj) — Admin Settings → Extra Settings.

One page with five tabs:
  System Colors · Email        (moved from Master Data)
  Sidebar Config · Page Title Config   (moved from their own sidebar pages)
  Sidebar Alignment             (new: drag sidebar items into a new order)

Sidebar Config, Sidebar Alignment and Page Title Config list the sidebar items
the way the Permission Matrix does ("<Section> - <current name>", in sidebar
order). OFF by default for every role except admin (DEFAULT_DISABLED_MODULES);
Email stays admin-only because it writes the server's .env.
"""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_module_perm, verify_csrf
from database import get_db
from models.settings import AppSetting
from models.user import User
from templates_config import templates

router = APIRouter(prefix="/admin/extra-settings", tags=["extra_settings"], dependencies=[Depends(verify_csrf)])

TABS = [("system_colors", "System Colors", "bi-palette"), ("email", "Email", "bi-envelope"),
        ("sidebar_config", "Sidebar Config", "bi-list-ul"), ("sidebar_alignment", "Sidebar Alignment", "bi-arrow-down-up"),
        ("page_titles", "Page Title Config", "bi-layout-text-sidebar")]


def _role(u):
    return u.role.value if hasattr(u.role, "value") else str(u.role)


async def _settings_like(db, prefix):
    rows = (await db.execute(select(AppSetting).where(AppSetting.key.like(f"{prefix}%")))).scalars().all()
    return {r.key[len(prefix):]: r.value for r in rows}


async def _sidebar_items(db):
    """Permission Matrix rows (sidebar order, "Section - current name")."""
    from routers.master import perm_modules_in_sidebar_order, perm_module_display_names
    from routers.sidebar_config import SETTING_PREFIX, _SIDEBAR_DEFAULTS
    custom = await _settings_like(db, SETTING_PREFIX)
    names = perm_module_display_names()
    out = []
    for key, label in perm_modules_in_sidebar_order():
        default = _SIDEBAR_DEFAULTS.get(key, label)
        out.append({"key": key, "name": names.get(key, default), "default_label": default,
                    "current_label": custom.get(key, default), "is_custom": key in custom})
    return out


async def _page_titles(db):
    from routers.landing_pages import NAV_PAGE_TITLES, BREADCRUMB_PREFIX
    from routers.master import perm_module_display_names
    from services.sidebar_layout import effective_ranks
    titles = await _settings_like(db, "page_title_")
    bc = {k: v == "1" for k, v in (await _settings_like(db, BREADCRUMB_PREFIX)).items()}
    names, ranks = perm_module_display_names(), effective_ranks()
    big = max(ranks.values(), default=0) + 1
    rows = [{"key": k, "name": names.get(k) or nav, "nav_label": nav, "default_title": dt,
             "current_title": titles.get(k, dt), "is_custom": k in titles, "url": url,
             "breadcrumb_enabled": bc.get(k, False), "_r": (ranks.get(k, big), i)}
            for i, (k, nav, dt, url) in enumerate(NAV_PAGE_TITLES)]
    return sorted(rows, key=lambda r: r["_r"])


@router.get("", response_class=HTMLResponse)
async def extra_settings(request: Request, tab: str = "system_colors", db: AsyncSession = Depends(get_db),
                         current_user: User = Depends(require_module_perm("extra_settings"))):
    is_admin = _role(current_user) == "admin"
    tabs = [t for t in TABS if t[0] != "email" or is_admin]
    if tab not in [t[0] for t in tabs]:
        tab = tabs[0][0]
    ctx = {"request": request, "current_user": current_user, "tabs": tabs, "tab": tab,
           "success": request.query_params.get("success"), "error": request.query_params.get("error")}
    if tab == "system_colors":
        from routers.master import _system_colors_ctx
        ctx.update(_system_colors_ctx())
    elif tab == "email":
        from routers.master import _email_ctx
        ctx.update(await _email_ctx(db))
    elif tab == "sidebar_config":
        ctx["items"] = await _sidebar_items(db)
    elif tab == "page_titles":
        ctx["modules"] = await _page_titles(db)
    elif tab == "sidebar_alignment":
        from services.sidebar_layout import groups
        ctx["sb_groups"] = groups()
    return templates.TemplateResponse("admin/extra_settings.html", ctx)


@router.post("/sidebar-alignment/save")
async def save_alignment(request: Request, db: AsyncSession = Depends(get_db),
                         current_user: User = Depends(require_module_perm("extra_settings", "edit"))):
    """Save the order of one section's (or accordion's) items: JSON body
    {"group": "<id>", "order": [ids...]} — or {"all": {group: [ids]}}."""
    from services import sidebar_layout as sl
    from services.audit_engine import audit
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)
    data = sl.current_alignment()
    if isinstance(body.get("all"), dict):
        data = body["all"]
    elif body.get("group"):
        data[str(body["group"])] = [str(x) for x in (body.get("order") or [])]
    clean = sl.set_alignment(data)
    row = (await db.execute(select(AppSetting).where(AppSetting.key == sl.SETTING_KEY))).scalar_one_or_none()
    old = row.value if row else None
    if row:
        row.value, row.updated_by = json.dumps(clean), current_user.username
    else:
        db.add(AppSetting(key=sl.SETTING_KEY, value=json.dumps(clean), description="Sidebar Alignment (Extra Settings)",
                          updated_by=current_user.username))
    await audit(db, "SIDEBAR_ALIGNMENT_SAVED", user=current_user, table_name="app_settings", record_id=sl.SETTING_KEY,
                old_value=old, new_value={"group": body.get("group") or "all"}, request=request)
    await db.commit()
    return JSONResponse({"ok": True, "saved": clean})


@router.post("/sidebar-alignment/reset")
async def reset_alignment(request: Request, db: AsyncSession = Depends(get_db),
                          current_user: User = Depends(require_module_perm("extra_settings", "edit"))):
    """Put one section (form/JSON "group") — or every section — back to the default order."""
    from services import sidebar_layout as sl
    from services.audit_engine import audit
    try:
        body = await request.json()
    except ValueError:
        body = {}
    data = sl.current_alignment()
    g = body.get("group")
    if g:
        data.pop(str(g), None)
    else:
        data = {}
    clean = sl.set_alignment(data)
    row = (await db.execute(select(AppSetting).where(AppSetting.key == sl.SETTING_KEY))).scalar_one_or_none()
    if row:
        old = row.value
        row.value, row.updated_by = json.dumps(clean), current_user.username
        await audit(db, "SIDEBAR_ALIGNMENT_RESET", user=current_user, table_name="app_settings", record_id=sl.SETTING_KEY,
                    old_value=old, new_value={"group": g or "all"}, request=request)
        await db.commit()
    return JSONResponse({"ok": True, "saved": clean})
