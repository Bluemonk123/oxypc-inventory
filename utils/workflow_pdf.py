"""QA Workflow PDFs (2026-11-04): the flow diagram (vector, same layout as the
page's SVG), the step journey, and the Stage Flows list. reportlab, with the
bundled DejaVu Sans (static/fonts) so ₹ and arrows print on any server."""
from __future__ import annotations

import io
import os
import re

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "fonts")
F, FB = "DejaVuSans", "DejaVuSans-Bold"
NAVY, LIGHT, MUTED = colors.HexColor("#0e3a7a"), colors.HexColor("#d9ebf6"), colors.HexColor("#6c757d")
EDGE = colors.HexColor("#6c8ebf")
KIND = {  # fill, stroke, text
    "step": ("#eef4fb", "#0e3a7a", "#1f2d3d"), "ok": ("#e8f6ee", "#0F7A37", "#1f2d3d"),
    "warn": ("#fff6e0", "#d39e00", "#1f2d3d"), "bad": ("#fdecee", "#dc3545", "#1f2d3d"),
    "end": ("#0e3a7a", "#0e3a7a", "#ffffff"), "decision": ("#ffffff", "#6c8ebf", "#1f2d3d"),
}
TONE = {"success": ("#e8f6ee", "#0F7A37"), "warning": ("#fff6e0", "#d39e00"),
        "danger": ("#fdecee", "#dc3545"), "info": ("#eef4fb", "#6c8ebf")}


