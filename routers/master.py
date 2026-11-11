import csv
import io
from templates_config import templates
from fastapi import APIRouter, Depends, Form, Request, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update, func
from database import get_db
from models.user import User, UserRole
from models.master import MasterData
from models.role_permissions import (
    RoleModulePermission, CustomRole, RoleAdditionalPermission,
    get_cached_perms, set_cached_perms, _PERM_CACHE,
    set_cached_additional_perms, _ADDITIONAL_PERM_CACHE,
    get_cached_module_hidden, set_cached_module_hidden, _MODULE_HIDDEN_CACHE,
    DEFAULT_DISABLED_MODULES,
)
from models.settings import AppSetting
from auth.dependencies import get_current_user, require_roles, verify_csrf
from routers.admin import _role_data
from utils.master_data import refresh_master_cache
from utils.size_units import space_size_unit, MASTER_SIZE_CATEGORIES

router = APIRouter(prefix="/admin/master", tags=["master"], dependencies=[Depends(verify_csrf)])
admin_only = require_roles(UserRole.admin)

# ── Accordion groupings for Tab 1 (Dropdown Configuration) ──────────────────
ACCORDION_SECTIONS = [
    {
        "id": "device", "label": "Device & Laptop", "icon": "bi-laptop",
        "cat_keys": [
            "entity", "brand", "sub_category", "device_type", "processor_brand", "processor_series",
            "generation", "storage_type", "storage_size", "ram_type", "ram_size", "screen_size", "grade",
            "battery_health", "os_version", "color", "port_type", "cosmetic_issue", "cosmetic_grade",
        ],
    },
    {
        "id": "repair", "label": "Repair & QC", "icon": "bi-tools",
        "cat_keys": [
            # The three parts lists sit together at the top of the section —
            # they are edited as a set (brand + name + category) whenever the
            # store adds a part, so splitting them across the accordion meant
            # scrolling past a dozen repair-issue lists between each edit.
            "spare_part_brand", "part_category", "iqc_part_category",
            "l34_activity", "l1_issue", "l2_issue", "l3_issue", "repair_issue",
            "repair_resolution", "qc_check_item",
            "repair_action_taken", "repair_received_from", "repair_scrap_reason",
            "repair_source_type", "qc_failure_reason", "cosmetic_final_qc_status",
            "iqc_r2v3_grade_category",
        ],
    },
    {
        "id": "inventory", "label": "Inventory & Logistics", "icon": "bi-box-seam",
        "cat_keys": [
            "floor", "warehouse", "location_zone", "location_unit_type", "supplier",
            "data_destruction_method",
            "transfer_type", "po_category", "spare_parts_ram_action", "spare_parts_ram_gb",
            "spare_parts_consume_stage", "report_year",
        ],
    },
    {
        "id": "sales", "label": "Sales & Returns", "icon": "bi-receipt",
        "cat_keys": [
            "payment_mode", "transport_mode", "delivery_courier", "return_reason", "condition_on_return",
            "customer_state", "sale_warranty_type", "return_type",
            "product_return_reason", "dealer_credit_reason",
        ],
    },
    {
        "id": "telecalling", "label": "Telecalling", "icon": "bi-telephone",
        "cat_keys": [
            "tc_category", "tc_model", "tc_configuration",
            "tc_deal_status", "tc_whom_to_sell", "tc_deals_in", "tc_stock_type",
        ],
    },
    {
        "id": "assign_leads", "label": "Assign Social Leads", "icon": "bi-person-lines-fill",
        "cat_keys": ["asl_status"],
    },
    {
        "id": "dealers", "label": "Dealer Management", "icon": "bi-people",
        "cat_keys": [
            "dealer_dealer_type", "dealer_sub_category", "dealer_state", "dealer_status",
            "call_outcome", "call_mode", "call_type",
        ],
    },
    {
        "id": "crm", "label": "CRM", "icon": "bi-diagram-3",
        "cat_keys": [
            "crm_source_type", "crm_material_type", "crm_buyer_type",
            "crm_priority", "crm_activity_type", "crm_activity_outcome", "crm_credit_days",
        ],
    },
    {
        "id": "other_modules", "label": "WhatsApp / Market / Attendance / QA", "icon": "bi-grid-3x3-gap",
        "cat_keys": [
            "whatsapp_message_type", "whatsapp_group_category",
            "market_trade_type", "market_item_category", "market_condition",
            "attendance_status", "qa_environment",
        ],
    },
    # Product Model Names (2026-11-06, Pankaj): one place for every model name,
    # filled from the tags by "Reload". Shown full width, values in 1-4 columns.
    {
        "id": "product_models", "label": "Product Model Names", "icon": "bi-type",
        "cat_keys": ["product_model_name"], "wide": True,
    },
]

