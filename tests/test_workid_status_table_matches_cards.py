"""WorkID Status (2026-11-22): the stage cards agree with the table rows —
for a single date and for a range. Recounting the table's rows the card way
(a tag once per day, its latest WorkID; reassigned left out) gives each
card's Moved In / Started / Pass / Fail / Scrap. Pending is not from the
table: it is the tags sitting on the stage on the last date."""
import json
import pathlib
import re
import subprocess
import sys
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
GROUPS = ("l1l2", "l3l4", "stress", "final_qc")


def _run(src):
    r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout.strip()


def _cards(html):
    out = {}
    for g in GROUPS:
        part = html.split('data-group="%s"' % g, 1)[1]
        stages = re.match(r'\s*data-stages="([^"]*)"', part).group(1).split(",")
        card = part.split('data-group=', 1)[0]
        out[g] = (stages, dict((k, int(v)) for k, v in re.findall(r'data-k="(\w+)" data-server="(\d+)"', card)))
    return out


def _recount(rows, stages, group, win):
    """The table rows counted the card way (same rules as the page)."""
    best = {}
    for r in rows:
        if r["sv"] not in stages or r["st"] == "reassigned":
            continue
        k = (r["b"], r["md"])
        if k not in best or r["mt"] >= best[k]["mt"]:
            best[k] = r
    mine = list(best.values())
    res = group in ("stress", "final_qc")
    c = {"moved": len(mine)}
    # Started (2026-11-22): engineer on the WorkID, Start Date in the window,
    # finished or not — a tag once per start day, from every row
    c["started"] = len({(r["b"], r["as"][:8]) for r in rows
                        if r["sv"] in stages and r["st"] != "reassigned"
                        and not r["wt"] and r["as"] and win[0] <= r["as"][:8] <= win[1]})
    if res:
        c["pass"] = sum(1 for r in mine if r["st"] == "completed" and r["sr"] == "pass")
        c["fail"] = sum(1 for r in mine if r["st"] == "completed" and r["sr"] == "fail")
    else:
        c["completed"] = sum(1 for r in mine if r["st"] == "completed" and not r["x"])
        c["scrap"] = sum(1 for r in mine if r["x"])
    return c


