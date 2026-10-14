import asyncio
import sys
import os
import webbrowser
import threading
import time
import logging
import traceback as _tb

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.exc import ProgrammingError, DataError, DBAPIError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from limiter import limiter

from config import APP_HOST, APP_PORT, APP_NAME, write_default_config, ALLOWED_ORIGINS

# Debug mode — set OXYPC_DEBUG=1 to show full exception detail in error pages (LAN dev only)
DEBUG = os.environ.get("OXYPC_DEBUG", "0") == "1"

# Write default config on first run
if not os.path.exists(os.path.join(os.path.dirname(__file__), "config.ini")):
    write_default_config()

app = FastAPI(title=APP_NAME, version="1.0.0")

# Rate limiting — global default: 100/min per IP; login is overridden to 5/min
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ── CORS — ecosystem apps (Customer Portal, AI Layer, ESG, Finance) ───────────
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
    expose_headers=["X-Total-Count"],
    max_age=600,
)

# ── HTTPS redirect — Railway terminates SSL at the edge and sets
# Upgrade plain-HTTP requests to HTTPS behind Railway's proxy.
#
# uvicorn runs with proxy_headers=True (see uvicorn.run below), so request.url.scheme
# already reflects the real client scheme from X-Forwarded-Proto. We redirect off that
# normalized value, NOT the raw header — the previous version compared the raw header
# `== "http"`, which loops forever when a proxy hop presents a comma-joined or bare
# "http" value on an already-HTTPS request (the cause of ERR_TOO_MANY_REDIRECTS).
#
# Loop-safety: we only redirect when the resolved scheme is exactly "http", so the
# redirected HTTPS request (scheme "https") never redirects again. /health is exempt
# so Railway's health check is never bounced. 307 keeps method/body and is not cached.
@app.middleware("http")
async def _force_https(request: Request, call_next):
    # Only a proxy in front of us sets X-Forwarded-Proto; when it is absent (local dev,
    # direct connection) we do nothing, exactly as before. When present, we redirect off
    # its FIRST token — never a bare `== "http"` on the whole header — so a comma-joined
    # multi-hop value like "https,http" resolves to "https" and is passed straight through.
    # That is the fix for the intermittent ERR_TOO_MANY_REDIRECTS: an already-HTTPS request
    # can never bounce again. /health is exempt so Railway's health check is never redirected.
    xfp = request.headers.get("x-forwarded-proto")
    if xfp:
        proto = xfp.split(",")[0].strip().lower()
        if proto == "http" and request.url.path != "/health":
            return RedirectResponse(url=str(request.url.replace(scheme="https")),
                                    status_code=307)
    return await call_next(request)


