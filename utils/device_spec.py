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
        return f"{n} GB"
    return space_size_text(v)


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
    """RAM as shown in Export: Total RAM Size, else RAM GB, else the summary.
    Sizes come out spaced, "16 GB" (2026-10-28)."""
    return space_size_text((getattr(d, "total_ram_size", None) or "").strip()
                           or gb_text_spaced(getattr(d, "ram_gb", None))
                           or (getattr(d, "ram_summary", None) or "").strip() or None)


def storage_text(d):
    """Storage as shown in Export: Total Hard Drive Size, else Hard Drive GB
    (old HDD GB column as a fallback), else the summary."""
    return space_size_text((getattr(d, "total_hdd_size", None) or "").strip()
                           or gb_text_spaced(getattr(d, "storage_gb", None) or getattr(d, "hdd_capacity_gb", None))
                           or (getattr(d, "hdd_summary", None) or "").strip() or None)


def storage_type_label(t) -> str:
    """Storage Type only when it really is a type (SSD, HDD, NVMe...). Some
    uploads put a SIZE in it ("256 GB", "0") — that is not a type."""
    t = (t or "").strip()
    return "" if not t or any(ch.isdigit() for ch in t) else t


def storage_display(d) -> str:
    """Storage for a summary row (2026-10-29): the size (Total HDD Size, else
    Storage GB, else Hard Drive summary; a zero never counts), then the
    Storage Type when it is a real type. A size sitting in Storage Type is
    used only when the tag has no other size — it used to be appended as the
    type, showing "256 GB 256 GB"."""
    size = ""
    for v in ((getattr(d, "total_hdd_size", None) or "").strip(),
              gb_text_spaced(getattr(d, "storage_gb", None) or getattr(d, "hdd_capacity_gb", None)),
              (getattr(d, "hdd_summary", None) or "").strip()):
        if v and not _is_zero(v):
            size = space_size_text(v)
            break
    st = (getattr(d, "storage_type", None) or "").strip()
    if not size and st and any(ch.isdigit() for ch in st) and not _is_zero(st):
        size = space_size_text(st)
    return " ".join(x for x in (size, storage_type_label(st)) if x)


# ── RAM / Storage entered the wrong way round (2026-10-27) ──────────────────
# No laptop / desktop here has more than 128 GB of RAM, and a drive of 4–64 GB
# fits a RAM size. RAM over 128 GB together with such a drive means the two
# were typed into each other's column (seen on whole lots: EliteBook x360 1030
# G7 "RAM 256GB, Storage 16GB", Zbook "RAM 512GB, Storage 32GB"). Storage 1–3
# is left alone: some tags carry SSD GB 1 / 2 meaning 1 TB / 2 TB.
RAM_MAX_GB = 128
_SWAP_STO_MIN, _SWAP_STO_MAX = 4, 64


def looks_swapped(ram_gb, storage_gb) -> bool:
    return bool(ram_gb and storage_gb and ram_gb > RAM_MAX_GB
                and _SWAP_STO_MIN <= storage_gb <= _SWAP_STO_MAX)


_RAM_SET = ("ram_gb", "ram_summary", "total_ram_count", "total_ram_size")
_STO_SET = ("storage_gb", "hdd_summary", "total_hdd_count", "total_hdd_size")


def space_size_text(v):
    """"16GB" -> "16 GB", "256GB SSD" -> "256 GB SSD", "1tb" -> "1 TB".
    Packed multi-stick / multi-drive summaries ("_" or ",") are left as typed."""
    if not v or not isinstance(v, str) or "_" in v or "," in v:
        return v
    return re.sub(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(GB|TB)\b",
                  lambda m: f"{m.group(1)} {m.group(2).upper()}", v, flags=re.IGNORECASE)


def _is_zero(v) -> bool:
    """A stored size that means nothing: 0, "0", "0GB", "0 GB"."""
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v == 0
    s = str(v).strip()
    return bool(s) and size_to_gb(s) == 0


