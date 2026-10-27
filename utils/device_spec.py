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
# One size per part. A part is split on "," or "+": "8GB+8GB", "1TB SSD + 500GB HDD",
# and OxyQC's packed "16GB_DDR4_2300MHz_Samsung, 8GB_DDR4_2133MHz_Crucial".
# Only a number carrying GB/TB counts (so DDR4's 4 and 2300MHz are ignored);
# a part that is just a number ("16") counts as GB.
_SPLIT = re.compile(r"[,+]")
_SIZED = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|T|G)(?![A-Za-z])", re.I)


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


def _part_sizes(value):
    """GB of each part in a size text, in order. Parts with no size are skipped."""
    v = (str(value) if value is not None else "").strip()
    out = []
    for part in _SPLIT.split(v) if v else []:
        m = _SIZED.search(part)
        if m:
            n = float(m.group(1))
            out.append(n * 1024 if m.group(2).upper().startswith("T") else n)
        elif _NUM_ONLY.match(part):
            out.append(float(part))
    return out


def size_to_gb(value):
    """Whole GB from a size text: "16GB" 16, "1TB" 1024, "8GB+8GB" 16,
    "512GB SSD" 512, "16" 16, "16GB_DDR4_2300MHz_X, 8GB_DDR4_2133MHz_Y" 24.
    None if no size."""
    parts = _part_sizes(value)
    return int(round(sum(parts))) if parts else None


def part_count(value):
    """How many sized parts a text has: "8GB+8GB" 2, "16GB" 1, "" 0."""
    return len(_part_sizes(value))


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


def gb_text_spaced(n):
    """Same as gb_text but with a space: "16 GB", "256 GB", "1 TB" — the form
    the Basic Specs edit modal saves RAM / Hard Drive text in (2026-10-27)."""
    t = gb_text(n)
    if not t:
        return None
    return t[:-2] + " " + t[-2:]


_SIZE_TOKEN = re.compile(r"^\s*\d+(?:\.\d+)?\s*(?:GB|TB|G|T)\b\s*", re.IGNORECASE)


def respace_summary(summary, n):
    """A summary like "256GB SSD" rewritten for size n as "256 GB SSD": the
    leading size is replaced, any words after it (drive type) are kept."""
    size = gb_text_spaced(n)
    if not size:
        return summary
    rest = _SIZE_TOKEN.sub("", summary or "", count=1).strip() if summary and _SIZE_TOKEN.match(summary) else ""
    # Keep only a plain drive-type word ("SSD", "NVMe"); a multi-part value
    # ("8GB+8GB") becomes just the new total.
    if any(ch.isdigit() for ch in rest) or rest[:1] in "+,/&":
        rest = ""
    return f"{size} {rest}".strip()


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
        g = size_to_gb(ram)
        out.update(total_ram_size=ram[:50], ram_gb=g, total_ram_count=str(max(part_count(ram), 1)),
                   ram_summary=(gb_text(g) or ram)[:255])
    sto = normalize_size(storage_value)
    if sto:
        g = size_to_gb(sto)
        out.update(total_hdd_size=sto[:50], storage_gb=g, total_hdd_count=str(max(part_count(sto), 1)),
                   hdd_summary=(gb_text(g) or sto)[:255])
    return out


def fill_defaults(d) -> dict:
    """Fill a tag's blank RAM / Storage fields from the ones it has, within
    each set. Never overwrites a value. (2026-10-22, second version)

    RAM set: RAM GB (ram_gb), RAM (ram_summary), Total RAM Count, Total RAM Size.
    Storage set: SSD GB (storage_gb), HDD GB (hdd_capacity_gb, the second
    drive), Hard Drive (hdd_summary), Total HDD Count, Total HDD Size.
    HDD GB is added to SSD GB, never copied into it, and is never filled.
    """
    ch = {}
    blank = lambda v: not (str(v).strip() if v is not None else "")
    # Values that look entered wrong are never copied (they stay for a person
    # to fix — see /admin/system/data-fixes): RAM over 128GB, storage under
    # 32GB (some tags carry SSD GB 1 / 2 meaning 1TB / 2TB). Text with no real
    # size ("0", "N/A") is treated as blank.
    ok_ram = lambda g: g if g and g <= 128 else None
    ok_sto = lambda g: g if g and g >= 32 else None
    sized = lambda t, ok: t if ok(size_to_gb(t)) else ""

    # ── RAM ──
    rs, rsum = sized((d.total_ram_size or "").strip(), ok_ram), sized((d.ram_summary or "").strip(), ok_ram)
    if d.ram_gb and not ok_ram(d.ram_gb):
        rs = rsum = ""                    # RAM GB itself looks wrong: fill nothing from this set
    ram_gb = ok_ram(d.ram_gb) or size_to_gb(rs) or size_to_gb(rsum)
    if not d.ram_gb and ram_gb:
        ch["ram_gb"] = ram_gb
    if blank(d.total_ram_size):
        v = rsum or gb_text(ram_gb)
        if v:
            ch["total_ram_size"] = v[:50]
    if blank(d.ram_summary):
        v = gb_text(ram_gb) or rs
        if v:
            ch["ram_summary"] = v[:255]
    if blank(d.total_ram_count) and ram_gb:
        ch["total_ram_count"] = str(max(part_count(rs), part_count(rsum), 1))

    # ── Storage ──
    # A Total HDD Size / Hard Drive text already on the tag is trusted first;
    # SSD GB + HDD GB is used only when there is no such text (some tags carry
    # the same drive in both GB fields, e.g. 256 + 256 with "256 GB" as total).
    hs, hsum = sized((d.total_hdd_size or "").strip(), ok_sto), sized((d.hdd_summary or "").strip(), ok_sto)
    if any(g and not ok_sto(g) for g in (d.storage_gb, d.hdd_capacity_gb)):
        return ch                         # a GB value looks wrong: fill nothing from this set
    drives = [g for g in (d.storage_gb, d.hdd_capacity_gb) if g]
    text_total = size_to_gb(hs) or size_to_gb(hsum)
    total = text_total or (sum(drives) if drives else None)
    if not drives and total:
        ch["storage_gb"] = total          # one figure, no second drive known
    if blank(d.total_hdd_size):
        v = hsum or gb_text(total)
        if v:
            ch["total_hdd_size"] = v[:50]
    if blank(d.hdd_summary):
        v = gb_text(total) or hs
        if v:
            ch["hdd_summary"] = v[:255]
    if blank(d.total_hdd_count) and total:
        n = max(part_count(hs), part_count(hsum))
        if not text_total or (len(drives) == 2 and sum(drives) == text_total):
            n = max(n, len(drives))
        ch["total_hdd_count"] = str(max(n, 1))
    return ch
