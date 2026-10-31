from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool
from jose import JWTError, jwt
import bcrypt as _bcrypt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import get_db
from models.user import User, UserRole
from config import SECRET_KEY, ACCESS_TOKEN_EXPIRE_MINUTES

ALGORITHM = "HS256"

ROLE_PERMISSIONS = {
    UserRole.admin: ["*"],
    UserRole.inventory_manager: ["dashboard", "lots", "stock", "iqc", "repair", "reports"],
    UserRole.iqc_inspector: ["dashboard", "iqc", "repair/move"],
    UserRole.l1_engineer: ["dashboard", "repair/l1", "repair/move"],
    UserRole.l2_engineer: ["dashboard", "repair/l2", "repair/move"],
    UserRole.l3_engineer: ["dashboard", "repair/l3", "repair/move"],
    UserRole.qc_inspector: ["dashboard", "qc", "repair/move", "reports"],
    UserRole.sales: [
        "dashboard", "sales", "returns", "reports/sales",
        "tc.call.create", "tc.call.view_own", "tc.queue.view_own",
        "tc.followup.view_own", "tc.quote.create",
    ],
    UserRole.spare_parts_manager: ["dashboard", "spare-parts", "ram-tracking"],
    UserRole.telecaller: [
        "dashboard", "tc.call.create", "tc.call.view_own",
        "tc.queue.view_own", "tc.followup.view_own", "tc.quote.create",
    ],
    UserRole.sales_manager: [
        "dashboard", "sales", "returns", "reports/sales",
        "tc.call.create", "tc.call.view_own", "tc.call.view_team",
        "tc.queue.view_own", "tc.queue.view_team",
        "tc.followup.view_own", "tc.quote.create",
        "tc.assign.create", "tc.kpi.view_team",
    ],
}


def _bcrypt_bytes(password: str) -> bytes:
    """Encode a password for bcrypt, capped at bcrypt's 72-byte limit.

    bcrypt has ALWAYS ignored everything past 72 bytes; the difference is that
    bcrypt < 5.0 truncated silently while 5.0 raises ValueError. Truncating here
    keeps the old behaviour exactly, so hashes stored before the upgrade still
    verify. Without it, hash_password() raises a 500 when someone sets a long
    passphrase, and verify_password() swallows the same error and reports a
    correct password as wrong.
    """
    return password.encode("utf-8")[:72]


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(_bcrypt_bytes(plain), hashed.encode("utf-8"))
    except Exception:
        return False


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(_bcrypt_bytes(password), _bcrypt.gensalt()).decode("utf-8")


# ── Async wrappers ────────────────────────────────────────────────────────────
# bcrypt is deliberately slow: at the stored cost factor of 12 one verify takes
# ~410 ms of solid CPU. Called straight from an `async def` endpoint that runs
# on the event loop, which means it blocks EVERY other request on that worker
# for those 410 ms — not just the person logging in. With 4 workers and a
# morning login rush, that is the whole app stalling: slow logins, requests
# timing out into 500s, unrelated pages hanging.
#
# Running it in the threadpool keeps the event loop free. Always use these in
# request handlers; the sync versions above are for scripts and tests.
async def verify_password_async(plain: str, hashed: str) -> bool:
    return await run_in_threadpool(verify_password, plain, hashed)


async def hash_password_async(password: str) -> str:
    return await run_in_threadpool(hash_password, password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_302_FOUND, headers={"Location": "/auth/login"})
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if not username:
            raise HTTPException(status_code=302, headers={"Location": "/auth/login"})
    except JWTError:
        raise HTTPException(status_code=302, headers={"Location": "/auth/login"})

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not user.status:
        raise HTTPException(status_code=302, headers={"Location": "/auth/login"})
    # Who is acting, for automatic log rows written at flush time
    # (services/repair_location.py — repair stages -> TRC Floor).
    try:
        from services.repair_location import current_user_id, current_user_name
        current_user_id.set(user.id)
        current_user_name.set(user.full_name or user.username)
    except Exception:
        pass
    return user


