"""Competitor website prices (2026-11-04, Pankaj).

A Competitive Brand with a Website gets its prices on Product Master -> Product
Pricing read from that website, for each product's Make + Model + CPU + CPU Gen
+ RAM + Storage. Runs from the "Fetch website prices" button and once a day
(DAILY_AT). The website price replaces whatever is in the cell ("website wins");
a product the website has no match for keeps its current price.

How a price is found, per website (first that works is remembered for the run):
  1. Shopify shops:      /search/suggest.json?q=...   (JSON: title, price, url)
  2. WooCommerce shops:  /wp-json/wc/store/v1/products?search=...  (JSON)
  3. Any other shop:     its HTML search page -> product links -> each product
                         page's schema.org Product data (JSON-LD) / price meta tags.
The search is "<Make> <Model>"; every result's title is then checked against
the product (see _matches). Only an exact spec match is used, never a guess.

Polite by design: robots.txt is honoured, at most one request at a time per
website with a short pause, 15 s timeouts, a plain User-Agent. Nothing about
users is sent. Stdlib HTML parsing (no extra dependency on the server).
"""
from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = "OxyPC-PriceFetch/1.0 (competitor price comparison)"
TIMEOUT = 15.0
PAUSE = 0.6                  # seconds between requests to one website
MAX_PAGES_PER_QUERY = 4      # product pages opened per search when there is no JSON API
DAILY_AT = (2, 30)           # 02:30 app time (runs between 02:30 and 04:30)
STATUS_KEY = "pm_comp_fetch_last"      # AppSetting: JSON summary of the last run
LAST_DAY_KEY = "pm_comp_fetch_day"     # AppSetting: date of the last daily run

_lock = asyncio.Lock()
PROGRESS: dict = {"running": False}    # live progress for the page (single uvicorn worker)


# ── matching ──────────────────────────────────────────────────────────────────

def _norm(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.]+", " ", str(s or "").lower())).strip()