# ── Module list for Tab 2 (Permission Matrix) ─────────────────────────────────
# Keys MUST match the has_perm(role, '<key>', ...) checks used in templates/base.html
# so that enabling/disabling here actually shows/hides the nav item.
PERM_MODULES = [
    # ── Top-level ──────────────────────────────────────────────────
    ("dashboard",            "Admin Dashboard"),
    ("dispatch",             "TRC Dashboard"),
    ("inventory_requests",   "Inventory Request"),
    ("model_requested",      "Model Requested"),
    ("devices",              "Inventory Search"),
    ("activity_board",       "Activity Board"),       # 2026-11-06: Supplier / Customer Deals + Account Verification
    # ── ATTENDANCE ─────────────────────────────────────────────────
    ("attendance",           "My Attendance"),
    ("attendance_report",    "Attendance Report"),
    # ── INTAKE ─────────────────────────────────────────────────────
    ("grn",                  "GRN with Invoice"),
    ("lots",                 "Lot Overview"),
    ("iqc",                  "IQC Line Items"),
    ("grn_post_iqc",         "GRN post IQC"),
    ("grn_records",          "GRN Records"),
    ("entity_movement",      "Entity Movement"),
    # ── INVENTORY ──────────────────────────────────────────────────
    ("stock",                "Stock Inwards"),
    ("production_manager",   "Production Manager"),
    ("extended_warranty",    "Extended Warranty"),
    ("part_estimation",      "Part Estimation"),
    ("rentals",              "Rentals"),
    ("credit_note",          "Credit Note"),
    ("scrap_products",       "Scrap Products"),
    ("transfers",            "Move Device"),
    # ── REPAIR ─────────────────────────────────────────────────────
    ("repair_l1",            "L1 Repair"),
    ("repair_l2",            "L2 Repair"),
    ("repair_l3",            "L3 Repair"),
    # repair_l3 above is still the permission key gating the sidebar's L3/L4
    # link (has_perm(role,'repair_l3','enable')) — this entry is the label
    # key that link's sidebar_label() call actually reads, which had no
    # PERM_MODULES row at all, so it could never be renamed from Sidebar
    # Config or shown on Module Page Titles.
    ("repair_l3l4",          "L3/L4 Repair"),
    ("qc_check",             "Stress Test"),
    # ── COSMETIC REFURB ────────────────────────────────────────────
    # Split from a single "cosmetic" key into one per pipeline page so each
    # can be enabled/edited independently (routers/cosmetic.py
    # PERM_MODULE_BY_STAGE / ASSIGN_ON_MOVE_STAGES).
    ("cosmetic_received",     "Cosmetic Received"),
    ("cosmetic_cleaning",     "Cosmetic - Cleaning"),
    ("cosmetic_putty",        "Cosmetic - Putty"),
    ("cosmetic_dry_sanding",  "Cosmetic - Dry Sanding"),
    ("cosmetic_masking",      "Cosmetic - Masking"),
    ("cosmetic_painting",     "Cosmetic - Painting"),
    ("cosmetic_water_sanding","Cosmetic - Water Sanding"),
    ("cosmetic_completed",    "Cosmetic Completed"),
    ("cosmetic_finalqc",     "Final QC"),
    ("pre_sale_iqc",         "Pre-Sale QC"),         # OFF by default (DEFAULT_DISABLED_MODULES), 2026-10-29
    ("workid_status",        "WorkID Status"),
    # ── STORE MANAGER ──────────────────────────────────────────────
    ("parts_dashboard",      "Parts Dashboard"),
    ("spare_parts",          "Parts Manager"),
    ("parts_sale_request",   "Parts Sale Request"),
    ("spare_parts_purchase", "Parts GRN / Parts Purchased"),  # 2026-10-30: one key gates both /parts-grn and /spare-parts/purchase
    ("parts_tracking",       "Parts Tracking"),
    ("parts_consumption",    "Parts Consumption"),
    # ── CRM ────────────────────────────────────────────────────────
    ("crm_dashboard",        "CRM Dashboard"),
    ("crm_contacts",         "Contact Leads"),
    ("crm_sourcing",         "Sourcing Deals"),
    ("crm_sales_opp",        "Sales Opportunities"),
    ("crm_price_matrix",     "Price Matrix"),
    # Gated on crm_sales_opp in the sidebar (quotes hang off Buyer Deals and
    # share their access) rather than its own permission — this entry exists
    # purely so Sidebar Config / Module Page Titles have a row for its label.
    ("crm_quotes",           "Quotes"),
    ("crm_purchase_orders",  "Purchase Orders"),
    ("crm_pickups",          "Pickup Process"),        # 2026-11-11, default disabled
    ("crm_analytics",        "CRM Analytics"),
    ("crm_assign_leads",     "Assign Social Leads"),
    # ── EXTERNAL PARTNER ──────────────────────────────────────────────
    # Split from a single "trade_partner" key into one per portal-admin page
    # (2026-09-22), same convention as the earlier cosmetic-pipeline split,
    # so each can be enabled per role independently. All 8 default disabled
    # for every role — see migrate_seed_trade_partner_split_perms.py.
    ("trade_partner_partners",    "External Partner - Partner Accounts"),
    ("trade_partner_listings",    "External Partner - Listings Manager"),
    ("trade_partner_bookings",    "External Partner - Bookings Queue"),
    ("trade_partner_my_desk",     "External Partner - My Desk"),
    ("trade_partner_floors",      "External Partner - Margin Floors"),
    ("trade_partner_settings",    "External Partner - Portal Settings"),
    ("trade_partner_manage_lots", "External Partner - Manage Lots"),
    ("trade_partner_all_deals",   "External Partner - All Deals"),
    ("trade_partner_bids",        "External Partner - Bids Created"),
    # ── CUSTOMER CARE AGENT ──────────────────────────────────────────
    ("care_support",         "Customer Care"),
    # ── SALES & CRM ────────────────────────────────────────────────
    ("telesales_dashboard",  "TeleSales Dashboard"),
    ("block_tags",           "Block Tags"),
    ("sales",                "Ready to Sale"),
    ("sales_list",           "Sales List"),
    ("ready_to_sale_parts",  "Ready to Sale Parts"),
    ("scrap_for_sale",       "Scrap for Sale"),
    ("scrap_sale_report",    "Scrap Sale Report"),   # OFF by default (DEFAULT_DISABLED_MODULES)
    ("part_sales",           "Spare Part Sales"),
    ("gate_pass",            "Gate Pass"),
    ("partner_payments",     "Partner Payments"),
    ("returns",              "Returns"),
    ("dealers",              "Dealers"),
    ("telecalling",          "Telecalling"),
    ("quotations",           "Quotations"),
    ("model_requests",       "Model Requests"),
    ("whatsapp",             "WhatsApp"),
    ("assign_dealer_leads",  "Assign Dealer Leads"),
    # ── FINANCE ────────────────────────────────────────────────────
    ("finance",              "Accounts"),
    ("finance_supplier",     "Supplier Payments"),
    ("finance_customer",     "Customer Receipts"),
    ("physical_audits",      "Physical Audits"),
    # ── INVENTORY LOCATIONS ────────────────────────────────────────
    ("locations",            "Location Map"),
    ("location_gaps",        "Gap Alerts"),
    ("location_audit",       "Zone Audit"),
    ("location_master",      "Manage Locations"),
    ("location_trash",       "Trash"),
    # ── REPORTS ────────────────────────────────────────────────────
    ("reports",              "Lot P&L"),
    ("report_sales",         "Sales Report"),
    ("report_daily_stock",   "Daily Stock"),
    ("report_stage",         "Stage Log"),
    ("report_bizpl",         "Business P&L"),
    ("report_aging",         "Stock Aging"),
    ("report_overdue",       "Overdue Devices"),
    ("report_receivables",   "Receivables"),
    ("market",               "Market Intel"),
    # ── ADMIN (admin-only) ────────────────────────────────────────────────
    # Product Management (2026-11-05): one row per page; the sidebar group
    # shows when any of them is enabled.
    ("qa",                   "QA Dashboard"),
    ("qa_uat",               "UAT Analysis"),
    ("qa_ux",                "UX Journeys"),
    ("manuals",              "Manuals"),
    # ── APPLICATION SETTINGS (admin-only by default; grantable via matrix) ─
    ("move_device_internal", "Move Device Internal"),
    ("stage_control",        "Stage Control"),
    ("aging_tracker",        "Aging Tracker"),
    ("stage_audit_log",      "Stage Audit Log"),
    ("system_audit_log",     "System Audit Log"),
    ("sidebar_config",       "Sidebar Config"),
    ("landing_pages",        "Module Page Titles"),
    ("wa_audit_log",         "WA Audit Log"),
    # ── ADMIN SETTINGS accordion (admin-only by default; grantable to
    #    sub_admin — or any role — via this matrix / the Sub Admin Role tab) ─
    ("admin_users",          "Users"),
    ("admin_master",         "Master Data"),
    ("company_settings",     "Company Settings"),
    ("cost_config",          "Cost Config"),
    ("attendance_config",    "Attendance Config"),
    ("terms_conditions",     "Terms & Conditions"),
    # ── PRODUCT MASTER (2026-10-14) — OFF by default for every role
    #    (models.role_permissions.DEFAULT_DISABLED_MODULES) ─────────────
    ("pm_product_master",    "Product Master"),
    ("pm_pricing_calculator", "Pricing Calculator"),
    ("pm_hardware",          "Hardware"),
    ("pm_spare_parts",       "Spare Parts"),
    ("pm_product_pricing",   "Product Pricing"),
]

# ── Permission Matrix module names (2026-11-03): "<Sidebar section> - <Sidebar
# name>", e.g. "Intake - Lot Overview". The section comes from where the
# module's link sits in templates/base.html; the name is its CURRENT sidebar
# label (Sidebar Config), so renaming a sidebar item renames it here too.
# Modules with no sidebar link of their own borrow the section of the page
# they live on; links above the first section show the name alone.
_SECTION_FROM = {
    "repair_l2": "repair_l1", "repair_l3": "repair_l1",
    "cosmetic_cleaning": "cosmetic_received", "cosmetic_putty": "cosmetic_received",
    "cosmetic_dry_sanding": "cosmetic_received", "cosmetic_masking": "cosmetic_received",
    "cosmetic_painting": "cosmetic_received", "cosmetic_water_sanding": "cosmetic_received",
    "cosmetic_completed": "cosmetic_received", "company_settings": "admin_users",
}
_KEEP_UPPER = {"CRM", "QC", "IQC", "TRC", "GRN", "P&L", "WA", "QA", "PNA"}
_SIDEBAR_SECTIONS_CACHE: dict | None = None
_SIDEBAR_ORDER: dict = {}


def _section_title(sec: str) -> str:
    return " ".join(w if w.upper() in _KEEP_UPPER else w.capitalize() for w in (sec or "").split())


def _sidebar_sections() -> dict:
    """{module_key: (section or None, default sidebar label)} parsed from base.html."""
    global _SIDEBAR_SECTIONS_CACHE
    if _SIDEBAR_SECTIONS_CACHE is None:
        import os
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "base.html")
        out, sec = {}, None
        try:
            for line in open(path, encoding="utf-8"):
                m = re.search(r'<li class="nav-section">([^<]+)</li>', line)
                if m:
                    sec = m.group(1).strip()
                for k, d in re.findall(r"sidebar_label\('([a-z0-9_]+)',\s*'([^']*)'\)", line):
                    if k not in out:
                        _SIDEBAR_ORDER[k] = len(_SIDEBAR_ORDER)   # position in the sidebar
                    out.setdefault(k, (sec, d))
        except OSError:
            pass
        _SIDEBAR_SECTIONS_CACHE = out
    return _SIDEBAR_SECTIONS_CACHE


def perm_modules_in_sidebar_order() -> list:
    """PERM_MODULES sorted the way the sidebar lists them (2026-11-03), so a
    section's items sit together in the matrix. A module with no sidebar link
    of its own sits right after the page it lives on (_SECTION_FROM); any
    other comes last, in its PERM_MODULES order."""
    _sidebar_sections()
    big = len(_SIDEBAR_ORDER) + 1

    def pos(item):
        i, (key, _label) = item
        if key in _SIDEBAR_ORDER:
            return (_SIDEBAR_ORDER[key], 0, i)
        host = _SECTION_FROM.get(key)
        if host in _SIDEBAR_ORDER:
            return (_SIDEBAR_ORDER[host], 1, i)
        return (big, 0, i)
    return [m for _i, m in sorted(enumerate(PERM_MODULES), key=pos)]


