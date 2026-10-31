"""
OxyPC Inventory — UAT Scenario Suite
Static analysis of source files; no live server required.

Run: pytest tests/test_uat.py -v --tb=short
Timing: pytest tests/test_uat.py -v --durations=0
"""

import os
import re
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def src(path: str) -> str:
    """Read a source file relative to project root."""
    full = os.path.join(ROOT, path)
    with open(full, encoding="utf-8") as fh:
        return fh.read()


def exists(path: str) -> bool:
    """Return True if the path exists under project root."""
    return os.path.exists(os.path.join(ROOT, path))


def _linked_from_any_template(url: str) -> bool:
    """True if any template contains an <a href> pointing at `url`.

    Matches href specifically, not a bare substring: a URL can appear in an
    active-state condition (`{% if '/dealers/overdue' in request.url.path %}`)
    without being a link, which makes a substring check pass while the page is
    actually unreachable. Searches all templates because navigation is not
    confined to base.html — several pages are reached from dashboard buttons.
    """
    pattern = re.compile(r'href\s*=\s*["\']' + re.escape(url) + r'(?:[/?#"\']|$)')
    tpl_root = os.path.join(ROOT, "templates")
    for dirpath, _dirnames, filenames in os.walk(tpl_root):
        for name in filenames:
            if not name.endswith(".html"):
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                if pattern.search(fh.read()):
                    return True
    return False


def has_route(router_src: str, method: str, pattern: str) -> bool:
    """
    Return True if the router source contains a route decorator matching
    the HTTP method and a path substring / regex pattern.
    """
    # Look for @router.<method>("<pattern>" …) — case-insensitive on path
    regex = rf'@router\.{method}\s*\(["\'][^"\']*{re.escape(pattern)}[^"\']*["\']'
    return bool(re.search(regex, router_src, re.IGNORECASE))


def has_any_route(router_src: str, method: str) -> bool:
    """Return True if any route of the given method exists."""
    regex = rf'@router\.{method}\s*\('
    return bool(re.search(regex, router_src))


# ---------------------------------------------------------------------------
# UAT-01: Authentication
# ---------------------------------------------------------------------------
class TestUAT01Authentication:

    def test_login_get_route_exists(self):
        """UAT-01-01: GET /auth/login route exists in auth router."""
        content = src("routers/auth.py")
        assert '@router.get("/login' in content, "GET /auth/login route not found"

    def test_login_post_route_exists(self):
        """UAT-01-02: POST /auth/login route exists in auth router."""
        content = src("routers/auth.py")
        assert '@router.post("/login' in content, "POST /auth/login route not found"

    def test_login_template_exists(self):
        """UAT-01-03: Login template file exists."""
        assert exists("templates/login.html"), "templates/login.html not found"

    def test_password_hashed_with_bcrypt(self):
        """UAT-01-04: bcrypt used for password hashing in auth dependencies.

        Was asserting passlib + CryptContext, which the code has not used for some
        time — so this test failed against correct code and told us nothing. It now
        checks what actually protects passwords: bcrypt's own hashpw/checkpw.
        """
        content = src("auth/dependencies.py")
        assert "bcrypt" in content, "bcrypt not imported in auth/dependencies.py"
        assert "checkpw" in content, "bcrypt.checkpw not used — password verification not configured"
        assert "hashpw" in content, "bcrypt.hashpw not used — password hashing not configured"

    def test_session_cookie_set_on_login(self):
        """UAT-01-05: access_token cookie set in login handler."""
        content = src("routers/auth.py")
        assert "set_cookie" in content and "access_token" in content, (
            "access_token cookie not set in login handler"
        )

    def test_rate_limit_on_login(self):
        """UAT-01-06: login is rate limited, and keyed on IP rather than cookie.

        Previously asserted the literal "5/minute". The limit was deliberately
        raised to 20/minute (commit 9e1a1ff) because staff share one NAT'd office
        IP and were being locked out, so the test failed against correct code and
        would fail again on any future tuning.

        What actually matters is asserted instead: a limit exists, and it keys on
        the client IP. Keying on the session cookie would void the protection —
        an attacker can mint a fresh cookie per attempt and land in a new bucket
        every time, which is exactly why limiter.ip_key_func exists.
        """
        content = src("routers/auth.py")
        login_pos = content.find('@router.post("/login")')
        assert login_pos != -1, "POST /login handler not found in routers/auth.py"
        # Decorators sit between the route decorator and the function body.
        window = content[login_pos:login_pos + 400]
        assert "@limiter.limit(" in window, "login endpoint is not rate limited"
        assert "ip_key_func" in window, (
            "login rate limit is not keyed on client IP — a cookie-keyed bucket "
            "can be rotated by an attacker and provides no brute-force protection"
        )


