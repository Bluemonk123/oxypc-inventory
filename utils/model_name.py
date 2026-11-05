"""Product Model Name consistency (2026-11-05, Pankaj).

The same model was saved in several spellings — "EliteBook x360 1030 G7",
"Elitebook X360 1030 G7", "EliteBook x360 1030G7", "ZBOOK STUDIO G7" /
"Zbook Studio G7", a leading tab … — so filters, the Sales "Model Names"
popup, pricing and reports split one model into several rows.

Two spellings are the SAME model when they match ignoring letter case,
spaces and punctuation (model_key). Each such group has one canonical
spelling (choose): capitals from the best-cased spelling (normal capitals,
not "ZBOOK STUDIO" or all lower-case; then the most-used) and spaces from
the best-separated one ("840 G6" over "840g6"; then the most-used).

  * Data Fixes -> "Product Model Name" sets every tag to its group's
    canonical spelling (one-time tidy, audited, reversible).
  * The admin can type the spelling for a model on that card; it is kept in
    Master Data (category "model_spelling", hidden from the Master Data
    page) and wins over the automatic choice, now and on later saves.
  * snap_on_flush (models/device.py listener) keeps it that way: whenever a
    tag's Model is saved from ANY page / upload / OxyQC, spaces are tidied
    and a spelling of a model that already exists is saved as that model's
    canonical spelling. A genuinely new model is kept as typed.
"""
from __future__ import annotations

import re
import threading
import time

_WORD = re.compile(r"[A-Za-z]+")


def tidy(value):
    """Spaces / tabs / newlines collapsed to one space and trimmed; None if empty."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s or None


def model_key(value) -> str:
    """Lower-case letters and digits only — "EliteBook x360 1030G7" and
    "Elitebook X360 1030 G7" give the same key."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _shouty(s: str) -> bool:
    return any(len(w) >= 4 and w.isupper() for w in _WORD.findall(s))


def _ld_breaks(s: str) -> int:
    """Separators that split letters from digits ("Optiplex 3070", "840 G6");
    a space inside a word or a number ("Think Pad", "E20 30") doesn't count."""
    return len(re.findall(r"(?<=[A-Za-z])[^A-Za-z0-9]+(?=\d)|(?<=\d)[^A-Za-z0-9]+(?=[A-Za-z])", s))


def _case_rank(s: str) -> int:
    """0 = normal capitals, 1 = SHOUTED ("ZBOOK STUDIO"), 2 = all lower-case."""
    if s.lower() == s and any(c.isalpha() for c in s):
        return 2
    return 1 if _shouty(s) else 0


def choose(counts: dict) -> str:
    """Canonical spelling from {spelling: tag count} (spellings already tidied),
    built from two picks of the same model (same letters and digits):
      * capitals from the best-cased spelling — normal capitals over SHOUTED
        over all lower-case, then the most-used;
      * spaces / dashes from the best-separated spelling — the most letter↔
        digit separations ("840 G6" over "840g6"), then the most-used.
    e.g. {"OPTIPLEX 3070": 12, "Optiplex3070": 4} -> "Optiplex 3070"."""
    by_case = min(counts, key=lambda s: (_case_rank(s), -counts[s], s))
    by_gap = min(counts, key=lambda s: (-_ld_breaks(s), -counts[s], s))
    letters = iter(c for c in by_case if c.isalnum())
    out = "".join(next(letters, c) if c.isalnum() else c for c in by_gap)
    return out


OVERRIDE_CATEGORY = "model_spelling"


def _groups(rows) -> dict:
    groups: dict = {}
    for model, n in rows:
        t = tidy(model)
        if not t:
            continue
        g = groups.setdefault(model_key(t), {})
        g[t] = g.get(t, 0) + int(n or 0)
    groups.pop("", None)
    return groups


def overrides_from(values) -> dict:
    """Typed spellings (Master Data values) -> {key: spelling}."""
    out = {}
    for v in values:
        t = tidy(v)
        if t and model_key(t):
            out[model_key(t)] = t
    return out


def canonical_map(rows, overrides=None) -> dict:
    """rows = [(model as saved, tag count)] -> {key: canonical spelling};
    a typed spelling (overrides) wins over the automatic one."""
    cmap = {k: choose(g) for k, g in _groups(rows).items()}
    cmap.update(overrides or {})
    return cmap


def changes(rows, overrides=None) -> list:
    """[(saved spelling, canonical, tag count)] for every saved spelling that
    differs from its canonical one — the Data Fix preview."""
    cmap = canonical_map(rows, overrides)
    out = []
    for model, n in rows:
        t = tidy(model)
        if not t:
            continue
        new = cmap.get(model_key(t), t)
        if new != model:
            out.append((model, new, int(n or 0)))
    return sorted(out, key=lambda x: (model_key(x[1]), -x[2]))


def fix_groups(rows, overrides=None) -> list:
    """One entry per model that has something to fix — for the card:
    {key, auto, final, typed, spellings: [(as saved, tags)], to_change}."""
    overrides = overrides or {}
    raw: dict = {}
    for model, n in rows:
        t = tidy(model)
        if t and model_key(t):
            raw.setdefault(model_key(t), []).append((model, int(n or 0)))
    groups = _groups(rows)
    out = []
    for k, saved in raw.items():
        auto = choose(groups[k])
        final = overrides.get(k, auto)
        change = sum(n for m, n in saved if m != final)
        if change:
            out.append({"key": k, "auto": auto, "final": final, "typed": k in overrides,
                        "spellings": sorted(saved, key=lambda x: -x[1]), "to_change": change})
    return sorted(out, key=lambda g: -g["to_change"])


# ── On-save guard (used by the Device listener) ──────────────────────────────
_CACHE = {"at": 0.0, "map": {}}
_LOCK = threading.Lock()
_TTL = 600            # seconds; cleared at once by the Data Fix


def clear_cache() -> None:
    with _LOCK:
        _CACHE["at"], _CACHE["map"] = 0.0, {}


def _load(connection) -> dict:
    from sqlalchemy import text
    rows = connection.execute(text(
        "SELECT model, count(*) FROM devices WHERE model IS NOT NULL GROUP BY model")).all()
    typed = connection.execute(text(
        "SELECT value FROM master_data WHERE category = :c AND is_active IS NOT FALSE"),
        {"c": OVERRIDE_CATEGORY}).scalars().all()
    return canonical_map(rows, overrides_from(typed))


def snap(connection, value):
    """The spelling to save for a typed Model value."""
    t = tidy(value)
    if not t:
        return None
    k = model_key(t)
    if not k:
        return t
    with _LOCK:
        fresh = time.monotonic() - _CACHE["at"] < _TTL
        cmap = _CACHE["map"]
    if not fresh:
        try:
            cmap = _load(connection)
        except Exception:                 # never block a save over this
            return t
        with _LOCK:
            _CACHE["at"], _CACHE["map"] = time.monotonic(), cmap
    canon = cmap.get(k)
    if canon is None:
        with _LOCK:
            _CACHE["map"][k] = t          # a new model: later spellings follow this one
        return t
    return canon


def snap_on_flush(mapper, connection, target) -> None:
    """before_insert / before_update on Device: tidy + snap the Model."""
    from sqlalchemy import inspect
    state = inspect(target)
    if state.persistent or state.deleted:
        hist = state.attrs.model.history
        if not hist.has_changes():
            return
    value = target.model
    if value is None:
        return
    new = snap(connection, value)
    if new != value:
        target.model = new
