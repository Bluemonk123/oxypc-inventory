"""Competitor website prices (2026-11-04): a Competitive Brand with a Website
gets its Product Pricing prices read from that shop for the same Make + Model +
CPU + Gen + RAM + Storage. Three local fake shops cover the three ways a price
is found: Shopify JSON, WooCommerce JSON, and plain HTML search + product page
JSON-LD. Website price wins over a typed one; a manual edit marks it manual."""
import uuid

from tests.test_customise_ram_storage import _run, ROOT
from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

SFX = uuid.uuid4().hex[:6].upper()
MODEL = f"Zbk{SFX}"           # unique model so other products never match


def test_matcher_rules():
    from services.competitor_fetch import _matches
    spec = {"make": "HP", "model": "EliteBook 840 G6", "cpu": "i5", "cpu_gen": "8th", "ram_type": "DDR4",
            "ram_size": "8 GB", "storage_type": "SSD", "storage_size": "256 GB"}
    assert _matches("Refurbished HP EliteBook 840 G6 | Intel i5-8365U | 8 GB | 256 GB SSD", spec)
    assert _matches("HP EliteBook 840 G6 i5 8GB 256GB", spec)               # gen / types not stated: ok
    for bad in ("HP EliteBook 840 G6 i5 16GB 256GB SSD", "HP EliteBook 840 G5 i5 8GB 256GB",
                "HP EliteBook 840 G6 i7 8GB 256GB", "HP EliteBook 840 G6 i5 10th Gen 8GB 256GB",
                "HP EliteBook 840 G6 i5 8GB 256GB HDD", "Dell EliteBook 840 G6 i5 8GB 256GB"):
        assert not _matches(bad, spec), bad


