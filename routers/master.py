import csv
import io
from templates_config import templates
from fastapi import APIRouter, Depends, Form, Request, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update
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
            "crm_priority", "crm_activity_type", "crm_activity_outcome",
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
    ("workid_status",        "WorkID Status"),
    # ── STORE MANAGER ──────────────────────────────────────────────
    ("parts_dashboard",      "Parts Dashboard"),
    ("spare_parts",          "Parts Manager"),
    ("parts_sale_request",   "Parts Sale Request"),
    ("spare_parts_purchase", "Parts Purchased"),
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
    ("qa",                   "QA Dashboard"),
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
    ("can_pricing", "Pricing"),
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
            out.setdefault(m, set()).add(action)
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
    ("warehouse",           "Warehouses / Zones",           "inventory"),
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
]

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
        grouped[cat_key] = {
            "label": cat_label,
            "tab": cat_tab,
            "items": [i for i in items if i.category == cat_key],
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
        "tabs": TABS,
        "accordion_data": accordion_data,
        "current_user": current_user,
        # Permission tab data
        "all_roles": all_roles,
        "selected_role": selected_role,
        "perm_rows": perm_rows,
        "default_disabled_modules": DEFAULT_DISABLED_MODULES,
        "perm_modules": PERM_MODULES,
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
    })


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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    value = value.strip()
    if category in MASTER_SIZE_CATEGORIES:  # "12GB" -> "12 GB" (2026-10-19)
        value = space_size_unit(value)
    existing = await db.execute(
        select(MasterData).where(MasterData.category == category, MasterData.value == value)
    )
    if existing.scalar_one_or_none():
        return RedirectResponse(url=f"/admin/master?error=Value+already+exists+in+{category}", status_code=302)
    item = MasterData(category=category, value=value, description=description.strip() or None)
    db.add(item)
    await db.commit()
    await refresh_master_cache(db)
    cat_tab = dict((c[0], c[2]) for c in CATEGORIES).get(category, 'laptop')
    return RedirectResponse(url=f"/admin/master?success=Value+added&tab={cat_tab}", status_code=302)


@router.post("/{item_id}/edit")
async def edit_master_value(
    item_id: str,
    value: str = Form(...),
    description: str = Form(""),
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
    writer.writerow(["value", "description", "is_active", "display_order"])
    for item in items:
        writer.writerow([item.value, item.description or "", "yes" if item.is_active else "no", item.display_order])
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

        existing = await db.execute(
            select(MasterData).where(MasterData.category == category, MasterData.value == value)
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue

        item = MasterData(category=category, value=value, description=description)
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
async def get_master_values(category: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = await db.execute(
        select(MasterData.value)
        .where(MasterData.category == category, MasterData.is_active == True)
        .order_by(MasterData.display_order, MasterData.value)
    )
    return JSONResponse({"values": [r[0] for r in result.all()]})


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
        "tabs": TABS,
        "accordion_data": [],
        # Permission tab data
        "active_main_tab": "permissions",
        "all_roles": all_roles,
        "selected_role": selected_role,
        "perm_rows": perm_rows,
        "default_disabled_modules": DEFAULT_DISABLED_MODULES,
        "perm_modules": PERM_MODULES,
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
            can_export  = can_export,
            updated_by = current_user.username,
        ))
        new_perms[mod_key] = {
            "enable": can_enable, "add": can_add,
            "edit":   can_edit,   "upload": can_upload,
            "delete": can_delete, "pricing": can_pricing,
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


@router.post("/permissions/save-pricing-visibility")
async def save_pricing_visibility(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(admin_only),
):
    """Pricing Visibility tab (Batch 9) — bulk-save can_view_pricing for every
    non-admin role in one submit, matching this page's other tabs' plain-form
    convention. Upserts RoleAdditionalPermission rows without touching their
    other fields (ADDITIONAL_PERMS)."""
    form = await request.form()
    all_roles, _ = await _role_data(db)

    existing_rows = {
        r.role_name: r for r in (await db.execute(select(RoleAdditionalPermission))).scalars().all()
    }

    for role_val, _label in all_roles:
        if role_val == "admin":
            continue
        new_val = f"pricing_{role_val}" in form
        set_price_val = f"setprice_{role_val}" in form
        row = existing_rows.get(role_val)
        if row:
            row.can_view_pricing = new_val
            row.can_set_price = set_price_val
            row.updated_by = current_user.username
        else:
            db.add(RoleAdditionalPermission(role_name=role_val, can_view_pricing=new_val,
                                             can_set_price=set_price_val,
                                             updated_by=current_user.username))

        cached = dict(_ADDITIONAL_PERM_CACHE.get(role_val) or {
            key.removeprefix("can_"): True for key, _label in ADDITIONAL_PERMS
        })
        cached["view_pricing"] = new_val
        cached["set_price"] = set_price_val
        set_cached_additional_perms(role_val, cached)

    await db.commit()
    return RedirectResponse(
        url="/admin/master?main_tab=pricing_visibility&success=Pricing+visibility+saved", status_code=302)


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
            can_export  = can_enable,
            updated_by = current_user.username,
        ))
        new_perms[mod_key] = {
            "enable": can_enable, "add": can_enable,
            "edit":   can_enable, "upload": can_enable,
            "delete": can_enable, "pricing": can_enable,
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
