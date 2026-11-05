"""
QA / UAT Tracking Module
========================
Routes:
  GET  /qa/                        — Dashboard (metrics + KPIs)
  GET  /qa/requirements            — Requirements list + RTM link
  POST /qa/requirements/new        — Create requirement
  POST /qa/requirements/{id}/status — Update status
  GET  /qa/test-cases              — Test cases list
  POST /qa/test-cases/new          — Create test case
  POST /qa/test-cases/{id}/execute — Log execution result
  GET  /qa/defects                 — Defect tracker
  POST /qa/defects/new             — Log defect
  POST /qa/defects/{id}/status     — Update defect status
  GET  /qa/uat                     — UAT scenarios list
  POST /qa/uat/new                 — Create UAT scenario
  POST /qa/uat/{id}/execute        — Record UAT result + sign-off
  GET  /qa/releases                — Release list
  POST /qa/releases/new            — Create release
  POST /qa/releases/{id}/status    — Advance release status
  GET  /qa/rtm                     — Requirement Traceability Matrix
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from utils.timezone import app_now
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user, require_roles, verify_csrf
from services.audit_engine import audit
from database import get_db
from models.user import User, UserRole
from models.qa_uat import (
    QARequirement, QATestCase, QATestExecution, QADefect,
    QAUATScenario, QARelease,
    RequirementSource, RequirementStatus, RequirementPriority,
    TestCaseType, TestCaseStatus, ExecutionStatus,
    DefectSeverity, DefectPriority, DefectStatus,
    UATStatus, ReleaseStatus,
)
from templates_config import templates

# CSRF on every QA write (2026-11-04): the forms already send csrf_token; GETs skip it.
router = APIRouter(prefix="/qa", tags=["qa-uat"], dependencies=[Depends(verify_csrf)])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Any logged-in user can view; admin + inventory_manager + qc_inspector can edit
_UAT_PATHS = ("/qa/uat", "/qa/requirements", "/qa/test-cases", "/qa/defects")
_UX_PATHS = ("/qa/workflow", "/qa/journeys", "/qa/database-diagram", "/qa/stage-workflows")


def qa_module_for(path: str) -> str:
    """Permission Matrix row for a Product Management page (2026-11-05):
    QA Dashboard = "qa", UAT Analysis = "qa_uat", UX Journeys = "qa_ux"."""
    if path.startswith(_UAT_PATHS):
        return "qa_uat"
    if path.startswith(_UX_PATHS):
        return "qa_ux"
    return "qa"


async def _qa_access(request: Request, current_user: User = Depends(get_current_user)) -> User:
    """Product Management pages (2026-11-05, Pankaj): admin, or a role with
    Enable ticked on that page's Permission Matrix row (QA Dashboard / UAT
    Analysis / UX Journeys). Strict — a role with no row is NOT allowed (was:
    any signed-in user could open /qa/... by typing the address)."""
    from models.role_permissions import has_explicit_perm
    role = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    if not has_explicit_perm(role, qa_module_for(request.url.path)):
        raise HTTPException(status_code=403, detail="Your role does not have access to this Product Management page.")
    return current_user


_view  = _qa_access
_edit  = require_roles(UserRole.admin, UserRole.inventory_manager, UserRole.qc_inspector)
_admin = require_roles(UserRole.admin, UserRole.inventory_manager)


# ── helpers ───────────────────────────────────────────────────────────────────

_HARDCODED_COMMITS = [
    # ── 2026-07-07 ──────────────────────────────────────────────────────────────
    {"date": "2026-07-07", "msg": "Lot Add/Edit: Quantity and Buying Price made optional (default qty=1, price=0 when left blank)", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-07", "msg": "Dealer Bulk Upload: business name duplicate check is now case-insensitive; phone field rejects multi-number/garbage entries instead of storing them silently", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-07", "msg": "Move Device page: removed forced-uppercase on all 3 tabs' scan fields; Tag Number lookup made case-insensitive to match", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-07", "msg": "Fixed Product IQC showing only 50 of 4500+ records — converted 9 pages (IQC, Sales List, Dealers List, WhatsApp Audit, Stress Test, L1/L2/L3 Repair, Lot Overview, TRC Production, Market Intel, System Audit Log) from server-side page/page_size pagination to full-fetch + client-side pagination; also found and fixed 4 tables with paging disabled entirely", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-07", "msg": "App-wide: standardized every DataTable to 12 rows/page with pagination centered at the bottom; removed 18 hard backend row-caps (500/1000/5000 etc.) that were silently truncating list pages across devices, GRN, transfers, dealers, telecalling, admin, reports and more", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-07", "msg": "Devices bulk upload expanded to cover every IQC entry field — full physical inspection checklist (screen/panel/keyboard/touchpad/ports) now included in the template and parser, not just core device fields; removed redundant CSV Template button from Product IQC page", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-07", "msg": "Wired Device Type filter dropdowns (Overall Inventory, Stock Inward/TRC, IQC list + bulk modal + entry form) to Master Data instead of hardcoded lists; fixed 'Purchase Orders: PO Category' missing from the Master Data configuration page", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-07", "msg": "Purchase Order form reworked with Item Name/Description/PO Category/Lot No line items, GSTIN + City/State/Pincode with supplier autofill; PO list gets a Generate PO download button using real Company Settings", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-07", "msg": "Part Master table: added GRN column with Harvested badge, reordered columns; Ready to Sale: Model Summary gets checkboxes, Total Count Sold, and Request/Sell actions; TRC Dashboard: new Sales Request for Lot table", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-07", "msg": "Fixed GRN→Part Master sync failing silently on part-code collisions (now upserts instead of erroring the whole GRN); added soft-delete to GRN and Part Master records", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-07", "msg": "Added Attendance Config (group-manager scoping), Part Master bulk upload, PO Quantity field rename, and Harvest modal cleanup (table now auto-refreshes on save instead of requiring a manual reload)", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-07", "msg": "Fixed a bulk-upload upstream/timeout error on large CSVs; Move Item multi-scan support; Attendance Check In modal with duration rules", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-07", "msg": "Mirrored Part GRN line items into Part Master automatically; renamed L1 Engineer role label", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-07", "msg": "Added Invoice Number/PO Number fields to the IQC Customise (bulk Apply Grade/Device Type) modal; Device Type column added across inventory pages", "category": "Enhancement", "badge": "success"},
    # ── 2026-07-06 ──────────────────────────────────────────────────────────────
    {"date": "2026-07-06", "msg": "Sourcing Sales role now sees all-user dealer/call records (previously scoped only to Telecaller); Dealer Call Log field revisions; Assigned/User columns added", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-06", "msg": "Fixed TRC Manager Approve button visibility for custom roles; Dealer Management filter/pagination/AJAX overhaul; added Dealer Quotation feature", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-06", "msg": "Added Parts Consumption/IQC/Dashboard feature batch; fixed a page-title caching bug", "category": "Enhancement", "badge": "success"},
    # ── 2026-07-05 ──────────────────────────────────────────────────────────────
    {"date": "2026-07-05", "msg": "Fixed a 500 error on Assign User; added pagination; shared the Add Dealer modal across pages; hardened bulk upload error handling", "category": "Bug Fix", "badge": "danger"},
    # ── 2026-07-04 ──────────────────────────────────────────────────────────────
    {"date": "2026-07-04", "msg": "Fixed admin ticket visibility bug; Attendance Report gets a Role column defaulting to today-only view", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-04", "msg": "Removed the inline qty field from Parts Consumption's Action column; fixed In Stock not crediting correctly after a Sourcing Request is closed post-verification", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-04", "msg": "Part Request lifecycle status sync; Inventory Search gets pagination + search; Lot Detail page improvements", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-04", "msg": "Device Profile Parts Consumption: added Verify button and a Changed pill for handed-over parts", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-04", "msg": "Added Sidebar Config module for renaming sidebar labels, plus Permission Matrix updates", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-04", "msg": "Renamed the Assign Social Leads route path; fixed an accordion status-filter bug; reordered Zone dropdown options", "category": "Bug Fix", "badge": "danger"},
    # ── 2026-07-03 ──────────────────────────────────────────────────────────────
    {"date": "2026-07-03", "msg": "Added production DB drift-fix migration scripts (already applied to production)", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-03", "msg": "Fixed two production 500 errors and removed an N+1 query on Storage Location Master", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-03", "msg": "Dealer Call Log quantities; Application Settings permission matrix; Stock Inward location cascade; Attendance Role column; Storage Location edit", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-03", "msg": "Device Edit/IQC parity; Model/Lot-based tables; optional Lot + text-box specs; Dealer Management overhaul", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-03", "msg": "Unified Part Category across all parts modals; wired CRM detail dropdowns; fixed Device Detail Location fields and Inventory Search export", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-03", "msg": "Added 34 new Master Data dropdown categories with a cache-backed master_options() Jinja global", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-03", "msg": "Added Not In Stock/High Price/Invalid Number statuses, a Status filter, and accordion pill counts to Assign Social Leads", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-03", "msg": "Fixed sidebar scroll position to be exact — no visible jump on nav click", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-03", "msg": "Added a connection watchdog — full-screen loader instead of a raw 502 page during backend downtime", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-03", "msg": "Fixed the sidebar resetting scroll to top on every navigation", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-03", "msg": "Renamed Category to Part Category app-wide; added Part Category cascade to the New Request/Replace modal", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-03", "msg": "Reworked Assign Social Leads: no-reload modals, deal-detail fields, summary cards", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-03", "msg": "Added telecalling deal-detail fields to the call log and dashboard, admin-managed via Master Data", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-03", "msg": "Added one-shot migration/verification scripts, uploaded manuals, and a warranty-agent Phase 2 scoping doc", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-03", "msg": "Added a 24-item UI/feature batch: session-expiry fix, IQC location cascade, Move Device 3-tab rework, Stock Inward/TRC cost tables, CRM sourcing docs, Part Master sync fix", "category": "Enhancement", "badge": "success"},
    # ── 2026-07-01 ──────────────────────────────────────────────────────────────
    {"date": "2026-07-01", "msg": "Left nav: WA Audit Log + Parts Purchased/Tracking/Consumption moved into Application Settings; Company Setting + Cost Config moved into FINANCE section", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-01", "msg": "Sidebar auto-scroll-to-bottom on page load fixed (stale scroll-position restore removed)", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-01", "msg": "Cache-Control: no-store added to all auth/home redirects — prevents stale-redirect browser caching after deploys", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-01", "msg": "Inventory Search is now the true application home page — root \"/\" redirects here; Admin Dashboard moved to its own /dashboard URL", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-01", "msg": "Diagnose Agent status UI: fixed stuck \"Agent is running\" state when the agent wasn't actually running; now a continuous self-correcting poller", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-01", "msg": "IQC: new interactive Keyboard & Click Test — technician presses every key and clicks left/right; Keyboard Working / Click Functional auto-fill Yes only if everything registered", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-01", "msg": "User Management column reorder: Full Name, Role, Last Login, Username, Created By, Status, Actions; Perms button removed from Actions", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-01", "msg": "Master Data: new \"Role Additional Permissions\" tab (File Upload/Download/Export, Print Page, Add New Data) with role rename + edit support", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-01", "msg": "Stress Test always showing FAIL — root cause (WiFi ping to public IP blocked by LAN firewalls) fixed; real macOS support added for USB/battery/display tests", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-07-01", "msg": "New TeleSales Dashboard page — Business Snapshot (Dealers, Device Requests, Sales, Returns) moved out of Telecalling Dashboard into its own page", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-01", "msg": "Final QC: 3 price values shown per device — Current Unit Price, After Repair Price, Updated Price; Device Detail P&L now itemizes each part consumed", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-01", "msg": "Renamed \"Assign Leads\" to \"Assign Social Leads\" across nav, Permission Matrix, and Landing Pages", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-01", "msg": "Role labels renamed: Cosmetic Manager → Inventory Manager, Store Manager → Parts Manager, IQC Inspector → IQC Handler, QC Manager → QC Handler; Master Data role dropdown synced with Add User page", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-01", "msg": "Dealer follow-ups: date + time on Call Log and Dealer Detail; Today Follow-ups button added; Follow-ups Due card no longer clickable; follow-up counts scoped to logged-in user unless admin", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-01", "msg": "New Assign Dealer Leads module — bulk-assign dealers to users via checkbox multi-select", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-01", "msg": "Global card styling: consistent card-header padding, card-body table padding + rounded corners app-wide", "category": "Enhancement", "badge": "success"},
    {"date": "2026-07-01", "msg": "App CSS not reflecting after edits — added cache-busting version query string to the stylesheet link", "category": "Bug Fix", "badge": "danger"},
    # ── 2026-06-30 ──────────────────────────────────────────────────────────────
    {"date": "2026-06-30", "msg": "Speed: GZip compression added (1 KB+ responses); role-based post-login redirect — non-admin users land on their first accessible nav item, not Admin Dashboard", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "Nav: Inventory Locations moved to INVENTORY section below Scrap Products; QA Dashboard + Manuals added to Landing Pages admin and Permission Matrix", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "GRN post IQC: scan-to-select barcode input with selected-count badge; Map this GRN always visible (Validate GRN removed)", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "Role badge: unknown/custom roles prettified in global header (e.g. 'trc_manager' → 'TRC Manager')", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-30", "msg": "L1/L2/L3 Repair tables: Bucket column added showing bucket mapped to each Tag Number", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "Role label in global header badge via Jinja2 global helper (e.g. 'inventory_manager' → 'Cosmetic Manager')", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "Device Detail: Edit button always visible; Edit icon alongside View on Inventory Search list", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "Stock Inward: Move-to-Stock section + Transfer button removed; Assign Bucket in table toolbar; 8-row pagination on both tables", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "Invoice Number on Device Detail + Inventory Search; Devices Returned (In/Out of Warranty) on TRC Dashboard", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "Bucket/Carton system: Stock Inward grouping workflow + TRC Production bucket allocation", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "Parts GRN: 500 error fix (wrong session key); Source + Added As columns; Parts Dashboard tab/column/action fixes", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-30", "msg": "IQC: form field reorganisation + Parts GRN module (record parts received from suppliers)", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "Finance: CustomerReceipt missing Contact + Dealer relationships added (was causing 500 on Accounts pages)", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-30", "msg": "Sales Report: date string → datetime parse fix; date range filter UI added to report page", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-30", "msg": "IQC Diagnose Agent: HTTPS detection enabled for app.oxypc.com (was blocked by mixed-content check)", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-30", "msg": "Permission Matrix expanded; Landing Pages admin module added; nav restructured with collapsible sections", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "Role renames across system; warranty status in Device Detail; Return form Check button added", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "CRM Lead Call: stray relationship removed (was causing startup crash preventing all logins)", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-30", "msg": "CRM Assign Leads: new module for ad-campaign lead management with funnel stage tracking", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-30", "msg": "Global Raise Ticket system: any user can raise a support ticket; per-user Tickets Raised page", "category": "Enhancement", "badge": "success"},
    # ── 2026-06-29 ──────────────────────────────────────────────────────────────
    {"date": "2026-06-29", "msg": "HTTPS force-redirect loop fix on Railway; IQC Diagnose Agent ping suppressed on HTTPS (was causing browser mixed-content console errors)", "category": "Bug Fix", "badge": "danger"},
    # ── 2026-06-27 ──────────────────────────────────────────────────────────────
    {"date": "2026-06-27", "msg": "Mobile sidebar vertical scroll; Manuals dir safety on restricted filesystems", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-27", "msg": "PDF download 500 error — invalid Latin-1 character in Helvetica font", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-27", "msg": "Manuals module: 15 downloadable PDF learning manuals with Share-to-user notification", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-27", "msg": "Mobile navigation: hamburger opens fixed sidebar overlay with backdrop", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-27", "msg": "TRC Dashboard title + Devices Returned section (Within/Out of Warranty tables)", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-27", "msg": "QA Dashboard: Recent Changes 60-day changelog section", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-27", "msg": "User Management: email field added to profile page and user form", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-27", "msg": "Login: renamed title; clickable username in topbar opens profile page", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-27", "msg": "IQC Storage Health + Fan Sound fields editable; Stress Test 500 errors fixed", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-27", "msg": "QC table: 'Verify QC' + 'Go Cleaning' buttons renamed; Tag Number clickable", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-27", "msg": "Final QC: first device accordion open, rest collapsed; device count badge", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-27", "msg": "Escape % in DATABASE_URL when writing to configparser (config.ini fix)", "category": "Bug Fix", "badge": "danger"},
    # ── 2026-06-26 ──────────────────────────────────────────────────────────────
    {"date": "2026-06-26", "msg": "Sprint 31 — IQC overhaul, GRN AJAX, Assign Stock defaults, Parts Replace, Scrap Verify, Sale PDF import, Notifications", "category": "Sprint Release", "badge": "primary"},
    {"date": "2026-06-26", "msg": "Server-side stress testing — removes dependency on standalone OxyQC desktop app", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-26", "msg": "Stress Test page redesign — live 2/3 + 1/3 split panel layout", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-26", "msg": "Web-based installer wizard (WordPress-style first-time setup at /setup)", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-26", "msg": "Register StressTestResult in models __init__ for Alembic discovery", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-26", "msg": "Stress panel: clear default agent URL; help text for server-side setup", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-26", "msg": "IQC diagnose: touchpad logic-board detection, fan detection, battery health fill", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-26", "msg": "Sales table DataTables column count error fixed; Download invoice link fixed", "category": "Bug Fix", "badge": "danger"},
    # ── 2026-06-24 ──────────────────────────────────────────────────────────────
    {"date": "2026-06-24", "msg": "Edit Device: replicates IQC Entry layout; all IQC fields editable", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-24", "msg": "IQC: per-station OxyQC Diagnose agent + expanded hardware auto-detection", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-24", "msg": "IQC: Download Agent button + agent button states (blue/grey/green)", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-24", "msg": "IQC: Battery health fallbacks added to PowerShell diagnose probe", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-24", "msg": "IQC: Display + Keyboard & Touchpad panel-style cards; one-row grade/floor/warehouse", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-24", "msg": "IQC: Screen-functional/defect detection + Charging Port field added", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-24", "msg": "Dispatch (TRC): Grade D removed; A|B side-by-side + C full-width; 6-row pagination", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-24", "msg": "Final QC: 3-column card — IQC details, repair history + parts, decision form", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-24", "msg": "Nav: optional accordion + TRC telecaller filter row + sticky Stage Timeline", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-24", "msg": "Nav: renamed labels + moved TRC Dashboard; Parts Consumption for all roles", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-24", "msg": "Parts tabs: export + checkboxes; column hides; stage layout improvements", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-24", "msg": "IQC screen size detection + HDD field rename and reorder", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-24", "msg": "IQC battery-health cap; touchpad missing/click + speaker Not-Checked fixed", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-24", "msg": "Control engine: same-stage no-op transitions allowed (fixes Start Repair 403)", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-24", "msg": "Permissions: custom roles pass non-admin route gates (fixes 403 on enabled modules)", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-24", "msg": "Permissions: Enable is master switch; 'Not Working' option removed", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-24", "msg": "Transfers: never insert NULL warehouse; Cosmetic Skip to Final QC shortcut", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-24", "msg": "DataTables column count warning fixed on Parts Dashboard", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-24", "msg": "IQC: OxyQC_Agent.exe shipped in repo so Diagnose download works on production", "category": "Bug Fix", "badge": "danger"},
    # ── 2026-06-23 ──────────────────────────────────────────────────────────────
    {"date": "2026-06-23", "msg": "Sprint 30 — warranty/returns/GRN-post-IQC + per-user WhatsApp + global error modal", "category": "Sprint Release", "badge": "primary"},
    {"date": "2026-06-23", "msg": "GRN: map records to Stock Inward; tables 5/page recent-first; admin nav accordions", "category": "Enhancement", "badge": "success"},
    {"date": "2026-06-23", "msg": "GRN: soft-delete on both GRN pages + clearer duplicate messages", "category": "Enhancement", "badge": "success"},
    # ── 2026-06-22 ──────────────────────────────────────────────────────────────
    {"date": "2026-06-22", "msg": "Sprint 31 — TRC Production, Scrap Products, WorkID Status, GRN validate, repair routing, OxyQC USB import", "category": "Sprint Release", "badge": "primary"},
    {"date": "2026-06-22", "msg": "Database migration fixed for empty database", "category": "Bug Fix", "badge": "danger"},
    {"date": "2026-06-22", "msg": "Database password setup fixed", "category": "Bug Fix", "badge": "danger"},
    # ── 2026-06-13 ──────────────────────────────────────────────────────────────
    {"date": "2026-06-13", "msg": "Sprint 29 — work-order/parts/dispatch workflow + 21 feature enhancements", "category": "Sprint Release", "badge": "primary"},
]


# Conventional-commit prefix -> (category, badge). Checked in order; first match wins.
_COMMIT_PREFIX_MAP = [
    ("fix:",     ("Bug Fix", "danger")),
    ("feat:",    ("Enhancement", "success")),
    ("style:",   ("Enhancement", "success")),
    ("refactor:",("Enhancement", "success")),
    ("rename:",  ("Enhancement", "success")),
    ("perf:",    ("Enhancement", "success")),
    ("docs:",    ("Enhancement", "success")),
    ("chore:",   ("Enhancement", "success")),
    ("ci:",      ("Enhancement", "success")),
    ("test:",    ("Enhancement", "success")),
]


def _changelog_file_lines(after_date: str) -> list[str]:
    """qa_docs/changelog.txt lines ("sha|YYYY-MM-DD|subject", newest first)
    dated on or after `after_date`."""
    import os
    path = os.path.join(_PROJECT_ROOT, "qa_docs", "changelog.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            out = []
            for ln in fh:
                parts = ln.rstrip("\n").split("|", 2)
                if len(parts) == 3 and parts[1] >= after_date:
                    out.append(ln.rstrip("\n"))
            return out
    except OSError:
        return []


def _auto_commits_since(after_date: str) -> list[dict]:
    """Pull every real git commit strictly after `after_date` (YYYY-MM-DD) and
    turn each into a changelog entry, inferring category/badge from the
    conventional-commit prefix. This is what makes new commits show up on the
    QA Dashboard automatically — no manual list edit needed going forward.
    Best-effort: returns [] on any git/parsing failure rather than raising,
    since the changelog must never break page load."""
    try:
        lines = None
        try:
            result = subprocess.run(
                ["git", "log", f"--since={after_date} 00:00:00",
                 "--format=%H|%ad|%s", "--date=format:%Y-%m-%d", "--no-merges"],
                cwd=_PROJECT_ROOT, capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().splitlines()
        except Exception:
            lines = None
        if lines is None:
            # Production runs in Docker without .git (2026-11-04): read the
            # changelog file committed with every push (_gen_qa_changelog.py).
            lines = _changelog_file_lines(after_date)
        entries = []
        seen_subjects = set()
        for line in lines:
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            _sha, date, subject = parts
            subject = subject.strip()
            if not subject or subject in seen_subjects:
                continue
            seen_subjects.add(subject)
            # Skip the automated exe-rebuild noise commit — not a product change.
            if "[skip ci]" in subject:
                continue
            category, badge = "Enhancement", "success"
            msg = subject
            for prefix, (cat, bg) in _COMMIT_PREFIX_MAP:
                if subject.lower().startswith(prefix):
                    category, badge = cat, bg
                    msg = subject[len(prefix):].strip()
                    break
            entries.append({"date": date, "msg": msg, "category": category, "badge": badge})
        return entries
    except Exception:
        return []


def _get_recent_commits() -> list[dict]:
    """Changelog for the QA dashboard: curated hand-written entries (more
    descriptive than raw commit subjects) for everything up to the latest
    hardcoded date, PLUS every real git commit after that date pulled
    automatically — so new work shows up here without editing this file.
    Full history, not windowed to any number of days — the Global Table
    module on the template side paginates it instead of a server-side cap.
    """
    latest_hardcoded_date = max((c["date"] for c in _HARDCODED_COMMITS), default="1970-01-01")
    # Start the automatic pull the day AFTER the last curated date, so today's
    # already-hand-written entries above never get duplicated by the auto-pull.
    cutoff = (datetime.strptime(latest_hardcoded_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    auto_entries = _auto_commits_since(cutoff)
    # Newest first, same as the hardcoded list's ordering.
    auto_entries.sort(key=lambda e: e["date"], reverse=True)
    return auto_entries + _HARDCODED_COMMITS


def _week_start(date_str: str):
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return d - timedelta(days=d.weekday())


async def auto_track_releases(db: AsyncSession) -> list[str]:
    """Keep QARelease tracking current automatically, called from main.py's
    startup event — so every deploy (which is every real commit + restart in
    this project's workflow) closes out any past week of real changelog
    activity that doesn't have a release yet, the same way
    seed_qa_backfill_jul17_sep24.py did for the gap that had already
    accumulated by 2026-09-24. That script is the one-off catch-up; this is
    what stops the gap from ever reopening.

    Only CLOSED past weeks (ending before the current week) are turned into
    releases — never the in-progress current week, so a server restarted
    more than once in the same week (routine during active development)
    never creates a partial, still-growing release. The cutoff is always
    read from the latest existing release's own release_date, so this is
    naturally idempotent: once a week has a release, re-running finds
    nothing left to do for it.

    Returns the list of version strings created (empty if already current).
    """
    latest = (await db.execute(
        select(QARelease).where(QARelease.version.op("~")(r"^v\d+\.\d+\.\d+$"))
        .order_by(QARelease.release_date.desc()).limit(1)
    )).scalars().first()
    if not latest or not latest.release_date:
        return []  # no baseline to work from — nothing to safely auto-track yet

    cutoff = latest.release_date.strftime("%Y-%m-%d")
    major, minor, _patch = latest.version.lstrip("v").split(".")
    next_minor = int(minor) + 1

    commits = _get_recent_commits()
    since = [c for c in commits if c["date"] > cutoff]
    if not since:
        return []
    since.sort(key=lambda c: c["date"])

    this_week_start = _week_start(app_now().strftime("%Y-%m-%d"))
    groups: dict = {}
    for c in since:
        ws = _week_start(c["date"])
        if ws >= this_week_start:
            continue  # in-progress week — leave for next time it's actually closed
        groups.setdefault(ws, []).append(c)

    now = app_now()
    created = []
    for week_start in sorted(groups):
        items = groups[week_start]
        dates = sorted({i["date"] for i in items})
        start_d, end_d = dates[0], dates[-1]
        bug_fixes = [i["msg"] for i in items if i["category"] == "Bug Fix"]
        others = [i["msg"] for i in items if i["category"] != "Bug Fix"]

        version = f"v{major}.{next_minor}.0"
        start_label = datetime.strptime(start_d, "%Y-%m-%d").strftime("%b %d")
        end_label = datetime.strptime(end_d, "%Y-%m-%d").strftime("%b %d")
        title = f"{start_label}-{end_label} batch — {len(items)} changes ({len(bug_fixes)} fixes)"

        desc_parts = others + ([f"Fixes: {'; '.join(bug_fixes)}"] if bug_fixes else [])
        description = " + ".join(desc_parts)
        if len(description) > 1800:
            description = description[:1800].rsplit(" + ", 1)[0] + " + …"

        release_dt = datetime.combine(datetime.strptime(end_d, "%Y-%m-%d").date(), datetime.min.time().replace(hour=18))
        planned_dt = datetime.combine(datetime.strptime(start_d, "%Y-%m-%d").date(), datetime.min.time().replace(hour=9))

        db.add(QARelease(
            version=version, title=title, description=description,
            status=ReleaseStatus.deployed,
            planned_date=planned_dt, release_date=release_dt,
            qa_sign_off_by="system-auto", qa_sign_off_at=release_dt,
            # created_at = release_dt, not `now` — Recent Releases / the
            # Releases list both order by created_at.desc(); every row
            # sharing `now` would sort in insertion order instead of
            # newest-release-first (same bug the backfill script hit first).
            created_by="system-auto", created_at=release_dt, updated_at=now,
        ))
        created.append(version)
        next_minor += 1

    if created:
        await db.commit()
    return created


def _s(v: Optional[str]) -> Optional[str]:
    """Return None for blank strings."""
    return v.strip() or None if v else None


def _dt(v: Optional[str]) -> Optional[datetime]:
    """Parse YYYY-MM-DD[THH:MM] into datetime, or return None."""
    if not v or not v.strip():
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v.strip(), fmt)
        except ValueError:
            continue
    return None


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def qa_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_view),
):
    # Quick counts for KPI cards
    async def _count(model, *filters):
        q = select(func.count()).select_from(model)
        for f in filters:
            q = q.where(f)
        return (await db.execute(q)).scalar() or 0

    # Requirements
    total_reqs   = await _count(QARequirement)
    open_reqs    = await _count(QARequirement, QARequirement.status == RequirementStatus.open)

    # Test cases
    total_tcs    = await _count(QATestCase, QATestCase.status == TestCaseStatus.active)
    automated    = await _count(QATestCase, QATestCase.is_automated == True,
                                QATestCase.status == TestCaseStatus.active)

    # Executions (latest cycle — all-time totals)
    exec_pass    = await _count(QATestExecution, QATestExecution.status == ExecutionStatus.pass_)
    exec_fail    = await _count(QATestExecution, QATestExecution.status == ExecutionStatus.fail)
    exec_total   = await _count(QATestExecution)

    # Defects by status
    def_new      = await _count(QADefect, QADefect.status == DefectStatus.new)
    def_open     = await _count(QADefect, QADefect.status.in_([
                       DefectStatus.new, DefectStatus.assigned,
                       DefectStatus.in_progress, DefectStatus.reopened]))
    def_critical = await _count(QADefect,
                                QADefect.severity == DefectSeverity.critical,
                                QADefect.status.notin_([DefectStatus.closed, DefectStatus.wont_fix]))
    def_total    = await _count(QADefect)

    # UAT — all 4 reachable statuses counted (2026-09-24: fail/blocked were
    # silently invisible on the dashboard before; the progress bar's own
    # "% complete" was uat_pass/uat_total, conflating pass rate with
    # execution completion — a failed or blocked scenario still counts as
    # "executed", just not "passed"). 'Draft' isn't counted: it's a status
    # value in the enum but nothing in this app ever creates or sets it
    # (new scenarios default to 'Pending Execution', and the Record Result
    # modal explicitly excludes 'Draft' from its options) — unreachable.
    uat_total    = await _count(QAUATScenario)
    uat_pass     = await _count(QAUATScenario, QAUATScenario.status == UATStatus.pass_)
    uat_fail     = await _count(QAUATScenario, QAUATScenario.status == UATStatus.fail)
    uat_blocked  = await _count(QAUATScenario, QAUATScenario.status == UATStatus.blocked)
    uat_pending  = await _count(QAUATScenario, QAUATScenario.status == UATStatus.pending)
    uat_executed = uat_pass + uat_fail + uat_blocked
    uat_pct_executed = round(uat_executed / uat_total * 100) if uat_total else 0

    # Releases: most recent 5 regardless of status (2026-09-24) — every
    # release eventually reaches 'Deployed', so filtering to only non-
    # deployed/rolled-back ones left this card permanently empty ("No
    # active releases") the moment a project's releases all ship. Recent
    # releases with their real status badges is useful indefinitely; an
    # always-empty card isn't.
    recent_rel_res = await db.execute(
        select(QARelease)
        .order_by(QARelease.created_at.desc())
    )  # all releases (2026-11-04): the card scrolls to fill the SRS card's height
    recent_releases = recent_rel_res.scalars().all()

    # Recent defects (critical/high, open)
    recent_def_res = await db.execute(
        select(QADefect)
        .where(QADefect.status.notin_([DefectStatus.closed, DefectStatus.wont_fix]))
        .where(QADefect.severity.in_([DefectSeverity.critical, DefectSeverity.high]))
        .order_by(QADefect.reported_at.desc())
        .limit(8)
    )
    critical_defects = recent_def_res.scalars().all()

    recent_commits = _get_recent_commits()
    # Historical Pass/Fail proxy from real change history (2026-09-24): with
    # zero QATestExecution rows to draw on, "Bug Fix" changelog entries are
    # the only real, grounded signal of something having gone wrong — a
    # defensible stand-in for "fail" until actual test executions exist.
    # Everything else (Enhancement, Sprint Release, ...) counts as "pass".
    changelog_total = len(recent_commits)
    changelog_fail = sum(1 for c in recent_commits if c["category"] == "Bug Fix")
    changelog_pass = changelog_total - changelog_fail
    changelog_pass_rate = round(changelog_pass / changelog_total * 100, 1) if changelog_total else 0

    # Pass Rate tile: real QATestExecution data when any exists; otherwise
    # the changelog-derived proxy above instead of an unfounded 0% or a
    # blind 100% — grounded in the same 549 real records shown just below,
    # not a made-up number either way.
    if exec_total:
        pass_rate = round(exec_pass / exec_total * 100, 1)
    else:
        pass_rate = changelog_pass_rate

    return templates.TemplateResponse("qa/dashboard.html", {
        "request": request, "current_user": current_user,
        "total_reqs": total_reqs, "open_reqs": open_reqs,
        "total_tcs": total_tcs, "automated": automated,
        "exec_pass": exec_pass, "exec_fail": exec_fail,
        "exec_total": exec_total, "pass_rate": pass_rate,
        "def_new": def_new, "def_open": def_open,
        "def_critical": def_critical, "def_total": def_total,
        "uat_total": uat_total, "uat_pass": uat_pass, "uat_pending": uat_pending,
        "uat_fail": uat_fail, "uat_blocked": uat_blocked,
        "uat_executed": uat_executed, "uat_pct_executed": uat_pct_executed,
        "recent_releases": recent_releases,
        "srs_handbook": SRS_HANDBOOK,
        "critical_defects": critical_defects,
        "recent_commits": recent_commits,
        "changelog_total": changelog_total, "changelog_pass": changelog_pass,
        "changelog_fail": changelog_fail, "changelog_pass_rate": changelog_pass_rate,
    })


# ── Requirements ──────────────────────────────────────────────────────────────

@router.get("/requirements", response_class=HTMLResponse)
async def req_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_view),
    status: str = "", module: str = "", priority: str = "",
):
    q = select(QARequirement).order_by(QARequirement.created_at.desc())
    if status:
        q = q.where(QARequirement.status == status)
    if module:
        q = q.where(QARequirement.module.ilike(f"%{module}%"))
    if priority:
        q = q.where(QARequirement.priority == priority)
    res = await db.execute(q)
    reqs = res.scalars().all()
    tc_counts = {str(rid): n for rid, n in (await db.execute(
        select(QATestCase.requirement_id, func.count()).where(QATestCase.requirement_id.isnot(None))
        .group_by(QATestCase.requirement_id))).all()}
    return templates.TemplateResponse("qa/requirements.html", {
        "request": request, "current_user": current_user,
        "reqs": reqs, "tc_counts": tc_counts,
        "statuses": list(RequirementStatus),
        "priorities": list(RequirementPriority),
        "sources": list(RequirementSource),
        "f_status": status, "f_module": module, "f_priority": priority,
    })


@router.post("/requirements/new")
async def req_create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_edit),
    title: str = Form(...),
    description: str = Form(""),
    source: str = Form(RequirementSource.prd),
    priority: str = Form(RequirementPriority.medium),
    module: str = Form(""),
    req_code: str = Form(""),
):
    r = QARequirement(
        req_code=_s(req_code),
        title=title.strip(),
        description=_s(description),
        source=source,
        priority=priority,
        module=_s(module),
        created_by=current_user.full_name or current_user.username,
    )
    db.add(r)
    await db.commit()
    return RedirectResponse(f"/qa/requirements?success=Requirement+added", status_code=303)


@router.post("/requirements/{req_id}/status")
async def req_status(
    req_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_edit),
    status: str = Form(...),
):
    res = await db.execute(select(QARequirement).where(QARequirement.id == req_id))
    r = res.scalar_one_or_none()
    if not r:
        raise HTTPException(404)
    r.status = status
    await db.commit()
    return RedirectResponse("/qa/requirements?success=Status+updated", status_code=303)


# ── Test Cases ────────────────────────────────────────────────────────────────

@router.get("/test-cases", response_class=HTMLResponse)
async def tc_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_view),
    module: str = "", type_: str = "", status: str = "",
):
    q = select(QATestCase).order_by(QATestCase.created_at.desc())
    if module:
        q = q.where(QATestCase.module.ilike(f"%{module}%"))
    if type_:
        q = q.where(QATestCase.type == type_)
    if status:
        q = q.where(QATestCase.status == status)
    res = await db.execute(q)
    tcs = res.scalars().all()

    # Get last execution per test case (latest pass/fail)
    last_exec: dict[str, QATestExecution] = {}
    if tcs:
        tc_ids = [tc.id for tc in tcs]
        ex_res = await db.execute(
            select(QATestExecution)
            .where(QATestExecution.test_case_id.in_(tc_ids))
            .order_by(QATestExecution.executed_at.desc())
        )
        for ex in ex_res.scalars().all():
            k = str(ex.test_case_id)
            if k not in last_exec:
                last_exec[k] = ex

    # requirements for dropdown
    req_res = await db.execute(select(QARequirement).order_by(QARequirement.req_code))
    reqs = req_res.scalars().all()

    return templates.TemplateResponse("qa/test_cases.html", {
        "request": request, "current_user": current_user,
        "tcs": tcs, "last_exec": last_exec, "reqs": reqs,
        "tc_types": list(TestCaseType),
        "tc_statuses": list(TestCaseStatus),
        "exec_statuses": list(ExecutionStatus),
        "f_module": module, "f_type": type_, "f_status": status,
    })


@router.post("/test-cases/new")
async def tc_create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_edit),
    tc_code: str = Form(""),
    requirement_id: str = Form(""),
    title: str = Form(...),
    scenario: str = Form(""),
    preconditions: str = Form(""),
    steps: str = Form(""),
    expected_result: str = Form(""),
    type_: str = Form(TestCaseType.functional),
    module: str = Form(""),
    is_automated: bool = Form(False),
):
    tc = QATestCase(
        tc_code=_s(tc_code),
        requirement_id=_s(requirement_id) or None,
        title=title.strip(),
        scenario=_s(scenario),
        preconditions=_s(preconditions),
        steps=_s(steps),
        expected_result=_s(expected_result),
        type=type_,
        module=_s(module),
        is_automated=is_automated,
        created_by=current_user.full_name or current_user.username,
    )
    db.add(tc)
    await db.commit()
    return RedirectResponse("/qa/test-cases?success=Test+case+created", status_code=303)


@router.post("/test-cases/{tc_id}/execute")
async def tc_execute(
    tc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_edit),
    status: str = Form(...),
    actual_result: str = Form(""),
    notes: str = Form(""),
    build_version: str = Form(""),
    environment: str = Form("QA"),
):
    res = await db.execute(select(QATestCase).where(QATestCase.id == tc_id))
    tc = res.scalar_one_or_none()
    if not tc:
        raise HTTPException(404)

    ex = QATestExecution(
        test_case_id=tc.id,
        status=status,
        actual_result=_s(actual_result),
        notes=_s(notes),
        build_version=_s(build_version),
        environment=environment,
        executed_by=current_user.full_name or current_user.username,
    )
    db.add(ex)
    await db.commit()
    return RedirectResponse(
        f"/qa/test-cases?success=Execution+logged+({status})", status_code=303
    )


# ── Defects ───────────────────────────────────────────────────────────────────

@router.get("/defects", response_class=HTMLResponse)
async def defect_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_view),
    severity: str = "", status: str = "", module: str = "",
):
    q = select(QADefect).order_by(QADefect.reported_at.desc())
    if severity:
        q = q.where(QADefect.severity == severity)
    if status:
        q = q.where(QADefect.status == status)
    if module:
        q = q.where(QADefect.module.ilike(f"%{module}%"))
    res = await db.execute(q)
    defects = res.scalars().all()

    # test cases for dropdown
    tc_res = await db.execute(
        select(QATestCase)
        .where(QATestCase.status == TestCaseStatus.active)
        .order_by(QATestCase.tc_code)
    )
    tcs = tc_res.scalars().all()

    return templates.TemplateResponse("qa/defects.html", {
        "request": request, "current_user": current_user,
        "defects": defects, "tcs": tcs,
        "severities": list(DefectSeverity),
        "priorities": list(DefectPriority),
        "def_statuses": list(DefectStatus),
        "f_severity": severity, "f_status": status, "f_module": module,
    })


@router.post("/defects/new")
async def defect_create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_edit),
    defect_code: str = Form(""),
    test_case_id: str = Form(""),
    title: str = Form(...),
    description: str = Form(""),
    steps_to_reproduce: str = Form(""),
    expected_result: str = Form(""),
    actual_result: str = Form(""),
    severity: str = Form(DefectSeverity.medium),
    priority: str = Form(DefectPriority.p3),
    module: str = Form(""),
    environment: str = Form("QA"),
    build_version: str = Form(""),
    assigned_to: str = Form(""),
):
    d = QADefect(
        defect_code=_s(defect_code),
        test_case_id=_s(test_case_id) or None,
        title=title.strip(),
        description=_s(description),
        steps_to_reproduce=_s(steps_to_reproduce),
        expected_result=_s(expected_result),
        actual_result=_s(actual_result),
        severity=severity,
        priority=priority,
        module=_s(module),
        environment=environment,
        build_version=_s(build_version),
        assigned_to=_s(assigned_to),
        reported_by=current_user.full_name or current_user.username,
    )
    db.add(d)
    await db.commit()
    return RedirectResponse("/qa/defects?success=Defect+logged", status_code=303)


@router.post("/defects/{defect_id}/status")
async def defect_status(
    defect_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_edit),
    status: str = Form(...),
    root_cause: str = Form(""),
    resolution: str = Form(""),
):
    res = await db.execute(select(QADefect).where(QADefect.id == defect_id))
    d = res.scalar_one_or_none()
    if not d:
        raise HTTPException(404)
    d.status = status
    if _s(root_cause):
        d.root_cause = root_cause
    if _s(resolution):
        d.resolution = resolution
    if status in (DefectStatus.fixed, "Fixed"):
        d.resolved_at = app_now()
    if status in (DefectStatus.closed, "Closed"):
        d.closed_at = app_now()
    await db.commit()
    return RedirectResponse("/qa/defects?success=Defect+updated", status_code=303)


# ── UAT Scenarios ─────────────────────────────────────────────────────────────

@router.get("/uat", response_class=HTMLResponse)
async def uat_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_view),
    status: str = "",
):
    q = select(QAUATScenario).order_by(QAUATScenario.created_at.desc())
    if status:
        q = q.where(QAUATScenario.status == status)
    res = await db.execute(q)
    scenarios = res.scalars().all()

    req_res = await db.execute(select(QARequirement).order_by(QARequirement.req_code))
    reqs = req_res.scalars().all()

    return templates.TemplateResponse("qa/uat.html", {
        "request": request, "current_user": current_user,
        "scenarios": scenarios, "reqs": reqs,
        "uat_statuses": list(UATStatus),
        "f_status": status,
    })


@router.post("/uat/new")
async def uat_create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_edit),
    uat_code: str = Form(""),
    requirement_id: str = Form(""),
    title: str = Form(...),
    scenario: str = Form(""),
    acceptance_criteria: str = Form(""),
    business_owner: str = Form(""),
):
    s = QAUATScenario(
        uat_code=_s(uat_code),
        requirement_id=_s(requirement_id) or None,
        title=title.strip(),
        scenario=_s(scenario),
        acceptance_criteria=_s(acceptance_criteria),
        business_owner=_s(business_owner),
        created_by=current_user.full_name or current_user.username,
    )
    db.add(s)
    await db.commit()
    return RedirectResponse("/qa/uat?success=UAT+scenario+created", status_code=303)


@router.post("/uat/{uat_id}/execute")
async def uat_execute(
    uat_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_edit),
    status: str = Form(...),
    result_notes: str = Form(""),
    feedback: str = Form(""),
    sign_off: bool = Form(False),
):
    res = await db.execute(select(QAUATScenario).where(QAUATScenario.id == uat_id))
    s = res.scalar_one_or_none()
    if not s:
        raise HTTPException(404)
    s.status = status
    s.result_notes = _s(result_notes)
    s.feedback = _s(feedback)
    s.executed_by = current_user.full_name or current_user.username
    s.executed_at = app_now()
    if sign_off:
        s.sign_off_by = current_user.full_name or current_user.username
        s.sign_off_at = app_now()
    await db.commit()
    return RedirectResponse("/qa/uat?success=UAT+result+recorded", status_code=303)


# ── Releases ──────────────────────────────────────────────────────────────────

@router.get("/releases", response_class=HTMLResponse)
async def release_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_view),
):
    res = await db.execute(
        select(QARelease).order_by(QARelease.created_at.desc())
    )
    releases = res.scalars().all()
    return templates.TemplateResponse("qa/releases.html", {
        "request": request, "current_user": current_user,
        "releases": releases,
        "rel_statuses": list(ReleaseStatus),
    })


@router.post("/releases/new")
async def release_create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_admin),
    version: str = Form(...),
    title: str = Form(""),
    description: str = Form(""),
    planned_date: str = Form(""),
    rollback_plan: str = Form(""),
    notes: str = Form(""),
):
    r = QARelease(
        version=version.strip(),
        title=_s(title),
        description=_s(description),
        planned_date=_dt(planned_date),
        rollback_plan=_s(rollback_plan),
        notes=_s(notes),
        created_by=current_user.full_name or current_user.username,
    )
    db.add(r)
    await db.commit()
    return RedirectResponse("/qa/releases?success=Release+created", status_code=303)


@router.post("/releases/{rel_id}/status")
async def release_status(
    rel_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_admin),
    status: str = Form(...),
    notes: str = Form(""),
):
    res = await db.execute(select(QARelease).where(QARelease.id == rel_id))
    r = res.scalar_one_or_none()
    if not r:
        raise HTTPException(404)
    r.status = status
    now = app_now()
    actor = current_user.full_name or current_user.username
    if status == ReleaseStatus.qa_done:
        r.qa_sign_off_by = actor
        r.qa_sign_off_at = now
    elif status == ReleaseStatus.uat_done:
        r.uat_sign_off_by = actor
        r.uat_sign_off_at = now
    elif status == ReleaseStatus.deployed:
        r.deployed_by = actor
        r.release_date = now
    if _s(notes):
        r.notes = (r.notes or "") + f"\n[{now.strftime('%d-%m-%Y %H:%M')}] {notes}"
    await db.commit()
    return RedirectResponse("/qa/releases?success=Release+status+updated", status_code=303)


# ── RTM ───────────────────────────────────────────────────────────────────────

@router.get("/rtm", response_class=HTMLResponse)
async def rtm_view(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_view),
):
    req_res = await db.execute(
        select(QARequirement).order_by(QARequirement.req_code, QARequirement.created_at)
    )
    reqs = req_res.scalars().all()

    # For each requirement load test cases with their latest execution
    rtm_rows = []
    for req in reqs:
        tc_res = await db.execute(
            select(QATestCase)
            .where(QATestCase.requirement_id == req.id)
            .where(QATestCase.status == TestCaseStatus.active)
        )
        tcs = tc_res.scalars().all()

        # Last execution per test case
        last_exec: dict[str, QATestExecution] = {}
        if tcs:
            ex_res = await db.execute(
                select(QATestExecution)
                .where(QATestExecution.test_case_id.in_([t.id for t in tcs]))
                .order_by(QATestExecution.executed_at.desc())
            )
            for ex in ex_res.scalars().all():
                k = str(ex.test_case_id)
                if k not in last_exec:
                    last_exec[k] = ex

        # UAT scenarios for this requirement
        uat_res = await db.execute(
            select(QAUATScenario)
            .where(QAUATScenario.requirement_id == req.id)
        )
        uats = uat_res.scalars().all()

        rtm_rows.append({
            "req": req,
            "test_cases": tcs,
            "last_exec": last_exec,
            "uat_scenarios": uats,
        })

    # Uncovered requirements (no test cases at all)
    uncovered = [r for r in rtm_rows if not r["test_cases"]]

    return templates.TemplateResponse("qa/rtm.html", {
        "request": request, "current_user": current_user,
        "rtm_rows": rtm_rows,
        "uncovered": uncovered,
        "total_reqs": len(reqs),
        "covered_reqs": len([r for r in rtm_rows if r["test_cases"]]),
    })


@router.get("/workflow", response_class=HTMLResponse)
async def workflow_view(
    request: Request,
    tab: str = "new_grn",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(_view),
):
    """Application Workflow — tabs New GRN, Re-GRN and Stage Flows (2026-11-04;
    Stage Flows moved in from the old Stage Workflows tab). Each tab: flow
    diagram (built-in, or one uploaded from Figma), PDF downloads, step journey."""
    return templates.TemplateResponse("qa/workflow.html", {
        "request": request, "current_user": current_user,
        "flows": WORKFLOW_FLOWS, "workflows": STAGE_WORKFLOWS,
        "active_tab": tab if tab in _FLOW_TAB_KEYS else "new_grn",
        "uploads": {k: await _flow_upload(db, k) for k in _FLOW_TAB_KEYS},
    })


def _journeys_page(request, current_user, view, module):
    """User Journeys / Database Diagram (2026-11-05, split from the one
    Journey Diagrams tab): the combined view on top, one module at a time
    below. Data: services/qa_journeys.py; diagrams: static/qa/journeys/*.svg."""
    from services.qa_journeys import MODULES, COMBINED_PHASES, combined_columns
    keys = [m["key"] for m in MODULES]
    return templates.TemplateResponse("qa/journeys.html", {
        "request": request, "current_user": current_user, "view": view,
        "active_module": module if module in keys else keys[0],
        "modules": MODULES, "phases": COMBINED_PHASES, "columns": combined_columns(),
    })


@router.get("/journeys", response_class=HTMLResponse)
async def journeys_view(request: Request, module: str = "", current_user: User = Depends(_view)):
    """QA › User Journeys: the end-to-end journey, then each module's."""
    return _journeys_page(request, current_user, "journeys", module)


@router.get("/database-diagram", response_class=HTMLResponse)
async def database_diagram_view(request: Request, module: str = "", current_user: User = Depends(_view)):
    """QA › Database Diagram: the combined diagram, then each module's."""
    return _journeys_page(request, current_user, "diagrams", module)


# ── Workflow downloads + uploaded diagrams (2026-11-04) ───────────────────────
_FLOW_TAB_KEYS = ("new_grn", "regrn", "stage")
_FLOWS_BY_KEY = {}            # filled after WORKFLOW_FLOWS is defined (below)
_FLOW_UPLOAD_DIR = os.path.join("uploads", "qa_flows")
_FLOW_UPLOAD_MAX = 10 * 1024 * 1024
_FLOW_UPLOAD_TYPES = {"png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml", "pdf": "application/pdf"}


def _flow_or_404(key):
    f = _FLOWS_BY_KEY.get(key)
    if not f:
        raise HTTPException(404, "Flow not found")
    return f


def _pdf_response(data: bytes, filename: str):
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/workflow/{flow_key}/diagram.pdf")
async def workflow_diagram_pdf(flow_key: str, current_user: User = Depends(_view)):
    from utils.workflow_pdf import diagram_pdf
    f = _flow_or_404(flow_key)
    return _pdf_response(diagram_pdf(f["title"], f.get("sub", ""), f["diagram"]),
                         f"OxyPC_{flow_key}_flow_diagram.pdf")


@router.get("/workflow/{flow_key}/journey.pdf")
async def workflow_journey_pdf(flow_key: str, current_user: User = Depends(_view)):
    from utils.workflow_pdf import journey_pdf, stage_flows_pdf
    if flow_key == "stage":
        return _pdf_response(stage_flows_pdf(STAGE_WORKFLOWS), "OxyPC_stage_flows.pdf")
    f = _flow_or_404(flow_key)
    return _pdf_response(journey_pdf(f["title"], f.get("sub", ""), f["steps"]), f"OxyPC_{flow_key}_journey.pdf")


def _upload_key(k):
    return f"qa_flow_upload_{k}"


async def _flow_upload(db, key):
    import json as _json
    from models.settings import AppSetting
    v = (await db.execute(select(AppSetting.value).where(AppSetting.key == _upload_key(key)))).scalar()
    try:
        meta = _json.loads(v) if v else None
    except ValueError:
        return None
    if meta and os.path.exists(os.path.join(_FLOW_UPLOAD_DIR, meta.get("file", ""))):
        return meta
    return None


def _sniff(head: bytes, text_head: str):
    """File type from its first bytes, never from the name."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"%PDF-"):
        return "pdf"
    t = text_head.lstrip("\ufeff \t\r\n").lower()
    if (t.startswith("<?xml") or t.startswith("<svg") or t.startswith("<!--")) and "<svg" in t:
        return "svg"
    return None


_SVG_BAD = re.compile(r"<\s*script|<\s*foreignobject|\bon[a-z]+\s*=|javascript:|<\s*iframe|<\s*embed|<\s*object"
                      r"|xlink:href\s*=\s*[\"'](?!#|data:image/)|<!entity", re.I)


@router.post("/workflow/{flow_key}/upload")
async def workflow_upload(flow_key: str, request: Request, file: UploadFile = File(...),
                          db: AsyncSession = Depends(get_db), current_user: User = Depends(_view)):
    """Upload a flow diagram exported from Figma (PNG / JPG / SVG / PDF, 10 MB).
    Replaces the tab's previous upload. SVGs with scripts or event handlers are
    refused; files are served back only through workflow_uploaded (with a CSP)."""
    import json as _json
    import uuid as _uuid
    from models.settings import AppSetting
    if flow_key not in _FLOW_TAB_KEYS:
        raise HTTPException(404, "Flow not found")
    data = await file.read(_FLOW_UPLOAD_MAX + 1)
    if len(data) > _FLOW_UPLOAD_MAX:
        return JSONResponse({"ok": False, "error": "File is over 10 MB."}, status_code=400)
    ext = _sniff(data[:16], data[:2048].decode("utf-8", "ignore"))
    if ext is None:
        return JSONResponse({"ok": False, "error": "Upload a PNG, JPG, SVG or PDF exported from Figma."}, status_code=400)
    if ext == "svg" and _SVG_BAD.search(data.decode("utf-8", "ignore")):
        return JSONResponse({"ok": False, "error": "This SVG contains scripts or links and can't be shown. "
                                                   "Export it again from Figma, or use PNG / PDF."}, status_code=400)
    os.makedirs(_FLOW_UPLOAD_DIR, exist_ok=True)
    old = await _flow_upload(db, flow_key)
    fname = f"{flow_key}_{_uuid.uuid4().hex}.{ext}"
    with open(os.path.join(_FLOW_UPLOAD_DIR, fname), "wb") as fh:
        fh.write(data)
    meta = {"file": fname, "ext": ext, "name": (file.filename or fname)[:120], "size": len(data),
            "by": current_user.username, "at": app_now().strftime("%d-%m-%Y %H:%M")}
    row = (await db.execute(select(AppSetting).where(AppSetting.key == _upload_key(flow_key)))).scalar_one_or_none()
    if row is None:
        db.add(AppSetting(key=_upload_key(flow_key), value=_json.dumps(meta), description="QA Workflow uploaded diagram",
                          updated_by=current_user.username))
    else:
        row.value, row.updated_by = _json.dumps(meta), current_user.username
    await audit(db, action="QA_FLOW_DIAGRAM_UPLOADED", user=current_user, table_name="app_settings",
                record_id=_upload_key(flow_key), old_value=old, new_value=meta, request=request)
    await db.commit()
    return JSONResponse({"ok": True, "upload": meta})


@router.post("/workflow/{flow_key}/upload/remove")
async def workflow_upload_remove(flow_key: str, request: Request, db: AsyncSession = Depends(get_db),
                                 current_user: User = Depends(_view)):
    """Stop showing the uploaded diagram (the built-in one shows again). The file
    stays on disk; the audit row records which one it was."""
    from models.settings import AppSetting
    row = (await db.execute(select(AppSetting).where(AppSetting.key == _upload_key(flow_key)))).scalar_one_or_none()
    if row is None:
        return JSONResponse({"ok": True})
    old = row.value
    row.value, row.updated_by = "", current_user.username
    await audit(db, action="QA_FLOW_DIAGRAM_REMOVED", user=current_user, table_name="app_settings",
                record_id=_upload_key(flow_key), old_value={"upload": old}, request=request)
    await db.commit()
    return JSONResponse({"ok": True})


@router.get("/workflow/{flow_key}/uploaded")
async def workflow_uploaded(flow_key: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(_view)):
    meta = await _flow_upload(db, flow_key)
    if not meta:
        raise HTTPException(404, "No uploaded diagram")
    path = os.path.join(_FLOW_UPLOAD_DIR, meta["file"])
    return FileResponse(path, media_type=_FLOW_UPLOAD_TYPES[meta["ext"]], headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
        "Content-Disposition": f'inline; filename="{meta["file"]}"'})


# Workflow tab (2026-11-04): New GRN and Re-GRN as they work today. Each step:
# (title, where in the app, what happens, branches [(label, outcome, tone)]).
WORKFLOW_FLOWS = [
    {
        "key": "new_grn",
        "title": "Flow 1 — New GRN",
        "sub": "GRN with invoice → tags entered at IQC → GRN mapped → Stock In → repair pipeline",
        "steps": [
            ("Add GRN", "GRN post IQC → Add GRN",
             "GRN number is generated by the system. Lot numbers, GRN date, location, vendor and state, PO, invoice, vehicle, invoice price and quantities are required; tax is worked out from entity and vendor state.",
             [("E-Way Bill", "Number and date required only when Invoice Price is above ₹50,000", "info"),
              ("Same invoice on a live GRN", "Save refused", "danger"),
              ("Lot numbers", "Existing lot is linked, missing lot is created, lot in Trash is refused", "info")]),
            ("Enter tags at IQC", "Product IQC — Bulk Upload, manual IQC form, or OxyQC agent",
             "Each tag is created at IQC. A row that carries a valid GRN number (or an invoice that matches one GRN) takes that GRN's number, invoice, entity and location.",
             [("Valid GRN / invoice on the row", "Goes straight to Stock In", "success"),
              ("No GRN on the row", "Stays in IQC — listed under GRN Pending", "warning"),
              ("Wrong GRN / invoice, invoice on 2+ GRNs, RAM and Storage swapped", "Row skipped; downloadable file with the reason", "danger"),
              ("Tag already exists", "Duplicate review: Approve Move, Update / Update All, or Add Lot", "warning")]),
            ("GRN Pending for these Tag Numbers", "GRN post IQC → accordion",
             "Every tag at IQC with no GRN, filterable by Device Type, Entity, Invoice and PO; scanned tags are matched and ticked.",
             [("GRN Records → Pending for TRC", "Sending for Selling: back to IQC, GRN cleared · Sold to OxyPC: Stock In", "info")]),
            ("Map GRN", "GRN given at upload, or Map this GRN",
             "Tag takes the GRN number (entity defaults to OxyPC Computers), moves from IQC to Stock In and gets the default stock-in location.",
             [("GRN Stocked", "Tag is counted under the GRN and stays counted even after a later ReGRN", "success"),
              ("Yes As-Is", "Writes a Sub-Lot Number and Assigned To on every stocked tag", "info")]),
            ("Stock In", "Stock Inwards",
             "Tags wait in inventory, optionally grouped into a bucket / carton.",
             [("Move to L1/L2", "L1/L2 Repair, with a pending WorkID", "success"),
              ("Move to TRC Production", "Production Manager queue", "success")]),
            ("Production Manager", "Assign bucket / tag to a department",
             "Hands the work to the shop floor.",
             [("L1/L2 Repair", "Stage L1/L2", "success"),
              ("Stress Test", "Stage Stress Test", "success"),
              ("Cosmetic Repair", "Stage Cosmetic Received", "success")]),
        ],
    },
    {
        "key": "regrn",
        "title": "Flow 2 — Re-GRN (Item ReGRN)",
        "sub": "Move chosen tags onto another GRN — the tag number never changes",
        "steps": [
            ("Pick the tags", "Item ReGRN → Tag Numbers box or CSV",
             "Paste tag numbers or drop a CSV with a barcode column. Any live tag in any stage can be moved; matching ignores case.",
             [("Tag not found", "Reported, nothing moved", "danger"),
              ("Only a trashed copy exists", "Reported as in Trash, not moved", "danger")]),
            ("Change Entities", "Modal → Move tags to GRN",
             "Choose the target GRN (required) and optionally attach an invoice PDF.",
             [("Target GRN has no entity", "Not selectable; server refuses it", "danger")]),
            ("Apply", "Per tag",
             "The old GRN keeps the tag in its GRN Stocked count, then the tag takes the new GRN's number, invoice and entity. If the GRN has a location, the tag moves there (bin cleared).",
             [("Tag was Sold", "Back to Ready to Sale — old sale kept as history, can be sold again", "warning"),
              ("Any other stage", "Stage unchanged", "info"),
              ("Rest of the lot", "Keeps its original GRN", "info")]),
            ("Recorded", "Tag history + audit",
             "A REGRN entry shows in the tag's movement history; one audit row per tag plus one for the batch.",
             [("GRN Stocked", "Tag now counts under the new GRN; the old GRN still counts it", "success")]),
        ],
    },
]


# Flow diagrams (2026-11-04): a swimlane per page / section above each flow's
# step cards. Nodes: (id, lane index, column, row in lane, label, kind) with
# kind = step | decision | ok | warn | bad | end. Edges: (from, to, label).
WORKFLOW_DIAGRAMS = {
    # Detailed (2026-11-04, Pankaj asked for more detail): every decision and
    # outcome from the step journey below, plus where the tag goes after.
    "new_grn": {
        "lanes": [("GRN post IQC", "bi-receipt"), ("Product IQC", "bi-clipboard-check"),
                  ("Stock Inwards", "bi-box-arrow-in-down"), ("Production Manager", "bi-diagram-3"),
                  ("Repair & Production", "bi-tools"), ("Final QC & Sales", "bi-patch-check")],
        "nodes": [
            ("grn", 0, 0, 0, "Add GRN|auto no., invoice, PO", "step"),
            ("ewb", 0, 1, 1, "E-Way Bill|if price > ₹50,000", "warn"),
            ("inv", 0, 1, 0, "Invoice on|live GRN?", "decision"),
            ("refuse", 0, 2, 1, "Save refused|duplicate invoice", "bad"),
            ("lots", 0, 2, 0, "Lots linked|missing lot created", "step"),
            ("tags", 1, 3, 0, "Enter tags|Upload / form / OxyQC", "step"),
            ("chk", 1, 4, 0, "Row|valid?", "decision"),
            ("dup", 1, 5, 1, "Duplicate review|Move · Update · Add Lot", "warn"),
            ("skip", 1, 5, 2, "Row skipped|reason file", "bad"),
            ("pend", 0, 5, 0, "GRN Pending|filter & scan tags", "warn"),
            ("trc", 0, 6, 0, "Pending|for TRC?", "decision"),
            ("sell", 0, 7, 1, "Send for Selling|back to IQC, GRN cleared", "warn"),
            ("map", 0, 7, 0, "Map GRN|GRN Stocked", "step"),
            ("stock", 2, 8, 0, "Stock In|default location", "step"),
            ("asis", 2, 8, 1, "As-Is lot|Sub-Lot + Assigned To", "warn"),
            ("pm", 3, 9, 0, "Assign bucket|to a department", "step"),
            ("l12", 4, 10, 0, "L1/L2 Repair|pending WorkID", "ok"),
            ("stress", 4, 10, 1, "Stress Test", "ok"),
            ("cos", 4, 10, 2, "Cosmetic|& Paint", "ok"),
            ("fqc", 5, 11, 0, "Final QC|Pass / Hold", "step"),
            ("rts", 5, 12, 0, "Ready to Sale", "end"),
        ],
        "edges": [("grn", "inv", ""), ("grn", "ewb", "Invoice > ₹50k"), ("inv", "refuse", "Yes"),
                  ("inv", "lots", "No"), ("lots", "tags", ""), ("tags", "chk", ""),
                  ("chk", "pend", "No GRN"), ("chk", "dup", "Tag exists"), ("chk", "skip", "Wrong GRN / RAM-SSD swap"),
                  ("chk", "stock", "Valid GRN"), ("pend", "trc", ""), ("trc", "map", "Sold to OxyPC"),
                  ("trc", "sell", "Selling"), ("map", "stock", ""), ("map", "asis", "Yes As-Is"),
                  ("stock", "l12", "Move to L1/L2"), ("stock", "pm", "TRC Production"),
                  ("pm", "l12", ""), ("pm", "stress", ""), ("pm", "cos", ""),
                  ("l12", "stress", ""), ("stress", "cos", "Pass"), ("cos", "fqc", ""), ("fqc", "rts", "Pass")],
    },
    "regrn": {
        "lanes": [("Item ReGRN", "bi-arrow-left-right"), ("Move-to-GRN modal", "bi-window-stack"),
                  ("Tag record", "bi-upc-scan"), ("Location", "bi-geo-alt"), ("History & Audit", "bi-journal-text")],
        "nodes": [
            ("pick", 0, 0, 0, "Pick tags|paste or CSV, any stage", "step"),
            ("found", 0, 1, 0, "Found &|live?", "decision"),
            ("rep", 0, 2, 0, "Reported|not found / in Trash", "bad"),
            ("grn", 1, 2, 0, "Choose target GRN|required", "step"),
            ("ent", 1, 3, 0, "GRN has|entity?", "decision"),
            ("refuse", 1, 4, 1, "Refused|not selectable", "bad"),
            ("pdf", 1, 4, 0, "Invoice PDF|optional", "step"),
            ("apply", 1, 5, 0, "Apply|per tag", "step"),
            ("old", 2, 6, 0, "Old GRN keeps|its Stocked count", "ok"),
            ("takes", 2, 7, 0, "Takes new GRN|no., invoice, entity", "step"),
            ("sold", 2, 8, 0, "Was it|Sold?", "decision"),
            ("rts", 2, 9, 0, "Ready to Sale|old sale kept", "warn"),
            ("same", 2, 9, 1, "Stage unchanged", "ok"),
            ("loc", 3, 8, 0, "Moves to GRN|location, bin cleared", "step"),
            ("hist", 4, 10, 0, "REGRN in history|audit: tag + batch", "end"),
        ],
        "edges": [("pick", "found", ""), ("found", "rep", "No"), ("found", "grn", "Yes"),
                  ("grn", "ent", ""), ("ent", "refuse", "No"), ("ent", "pdf", "Yes"), ("pdf", "apply", ""),
                  ("apply", "old", ""), ("old", "takes", ""), ("takes", "sold", ""),
                  ("takes", "loc", "GRN has location"), ("sold", "rts", "Yes"), ("sold", "same", "No"),
                  ("rts", "hist", ""), ("same", "hist", ""), ("loc", "hist", "")],
    },
}

_DG = {"label_w": 150, "col_w": 172, "node_w": 136, "node_h": 46, "row_h": 58, "pad": 10}


def _diagram_layout(d: dict) -> dict:
    """Coordinates for the SVG: lanes (y, h), nodes (x, y, w, h, lines), edges (path, label x/y)."""
    g = _DG
    rows = [1] * len(d["lanes"])
    for _id, lane, _c, row, *_ in d["nodes"]:
        rows[lane] = max(rows[lane], row + 1)
    lanes, y = [], 0
    for i, (name, icon) in enumerate(d["lanes"]):
        h = rows[i] * g["row_h"] + g["pad"] * 2
        lanes.append({"name": name, "icon": icon, "y": y, "h": h, "alt": i % 2})
        y += h
    ncols = max(n[2] for n in d["nodes"]) + 1
    width, height = g["label_w"] + ncols * g["col_w"] + 10, y
    nodes = {}
    for nid, lane, col, row, label, kind in d["nodes"]:
        w = g["node_w"]
        x = g["label_w"] + col * g["col_w"] + (g["col_w"] - w) / 2
        ny = lanes[lane]["y"] + g["pad"] + row * g["row_h"] + (g["row_h"] - g["node_h"]) / 2
        nodes[nid] = {"id": nid, "x": x, "y": ny, "w": w, "h": g["node_h"], "kind": kind,
                      "lines": label.split("|"), "cx": x + w / 2, "cy": ny + g["node_h"] / 2}
    edges, elbows = [], {}
    for a, b, label in d["edges"]:
        A, B = nodes[a], nodes[b]
        if B["x"] > A["x"] + 1:                       # forward: right side -> left side, elbow
            x1, y1, x2, y2 = A["x"] + A["w"], A["cy"], B["x"], B["cy"]
            if A["kind"] == "decision":
                x1 = A["cx"] + 34                      # diamond tip
            if B["kind"] == "decision":
                x2 = B["cx"] - 34
            if abs(y1 - y2) > 1:
                k = elbows.get(a, 0)
                elbows[a] = k + 1
                mx = x1 + 8 + 6 * k                    # each elbow from one node gets its own lane
            else:
                mx = (x1 + x2) / 2
            path = f"M{x1:.0f},{y1:.0f} H{mx:.0f} V{y2:.0f} H{x2 - 2:.0f}"
            lx, ly, anchor = x2 - 6, y2 - 5, "end"       # label just before the arrowhead
        else:                                          # same column: vertical
            x1, x2 = A["cx"], B["cx"]
            y1, y2 = (A["y"] + A["h"], B["y"]) if B["y"] > A["y"] else (A["y"], B["y"] + B["h"])
            path = f"M{x1:.0f},{y1:.0f} V{y2:.0f}"
            lx, ly, anchor = x1 + 5, (y1 + y2) / 2, "start"
        edges.append({"path": path, "label": label, "lx": lx, "ly": ly, "anchor": anchor})
    return {"lanes": lanes, "nodes": list(nodes.values()), "edges": edges, "w": width, "h": height,
            "label_w": g["label_w"]}


for _f in WORKFLOW_FLOWS:
    if _f.get("key") in WORKFLOW_DIAGRAMS:
        _f["diagram"] = _diagram_layout(WORKFLOW_DIAGRAMS[_f["key"]])
    _FLOWS_BY_KEY[_f["key"]] = _f


# ── Stage Workflows tab — per-stage-transition detail + download ─────────────
# Each entry: key, title, stages (ordered list of stage names), and a detail
# block describing what actually happens at each hop (used both on-screen in
# the "Create Detail Workflow" modal and in the downloaded text file).
STAGE_WORKFLOWS = [  # rewritten 2026-11-04 from the current stage handlers
    {
        "key": "new_grn",
        "title": "New GRN → IQC → Stock In",
        "stages": ["GRN", "IQC", "Stock In"],
        "detail": [
            ("Add GRN", "GRN post IQC → Add GRN. GRN number generated by the system; E-Way Bill required only above ₹50,000 invoice price; a duplicate live invoice is refused; lot numbers are linked or created."),
            ("IQC", "Tags enter through Bulk Upload, the manual IQC form or the OxyQC agent. A row with a valid GRN / invoice goes straight to Stock In; a row without a GRN stays in IQC; bad rows are skipped with a reason file; existing tags go to duplicate review."),
            ("Map GRN", "Tags in GRN Pending get their GRN (at upload or via Map this GRN) and move IQC → Stock In. GRN Stocked counts them and keeps the count after any ReGRN."),
            ("Stock In", "Move to L1/L2 or to TRC Production; Production Manager assigns L1/L2, Stress Test or Cosmetic Received."),
        ],
    },
    {
        "key": "regrn",
        "title": "Re-GRN (Item ReGRN)",
        "stages": ["Any stage", "New GRN", "Same stage (Sold → Ready to Sale)"],
        "detail": [
            ("Pick tags", "Paste or upload tag numbers; any live tag in any stage qualifies. Not found or trashed tags are reported and not moved."),
            ("Choose GRN", "Change Entities → Move tags to GRN (must have an entity); invoice PDF optional."),
            ("Apply", "Old GRN keeps the tag in GRN Stocked; tag takes the new GRN number, invoice, entity and GRN location. Tag number never changes."),
            ("Stage", "Sold tags go back to Ready to Sale (old sale kept as history); every other stage is unchanged. A REGRN entry is added to the tag history."),
        ],
    },
    {
        "key": "repair",
        "title": "L1/L2 ↔ L3/L4 Repair",
        "stages": ["L1/L2", "L3/L4", "L1/L2", "Stress Test"],
        "detail": [
            ("L1/L2 Repair", "Engineer works the tag; parts requested against it. Mark Complete is blocked while PNA parts are open."),
            ("Request L3/L4", "Escalates to L3/L4 with an L3L4 WorkID."),
            ("L3/L4 Complete / Scrap", "Complete sends the tag back to L1/L2 (Returned from L3/L4). Scrap (Normal or Replacement, reason required) also returns to L1/L2, then Back to Inventory moves it to Tags Scrapped."),
            ("Mark Complete", "L1/L2 → Stress Test with a STRS WorkID. Exception: a customer return (Return for Repair / external tag) goes straight back to Sold."),
        ],
    },
    {
        "key": "stress",
        "title": "Stress Test",
        "stages": ["Stress Test", "Cosmetic Received", "L1/L2 (fail)"],
        "detail": [
            ("Stress Test", "Tag is tested against its STRS WorkID."),
            ("Pass", "Complete to Paint → Cosmetic Received."),
            ("Fail", "Back to L1/L2 (or a chosen stage from the QC page). The 3rd fail is flagged as recommend scrap."),
        ],
    },
    {
        "key": "cosmetic",
        "title": "Cosmetic & Paint",
        "stages": ["Cosmetic Received", "Cleaning", "Putty", "Dry Sanding", "Masking", "Painting", "Water Sanding", "Cosmetic Completed", "Final QC"],
        "detail": [
            ("Forward Move", "Each step is moved on with an assignee: Received → Cleaning → Putty → Dry Sanding → Masking → Painting → Water Sanding → Completed → Final QC."),
            ("Skip Cosmetic", "Any cosmetic step can jump straight to Final QC."),
            ("Fail", "Fail on Received, Cleaning, Water Sanding or Completed sends the tag to L1/L2."),
            ("Paint Fail", "Fail on Water Sanding sends it back to Painting."),
        ],
    },
    {
        "key": "final_qc",
        "title": "Final QC → Ready to Sale",
        "stages": ["Final QC", "Pass Hold / Fail Hold", "Ready to Sale"],
        "detail": [
            ("Final QC", "Inspector records Pass or Fail (Fail needs a reason)."),
            ("Pass Hold", "Price is finalised once per device; Move sends the tag to Ready to Sale."),
            ("Fail Hold", "Move by failure reason: Hardware → L1/L2 (or TRC Production when no engineer resolves), Software → Stress Test, Cosmetic → Cosmetic Received."),
        ],
    },
    {
        "key": "ready_sold",
        "title": "Ready to Sale → Sold",
        "stages": ["Ready to Sale", "Pre-Sale QC", "Sold"],
        "detail": [
            ("Ready to Sale", "Listed on Ready to Sale (Tags / Lots). Pre-Sale QC is a WorkID queue — the tag stays in Ready to Sale; Mark Complete needs Pass or Fail (Fail needs a reason)."),
            ("Blocked / On Hold", "A flag, not a stage: a blocked tag cannot be sold."),
            ("Sold", "New Tag Sale creates the sale record. A sale is allowed from any stage unless the tag is blocked."),
            ("On Rent", "Rentals move a tag Ready to Sale ↔ On Rent; return or cancel brings it back."),
        ],
    },
    {
        "key": "customer_return",
        "title": "Customer Return → Return Stock",
        "stages": ["Sold", "Stock In (return)", "L1/L2", "Sold"],
        "detail": [
            ("Return from Customer", "Tag goes to Stock In marked as a return — Return for Repair or Replaced by another tag. Replace Now sells a Ready to Sale replacement at ₹0. External tags can be returned too."),
            ("Return Stock", "Assign Bucket, then Production Manager → L1/L2."),
            ("L1/L2 Mark Complete", "Return for Repair goes back to Sold — no new sale, no Stress WorkID."),
            ("Verify Payment → Ready to Dispatch", "A receipt number is required when a paid repair cost is due; Ready to Dispatch is refused until payment is verified, then a new sale copied from the original is created and the tag shows on Gate Pass."),
        ],
    },
    {
        "key": "scrap",
        "title": "Scrap → Scrap for Sale",
        "stages": ["Scrapped / L3/L4 scrap", "Tags Scrapped", "Sold"],
        "detail": [
            ("Scrap decision", "From L3/L4 scrap (via L1/L2 Back to Inventory) or a scrapped tag's Move to Inventory, the tag lands in Tags Scrapped with grade Scrap."),
            ("Scrap for Sale", "Sold through the Scrap-for-Sale custom sale."),
        ],
    },
]
_STAGE_WORKFLOWS_BY_KEY = {w["key"]: w for w in STAGE_WORKFLOWS}


@router.get("/stage-workflows", response_class=HTMLResponse)
async def stage_workflows_view(
    request: Request,
    current_user: User = Depends(_view),
):
    """Old Stage Workflows tab — moved into Workflow → Stage Flows (2026-11-04)."""
    return RedirectResponse("/qa/workflow?tab=stage", status_code=302)


@router.get("/stage-workflows/{workflow_key}/download")
async def stage_workflow_download(
    workflow_key: str,
    current_user: User = Depends(_view),
):
    wf = _STAGE_WORKFLOWS_BY_KEY.get(workflow_key)
    if not wf:
        raise HTTPException(404, "Workflow not found")

    lines = [wf["title"], "=" * len(wf["title"]), "", "Stages: " + " > ".join(wf["stages"]), "", "Detail:"]
    for stage_name, desc in wf["detail"]:
        lines.append(f"- {stage_name}: {desc}")
    content = "\n".join(lines) + "\n"

    from fastapi.responses import PlainTextResponse
    filename = f"workflow_{workflow_key}.txt"
    return PlainTextResponse(content, headers={
        "Content-Disposition": f'attachment; filename="{filename}"'
    })


# ── SRS Documents (Technical Spec / Functional Spec) ──────────────────────────

_TSD_CONTENT = """OXYPC INVENTORY — TECHNICAL SPECIFICATION DOCUMENT (TSD)
==========================================================
Generated from the QA Dashboard — reflects the current build.

1. SYSTEM ARCHITECTURE
-----------------------
- Backend: FastAPI (Python), async SQLAlchemy ORM
- Database: PostgreSQL (Supabase-hosted in production)
- Templating: Jinja2 server-rendered HTML, Bootstrap 5
- Deployment: Railway (project "Inventory Lifecycle", service "oxypc-inventory")
- Process model: single uvicorn worker, manual restart (no autoreload)

2. CORE MODULES
---------------
- Sourcing & GRN (Goods Receipt Note)
- IQC (Incoming Quality Check) — Product IQC, duplicate Tag/Lot review
- Stock Inward / TRC Production / Repair (L1/L2/L3) / Cosmetic / Final QC
- Overall Inventory — Tag Number, Lot Based Summary, Model Based Summary views
- Sales — Telecalling, Showroom/Counter Sale, Dispatch, Returns
- Dealer Management & Trade Partner Portal (dealer-facing PWA)
- Spare Parts — Sourcing Request, GRN, Consumption, Harvest tracking
- CRM — Contacts, Purchase Orders, Sourcing Deals, Quotations
- Admin — User Management, Master Data, Permission Matrix, Audit Log
- QA/UAT Tracking — Requirements, Test Cases, Defects, UAT, Releases, RTM

3. DATA MODEL CONVENTIONS
-------------------------
- Primary keys: uuid (or bigserial for high-volume tables)
- Soft delete: is_active flag / is_trashed flag, no physical deletes on
  serialised assets, certificates, or compliance records
- Audit: append-only audit_logs table capturing actor, before/after values,
  action, and timestamp for every state-changing operation
- Stage transitions: validated via the AllowedTransition table through
  services/control_engine.py — no manual/unvalidated stage overrides

4. SECURITY
-----------
- Session-based auth for staff (role-gated via require_module_perm),
  separate JWT-based auth for the Trade Partner dealer portal
- CSRF token verification (verify_csrf) on every state-changing POST
- RBAC enforced per-module via the Permission Matrix (admin-configurable)
- Passwords hashed (never stored in plaintext); portal login credentials
  are per-dealer and independently resettable by an admin

5. INTEGRATIONS
---------------
- WhatsApp messaging (per-user session, multi-client)
- Railway CLI for production database operations (audited, approval-gated)

6. NON-FUNCTIONAL REQUIREMENTS
-------------------------------
- No client-side pagination on core reports — server-computed lists
- DataTable sorting/pagination on high-volume list screens
- Regression sweep (_scan_routes.py) run before every commit — validates
  every parameter-free GET route returns a non-error response

This document is generated on demand from the QA Dashboard and reflects the
live module list as of the current codebase — regenerate after any major
architectural change.
"""

_FSD_CONTENT = """OXYPC INVENTORY — FUNCTIONAL SPECIFICATION DOCUMENT (FSD)
===========================================================
Generated from the QA Dashboard — reflects the current build.

1. PURPOSE
----------
OxyPC Inventory Lifecycle is the operational ERP for OxyPC Computers' IT asset
refurbishment lifecycle — from sourcing/GRN through IQC, repair, cosmetic
and final QC, into inventory, and out through sales/dispatch or the dealer
trade partner portal.

2. USER ROLES
-------------
- Admin — full system access, dashboards, master data, permissions
- Inventory Manager — stock movement, bucket allocation, lot management
- IQC / QC staff — incoming inspection, grading, stage moves
- Repair (L1/L2/L3) staff — device repair workflow
- Telecaller — outbound dealer calling, quotations, telecalling sales
- Sales / Counter Sale Executive — showroom sales, dispatch
- Dealer (Trade Partner portal) — token booking, browsing floors, own orders

3. KEY WORKFLOWS
-----------------
a. Sourcing > GRN
   Vendor/lot sourced -> GRN raised -> devices registered against the lot.
b. GRN > IQC
   GRN'd devices enter Incoming Quality Check; grade, device type, and
   duplicate Tag/Lot conflicts are resolved before entering the pipeline.
c. IQC > Stock IN > Repair > Cosmetic > Final QC > Ready To Sale
   Devices move through validated stage transitions only (AllowedTransition
   rules); every move is logged with actor + timestamp.
d. Ready to Sale > Sold
   Devices are sold via Telecalling, Showroom/Counter Sale, or Procurement
   channel; sale price, buyer, and channel are recorded.
e. Sold > Return
   Returned devices re-enter the pipeline with return_status and replaced
   flags tracked for warranty/audit purposes.

4. REPORTING & DASHBOARDS
--------------------------
- Admin Dashboard — 12+ stat cards (Products, Stage Products, GRN, Parts,
  Dealers, Accounts, PO, Source Requests, In/Out, Sales) + 5 chart groups
  (weekly product/parts movement, stage distribution, sourcing/sales price)
- QA Dashboard — Requirements, Test Cases, Defects, UAT Board, Releases,
  RTM, Stage Workflows, SRS Documents
- Trade Partner Portal — dealer-facing floors, token booking, order history

5. ACCEPTANCE CRITERIA (SAMPLE)
--------------------------------
- A device cannot move to a stage not explicitly allowed from its current
  stage (enforced server-side, not just in the UI).
- Every stage move, sale, and return writes an audit_logs entry.
- Dealer portal login only works for dealers with portal_enabled = true
  and a registered portal_phone.
- Admin Dashboard card totals reconcile against a direct DB count query.

This document is generated on demand from the QA Dashboard and reflects the
live workflow list as of the current codebase — regenerate after any major
process change.
"""


@router.get("/srs/technical-spec/download")
async def srs_technical_spec_download(current_user: User = Depends(_view)):
    from fastapi.responses import Response
    from utils.srs_pdf import build_spec_pdf
    pdf_bytes = build_spec_pdf(
        doc_code="TSD-1.0",
        title="OxyPC Inventory Lifecycle — Technical Specification Document",
        subtitle="Architecture, data model, security, and integrations",
        raw_content=_TSD_CONTENT,
        generated_on=app_now().strftime("%d %b %Y"),
    )
    return Response(pdf_bytes, media_type="application/pdf", headers={
        "Content-Disposition": 'attachment; filename="OxyPC_Technical_Specification_Document.pdf"'
    })


@router.get("/srs/functional-spec/download")
async def srs_functional_spec_download(current_user: User = Depends(_view)):
    from fastapi.responses import Response
    from utils.srs_pdf import build_spec_pdf
    pdf_bytes = build_spec_pdf(
        doc_code="FSD-1.0",
        title="OxyPC Inventory Lifecycle — Functional Specification Document",
        subtitle="Roles, workflows, and acceptance criteria",
        raw_content=_FSD_CONTENT,
        generated_on=app_now().strftime("%d %b %Y"),
    )
    return Response(pdf_bytes, media_type="application/pdf", headers={
        "Content-Disposition": 'attachment; filename="OxyPC_Functional_Specification_Document.pdf"'
    })


# Design System document (2026-10-15) — a fixed PDF kept in qa_docs/ (shipped
# with the app; docs/ is excluded from the Docker image), served behind the
# same QA view permission as the other SRS documents.
_DESIGN_SYSTEM_PDF = "OxyPC_Inventory_Lifecycle_Design_System_2.4.pdf"  # v2.4, 21 Oct 2026


@router.get("/srs/design-system/download")
async def srs_design_system_download(current_user: User = Depends(_view)):
    import os
    from fastapi.responses import FileResponse
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "qa_docs", _DESIGN_SYSTEM_PDF)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Design System document not found")
    return FileResponse(path, media_type="application/pdf", filename=_DESIGN_SYSTEM_PDF)


# Product & Architecture Handbook (2026-11-04) — the 8 documents plus the
# combined handbook, fixed PDFs in qa_docs/handbook/ (last updated 25 Oct 2026).
SRS_HANDBOOK = [
    ("handbook", "00_OxyPC_Handbook_All_Documents.pdf", "Complete Handbook", "All 8 documents in one file", 222),
    ("key-findings", "01_Key_Findings.pdf", "Key Findings", "Security, data-safety and process gaps, what to fix first", 3),
    ("product-overview", "02_Product_Overview.pdf", "Product Overview", "Vision, roadmap and KPI definitions", 21),
    ("personas", "03_Personas_and_Journeys.pdf", "Personas & Journeys", "The 12 roles that handle a device, and their journeys", 15),
    ("business-process", "04_Business_Process_and_Rules.pdf", "Business Process & Rules", "How a device moves through every stage, and the rules", 32),
    ("access-security", "05_Access_and_Security.pdf", "Access & Security", "Role & permission matrix, security design", 29),
    ("technical-architecture", "06_Technical_Architecture.pdf", "Technical Architecture", "System design, data model, integrations", 61),
    ("operations-testing", "07_Operations_and_Testing.pdf", "Operations & Testing", "Deploy, run, recover; quality targets and sign-off", 35),
    ("prds", "08_Product_Requirements_PRDs.pdf", "Product Requirements (PRDs)", "Top improvement opportunities, plus the PRD template", 24),
]
_SRS_HANDBOOK_BY_KEY = {k: f for k, f, *_ in SRS_HANDBOOK}


@router.get("/srs/handbook/{doc_key}/download")
async def srs_handbook_download(doc_key: str, current_user: User = Depends(_view)):
    import os
    from fastapi.responses import FileResponse
    fname = _SRS_HANDBOOK_BY_KEY.get(doc_key)
    if not fname:
        raise HTTPException(status_code=404, detail="Document not found")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qa_docs", "handbook", fname)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Document not found")
    return FileResponse(path, media_type="application/pdf", filename="OxyPC_" + fname[3:])


@router.get("/srs/module-map/download")
async def srs_module_map_pdf(current_user: User = Depends(_view)):
    from fastapi.responses import Response
    from utils.module_map_doc import build_module_map_pdf
    pdf_bytes = build_module_map_pdf(generated_on=app_now().strftime("%d %b %Y"))
    return Response(pdf_bytes, media_type="application/pdf", headers={
        "Content-Disposition": 'attachment; filename="OxyPC_Module_Interconnectivity_Map.pdf"'
    })


@router.get("/srs/module-map/download.pptx")
async def srs_module_map_pptx(current_user: User = Depends(_view)):
    from fastapi.responses import Response
    from utils.module_map_doc import build_module_map_pptx
    ppt_bytes = build_module_map_pptx(generated_on=app_now().strftime("%d %b %Y"))
    return Response(
        ppt_bytes,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={
            "Content-Disposition":
                'attachment; filename="OxyPC_Module_Interconnectivity_Map.pptx"'
        },
    )