# ---------------------------------------------------------------------------
# UAT-02: Dashboard
# ---------------------------------------------------------------------------
class TestUAT02Dashboard:

    def test_dashboard_route_exists(self):
        """UAT-02-01: GET / dashboard route exists."""
        content = src("routers/dashboard.py")
        # Route may have extra kwargs: @router.get("/", response_class=...)
        assert re.search(r'@router\.get\s*\(\s*["\']/', content), (
            "Dashboard GET / route not found"
        )

    def test_dashboard_template_exists(self):
        """UAT-02-02: dashboard.html template exists."""
        assert exists("templates/dashboard.html"), "templates/dashboard.html not found"

    def test_stage_counts_in_template(self):
        """UAT-02-03: stage counts referenced in dashboard template.
        2026-10-31: the pipeline renders pipeline_counts / side_counts (both
        built from the stage counts); its Total Inventory uses total_devices."""
        content = src("templates/dashboard.html")
        assert "pipeline_counts" in content and "side_counts" in content, \
            "stage counts not referenced in dashboard.html"

    def test_user_queue_in_template(self):
        """UAT-02-04: user_queue variable referenced in dashboard template (role-specific banners)."""
        content = src("templates/dashboard.html")
        assert "user_queue" in content, "user_queue not referenced in dashboard.html"

    def test_role_based_blocks_present(self):
        """UAT-02-05: Role-based work queue logic present in dashboard router."""
        # Sprint 18: role-based block moved from template to router (ROLE_STAGE_MAP)
        content = src("routers/dashboard.py")
        assert "qc_inspector" in content, "qc_inspector role mapping missing in dashboard router"

    def test_dashboard_role_guard(self):
        """UAT-02-06: get_current_user dependency injected in dashboard router."""
        content = src("routers/dashboard.py")
        assert "get_current_user" in content, (
            "get_current_user not used in dashboard router — unauthenticated access possible"
        )


# ---------------------------------------------------------------------------
# UAT-03: IQC (Incoming Quality Control)
# ---------------------------------------------------------------------------
class TestUAT03IQC:

    def test_iqc_list_route_exists(self):
        """UAT-03-01: GET /iqc list route exists."""
        content = src("routers/iqc.py")
        assert has_any_route(content, "get"), "No GET route found in iqc router"

    def test_iqc_process_post_route_exists(self):
        """UAT-03-02: POST route for IQC processing exists."""
        content = src("routers/iqc.py")
        assert has_any_route(content, "post"), "No POST route found in iqc router"

    def test_iqc_templates_directory_has_files(self):
        """UAT-03-03: templates/iqc/ directory contains at least one HTML template."""
        iqc_dir = os.path.join(ROOT, "templates", "iqc")
        assert os.path.isdir(iqc_dir), "templates/iqc/ directory does not exist"
        html_files = [f for f in os.listdir(iqc_dir) if f.endswith(".html")]
        assert len(html_files) >= 1, "No HTML templates found in templates/iqc/"

    def test_iqc_role_guard_present(self):
        """UAT-03-04: get_current_user role guard in iqc router."""
        content = src("routers/iqc.py")
        assert "get_current_user" in content, (
            "get_current_user not used in iqc router — unauthenticated access possible"
        )


# ---------------------------------------------------------------------------
# UAT-04: Stock Management
# ---------------------------------------------------------------------------
class TestUAT04Stock:

    def test_stock_in_route_exists(self):
        """UAT-04-01: Stock-in route exists in stock router."""
        content = src("routers/stock.py")
        assert has_any_route(content, "get") or has_any_route(content, "post"), (
            "No routes found in stock router"
        )

    def test_stock_templates_exist(self):
        """UAT-04-02: Stock-in template exists."""
        assert exists("templates/lots/stock_in.html") or exists("templates/lots/list.html"), (
            "Stock templates (lots/stock_in.html or lots/list.html) not found"
        )

    def test_csrf_in_stock_router(self):
        """UAT-04-03: verify_csrf present in stock router."""
        content = src("routers/stock.py")
        assert "verify_csrf" in content, "verify_csrf not found in stock router"