# Path segments whose router serves pages that no nav URL prefixes — e.g. the
# Sales router answers /sales/new but its only nav entry is /sales/ready, so
# prefix matching alone would miss it. Each entry lists exactly which matrix
# modules cover that segment.
#
# "admin" is deliberately absent. /admin/audit-log, /admin/sidebar-config and
# /admin/landing-pages are matrix modules, but /admin/master (User Management
# and the Permission Matrix itself) is not — a segment-wide alias there would
# let anyone granted the audit log grant themselves everything else.
_SEGMENT_MODULE_ALIASES = {
    "sales":      ("sales",),
    "cosmetic":   ("cosmetic", "cosmetic_finalqc"),
    "crm":        ("crm_dashboard", "crm_contacts", "crm_sourcing", "crm_sales_opp",
                   "crm_price_matrix", "crm_purchase_orders", "crm_analytics",
                   "crm_assign_leads"),
    "reports":    ("reports", "report_sales", "report_stage", "report_bizpl",
                   "report_aging", "report_overdue", "report_receivables"),
    "locations":  ("locations", "location_gaps", "location_audit", "location_master"),
    "accounts":   ("finance", "finance_supplier", "finance_customer"),
    "repair":     ("repair_l1", "repair_l2", "repair_l3"),
    "spare-parts": ("spare_parts", "spare_parts_purchase", "parts_consumption"),
    # Routers whose URLs no nav entry prefixes — without these, a module ticked
    # ON in the Permission Matrix still 403'd on the module's action endpoints.
    "part-requests":     ("spare_parts", "parts_consumption"),
    "part-sourcing":     ("spare_parts", "spare_parts_purchase"),
    "parts-grn":         ("spare_parts", "spare_parts_purchase"),
    "procure-dashboard": ("spare_parts_purchase", "spare_parts"),
    "buckets":           ("production_manager",),
    "api":               ("production_manager",),   # /api/bucket-engineers (assign modal)
    "scrap":             ("scrap_products",),
    "dealer-quotations": ("sales", "crm_sales_opp"),
    "bulk-upload":       ("iqc", "devices"),
}


def _matrix_grants_path(role_name: str, path: str) -> bool:
    """Whether the admin-configured Module Permission matrix explicitly enables
    the module that `path` belongs to, for `role_name`.

    The matrix is the single place an admin grants access to a module, and the
    left nav already honours it. Without this check the two disagree: a role
    could have a module ticked ON (so the nav item renders) and still be turned
    away with 403 by the built-in allow-list baked into each router. This closes
    that gap.

    Two ways a path is matched, in order:
      1. Longest nav-URL prefix — /iqc/new -> "iqc", /grn/post-iqc -> "grn_post_iqc".
      2. _SEGMENT_MODULE_ALIASES, for routers whose pages no nav URL prefixes.
    Only EXPLICIT grants count (see has_explicit_perm), so a role with no matrix
    row configured gains nothing here.
    """
    from models.role_permissions import has_explicit_perm
    from templates_config import _resolve_module_key

    module_key = _resolve_module_key(path)
    if module_key and has_explicit_perm(role_name, module_key):
        return True

    seg = path.strip("/").split("/", 1)[0]
    return any(has_explicit_perm(role_name, m)
               for m in _SEGMENT_MODULE_ALIASES.get(seg, ()))


def require_roles(*roles: UserRole):
    async def checker(request: Request, current_user: User = Depends(get_current_user)):
        role = current_user.role
        if role == UserRole.admin or role in roles:
            return current_user
        role_val = getattr(role, "value", None) or str(role)
        builtin = {r.value for r in UserRole}
        # Custom (admin-created) roles are NOT part of the UserRole enum; they are
        # governed by the Module Permission matrix (left-nav visibility + per-action
        # require_module_perm), not these built-in role allow-lists. Let a custom
        # role through any NON-admin-only gate so a module enabled for it in the
        # matrix actually works. Admin-only gates — require_roles(UserRole.admin)
        # alone — still block custom roles unless the matrix grants them below.
        if role_val not in builtin and set(roles) != {UserRole.admin}:
            return current_user
        # Built-in roles reach here whenever the router's hard-coded allow-list
        # omits them. Defer to the matrix: an explicit grant wins, including on
        # admin-only gates, since several such modules (Stage Control, Landing
        # Pages, System Audit Log …) are documented as "admin-only by default;
        # grantable via matrix".
        if _matrix_grants_path(role_val, request.url.path):
            return current_user
        raise HTTPException(status_code=403, detail="Access denied")
    return checker


def get_nav_permissions(role: UserRole) -> list:
    perms = ROLE_PERMISSIONS.get(role, [])
    if "*" in perms:
        return ["*"]
    return perms


