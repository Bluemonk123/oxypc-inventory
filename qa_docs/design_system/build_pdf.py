"""Rebuild the design system PDF from design_system.html (Playwright Chromium)."""
import pathlib
from playwright.sync_api import sync_playwright
here = pathlib.Path(__file__).resolve().parent
out = here.parent / "OxyPC_Inventory_Lifecycle_Design_System_2.0.pdf"
foot = ('<div style="font-family:Inter,Arial;font-size:7.5pt;color:#6c757d;width:100%;padding:0 16mm;display:flex">'
        '<span>OxyPC Inventory Lifecycle — Design System v1.2</span><span style="margin-left:auto">'
        '<span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    pg.goto((here / "design_system.html").as_uri(), wait_until="networkidle")
    pg.pdf(path=str(out), format="A4", print_background=True, display_header_footer=True,
           header_template="<div></div>", footer_template=foot, prefer_css_page_size=True)
    b.close()
print(out)