# ---------------------------------------------------------------------------
# UAT-05: Repair (L1/L2/L3)
# ---------------------------------------------------------------------------
class TestUAT05Repair:

    @pytest.mark.parametrize("level", ["l1", "l2", "l3"])
    def test_repair_level_route_exists(self, level):
        """UAT-05-01/02/03: Repair L1/L2/L3 routes exist in repair router."""
        content = src("routers/repair.py")
        assert level in content.lower(), (
            f"Repair level '{level}' not referenced in repair router"
        )

    @pytest.mark.parametrize("template", [
        "templates/repair/l1.html",
        "templates/repair/l2.html",
        "templates/repair/l3.html",
    ])
    def test_repair_templates_exist(self, template):
        """UAT-05-04/05/06: L1/L2/L3 repair templates exist."""
        assert exists(template), f"{template} not found"

    def test_stage_movement_logged_in_repair(self):
        """UAT-05-07: StageMovement model used in repair router (stage transitions logged)."""
        content = src("routers/repair.py")
        assert "StageMovement" in content, (
            "StageMovement not imported/used in repair router — stage transitions not logged"
        )


# ---------------------------------------------------------------------------
# UAT-06: QC (Quality Control)
# ---------------------------------------------------------------------------
class TestUAT06QC:

    def test_qc_get_route_exists(self):
        """UAT-06-01: GET route exists in qc router."""
        content = src("routers/qc.py")
        assert has_any_route(content, "get"), "No GET route found in qc router"

    def test_qc_post_route_exists(self):
        """UAT-06-02: POST route exists in qc router."""
        content = src("routers/qc.py")
        assert has_any_route(content, "post"), "No POST route found in qc router"

    def test_qc_template_exists(self):
        """UAT-06-03: QC form template exists."""
        assert exists("templates/qc/form.html"), "templates/qc/form.html not found"

    def test_csrf_in_qc_router(self):
        """UAT-06-04: verify_csrf present in qc router."""
        content = src("routers/qc.py")
        assert "verify_csrf" in content, "verify_csrf not found in qc router"


# ---------------------------------------------------------------------------
# UAT-07: Cosmetic Assessment
# ---------------------------------------------------------------------------
class TestUAT07Cosmetic:

    def test_cosmetic_get_route_exists(self):
        """UAT-07-01: GET route exists in cosmetic router."""
        content = src("routers/cosmetic.py")
        assert has_any_route(content, "get"), "No GET route found in cosmetic router"

    def test_cosmetic_post_route_exists(self):
        """UAT-07-02: POST route exists in cosmetic router."""
        content = src("routers/cosmetic.py")
        assert has_any_route(content, "post"), "No POST route found in cosmetic router"

    def test_cosmetic_template_exists(self):
        """UAT-07-03: Cosmetic template exists."""
        cosmetic_dir = os.path.join(ROOT, "templates", "cosmetic")
        assert os.path.isdir(cosmetic_dir), "templates/cosmetic/ directory does not exist"
        html_files = [f for f in os.listdir(cosmetic_dir) if f.endswith(".html")]
        assert len(html_files) >= 1, "No HTML templates found in templates/cosmetic/"


# ---------------------------------------------------------------------------
# UAT-08: Sales
# ---------------------------------------------------------------------------
class TestUAT08Sales:

    def test_sales_list_route_exists(self):
        """UAT-08-01: Sales list GET route exists."""
        content = src("routers/sales.py")
        assert has_any_route(content, "get"), "No GET route found in sales router"

    def test_sales_template_exists(self):
        """UAT-08-02: Sales list template exists."""
        assert exists("templates/sales/list.html") or exists("templates/sales/new.html"), (
            "Sales templates not found"
        )

    def test_sales_create_post_exists(self):
        """UAT-08-03: Sale creation POST route exists."""
        content = src("routers/sales.py")
        assert has_any_route(content, "post"), "No POST route found in sales router"

    def test_csrf_in_sales_router(self):
        """UAT-08-04: verify_csrf present in sales router."""
        content = src("routers/sales.py")
        assert "verify_csrf" in content, "verify_csrf not found in sales router"