# ── Never let a browser hold a stale PAGE across a deploy ─────────────────────
# TemplateResponse sends no Cache-Control, no ETag and no Last-Modified, so what
# a browser does with a rendered page is left to its heuristics — and the answer
# differs by browser, by whether the page was reached by link, reload, or the
# back button, and by whether bfcache kept the whole document alive. The visible
# symptom is always the same and always looks like a failed deploy: the code is
# live on the server, and the user is looking at yesterday's form.
#
# no-store (not no-cache) because there is nothing worth revalidating: every
# page here is per-user, permission-filtered and built from live inventory. It
# also keeps a logged-in page out of the back button after logout on a shared
# floor machine.
#
# /static is deliberately untouched: CSS/JS/vendor bundles SHOULD stay cached,
# and they are already cache-busted by the ?v=ASSET_VERSION stamp that
# templates_config.py derives from asset mtime. An explicit Cache-Control set
# by a route (a file download, an export) wins over this default.
@app.middleware("http")
async def _no_store_pages(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static"):
        return resp
    if resp.headers.get("cache-control"):
        return resp
    if resp.headers.get("content-type", "").split(";")[0].strip() == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Error logger (writes to errors.log next to main.py) ───────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_err_logger = logging.getLogger("oxypc.errors")
_err_logger.setLevel(logging.ERROR)
_err_handler = logging.FileHandler(os.path.join(BASE_DIR, "errors.log"), encoding="utf-8")
_err_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_err_logger.addHandler(_err_handler)
if not logging.getLogger().handlers:          # also echo to console if no root handler
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

# Static files
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """No favicon asset yet — respond 204 instead of letting the browser's
    automatic /favicon.ico request 404 (noise in logs, no functional impact)."""
    from fastapi import Response
    return Response(status_code=204)

# Uploads directory — CRM file attachments (product records, etc.)
_uploads_dir = os.path.join(BASE_DIR, "uploads")
os.makedirs(os.path.join(_uploads_dir, "crm"), exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_uploads_dir), name="uploads")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Import and include all routers
from routers.health import router as health_router
from routers.auth import router as auth_router
from routers.dashboard import router as dashboard_router
from routers.fieldops import router as fieldops_router
from routers.admin import router as admin_router
from routers.iqc import router as iqc_router
from routers.stock import router as stock_router
from routers.repair import router as repair_router
from routers.qc import router as qc_router
from routers.sales import router as sales_router
from routers.spare_parts import router as spare_parts_router
from routers.reports import router as reports_router
from routers.master import router as master_router
import models.role_permissions  # ensures tables are in Base.metadata for db_validator
import models.ticket  # ensures tickets table is created on startup
from routers.bulk_upload import router as bulk_upload_router
from routers.cosmetic import router as cosmetic_router
from routers.workid_status import router as workid_status_router
from routers.scrap import router as scrap_router
from routers.scrap_for_sale import router as scrap_for_sale_router
import models.scrap_for_sale  # ensure scrap_for_sale table is in Base.metadata
from routers.devices import router as devices_router
from routers.transfers import router as transfers_router
from routers.part_requests import router as part_requests_router
from routers.dispatch import router as dispatch_router
from routers.model_requests import router as model_requests_router
from routers.model_requested import router as model_requested_router
import models.model_requests  # ensure model_requests table is in Base.metadata
from routers.attendance import router as attendance_router
from routers.attendance_group_config import router as attendance_group_config_router
from routers.telesales_dashboard import router as telesales_dashboard_router
from routers.dealers import router as dealers_router
from routers.telecalling import router as telecalling_router
from routers.m_telecalling import router as m_telecalling_router
# WhatsApp integration removed — router import disabled (unhooked, file left on disk)
# from routers.whatsapp import router as whatsapp_router
from routers.grn import router as grn_router
from routers.entity_movement import router as entity_movement_router
from routers.part_estimation import router as part_estimation_router
from routers.extended_warranty import router as extended_warranty_router
from routers.parts_grn import router as parts_grn_router
import models.parts_grn  # ensure parts_grn tables are in Base.metadata
import models.company  # ensure companies table is in Base.metadata
from routers.procure_dashboard import router as procure_dashboard_router
from routers.stage_control import router as stage_control_router
from routers.market import router as market_router
from routers.inventory_location import router as inventory_location_router
from routers.physical_audits import router as physical_audits_router
from routers.block_tags import router as block_tags_router
from routers.product_master import router as product_master_router
import models.product_master  # ensure Product Master tables are in Base.metadata
from routers.stress_api import router as stress_api_router
from routers.iqc_api import router as iqc_api_router
from routers.qa_uat import router as qa_uat_router
from routers.crm_dashboard import router as crm_dashboard_router
from routers.crm_contacts import router as crm_contacts_router
from routers.crm_sourcing import router as crm_sourcing_router
from routers.crm_sales import router as crm_sales_router
from routers.crm_quotes import router as crm_quotes_router
from routers.crm_activities import router as crm_activities_router
from routers.crm_price_matrix import router as crm_price_matrix_router
from routers.invoices import router as invoices_router
from routers.gate_pass import router as gate_pass_router
from routers.part_sales import router as part_sales_router
from routers.partner_payments import router as partner_payments_router
from routers.crm_purchase_orders import router as crm_purchase_orders_router
from routers.deals import router as deals_router
from routers.accounts import router as accounts_router
from routers.crm_reports import router as crm_reports_router
from routers.crm_assign_leads import router as crm_assign_leads_router
from routers.assign_dealer_leads import router as assign_dealer_leads_router
from routers.tickets import router as tickets_router
from routers.settings import router as settings_router
from routers.trash import router as trash_router
from routers.notifications import router as notifications_router
from routers.manuals import router as manuals_router
from routers.landing_pages import router as landing_pages_router
from routers.company_settings import router as company_settings_router
from routers.terms_conditions import router as terms_conditions_router
from routers.dealer_quotations import router as dealer_quotations_router
from routers.quotations_list import router as quotations_list_router
from routers.sidebar_config import router as sidebar_config_router
from routers.buckets import router as buckets_router
from routers.partner_portal import router as partner_portal_router
from routers.partner_admin import router as partner_admin_router
from routers.care_api import router as care_api_router
from routers.care_internal import router as care_internal_router
from routers.care_admin import router as care_admin_router
from routers.api import router as api_router
from routers.api_v1 import router as api_v1_router
from services.event_bus import subscribe, EventType
from services.webhook_dispatcher import handle_event

