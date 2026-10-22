"""RAM / Storage — one set of rules for every screen (2026-10-22).

A tag keeps eight spec fields:
    Total RAM Summary  (ram_summary)        Total Hard Drive Summary (hdd_summary)
    Total RAM Count    (total_ram_count)    Total Hard Drive Count   (total_hdd_count)
    Total RAM Size     (total_ram_size)     Total Hard Drive Size    (total_hdd_size)
    RAM                (ram_gb, in GB)      Hard Drive / Storage     (storage_gb, in GB)

Upload and Export use just two values, RAM and Storage. A bare number gets
"GB" added ("16" -> "16GB"); a value already carrying GB/TB is kept as typed
("8GB+8GB", "1TB SSD"). The number in GB is worked out from it for ram_gb /
storage_gb (TB x 1024, parts like "8GB+8GB" added up).
"""
import re

_NUM_ONLY = re.compile(r"^\s*\d+(?:\.\d+)?\s*$")
_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|T|G)?", re.I)


def normalize_size(value):
    """"16" -> "16GB"; "8GB+8GB" / "1TB SSD" kept as typed; blank -> None."""
    v = (str(value) if value is not None else "").strip()
    if not v:
        return None
    if _NUM_ONLY.match(v):
        n = v.strip()
        n = n[:-2] if n.endswith(".0") else n
        return f"{n}GB"
    return v


def size_to_gb(value):
    """Whole GB from a size text: "16GB" 16, "1TB" 1024, "8GB+8GB" 16,
    "512GB SSD" 512, "16" 16. None if no number."""
    v = (str(value) if value is not None else "").strip()
    if not v:
        return None
    total, found = 0.0, False
    for num, unit in _PART.findall(v):
        found = True
        n = float(num)
        total += n * 1024 if unit and unit.upper().startswith("T") else n
    return int(round(total)) if found else None


def gb_text(n):
    """GB number as shown: whole terabytes in TB ("1TB" for 1024 or 1000,
    "2TB" for 2048), everything else in GB ("512GB")."""
    if not n:
        return None
    n = int(n)
    for base in (1024, 1000):
        if n >= base and n % base == 0:
            return f"{n // base}TB"
    return f"{n}GB"


_gb_text = gb_text


def ram_text(d):
    """RAM as shown in Export: Total RAM Size, else RAM GB, else the summary."""
    return ((getattr(d, "total_ram_size", None) or "").strip()
            or _gb_text(getattr(d, "ram_gb", None))
            or (getattr(d, "ram_summary", None) or "").strip() or None)


def storage_text(d):
    """Storage as shown in Export: Total Hard Drive Size, else Hard Drive GB
    (old HDD GB column as a fallback), else the summary."""
    return ((getattr(d, "total_hdd_size", None) or "").strip()
            or _gb_text(getattr(d, "storage_gb", None) or getattr(d, "hdd_capacity_gb", None))
            or (getattr(d, "hdd_summary", None) or "").strip() or None)


def upload_spec(ram_value, storage_value) -> dict:
    """Device fields from an upload's RAM / Storage cells. Only the fields
    the cell gives are returned, so a blank cell never wipes a value."""
    out = {}
    ram = normalize_size(ram_value)
    if ram:
        out.update(total_ram_size=ram[:50], ram_gb=size_to_gb(ram), total_ram_count="1")
    sto = normalize_size(storage_value)
    if sto:
        out.update(total_hdd_size=sto[:50], storage_gb=size_to_gb(sto), total_hdd_count="1")
    return out


def fill_defaults(d) -> dict:
    """Changes that bring an existing tag in line (blank fields only):
    counts -> "1" when the tag has that spec, sizes <-> GB filled from each
    other, else from the old HDD GB column. Never overwrites a value."""
    ch = {}
    rs = (d.total_ram_size or "").strip()
    if not rs and d.ram_gb:
        ch["total_ram_size"] = gb_text(d.ram_gb)
    if not d.ram_gb and rs:
        g = size_to_gb(rs)
        if g:
            ch["ram_gb"] = g
    if not (d.total_ram_count or "").strip() and (rs or d.ram_gb or ch.get("ram_gb")):
        ch["total_ram_count"] = "1"
    hs = (d.total_hdd_size or "").strip()
    gb = d.storage_gb or d.hdd_capacity_gb
    if not d.storage_gb and d.hdd_capacity_gb:
        ch["storage_gb"] = d.hdd_capacity_gb
    if not hs and gb:
        ch["total_hdd_size"] = gb_text(gb)
    if not gb and hs:
        g = size_to_gb(hs)
        if g:
            ch["storage_gb"] = g
    if not (d.total_hdd_count or "").strip() and (hs or gb or ch.get("storage_gb")):
        ch["total_hdd_count"] = "1"
    return ch