# ---------------------------------------------------------------------------
# UAT-09: Dealer Management
# ---------------------------------------------------------------------------
class TestUAT09Dealers:

    def test_dealer_list_get_route(self):
        """UAT-09-01: GET /dealers list route exists."""
        content = src("routers/dealers.py")
        # The list route is @router.get("") with prefix /dealers — may have extra kwargs
        assert re.search(r'@router\.get\s*\(\s*["\']["\']', content), (
            "Dealer list GET route not found"
        )

    def test_dealer_profile_route(self):
        """UAT-09-02: Dealer profile GET /{dealer_id} route exists."""
        content = src("routers/dealers.py")
        assert '/{dealer_id}"' in content or "/{dealer_id}'" in content, (
            "Dealer profile route /{dealer_id} not found"
        )

    def test_dealer_overdue_route(self):
        """UAT-09-03: GET /dealers/overdue route exists."""
        content = src("routers/dealers.py")
        assert '"/overdue"' in content or "'/overdue'" in content, (
            "Dealer overdue route not found"
        )

    def test_dealer_ageing_route(self):
        """UAT-09-04: GET /dealers/ageing route exists."""
        content = src("routers/dealers.py")
        assert '"/ageing"' in content or "'/ageing'" in content, (
            "Dealer ageing route not found"
        )

    def test_credit_note_apply_route(self):
        """UAT-09-05: POST /dealers/{dealer_id}/credit-notes/{cn_id}/apply route exists."""
        content = src("routers/dealers.py")
        assert "credit-notes" in content and "apply" in content, (
            "Credit note apply route not found in dealers router"
        )

    def test_dealer_order_invoice_route(self):
        """UAT-09-06: GET /dealers/{dealer_id}/orders/{order_id}/invoice route exists."""
        content = src("routers/dealers.py")
        assert "invoice" in content and "order_id" in content, (
            "Dealer order invoice route not found"
        )

    def test_csrf_in_dealers_router(self):
        """UAT-09-07: verify_csrf present in dealers router."""
        content = src("routers/dealers.py")
        assert "verify_csrf" in content, "verify_csrf not found in dealers router"


# ---------------------------------------------------------------------------
# UAT-10: Telecalling
# ---------------------------------------------------------------------------
class TestUAT10Telecalling:

    def test_telecalling_get_route_exists(self):
        """UAT-10-01: GET route exists in telecalling router."""
        content = src("routers/telecalling.py")
        assert has_any_route(content, "get"), "No GET route found in telecalling router"

    def test_telecalling_post_route_exists(self):
        """UAT-10-02: POST route exists in telecalling router."""
        content = src("routers/telecalling.py")
        assert has_any_route(content, "post"), "No POST route found in telecalling router"

    def test_csrf_in_telecalling_router(self):
        """UAT-10-03: verify_csrf present in telecalling router."""
        content = src("routers/telecalling.py")
        assert "verify_csrf" in content, "verify_csrf not found in telecalling router"


# ---------------------------------------------------------------------------
# UAT-11: WhatsApp Integration
# ---------------------------------------------------------------------------
class TestUAT11WhatsApp:

    def test_whatsapp_get_route_exists(self):
        """UAT-11-01: GET route exists in whatsapp router."""
        content = src("routers/whatsapp.py")
        assert has_any_route(content, "get"), "No GET route in whatsapp router"

    def test_whatsapp_compose_route(self):
        """UAT-11-02: GET /compose route exists in whatsapp router."""
        content = src("routers/whatsapp.py")
        assert '"/compose"' in content or "'/compose'" in content, (
            "WhatsApp /compose route not found"
        )

    def test_csrf_in_whatsapp_router(self):
        """UAT-11-03: verify_csrf present in whatsapp router."""
        content = src("routers/whatsapp.py")
        assert "verify_csrf" in content, "verify_csrf not found in whatsapp router"


