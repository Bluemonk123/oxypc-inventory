"""Dashboard View permission (2026-11-14, Pankaj; approved schema change:
role_module_permissions.can_dashboard).

The Permission Matrix "Dashboard View" tick, offered only on the modules below,
decides who sees each page's Option C dashboard:
  - a page with a dashboard AND its own content (View Data, or the deal list /
    Accounts cards below the dashboard): without the tick the dashboard part is
    left out and the page's own content shows, with no View Data toggle;
  - a page that is only a dashboard (Main Dashboard, Parts Dashboard): without
    the tick the page itself is refused.
Off for every role until an admin ticks it (Pankaj, 2026-11-14) — no row or
not set = not allowed. Admin always allowed.
"""
DASH_PAGES = {   # path: (matrix module, page is only a dashboard)
    "/dashboard": ("dashboard", True),
    "/parts-dashboard": ("parts_dashboard", True),
    "/devices": ("devices", False),
    "/grn/post-iqc": ("grn_post_iqc", False),
    "/iqc": ("iqc", False),
    "/stock": ("stock", False),
    "/trc-production": ("production_manager", False),
    "/part-estimation": ("part_estimation", False),
    "/transfers": ("transfers", False),
    "/workid-status": ("workid_status", False),
    "/procure-dashboard": ("crm_dashboard", False),   # nav item is gated by crm_dashboard
    "/sales-coordinator": ("sales_coordinator", False),
    "/crm/sourcing": ("crm_sourcing", False),
    "/crm/sales": ("crm_sales_opp", False),
    "/accounts": ("finance", False),
    "/sales": ("sales_list", False),
    "/part-sales": ("part_sales", False),
    "/scrap-sale-report": ("scrap_sale_report", False),
    "/returns": ("returns", False),
    "/reports/business-pl": ("report_bizpl", False),
    "/reports/lot-pl": ("reports", False),
    "/reports/sales": ("report_sales", False),
}
DASH_MODULES = frozenset(m for m, _ in DASH_PAGES.values())
DASH_KEYS = ("dash_kpis", "dash_widgets", "dash_top", "dash_bottom")


def _role(user):
    return getattr(user.role, "value", None) or str(user.role)


def dashboard_allowed(user, path: str) -> bool:
    """True unless `path` is a dashboard page whose module has Dashboard View unticked for the user's role."""
    page = DASH_PAGES.get((path or "").rstrip("/") or "/")
    if not page or user is None:
        return True
    from models.role_permissions import has_perm
    return has_perm(_role(user), page[0], "dashboard")
