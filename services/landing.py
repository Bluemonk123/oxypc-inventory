"""Default landing page per role (2026-11-15, Pankaj).

Master Data -> Role Additional Permissions -> "Default landing page": the page
a role opens on right after signing in (and on the bare site address). The
list offers only pages the role can open — module enabled for the role in
Module Permissions, and Dashboard View ticked for a page that is only a
dashboard. Stored in app_settings "landing_page_<role>" = page URL (no schema
change). Not set, or the page no longer allowed: the old rule — admin ->
Admin Dashboard, everyone else -> My Attendance.
"""
from sqlalchemy import select

from models.settings import AppSetting

PREFIX = "landing_page_"
NON_ADMIN_DEFAULT = "/attendance"
ADMIN_DEFAULT = "/dashboard"
# Pages the sidebar has that Module Page Titles doesn't list yet
EXTRA_PAGES = [
    ("activity_board", "Activity Board", "/activity-board"),
]


def _pages():
    """[(module, label, url)] — the sidebar pages, in sidebar order."""
    from routers.landing_pages import NAV_PAGE_TITLES
    seen, out = set(), []
    for mod, label, _title, url in NAV_PAGE_TITLES:
        if url not in seen:
            seen.add(url)
            out.append((mod, label, url))
    for mod, label, url in EXTRA_PAGES:
        if url not in seen:
            seen.add(url)
            out.insert(1, (mod, label, url))
    return out


def page_allowed(role, module, url) -> bool:
    from models.role_permissions import has_explicit_perm, has_perm
    from services.dash_access import DASH_PAGES
    if not has_explicit_perm(role, module):
        return False
    dash = DASH_PAGES.get(url.rstrip("/") or "/")
    if dash and dash[1] and not has_perm(role, dash[0], "dashboard"):
        return False
    return True


def default_for(role) -> str:
    return ADMIN_DEFAULT if role == "admin" else NON_ADMIN_DEFAULT


def options(role):
    """[(url, label)] the role may land on."""
    return [(url, label) for mod, label, url in _pages() if page_allowed(role, mod, url)]


async def stored(db, role):
    row = (await db.execute(select(AppSetting).where(AppSetting.key == PREFIX + role))).scalar_one_or_none()
    return (row.value or "") if row else ""


async def landing_for(db, role) -> str:
    """Where this role goes after sign-in."""
    url = await stored(db, role) if role else ""
    if url:
        for mod, _label, u in _pages():
            if u == url and page_allowed(role, mod, u):
                return url
    return default_for(role)


async def save(db, role, url, username):
    """Store the role's landing page ('' = default). Returns (old, new) or None
    when the URL is not one the role may open."""
    if url and url not in {u for u, _l in options(role)}:
        return None
    row = (await db.execute(select(AppSetting).where(AppSetting.key == PREFIX + role))).scalar_one_or_none()
    old = (row.value or "") if row else ""
    if row:
        row.value, row.updated_by = url, username
    elif url:
        db.add(AppSetting(key=PREFIX + role, value=url, updated_by=username,
                          description="Landing page after sign-in for role %s" % role))
    return old, url