# ---------------------------------------------------------------------------
# UAT-12: GRN (Goods Receipt Note)
# ---------------------------------------------------------------------------
class TestUAT12GRN:

    def test_grn_get_route_exists(self):
        """UAT-12-01: GET route exists in grn router."""
        content = src("routers/grn.py")
        assert has_any_route(content, "get"), "No GET route found in grn router"

    def test_grn_post_route_exists(self):
        """UAT-12-02: POST route exists in grn router."""
        content = src("routers/grn.py")
        assert has_any_route(content, "post"), "No POST route found in grn router"

    def test_grn_templates_exist(self):
        """UAT-12-03: GRN templates exist."""
        assert exists("templates/grn/index.html") or exists("templates/grn/form.html"), (
            "GRN templates not found"
        )

    def test_csrf_in_grn_router(self):
        """UAT-12-04: verify_csrf present in grn router."""
        content = src("routers/grn.py")
        assert "verify_csrf" in content, "verify_csrf not found in grn router"


# ---------------------------------------------------------------------------
# UAT-13: Transfers
# ---------------------------------------------------------------------------
class TestUAT13Transfers:

    def test_transfers_get_route_exists(self):
        """UAT-13-01: GET route exists in transfers router."""
        content = src("routers/transfers.py")
        assert has_any_route(content, "get"), "No GET route found in transfers router"

    def test_transfers_post_route_exists(self):
        """UAT-13-02: POST route exists in transfers router."""
        content = src("routers/transfers.py")
        assert has_any_route(content, "post"), "No POST route found in transfers router"

    def test_csrf_in_transfers_router(self):
        """UAT-13-03: verify_csrf present in transfers router."""
        content = src("routers/transfers.py")
        assert "verify_csrf" in content, "verify_csrf not found in transfers router"


# ---------------------------------------------------------------------------
# UAT-14: Spare Parts
# ---------------------------------------------------------------------------
class TestUAT14SpareParts:

    def test_spare_parts_get_route_exists(self):
        """UAT-14-01: GET route exists in spare_parts router."""
        content = src("routers/spare_parts.py")
        assert has_any_route(content, "get"), "No GET route found in spare_parts router"

    def test_spare_parts_post_route_exists(self):
        """UAT-14-02: POST route exists in spare_parts router."""
        content = src("routers/spare_parts.py")
        assert has_any_route(content, "post"), "No POST route found in spare_parts router"

    def test_low_stock_alert_logic_present(self):
        """UAT-14-03: Low stock alert field (min_stock_alert) present in spare_parts router."""
        content = src("routers/spare_parts.py")
        assert "min_stock_alert" in content, (
            "min_stock_alert not found in spare_parts router — low stock alert logic missing"
        )

    def test_csrf_in_spare_parts_router(self):
        """UAT-14-04: verify_csrf present in spare_parts router."""
        content = src("routers/spare_parts.py")
        assert "verify_csrf" in content, "verify_csrf not found in spare_parts router"


# ---------------------------------------------------------------------------
# UAT-15: Reports
# ---------------------------------------------------------------------------
class TestUAT15Reports:

    def test_stage_movement_report_route(self):
        """UAT-15-01: Stage movement report route exists."""
        content = src("routers/reports.py")
        assert "stage-movement" in content or "stage_movement" in content, (
            "Stage movement report route not found"
        )

    def test_sales_report_route(self):
        """UAT-15-02: Sales report route exists."""
        content = src("routers/reports.py")
        assert '"/sales"' in content or "'/sales'" in content, (
            "Sales report route not found"
        )

    def test_lot_pl_report_route(self):
        """UAT-15-03: Lot P&L report route exists."""
        content = src("routers/reports.py")
        assert "lot-pl" in content or "lot_pl" in content, (
            "Lot P&L report route not found"
        )

    def test_receivables_ageing_route(self):
        """UAT-15-04: Receivables ageing report route exists at /reports/receivables."""
        content = src("routers/reports.py")
        assert "receivables" in content, (
            "/reports/receivables route not found"
        )

    def test_csv_export_present(self):
        """UAT-15-05: CSV export present in at least one report (StreamingResponse or csv)."""
        content = src("routers/reports.py")
        assert "csv" in content.lower() or "StreamingResponse" in content, (
            "No CSV export found in reports router"
        )


