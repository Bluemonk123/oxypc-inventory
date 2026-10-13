"""Role-based module permission matrix.

Each row stores what a named role can do for a specific module:
  can_enable  — the module is visible / accessible to this role
  can_add     — the role can create new records in this module
  can_edit    — the role can edit existing records
  can_upload  — the role can use bulk-upload / CSV import features

Admin role always bypasses all checks (full access).
Custom roles can be created by admin and assigned here.
"""
import uuid
from utils.timezone import app_now
from sqlalchemy import Column, String, Boolean, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from database import Base


class RoleModulePermission(Base):
    __tablename__ = "role_module_permissions"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role_name  = Column(String(60),  nullable=False, index=True)  # e.g. "sales", "iqc_inspector"
    module     = Column(String(60),  nullable=False)              # e.g. "lots", "repair_l1"
    can_enable = Column(Boolean, default=True)   # module visible / accessible
    can_add    = Column(Boolean, default=False)  # create records
    can_edit   = Column(Boolean, default=False)  # edit records
    can_upload = Column(Boolean, default=False)  # bulk upload / CSV import
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)
    updated_by = Column(String(50), nullable=True)

    __table_args__ = (
        UniqueConstraint("role_name", "module", name="uq_role_module_perm"),
    )


class CustomRole(Base):
    """Admin-defined role names beyond the built-in UserRole enum."""
    __tablename__ = "custom_roles"

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role_name    = Column(String(60), unique=True, nullable=False, index=True)
    display_name = Column(String(100), nullable=False)
    created_by   = Column(String(50), nullable=True)
    created_at   = Column(DateTime, default=app_now)


class RoleAdditionalPermission(Base):
    """Cross-cutting, non-module-specific permissions per role — apply app-wide
    rather than to one module (unlike RoleModulePermission above).

      can_upload       — File Upload (any bulk-upload/import control app-wide)
      can_download     — File Download (any file/attachment download control)
      can_export       — File Export (CSV/Excel export buttons)
      can_print        — Print Page
      can_add_new_data — Add New Data (legacy; superseded by the can_add_*
                          fields below, kept — unused by the UI — so nothing
                          that reads the column directly breaks)
      can_add_lot      — Add Lot (POST /lots/new)
      can_add_iqc      — Add IQC (POST /iqc/new)
      can_add_grn      — Add GRN (POST /grn/create-manual)
      can_edit_devices — Devices Edit (POST /devices/{barcode}/edit, and
                          jointly with can_edit_iqc gates the Customise bulk
                          edit at POST /iqc/bulk-apply-grade-type)
      can_edit_iqc     — IQC Edit (jointly with can_edit_devices gates the
                          Customise bulk edit — no separate IQC-record-edit
                          form exists today)
      can_edit_lot     — Lot Edit (POST /lots/{lot_id}/edit)
      can_edit_grn     — GRN Edit (POST /grn/{grn_id}/edit)

    Admin role always bypasses all checks (full access), same as the module matrix.
    Additive columns (can_add_lot onward); auto-provisioned by db_validator.
    """
    __tablename__ = "role_additional_permissions"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role_name        = Column(String(60), unique=True, nullable=False, index=True)
    can_upload       = Column(Boolean, default=True)
    can_download     = Column(Boolean, default=True)
    can_export       = Column(Boolean, default=True)
    can_print        = Column(Boolean, default=True)
    can_add_new_data = Column(Boolean, default=True)
    can_view_pricing = Column(Boolean, default=True)  # Pricing Visibility tab (Batch 9)
    # Pricing Visibility tab's "Set Price" column: may this role use the Set
    # price / Bulk Set price buttons on Ready to Sale? Default True keeps
    # existing roles' access until an admin unticks it.
    can_set_price    = Column(Boolean, default=True)
    can_add_lot      = Column(Boolean, default=True)
    can_add_iqc      = Column(Boolean, default=True)
    can_add_grn      = Column(Boolean, default=True)
    can_edit_devices = Column(Boolean, default=True)
    can_edit_iqc     = Column(Boolean, default=True)
    can_edit_lot     = Column(Boolean, default=True)
    can_edit_grn     = Column(Boolean, default=True)
    updated_at       = Column(DateTime, default=app_now, onupdate=app_now)
    updated_by       = Column(String(50), nullable=True)