def _size_gb(v) -> int | None:
    """'8 GB' -> 8, '1 TB' -> 1024, '512' -> 512."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(tb|gb)?", str(v or "").lower())
    if not m:
        return None
    n = float(m.group(1))
    return int(n * 1024) if m.group(2) == "tb" else int(n)


def _sizes_in(title: str) -> set[int]:
    return {int(float(n) * (1024 if u == "tb" else 1))
            for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(gb|tb)\b", title)}


def _gen_of(text: str) -> int | None:
    t = text.lower()
    m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*gen", t)
    if m:
        return int(m.group(1))
    m = re.search(r"\bi[3579][\s-]?(\d{4,5})[a-z]{0,2}\d?\b", t)   # i5-8250U -> 8, i7-1165G7 -> 11, i5-12450H -> 12
    if m:
        d = m.group(1)
        if len(d) == 5 or (d[0] == "1" and d[1] in "01234"):
            return int(d[:2])
        return int(d[0])
    return None


def _cpu_token(cpu: str) -> str | None:
    c = _norm(cpu)
    m = re.search(r"\bi\s?([3579])\b", c) or re.search(r"core\s?i([3579])", c)
    if m:
        return f"i{m.group(1)}"
    m = re.search(r"ryzen\s?([3579])", c)
    if m:
        return f"ryzen {m.group(1)}"
    return c or None


_TYPES = {"ssd": "ssd", "nvme": "ssd", "hdd": "hdd", "emmc": "emmc",
          "ddr3": "ddr3", "ddr4": "ddr4", "ddr5": "ddr5", "lpddr4": "ddr4", "lpddr5": "ddr5"}


def _matches(title: str, spec: dict) -> bool:
    """True only when the listing is this exact product.
    Must appear in the title: Make, every word of Model, the CPU (i5 / Ryzen 5),
    the RAM size and the Storage size. CPU Gen, RAM Type and Storage Type are
    checked when the title states them (a different value = no match); titles
    often leave them out, so a missing one is not a mismatch."""
    t = " " + _norm(title) + " "
    if not spec.get("make") or f" {_norm(spec['make'])} " not in t:
        return False
    for w in _norm(spec.get("model")).split():
        if w != _norm(spec["make"]) and f" {w} " not in t:
            return False
    cpu = _cpu_token(spec.get("cpu"))
    if cpu and cpu.replace(" ", "") not in t.replace(" ", ""):
        return False
    sizes = _sizes_in(t)
    ram, sto = _size_gb(spec.get("ram_size")), _size_gb(spec.get("storage_size"))
    if ram is None or sto is None or ram not in sizes or sto not in sizes:
        return False
    if ram == sto:
        return False                       # can't tell which is which
    want_gen = _gen_of(str(spec.get("cpu_gen") or "") + " gen")
    have_gen = _gen_of(t)
    if want_gen and have_gen and want_gen != have_gen:
        return False
    for key in ("ram_type", "storage_type"):
        want = _TYPES.get(_norm(spec.get(key)).replace(" ", ""))
        if not want:
            continue
        group = {"ram_type": ("ddr3", "ddr4", "ddr5"), "storage_type": ("ssd", "hdd", "emmc")}[key]
        said = {_TYPES[w] for w in t.split() if w in _TYPES and _TYPES[w] in group}
        if said and want not in said:
            return False
    return True


def _money(v) -> Decimal | None:
    if v is None:
        return None
    s = re.sub(r"[^\d.]", "", str(v).replace(",", ""))
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d.quantize(Decimal("0.01")) if Decimal("1") <= d <= Decimal("10000000") else None


# ── page reading (stdlib) ─────────────────────────────────────────────────────

class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ld, self.meta, self.links = [], {}, []
        self._in_ld, self._buf, self._a = False, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_ld, self._buf = True, []
        elif tag == "meta":
            k = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if k and a.get("content") is not None:
                self.meta.setdefault(k, a["content"])
        elif tag == "a" and a.get("href"):
            self._a = [a["href"], a.get("title") or "", []]
        elif tag in ("span", "div", "data") and (a.get("itemprop") or "").lower() == "price" and a.get("content"):
            self.meta.setdefault("price", a["content"])

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.ld.append("".join(self._buf))
        elif tag == "a" and self._a:
            href, title, txt = self._a
            self.links.append((href, (title or " ".join(txt)).strip()))
            self._a = None

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)
        elif self._a is not None:
            self._a[2].append(data.strip())


def _ld_products(blobs):
    out = []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            t = o.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types:
                out.append(o)
            for k in ("@graph", "itemListElement", "item", "mainEntity"):
                if k in o:
                    walk(o[k])
    for b in blobs:
        try:
            walk(json.loads(b))
        except ValueError:
            continue
    return out


def _offer_price(product: dict):
    offers = product.get("offers")
    for o in (offers if isinstance(offers, list) else [offers]):
        if not isinstance(o, dict):
            continue
        cur = (o.get("priceCurrency") or "INR").upper()
        if cur != "INR":
            continue
        p = _money(o.get("price") or o.get("lowPrice"))
        if p is None and isinstance(o.get("priceSpecification"), dict):
            p = _money(o["priceSpecification"].get("price"))
        if p is not None:
            return p
    return None


# ── per-website fetcher ───────────────────────────────────────────────────────

class _Site:
    def __init__(self, client, base: str):
        self.client, self.base = client, base.rstrip("/")
        self.mode = None            # 'shopify' | 'woo' | 'html' | 'none'
        self.robots = None
        self.cache: dict = {}       # query -> [(title, price, url)]

    async def _get(self, url):
        if self.robots is not None and not self.robots.can_fetch(USER_AGENT, url):
            return None
        await asyncio.sleep(PAUSE)
        try:
            r = await self.client.get(url)
        except Exception:            # noqa: BLE001 — a dead site is just "no prices"
            return None
        return r if r.status_code == 200 else None

    async def _robots(self):
        rp = RobotFileParser()
        try:
            r = await self.client.get(self.base + "/robots.txt")
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:            # noqa: BLE001
            rp.parse([])
        self.robots = rp

    async def search(self, query: str) -> list:
        if query in self.cache:
            return self.cache[query]
        if self.robots is None:
            await self._robots()
        res = []
        modes = [self.mode] if self.mode else (["html"] if getattr(self, "_no_api", False) else ["shopify", "woo", "html"])
        for mode in modes:
            res = await getattr(self, "_" + mode)(query)
            if res is not None:
                if mode != "html" or not self.mode:
                    self.mode = mode
                break
        self._no_api = True          # Shopify / Woo checked once; don't re-try them per query
        self.cache[query] = res or []
        return self.cache[query]

    async def _none(self, query):
        return []

    async def _shopify(self, query):
        r = await self._get(f"{self.base}/search/suggest.json?q={quote_plus(query)}"
                            "&resources[type]=product&resources[limit]=10")
        try:
            prods = r.json()["resources"]["results"]["products"] if r else None
        except (ValueError, KeyError, TypeError):
            return None
        if prods is None:
            return None
        out = []
        for p in prods:
            price = _money(p.get("price"))
            if price is not None and p.get("available", True) is not False:
                out.append((p.get("title", ""), price, urljoin(self.base + "/", p.get("url", ""))))
        return out

    async def _woo(self, query):
        r = await self._get(f"{self.base}/wp-json/wc/store/v1/products?search={quote_plus(query)}&per_page=20")
        try:
            prods = r.json() if r else None
        except ValueError:
            return None
        if not isinstance(prods, list):
            return None
        out = []
        for p in prods:
            pr = p.get("prices") or {}
            if (pr.get("currency_code") or "INR").upper() != "INR":
                continue
            try:
                raw = Decimal(str(pr.get("price"))) / (Decimal(10) ** int(pr.get("currency_minor_unit", 2)))
            except (InvalidOperation, ValueError, TypeError):
                continue
            price = _money(raw)
            if price is not None and p.get("is_in_stock", True) is not False:
                out.append((p.get("name", ""), price, p.get("permalink") or self.base))
        return out

    async def _html(self, query):
        q = quote_plus(query)
        for path in (f"/search?q={q}", f"/?s={q}&post_type=product", f"/catalogsearch/result/?q={q}",
                     f"/search?query={q}"):
            r = await self._get(self.base + path)
            if r is None or "html" not in r.headers.get("content-type", "html"):
                continue
            pg = _Page()
            pg.feed(r.text)
            host = urlsplit(self.base).netloc
            seen, cands = set(), []
            for href, text in pg.links:
                url = urljoin(str(r.url), href).split("#")[0]
                if urlsplit(url).netloc != host or url in seen or not text:
                    continue
                seen.add(url)
                cands.append((text, url))
            # products listed in the search page's own JSON-LD (ItemList)
            out = [(p.get("name", ""), _offer_price(p), urljoin(self.base + "/", p.get("url") or ""))
                   for p in _ld_products(pg.ld) if _offer_price(p) is not None]
            if out:
                return out
            words = set(_norm(query).split())
            cands = [c for c in cands if words <= set(_norm(c[0]).split())][:MAX_PAGES_PER_QUERY]
            for text, url in cands:
                price, name = await self._product_page(url)
                if price is not None:
                    out.append((name or text, price, url))
            if cands:
                return out
        return None

    async def _product_page(self, url):
        r = await self._get(url)
        if r is None:
            return None, None
        pg = _Page()
        pg.feed(r.text)
        for p in _ld_products(pg.ld):
            price = _offer_price(p)
            if price is not None:
                return price, p.get("name")
        cur = (pg.meta.get("product:price:currency") or pg.meta.get("og:price:currency") or "INR").upper()
        if cur == "INR":
            price = _money(pg.meta.get("product:price:amount") or pg.meta.get("og:price:amount") or pg.meta.get("price"))
            if price is not None:
                return price, pg.meta.get("og:title")
        return None, None


def _spec(p) -> dict:
    return {"make": p.make, "model": p.model, "cpu": p.cpu, "cpu_gen": p.cpu_gen,
            "ram_type": p.ram_type, "ram_size": p.ram_size,
            "storage_type": p.storage_type, "storage_size": p.storage_size}


def _product_label(p) -> str:
    return " / ".join(x for x in (p.make, p.model, p.cpu, p.cpu_gen,
                                  f"{p.ram_type or ''} {p.ram_size or ''}".strip(),
                                  f"{p.storage_type or ''} {p.storage_size or ''}".strip()) if x)


# ── the run ──────────────────────────────────────────────────────────────────

async def run_fetch(triggered_by: str = "system", client=None) -> dict:
    """Fetch every website brand's prices. One run at a time; returns the summary."""
    if _lock.locked():
        return {"ok": False, "detail": "A fetch is already running."}
    async with _lock:
        return await _run(triggered_by, client)