def require_module_perm(module: str, action: str = "enable"):
    """Dependency factory enforcing the admin-configured Module Permission matrix.

    Usage on a route:
        from auth.dependencies import require_module_perm
        @router.post("/lots/add", dependencies=[Depends(require_module_perm("lots", "add"))])

    Behaviour:
      - admin always passes
      - if no matrix row is configured for the role/module → passes (permissive default)
      - otherwise the specific action bit (enable/add/edit/upload) must be granted,
        else 403.
    """
    from models.role_permissions import has_perm

    async def checker(current_user: User = Depends(get_current_user)) -> User:
        role_name = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
        if not has_perm(role_name, module, action):
            raise HTTPException(
                status_code=403,
                detail=f"Your role ({role_name}) does not have '{action}' permission for the {module} module.",
            )
        return current_user

    return checker


def require_any_module_perm(*modules: str, action: str = "enable"):
    """Like require_module_perm, but passes if ANY of the modules grants it.

    Needed where one endpoint serves several pages: the Customise bulk-edit is
    posted from both All Inventory (/devices) and Product IQC (/iqc), so
    demanding the 'iqc' bit alone locked out an Inventory Manager who legitimately
    has edit rights on the page they were actually using.
    """
    from models.role_permissions import has_perm

    async def checker(current_user: User = Depends(get_current_user)) -> User:
        role_name = (current_user.role.value if hasattr(current_user.role, "value")
                     else str(current_user.role))
        if any(has_perm(role_name, m, action) for m in modules):
            return current_user
        raise HTTPException(
            status_code=403,
            detail=(f"Your role ({role_name}) does not have '{action}' permission "
                    f"for any of: {', '.join(modules)}."),
        )

    return checker


def require_additional_perm(action: str):
    """Dependency factory enforcing one cross-cutting Role Additional
    Permission flag (RoleAdditionalPermission — Admin > Master Data > Role
    Additional Permissions tab), e.g. "edit_devices", "add_lot".

    Unlike require_module_perm (which only ever checks a module's overall
    "enable" bit — see has_perm()'s docstring — the finer per-action columns
    there are display-only), this checks the specific action column directly,
    so these ARE real, enforced gates: admin always passes; no matrix row
    configured for the role → passes (permissive default, matching every
    other permission check in this app).
    """
    from models.role_permissions import has_additional_perm

    async def checker(current_user: User = Depends(get_current_user)) -> User:
        role_name = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
        if not has_additional_perm(role_name, action):
            raise HTTPException(
                status_code=403,
                detail=f"Your role ({role_name}) does not have '{action}' permission.",
            )
        return current_user

    return checker


def require_any_additional_perm(*actions: str):
    """Like require_additional_perm, but passes if ANY of the actions grants
    it — the Customise bulk-edit endpoint is reachable from both the Devices
    and IQC pages, so it's gated on edit_devices OR edit_iqc rather than
    demanding both."""
    from models.role_permissions import has_additional_perm

    async def checker(current_user: User = Depends(get_current_user)) -> User:
        role_name = (current_user.role.value if hasattr(current_user.role, "value")
                     else str(current_user.role))
        if any(has_additional_perm(role_name, a) for a in actions):
            return current_user
        raise HTTPException(
            status_code=403,
            detail=(f"Your role ({role_name}) does not have permission "
                    f"for any of: {', '.join(actions)}."),
        )

    return checker


async def verify_csrf(request: Request) -> None:
    """Dependency: validate CSRF double-submit cookie for mutating requests.

    Usage in POST routes:
        from auth.dependencies import verify_csrf
        from fastapi import Depends

        @router.post("/some-path")
        async def handler(_csrf=Depends(verify_csrf), ...):
            ...

    Validates that the 'csrf_token' form field matches the 'csrf_token' cookie.
    Skips validation for GET/HEAD/OPTIONS requests.
    """
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return
    cookie_token = request.cookies.get("csrf_token", "")
    if not cookie_token:
        # Missing csrf_token cookie almost always means the session itself has
        # expired (both cookies share the same lifetime and get cleared together
        # by the browser) — not a genuine CSRF attack. Redirect to login instead
        # of surfacing a raw 403 "Access Denied" page for what is really just an
        # inactive-page timeout.
        raise HTTPException(status_code=302, headers={"Location": "/auth/login"})
    # Form field must match cookie — read form data (FastAPI caches it per request)
    try:
        form = await request.form()
        form_token = form.get("csrf_token", "")
    except Exception:
        form_token = ""
    if not form_token:
        form_token = request.headers.get("X-CSRF-Token", "")
    if not form_token or form_token != cookie_token:
        # A stale form_token (page open since before the session was last
        # extended/rotated) is also almost always a timeout, not an attack —
        # send the user back to login to re-authenticate cleanly rather than
        # showing a scary CSRF error.
        raise HTTPException(status_code=302, headers={"Location": "/auth/login"})
