"""Model cards (2026-11-18, Pankaj): the card listing at the bottom of Ready for
Sale - Tags, Ready for Sale - Lots and All Inventories.

One card per Make + Model: total tags, blocked tags, grade counts and its
variants (CPU + Gen · RAM · Storage — the same matching as the Model Summary,
routers/devices._ms_key2) each with their own grade counts. The page shows 6
rows of cards at a time ("Load more" asks for the next ones); filters, search
and sort run here over every model.

  GET /model-cards/data  — cards (offset / limit, sort, filters, c_grade) + grade tab counts
  GET /model-cards/barcodes — Tag Numbers under the filters (header Export)
  GET /model-cards/tags  — one variant's tags (variant click), optionally one grade

scope: "ready" (Ready for Sale tags, no sub-lot), "lots" (As-Is: Ready for Sale
tags with a sub-lot; one card per As-Is sub-lot, variants = Make + Model + spec) or "inventory" (All Inventories: the Tag table's own page
filters, passed through). Card filters (c_*) are the Model Summary card's.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user
from database import get_db
from models.device import Device, DeviceStage
from models.location import StorageLocation, ZONE_LABELS
from models.lot import Lot
from models.user import User

router = APIRouter(prefix="/model-cards", tags=["model_cards"])

GRADES = ["A", "B", "C", "D", "E", "Scrap", "NG"]
MAX_LIMIT = 300          # cards per request
TAGS_CAP = 3000          # tags per variant pop-up

# Category -> Bootstrap icon (same icons as the device type tiles).
_ICONS = (("laptop", "bi-laptop"), ("desktop", "bi-pc-display"), ("monitor", "bi-display"),
          ("tft", "bi-display"), ("tablet", "bi-tablet"), ("mini", "bi-cpu"), ("server", "bi-hdd-network"))


def _icon(category: str) -> str:
    c = (category or "").lower()
    for k, ic in _ICONS:
        if k in c:
            return ic
    return "bi-box-seam"


def _role(user) -> str:
    return getattr(user.role, "value", user.role)


def _check_scope(scope: str, user) -> None:
    from models.role_permissions import has_perm
    module = "devices" if scope == "inventory" else "sales"
    if scope not in ("ready", "lots", "inventory"):
        raise HTTPException(400, "Unknown scope")
    if not has_perm(_role(user), module, "enable"):
        raise HTTPException(403, "Not allowed")


async def _where(db, scope: str, qp) -> list:
    """Scope + card filters, as SQL conditions on Device (Lot / StorageLocation joined)."""
    from routers.devices import _ms_loc_avail_filters, _ms_own_filters, _ms_page_scope
    g = lambda k: qp.get(k, "") or ""  # noqa: E731
    w = [Device.is_trashed.isnot(True)]
    no_sub = or_(Device.sub_lot_number.is_(None), func.trim(Device.sub_lot_number) == "")
    if scope == "ready":
        w += [Device.current_stage == DeviceStage.ready_to_sale, no_sub]
    elif scope == "lots":
        w += [Device.current_stage == DeviceStage.ready_to_sale, ~no_sub]
    else:
        w += await _ms_page_scope(db, g("q"), g("stage"), g("lot"), g("grade"), g("category"), g("device_type"),
                                  g("date_from"), g("date_to"), g("employee"), g("entity"), g("exclude_sold"),
                                  g("fs"), "", g("stock_type"), g("pna"), g("zone"))
    w += _ms_own_filters(g("c_entity"), g("c_make"), g("c_device_type"), g("c_touch"), g("c_stock"), "")
    w += _ms_loc_avail_filters(g("c_loc"), g("c_avail"))
    return w


def _variant(r) -> tuple:
    """(cpu, gen, ram, storage) of a row — Model Summary matching."""
    from routers.devices import _ms_key2
    k = _ms_key2(r.model, r.brand, r.cpu, r.generation, r.ram_summary, r.ram_gb,
                 r.hdd_summary, r.storage_gb, r.storage_type)
    return k[2], k[3], k[4], k[5]


def _variant_label(v) -> str:
    cpu, gen, ram, sto = v
    gen_s = (gen[:-2] + gen[-2:].lower()) if gen and gen != "—" and gen[-2:].isalpha() else gen
    head = " ".join(x for x in (cpu if cpu != "—" else "", gen_s if gen_s != "—" else "") if x) or "—"
    return f"{head} · {ram} · {sto}"


def _grade(g) -> str:
    """Grade label: A-E, "Scrap" (DeviceGrade.scrap), "NG" (no grade)."""
    v = (getattr(g, "value", g) or "").strip()
    if not v:
        return "NG"
    if v.lower() == "scrap":
        return "Scrap"
    return v.upper() if v.upper() in GRADES else v


_COLS = (Device.brand, Device.model, Device.device_type, Device.sub_category, Device.cpu, Device.generation,
         Device.ram_summary, Device.ram_gb, Device.hdd_summary, Device.storage_gb, Device.storage_type,
         Device.grade)


def _base_select(*cols):
    return (select(*cols).select_from(Device).join(Lot, Device.lot_id == Lot.id)
            .outerjoin(StorageLocation, Device.location_id == StorageLocation.id))


def _card_key(r, lots: bool) -> tuple:
    """Card of a row: (Make, Model) — or, on Ready for Sale - Lots, the As-Is
    lot: ("", Sub-Lot Number) (2026-11-18, Pankaj)."""
    from routers.devices import MS_UNKNOWN_MAKE, MS_UNKNOWN_MODEL
    if lots:   # the As-Is lot is the sub-lot, which can span several parent lots
        return ("", (r.sub_lot_number or "").strip() or "—")
    return ((r.brand or MS_UNKNOWN_MAKE), (r.model or MS_UNKNOWN_MODEL))


def _row_variant(r, lots: bool) -> tuple:
    """Variant of a row: (cpu, gen, ram, storage); a lot card mixes models, so
    there the variant starts with Make + Model."""
    if lots:
        from routers.devices import MS_UNKNOWN_MAKE, MS_UNKNOWN_MODEL
        return ((r.brand or MS_UNKNOWN_MAKE), (r.model or MS_UNKNOWN_MODEL)) + _variant(r)
    return _variant(r)


def _row_variant_label(v: tuple) -> str:
    if len(v) == 6:
        return f"{v[0]} {v[1]} · " + _variant_label(v[2:])
    return _variant_label(v)


def _search_hit(words, text: str) -> bool:
    """Every word in the text, case- and space-insensitive."""
    t = text.lower()
    compact = "".join(t.split())
    return all(w in t or w in compact for w in words)


async def _models(db, scope: str, qp) -> list:
    """Every card for the scope and card filters, after search; grade not
    applied yet. Shared by /data and /barcodes. One card per Make + Model, or
    per As-Is sub-lot on Ready for Sale - Lots."""
    lots = scope == "lots"
    cols = _COLS + ((Lot.lot_number, Device.sub_lot_number) if lots else ())
    # Blocked = a Block Tags hold or assigned to a user (Availability rule).
    held = or_(Device.is_blocked.is_(True), Device.assigned_to_user_id.isnot(None))
    # Grouped in SQL on the raw columns, folded in Python (labels need _ms_key2).
    from sqlalchemy import case
    blocked_n = func.sum(case((held, 1), else_=0)).label("blocked")
    rows = (await db.execute(
        _base_select(*cols, func.count(Device.id).label("n"), blocked_n)
        .where(*(await _where(db, scope, qp))).group_by(*cols)
    )).all()
    models: dict = {}
    for r in rows:
        n = int(r.n or 0)
        key = _card_key(r, lots)
        m = models.get(key)
        if m is None:
            m = models[key] = {"make": key[0], "model": key[1], "kind": "lot" if lots else "model",
                               "total": 0, "blocked": 0,
                               "grades": {}, "variants": {}, "cats": {}, "blk_g": {}}
        if lots and r.lot_number:
            m.setdefault("lot_set", {})[r.lot_number] = m.get("lot_set", {}).get(r.lot_number, 0) + n
        gr = _grade(r.grade)
        m["total"] += n
        m["blocked"] += int(r.blocked or 0)
        m["blk_g"][gr] = m["blk_g"].get(gr, 0) + int(r.blocked or 0)
        m["grades"][gr] = m["grades"].get(gr, 0) + n
        cat = (r.device_type or r.sub_category or "").strip() or "Other"
        m["cats"][cat] = m["cats"].get(cat, 0) + n
        v = _row_variant(r, lots)
        vv = m["variants"].setdefault(v, {"key": list(v), "label": _row_variant_label(v), "total": 0, "grades": {},
                                          "blocked": 0, "blk_g": {}})
        vv["total"] += n
        vv["grades"][gr] = vv["grades"].get(gr, 0) + n
        vv["blocked"] += int(r.blocked or 0)
        vv["blk_g"][gr] = vv["blk_g"].get(gr, 0) + int(r.blocked or 0)
    out = []
    for m in models.values():
        cat = max(m["cats"].items(), key=lambda kv: kv[1])[0] if m["cats"] else "Other"
        m["category"], m["icon"] = cat, _icon(cat)
        m["variants"] = sorted(m["variants"].values(), key=lambda v: -v["total"])
        del m["cats"]
        if lots:   # parent lot numbers, biggest first
            m["lots"] = [k for k, _ in sorted(m.pop("lot_set", {}).items(), key=lambda kv: -kv[1])]
        out.append(m)
    # Search (2026-11-18): Make / Model / Lot / CPU / Gen / RAM / Storage —
    # every word must match, spaces ignored ("8gb" finds "8 GB"). A card keeps
    # only the variants that match, with its counts recomputed from them.
    words = [w for w in (qp.get("c_q", "") or "").lower().split() if w]
    if words:
        found = []
        for m in out:
            head = " ".join([m["make"], m["model"], m["category"]] + m.get("lots", []))
            kept = [v for v in m["variants"] if _search_hit(words, head + " " + v["label"])]
            if not kept:
                continue
            if len(kept) != len(m["variants"]):
                grades, blk_g = {}, {}
                for v in kept:
                    for g, k in v["grades"].items():
                        grades[g] = grades.get(g, 0) + k
                    for g, k in v["blk_g"].items():
                        blk_g[g] = blk_g.get(g, 0) + k
                m = dict(m, variants=kept, total=sum(v["total"] for v in kept), grades=grades,
                         blocked=sum(v["blocked"] for v in kept), blk_g=blk_g)
            found.append(m)
        out = found
    return out


@router.get("/data")
async def model_cards_data(request: Request, db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    qp = request.query_params
    scope = qp.get("scope", "")
    _check_scope(scope, current_user)
    out = await _models(db, scope, qp)
    # Grade tabs (2026-11-18): counts over every other filter; a picked grade
    # (c_grade) then narrows each card to that grade's tags.
    tabs: dict = {}
    for m in out:
        for g, n in m["grades"].items():
            tabs[g] = tabs.get(g, 0) + n
    grade_tabs = [{"grade": "", "label": "All", "n": sum(tabs.values())}] + [
        {"grade": g, "label": g, "n": tabs[g]} for g in GRADES + sorted(set(tabs) - set(GRADES)) if tabs.get(g)]
    pick = qp.get("c_grade", "") or ""
    if pick:
        narrowed = []
        for m in out:
            n = m["grades"].get(pick, 0)
            if not n:
                continue
            vs = [{"key": v["key"], "label": v["label"], "total": v["grades"][pick], "grades": {pick: v["grades"][pick]}}
                  for v in m["variants"] if v["grades"].get(pick)]
            narrowed.append(dict(m, total=n, blocked=m["blk_g"].get(pick, 0), grades={pick: n},
                                 variants=sorted(vs, key=lambda v: -v["total"])))
        out = narrowed
    for m in out:
        m.pop("blk_g", None)
        m["variants"] = [{k: v[k] for k in ("key", "label", "total", "grades")} for v in m["variants"]]
    sort = qp.get("sort", "stock")
    if sort == "a":
        out.sort(key=lambda m: (-m["grades"].get("A", 0), -m["total"]))
    elif sort == "az":
        out.sort(key=lambda m: (m["make"].lower(), m["model"].lower()))
    else:
        out.sort(key=lambda m: (-m["total"], m["make"].lower(), m["model"].lower()))
    try:
        offset = max(0, int(qp.get("offset", 0)))
        limit = max(1, min(MAX_LIMIT, int(qp.get("limit", 30))))
    except ValueError:
        offset, limit = 0, 30
    return JSONResponse({"total_models": len(out), "total_tags": sum(m["total"] for m in out),
                         "grade_tabs": grade_tabs, "grade": pick,
                         "offset": offset, "cards": out[offset:offset + limit]})


@router.get("/barcodes")
async def model_cards_barcodes(request: Request, db: AsyncSession = Depends(get_db),
                               current_user: User = Depends(get_current_user)):
    """Header Export button (2026-11-18): Tag Numbers of every card under the
    current filters, search and grade tab — posted on to /devices/export."""
    qp = request.query_params
    scope = qp.get("scope", "")
    _check_scope(scope, current_user)
    lots = scope == "lots"
    keys = {(m["make"], m["model"], tuple(v["key"])) for m in await _models(db, scope, qp) for v in m["variants"]}
    pick = qp.get("c_grade", "") or ""
    rows = (await db.execute(
        _base_select(Device.barcode, Device.brand, Device.model, Device.cpu, Device.generation, Device.ram_summary,
                     Device.ram_gb, Device.hdd_summary, Device.storage_gb, Device.storage_type, Device.grade,
                     Lot.lot_number, Device.sub_lot_number)
        .where(*(await _where(db, scope, qp))).order_by(Device.barcode)
    )).all()
    out = [r.barcode for r in rows
           if _card_key(r, lots) + (_row_variant(r, lots),) in keys and (not pick or _grade(r.grade) == pick)]
    return JSONResponse({"barcodes": out})


@router.get("/tags")
async def model_cards_tags(request: Request, db: AsyncSession = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    """Variant click: the tags of one card (Make + Model, or As-Is lot) +
    variant (and grade, if picked)."""
    qp = request.query_params
    scope = qp.get("scope", "")
    _check_scope(scope, current_user)
    from routers.devices import MS_UNKNOWN_MAKE, MS_UNKNOWN_MODEL
    lots = scope == "lots"
    make, model = qp.get("make", ""), qp.get("model", "")
    want = tuple(qp.get(k, "") for k in ("cpu", "gen", "ram", "storage"))
    grade = qp.get("grade", "")
    w = await _where(db, scope, qp)
    if lots:   # card = As-Is sub-lot (model = Sub-Lot Number); variant = Make + Model + spec
        want = (qp.get("vmake", ""), qp.get("vmodel", "")) + want
        w.append(func.trim(Device.sub_lot_number) == model)
    else:
        w.append(Device.brand.is_(None) if make == MS_UNKNOWN_MAKE else Device.brand == make)
        w.append(Device.model.is_(None) if model == MS_UNKNOWN_MODEL else Device.model == model)
    rows = (await db.execute(
        _base_select(Device.id, Device.barcode, Device.brand, Device.model, Device.cpu, Device.generation,
                     Device.ram_summary, Device.ram_gb, Device.hdd_summary, Device.storage_gb, Device.storage_type,
                     Device.grade, Device.entity, Device.is_blocked, Device.assigned_to_user_id,
                     Device.current_stage, StorageLocation.zone, StorageLocation.unit_id)
        .where(*w).order_by(Device.barcode)
    )).all()
    tags = []
    for r in rows:
        if _row_variant(r, lots) != want:
            continue
        g = _grade(r.grade)
        if grade and g != grade:
            continue
        tags.append({"id": str(r.id), "barcode": r.barcode, "grade": g, "entity": r.entity or "",
                     "model": f"{r.brand or ''} {r.model or ''}".strip(),
                     "blocked": bool(r.is_blocked or r.assigned_to_user_id),
                     # Block button (2026-11-18): same rule as Block Tags' tag search.
                     "blockable": r.current_stage == DeviceStage.ready_to_sale and not r.is_blocked,
                     "location": (ZONE_LABELS.get(r.zone, getattr(r.zone, "value", r.zone)) if r.zone else "No Location")
                                 + (f" · {r.unit_id}" if r.unit_id else "")})
        if len(tags) >= TAGS_CAP:
            break
    return JSONResponse({"tags": tags, "capped": len(tags) >= TAGS_CAP})