def spec_repair(d):
    """Changes that put a tag's RAM / Storage right, as (kinds, {field: new}).
    Empty when nothing is wrong. Rules, in order:

      zero      a "0" size (text or GB) is cleared, so it no longer hides the
                real value in another field
      swapped   RAM over 128 GB with a 4–64 GB drive: the whole RAM set and the
                whole Storage set trade places
      moved     RAM over 128 GB and no Storage at all: the RAM set is really
                the drive, so it moves to Storage
      summary   RAM Summary holds a drive size (over 128 GB, not the RAM size)
                and Storage is empty: it moves to Storage
    then any field left blank is filled from the others in its set
    (fill_defaults — same rule as the RAM / Storage fill). A tag with a second
    drive (HDD GB) is never swapped or moved — too ambiguous to guess."""
    from types import SimpleNamespace
    orig = {f: getattr(d, f, None) for f in _RAM_SET + _STO_SET + ("hdd_capacity_gb",)}
    kinds = []

    # Sizes worked out with "0" read as blank. A set that is ALL "0" is kept
    # as it is (no RAM / no drive in the unit is a real value); a "0" is only
    # cleared where it hides a real size in its own set, or where a drive
    # size is moved into a Storage set that only had "0".
    nz = {f: (None if _is_zero(v) else v) for f, v in orig.items()}
    view = SimpleNamespace(**nz)
    ram = size_to_gb(ram_text(view))
    sto = size_to_gb(storage_text(view))
    cur = dict(orig)

    def _clear_zeros(fields):
        hit = False
        for f in fields:
            if _is_zero(cur[f]):
                cur[f], hit = None, True
        if hit and "zero" not in kinds:
            kinds.append("zero")

    if ram:
        _clear_zeros(_RAM_SET)
    if sto:
        _clear_zeros(_STO_SET)

    plain = lambda t: not t or ("_" not in t and "," not in t)
    second_drive = bool(cur["hdd_capacity_gb"])

    if not second_drive and looks_swapped(ram, sto):
        for a, b in zip(_RAM_SET, _STO_SET):
            cur[a], cur[b] = cur[b], cur[a]
        kinds.append("swapped")
    elif not second_drive and ram and ram > RAM_MAX_GB and not sto:
        _clear_zeros(_STO_SET)
        for a, b in zip(_RAM_SET, _STO_SET):
            cur[a], cur[b] = None, cur[a]
        kinds.append("moved")
    elif not sto and plain(cur["ram_summary"]):
        rsum = size_to_gb(cur["ram_summary"])
        rsize = size_to_gb(cur["total_ram_size"]) or cur["ram_gb"]
        if rsum and rsum > RAM_MAX_GB and rsize and rsize != rsum and rsize <= RAM_MAX_GB:
            _clear_zeros(_STO_SET)
            cur["hdd_summary"], cur["ram_summary"] = cur["ram_summary"], None
            kinds.append("summary")

    if not kinds:
        return [], {}
    cur.update(fill_defaults(SimpleNamespace(**cur)))
    for f in ("ram_summary", "total_ram_size", "hdd_summary", "total_hdd_size"):
        if cur[f] != orig[f]:
            cur[f] = space_size_text(cur[f])      # "16GB" -> "16 GB", as the edit modal saves
    changes = {f: cur[f] for f in _RAM_SET + _STO_SET if cur[f] != orig[f]}
    return (kinds, changes) if changes else ([], {})


def upload_spec(ram_value, storage_value) -> dict:
    """Device fields from an upload's RAM / Storage cells. Only the fields
    the cell gives are returned, so a blank cell never wipes a value."""
    out = {}
    ram = normalize_size(ram_value)
    if ram:
        g = size_to_gb(ram)
        out.update(total_ram_size=ram[:50], ram_gb=g, total_ram_count=str(max(part_count(ram), 1)),
                   ram_summary=(gb_text_spaced(g) or ram)[:255])
    sto = normalize_size(storage_value)
    if sto:
        g = size_to_gb(sto)
        out.update(total_hdd_size=sto[:50], storage_gb=g, total_hdd_count=str(max(part_count(sto), 1)),
                   hdd_summary=(gb_text_spaced(g) or sto)[:255])
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