# ── In-memory permissions cache ───────────────────────────────────────────────
# Populated on startup and refreshed whenever admin saves the matrix.
# Structure: {role_name: {module: {"enable": bool, "add": bool, "edit": bool, "upload": bool}}}
_PERM_CACHE: dict = {}

# Structure: {role_name: {"upload": bool, "download": bool, "export": bool, "print": bool, "add_new_data": bool}}
_ADDITIONAL_PERM_CACHE: dict = {}

# ── Sidebar Config: custom labels for nav items / Permission Matrix "Module"
#    column, keyed by the same module key used everywhere else (PERM_MODULES,
#    NAV_PAGE_TITLES, has_perm()). Persisted as AppSetting rows
#    (key=f"sidebar_label_{module_key}"), same convention as Landing Pages'
#    page_title_{module_key}. Structure: {module_key: custom_label}
_SIDEBAR_LABEL_CACHE: dict = {}


def get_cached_sidebar_label(module_key: str, default: str) -> str:
    return _SIDEBAR_LABEL_CACHE.get(module_key) or default


def set_cached_sidebar_label(module_key: str, label: str) -> None:
    _SIDEBAR_LABEL_CACHE[module_key] = label


# ── Landing Pages: custom page titles (AppSetting key=f"page_title_{module_key}").
#    Structure: {module_key: custom_title}. Read by resolve_page_title() in
#    templates_config.py, matched against NAV_PAGE_TITLES' url column.
_PAGE_TITLE_CACHE: dict = {}


def get_cached_page_title(module_key: str) -> str | None:
    return _PAGE_TITLE_CACHE.get(module_key) or None


def set_cached_page_title(module_key: str, title: str) -> None:
    _PAGE_TITLE_CACHE[module_key] = title


# ── Landing Pages: per-page breadcrumb toggle (AppSetting key=f"breadcrumb_{module_key}").
#    Structure: {module_key: bool}. Missing key => breadcrumb shown (default on,
#    matches pre-existing behavior before this toggle existed).
_BREADCRUMB_CACHE: dict = {}


def get_cached_breadcrumb_enabled(module_key: str) -> bool:
    return _BREADCRUMB_CACHE.get(module_key, True)


def set_cached_breadcrumb_enabled(module_key: str, enabled: bool) -> None:
    _BREADCRUMB_CACHE[module_key] = enabled


# ── Master Data "Global Visibility" tab: hide a module from the sidebar for
#    EVERY user, admin included — a separate layer from has_perm()/
#    has_explicit_perm() (both of which always let admin through), never
#    consulted by them. Persisted as AppSetting rows
#    (key=f"global_hidden_{module_key}"). Structure: {module_key: bool}.
#    Missing key => not hidden (default visible, matches pre-existing
#    behavior before this toggle existed).
_MODULE_HIDDEN_CACHE: dict = {}


def get_cached_module_hidden(module_key: str) -> bool:
    return _MODULE_HIDDEN_CACHE.get(module_key, False)


def set_cached_module_hidden(module_key: str, hidden: bool) -> None:
    _MODULE_HIDDEN_CACHE[module_key] = hidden


def get_cached_additional_perms(role_name: str) -> dict:
    return _ADDITIONAL_PERM_CACHE.get(role_name, {})


def set_cached_additional_perms(role_name: str, perms: dict) -> None:
    _ADDITIONAL_PERM_CACHE[role_name] = perms


def has_additional_perm(role_name: str, action: str) -> bool:
    """Check if role_name has the given cross-cutting action allowed.
    Admin always returns True. Unknown roles default to True (permissive) —
    same "opt-in restriction" behavior as has_perm() below."""
    if role_name == "admin":
        return True
    perms = _ADDITIONAL_PERM_CACHE.get(role_name)
    if not perms:
        return True   # no row configured → allow everything
    return bool(perms.get(action, True))