app.include_router(api_v1_router)
app.include_router(api_router)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(fieldops_router)
app.include_router(admin_router)
app.include_router(iqc_router)
app.include_router(stock_router)
app.include_router(repair_router)
app.include_router(qc_router)
app.include_router(sales_router)
app.include_router(spare_parts_router)
app.include_router(reports_router)
app.include_router(master_router)
app.include_router(bulk_upload_router)
app.include_router(cosmetic_router)
app.include_router(workid_status_router)
app.include_router(scrap_router)
app.include_router(scrap_for_sale_router)
app.include_router(devices_router)
app.include_router(transfers_router)
app.include_router(part_requests_router)
app.include_router(dispatch_router)
app.include_router(model_requests_router)
app.include_router(model_requested_router)
app.include_router(attendance_router)
app.include_router(attendance_group_config_router)
app.include_router(telesales_dashboard_router)
app.include_router(dealers_router)
app.include_router(telecalling_router)
app.include_router(m_telecalling_router)
# WhatsApp integration removed — router registration disabled (routes unreachable)
# app.include_router(whatsapp_router)
app.include_router(grn_router)
app.include_router(entity_movement_router)
app.include_router(part_estimation_router)
app.include_router(extended_warranty_router)
app.include_router(parts_grn_router)
app.include_router(stage_control_router)
app.include_router(market_router)
app.include_router(inventory_location_router)
app.include_router(physical_audits_router)
app.include_router(block_tags_router)
app.include_router(product_master_router)
app.include_router(iqc_api_router)
app.include_router(qa_uat_router)
app.include_router(crm_dashboard_router)
app.include_router(crm_contacts_router)
app.include_router(crm_sourcing_router)
app.include_router(crm_sales_router)
app.include_router(crm_quotes_router)
app.include_router(crm_activities_router)
app.include_router(crm_price_matrix_router)
app.include_router(invoices_router)
app.include_router(gate_pass_router)
app.include_router(part_sales_router)
app.include_router(partner_payments_router)
app.include_router(crm_purchase_orders_router)
app.include_router(deals_router)
app.include_router(accounts_router)
app.include_router(crm_reports_router)
app.include_router(crm_assign_leads_router)
app.include_router(assign_dealer_leads_router)
app.include_router(tickets_router)
app.include_router(settings_router)
app.include_router(trash_router)
app.include_router(notifications_router)
app.include_router(manuals_router)
app.include_router(landing_pages_router)
app.include_router(company_settings_router)
app.include_router(procure_dashboard_router)
app.include_router(terms_conditions_router)
app.include_router(dealer_quotations_router)
app.include_router(quotations_list_router)
app.include_router(sidebar_config_router)
app.include_router(stress_api_router)
app.include_router(buckets_router)
app.include_router(partner_portal_router)
app.include_router(partner_admin_router)
app.include_router(care_api_router)
app.include_router(care_internal_router)
app.include_router(care_admin_router)