# ---------------------------------------------------------------------------
# UAT-16: Admin Panel
# ---------------------------------------------------------------------------
class TestUAT16Admin:

    def test_admin_users_route_exists(self):
        """UAT-16-01: Admin users route exists."""
        content = src("routers/admin.py")
        assert '"/users"' in content or "'/users'" in content, (
            "Admin /users route not found"
        )

    def test_admin_audit_log_route_exists(self):
        """UAT-16-02: Admin audit-log route exists at /admin/audit-log."""
        content = src("routers/admin.py")
        assert "audit-log" in content or "audit_log" in content, (
            "Admin audit-log route not found"
        )

    def test_user_management_route_exists(self):
        """UAT-16-03: User management POST route (create/edit) exists."""
        content = src("routers/admin.py")
        assert has_any_route(content, "post"), (
            "No POST routes in admin router — user management not functional"
        )

    def test_csrf_in_admin_router(self):
        """UAT-16-04: verify_csrf present in admin router."""
        content = src("routers/admin.py")
        assert "verify_csrf" in content, "verify_csrf not found in admin router"


# ---------------------------------------------------------------------------
# UAT-17: Accounts & Payments
# ---------------------------------------------------------------------------
class TestUAT17Accounts:

    def test_customer_receipts_route_exists(self):
        """UAT-17-01: Customer receipts route exists in accounts router."""
        content = src("routers/accounts.py")
        assert "customer-receipts" in content or "customer_receipts" in content, (
            "Customer receipts route not found in accounts router"
        )

    def test_supplier_payments_route_exists(self):
        """UAT-17-02: Supplier payments route exists in accounts router."""
        content = src("routers/accounts.py")
        assert "supplier-payments" in content or "supplier_payments" in content, (
            "Supplier payments route not found in accounts router"
        )

    def test_dealer_order_id_reconciliation(self):
        """UAT-17-03: dealer_order_id FK present in CustomerReceipt model (reconciliation)."""
        content = src("models/crm.py")
        assert "dealer_order_id" in content, (
            "dealer_order_id FK not found in CustomerReceipt model — reconciliation missing"
        )

    def test_csrf_in_accounts_router(self):
        """UAT-17-04: verify_csrf present in accounts router."""
        content = src("routers/accounts.py")
        assert "verify_csrf" in content, "verify_csrf not found in accounts router"


# ---------------------------------------------------------------------------
# UAT-18: Invoices
# ---------------------------------------------------------------------------
class TestUAT18Invoices:

    def test_invoice_print_route_for_device_sales(self):
        """UAT-18-01: Invoice print route for device sales exists."""
        content = src("routers/invoices.py")
        assert "print" in content, (
            "Invoice print route not found in invoices router"
        )

    def test_invoice_print_template_exists(self):
        """UAT-18-02: Invoice print template exists."""
        assert exists("templates/invoices/print.html"), (
            "templates/invoices/print.html not found"
        )

    def test_dealer_order_invoice_covered_in_dealers(self):
        """UAT-18-03: Dealer order invoice route covered in dealers router (UAT-09 cross-check)."""
        content = src("routers/dealers.py")
        assert "invoice" in content, (
            "Dealer order invoice route not found in dealers router"
        )