def perm_module_display_names() -> dict:
    """{module_key: "Section - Current sidebar name"} for the Permission Matrix."""
    from models.role_permissions import get_cached_sidebar_label
    sb = _sidebar_sections()
    names = {}
    for key, label in PERM_MODULES:
        sec, default = sb.get(key, (None, None))
        if key not in sb and key in _SECTION_FROM:
            sec = sb.get(_SECTION_FROM[key], (None, None))[0]
        name = get_cached_sidebar_label(key, default or label)
        sec_t = _section_title(sec) if sec else ""
        names[key] = f"{sec_t} - {name}" if sec_t and not name.lower().startswith(sec_t.lower()) else name
    return names


# ── Sub Admin Role tab (item 7) ───────────────────────────────────────────────
# 'admin_settings' is a SYNTHETIC master switch — not a real navigable page, so
# it stays out of PERM_MODULES (would render nonsensically on the Sidebar
# Config / Module Page Titles pages, which iterate PERM_MODULES expecting real
# URLs). It's the one thing exclusive to the "Sub Admin Role" tab; every other
# admin module above is a real page and lives in PERM_MODULES so it shows
# consistently in the general Permission Matrix, Sidebar Config, and Module
# Page Titles too — any role can be granted it there, not just sub_admin.
SUB_ADMIN_EXTRA_MODULES = [
    ("admin_settings",    "Admin Settings (accordion master switch)"),
]

# Everything the Sub Admin Role tab can toggle for the sub_admin role: every
# regular module PLUS the synthetic master switch above.
SUB_ADMIN_MODULES = PERM_MODULES + SUB_ADMIN_EXTRA_MODULES
SUB_ADMIN_ROLE = "sub_admin"

# ── Global Visibility tab ─────────────────────────────────────────────────────
# Same module list as Sub Admin Role (includes the synthetic 'admin_settings'
# switch, since hiding the whole Admin Settings accordion is a real, useful
# toggle) MINUS 'admin_master' — Master Data is the page this very tab lives
# on, so hiding it would strand admin with no UI path left to undo the hide.
# Unlike has_perm()/has_explicit_perm() (role-scoped, admin always bypasses),
# this hides a module from EVERY user including admin — see
# models/role_permissions.py get_cached_module_hidden / module_hidden() in
# templates_config.py.
GLOBAL_VISIBILITY_MODULES = [(k, l) for k, l in SUB_ADMIN_MODULES if k != "admin_master"]
GLOBAL_HIDDEN_PREFIX = "global_hidden_"


async def load_module_visibility_to_cache(db: AsyncSession) -> None:
    """Populate the in-memory Global Visibility cache from AppSetting rows.
    Called at app startup and after every toggle. Missing rows default to
    visible (not hidden) via get_cached_module_hidden()."""
    rows = (await db.execute(
        select(AppSetting).where(AppSetting.key.like(f"{GLOBAL_HIDDEN_PREFIX}%"))
    )).scalars().all()
    _MODULE_HIDDEN_CACHE.clear()
    for r in rows:
        module_key = r.key[len(GLOBAL_HIDDEN_PREFIX):]
        _MODULE_HIDDEN_CACHE[module_key] = (r.value == "1")

PERM_ACTIONS = [
    ("can_enable", "Enable"),
    ("can_pricing", "Pricing View"),          # see prices (was "Pricing")
    ("can_pricing_add", "Pricing Add"),       # set / change prices (2026-11-03)
    ("can_pricing_export", "Pricing Export"), # price columns in exports (2026-11-03)
    ("can_add", "Add"),
    ("can_edit", "Edit"),
    ("can_delete", "Delete"),
    ("can_upload", "Upload"),
    ("can_export", "Export"),
]


def module_action_map() -> dict:
    """{module: {"add", "edit", ...}} — which action ticks actually gate
    something for each module, from auth/route_actions.py. Ticks a module has
    no route for show as "—" in the matrix. Pricing is always offered: it
    also hides price columns on the module's pages, not only price changes."""
    from auth.route_actions import ROUTE_ACTIONS
    out: dict = {}
    for entry in ROUTE_ACTIONS.values():
        if not entry:
            continue
        mods, action = entry
        for m in ((mods,) if isinstance(mods, str) else mods):
            out.setdefault(m, set()).update(action if isinstance(action, tuple) else (action,))
    return out

# ── Role Additional Permissions — cross-cutting, not tied to any one module ──
# "Add New Data" was split into per-record-type toggles (can_add_lot/iqc/grn)
# so an admin can grant "raise a lot" without also granting "register an IQC
# device" — the old single switch couldn't distinguish those. The Edit group
# is new: unlike the per-module matrix's Edit checkbox (display-only — see
# has_perm()'s docstring), these ARE enforced (require_additional_perm).
ADDITIONAL_PERMS = [
    ("can_upload",        "File Upload"),
    ("can_download",      "File Download"),
    ("can_export",        "File Export"),
    ("can_print",         "Print Page"),
    ("can_add_lot",       "Add Lot"),
    ("can_add_iqc",       "Add IQC"),
    ("can_add_grn",       "Add GRN"),
    ("can_edit_devices",  "Devices Edit"),
    ("can_edit_iqc",      "IQC Edit"),
    ("can_edit_lot",      "Lot Edit"),
    ("can_edit_grn",      "GRN Edit"),
]