# ── Error UI: per-code message + solution, JSON for API/AJAX else HTML modal ──
_ERROR_INFO = {
    400: ("Bad Request", "The request was invalid or some fields were missing/incorrect.",
          "Go back, check the values you entered, and try again."),
    401: ("Not Signed In", "Your session expired or you are not logged in.",
          "Log in again to continue."),
    403: ("Access Denied", "You don't have permission to open this page or action.",
          "Ask an admin to enable it for your role in Master Data → Module Permissions, or go back."),
    404: ("Not Found", "The page or record you requested doesn't exist or was moved.",
          "Check the link, or go back and pick a valid item."),
    405: ("Action Not Allowed", "This action isn't allowed on this page.",
          "Go back and use the buttons/links provided on the page."),
    422: ("Invalid Input", "Some values weren't in the expected format.",
          "Correct the highlighted fields and submit again."),
    429: ("Too Many Requests", "You've sent too many requests in a short time.",
          "Wait a minute, then try again."),
    500: ("Something Went Wrong", "An unexpected error occurred on the server.",
          "Try again. If it keeps happening, note what you were doing and inform the admin."),
}


def _wants_json(request: Request) -> bool:
    """API / AJAX callers get JSON, not the HTML error modal."""
    p = request.url.path or ""
    if "/api/" in p:
        return True
    if any(p.endswith(s) for s in ("/usb-import", "/exists", "/brief")) \
       or any(s in p for s in ("/qr-poll", "/status-poll", "/sync-status",
                               "/groups/messages", "/groups/analytics", "/lookup")):
        return True
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    return request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"


def _render_error(request: Request, code: int, message: str = None, detail: str = None):
    title, default_msg, solution = _ERROR_INFO.get(
        code, ("Error", "An error occurred.", "Go back and try again."))
    msg = message or default_msg
    if _wants_json(request):
        return JSONResponse({"error": msg, "code": code, "solution": solution}, status_code=code)
    return templates.TemplateResponse("error.html", {
        "request": request, "code": code, "title": title,
        "message": msg, "solution": solution, "detail": detail,
    }, status_code=code)