async def _run(triggered_by, client):
    import httpx
    from sqlalchemy import select
    from database import AsyncSessionLocal
    from models.product_master import (PMCompetitiveList, PMCompetitorPrice, PMProduct,
                                       recompute_sell_prices)
    from models.settings import AppSetting
    from services.audit_engine import audit
    from utils.timezone import app_now

    started = app_now()
    PROGRESS.clear()
    PROGRESS.update({"running": True, "started_at": started.strftime("%d-%m-%Y %H:%M"),
                     "by": triggered_by, "sites": {}})
    summary = {"started_at": PROGRESS["started_at"], "by": triggered_by, "sites": {}}
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                   headers={"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.8"})
    try:
        async with AsyncSessionLocal() as db:
            lists = (await db.execute(select(PMCompetitiveList).where(
                PMCompetitiveList.website.isnot(None), PMCompetitiveList.website != ""))).scalars().all()
            products = (await db.execute(select(PMProduct))).scalars().all()
            ready = [p for p in products if p.make and p.model and p.ram_size and p.storage_size]
            changed_products, changes = set(), []
            for cl in lists:
                site = _Site(client, cl.website)
                st = {"website": cl.website, "total": len(ready), "done": 0, "updated": 0,
                      "unchanged": 0, "not_found": 0, "not_found_list": []}
                PROGRESS["sites"][cl.name] = st
                existing = {r.product_id: r for r in (await db.execute(select(PMCompetitorPrice).where(
                    PMCompetitorPrice.list_id == cl.id))).scalars().all()}
                for p in ready:
                    spec = _spec(p)
                    hits = [h for h in await site.search(f"{p.make} {p.model}") if _matches(h[0], spec)]
                    st["done"] += 1
                    if not hits:
                        st["not_found"] += 1
                        if len(st["not_found_list"]) < 300:
                            st["not_found_list"].append(_product_label(p))
                        continue
                    title, price, url = min(hits, key=lambda h: h[1])     # cheapest exact match
                    cur = existing.get(p.id)
                    now = app_now()
                    if cur is None:
                        db.add(PMCompetitorPrice(product_id=p.id, list_id=cl.id, price=price,
                                                 source="website", source_url=url[:500], fetched_at=now,
                                                 updated_by="website-fetch"))
                    else:
                        if cur.price == price and cur.source == "website":
                            cur.fetched_at, cur.source_url = now, url[:500]
                            st["unchanged"] += 1
                            continue
                        changes.append({"product": _product_label(p), "brand": cl.name,
                                        "old": str(cur.price), "new": str(price)})
                        cur.price, cur.source, cur.source_url = price, "website", url[:500]
                        cur.fetched_at, cur.updated_by = now, "website-fetch"
                    if cur is None:
                        changes.append({"product": _product_label(p), "brand": cl.name, "old": None,
                                        "new": str(price)})
                    st["updated"] += 1
                    changed_products.add(p.id)
                summary["sites"][cl.name] = {k: v for k, v in st.items()}
            await db.flush()
            await recompute_sell_prices(db, changed_products)
            summary.update({"finished_at": app_now().strftime("%d-%m-%Y %H:%M"),
                            "products_checked": len(ready),
                            "products_skipped": len(products) - len(ready),
                            "prices_updated": sum(s["updated"] for s in summary["sites"].values())})
            await audit(db, action="PM_COMPETITOR_WEBSITE_FETCH", table_name="pm_competitor_prices",
                        record_id=f"{summary['prices_updated']} updated",
                        new_value={"by": triggered_by, "sites": {k: {x: v[x] for x in ("website", "updated",
                                   "unchanged", "not_found")} for k, v in summary["sites"].items()},
                                   "changes": changes[:300]},
                        notes="Competitor website prices fetched (website wins). Sell Prices recomputed.")
            row = (await db.execute(select(AppSetting).where(AppSetting.key == STATUS_KEY))).scalar_one_or_none()
            val = json.dumps(summary)
            if row is None:
                db.add(AppSetting(key=STATUS_KEY, value=val, description="Last competitor website price fetch",
                                  updated_by=triggered_by[:50]))
            else:
                row.value, row.updated_by = val, triggered_by[:50]
            await db.commit()
        return {"ok": True, **summary}
    finally:
        if own_client:
            await client.aclose()
        PROGRESS["running"] = False


