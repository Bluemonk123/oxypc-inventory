"""Per-module action permissions — server-side gate (2026-10-25).

Every write request (POST/PUT/PATCH/DELETE) is matched to its route, looked
up in auth/route_actions.py (ROUTE_ACTIONS: (method, path) -> (module, action)),
and refused unless the user's role has that tick in Master Data -> Permission
Matrix: Add, Bulk Upload, Edit, Delete or Pricing (see has_perm()).

Why one gate instead of a Depends() on each route: there are ~470 write
routes. A single table that _scan_action_coverage.py checks against the live
route list means a new route that nobody mapped is reported, instead of
silently allowing everything (which is how the previous attempt failed).

Admin always passes. Routes mapped to None (login, attendance self check-in,
exports done by POST, partner-portal/API routes, ...) are not gated here.
"""
import time
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl

from jose import JWTError, jwt
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Match

from config import SECRET_KEY
from models.role_permissions import has_any_perm

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ACTION_LABELS = {"add": "Add", "upload": "Upload", "edit": "Edit",
                 "delete": "Delete", "pricing": "Pricing", "export": "Export"}

_ROLE_CACHE: dict = {}          # username -> (role_name, expires_at)
_ROLE_TTL = 60                  # seconds; role changes apply within a minute


def _algorithm():
    try:
        from auth.dependencies import ALGORITHM
        return ALGORITHM
    except Exception:
        return "HS256"


async def _role_for(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        username = jwt.decode(token, SECRET_KEY, algorithms=[_algorithm()]).get("sub")
    except JWTError:
        return None
    if not username:
        return None
    hit = _ROLE_CACHE.get(username)
    now = time.monotonic()
    if hit and hit[1] > now:
        return hit[0]
    from database import AsyncSessionLocal
    from models.user import User
    async with AsyncSessionLocal() as db:
        role = (await db.execute(select(User.role).where(User.username == username))).scalar_one_or_none()
    if role is None:
        return None
    role_name = role.value if hasattr(role, "value") else str(role)
    _ROLE_CACHE[username] = (role_name, now + _ROLE_TTL)
    return role_name


def clear_role_cache(username: str | None = None) -> None:
    """Call after a user's role is changed so the gate sees it at once."""
    if username is None:
        _ROLE_CACHE.clear()
    else:
        _ROLE_CACHE.pop(username, None)


def _match_route(request: Request):
    for route in request.app.router.routes:
        try:
            match, child = route.matches(request.scope)
        except Exception:
            continue
        if match == Match.FULL:
            return route, child.get("path_params", {})
    return None, {}


def _wants_json(request: Request) -> bool:
    h = request.headers
    return ("application/json" in h.get("accept", "") or h.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in h.get("content-type", "") or "/api/" in request.url.path)


def _denied(request: Request, msg: str):
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": msg, "detail": msg}, status_code=403)
    ref = request.headers.get("referer")
    if ref and urlsplit(ref).netloc == request.url.netloc:
        parts = urlsplit(ref)
        q = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("error", "success")] + [("error", msg)]
        return RedirectResponse(urlunsplit(parts._replace(query=urlencode(q))), status_code=303)
    return JSONResponse({"ok": False, "error": msg, "detail": msg}, status_code=403)


async def action_permission_middleware(request: Request, call_next):
    # Writes are always looked up; GETs only when ROUTE_ACTIONS lists them
    # (export / download links, 2026-10-26) — normal page views are never gated here.
    if request.method not in WRITE_METHODS and request.method != "GET":
        return await call_next(request)
    from auth.route_actions import ROUTE_ACTIONS, resolve_module, GATED_GET_PREFIXES
    if request.method == "GET":
        # Cheap pre-check so ordinary page loads skip route matching.
        seg = request.url.path.split("/", 2)[1] if request.url.path.count("/") >= 1 else ""
        if seg not in GATED_GET_PREFIXES:
            return await call_next(request)
    route, path_params = _match_route(request)
    path = getattr(route, "path", None)
    entry = ROUTE_ACTIONS.get((request.method, path)) if path else None
    if not entry:
        return await call_next(request)
    module, action = entry
    # (A GET that isn't in ROUTE_ACTIONS was already passed through above.)
    role = await _role_for(request)
    if role is None or role == "admin":
        return await call_next(request)          # not logged in -> the route's own auth handles it
    modules = resolve_module(request.method, path, module, path_params)
    if not has_any_perm(role, modules, action):
        label = ACTION_LABELS.get(action, action)
        msg = f"Your role does not have {label} permission here. Ask an admin to tick {label} in the Permission Matrix."
        return _denied(request, msg)
    return await call_next(request)
