"""Scannable Code128 barcode label (6x3 inch, landscape) for a Block Tags
Block ID. (Was 4x2 until 2026-10-10 — the bars were too thin for scanners to
read when the label was printed landscape; everything is scaled 1.5x.)

No barcode-rendering precedent existed anywhere in this codebase before this
(grepped — only plain-text Tag Numbers everywhere, never a rendered symbol).
python-barcode draws the Code128 as a PNG (needs Pillow, already a project
dependency via other image handling); reportlab — already used elsewhere in
this app for PDFs — lays that PNG into a fixed 6in x 3in landscape page
alongside the human-readable Block ID text.
"""
import io

import barcode
from barcode.writer import ImageWriter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

PAGE_WIDTH = 6 * inch
PAGE_HEIGHT = 3 * inch
# Every position/size below was laid out for the old 4x2 label; S scales it.
S = 1.5


def render_block_id_barcode_pdf(block_id: str, quantity: int | None = None, released: int = 0,
                                box_number: str | None = None) -> bytes:
    """quantity = tags currently held under this Block ID; printed under the
    Block ID text when given. released = tags since released from the block,
    shown alongside so a reprinted label explains why the count dropped.
    box_number = optional physical box, printed on the same line:
    "Quantity: 3 - Box Number: B-12"."""
    code128 = barcode.get("code128", block_id, writer=ImageWriter())
    # Bar width is sized so the symbol (plus its quiet zones) spans the full
    # barcode area at its natural size — just scaling the old image up made
    # it taller but no wider, and the thin bars stopped scanning. A wider
    # module is what lets a scanner read it from further away / at an angle.
    barcode_w_mm = (PAGE_WIDTH - 0.6 * S * inch) / inch * 25.4
    barcode_h_mm = 0.9 * S * 25.4
    modules = len(code128.build()[0])
    module_w = min(0.6, barcode_w_mm / (modules + 20))   # +20 = 10-module quiet zone each side
    png_buf = io.BytesIO()
    code128.write(png_buf, options={
        "module_width": module_w, "module_height": barcode_h_mm,
        "quiet_zone": module_w * 10, "font_size": 0, "text_distance": 0,
        "write_text": False, "dpi": 600,
    })
    png_buf.seek(0)

    pdf_buf = io.BytesIO()
    c = canvas.Canvas(pdf_buf, pagesize=(PAGE_WIDTH, PAGE_HEIGHT))

    title_y = PAGE_HEIGHT - 0.3 * S * inch
    c.setFont("Helvetica-Bold", 10 * S)
    c.drawCentredString(PAGE_WIDTH / 2, title_y, "Inventory Lifecycle")

    barcode_w = PAGE_WIDTH - 0.6 * S * inch
    barcode_h = 0.9 * S * inch
    barcode_y = title_y - 0.15 * S * inch - barcode_h
    c.drawImage(
        _as_image_reader(png_buf),
        0.3 * S * inch, barcode_y,
        width=barcode_w, height=barcode_h, preserveAspectRatio=True, anchor="c",
    )

    # Block IDs run ~19 chars (DDMMYYYY-CONTAINER-NNNN) — auto-shrink so a
    # longer container name (e.g. "CABINET") never overflows the page width.
    max_text_width = PAGE_WIDTH - 0.3 * S * inch
    font_size = int(14 * S)
    while font_size > 6 and c.stringWidth(block_id, "Helvetica-Bold", font_size) > max_text_width:
        font_size -= 1
    c.setFont("Helvetica-Bold", font_size)
    c.drawCentredString(PAGE_WIDTH / 2, barcode_y - 0.25 * S * inch, block_id)

    parts = []
    if quantity is not None:
        parts.append(f"Quantity: {quantity}" + (f" ({released} released)" if released else ""))
    if box_number:
        parts.append(f"Box Number: {box_number}")
    if parts:
        info = " - ".join(parts)
        info_size = int(11 * S)
        while info_size > 6 and c.stringWidth(info, "Helvetica-Bold", info_size) > max_text_width:
            info_size -= 1
        c.setFont("Helvetica-Bold", info_size)
        c.drawCentredString(PAGE_WIDTH / 2, barcode_y - 0.49 * S * inch, info)

    c.showPage()
    c.save()
    pdf_buf.seek(0)
    return pdf_buf.read()


def _as_image_reader(png_buf: io.BytesIO):
    from reportlab.lib.utils import ImageReader
    return ImageReader(png_buf)
