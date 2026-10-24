import os
from fastapi.templating import Jinja2Templates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ── Static asset cache-busting ────────────────────────────────────────────────
# Browsers cache /static/* aggressively since StaticFiles sends no explicit
# Cache-Control header (only Last-Modified/ETag), so CSS/JS edits can silently
# not show up for users until a hard refresh. Append this as a ?v= query string
# on asset URLs so every restart forces a fresh fetch.
# Version off the newest mtime across the app-wide assets we cache-bust, so
# editing any one of them (not just app.css) forces browsers to refetch.
_VERSIONED_ASSETS = [
    os.path.join(BASE_DIR, "static", "css", "app.css"),
    os.path.join(BASE_DIR, "static", "js", "form-autosave.js"),
    # global-table.js is tagged with this same ?v= stamp in base.html but was
    # missing from this list (2026-09-03 bug) — editing it alone never moved
    # ASSET_VERSION, so a browser that had already cached an older copy under
    # the unchanged ?v=N URL kept running it indefinitely, even across server
    # restarts. Confirmed live: a frozen-column header that scrolled away
    # like a normal column in one browser but stayed pinned in another,
    # despite both hitting the same deployed HEAD — a stale-cache symptom,
    # not a real position:sticky engine gap between browsers.
    os.path.join(BASE_DIR, "static", "js", "global-table.js"),
    # Same stale-cache gap as global-table.js above, found the same way: the
    # dropdown-positioning fix landed in this file and deployed cleanly, but
    # templates/workid_status/list.html's <script src> had no ?v= stamp, so
    # browsers that had already loaded the old copy kept running it forever.
    os.path.join(BASE_DIR, "static", "js", "multiselect-filter.js"),
    # Same gap, found proactively rather than reported: already tagged with
    # ?v={{ ASSET_VERSION }} in templates/cosmetic/received.html but missing
    # here since it was added — fixed before it could bite the same way, now
    # that templates/lots/trc_production.html adopts it too.
    os.path.join(BASE_DIR, "static", "js", "tag-scan-autocheck.js"),
    # Top-bar Search / Scan box (2026-10-24), loaded on every page by base.html.
    os.path.join(BASE_DIR, "static", "js", "topbar-search.js"),
]
try:
    ASSET_VERSION = str(int(max(
        os.path.getmtime(p) for p in _VERSIONED_ASSETS if os.path.exists(p)
    )))
except (OSError, ValueError):
    ASSET_VERSION = "1"
templates.env.globals["ASSET_VERSION"] = ASSET_VERSION


# ── Datetime display filters ──────────────────────────────────────────────────
# Timestamps are now stored in the configured app timezone (default: IST/Asia/Kolkata).
# These filters simply format the stored value directly — no UTC conversion needed.
# Old records stored as UTC will display UTC time (slightly off) — acceptable
# for this internal system; new records going forward will show IST correctly.

def ist_format(dt, fmt="%d-%m-%Y %H:%M"):
    """Jinja2 filter: format a datetime for display (stored in app timezone)."""
    if dt is None:
        return "—"
    return dt.strftime(fmt)


def ist_date(dt):
    return ist_format(dt, "%d-%m-%Y")


def ist_time(dt):
    return ist_format(dt, "%H:%M")


def ist_datetime(dt):
    return ist_format(dt, "%d-%m-%Y %H:%M")


# Register Jinja2 filters
templates.env.filters["ist"]          = ist_format    # {{ dt | ist }}
templates.env.filters["ist_date"]     = ist_date      # {{ dt | ist_date }}
templates.env.filters["ist_time"]     = ist_time      # {{ dt | ist_time }}
templates.env.filters["ist_datetime"] = ist_datetime  # {{ dt | ist_datetime }}