CATEGORIES = [
    # ── Device Identity ───────────────────────────────────────────
    ("entity",              "Entities",                     "laptop"),
    ("brand",               "Device Brands",                "laptop"),
    ("sub_category",        "Device Sub-Categories",        "laptop"),
    ("device_type",         "Device Form Factors",          "laptop"),
    ("processor_brand",     "Processor Brands",             "laptop"),
    ("processor_series",    "Processor Series",             "laptop"),
    ("generation",          "CPU Generations",              "laptop"),
    ("storage_type",        "Storage Types",                "laptop"),
    ("storage_size",        "Storage Sizes",                "laptop"),
    ("ram_type",            "RAM Types",                    "laptop"),
    ("ram_size",            "RAM Sizes",                    "laptop"),
    ("screen_size",         "Screen Sizes",                 "laptop"),
    ("grade",               "Device Grades",                "laptop"),
    ("battery_health",      "Battery Health Levels",        "laptop"),
    ("os_version",          "OS Versions",                  "laptop"),
    ("color",               "Colors",                       "laptop"),
    ("port_type",           "Port Types",                   "laptop"),
    ("cosmetic_issue",      "Cosmetic Issues",              "laptop"),
    ("cosmetic_grade",      "Cosmetic Grade Descriptions",  "laptop"),
    # ── Repair ───────────────────────────────────────────────────
    ("l34_activity",        "L3/L4 Mark Complete Activities","repair"),
    ("l1_issue",            "L1 Repair Issues",             "repair"),
    ("l2_issue",            "L2 Repair Issues",             "repair"),
    ("l3_issue",            "L3 Repair Issues",             "repair"),
    ("repair_issue",        "General Repair Issues",        "repair"),
    ("repair_resolution",   "Repair Resolutions",           "repair"),
    ("spare_part_brand",    "Spare Part Brands",            "repair"),
    ("part_category",       "Spare Part Names",             "repair"),
    ("qc_check_item",       "QC Check Items",               "repair"),
    ("repair_action_taken", "Repair L3: Action Taken",      "repair"),
    ("repair_received_from","Repair L3: Received From",     "repair"),
    ("repair_scrap_reason", "Repair L3: Scrap Reason",      "repair"),
    ("repair_source_type",  "Repair L3: Customer/Internal", "repair"),
    ("qc_failure_reason",   "Cosmetic/QC: Failure Reason",  "repair"),
    ("cosmetic_final_qc_status", "Cosmetic: Final QC Status", "repair"),
    ("iqc_r2v3_grade_category", "IQC: R2V3 Grade Category",  "repair"),
    ("iqc_part_category",   "Parts: Part Category",         "repair"),
    # ── Inventory / Logistics ─────────────────────────────────────
    ("floor",               "Floors / Locations",           "inventory"),
    ("warehouse",           "Warehouses",                   "inventory"),
    ("location_zone",       "Storage Locations: Zone",      "inventory"),
    ("location_unit_type",  "Storage Locations: Unit Type", "inventory"),
    ("supplier",            "Suppliers",                    "inventory"),
    ("data_destruction_method", "Data Destruction Methods", "inventory"),
    ("transfer_type",       "Transfers: Transfer Type",     "inventory"),
    ("po_category",         "Purchase Orders: PO Category", "inventory"),
    ("spare_parts_ram_action", "Spare Parts: RAM Action",    "inventory"),
    ("spare_parts_ram_gb",  "Spare Parts: RAM Capacity (GB)", "inventory"),
    ("spare_parts_consume_stage", "Spare Parts: Consumption Stage", "inventory"),
    ("report_year",         "Reports: Year Options (Dashboard/Business P&L)", "inventory"),
    # ── Sales / Returns ──────────────────────────────────────────
    ("payment_mode",        "Payment Modes",                "sales"),
    ("transport_mode",      "Sales: Transport Mode",        "sales"),
    ("delivery_courier",    "Sales: Delivery Courier",      "sales"),
    ("return_reason",       "Return Reasons",               "sales"),
    ("condition_on_return", "Condition on Return",          "sales"),
    ("customer_state",      "Sales: Customer State (GST)",  "sales"),
    ("sale_warranty_type",  "Sales: Warranty Type",         "sales"),
    ("return_type",         "Sales: Return Type",           "sales"),
    ("product_return_reason", "Sales: Product Return Reason", "sales"),
    ("dealer_credit_reason", "Dealers: Credit Note Reason", "sales"),
    # ── Dealer Management ─────────────────────────────────────────
    ("dealer_dealer_type",  "Dealers: Dealer Type",         "dealers"),
    ("dealer_sub_category", "Dealers: Dealer Sub Category", "dealers"),
    ("dealer_state",        "Dealers: State",               "dealers"),
    ("dealer_status",       "Dealers: Status",              "dealers"),
    ("call_outcome",        "Dealer Calls: Call Outcome",   "dealers"),
    ("call_mode",           "Dealer Calls: Call Mode",      "dealers"),
    ("call_type",           "Dealer Calls: Call Type",      "dealers"),
    # ── CRM ────────────────────────────────────────────────────────
    ("crm_source_type",     "CRM: Source Type",             "crm"),
    ("crm_material_type",   "CRM: Material Type",           "crm"),
    ("crm_buyer_type",      "CRM: Buyer Type",              "crm"),
    ("crm_priority",        "CRM: Deal Priority",           "crm"),
    ("crm_activity_type",   "CRM: Activity Type",           "crm"),
    ("crm_activity_outcome","CRM: Activity Outcome",        "crm"),
    ("crm_credit_days",     "CRM: Credit Days (Accounts)",  "crm"),
    # ── WhatsApp / Market / Attendance / QA ───────────────────────
    ("whatsapp_message_type", "WhatsApp: Message Type",     "other_modules"),
    ("whatsapp_group_category", "WhatsApp: Group Category", "other_modules"),
    ("market_trade_type",   "Market: Trade Type",           "other_modules"),
    ("market_item_category","Market: Item Category",        "other_modules"),
    ("market_condition",    "Market: Item Condition",       "other_modules"),
    ("attendance_status",   "Attendance: Status",           "other_modules"),
    ("qa_environment",      "QA: Test Environment",         "other_modules"),
    # ── Telecalling ────────────────────────────────────────────────
    ("tc_category",         "Telecalling: Category",         "sales"),
    ("tc_model",            "Telecalling: Model",            "sales"),
    ("tc_configuration",    "Telecalling: Configuration",    "sales"),
    ("tc_deal_status",      "Telecalling: Status",           "sales"),
    ("tc_whom_to_sell",     "Telecalling: Whom To Sale",     "sales"),
    ("tc_deals_in",         "Telecalling: Deals In",         "sales"),
    ("tc_stock_type",       "Telecalling: Ready / Lot",      "sales"),
    # ── Assign Social Leads ────────────────────────────────────────
    ("asl_status",          "Assign Leads: Status",          "sales"),
    # ── Product Model Names (2026-11-06) ────────────────────────────
    ("product_model_name",  "Product Model Names",           "laptop"),
]

# Dropdowns whose values belong to a value of another dropdown (2026-11-06):
# category -> the category its parent comes from.
PARENT_CATEGORIES = {"product_model_name": "brand"}

# Group by tab
TABS = [
    ("laptop",    "Device / Laptop",  "bi-laptop"),
    ("repair",    "Repair & QC",      "bi-tools"),
    ("inventory", "Inventory",        "bi-box-seam"),
    ("sales",     "Sales & Returns",  "bi-receipt"),
]