def test_fetch_from_three_kinds_of_shop(app_client, make_user):  # noqa: F811
    out = _run(f"""
import asyncio, json, sys, threading, uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
sys.path.insert(0, r"{ROOT}")
import httpx
import services.competitor_fetch as cf
cf.PAUSE = 0
MODEL = "{MODEL}"
T_OK = f"HP {{MODEL}} Core i5 8th Gen 8GB RAM 256GB SSD"
T_BAD = f"HP {{MODEL}} Core i5 8th Gen 16GB RAM 512GB SSD"

class Shop(BaseHTTPRequestHandler):
    kind = None
    def log_message(self, *a): pass
    def send(self, body, ctype="application/json", code=200):
        b = body.encode(); self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urlsplit(self.path); q = parse_qs(u.query)
        if u.path == "/robots.txt":
            return self.send("User-agent: *\\nDisallow: /private\\n", "text/plain")
        k = self.kind
        if k == "shopify" and u.path == "/search/suggest.json":
            return self.send(json.dumps({{"resources": {{"results": {{"products": [
                {{"title": T_BAD, "price": "30999.00", "url": "/products/bad"}},
                {{"title": T_OK, "price": "24,499.00", "url": "/products/ok"}}]}}}}}}))
        if k == "woo" and u.path == "/wp-json/wc/store/v1/products":
            return self.send(json.dumps([{{"name": T_OK, "permalink": "http://x/p/ok", "is_in_stock": True,
                "prices": {{"price": "2289900", "currency_code": "INR", "currency_minor_unit": 2}}}}]))
        if k == "html" and u.path == "/search":
            return self.send(f'<html><body><a href="/p/1">{{T_BAD}}</a><a href="/p/2" title="{{T_OK}}">x</a>'
                             '<a href="https://other.example/p/3">elsewhere</a></body></html>', "text/html")
        if k == "html" and u.path == "/p/2":
            ld = json.dumps({{"@context": "https://schema.org", "@type": "Product", "name": T_OK,
                             "offers": {{"@type": "Offer", "price": "21500", "priceCurrency": "INR"}}}})
            return self.send(f'<html><head><script type="application/ld+json">{{ld}}</script></head></html>', "text/html")
        if k == "html" and u.path == "/p/1":
            return self.send('<html><head><meta property="product:price:amount" content="99999"></head></html>', "text/html")
        return self.send("not found", "text/plain", 404)

def serve(kind):
    h = type("H" + kind, (Shop,), {{"kind": kind}})
    s = ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{{s.server_address[1]}}"

async def main():
    from sqlalchemy import select, text
    from database import AsyncSessionLocal
    from models.product_master import PMProduct, PMCompetitiveList, PMCompetitorPrice
    servers = {{k: serve(k) for k in ("shopify", "woo", "html")}}
    async with AsyncSessionLocal() as db:
        p = PMProduct(id=uuid.uuid4(), product_name=f"HP/{{MODEL}}/i5/8th", make="HP", model=MODEL, cpu="i5",
                      cpu_gen="8th", ram_type="DDR4", ram_size="8 GB", storage_type="SSD", storage_size="256 GB",
                      sku=None)
        db.add(p)
        lists = {{}}
        for i, k in enumerate(("shopify", "woo", "html")):
            cl = PMCompetitiveList(id=uuid.uuid4(), name=f"ITCF{SFX}{{k}}", website=servers[k][1], display_order=900 + i)
            db.add(cl); lists[k] = cl
        await db.flush()
        # a typed price that the website must replace
        db.add(PMCompetitorPrice(product_id=p.id, list_id=lists["shopify"].id, price=Decimal("1"), source="manual"))
        await db.commit()
        pid = p.id
    async with httpx.AsyncClient(timeout=10, trust_env=False) as c:
        res = await cf.run_fetch("itest", client=c)
    async with AsyncSessionLocal() as db:
        rows = {{r.list_id: r for r in (await db.execute(select(PMCompetitorPrice).where(PMCompetitorPrice.product_id == pid))).scalars()}}
        got = {{k: (str(rows[lists[k].id].price), rows[lists[k].id].source, bool(rows[lists[k].id].source_url)) for k in lists if lists[k].id in rows}}
        sell = (await db.execute(select(PMProduct.product_price).where(PMProduct.id == pid))).scalar()
    print(json.dumps({{"ok": res.get("ok"), "got": got, "sell": str(sell), "pid": str(pid),
                      "lists": {{k: str(v.id) for k, v in lists.items()}}}}))
asyncio.run(main())
""")
    import json
    d = json.loads(out.splitlines()[-1])
    try:
        assert d["ok"] is True
        assert d["got"]["shopify"] == ["24499.00", "website", True]      # cheapest exact match; typed ₹1 replaced
        assert d["got"]["woo"] == ["22899.00", "website", True]          # minor units -> rupees
        assert d["got"]["html"] == ["21500.00", "website", True]         # JSON-LD on the product page
        # Sell Price = formula over the three (average by default)
        assert d["sell"] not in ("None", "1.00")

        u, pw = make_user("admin")
        _login(app_client, u, pw)
        html = app_client.get("/product-master/pricing").text
        assert "Fetch website prices" in html and "bi-globe2" in html and "Website prices" in html
        st = app_client.get("/product-master/pricing/fetch-status").json()
        assert st["last"]["prices_updated"] >= 3

        # typing a different price by hand marks it manual
        csrf = app_client.cookies.get("csrf_token") or "dummy"
        form = {"csrf_token": csrf, "ram_type": "DDR4", "ram_size": "8 GB", "storage_type": "SSD",
                "storage_size": "256 GB", f"comp_{d['lists']['woo']}": "20000",
                f"comp_{d['lists']['shopify']}": "24499", f"comp_{d['lists']['html']}": "21500"}
        r = app_client.post(f"/product-master/pricing/{d['pid']}/save", data=form, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json().get("ok"), r.text
        src = _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("select l.name, p.source from pm_competitor_prices p join pm_competitive_lists l on l.id = p.list_id where p.product_id = '{d['pid']}' order by l.name"))).all()
        print(";".join(f"{{n.replace('ITCF{SFX}', '')}}={{s}}" for n, s in rows))
asyncio.run(main())
""")
        assert src == "html=website;shopify=website;woo=manual", src
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from pm_competitor_prices where product_id = '{d['pid']}'"))
        await db.execute(text("delete from pm_products where id = '{d['pid']}'"))
        await db.execute(text("delete from pm_competitive_lists where name like 'ITCF{SFX}%'"))
        await db.commit()
asyncio.run(main())
""")