def inr(value, decimals=0):
    """Jinja2 filter: Indian digit grouping (last 3 digits, then pairs) —
    e.g. 166807419 -> "16,68,07,419" instead of the Western "166,807,419"."""
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0
    negative = value < 0
    value = abs(value)
    if decimals:
        int_part, _, dec_part = f"{value:.{decimals}f}".partition(".")
    else:
        int_part, dec_part = str(int(round(value))), ""
    if len(int_part) <= 3:
        grouped = int_part
    else:
        last3, rest = int_part[-3:], int_part[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ",".join(parts) + "," + last3
    result = grouped + (f".{dec_part}" if dec_part else "")
    return ("-" + result) if negative else result


templates.env.filters["inr"] = inr  # {{ amount | inr }} -> Indian lakh/crore grouping

# ── Permission helpers — usable in any template ──
#   has_perm(role, module, action)  → single-module check (matrix-driven)
#   any_perm(role, *modules)        → True if ANY listed module is enabled
#                                      (used to show a nav SECTION header only when
#                                       at least one of its modules is visible).
from models.role_permissions import (
    has_perm as _has_perm,
    has_explicit_perm as _has_explicit_perm_fn,
    is_narrowly_scoped_to as _is_narrowly_scoped_to_fn,
    get_cached_sidebar_label as _sidebar_label,
    get_cached_page_title as _get_cached_page_title,
    can_view_pricing as _can_view_pricing_by_role,
)


def _has_explicit_perm(role, module):
    """Template helper mirroring models.role_permissions.has_explicit_perm:
    True only when the matrix holds an explicit ENABLE row for (role, module),
    with admin always True. Used by the Admin Settings accordion so admin can
    grant a sub_admin individual admin modules via the Sub Admin Role tab."""
    role_val = getattr(role, "value", None) or str(role)
    return _has_explicit_perm_fn(role_val, module)


def _can_view_pricing(current_user):
    """Template helper: {% if can_view_pricing(current_user) %}. Accepts the
    current_user object (not a bare role string) since that's what every
    template already has in context — resolves its .role.value internally."""
    if current_user is None:
        return True
    role_val = getattr(getattr(current_user, "role", None), "value", None) or str(getattr(current_user, "role", ""))
    return _can_view_pricing_by_role(role_val)


def _can_set_price(current_user):
    """Template helper: {% if can_set_price(current_user) %} — the Pricing
    Visibility tab's Set Price column (Ready to Sale's Set price buttons)."""
    if current_user is None:
        return True
    from models.role_permissions import can_set_price as _csp
    role_val = getattr(getattr(current_user, "role", None), "value", None) or str(getattr(current_user, "role", ""))
    return _csp(role_val)


def _any_perm(role, *modules):
    return any(_has_perm(role, m, "enable") for m in modules)


def _narrowly_scoped_to(role, *modules):
    """Template helper mirroring models.role_permissions.is_narrowly_scoped_to
    — True if `role` was deliberately scoped down to at least one of
    `modules` (an explicit matrix row), OR has zero matrix configuration
    anywhere (unconfigured-role-defaults-to-narrow convention). False for a
    role that's configured elsewhere in the matrix but just never had these
    particular modules touched — e.g. distinguishing a genuinely narrow
    single-stage custom role from a broad one nobody scoped down (mirrors
    routers/cosmetic.py _is_cosmetic_stage_role)."""
    role_val = getattr(role, "value", None) or str(role)
    return _is_narrowly_scoped_to_fn(role_val, *modules)


from models.user import UserRole as _UserRole

_BUILTIN_ROLE_VALUES = {r.value for r in _UserRole}


def _role_allowed(role, *allowed_values):
    """Mirrors auth.dependencies.require_roles: matches an exact allowed value,
    or lets any custom (admin-created, non-builtin) role through — since those
    are governed by the Module Permission matrix, not this allow-list — unless
    the allow-list is admin-only. Use this instead of a raw
    `current_user.role.value in [...]` check in templates so custom roles
    (e.g. 'trc_manager') see the same buttons their matching backend
    `require_roles(...)` dependency already lets them use."""
    role_val = getattr(role, "value", None) or str(role)
    if role_val in allowed_values:
        return True
    if role_val not in _BUILTIN_ROLE_VALUES and set(allowed_values) != {"admin"}:
        return True
    return False


templates.env.globals["role_allowed"] = _role_allowed


def _resolve_module_key(path: str):
    """Match a request path against NAV_PAGE_TITLES by longest url prefix,
    returning the module_key (or None if no page in the nav list covers this
    path). Shared by _resolve_page_title and _breadcrumb_enabled below."""
    from routers.landing_pages import NAV_PAGE_TITLES  # deferred: avoids a
    # circular import, since routers/landing_pages.py imports `templates` from
    # this module at its own top level.
    best_key, best_len = None, -1
    for module_key, _nav_label, _default_title, url in NAV_PAGE_TITLES:
        u = url.rstrip("/") or "/"
        # Trailing-slash-tolerant, segment-boundary match: nav url "/qa/" must
        # cover both "/qa" and "/qa/uat" (bare "/qa" previously matched nothing,
        # so the permission matrix could never unlock it).
        if u == "/":
            matched = path == "/"
        else:
            matched = path == u or path.startswith(u + "/")
        if matched and len(u) > best_len:
            best_key, best_len = module_key, len(u)
    return best_key


def _resolve_page_title(path: str):
    """Admin-customised page title (Landing Pages tool) for the given request
    path. Returns None if no custom title is set, so callers fall back to the
    template's own {% block page_title %} default."""
    module_key = _resolve_module_key(path)
    if module_key is None:
        return None
    return _get_cached_page_title(module_key)


def _module_hidden(module_key: str) -> bool:
    """Master Data's Global Visibility toggle — True hides a module's sidebar
    entry from EVERY user, admin included (a separate layer from has_perm()/
    has_explicit_perm(), which always let admin through and are never
    consulted here). Takes a module_key directly, unlike breadcrumb_enabled
    (which resolves a request path), since base.html calls this with the
    same literal keys it already passes to has_perm()."""
    from models.role_permissions import get_cached_module_hidden
    return get_cached_module_hidden(module_key)


def _breadcrumb_enabled(path: str) -> bool:
    """Whether the breadcrumb trail should render for this request path.
    Default off (2026-10-15): hidden for any path not in NAV_PAGE_TITLES and
    for any page without an explicit "on" in Page Title Config."""
    from models.role_permissions import get_cached_breadcrumb_enabled
    module_key = _resolve_module_key(path)
    if module_key is None:
        return False
    return get_cached_breadcrumb_enabled(module_key)


templates.env.globals["has_perm"] = _has_perm
templates.env.globals["has_explicit_perm"] = _has_explicit_perm
templates.env.globals["any_perm"] = _any_perm
templates.env.globals["narrowly_scoped_to"] = _narrowly_scoped_to
templates.env.globals["can_view_pricing"] = _can_view_pricing
templates.env.globals["can_set_price"] = _can_set_price
templates.env.globals["breadcrumb_enabled"] = _breadcrumb_enabled
templates.env.globals["module_hidden"] = _module_hidden
templates.env.globals["sidebar_label"] = _sidebar_label
templates.env.globals["resolve_page_title"] = _resolve_page_title

from models.qa_uat import get_cached_app_version as _get_cached_app_version
templates.env.globals["app_version"] = _get_cached_app_version

_ROLE_DISPLAY = {
    "admin": "Admin",
    "inventory_manager": "Inventory Manager",
    "iqc_inspector": "IQC Handler",
    "l1_engineer": "L1/L2 Engineer",
    "l2_engineer": "L2 Engineer",
    "l3_engineer": "L3/L4 Engineer",
    "qc_inspector": "QC Handler",
    "sales": "Sourcing Sales",
    "spare_parts_manager": "Parts Manager",
    "telecaller": "Telecaller Sales",
    "sales_manager": "Sales Manager",
}


def _prettify_role(val: str) -> str:
    """'trc_manager' → 'TRC Manager': words ≤3 chars uppercased, longer words title-cased."""
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in val.replace("_", " ").split())