def _fonts():
    if F not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(F, os.path.join(_FONT_DIR, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont(FB, os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")))


def diagram_pdf(title: str, subtitle: str, d: dict) -> bytes:
    """One page sized to the diagram; coordinates are the SVG's (y flipped)."""
    _fonts()
    margin, head = 24, 46
    W, H = d["w"] + 2 * margin, d["h"] + 2 * margin + head
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(title)
    Y = lambda y: H - margin - head - y           # svg y -> pdf y  # noqa: E731
    X = lambda x: margin + x                      # noqa: E731
    c.setFont(FB, 15); c.setFillColor(NAVY); c.drawString(margin, H - margin - 14, title)
    c.setFont(F, 9); c.setFillColor(MUTED); c.drawString(margin, H - margin - 30, subtitle)
    for ln in d["lanes"]:
        c.setFillColor(colors.HexColor("#f6f9fd") if ln["alt"] else colors.white)
        c.rect(X(0), Y(ln["y"] + ln["h"]), d["w"], ln["h"], stroke=0, fill=1)
        c.setFillColor(LIGHT); c.rect(X(0), Y(ln["y"] + ln["h"]), d["label_w"] - 8, ln["h"], stroke=0, fill=1)
        c.setFillColor(NAVY); c.setFont(FB, 9.5); c.drawString(X(10), Y(ln["y"] + ln["h"] / 2 + 3), ln["name"])
        c.setStrokeColor(colors.HexColor("#e3e9f2")); c.setLineWidth(0.6)
        c.line(X(0), Y(ln["y"]), X(d["w"]), Y(ln["y"]))
    c.setStrokeColor(EDGE); c.setLineWidth(1.1)
    for e in d["edges"]:
        pts, cur = [], None
        for cmd, a, b in re.findall(r"([MHV])\s*(-?[\d.]+)(?:,(-?[\d.]+))?", e["path"]):
            if cmd == "M":
                cur = [float(a), float(b)]
            elif cmd == "H":
                cur = [float(a), cur[1]]
            else:
                cur = [cur[0], float(a)]
            pts.append(tuple(cur))
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            c.line(X(x1), Y(y1), X(x2), Y(y2))
        (x1, y1), (x2, y2) = pts[-2], pts[-1]       # arrowhead on the last segment
        dx, dy = (1 if x2 > x1 else -1 if x2 < x1 else 0), (1 if y2 > y1 else -1 if y2 < y1 else 0)
        tip = (X(x2 + 2 * dx), Y(y2 + 2 * dy))
        p = c.beginPath(); p.moveTo(*tip)
        if dx:
            p.lineTo(tip[0] - 6 * dx, tip[1] + 3.5); p.lineTo(tip[0] - 6 * dx, tip[1] - 3.5)
        else:
            p.lineTo(tip[0] - 3.5, tip[1] + 6 * dy); p.lineTo(tip[0] + 3.5, tip[1] + 6 * dy)
        p.close(); c.setFillColor(EDGE); c.drawPath(p, stroke=0, fill=1)
    for n in d["nodes"]:
        fill, stroke, txt = KIND.get(n["kind"], KIND["step"])
        c.setFillColor(colors.HexColor(fill)); c.setStrokeColor(colors.HexColor(stroke)); c.setLineWidth(1.2)
        if n["kind"] == "decision":
            p = c.beginPath()
            p.moveTo(X(n["cx"]), Y(n["y"] - 4)); p.lineTo(X(n["cx"] + 34), Y(n["cy"]))
            p.lineTo(X(n["cx"]), Y(n["y"] + n["h"] + 4)); p.lineTo(X(n["cx"] - 34), Y(n["cy"])); p.close()
            c.drawPath(p, stroke=1, fill=1)
        else:
            c.roundRect(X(n["x"]), Y(n["y"] + n["h"]), n["w"], n["h"], 6, stroke=1, fill=1)
        c.setFillColor(colors.HexColor(txt))
        k = len(n["lines"])
        for i, t in enumerate(n["lines"]):
            c.setFont(FB if i == 0 else F, 7.6 if n["kind"] == "decision" else 8.2)
            c.drawCentredString(X(n["cx"]), Y(n["cy"] + (i - (k - 1) / 2) * 11.5 + 3), t)
    c.setFont(F, 7.4)
    for e in d["edges"]:
        if not e["label"]:
            continue
        x, y = X(e["lx"]), Y(e["ly"])
        w = pdfmetrics.stringWidth(e["label"], F, 7.4)
        bx = x - w if e["anchor"] == "end" else (x - w / 2 if e["anchor"] == "middle" else x)
        c.setFillColor(colors.white); c.rect(bx - 1.5, y - 2, w + 3, 9, stroke=0, fill=1)
        c.setFillColor(colors.HexColor("#495057")); c.drawString(bx, y, e["label"])
    c.showPage(); c.save()
    return buf.getvalue()


def _doc(buf, title):
    return SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm,
                             topMargin=12 * mm, bottomMargin=12 * mm, title=title)


def _styles():
    _fonts()
    return {
        "h1": ParagraphStyle("h1", fontName=FB, fontSize=15, leading=19, textColor=NAVY, spaceAfter=3),
        "sub": ParagraphStyle("sub", fontName=F, fontSize=9, textColor=MUTED, spaceAfter=10),
        "t": ParagraphStyle("t", fontName=FB, fontSize=10, textColor=NAVY, leading=13),
        "w": ParagraphStyle("w", fontName=F, fontSize=8, textColor=MUTED, leading=10),
        "b": ParagraphStyle("b", fontName=F, fontSize=8.6, leading=11.5),
        "br": ParagraphStyle("br", fontName=F, fontSize=8.2, leading=10.5),
    }


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def journey_pdf(title: str, subtitle: str, steps: list) -> bytes:
    """The step journey: one block per step (number, title, where, what, branches)."""
    st = _styles()
    buf = io.BytesIO()
    story = [Paragraph(_esc(title) + " — step journey", st["h1"]), Paragraph(_esc(subtitle), st["sub"])]
    for i, (stitle, where, what, branches) in enumerate(steps, 1):
        rows = [[Paragraph(f"{i}", ParagraphStyle("n", fontName=FB, fontSize=11, textColor=colors.white, alignment=1)),
                 [Paragraph(_esc(stitle), st["t"]), Paragraph(_esc(where), st["w"]),
                  Spacer(1, 3), Paragraph(_esc(what), st["b"])]]]
        style = [("BACKGROUND", (0, 0), (0, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#dbe3ee")),
                 ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#f4f8fd")),
                 ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
        for j, (label, outcome, tone) in enumerate(branches, 1):
            fill, line = TONE.get(tone, TONE["info"])
            rows.append(["", Paragraph(f"<b>{_esc(label)}</b> — {_esc(outcome)}", st["br"])])
            style += [("BACKGROUND", (1, j), (1, j), colors.HexColor(fill)),
                      ("LINEBEFORE", (1, j), (1, j), 2.2, colors.HexColor(line))]
        t = Table(rows, colWidths=[9 * mm, None])
        t.setStyle(TableStyle(style))
        story += [KeepTogether([t]), Spacer(1, 6)]
    _doc(buf, title).build(story)
    return buf.getvalue()


def stage_flows_pdf(workflows: list) -> bytes:
    """Stage Flows: each workflow's stage chain and step detail."""
    st = _styles()
    buf = io.BytesIO()
    story = [Paragraph("Stage Flows", st["h1"]),
             Paragraph("Each part of the device pipeline as it works today.", st["sub"])]
    for wf in workflows:
        block = [Paragraph(_esc(wf["title"]), st["t"]),
                 Paragraph(" → ".join(_esc(x) for x in wf["stages"]), st["w"]), Spacer(1, 3)]
        rows = [[Paragraph(f"<b>{_esc(a)}</b>", st["br"]), Paragraph(_esc(b), st["br"])] for a, b in wf["detail"]]
        if rows:
            t = Table(rows, colWidths=[55 * mm, None])
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e3e9f2")),
                                   ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
            block.append(t)
        story += [KeepTogether(block), Spacer(1, 10)]
    _doc(buf, "Stage Flows").build(story)
    return buf.getvalue()
