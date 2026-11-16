"""Quotation line items — one shape for CRM quotes and Telecaller quotations
(2026-11-16, Pankaj).

Fields per line: Device Category, Make, Model Name, CPU, CPU Gen, RAM, Storage,
Grade, Quantity, Unit Price, Total Price. Dropdown lists come from Master
Data. The same fields are what a line is matched on against Ready-for-Sale
tags (the Tags column on both quotation lists), and what a lot's tags are
grouped by when a partner booking / won bid fills a quotation.
"""
import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from sqlalchemy import select

SPEC_KEYS = ("device_category", "make", "model_name", "cpu", "cpu_gen", "ram", "storage", "grade")
GRADES = ["A", "B", "C", "D", "Mix", "As-Is", "Scrap"]


def options() -> dict:
    """Dropdown lists for the Create Quotation modal."""
    from utils.master_data import master_options
    from models.master import MASTER_SEED

    def mo(cat):   # Master Data list; the seed list when the category is still empty
        return master_options(cat) or list(MASTER_SEED.get(cat, []))
    cats = list(dict.fromkeys((mo("device_type") or []) + (mo("sub_category") or [])))
    return {"device_category": cats, "make": mo("brand") or [], "cpu": mo("processor_series") or [],
            "cpu_gen": mo("generation") or [], "ram": mo("ram_size") or [], "storage": mo("storage_size") or [],
            "grade": GRADES}