# ---------------------------------------------------------------------------
# UAT-19: CRM
# ---------------------------------------------------------------------------
class TestUAT19CRM:

    def test_crm_dashboard_route_exists(self):
        """UAT-19-01: CRM dashboard route exists."""
        content = src("routers/crm_dashboard.py")
        assert has_any_route(content, "get"), "No GET route in crm_dashboard router"

    def test_crm_contacts_route_exists(self):
        """UAT-19-02: CRM contacts list route exists."""
        content = src("routers/crm_contacts.py")
        assert has_any_route(content, "get"), "No GET route in crm_contacts router"

    def test_crm_quotes_route_exists(self):
        """UAT-19-03: CRM quotes route exists."""
        content = src("routers/crm_quotes.py")
        assert has_any_route(content, "get"), "No GET route in crm_quotes router"

    def test_crm_activities_route_exists(self):
        """UAT-19-04: CRM activities route exists."""
        content = src("routers/crm_activities.py")
        assert has_any_route(content, "get") or has_any_route(content, "post"), (
            "No routes in crm_activities router"
        )

    def test_csrf_in_crm_contacts_router(self):
        """UAT-19-05: verify_csrf present in crm_contacts router."""
        content = src("routers/crm_contacts.py")
        assert "verify_csrf" in content, "verify_csrf not found in crm_contacts router"

    def test_crm_dashboard_template_exists(self):
        """UAT-19-06: CRM dashboard template exists."""
        assert exists("templates/crm/dashboard.html"), (
            "templates/crm/dashboard.html not found"
        )

    def test_crm_contacts_list_template_exists(self):
        """UAT-19-07: CRM contacts list template exists."""
        assert exists("templates/crm/contacts/list.html"), (
            "templates/crm/contacts/list.html not found"
        )

    def test_crm_quotes_list_template_exists(self):
        """UAT-19-08: CRM quotes list template exists."""
        assert exists("templates/crm/quotes/list.html"), (
            "templates/crm/quotes/list.html not found"
        )


# ---------------------------------------------------------------------------
# UAT-20: Security & Config
# ---------------------------------------------------------------------------
class TestUAT20SecurityConfig:

    def test_config_ini_in_gitignore(self):
        """UAT-20-01: config.ini is listed in .gitignore."""
        content = src(".gitignore")
        assert "config.ini" in content, (
            "config.ini not in .gitignore — secrets may be committed to source control"
        )

    def test_generic_exception_handler_in_main(self):
        """UAT-20-02: Generic Exception handler registered in main.py."""
        content = src("main.py")
        assert "exception_handler(Exception)" in content, (
            "Generic Exception handler not found in main.py"
        )

    def test_csrf_on_all_mutation_routers(self):
        """UAT-20-03: All 26 mutation routers contain verify_csrf."""
        mutation_routers = [
            "routers/sales.py",
            "routers/stock.py",
            "routers/repair.py",
            "routers/iqc.py",
            "routers/qc.py",
            "routers/cosmetic.py",
            "routers/grn.py",
            "routers/transfers.py",
            "routers/spare_parts.py",
            "routers/admin.py",
            "routers/dealers.py",
            "routers/accounts.py",
            "routers/telecalling.py",
            "routers/whatsapp.py",
            "routers/devices.py",
            "routers/master.py",
            "routers/bulk_upload.py",
            "routers/stage_control.py",
            "routers/inventory_location.py",
            "routers/crm_contacts.py",
            "routers/crm_activities.py",
            "routers/crm_sourcing.py",
            "routers/crm_sales.py",
            "routers/crm_quotes.py",
            "routers/crm_price_matrix.py",
            "routers/crm_purchase_orders.py",
        ]
        missing = []
        for router_path in mutation_routers:
            if exists(router_path):
                content = src(router_path)
                if "verify_csrf" not in content:
                    missing.append(router_path)
            else:
                missing.append(f"{router_path} (FILE NOT FOUND)")
        assert not missing, (
            f"verify_csrf missing in the following mutation routers:\n"
            + "\n".join(f"  - {r}" for r in missing)
        )

    def test_login_banner_no_default_password(self):
        """UAT-20-04: Login page does not expose a default password."""
        content = src("templates/login.html")
        bad_patterns = ["admin123", "password123", "oxypc123", "default password", "test123"]
        found = [p for p in bad_patterns if p.lower() in content.lower()]
        assert not found, (
            f"Default password hint found in login.html: {found}"
        )

    def test_requirements_pinned_versions(self):
        """UAT-20-05: requirements.txt uses pinned (==) versions for critical packages."""
        content = src("requirements.txt")
        # Core packages that must be pinned
        # bcrypt replaces passlib here: passlib is no longer a dependency, and an
        # unpinned bcrypt is exactly the drift this test exists to catch.
        critical = ["fastapi", "sqlalchemy", "bcrypt", "python-jose"]
        unpinned = []
        for pkg in critical:
            # Check if pkg appears but NOT with == pinning
            lines_with_pkg = [
                l.strip() for l in content.splitlines()
                if l.lower().startswith(pkg.lower())
            ]
            for line in lines_with_pkg:
                if "==" not in line:
                    unpinned.append(line)
        assert not unpinned, (
            f"Critical packages not pinned with == in requirements.txt: {unpinned}"
        )

    def test_oxypc_auto_fix_env_gate_in_main(self):
        """UAT-20-06: OXYPC_AUTO_FIX environment variable gate present in main.py."""
        content = src("main.py")
        assert "OXYPC_AUTO_FIX" in content, (
            "OXYPC_AUTO_FIX env var gate not found in main.py"
        )


