"""System Colors (2026-11-08, Pankaj) — Admin → Master Data → System Colors.

The app's main colours as named tokens, each with a Light and a Dark value.
Defaults are the Design System v2.6 colours (chapter 02). An admin can change
any value; the change applies to every user as soon as it is saved, and
"Reset to Design System" brings the defaults back.

Storage: one AppSetting row, key ``system_colors``, value = JSON
``{"<token>": {"light": "#rrggbb", "dark": "#rrggbb"}, ...}`` holding only the
values that differ from the defaults (no schema change). The admin's default
mode for users is AppSetting ``theme_default_mode`` ("light" / "dark"); each
user can flip Light / Dark for themselves (cookie ``oxy_theme``).

base.html renders ``system_colors_css()`` right after app.css: CSS variables
for both modes, plus Bootstrap overrides for any semantic colour that was
changed. ``theme_mode(request)`` gives the mode for ``<html data-bs-theme>``.
"""
from __future__ import annotations

import json
import re

SETTING_KEY = "system_colors"
MODE_KEY = "theme_default_mode"
COOKIE = "oxy_theme"

# (key, label, group, css variables, light default, dark default, where it is used)
TOKENS = [
    # Brand and neutrals
    ("navy", "Bluemonk Navy", "brand", ["--brand-blue"], "#0e3a7a", "#2b5a9e",
     "Table header rows, active main tab, filter-bar rule, sidebar footer, card titles"),
    ("sidebar_active", "Bluemonk Blue", "brand", ["--sidebar-active"], "#0377c2", "#1f8fdc",
     "Active sidebar link"),
    ("sky", "Sky Blue", "brand", ["--oxy-sky"], "#d9ebf6", "#1f2c3b",
     "Global Card header background"),
    ("light", "Light Colour", "brand", ["--oxy-light"], "#f0f4f8", "#20252b",
     "Card header under tabs, light panels"),
    ("page_bg", "Page background", "brand", ["--oxy-page-bg"], "#f0f4f8", "#121417",
     "Behind all content"),
    ("surface", "Card and field background", "brand", ["--oxy-surface"], "#ffffff", "#1b1f24",
     "Cards, table rows, filter fields, topbar"),
    ("text", "Dark Colour (text)", "brand", ["--oxy-text-main"], "#212529", "#e4e7eb",
     "Body text, table cells, form fields"),
    ("border", "Borders", "brand", ["--oxy-border"], "#ced4da", "#3a414a",
     "Field and card borders"),
    ("disabled", "Disabled", "brand", ["--oxy-disabled"], "#d8d8d8", "#3b4047",
     "Every disabled button"),
    ("sidebar_bg", "Sidebar background", "sidebar", ["--sidebar-bg"], "#212529", "#0f1215",
     "Sidebar menu"),
    ("sidebar_text", "Sidebar text", "sidebar", ["--sidebar-text"], "#f0f4f8", "#e4e7eb",
     "Sidebar links"),
    ("sidebar_header", "Sidebar header", "sidebar", ["--oxy-sidebar-header"], "#129042", "#0f6e34",
     "Logo bar at the top of the sidebar"),
    ("oxy_leaf", "OxyPC Leaf", "oxypc", ["--oxy-leaf"], "#54ba48", "#54ba48",
     "Accents only (logo)"),
    ("oxy_forest", "OxyPC Forest", "oxypc", ["--oxy-forest"], "#129042", "#2fb463",
     "Profit figures, solid green fills"),
    ("oxy_mint", "Mint", "oxypc", ["--oxy-mint"], "#e8f5e6", "#17301b",
     "Green tile and label backgrounds"),
    # Bootstrap semantic colours (buttons, badges, text and backgrounds)
    ("primary", "Primary", "semantic", ["--bs-primary"], "#0d6efd", "#3d8bfd",
     "Main actions (Add, Save, Apply), links"),
    ("success", "Success", "semantic", ["--bs-success", "--oxy-text"], "#0f7a37", "#2fb463",
     "Save, Ready to Sell, done"),
    ("warning", "Warning", "semantic", ["--bs-warning"], "#ffc107", "#ffcd39",
     "Pending, GRN, row-count badge"),
    ("danger", "Danger", "semantic", ["--bs-danger"], "#dc3545", "#e35d6a",
     "Delete, failures, PNA"),
    ("info", "Info", "semantic", ["--bs-info"], "#0dcaf0", "#3dd5f3",
     "Stock In, TRC Production"),
    ("secondary", "Secondary", "semantic", ["--bs-secondary"], "#6c757d", "#8a939b",
     "Neutral, Inactive, Cancel"),
]
GROUPS = [("brand", "Brand and neutrals"), ("sidebar", "Sidebar"),
          ("oxypc", "OxyPC greens"), ("semantic", "Semantic (buttons, badges, states)")]