@router.get("", response_class=HTMLResponse)
async def master_list(
    request: Request,
    main_tab: str = "dropdowns",
    role: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Single entry point for BOTH tabs.

    Always loads the dropdown-config accordion data AND the permission-matrix
    data, so either tab renders correctly regardless of which one is active.
    """
    result = await db.execute(
        select(MasterData).order_by(MasterData.category, MasterData.display_order, MasterData.value)
    )
    items = result.scalars().all()
    grouped = {}
    for cat_key, cat_label, cat_tab in CATEGORIES:
        its = [i for i in items if i.category == cat_key]
        if cat_key in PARENT_CATEGORIES:     # grouped by Make, then name (2026-11-06)
            its.sort(key=lambda i: ((i.parent or "~").lower(), i.value.lower()))
        grouped[cat_key] = {
            "label": cat_label,
            "tab": cat_tab,
            "items": its,
        }

    # Build accordion sections with data attached
    accordion_data = []
    for sec in ACCORDION_SECTIONS:
        cats = []
        for ck in sec["cat_keys"]:
            if ck in grouped:
                cats.append({"key": ck, **grouped[ck]})
        accordion_data.append({**sec, "categories": cats})

    # ── Permission-matrix data (always loaded so Tab 2 works) ────────────────
    # Same role list + display labels as the Add User page (models.user.ROLE_LABELS
    # + custom roles), so "Cosmetic Manager" / "Store Manager" etc. never drift
    # out of sync between the two dropdowns.
    all_roles, _ = await _role_data(db)

    # Default selected role: requested role, else first non-admin role
    selected_role = role
    if not selected_role and len(all_roles) > 1:
        selected_role = all_roles[1][0]

    perm_rows = {}
    if selected_role:
        rows = (await db.execute(
            select(RoleModulePermission)
            .where(RoleModulePermission.role_name == selected_role)
        )).scalars().all()
        perm_rows = {r.module: r for r in rows}

    custom_roles_q2 = await db.execute(select(CustomRole).order_by(CustomRole.display_name))
    custom_roles_list = custom_roles_q2.scalars().all()

    # ── Role Additional Permissions data (Tab 3) ──────────────────────────────
    additional_perm_row = None
    if selected_role:
        additional_perm_row = (await db.execute(
            select(RoleAdditionalPermission).where(RoleAdditionalPermission.role_name == selected_role)
        )).scalar_one_or_none()

    # ── Pricing Visibility data (Tab 4, Batch 9) ──────────────────────────────
    # One row per role (built-in + custom), default True (visible) when no
    # RoleAdditionalPermission row exists yet for that role — matches
    # can_view_pricing()'s permissive-by-default convention.
    all_add_perm_rows = (await db.execute(select(RoleAdditionalPermission))).scalars().all()
    pricing_by_role = {r.role_name: r.can_view_pricing for r in all_add_perm_rows}
    set_price_by_role = {r.role_name: (True if r.can_set_price is None else r.can_set_price)
                         for r in all_add_perm_rows}
    # (role value, label, Pricing Visible, Set Price) — Set Price controls the
    # Set price buttons on Ready to Sale.
    pricing_rows = [
        (rval, rlabel, pricing_by_role.get(rval, True), set_price_by_role.get(rval, True))
        for rval, rlabel in all_roles if rval != "admin"
    ]

    # ── Sub Admin Role data (item 7) ──────────────────────────────────────────
    # Always scoped to the fixed 'sub_admin' role (unlike the general matrix,
    # which follows the role dropdown). Includes the admin-only surfaces so an
    # admin can hand sub_admin the Admin Settings accordion and its pages.
    sub_admin_rows = (await db.execute(
        select(RoleModulePermission).where(RoleModulePermission.role_name == SUB_ADMIN_ROLE)
    )).scalars().all()
    sub_admin_perm_rows = {r.module: r for r in sub_admin_rows}

    # ── Global Visibility data ────────────────────────────────────────────────
    global_visibility_rows = [
        (mod_key, mod_label, get_cached_module_hidden(mod_key))
        for mod_key, mod_label in GLOBAL_VISIBILITY_MODULES
    ]

    return templates.TemplateResponse("admin/master.html", {
        "request": request,
        "grouped": grouped,
        "categories": CATEGORIES,
        "parent_categories": PARENT_CATEGORIES,
        "tabs": TABS,
        "accordion_data": accordion_data,
        "current_user": current_user,
        # Permission tab data
        "all_roles": all_roles,
        "selected_role": selected_role,
        "perm_rows": perm_rows,
        "default_disabled_modules": DEFAULT_DISABLED_MODULES,
        "perm_modules": perm_modules_in_sidebar_order(),
        "perm_module_names": perm_module_display_names(),
        "perm_actions": PERM_ACTIONS,
        "module_actions": module_action_map(),
        "custom_roles_list": custom_roles_list,
        # Additional permissions tab data
        "additional_perms": ADDITIONAL_PERMS,
        "additional_perm_row": additional_perm_row,
        # Pricing Visibility tab data
        "pricing_rows": pricing_rows,
        # Sub Admin Role tab data (item 7)
        "sub_admin_modules": SUB_ADMIN_MODULES,
        "sub_admin_perm_rows": sub_admin_perm_rows,
        "sub_admin_role": SUB_ADMIN_ROLE,
        # Global Visibility tab data
        "global_visibility_rows": global_visibility_rows,
        # System Colors tab data (2026-11-08)
        **_system_colors_ctx(),
        # Email tab data (2026-11-09)
        **((await _email_ctx(db)) if main_tab == "email" else {}),
    })


# ── System Colors tab (2026-11-08, Pankaj) ─────────────────────────────────
def _system_colors_ctx() -> dict:
    from services import system_colors as sc
    values = sc.current_values()
    groups = []
    for gkey, glabel in sc.GROUPS:
        rows = [{"key": t[0], "label": t[1], "vars": t[3], "usage": t[6],
                 "light": values[t[0]]["light"], "dark": values[t[0]]["dark"],
                 "def_light": t[4].lower(), "def_dark": t[5].lower()}
                for t in sc.TOKENS if t[2] == gkey]
        groups.append({"key": gkey, "label": glabel, "rows": rows})
    return {"sc_groups": groups, "sc_default_mode": sc.default_mode(),
            "sc_reference": sc.DESIGN_REFERENCE}


async def _save_setting(db: AsyncSession, key: str, value: str, description: str, username: str) -> None:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if row:
        row.value = value
        row.updated_by = username
    else:
        db.add(AppSetting(key=key, value=value, description=description, updated_by=username))


@router.post("/system-colors/save")
async def save_system_colors(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Save every colour token (Light and Dark) and the default mode for users.
    Applies app-wide at once (in-memory cache + AppSetting rows)."""
    from services import system_colors as sc
    form = await request.form()
    values, bad = {}, []
    for key in sc.DEFAULTS:
        values[key] = {}
        for mode in ("light", "dark"):
            v = (form.get(f"c_{key}_{mode}") or "").strip().lower()
            if not v:
                v = sc.DEFAULTS[key][mode]
            if not sc.valid_hex(v):
                bad.append(f"{key} ({mode})")
                v = sc.DEFAULTS[key][mode]
            values[key][mode] = v
    if bad:
        return RedirectResponse(url="/admin/master?main_tab=system_colors&error=Not+a+colour:+" + "+".join(bad)[:150],
                                status_code=302)
    mode = (form.get("default_mode") or "light").strip().lower()
    mode = mode if mode in ("light", "dark") else "light"
    raw = sc.overrides_json(values)
    await _save_setting(db, sc.SETTING_KEY, raw, "System Colors: changed colour tokens (Light/Dark)", current_user.username)
    await _save_setting(db, sc.MODE_KEY, mode, "System Colors: default Light/Dark mode for users", current_user.username)
    await db.commit()
    sc.apply_loaded(raw, mode)
    return RedirectResponse(url="/admin/master?main_tab=system_colors&success=System+colours+saved", status_code=302)


@router.post("/system-colors/reset")
async def reset_system_colors(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Reset every colour to the Design System default (keeps the default mode)."""
    from services import system_colors as sc
    await _save_setting(db, sc.SETTING_KEY, "{}", "System Colors: changed colour tokens (Light/Dark)", current_user.username)
    await db.commit()
    sc.apply_loaded("{}", sc.default_mode())
    return RedirectResponse(url="/admin/master?main_tab=system_colors&success=Colours+reset+to+the+Design+System",
                            status_code=302)


# ── Email tab (2026-11-09, Pankaj): notifications also go by email ──────────
async def _email_ctx(db: AsyncSession) -> dict:
    """Mail server status for the Email tab — never the password."""
    from services import email_notify as en
    from sqlalchemy import func as _f
    active = (await db.execute(select(_f.count()).select_from(User).where(User.status == True))).scalar() or 0  # noqa: E712
    with_email = (await db.execute(select(_f.count()).select_from(User).where(
        User.status == True, User.email.isnot(None), User.email.like("%@%")))).scalar() or 0  # noqa: E712
    boxes = []
    for key, label, prefix, cfg in (("main", "Main mailbox", "OXYPC_SMTP", en.settings()),
                                    ("backup", "Backup mailbox", "OXYPC_SMTP2", en.backup_settings())):
        boxes.append({"key": key, "label": label, "prefix": prefix, "ready": en.is_configured(cfg),
                      "host": cfg["host"], "port": cfg["port"], "user": cfg["user"], "from": cfg["from"],
                      "security": cfg["security"], "has_password": bool(cfg["password"])})
    return {"em_configured": en.is_configured(), "em_boxes": boxes,
            "em_public_url": en.settings()["public_url"],
            "em_active": active, "em_with_email": with_email}


@router.post("/email/test")
async def send_test_email(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Send one test email to the address typed (default: the admin's own)."""
    from urllib.parse import quote_plus
    from services import email_notify as en
    form = await request.form()
    to = (form.get("to") or current_user.email or "").strip()
    box = "backup" if form.get("box") == "backup" else "main"
    back = "/admin/master?main_tab=email"
    if not to or "@" not in to:
        return RedirectResponse(url=back + "&error=" + quote_plus(
            "Enter an email address (your profile has none)."), status_code=302)
    try:
        await en.send_now([to], f"Test email ({box} mailbox)",
                          f"This is a test from OxyPC ERP through the {box} mailbox, sent by "
                          f"{current_user.full_name or current_user.username}. "
                          "If you can read this, email notifications are working.", box=box)
    except Exception as e:
        return RedirectResponse(url=back + "&error=" + quote_plus(f"Test email ({box} mailbox) not sent: {e}"[:300]),
                                status_code=302)
    return RedirectResponse(url=back + "&success=" + quote_plus(f"Test email sent to {to} through the {box} mailbox"),
                            status_code=302)


@router.post("/email/settings")
async def save_email_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Save one mailbox's settings into the server's .env (2026-11-11, Pankaj).
    Only the SMTP keys — services/env_settings.py refuses anything else. The
    password box is never pre-filled: left empty = keep the current one."""
    import os
    from urllib.parse import quote_plus
    from services.audit_engine import audit
    from services.env_settings import update_env
    form = await request.form()
    box = "backup" if form.get("box") == "backup" else "main"
    prefix = "OXYPC_SMTP2" if box == "backup" else "OXYPC_SMTP"
    back = "/admin/master?main_tab=email"
    g = lambda k: (form.get(k) or "").strip()  # noqa: E731
    host, port, security, user, sender = g("host"), g("port"), g("security").lower(), g("user"), g("from")
    if form.get("turn_off") == "1":
        host = port = security = user = sender = ""
    if port and not (port.isdigit() and 0 < int(port) < 65536):
        return RedirectResponse(url=back + "&error=" + quote_plus("Port must be a number, e.g. 587 or 465"), status_code=302)
    if security not in ("", "starttls", "ssl", "none"):
        security = ""
    if host and not (user or sender):
        return RedirectResponse(url=back + "&error=" + quote_plus("Enter the mailbox (login) or Sent from address"), status_code=302)
    changes = {f"{prefix}_HOST": host, f"{prefix}_PORT": port, f"{prefix}_SECURITY": security,
               f"{prefix}_USER": user, f"{prefix}_FROM": sender}
    pw = form.get("password") or ""
    if form.get("turn_off") == "1" or form.get("clear_password") == "1":
        changes[f"{prefix}_PASSWORD"] = ""
    elif pw:
        changes[f"{prefix}_PASSWORD"] = pw
    if box == "main" and "public_url" in form:
        changes["OXYPC_PUBLIC_URL"] = g("public_url").rstrip("/")
    old = {k: ("(set)" if k.endswith("_PASSWORD") and os.environ.get(k) else os.environ.get(k) or "") for k in changes}
    try:
        changed = update_env(changes)
    except (ValueError, OSError) as e:
        return RedirectResponse(url=back + "&error=" + quote_plus(f"Not saved: {e}"[:300]), status_code=302)
    if not changed:
        return RedirectResponse(url=back + "&success=" + quote_plus("Nothing changed"), status_code=302)
    new = {k: ("(set)" if k.endswith("_PASSWORD") and v else v) for k, v in changes.items()}
    await audit(db, action="EMAIL_SETTINGS_SAVED", user=current_user, table_name=".env",
                record_id=box, old_value={k: old[k] for k in changed},
                new_value={k: new[k] for k in changed}, request=request)   # never the password itself
    await db.commit()
    label = "Backup" if box == "backup" else "Main"
    msg = f"{label} mailbox turned off" if form.get("turn_off") == "1" else f"{label} mailbox saved — send a test email to check it"
    return RedirectResponse(url=back + "&success=" + quote_plus(msg), status_code=302)


@router.post("/global-visibility/toggle")
async def toggle_global_visibility(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """AJAX toggle: hide/show one module from the sidebar for every user,
    admin included (see GLOBAL_VISIBILITY_MODULES / module_hidden())."""
    form = await request.form()
    module_key = (form.get("module_key") or "").strip()
    hidden = (form.get("hidden") or "").strip() == "1"

    if not module_key:
        return JSONResponse({"error": "module_key required"}, status_code=400)
    if module_key == "admin_master":
        return JSONResponse({"error": "Master Data cannot be hidden — it's where this toggle lives"},
                            status_code=400)

    setting_key = f"{GLOBAL_HIDDEN_PREFIX}{module_key}"
    existing = (await db.execute(
        select(AppSetting).where(AppSetting.key == setting_key)
    )).scalar_one_or_none()

    value = "1" if hidden else "0"
    if existing:
        existing.value = value
        existing.updated_by = current_user.username
    else:
        db.add(AppSetting(
            key=setting_key,
            value=value,
            description=f"Global sidebar visibility for {module_key}",
            updated_by=current_user.username,
        ))

    await db.commit()
    set_cached_module_hidden(module_key, hidden)
    return JSONResponse({"success": True, "module_key": module_key, "hidden": hidden})


@router.post("/add")
async def add_master_value(
    category: str = Form(...),
    value: str = Form(...),
    description: str = Form(""),
    parent: str = Form(""),          # Product Model Names: the Make (2026-11-06)
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    value = value.strip()
    if category in MASTER_SIZE_CATEGORIES:  # "12GB" -> "12 GB" (2026-10-19)
        value = space_size_unit(value)
    parent = parent.strip()[:200] or None if category in PARENT_CATEGORIES else None
    if category in PARENT_CATEGORIES and not parent:
        return RedirectResponse(url="/admin/master?error=Select+the+Make", status_code=302)
    existing = await db.execute(
        select(MasterData).where(MasterData.category == category, MasterData.value == value,
                                 func.coalesce(MasterData.parent, "") == (parent or ""))
    )
    if existing.scalar_one_or_none():
        return RedirectResponse(url=f"/admin/master?error=Value+already+exists+in+{category}", status_code=302)
    item = MasterData(category=category, value=value, description=description.strip() or None, parent=parent)
    db.add(item)
    await db.commit()
    await refresh_master_cache(db)
    cat_tab = dict((c[0], c[2]) for c in CATEGORIES).get(category, 'laptop')
    return RedirectResponse(url=f"/admin/master?success=Value+added&tab={cat_tab}", status_code=302)


@router.post("/product-model-names/reload")
async def reload_product_model_names(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Product Model Names "Reload" (2026-11-06, Pankaj): adds every Make +
    Model pair found on the tags that the list doesn't have yet. The model is
    named the way the Data Fixes "Product Model Name" card names it (capitals
    / spaces / punctuation ignored when comparing); the Make is the matching
    "Device Brands" value (else the tags' most-used spelling of it). A model
    under several Makes gets one entry per Make. Entries already in the list
    are left as they are (also if edited or disabled); nothing is deleted."""
    from sqlalchemy import func as _f
    from models.device import Device
    from services.audit_engine import audit
    from utils.model_name import OVERRIDE_CATEGORY, canonical_map, model_key, overrides_from, tidy
    cat = "product_model_name"
    rows = (await db.execute(select(Device.brand, Device.model, _f.count()).where(Device.model.isnot(None))
                             .group_by(Device.brand, Device.model))).all()
    typed = (await db.execute(select(MasterData.value).where(
        MasterData.category == OVERRIDE_CATEGORY, MasterData.is_active.isnot(False)))).scalars().all()
    names = canonical_map([(m, n) for _b, m, n in rows], overrides_from(typed))
    # Make: the Device Brands value with the same letters, else the most-used spelling on the tags
    brands = {model_key(v): v for v in (await db.execute(
        select(MasterData.value).where(MasterData.category == "brand"))).scalars().all()}
    seen_make: dict = {}
    for b, _m, n in rows:
        t = tidy(b)
        if t:
            seen_make.setdefault(model_key(t), {}).setdefault(t, 0)
            seen_make[model_key(t)][t] += int(n or 0)
    def _make(b):
        k = model_key(tidy(b) or "")
        if not k:
            return None
        return brands.get(k) or max(seen_make[k].items(), key=lambda x: x[1])[0]
    pairs = {}
    for b, m, _n in rows:
        mk = model_key(tidy(m) or "")
        if mk and names.get(mk):
            pairs[(model_key(_make(b) or ""), mk)] = (_make(b), names[mk])
    existing = (await db.execute(select(MasterData.parent, MasterData.value)
                                 .where(MasterData.category == cat))).all()
    have = {(model_key(p or ""), model_key(v)) for p, v in existing}
    added = []
    for key, (make, name) in sorted(pairs.items(), key=lambda kv: ((kv[1][0] or "~").lower(), kv[1][1].lower())):
        if key in have:
            continue
        db.add(MasterData(category=cat, value=name[:200], parent=(make or None) and make[:200], is_active=True))
        added.append(f"{make or '(no Make)'} / {name}")
        have.add(key)
    if added:
        await audit(db, user=current_user, action="MASTER_MODEL_NAMES_RELOAD", table_name="master_data",
                    record_id=f"{len(added)} names", new_value={"category": cat, "added": added[:500],
                                                                "count": len(added)},
                    notes="Product Model Names: Make + Model pairs from the tags added to Master Data.",
                    request=request)
    await db.commit()
    await refresh_master_cache(db)
    from urllib.parse import quote
    no_make = sum(1 for (pk, _k) in pairs if not pk)
    msg = (f"Product Model Names reloaded: {len(added):,} new Make + Model pair(s) added, "
           f"{len(pairs) - len(added):,} already in the list."
           + (f" {no_make:,} model(s) have tags with no Make — listed under (no Make)." if no_make else ""))
    return RedirectResponse(url=f"/admin/master?success={quote(msg)}#section-product_models", status_code=302)


@router.post("/{item_id}/edit")
async def edit_master_value(
    item_id: str,
    value: str = Form(...),
    description: str = Form(""),
    parent: str = Form(""),          # Product Model Names: the Make (2026-11-06)
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    result = await db.execute(select(MasterData).where(MasterData.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404)
    item.value = value.strip()
    if item.category in MASTER_SIZE_CATEGORIES:  # "12GB" -> "12 GB" (2026-10-19)
        item.value = space_size_unit(item.value)
    item.description = description.strip() or None
    if item.category in PARENT_CATEGORIES and parent.strip():
        item.parent = parent.strip()[:200]
    await db.commit()
    await refresh_master_cache(db)
    cat_tab = dict((c[0], c[2]) for c in CATEGORIES).get(item.category, 'laptop')
    return RedirectResponse(url=f"/admin/master?success=Updated&tab={cat_tab}", status_code=302)


@router.post("/{item_id}/toggle")
async def toggle_master_value(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    result = await db.execute(select(MasterData).where(MasterData.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404)
    item.is_active = not item.is_active
    await db.commit()
    await refresh_master_cache(db)
    cat_tab = dict((c[0], c[2]) for c in CATEGORIES).get(item.category, 'laptop')
    return RedirectResponse(url=f"/admin/master?success=Updated&tab={cat_tab}", status_code=302)


@router.post("/{item_id}/delete")
async def delete_master_value(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    result = await db.execute(select(MasterData).where(MasterData.id == item_id))
    item = result.scalar_one_or_none()
    cat_tab = "laptop"
    if item:
        cat_tab = dict((c[0], c[2]) for c in CATEGORIES).get(item.category, 'laptop')
    await db.execute(delete(MasterData).where(MasterData.id == item_id))
    await db.commit()
    await refresh_master_cache(db)
    return RedirectResponse(url=f"/admin/master?success=Deleted&tab={cat_tab}", status_code=302)


@router.get("/export/{category}")
async def export_category_csv(category: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(admin_only)):
    result = await db.execute(
        select(MasterData)
        .where(MasterData.category == category)
        .order_by(MasterData.display_order, MasterData.value)
    )
    items = result.scalars().all()
    output = io.StringIO()
    writer = csv.writer(output)
    linked = category in PARENT_CATEGORIES        # Product Model Names: + Make (2026-11-06)
    writer.writerow((["make"] if linked else []) + ["value", "description", "is_active", "display_order"])
    for item in items:
        writer.writerow(([item.parent or ""] if linked else [])
                        + [item.value, item.description or "", "yes" if item.is_active else "no", item.display_order])
    output.seek(0)
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=master_{category}.csv"}
    )


@router.get("/template/{category}")
async def download_category_template(category: str, current_user: User = Depends(admin_only)):
    output = io.StringIO()
    writer = csv.writer(output)
    if category in PARENT_CATEGORIES:
        writer.writerow(["make", "value", "description"])
        writer.writerow(["Lenovo", "ThinkPad T14 Gen 1", "Optional description"])
    else:
        writer.writerow(["value", "description"])
        writer.writerow(["Example Value", "Optional description"])
    output.seek(0)
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=template_{category}.csv"}
    )


@router.post("/bulk-upload/{category}")
async def bulk_upload_category(
    category: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    # Validate category
    valid_cats = [c[0] for c in CATEGORIES]
    if category not in valid_cats:
        raise HTTPException(400, "Invalid category")

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except Exception:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    added = 0
    skipped = 0

    for row in reader:
        value = (row.get("value") or "").strip()
        if category in MASTER_SIZE_CATEGORIES:
            value = space_size_unit(value)
        if not value:
            continue
        description = (row.get("description") or "").strip() or None
        parent = ((row.get("make") or "").strip()[:200] or None) if category in PARENT_CATEGORIES else None

        existing = await db.execute(
            select(MasterData).where(MasterData.category == category, MasterData.value == value,
                                     func.coalesce(MasterData.parent, "") == (parent or ""))
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue

        item = MasterData(category=category, value=value, description=description, parent=parent)
        db.add(item)
        added += 1

    await db.commit()
    await refresh_master_cache(db)
    cat_tab = dict((c[0], c[2]) for c in CATEGORIES).get(category, 'laptop')
    return RedirectResponse(
        url=f"/admin/master?success={added}+values+added,+{skipped}+skipped&tab={cat_tab}",
        status_code=302
    )


@router.get("/api/{category}", response_class=JSONResponse)
async def get_master_values(category: str, parent: str = "", db: AsyncSession = Depends(get_db),
                            current_user: User = Depends(get_current_user)):
    """Active values of a dropdown. ?parent=<Make> (2026-11-06) lists only the
    values linked to that parent — e.g. /admin/master/api/product_model_name?parent=Lenovo."""
    if parent.strip():
        from utils.master_data import master_children
        return JSONResponse({"values": master_children(category, parent)})
    result = await db.execute(
        select(MasterData.value)
        .where(MasterData.category == category, MasterData.is_active == True)
        .order_by(MasterData.display_order, MasterData.value)
    )
    vals = []
    for (v,) in result.all():
        if v not in vals:
            vals.append(v)
    return JSONResponse({"values": vals})


# ── Permission Matrix Routes ──────────────────────────────────────────────────

@router.get("/permissions", response_class=HTMLResponse)
async def permissions_matrix(
    request: Request,
    role: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Render the Module Permissions tab (called via HTMX/JS or direct nav)."""
    # Same role list + display labels as the Add User page.
    all_roles, _ = await _role_data(db)

    selected_role = role or (all_roles[1][0] if len(all_roles) > 1 else "")

    # Load existing permissions for this role
    perm_rows = {}
    if selected_role:
        rows = (await db.execute(
            select(RoleModulePermission)
            .where(RoleModulePermission.role_name == selected_role)
        )).scalars().all()
        perm_rows = {r.module: r for r in rows}

    custom_roles_q2 = await db.execute(select(CustomRole).order_by(CustomRole.display_name))
    custom_roles_list = custom_roles_q2.scalars().all()

    return templates.TemplateResponse("admin/master.html", {
        "request": request,
        "current_user": current_user,
        # Dropdown config tab data (needed for base template render)
        "grouped": {},
        "categories": CATEGORIES,
        # The Dropdown Config script needs this on every render (2026-11-08:
        # its absence made this Permissions page a 500).
        "parent_categories": PARENT_CATEGORIES,
        "tabs": TABS,
        "accordion_data": [],
        # Permission tab data
        "active_main_tab": "permissions",
        "all_roles": all_roles,
        "selected_role": selected_role,
        "perm_rows": perm_rows,
        "default_disabled_modules": DEFAULT_DISABLED_MODULES,
        "perm_modules": perm_modules_in_sidebar_order(),
        "perm_module_names": perm_module_display_names(),
        "perm_actions": PERM_ACTIONS,
        "module_actions": module_action_map(),
        "custom_roles_list": custom_roles_list,
    })


@router.get("/permissions/load", response_class=JSONResponse)
async def load_role_permissions(
    role: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Return permission matrix for a role as JSON (used by JS when user changes role dropdown)."""
    rows = (await db.execute(
        select(RoleModulePermission).where(RoleModulePermission.role_name == role)
    )).scalars().all()
    data = {
        r.module: {
            "can_enable": r.can_enable,
            "can_add":    r.can_add,
            "can_edit":   r.can_edit,
            "can_upload": r.can_upload,
            "can_delete": True if r.can_delete is None else r.can_delete,
            "can_pricing": True if r.can_pricing is None else r.can_pricing,
            # Not set yet -> follows Pricing View (2026-11-03)
            "can_pricing_add": (True if r.can_pricing is None else r.can_pricing)
                               if r.can_pricing_add is None else r.can_pricing_add,
            "can_pricing_export": (True if r.can_pricing is None else r.can_pricing)
                                  if r.can_pricing_export is None else r.can_pricing_export,
            "can_export": True if r.can_export is None else r.can_export,
        }
        for r in rows
    }
    return JSONResponse({"perms": data})


@router.post("/permissions/save")
async def save_role_permissions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Save the full permission matrix for one role. Replaces existing rows."""
    form = await request.form()
    role_name = (form.get("role_name") or "").strip()
    if not role_name:
        return RedirectResponse(url="/admin/master?main_tab=permissions&error=Role+name+required", status_code=302)

    # Delete only this tab's own module rows for the role — scoped to
    # PERM_MODULES keys so saving here for role_name='sub_admin' can never wipe
    # the synthetic 'admin_settings' row the Sub Admin Role tab (Tab 5) owns.
    await db.execute(delete(RoleModulePermission).where(
        RoleModulePermission.role_name == role_name,
        RoleModulePermission.module.in_([m for m, _ in PERM_MODULES]),
    ))

    # Re-insert from form — checkboxes only present when checked
    new_perms: dict = {}
    for mod_key, _mod_label in PERM_MODULES:
        can_enable = f"perm_{mod_key}_can_enable" in form
        can_add    = f"perm_{mod_key}_can_add"    in form
        can_edit   = f"perm_{mod_key}_can_edit"   in form
        can_upload = f"perm_{mod_key}_can_upload" in form
        can_delete  = f"perm_{mod_key}_can_delete"  in form
        can_pricing = f"perm_{mod_key}_can_pricing" in form
        can_pricing_add = f"perm_{mod_key}_can_pricing_add" in form
        can_pricing_export = f"perm_{mod_key}_can_pricing_export" in form
        can_export  = f"perm_{mod_key}_can_export"  in form
        db.add(RoleModulePermission(
            role_name  = role_name,
            module     = mod_key,
            can_enable = can_enable,
            can_add    = can_add,
            can_edit   = can_edit,
            can_upload = can_upload,
            can_delete  = can_delete,
            can_pricing = can_pricing,
            can_pricing_add = can_pricing_add,
            can_pricing_export = can_pricing_export,
            can_export  = can_export,
            updated_by = current_user.username,
        ))
        new_perms[mod_key] = {
            "enable": can_enable, "add": can_add,
            "edit":   can_edit,   "upload": can_upload,
            "delete": can_delete, "pricing": can_pricing,
            "pricing_add": can_pricing_add, "pricing_export": can_pricing_export,
            "export": can_export,
        }

    await db.commit()

    # Refresh in-memory cache so enforcement takes effect immediately. Preserve
    # any cached keys outside PERM_MODULES (e.g. the synthetic 'admin_settings'
    # row the Sub Admin Role tab owns for role_name='sub_admin') — this save
    # only touched PERM_MODULES rows, so a flat overwrite would otherwise evict
    # them from memory even though their DB rows were left untouched.
    perm_module_keys = {m for m, _ in PERM_MODULES}
    preserved = {k: v for k, v in get_cached_perms(role_name).items() if k not in perm_module_keys}
    set_cached_perms(role_name, {**preserved, **new_perms})

    return RedirectResponse(
        url=f"/admin/master?main_tab=permissions&role={role_name}&success=Permissions+saved+for+{role_name}",
        status_code=302,
    )


@router.post("/permissions/save-additional")
async def save_additional_permissions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Save the Role Additional Permissions row for one role (upsert)."""
    form = await request.form()
    role_name = (form.get("role_name") or "").strip()
    if not role_name:
        return RedirectResponse(
            url="/admin/master?main_tab=additional_permissions&error=Role+name+required", status_code=302
        )

    values = {key: (f"add_perm_{key}" in form) for key, _label in ADDITIONAL_PERMS}

    row = (await db.execute(
        select(RoleAdditionalPermission).where(RoleAdditionalPermission.role_name == role_name)
    )).scalar_one_or_none()
    if row:
        for key, val in values.items():
            setattr(row, key, val)
        row.updated_by = current_user.username
    else:
        db.add(RoleAdditionalPermission(role_name=role_name, updated_by=current_user.username, **values))

    await db.commit()

    # Preserve can_view_pricing / can_set_price — this form doesn't include them
    # (that's the separate Pricing Visibility tab), so re-read it from the
    # row rather than defaulting it back to True on every save here.
    refreshed = (await db.execute(
        select(RoleAdditionalPermission).where(RoleAdditionalPermission.role_name == role_name)
    )).scalar_one()
    set_cached_additional_perms(role_name, {
        **{key.removeprefix("can_"): val for key, val in values.items()},
        "view_pricing": refreshed.can_view_pricing,
        # Same for Set Price (also on the Pricing Visibility tab).
        "set_price": True if refreshed.can_set_price is None else bool(refreshed.can_set_price),
    })

    return RedirectResponse(
        url=f"/admin/master?main_tab=additional_permissions&role={role_name}&success=Additional+permissions+saved+for+{role_name}",
        status_code=302,
    )


# (Pricing Visibility tab save route removed 2026-10-26 — merged into the
# Permission Matrix Pricing tick; see main._merge_pricing_visibility_once.)


@router.post("/permissions/save-sub-admin")
async def save_sub_admin_permissions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Sub Admin Role tab (item 7): save the enable-matrix for the fixed
    'sub_admin' role across SUB_ADMIN_MODULES (regular modules + the admin-only
    surfaces). Hardwired to role_name='sub_admin' — this tab never touches any
    other role. Replaces existing rows, same as the general matrix save."""
    form = await request.form()

    await db.execute(delete(RoleModulePermission).where(RoleModulePermission.role_name == SUB_ADMIN_ROLE))

    new_perms: dict = {}
    for mod_key, _mod_label in SUB_ADMIN_MODULES:
        can_enable = f"perm_{mod_key}_can_enable" in form
        db.add(RoleModulePermission(
            role_name  = SUB_ADMIN_ROLE,
            module     = mod_key,
            can_enable = can_enable,
            can_add    = can_enable,
            can_edit   = can_enable,
            can_upload = can_enable,
            can_delete  = can_enable,
            can_pricing = can_enable,
            can_pricing_add = can_enable,
            can_pricing_export = can_enable,
            can_export  = can_enable,
            updated_by = current_user.username,
        ))
        new_perms[mod_key] = {
            "enable": can_enable, "add": can_enable,
            "edit":   can_enable, "upload": can_enable,
            "delete": can_enable, "pricing": can_enable,
            "pricing_add": can_enable, "pricing_export": can_enable,
            "export": can_enable,
        }

    await db.commit()
    set_cached_perms(SUB_ADMIN_ROLE, new_perms)

    return RedirectResponse(
        url="/admin/master?main_tab=sub_admin&success=Sub+Admin+Role+permissions+saved",
        status_code=302,
    )


@router.post("/permissions/add-role")
async def add_custom_role(
    request: Request,
    role_name: str = Form(...),
    display_name: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Create a new custom role."""
    # Sanitise role_name to snake_case
    import re
    clean = re.sub(r"[^a-z0-9_]", "_", role_name.strip().lower())
    if not clean:
        return RedirectResponse(url="/admin/master?main_tab=permissions&error=Invalid+role+name", status_code=302)

    if clean in {r.value for r in UserRole}:
        return RedirectResponse(url=f"/admin/master?main_tab=permissions&error=Role+{clean}+already+exists", status_code=302)

    existing = (await db.execute(select(CustomRole).where(CustomRole.role_name == clean))).scalar_one_or_none()
    if existing:
        return RedirectResponse(url=f"/admin/master?main_tab=permissions&error=Role+{clean}+already+exists", status_code=302)

    db.add(CustomRole(role_name=clean, display_name=display_name.strip(), created_by=current_user.username))
    await db.commit()
    return RedirectResponse(
        url=f"/admin/master?main_tab=permissions&role={clean}&success=Role+{clean}+created",
        status_code=302,
    )


@router.post("/permissions/edit-role/{role_id}")
async def edit_custom_role(
    request: Request,
    role_id: str,
    role_name: str = Form(...),
    display_name: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Rename a custom role's slug and/or display name. Cascades the slug
    change to RoleModulePermission rows and any User accounts on that role,
    and re-keys the in-memory permission cache. Built-in roles (UserRole
    enum members) are not stored in CustomRole and cannot be edited here."""
    import re
    custom_role = (await db.execute(select(CustomRole).where(CustomRole.id == role_id))).scalar_one_or_none()
    if not custom_role:
        return RedirectResponse(url="/admin/master?main_tab=permissions&error=Role+not+found", status_code=302)

    old_name = custom_role.role_name
    clean = re.sub(r"[^a-z0-9_]", "_", role_name.strip().lower())
    if not clean:
        return RedirectResponse(url="/admin/master?main_tab=permissions&error=Invalid+role+name", status_code=302)

    if clean != old_name:
        if clean in {r.value for r in UserRole}:
            return RedirectResponse(url=f"/admin/master?main_tab=permissions&error=Role+{clean}+already+exists", status_code=302)
        dupe = (await db.execute(
            select(CustomRole).where(CustomRole.role_name == clean, CustomRole.id != role_id)
        )).scalar_one_or_none()
        if dupe:
            return RedirectResponse(url=f"/admin/master?main_tab=permissions&error=Role+{clean}+already+exists", status_code=302)

    custom_role.role_name = clean
    custom_role.display_name = display_name.strip()

    if clean != old_name:
        # Cascade the slug rename to permission rows and any assigned users.
        await db.execute(
            update(RoleModulePermission).where(RoleModulePermission.role_name == old_name).values(role_name=clean)
        )
        await db.execute(update(User).where(User.role == old_name).values(role=clean))
        if old_name in _PERM_CACHE:
            _PERM_CACHE[clean] = _PERM_CACHE.pop(old_name)

    await db.commit()
    return RedirectResponse(
        url=f"/admin/master?main_tab=permissions&role={clean}&success=Role+updated",
        status_code=302,
    )


async def load_all_permissions_to_cache(db: AsyncSession) -> None:
    """Called on startup to warm the permission cache from DB."""
    rows = (await db.execute(select(RoleModulePermission))).scalars().all()
    tmp: dict = {}
    for r in rows:
        tmp.setdefault(r.role_name, {})[r.module] = {
            "enable": r.can_enable, "add": r.can_add,
            "edit":   r.can_edit,   "upload": r.can_upload,
            "delete": r.can_delete, "pricing": r.can_pricing,
            "pricing_add": r.can_pricing_add, "pricing_export": r.can_pricing_export,
            "export": r.can_export,
        }
    _PERM_CACHE.clear()
    _PERM_CACHE.update(tmp)

    additional_rows = (await db.execute(select(RoleAdditionalPermission))).scalars().all()
    tmp_add: dict = {}
    for r in additional_rows:
        tmp_add[r.role_name] = {
            **{key.removeprefix("can_"): getattr(r, key) for key, _label in ADDITIONAL_PERMS},
            "view_pricing": r.can_view_pricing,
            "set_price": True if r.can_set_price is None else bool(r.can_set_price),
        }
    _ADDITIONAL_PERM_CACHE.clear()
    _ADDITIONAL_PERM_CACHE.update(tmp_add)