# ---------------------------------------------------------------------------
# UAT-21: Database Models
# ---------------------------------------------------------------------------
class TestUAT21DatabaseModels:

    @pytest.mark.parametrize("model_file,class_name", [
        ("models/user.py", "User"),
        ("models/device.py", "Device"),
        ("models/lot.py", "Lot"),
        ("models/sales.py", "Sale"),
        ("models/dealers.py", "Dealer"),
        ("models/dealers.py", "DealerOrder"),
        ("models/dealers.py", "DealerCreditNote"),
        ("models/crm.py", "CustomerReceipt"),
        ("models/spare_parts.py", "SparePart"),
        ("models/engines.py", "AuditLog"),
    ])
    def test_key_model_class_exists(self, model_file, class_name):
        """UAT-21-01..10: Key model classes exist in their respective files."""
        assert exists(model_file), f"{model_file} not found"
        content = src(model_file)
        assert f"class {class_name}" in content, (
            f"class {class_name} not found in {model_file}"
        )

    def test_dealer_credit_note_is_applied_field(self):
        """UAT-21-11: DealerCreditNote has is_applied field."""
        content = src("models/dealers.py")
        assert "is_applied" in content, (
            "is_applied field not found in DealerCreditNote model"
        )

    def test_dealer_credit_note_server_default(self):
        """UAT-21-12: DealerCreditNote.is_applied has server_default set."""
        content = src("models/dealers.py")
        assert "server_default" in content, (
            "server_default not found in dealers model — DB-level default missing for is_applied"
        )

    def test_customer_receipt_dealer_order_id_fk(self):
        """UAT-21-13: CustomerReceipt has dealer_order_id FK."""
        content = src("models/crm.py")
        assert "dealer_order_id" in content, (
            "dealer_order_id FK not found in CustomerReceipt (models/crm.py)"
        )

    def test_audit_log_model_exists(self):
        """UAT-21-14: AuditLog model exists."""
        content = src("models/engines.py")
        assert "class AuditLog" in content, (
            "AuditLog model class not found in models/engines.py"
        )


# ---------------------------------------------------------------------------
# UAT-22: Navigation (base.html)
# ---------------------------------------------------------------------------
class TestUAT22Navigation:

    def test_dealer_ageing_page_is_reachable(self):
        """UAT-22-01: /dealers/ageing is linked from somewhere in the UI.

        Was asserting the link lived in base.html. It never has — these two pages
        are reached from the Dashboard's receivables buttons, not the sidebar —
        so the test failed against a perfectly reachable page. Its sibling below
        "passed" only because "/dealers/overdue" happens to appear inside the
        /dealers active-state condition, which is not a link at all.

        The invariant worth protecting is that the page is not orphaned, so both
        now search every template rather than one file, and neither can pass or
        fail by accident.
        """
        assert _linked_from_any_template("/dealers/ageing"), (
            "/dealers/ageing is not linked from any template — the dealer ageing "
            "report exists as a route but users cannot navigate to it"
        )

    def test_overdue_orders_page_is_reachable(self):
        """UAT-22-02: /dealers/overdue is linked from somewhere in the UI."""
        assert _linked_from_any_template("/dealers/overdue"), (
            "/dealers/overdue is not linked from any template — the overdue "
            "orders page exists as a route but users cannot navigate to it"
        )

    def test_audit_log_nav_link_present(self):
        """UAT-22-03: Audit Log nav link present in base.html."""
        content = src("templates/base.html")
        assert "/admin/audit-log" in content, (
            "/admin/audit-log nav link not found in base.html"
        )

    def test_receivables_nav_link_present(self):
        """UAT-22-04: Receivables nav link present in base.html."""
        content = src("templates/base.html")
        assert "/reports/receivables" in content, (
            "/reports/receivables nav link not found in base.html"
        )