def _role_display(role):
    val = str(getattr(role, "value", role))
    return _ROLE_DISPLAY.get(val, _prettify_role(val))


templates.env.globals["role_display"] = _role_display
templates.env.globals["ROLE_DISPLAY_MAP"] = _ROLE_DISPLAY

# ── Master Data dropdown options — usable in any template ──
#   master_options('category_key') → list of active option values, cache-backed
#   (see utils/master_data.py). Warmed at startup, refreshed on admin edits.
from utils.master_data import master_options as _master_options

templates.env.globals["master_options"] = _master_options

# grade_options() → [(enum value, admin-managed label)] for every Grade dropdown.
# Separate from master_options('grade') because that category holds display
# labels ("Grade A - Like New") which cannot be posted to the enum column.
from utils.grades import grade_options as _grade_options

templates.env.globals["grade_options"] = _grade_options

# flag_badge(device) → red FLAG badge (with tooltip) for a flagged device's
# Tag Number; renders nothing when the device isn't flagged. utils/flag.py.
from markupsafe import Markup as _Markup
from utils.flag import flag_badge_html as _flag_badge_html


def _flag_badge(device):
    return _Markup(_flag_badge_html(device)) if device is not None else ""


templates.env.globals["flag_badge"] = _flag_badge


# parts_badge(parts_badges, device_id) → the parts-changed icon after the Tag
# Number on pipeline pages. `parts_badges` is the {device_id: html} map a
# router builds with services/parts_changed.parts_badge_map(); a page whose
# router doesn't pass one simply renders nothing.
def _parts_badge(badges, device_id):
    if not badges or device_id is None:
        return ""
    try:
        html = badges.get(str(device_id))
    except AttributeError:
        return ""
    return _Markup(html) if html else ""


templates.env.globals["parts_badge"] = _parts_badge