# Exception handlers
# 303 See Other, NOT RedirectResponse's 307 default. 307 preserves the request
# method, so a failed CSRF check on POST /auth/logout re-sent the request as
# POST /auth/login — which fails the same check and redirects again, forever
# (ERR_TOO_MANY_REDIRECTS on the login page). 303 forces the browser to GET the
# login page, which is the only sane destination for an auth bounce anyway.
@app.exception_handler(302)
async def redirect_handler(request: Request, exc):
    resp = RedirectResponse(url=exc.headers.get("Location", "/auth/login"),
                            status_code=303)
    resp.headers["Cache-Control"] = "no-store"  # never let a browser cache an auth redirect
    return resp


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc):
    return _render_error(request, 403)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return _render_error(request, 404)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Catch-all for any HTTPException status not handled above (400/401/405/409/…)."""
    code = exc.status_code or 500
    if code in (301, 302, 307, 308):
        # 303 for the same reason as the handler above: a method-preserving
        # redirect on a POST can bounce between two routes that both reject it.
        resp = RedirectResponse(url=(exc.headers or {}).get("Location", "/auth/login"),
                                status_code=303)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    # A gateway-class failure on the login page means the backend behind us is
    # unhealthy (database unreachable, dependency down). An error card telling
    # the user to "wait a minute" on a LOGIN page is a dead end — they cannot get
    # in and there is nothing on the page to retry. Send them to the site root,
    # which re-enters the normal routing once the backend recovers.
    # Note: this only catches gateway errors OUR app raises. A 502 emitted by the
    # platform edge while the app itself is down never reaches this code.
    if code in (502, 503, 504) and request.url.path.startswith("/auth/login"):
        resp = RedirectResponse(url="/", status_code=302)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    detail_msg = exc.detail if isinstance(exc.detail, str) and exc.detail.strip() else None
    try:
        import http as _http
        generic_phrase = _http.HTTPStatus(code).phrase
    except Exception:
        generic_phrase = None
    # Show the developer-supplied detail as the message only if it's not the bare HTTP phrase
    message = detail_msg if (detail_msg and detail_msg != generic_phrase) else None
    return _render_error(request, code, message=message)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return _render_error(request, 400,
                         message="Some fields are missing or in the wrong format.",
                         detail=(str(exc.errors()) if DEBUG else None))


def _log_unhandled(request: Request, exc: Exception, status: int) -> None:
    """Print and file-log a full traceback for an unhandled error.

    The database handlers below used to return a 500 page without logging
    anything, so a DB-level failure (value too long for a column, bad enum,
    dropped connection) surfaced to the user as "500" and left no trace at all
    in the server log — nothing to diagnose from afterwards. Every path that
    ends in a 5xx logs the same way the generic handler does.
    """
    msg = (f"\n{'='*60}\n{status} {request.method} {request.url}\n"
           f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}{'='*60}")
    print(msg, flush=True)
    try:
        _err_logger.error(msg)
    except Exception:
        pass


@app.exception_handler(DBAPIError)
async def db_api_error_handler(request: Request, exc: DBAPIError):
    msg = str(exc).lower()
    if ("invalid input" in msg and "uuid" in msg) or "invalid uuid" in msg:
        return _render_error(request, 404, message="The record you requested doesn't exist.")
    _log_unhandled(request, exc, 500)
    return _render_error(request, 500, message="A database error occurred.")


@app.exception_handler(ProgrammingError)
async def db_programming_error_handler(request: Request, exc: ProgrammingError):
    msg = str(exc).lower()
    if ("invalid input" in msg and "uuid" in msg) or "invalid uuid" in msg:
        return _render_error(request, 404, message="The record you requested doesn't exist.")
    _log_unhandled(request, exc, 500)
    return _render_error(request, 500, message="A database error occurred.")


@app.exception_handler(DataError)
async def db_data_error_handler(request: Request, exc: DataError):
    msg = str(exc).lower()
    if ("invalid input" in msg and "uuid" in msg) or "invalid uuid" in msg:
        return _render_error(request, 422, message="That identifier isn't in a valid format.")
    _log_unhandled(request, exc, 500)
    return _render_error(request, 500, message="A database error occurred.")


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    tb_str = _tb.format_exc()
    msg = f"\n{'='*60}\n500 {request.method} {request.url}\n{type(exc).__name__}: {exc}\n{tb_str}{'='*60}"
    # always print to console (visible in server terminal)
    print(msg, flush=True)
    # also write to log file
    try:
        _err_logger.error(msg)
    except Exception:
        pass
    return _render_error(request, 500, message="An unexpected error occurred.",
                         detail=(f"{type(exc).__name__}: {exc}" if DEBUG else None))


@app.on_event("startup")
async def startup_event():
    print(f"\n{'='*50}")
    print(f"  {APP_NAME} starting...")
    print(f"{'='*50}")

    # ── Reliance FieldOps (standalone app at /fieldops) ───────────────────────
    # Its own database and accounts. Failures here must never stop OxyPC from
    # serving: the FieldOps routes report themselves unavailable instead.
    try:
        from fieldops_db import configured as _fo_configured, init_fieldops_db

        if _fo_configured():
            _fo = await init_fieldops_db()
            if _fo.get("created"):
                print(f"  [fieldops] accounts created: {', '.join(_fo['created'])}")
            for _note in _fo.get("notes", []):
                print(f"  [fieldops] {_note}")
            print("  [fieldops] ready at /fieldops")
        else:
            print("  [fieldops] FIELDOPS_DATABASE_URL not set — /fieldops disabled")
    except Exception as _fo_err:      # noqa: BLE001 — never block OxyPC startup
        print(f"  [fieldops] startup skipped: {_fo_err}")

    # ── Schema validation + auto-fix (MUST run before serving requests) ────────
    # Checks every ORM table/column exists in the DB, fixes what it can,
    # and raises RuntimeError (aborting startup) if anything is unfixable.
    try:
        from database import engine as _engine
        from db_validator import validate_and_fix

        _auto_fix = os.environ.get("OXYPC_AUTO_FIX", "1") == "1"
        summary = await validate_and_fix(_engine, auto_fix=_auto_fix)

        if summary["issues_fixed"]:
            print(f"  [Schema] Auto-fixed {summary['issues_fixed']} issue(s):")
            for msg in summary["fixed"]:
                print(f"    + {msg}")
        else:
            print(f"  [Schema] OK  DB schema matches ORM models - no issues")

    except RuntimeError as exc:
        # Unrecoverable schema problem - log loudly and let uvicorn abort startup
        print(str(exc))
        raise
    except Exception as exc:
        # DB not reachable yet -warn but don't abort (connection errors surface on first request)
        print(f"  [Schema] WARNING: Could not validate schema: {exc}")

    # ── Load saved application timezone ──────────────────────────────────────
    try:
        from sqlalchemy import text as _text
        from utils.timezone import set_app_timezone as _set_tz
        async with _engine.connect() as _conn:
            _row = await _conn.execute(
                _text("SELECT value FROM app_settings WHERE key='app_timezone' LIMIT 1")
            )
            _tz_row = _row.fetchone()
            if _tz_row and _tz_row[0]:
                _set_tz(_tz_row[0])
                print(f"  [Timezone] Loaded from DB: {_tz_row[0]}")
            else:
                print("  [Timezone] Using default: Asia/Kolkata (IST)")
    except Exception as _tz_exc:
        print(f"  [Timezone] Could not load from DB: {_tz_exc} — using default")

    # ── Subscribe webhook dispatcher to all event types ───────────────────────
    for _et in [
        EventType.DEVICE_REGISTERED,
        EventType.LOT_CREATED,
        EventType.QC_PASSED,
        EventType.SALE_COMPLETED,
        EventType.STAGE_MOVED,
        EventType.API_KEY_CREATED,
        EventType.API_KEY_REVOKED,
    ]:
        subscribe(_et, handle_event)
    print("  [Events] Webhook dispatcher subscribed to all event types")

    # ── Warm permission cache from DB ─────────────────────────────────────────
    try:
        from routers.master import load_all_permissions_to_cache
        from database import AsyncSessionLocal as _ASL
        async with _ASL() as _sess:
            await load_all_permissions_to_cache(_sess)
        print("  [Perms] Role permission cache loaded")
    except Exception as _pe:
        print(f"  [Perms] Could not load permission cache: {_pe}")

    # ── Ensure the 'sub_admin' custom role exists (item 7) ────────────────────
    # Seeded so it shows in role dropdowns and the "Sub Admin Role" tab has a
    # target. Idempotent: skips if already present. Starts with NO permissions
    # (admin grants them via the tab), so a fresh sub_admin sees nothing extra.
    try:
        from models.role_permissions import CustomRole as _CustomRole
        from database import AsyncSessionLocal as _ASLsa
        from sqlalchemy import select as _sel_sa
        async with _ASLsa() as _sess_sa:
            _exists = (await _sess_sa.execute(
                _sel_sa(_CustomRole).where(_CustomRole.role_name == "sub_admin")
            )).scalar_one_or_none()
            if not _exists:
                _sess_sa.add(_CustomRole(role_name="sub_admin", display_name="Sub Admin",
                                         created_by="system"))
                await _sess_sa.commit()
                print("  [Perms] Seeded 'sub_admin' custom role")
    except Exception as _sae:
        print(f"  [Perms] Could not seed sub_admin role: {_sae}")

    # ── Warm Sidebar Config label cache from DB ───────────────────────────────
    try:
        from routers.sidebar_config import load_sidebar_labels_to_cache
        from database import AsyncSessionLocal as _ASL3
        async with _ASL3() as _sess3:
            await load_sidebar_labels_to_cache(_sess3)
        print("  [SidebarConfig] Custom label cache loaded")
    except Exception as _se:
        print(f"  [SidebarConfig] Could not load label cache: {_se}")

    # ── Auto-track QA Releases for any closed-out past week ──────────────────
    # Runs on every startup (= every deploy, since this project restarts on
    # every real commit) so the QA Dashboard's release tracking never falls
    # behind reality again — see routers/qa_uat.py auto_track_releases and
    # seed_qa_backfill_jul17_sep24.py (the one-off catch-up this replaces
    # going forward). Only ever closes out a past, finished week; never
    # creates a partial release for the week still in progress.
    try:
        from routers.qa_uat import auto_track_releases
        from database import AsyncSessionLocal as _ASL7
        async with _ASL7() as _sess7:
            _new_releases = await auto_track_releases(_sess7)
        if _new_releases:
            print(f"  [QA] Auto-tracked {len(_new_releases)} release(s): {', '.join(_new_releases)}")
        else:
            print("  [QA] Release tracking up to date")
    except Exception as _qae:
        print(f"  [QA] Could not auto-track releases: {_qae}")

    # ── Warm sidebar-footer app version from the latest Deployed QARelease ────
    try:
        from models.qa_uat import QARelease, ReleaseStatus, set_cached_app_version
        from database import AsyncSessionLocal as _ASL5
        from sqlalchemy import select as _select5
        async with _ASL5() as _sess5:
            _rel_res = await _sess5.execute(
                _select5(QARelease)
                .where(QARelease.status == ReleaseStatus.deployed)
                .order_by(QARelease.release_date.desc().nulls_last(),
                          QARelease.created_at.desc())
                .limit(1)
            )
            _latest_rel = _rel_res.scalar_one_or_none()
            if _latest_rel:
                set_cached_app_version(_latest_rel.version)
        print(f"  [AppVersion] Sidebar footer version: {_latest_rel.version if _latest_rel else '(default)'}")
    except Exception as _ave:
        print(f"  [AppVersion] Could not load latest release version: {_ave}")

    # ── Warm Landing Pages custom page-title cache from DB ────────────────────
    try:
        from routers.landing_pages import load_page_titles_to_cache
        from database import AsyncSessionLocal as _ASL4
        async with _ASL4() as _sess4:
            await load_page_titles_to_cache(_sess4)
        print("  [LandingPages] Custom page-title cache loaded")
    except Exception as _lpe:
        print(f"  [LandingPages] Could not load page-title cache: {_lpe}")

    # ── Warm Landing Pages breadcrumb-toggle cache from DB ────────────────────
    try:
        from routers.landing_pages import load_breadcrumb_settings_to_cache
        from database import AsyncSessionLocal as _ASL6
        async with _ASL6() as _sess6:
            await load_breadcrumb_settings_to_cache(_sess6)
        print("  [LandingPages] Breadcrumb toggle cache loaded")
    except Exception as _bce:
        print(f"  [LandingPages] Could not load breadcrumb cache: {_bce}")

    # ── Product Master: one-time Dropdown Configuration backfill ─────────────
    try:
        from models.product_master import seed_pm_dropdowns_if_empty
        from database import AsyncSessionLocal as _ASLpm
        async with _ASLpm() as _sesspm:
            _n = await seed_pm_dropdowns_if_empty(_sesspm)
            if _n:
                print(f"  [ProductMaster] Seeded {_n} Dropdown Configuration value(s)")
    except Exception as _pme:
        print(f"  [ProductMaster] Could not seed dropdowns: {_pme}")

    # ── Warm Master Data dropdown-options cache from DB ───────────────────────
    try:
        from utils.master_data import refresh_master_cache, ensure_master_defaults
        from database import AsyncSessionLocal as _ASL2
        async with _ASL2() as _sess2:
            _seeded = await ensure_master_defaults(_sess2)
            if _seeded:
                print(f"  [MasterData] Seeded {_seeded} default value(s) into empty categories")
            await refresh_master_cache(_sess2)
        print("  [MasterData] Dropdown options cache loaded")
    except Exception as _me:
        print(f"  [MasterData] Could not load dropdown-options cache: {_me}")

    # ── Warm Master Data Global Visibility cache from DB ──────────────────────
    try:
        from routers.master import load_module_visibility_to_cache
        from database import AsyncSessionLocal as _ASL7
        async with _ASL7() as _sess7:
            await load_module_visibility_to_cache(_sess7)
        print("  [MasterData] Global Visibility cache loaded")
    except Exception as _gve:
        print(f"  [MasterData] Could not load Global Visibility cache: {_gve}")

    # ── Periodic cache refresh (enables safe multi-worker deployment) ─────────
    # Every in-memory cache above is populated once here and only invalidated
    # by an explicit save-handler call in whichever worker process handled that
    # request. With more than one uvicorn worker, every OTHER worker keeps
    # serving stale data indefinitely (permissions, stage-transition rules,
    # sidebar labels, master dropdown options, ...) since they're separate
    # processes with separate memory. Reloading all of them on a fixed
    # interval bounds that staleness to this interval regardless of which
    # worker made the change, which is what makes it safe to run more than
    # one worker (see deploy/oxypc.service and services/oxypc-inventory.service).
    async def _refresh_all_caches_periodically(interval_seconds: int = 60):
        from database import AsyncSessionLocal as _ASL_periodic
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                async with _ASL_periodic() as _sess:
                    from routers.master import load_all_permissions_to_cache as _p
                    await _p(_sess)
                from routers.sidebar_config import load_sidebar_labels_to_cache as _sc
                async with _ASL_periodic() as _sess:
                    await _sc(_sess)
                from routers.landing_pages import load_page_titles_to_cache as _pt
                async with _ASL_periodic() as _sess:
                    await _pt(_sess)
                from routers.landing_pages import load_breadcrumb_settings_to_cache as _bc
                async with _ASL_periodic() as _sess:
                    await _bc(_sess)
                from utils.master_data import refresh_master_cache as _mc
                async with _ASL_periodic() as _sess:
                    await _mc(_sess)
                from routers.master import load_module_visibility_to_cache as _gv
                async with _ASL_periodic() as _sess:
                    await _gv(_sess)
                from services.control_engine import invalidate_transitions_cache as _itc
                _itc()  # lazy-reloads on next call in this worker
            except Exception as _rce:
                print(f"  [CacheRefresh] Periodic refresh failed (will retry next interval): {_rce}")

    asyncio.create_task(_refresh_all_caches_periodically())
    print("  [CacheRefresh] Periodic cache refresh started (60s interval)")

    # ── Daily Report: Daily Stock exports generated every day at 9:00 PM ──────
    try:
        from services.daily_stock_reports import scheduler_loop as _daily_report_loop
        asyncio.create_task(_daily_report_loop())
        print("  [DailyReport] Scheduler started (generates at 9:00 PM)")
    except Exception as _dre:           # never block startup
        print(f"  [DailyReport] Scheduler not started: {_dre}")

    # ── Print startup banner ───────────────────────────────────────────────────
    print(f"\n  {APP_NAME} started successfully")
    print(f"  URL: http://localhost:{APP_PORT}")
    print(f"  LAN: http://<your-server-ip>:{APP_PORT}")
    print(f"  Default login: see config.ini or ask your administrator")
    print(f"{'='*50}\n")


def open_browser():
    time.sleep(1.5)
    webbrowser.open(f"http://localhost:{APP_PORT}")


if __name__ == "__main__":
    # Open browser only when explicitly requested (set OXYPC_OPEN_BROWSER=0 to disable)
    if os.environ.get("OXYPC_OPEN_BROWSER", "1") == "1":
        t = threading.Thread(target=open_browser, daemon=True)
        t.start()

    uvicorn.run(
        "main:app",
        host=APP_HOST,
        port=APP_PORT,
        reload=False,
        log_level="info",
        workers=1,          # single async worker — avoids stale in-memory caches (_PERM_CACHE, _transitions_cache) across forked processes
        # Behind Railway's proxy: trust X-Forwarded-Proto/-For so request.url.scheme is
        # the real client scheme. Without this the HTTPS-redirect middleware loops.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
