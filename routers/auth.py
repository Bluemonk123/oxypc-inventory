import secrets
import time
from templates_config import templates
from datetime import datetime, timedelta
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from database import get_db
from models.user import User, LoginLog
from auth.dependencies import (verify_password_async, hash_password_async,
                               create_access_token, get_current_user, verify_csrf)
from config import ACCESS_TOKEN_EXPIRE_MINUTES, COOKIE_SECURE
from limiter import limiter, ip_key_func


# Where a non-admin user starts. My Attendance is first so staff check in
# before anything else; every page they used to land on is still one click
# away in the sidebar. /attendance needs only a valid session — no module
# permission — so no role can be stranded here.
NON_ADMIN_LANDING = "/attendance"


def _first_landing(role_value: str) -> str:
    """Return the page to send a user to immediately after login.

    Admin lands on the Admin Dashboard; every other role lands on My
    Attendance.
    """
    if role_value == "admin":
        return "/dashboard"
    return NON_ADMIN_LANDING

router = APIRouter(prefix="/auth", tags=["auth"])

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    # Pre-issue a csrf_token cookie so the login form CSRF check works before session exists.
    # Reuse the browser's existing token (2026-11-15): issuing a new one on every
    # GET meant another tab, or a background request bounced to this page after
    # the session expired, replaced the cookie under an open login form — its
    # first Sign In then failed with no message.
    csrf_tok = request.cookies.get("csrf_token") or secrets.token_hex(32)
    resp = templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "login_csrf": csrf_tok,
    })
    resp.set_cookie("csrf_token", csrf_tok, httponly=False, samesite="strict", secure=COOKIE_SECURE)
    return resp


@router.post("/login")
@limiter.limit("20/minute", key_func=ip_key_func)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    def _login_error_resp(request, error_msg):
        """Return login page with a fresh CSRF token on error (re-render for retry).
        The username stays filled in (2026-11-15)."""
        tok = secrets.token_hex(32)
        resp = templates.TemplateResponse(
            "login.html", {"request": request, "error": error_msg, "login_csrf": tok,
                           "username": username}
        )
        resp.set_cookie("csrf_token", tok, httponly=False, samesite="strict", secure=COOKIE_SECURE)
        return resp

    # CSRF double-submit check done here, not with verify_csrf (2026-11-15): that
    # dependency answers a mismatch with a bare redirect to this page, so a login
    # form left open (token replaced meanwhile) just reloaded empty with no
    # message. Same check, but the user is told and can sign in straight away.
    cookie_tok = request.cookies.get("csrf_token", "")
    if not cookie_tok or not csrf_token or not secrets.compare_digest(cookie_tok, csrf_token):
        return _login_error_resp(request, "The sign-in page had expired. Please sign in again.")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if user:
        # Lockout: count failed attempts in the last LOCKOUT_MINUTES
        cutoff = app_now() - timedelta(minutes=LOCKOUT_MINUTES)
        fail_r = await db.execute(
            select(func.count(LoginLog.id)).where(
                LoginLog.user_id == user.id,
                LoginLog.action == "login_failed",
                LoginLog.timestamp >= cutoff,
            )
        )
        if (fail_r.scalar() or 0) >= MAX_FAILED_ATTEMPTS:
            return _login_error_resp(request,
                f"Account locked — too many failed attempts. "
                f"Try again in {LOCKOUT_MINUTES} minutes.")

    if not user or not await verify_password_async(password, user.password_hash):
        # Log the failure against the real account (prevents FK violation; generic message
        # avoids user-enumeration via different error text)
        if user:
            db.add(LoginLog(user_id=user.id, action="login_failed",
                            ip_address=request.client.host))
            await db.commit()
        return _login_error_resp(request, "Invalid username or password")
    if not user.status:
        return _login_error_resp(request, "Account is disabled. Contact admin.")

    token = create_access_token({"sub": user.username, "role": user.role})

    await db.execute(
        update(User).where(User.id == user.id).values(last_login=app_now())
    )
    log = LoginLog(user_id=user.id, action="login", ip_address=request.client.host)
    db.add(log)
    await db.commit()

    csrf_tok = secrets.token_hex(32)
    # Landing page set for the role in Master Data (2026-11-15), else the old rule
    from services.landing import landing_for
    landing = await landing_for(db, getattr(user.role, "value", None) or str(user.role))
    response = RedirectResponse(url=landing, status_code=302)
    _max_age = ACCESS_TOKEN_EXPIRE_MINUTES * 60
    _expires_epoch = int(time.time()) + _max_age
    response.set_cookie("access_token", token, httponly=True, samesite="strict",
                        max_age=_max_age, secure=COOKIE_SECURE)
    response.set_cookie("csrf_token", csrf_tok, httponly=False, samesite="strict",
                        max_age=_max_age, secure=COOKIE_SECURE)
    # session_expires: non-httponly so JS can read it for the warning popup
    response.set_cookie("session_expires", str(_expires_epoch), httponly=False,
                        samesite="strict", max_age=_max_age, secure=COOKIE_SECURE)
    return response