def money(v) -> Decimal:
    try:
        return Decimal(str(v if v not in (None, "") else 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def spec_note(line: dict) -> str:
    """'i5 · 8th Gen · 8 GB RAM · 256 GB' — the description printed under a line."""
    bits = [line.get("cpu"), line.get("cpu_gen"),
            (line.get("ram") + " RAM") if line.get("ram") else None, line.get("storage")]
    return " · ".join(b for b in bits if b)


def title_of(line: dict) -> str:
    """Model name, or the category + make when no model was given."""
    return (line.get("model_name") or " ".join(x for x in (line.get("make"), line.get("device_category")) if x)
            or "Item").strip()[:100]


def read_lines(form) -> list[dict]:
    """Line items from a submitted form (repeating fields, with or without [])."""
    def g(name):
        return form.getlist(name + "[]") or form.getlist(name)
    cols = {k: g(k) for k in SPEC_KEYS + ("quantity", "unit_price", "total_price")}
    n = max((len(v) for v in cols.values()), default=0)
    out = []
    for i in range(n):
        row = {k: ((cols[k][i] if i < len(cols[k]) else "") or "").strip() for k in cols}
        if not any(row[k] for k in SPEC_KEYS):
            continue
        try:
            qty = max(1, int(float(row["quantity"] or 1)))
        except ValueError:
            qty = 1
        unit, total = row["unit_price"], row["total_price"]
        # Unit Price and Total Price fill each other: whichever was typed wins,
        # the server recomputes the other (never trusts both from the client).
        if unit not in ("", None):
            u = money(unit)
            t = money(u * qty)
        else:
            t = money(total)
            u = money(t / qty) if qty else Decimal("0.00")
        row.update(quantity=qty, unit_price=u, total_price=t)
        for k in SPEC_KEYS:
            row[k] = row[k] or None
        out.append(row)
    return out


# ── Matching lines to Ready-for-Sale tags ───────────────────────────────────
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _num(s):
    m = re.search(r"(\d+(?:\.\d+)?)", str(s or ""))
    return float(m.group(1)) if m else None


def _gb(s):
    """'256 GB' -> 256, '1 TB' -> 1024, 512 -> 512."""
    if s in (None, ""):
        return None
    n = _num(s)
    if n is None:
        return None
    return n * 1024 if "tb" in str(s).lower() else n


def device_config(d) -> dict:
    """A device's specs in quotation-line terms."""
    st = d.storage_gb or d.hdd_capacity_gb
    return {
        "device_category": (d.device_type or d.sub_category or "").strip().title() or None,
        "make": (d.brand or "").strip() or None,
        "model_name": (d.model or "").strip() or None,
        "cpu": (d.cpu or "").strip() or None,
        "cpu_gen": (d.generation or "").strip() or None,
        "ram": f"{d.ram_gb} GB" if d.ram_gb else None,
        "storage": (f"{int(st // 1024)} TB" if st and st >= 1024 and st % 1024 == 0 else (f"{st} GB" if st else None)),
        "grade": (getattr(d.grade, "value", d.grade) or None),
    }


def line_matches(line: dict, cfg: dict) -> bool:
    """A device matches a line when every spec the line names agrees."""
    for k in ("device_category", "make", "model_name"):
        if line.get(k) and _norm(line[k]) not in _norm(cfg.get(k)) and _norm(cfg.get(k)) not in _norm(line[k]):
            return False
        if line.get(k) and not cfg.get(k):
            return False
    if line.get("cpu"):
        a, b = _norm(line["cpu"]), _norm(cfg.get("cpu"))
        if not b or (b not in a and a not in b):
            return False
    if line.get("cpu_gen"):
        if _num(line["cpu_gen"]) != _num(cfg.get("cpu_gen")):
            return False
    if line.get("ram") and _gb(line["ram"]) != _gb(cfg.get("ram")):
        return False
    if line.get("storage") and _gb(line["storage"]) != _gb(cfg.get("storage")):
        return False
    if line.get("grade") and line["grade"] not in ("Mix",):
        g = _norm(line["grade"]).replace("grade", "")[:1]
        if g and _norm(cfg.get("grade")).replace("grade", "")[:1] != g:
            return False
    return True


async def ready_devices(db):
    from models.device import Device, DeviceStage
    return (await db.execute(select(Device).where(
        Device.current_stage == DeviceStage.ready_to_sale, Device.is_active == True,  # noqa: E712
        Device.is_trashed.isnot(True)))).scalars().all()


def tags_for(lines: list[dict], devices, limit_per_line=True) -> list[str]:
    """Barcodes of Ready-for-Sale tags matching the lines' configurations; a
    line takes at most its quantity, and a tag is used once."""
    cfgs = [(d.barcode, device_config(d)) for d in devices]
    used, out = set(), []
    for ln in lines:
        if not any(ln.get(k) for k in SPEC_KEYS):
            continue
        want = int(ln.get("quantity") or 0) if limit_per_line else 10 ** 9
        got = 0
        for bc, cfg in cfgs:
            if bc in used or not line_matches(ln, cfg):
                continue
            used.add(bc); out.append(bc); got += 1
            if want and got >= want:
                break
    return out


def item_dict(it) -> dict:
    """A stored line (CRM or Telecaller) in line terms."""
    return {"device_category": getattr(it, "device_category", None), "make": getattr(it, "make", None),
            "model_name": getattr(it, "model_name", None) or getattr(it, "device_type", None),
            "cpu": getattr(it, "cpu", None), "cpu_gen": getattr(it, "cpu_gen", None),
            "ram": getattr(it, "ram", None), "storage": getattr(it, "storage", None),
            "grade": getattr(it, "grade", None), "quantity": it.quantity}


# ── A lot's tags as quotation lines ─────────────────────────────────────────
async def lot_lines(db, lot, qty=None, total=None, unit=None) -> list[dict]:
    """One line per configuration of the lot's live tags (biggest group first).
    qty caps the units (a booking of part of a lot takes the biggest groups
    first). Price: `unit` per unit, or `total` spread equally per unit with the
    last line absorbing rounding so the lines add up exactly."""
    from models.device import Device
    devs = (await db.execute(select(Device).where(
        Device.lot_id == lot.id, Device.is_active == True, Device.is_trashed.isnot(True))))  # noqa: E712
    groups = {}
    for d in devs.scalars().all():
        cfg = device_config(d)
        key = tuple(cfg.get(k) or "" for k in SPEC_KEYS)
        g = groups.setdefault(key, dict(cfg, quantity=0, tags=[]))
        g["quantity"] += 1
        g["tags"].append(d.barcode)
    lines = sorted(groups.values(), key=lambda g: -g["quantity"])
    if qty is not None:
        left, capped = int(qty), []
        for g in lines:
            if left <= 0:
                break
            take = min(left, g["quantity"])
            capped.append(dict(g, quantity=take, tags=g["tags"][:take]))
            left -= take
        lines = capped
    units = sum(g["quantity"] for g in lines)
    if not lines:
        return []
    if total is not None and units:
        u = money(Decimal(str(total)) / units)
        acc = Decimal("0")
        for i, g in enumerate(lines):
            if i < len(lines) - 1:
                g["unit_price"], g["total_price"] = u, money(u * g["quantity"])
                acc += g["total_price"]
            else:
                g["total_price"] = money(Decimal(str(total)) - acc)
                g["unit_price"] = money(g["total_price"] / g["quantity"])
    else:
        u = money(unit or 0)
        for g in lines:
            g["unit_price"], g["total_price"] = u, money(u * g["quantity"])
    return lines