def can_view_pricing(role_name: str) -> bool:
    """Pricing Visibility (Batch 9): whether role_name may see pricing columns/
    fields anywhere in the app. Admin always True; unconfigured roles default
    to True (permissive), same convention as has_perm()/has_additional_perm()."""
    return has_additional_perm(role_name, "view_pricing")


def can_set_price(role_name: str) -> bool:
    """Pricing Visibility tab's Set Price column: whether role_name may set
    Min/Max Selling Price on Ready to Sale (Set price, Bulk Set price, As-Is
    Lot Set price). Admin always True; unconfigured roles default to True."""
    return has_additional_perm(role_name, "set_price")


def get_cached_perms(role_name: str) -> dict:
    """Return the cached permission dict for a role. Empty dict = no restrictions loaded."""
    return _PERM_CACHE.get(role_name, {})


def set_cached_perms(role_name: str, perms: dict) -> None:
    _PERM_CACHE[role_name] = perms


def has_perm(role_name: str, module: str, action: str = "enable") -> bool:
    """Check if role_name has the given action on module.
    Admin always returns True. Unknown roles/modules default to True (permissive).

    "enable" is the MASTER SWITCH for a module: if a role has the module enabled,
    it may fully use it (view + add + edit + upload). A disabled module blocks
    everything. The finer Add/Edit/Upload bits do NOT further restrict an enabled
    module — this avoids surprising 403s where an admin enabled a module for a role
    but left the Add/Edit boxes unchecked, then the role got 403 on create/edit.
    """
    if role_name == "admin":
        return True
    role_perms = _PERM_CACHE.get(role_name)
    if not role_perms:
        return True   # no matrix configured → allow everything
    mod_perms = role_perms.get(module)
    if not mod_perms:
        return True   # module not in matrix → allow
    return bool(mod_perms.get("enable", True))


def is_narrowly_scoped_to(role_name: str, *modules: str) -> bool:
    """True if role_name was deliberately scoped down to (at least one of)
    `modules` — used to tell a genuine narrow-purpose custom role (e.g. a
    "Cosmetic Painting" role explicitly given only that one stage) apart
    from a broad role that simply never had these particular checkboxes
    touched (e.g. "trc_manager", explicitly configured across ~100 OTHER
    modules but never these).

    Three cases, in order:
    1. An explicit ENABLE row exists for at least one of `modules` — the
       clearest positive signal: someone deliberately turned this on.
    2. The role has ANY matrix configuration at all (rows for other
       modules), just none of these — a broadly-configured role that
       happens not to cover `modules`. Not narrow.
    3. The role has ZERO matrix configuration anywhere (freshly created,
       nothing ever set) — falls back to has_perm's permissive default
       (unconfigured module → enabled), matching this app's long-standing
       "an unconfigured role defaults to narrow/restricted on cosmetic
       stage pages" convention. Only reached when case 2 doesn't apply.

    Found 2026-09-24: cosmetic.py's _is_cosmetic_stage_role originally used
    has_perm() alone for the whole check, which is case 3's rule applied
    unconditionally — correct for a truly unconfigured role, wrong for one
    like trc_manager that IS configured, just not on these modules. That
    misread a broad, deliberately-configured manager role as a narrow
    single-stage worker and hid most of the Cosmetic pipeline from them.
    """
    if any(has_explicit_perm(role_name, m) for m in modules):
        return True
    if get_cached_perms(role_name):
        return False
    return any(has_perm(role_name, m, "enable") for m in modules)


def has_explicit_perm(role_name: str, module: str) -> bool:
    """True only when the matrix holds an explicit row ENABLING `module` for
    `role_name`.

    Deliberately stricter than has_perm(): an absent row means "not granted"
    here, not "allowed by default". This is used to WIDEN access past the
    built-in role allow-lists in require_roles(), so treating a missing row as a
    grant would hand every role every page and delete RBAC entirely.
    """
    if role_name == "admin":
        return True
    role_perms = _PERM_CACHE.get(role_name)
    if not role_perms:
        return False
    mod_perms = role_perms.get(module)
    return bool(mod_perms and mod_perms.get("enable"))
