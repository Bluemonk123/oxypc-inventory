"""
Learning Manuals
================
GET  /manuals/              — List all learning manuals
GET  /manuals/{mid}/download — Download PDF for a manual
GET  /manuals/all/download   — Download every manual as one PDF
POST /manuals/{mid}/share    — Share manual with a user (notification)
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user, verify_csrf
from database import get_db
from models.user import User
from models.notification import Notification
from templates_config import templates

router = APIRouter(prefix="/manuals", tags=["manuals"])

_MANUALS_DIR = Path(__file__).resolve().parent.parent / "static" / "manuals"
try:
    _MANUALS_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


# ── Manual content ────────────────────────────────────────────────────────────
# One manual per sidebar page (2026-10-16), kept in utils/manual_content.py.
from utils.manual_content import MANUALS, MANUAL_SECTIONS  # noqa: E402

_MANUAL_MAP = {m["id"]: m for m in MANUALS}


def _visible_manuals(user: User) -> list:
    """Role-based Manuals (2026-10-19): only the manuals whose page (its
    'key' = Permission Matrix module key, same as the sidebar's) is enabled
    for this user's role. Same has_perm() check the sidebar uses, so a
    custom role (e.g. trc_manager) is covered too; Admin always sees all."""
    from models.role_permissions import has_perm
    role = user.role.value if hasattr(user.role, "value") else str(user.role)
    return [m for m in MANUALS if has_perm(role, m["key"], "enable")]


def _manual_hash(manual: dict | None = None) -> str:
    """Short fingerprint of a manual's content (or of all manuals). The cached
    PDF filename carries it, so editing a manual's text automatically produces
    a fresh PDF instead of serving the stale cached one forever."""
    payload = json.dumps(manual if manual is not None else MANUALS, sort_keys=True, default=str)
    return hashlib.sha1((_PDF_LAYOUT_VERSION + payload).encode("utf-8")).hexdigest()[:10]


# Bump when the PDF layout itself changes (not the text) to re-render every PDF.
_PDF_LAYOUT_VERSION = "2026-10-16a"

# Core PDF fonts are Latin-1 only; map common typographic characters to ASCII.
_PDF_CHAR_MAP = {
    "\u2014": " - ", "\u2013": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2026": "...", "\u2192": "->", "\u2190": "<-", "\u00d7": "x", "\u20b9": "Rs ", "\u2022": "*",
    "\u00a0": " ", "\u2264": "<=", "\u2265": ">=", "\u2713": "OK",
}


def _pdf_safe(text) -> str:
    text = str(text or "")
    for k, v in _PDF_CHAR_MAP.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")


# ── PDF generation ────────────────────────────────────────────────────────────

_NAVY = (14, 58, 122)      # brand navy  #0e3a7a
_BLUE_BG = (217, 235, 246)  # data-card header blue #d9ebf6


def _generate_pdf(manuals: list, title: str | None = None) -> bytes:
    try:
        from fpdf import FPDF  # noqa: F401
        return _pdf_fpdf(manuals, title)
    except Exception:
        return _pdf_text(manuals)


def _pdf_fpdf(manuals: list, title: str | None) -> bytes:
    from fpdf import FPDF

    class _PDF(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 5, f"OxyPC Inventory Lifecycle -- Confidential Training Document   |   Page {self.page_no()}",
                      align="C")

    pdf = _PDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(15, 15, 15)

    def header_band(subtitle: str):
        pdf.set_fill_color(*_NAVY)
        pdf.rect(0, 0, 210, 26, "F")
        pdf.set_y(7)
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(0, 7, "OxyPC Inventory Lifecycle -- Learning Manual", align="C")
        pdf.ln(7)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(210, 225, 245)
        pdf.cell(0, 5, _pdf_safe(subtitle), align="C")
        pdf.set_y(32)

    if title:  # cover / contents page for the all-in-one PDF
        pdf.add_page()
        header_band(title)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "Contents")
        pdf.ln(9)
        section = None
        for m in manuals:
            if m["section"] != section:
                section = m["section"]
                pdf.set_font("Helvetica", "B", 10)
                pdf.set_text_color(*_NAVY)
                pdf.cell(0, 6, _pdf_safe(section))
                pdf.ln(6)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(60, 60, 60)
            pdf.set_x(20)
            pdf.cell(0, 5, _pdf_safe(f"{m['id']}. {m['module']}"))
            pdf.ln(5)

    for m in manuals:
        pdf.add_page()
        header_band(f"{m['section']}  >  {m['module']}")

        # Module title + description
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 9, _pdf_safe(m["module"]))
        pdf.ln(9)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(110, 110, 110)
        pdf.cell(0, 5, _pdf_safe(f"Menu: {m['section']} -> {m['module']}"))
        pdf.ln(6)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(60, 60, 60)
        pdf.multi_cell(0, 5.5, _pdf_safe(m["description"]))
        pdf.ln(2)
        pdf.set_draw_color(200, 200, 200)
        pdf.line(15, pdf.get_y(), 195, pdf.get_y())
        pdf.ln(4)

        # Steps
        for idx, (step_title, items) in enumerate(m["steps"], 1):
            pdf.set_fill_color(*_BLUE_BG)
            pdf.set_text_color(*_NAVY)
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 7, _pdf_safe(f"  Step {idx}: {step_title}"), fill=True)
            pdf.ln(8)
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 9)
            for n, item in enumerate(items, 1):
                pdf.set_x(18)
                pdf.cell(7, 5.5, f"{n}.")
                pdf.multi_cell(0, 5.5, _pdf_safe(item.strip()))
            pdf.ln(2)

        # Tips
        if m.get("tips"):
            pdf.set_fill_color(255, 243, 205)
            pdf.set_text_color(100, 80, 0)
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 7, "  Tips & Rules", fill=True)
            pdf.ln(8)
            pdf.set_font("Helvetica", "", 9)
            for tip in m["tips"]:
                pdf.set_x(18)
                pdf.cell(5, 5.5, "*")
                pdf.multi_cell(0, 5.5, _pdf_safe(tip))
            pdf.ln(2)

    return bytes(pdf.output())


def _pdf_text(manuals: list) -> bytes:
    lines = []
    for m in manuals:
        lines += ["OxyPC Inventory Lifecycle -- Learning Manual", "=" * 60,
                  f"Module: {m['section']} -> {m['module']}", f"Description: {m['description']}", ""]
        for idx, (title, items) in enumerate(m["steps"], 1):
            lines += [f"Step {idx}: {title}", "-" * 40]
            lines += [f"  {n}. {item.strip()}" for n, item in enumerate(items, 1)]
            lines.append("")
        if m.get("tips"):
            lines += ["Tips & Rules", "-" * 40] + [f"  * {t}" for t in m["tips"]]
        lines += ["", ""]
    return "\n".join(lines).encode("utf-8")


def _cached_pdf(cache_name: str, build) -> bytes:
    cache_path = _MANUALS_DIR / cache_name
    if cache_path.exists():
        return cache_path.read_bytes()
    pdf_bytes = build()
    if pdf_bytes[:5] == b"%PDF-":  # never cache the plain-text fallback
        try:
            cache_path.write_bytes(pdf_bytes)
        except Exception:
            pass
    return pdf_bytes


def _file_response(pdf_bytes: bytes, slug: str) -> StreamingResponse:
    # Sniff actual bytes -- fpdf2 may be missing, in which case it's plain text
    if pdf_bytes[:5] == b"%PDF-":
        media_type, filename = "application/pdf", f"OxyPC_Manual_{slug}.pdf"
    else:
        media_type, filename = "text/plain; charset=utf-8", f"OxyPC_Manual_{slug}.txt"
    return Response(content=pdf_bytes, media_type=media_type,
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in text.lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def manuals_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Fetch all active users for the share dropdown
    res = await db.execute(
        select(User).where(User.status == True).order_by(User.full_name)
    )
    users = res.scalars().all()

    shared = request.query_params.get("shared")
    manuals = _visible_manuals(current_user)
    present = {m["section"] for m in manuals}
    return templates.TemplateResponse("manuals/index.html", {
        "request": request,
        "current_user": current_user,
        "manuals": manuals,
        "sections": [s for s in MANUAL_SECTIONS if s in present],
        "users": users,
        "shared": shared,
    })


@router.get("/all/download")
async def download_all_manuals(current_user: User = Depends(get_current_user)):
    """Every manual this user's role may see, in one PDF, with a contents
    page, grouped by sidebar section."""
    manuals = _visible_manuals(current_user)
    if not manuals:
        raise HTTPException(403, "No manuals are enabled for your role.")
    cache_key = _manual_hash() if len(manuals) == len(MANUALS) else _manual_hash(manuals)
    pdf_bytes = _cached_pdf(f"manual_all_{cache_key}.pdf",
                            lambda: _generate_pdf(manuals, "All Manuals"))
    return _file_response(pdf_bytes, "All_Modules")


@router.get("/{mid}/download")
async def download_manual(
    mid: int,
    current_user: User = Depends(get_current_user),
):
    manual = _MANUAL_MAP.get(mid)
    if not manual:
        raise HTTPException(404, "Manual not found")
    if manual not in _visible_manuals(current_user):
        raise HTTPException(403, "This manual's module is not enabled for your role.")
    pdf_bytes = _cached_pdf(f"manual_{mid}_{_manual_hash(manual)}.pdf",
                            lambda: _generate_pdf([manual]))
    return _file_response(pdf_bytes, _slug(manual["module"]))


@router.post("/{mid}/share", dependencies=[Depends(verify_csrf)])
async def share_manual(
    mid: int,
    request: Request,
    target_user_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manual = _MANUAL_MAP.get(mid)
    if not manual:
        raise HTTPException(404, "Manual not found")
    if manual not in _visible_manuals(current_user):
        raise HTTPException(403, "This manual's module is not enabled for your role.")

    # Verify target user exists
    res = await db.execute(select(User).where(User.id == target_user_id))
    target = res.scalar_one_or_none()
    if not target:
        raise HTTPException(404, "User not found")

    notif = Notification(
        user_id=target.id,
        title=f"Learning Manual Shared: {manual['module']}",
        message=(
            f"{current_user.full_name or current_user.username} shared the "
            f"'{manual['module']}' learning manual with you. "
            f"Download it from Manuals in the left navigation."
        ),
        notification_type="info",
        stage="Manuals",
    )
    db.add(notif)
    await db.commit()

    return RedirectResponse(f"/manuals/?shared={manual['module']}", status_code=303)