async def last_summary(db) -> dict | None:
    from sqlalchemy import select
    from models.settings import AppSetting
    v = (await db.execute(select(AppSetting.value).where(AppSetting.key == STATUS_KEY))).scalar()
    try:
        return json.loads(v) if v else None
    except ValueError:
        return None


async def daily_loop() -> None:
    """Started from main.py. Once a day at DAILY_AT (app time), if any brand has a website."""
    from sqlalchemy import select
    from database import AsyncSessionLocal
    from models.settings import AppSetting
    from utils.timezone import app_now
    await asyncio.sleep(60)
    while True:
        try:
            now = app_now()
            # only in the early-morning window — a restart during the day (a deploy)
            # must not start a run; a missed window waits for the next day / the button
            if DAILY_AT <= (now.hour, now.minute) < (DAILY_AT[0] + 2, DAILY_AT[1]):
                today = now.date().isoformat()
                async with AsyncSessionLocal() as db:
                    row = (await db.execute(select(AppSetting).where(AppSetting.key == LAST_DAY_KEY))).scalar_one_or_none()
                    if row is None or row.value != today:
                        if row is None:
                            db.add(AppSetting(key=LAST_DAY_KEY, value=today, description="Daily competitor price fetch",
                                              updated_by="system"))
                        else:
                            row.value = today
                        await db.commit()
                        run_now = True
                    else:
                        run_now = False
                if run_now:
                    await run_fetch("daily")
        except Exception as exc:     # noqa: BLE001 — never kill the loop
            print(f"  [CompetitorFetch] daily run error: {exc}")
        await asyncio.sleep(300)