@router.post("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    """Always log the user out.

    Deliberately NOT behind verify_csrf. When the CSRF cookie was missing or
    stale the dependency bounced the request before this body ran, so the
    session cookie was never cleared — the user landed on the login page still
    authenticated, looking logged out while they were not. A logout that can
    fail is worse than the risk it guards against, and the auth cookies are
    SameSite=strict, so a cross-site POST cannot carry them here anyway.

    303 See Other, not 302/307: the browser must switch to GET for the login
    page. A method-preserving redirect turns this into POST /auth/login, which
    rejects it and redirects again — the ERR_TOO_MANY_REDIRECTS loop.
    """
    response = RedirectResponse(url="/auth/login", status_code=303)
    # session_expires was previously left behind: a stale expiry cookie makes
    # the session watcher on the next page think the session already died.
    for name in ("access_token", "csrf_token", "session_expires"):
        response.delete_cookie(name, path="/")
    return response


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return templates.TemplateResponse("profile.html", {"request": request, "current_user": current_user})


@router.post("/profile", response_class=HTMLResponse)
async def profile_update(
    request: Request,
    full_name: str = Form(...),
    whatsapp_number: str = Form(""),
    email: str = Form(""),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        update(User).where(User.id == current_user.id).values(
            full_name=full_name.strip(),
            whatsapp_number=whatsapp_number.strip() or None,
            email=email.strip() or None,
        )
    )
    await db.commit()
    return RedirectResponse(url="/auth/profile?success=Profile+updated", status_code=302)


@router.post("/change-password", response_class=HTMLResponse)
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    _csrf=Depends(verify_csrf),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await verify_password_async(current_password, current_user.password_hash):
        return templates.TemplateResponse("profile.html", {
            "request": request, "current_user": current_user,
            "pw_error": "Current password is incorrect",
        })
    if new_password != confirm_password:
        return templates.TemplateResponse("profile.html", {
            "request": request, "current_user": current_user,
            "pw_error": "New passwords do not match",
        })
    if len(new_password) < 6:
        return templates.TemplateResponse("profile.html", {
            "request": request, "current_user": current_user,
            "pw_error": "New password must be at least 6 characters",
        })
    await db.execute(
        update(User).where(User.id == current_user.id).values(
            password_hash=await hash_password_async(new_password)
        )
    )
    await db.commit()
    return RedirectResponse(url="/auth/profile?success=Password+changed+successfully", status_code=302)


@router.get("/extend-session")
async def extend_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Re-issues JWT and session_expires cookies without a page reload.
    Called by the session warning popup's 'Stay Logged In' button via fetch().
    Returns JSON {ok: true} on success, 401 if the current token is expired/invalid.
    """
    from fastapi.responses import JSONResponse
    try:
        current_user = await get_current_user(request, db)
    except Exception:
        return JSONResponse({"ok": False, "reason": "session_expired"}, status_code=401)

    token = create_access_token({"sub": current_user.username, "role": current_user.role})
    csrf_tok = secrets.token_hex(32)
    _max_age = ACCESS_TOKEN_EXPIRE_MINUTES * 60
    _expires_epoch = int(time.time()) + _max_age

    response = JSONResponse({"ok": True})
    response.set_cookie("access_token", token, httponly=True, samesite="strict", max_age=_max_age, secure=COOKIE_SECURE)
    response.set_cookie("csrf_token", csrf_tok, httponly=False, samesite="strict", max_age=_max_age, secure=COOKIE_SECURE)
    response.set_cookie("session_expires", str(_expires_epoch), httponly=False,
                        samesite="strict", max_age=_max_age, secure=COOKIE_SECURE)
    return response