def test_cards_match_table_single_date_and_range(app_client, make_user):  # noqa: F811
    sfx = uuid.uuid4().hex[:5].upper()
    eng = f"tm_{sfx.lower()}"
    _run(f"""
import asyncio, sys, uuid
from datetime import datetime
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
from models.device import Device, DeviceStage as S, StageMovement
from models.work_order import WorkOrder
async def main():
    async with AsyncSessionLocal() as db:
        lot = (await db.execute(text("select id from lots where is_trashed is not true limit 1"))).scalar()
        D = lambda d, h, m=0: datetime(2026, 8, d, h, m)
        n = [0]
        def dev(now):
            n[0] += 1
            d = Device(id=uuid.uuid4(), barcode="TM%02d{sfx}" % n[0], lot_id=lot, current_stage=now)
            db.add(d)
            return d
        def wo(d, code, st, a, c=None, prefix=""):
            n[0] += 1
            db.add(WorkOrder(work_id=(prefix or "T") + "%02d{sfx}" % n[0], device_id=d.id, barcode=d.barcode,
                             stage=code, status=st, created_at=a, assigned_at=a, completed_at=c,
                             assigned_username="{eng}", assigned_name="{eng}"))
        def mv(d, f, t, at, notes=None):
            db.add(StageMovement(device_id=d.id, from_stage=f, to_stage=t, moved_at=at, moved_by="t", notes=notes))
        # A: L1/L2 done on the 10th, failed Stress, back to L1/L2 the same day (pending) -> once that day
        a = dev(S.l1); await db.flush()
        wo(a, "l1", "completed", D(10, 9), D(10, 10)); mv(a, S.l1, S.qc_check, D(10, 10))
        wo(a, "qc", "completed", D(10, 10), D(10, 11), "STRS-"); mv(a, S.qc_check, S.l1, D(10, 11))
        wo(a, "l1", "pending", D(10, 11, 5))
        # B: L1/L2 in progress on the 10th
        b = dev(S.l1); await db.flush(); wo(b, "l1", "in_progress", D(10, 9))
        # C: L1/L2 done on the 11th -> Stress pass -> Cosmetic
        c = dev(S.cosmetic_received); await db.flush()
        wo(c, "l1", "completed", D(11, 9), D(11, 10)); mv(c, S.l1, S.qc_check, D(11, 10))
        wo(c, "qc", "completed", D(11, 10), D(11, 12), "STRS-"); mv(c, S.qc_check, S.cosmetic_received, D(11, 12))
        # E: L3/L4 scrap then L1/L2 Back to Inventory on the 11th
        e = dev(S.scrap_for_sale); await db.flush()
        wo(e, "l3", "completed", D(11, 9), D(11, 13), "L3L4-"); mv(e, S.l3, S.l1, D(11, 13), "L3/L4 Normal Scrap")
        wo(e, "l1", "completed", D(11, 9), D(11, 14)); mv(e, S.l1, S.scrap_for_sale, D(11, 14), "Back to Inventory \\u2014 Normal Scrap")
        # F: Final QC pass on the 10th, G: Final QC fail on the 11th
        f = dev(S.final_qc_pass_hold); await db.flush()
        wo(f, "fqc", "completed", D(10, 9), D(10, 12)); mv(f, S.final_qc, S.final_qc_pass_hold, D(10, 12))
        g = dev(S.final_qc_fail_hold); await db.flush()
        wo(g, "fqc", "completed", D(11, 9), D(11, 12)); mv(g, S.final_qc, S.final_qc_fail_hold, D(11, 12))
        await db.commit()
asyncio.run(main())
""")
    try:
        u, p = make_user("admin")
        _login(app_client, u, p)
        for q, multi, win in (("work_from=2026-08-10&work_to=2026-08-10", False, ("20260810", "20260810")),
                              ("work_from=2026-08-11", False, ("20260811", "20260811")),
                              ("work_from=2026-08-10&work_to=2026-08-11", True, ("20260810", "20260811"))):
            html = app_client.get(f"/workid-status?engineer={eng}&{q}").text
            rows = json.loads(re.search(r'id="workidRows">(.*?)</script>', html, re.S).group(1))
            assert rows, q
            for g, (stages, card) in _cards(html).items():
                want = _recount(rows, stages, g, win)
                for k, v in want.items():
                    if k in card:                       # range: Moved In / Started not shown
                        assert card[k] == v, (q, g, k, card, want)
                if multi:
                    assert "moved" not in card and "started" not in card and "pending" not in card, (q, g, card)
            # dark tiles agree with the cards
            dk = {lbl: int(n.replace(",", "")) for n, lbl in re.findall(
                r'oxt-count"[^>]*>([\d,]+)</div>\s*<div class="oxt-label"[^>]*>(Total [\w ]+?)<', html)}
            cards = {g: c for g, (_s, c) in _cards(html).items()}
            assert dk["Total Repaired"] == cards["final_qc"]["pass"], (q, dk)
            assert dk["Total Scrap"] <= cards["l1l2"]["scrap"] + cards["l3l4"]["scrap"], (q, dk)
        # spot values: the 10th
        h10 = _cards(app_client.get(f"/workid-status?engineer={eng}&work_from=2026-08-10&work_to=2026-08-10").text)
        assert h10["l1l2"][1]["moved"] == 2 and h10["l1l2"][1]["completed"] == 0       # A once (latest: pending), B
        assert h10["l1l2"][1]["started"] == 2      # A (started twice that day: once) and B
        assert h10["stress"][1]["fail"] == 1 and h10["final_qc"][1]["pass"] == 1
        # range 10th-11th: E scrapped on both cards, counted once in Total Scrap
        hr = app_client.get(f"/workid-status?engineer={eng}&work_from=2026-08-10&work_to=2026-08-11").text
        cr = _cards(hr)
        assert cr["l1l2"][1]["scrap"] == 1 and cr["l3l4"][1]["scrap"] == 1
        assert re.search(r'>1</div>\s*<div class="oxt-label"[^>]*>Total Scrap<', hr)
        assert cr["stress"][1] == {"pass": 1, "fail": 1} and cr["final_qc"][1] == {"pass": 1, "fail": 1}
    finally:
        _run(f"""
import asyncio, sys
sys.path.insert(0, r"{ROOT}")
from sqlalchemy import text
from database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        ids = "select id from devices where barcode like 'TM%{sfx}'"
        for t in ("work_orders", "stage_movements"):
            await db.execute(text(f"delete from {{t}} where device_id in ({{ids}})"))
        await db.execute(text("delete from devices where barcode like 'TM%{sfx}'"))
        await db.commit()
asyncio.run(main())
""")