SEMANTIC = [t[0] for t in TOKENS if t[2] == "semantic"]
DEFAULTS = {t[0]: {"light": t[4], "dark": t[5]} for t in TOKENS}

# Design System reference (read-only, shown in the slim right panel).
DESIGN_REFERENCE = [
    ("Brand and neutrals", [("Bluemonk Navy", "#0e3a7a"), ("Bluemonk Blue", "#0377c2"),
                            ("Sky Blue", "#d9ebf6"), ("Light Colour", "#f0f4f8"),
                            ("Dark Colour", "#212529"), ("Disabled", "#d8d8d8")]),
    ("OxyPC greens", [("Leaf", "#54ba48"), ("Forest", "#129042"), ("Text green", "#0f7a37"),
                      ("Mint", "#e8f5e6")]),
    ("Semantic", [("Primary", "#0d6efd"), ("Success", "#0f7a37"), ("Warning", "#ffc107"),
                  ("Danger", "#dc3545"), ("Info", "#0dcaf0"), ("Secondary", "#6c757d"),
                  ("Teal", "#0d9488"), ("Purple", "#6f42c1"), ("Pink", "#d63384")]),
    ("Tile colours", [("Blue", "#0d6efd"), ("Green", "#0f7a37"), ("Orange", "#fd7e14"),
                      ("Amber", "#f0ad00"), ("Teal", "#0aa2c0"), ("Mint", "#20c997"),
                      ("Red", "#dc3545"), ("Pink", "#e83e8c"), ("Purple", "#6f42c1"),
                      ("Grey", "#6c757d")]),
    ("Money", [("Profit", "#129042"), ("Loss", "#d63031"), ("Break-even", "#888888")]),
]

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# In-memory cache (single uvicorn worker; reloaded at startup, after a save
# and by the periodic cache refresh in main.py).
_STATE = {"overrides": {}, "default_mode": "light", "css": None}


def valid_hex(v: str) -> bool:
    return bool(v) and bool(_HEX.match(v))


def current_values() -> dict:
    """Effective {token: {"light", "dark"}} = defaults + saved overrides."""
    out = {}
    ov = _STATE["overrides"]
    for k, d in DEFAULTS.items():
        o = ov.get(k, {})
        out[k] = {"light": o.get("light", d["light"]).lower(), "dark": o.get("dark", d["dark"]).lower()}
    return out


def default_mode() -> str:
    return _STATE["default_mode"]


def apply_loaded(raw_json: str | None, mode: str | None) -> None:
    """Put DB values into the cache (also used after a save)."""
    ov = {}
    if raw_json:
        try:
            data = json.loads(raw_json)
            for k, v in (data or {}).items():
                if k in DEFAULTS and isinstance(v, dict):
                    keep = {m: v[m].lower() for m in ("light", "dark")
                            if valid_hex(v.get(m, "")) and v[m].lower() != DEFAULTS[k][m].lower()}
                    if keep:
                        ov[k] = keep
        except (ValueError, TypeError, AttributeError):
            ov = {}
    _STATE["overrides"] = ov
    _STATE["default_mode"] = mode if mode in ("light", "dark") else "light"
    _STATE["css"] = None


