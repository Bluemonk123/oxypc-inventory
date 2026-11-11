"""Quick Add — the top-bar "+" menu (2026-11-08, Pankaj).

Every module's Add action in one place. An item shows only when the user's
role may open the module (Permission Matrix "enable", module not hidden) AND
may add in it (same check as the page's own Add button: can(user, module,
'add')). No items → no "+" button.

url: a page of its own ("/lots/new"), or a list page whose Add opens a
modal — "#add=<button text>" makes static/js/quick-add.js click that page's
Add button once it has loaded.

Names follow the sidebar (2026-11-08): each item belongs to a sidebar link
(its list page — the url without "/new" or "#add=…", or SIDE below). In the
browser, quick-add.js takes that link's current label, icon and section from
the sidebar, so renaming a sidebar item (Sidebar Config) renames it here too.
The label / group below are only fallbacks. Items listed in KEEP_LABEL have
no sidebar link of their own (QA pages reached through UAT): they keep their
label and take the section from SIDE.
"""

# (group, [(label, icon, url, module, action), ...])
CATALOG = [
    ("Intake", [
        ("GRN with Invoice", "bi-receipt", "/grn/new", "grn", "add"),
        ("GRN post IQC", "bi-clipboard2-check", "/grn/post-iqc#add=Add GRN", "grn_post_iqc", "add"),
        ("Lot", "bi-box-seam", "/lots/new", "lots", "add"),
        ("IQC Asset", "bi-clipboard-check", "/iqc/new", "iqc", "add"),
    ]),
    ("Inventory", [
        ("Transfer", "bi-arrow-left-right", "/transfers/new", "transfers", "add"),
        ("Model Request", "bi-question-square", "/model-requests/new", "model_requests", "add"),
        ("Block Tags", "bi-slash-circle", "/block-tags#add=Add Block Tags", "block_tags", "add"),
        ("Location", "bi-geo-alt", "/locations/master#add=Add Location", "location_master", "add"),
    ]),
    ("Sales", [
        ("Sale", "bi-cart-check", "/sales/new", "sales", "add"),
        ("Return", "bi-arrow-return-left", "/returns/new", "returns", "add"),
        ("Rental", "bi-calendar2-range", "/rentals/new", "rentals", "add"),
        ("Parts Sale", "bi-bag", "/part-sales/new", "part_sales", "add"),
        ("Market Entry", "bi-graph-up", "/market#add=Add Entry", "market", "add"),
    ]),
    ("Spare Parts", [
        ("Spare Part", "bi-cpu", "/spare-parts#add=Add Part", "spare_parts", "add"),
        ("Parts GRN", "bi-receipt-cutoff", "/parts-grn/new", "spare_parts_purchase", "add"),
    ]),
    ("CRM", [
        ("Account", "bi-person-vcard", "/crm/contacts/new", "crm_contacts", "add"),
        ("Sourcing Deal", "bi-box-arrow-in-down", "/crm/sourcing/new", "crm_sourcing", "add"),
        ("Sales Opportunity", "bi-box-arrow-up", "/crm/sales/new", "crm_sales_opp", "add"),
        ("Quote", "bi-file-earmark-text", "/crm/quotes/new", "crm_quotes", "add"),
        ("Purchase Order", "bi-file-earmark-check", "/crm/purchase-orders/new", "crm_purchase_orders", "add"),
        ("Price Matrix Row", "bi-grid-3x3", "/crm/price-matrix/new", "crm_price_matrix", "pricing_add"),
        ("Lead Group", "bi-people", "/crm/assign-social-leads#add=Add Lead Group", "crm_assign_leads", "add"),
    ]),
    ("Dealers", [
        ("Dealer", "bi-shop", "/dealers#add=Add Dealer", "dealers", "add"),
        ("Dealer Quotation", "bi-file-earmark-ruled", "/quotations#add=Add Quote", "dealers", "add"),
    ]),
    ("Trade Partner", [
        ("Listing", "bi-megaphone", "/trade-partner/listings#add=Add Listing", "trade_partner_listings", "add"),
        ("Lot GRN", "bi-box-seam", "/trade-partner/manage-lots#add=Add GRN", "trade_partner_manage_lots", "add"),
    ]),
    ("Product Master", [
        ("Product", "bi-laptop", "/product-master#add=Add Product", "pm_product_master", "add"),
    ]),
    ("QA", [
        ("Release", "bi-rocket-takeoff", "/qa/releases#add=Add Release", "qa", "add"),
        ("Requirement", "bi-list-check", "/qa/requirements#add=Add Requirement", "qa_uat", "add"),
        ("Test Case", "bi-ui-checks", "/qa/test-cases#add=Add Test Case", "qa_uat", "add"),
        ("UAT Scenario", "bi-person-check", "/qa/uat#add=Add UAT Scenario", "qa_uat", "add"),
    ]),
    ("Admin", [
        ("User", "bi-person-plus", "/admin/users/new", "admin_users", "add"),
        ("Attendance Group", "bi-people", "/admin/attendance-config#add=Add Group", "attendance_config", "add"),
        ("Policy", "bi-file-text", "/admin/terms-conditions#add=Add Policy", "terms_conditions", "add"),
    ]),
]


# Sidebar link for items whose list page isn't simply url minus "/new" / "#add=…".
SIDE = {
    "/qa/releases#add=Add Release": "/qa/uat",
    "/qa/requirements#add=Add Requirement": "/qa/uat",
    "/qa/test-cases#add=Add Test Case": "/qa/uat",
}
KEEP_LABEL = set(SIDE)


def _side(url: str) -> str:
    if url in SIDE:
        return SIDE[url]
    base = url.split("#", 1)[0]
    return base[:-4] if base.endswith("/new") else base


def quick_add_menu(current_user, can, has_perm, module_hidden) -> list:
    """[(group, [item dict, ...]), ...] — only what this user may add."""
    if not current_user:
        return []
    role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    out = []
    for group, items in CATALOG:
        keep = [
            {"label": label, "icon": icon, "url": url, "side": _side(url),
             "keep": url in KEEP_LABEL}
            for label, icon, url, module, action in items
            if has_perm(role, module, "enable") and not module_hidden(module)
            and can(current_user, module, action)
        ]
        if keep:
            out.append((group, keep))
    return out