def overrides_json(values: dict) -> str:
    """JSON of the values that differ from the defaults (what gets saved)."""
    ov = {}
    for k, d in DEFAULTS.items():
        v = values.get(k, {})
        keep = {m: v[m].lower() for m in ("light", "dark")
                if valid_hex(v.get(m, "")) and v[m].lower() != d[m].lower()}
        if keep:
            ov[k] = keep
    return json.dumps(ov, separators=(",", ":"))


def _rgb(h: str) -> str:
    h = h.lstrip("#")
    return f"{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}"


def _vars(values: dict, mode: str) -> str:
    parts = []
    for key, _l, _g, css_vars, *_ in TOKENS:
        val = values[key][mode]
        for var in css_vars:
            parts.append(f"{var}:{val}")
        if key in SEMANTIC:
            parts.append(f"--bs-{key}-rgb:{_rgb(val)}")
    # Bootstrap's own page colours follow the tokens too.
    parts.append(f"--bs-body-color:{values['text'][mode]}")
    parts.append(f"--bs-body-bg:{values['surface'][mode]}")
    parts.append(f"--bs-border-color:{values['border'][mode]}")
    return ";".join(parts)


def _semantic_overrides(values: dict) -> str:
    """Bootstrap compiles button colours as fixed values, so a changed semantic
    colour also needs its .btn-* rules redefined (only for changed ones)."""
    css = []
    for key in SEMANTIC:
        for mode in ("light", "dark"):
            if values[key][mode] == DEFAULTS[key][mode].lower():
                continue
            c = values[key][mode]
            sel = ":root:not([data-bs-theme=dark])" if mode == "light" else "[data-bs-theme=dark]"
            css.append(
                f"{sel} .btn-{key}{{--bs-btn-bg:{c};--bs-btn-border-color:{c};"
                f"--bs-btn-hover-bg:color-mix(in srgb,{c} 85%,#000);--bs-btn-hover-border-color:color-mix(in srgb,{c} 80%,#000);"
                f"--bs-btn-active-bg:color-mix(in srgb,{c} 78%,#000);--bs-btn-active-border-color:color-mix(in srgb,{c} 72%,#000);"
                f"--bs-btn-disabled-bg:{c};--bs-btn-disabled-border-color:{c}}}"
                f"{sel} .btn-outline-{key}{{--bs-btn-color:{c};--bs-btn-border-color:{c};--bs-btn-hover-bg:{c};"
                f"--bs-btn-hover-border-color:{c};--bs-btn-active-bg:{c};--bs-btn-active-border-color:{c};"
                f"--bs-btn-disabled-color:{c};--bs-btn-disabled-border-color:{c}}}"
            )
    return "".join(css)


def css() -> str:
    """The <style> body base.html renders after app.css (cached)."""
    if _STATE["css"] is None:
        v = current_values()
        _STATE["css"] = (f":root,[data-bs-theme=light]{{{_vars(v, 'light')}}}"
                         f"[data-bs-theme=dark]{{{_vars(v, 'dark')}}}"
                         + _semantic_overrides(v))
    return _STATE["css"]


def theme_mode(request) -> str:
    """Mode for <html data-bs-theme>: the user's own choice (cookie), else the
    admin's default."""
    try:
        c = (request.cookies.get(COOKIE) or "").lower()
    except Exception:   # noqa: BLE001 — templates without a request
        c = ""
    return c if c in ("light", "dark") else _STATE["default_mode"]


async def load_system_colors_to_cache(db) -> None:
    from sqlalchemy import select
    from models.settings import AppSetting
    rows = (await db.execute(select(AppSetting).where(AppSetting.key.in_([SETTING_KEY, MODE_KEY])))).scalars().all()
    by = {r.key: r.value for r in rows}
    apply_loaded(by.get(SETTING_KEY), by.get(MODE_KEY))
