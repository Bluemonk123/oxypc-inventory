"""Hide External Partner Test (ETP) tags and lots, trashed and inactive tags
from every page and export (2026-11-27, Pankaj).

One rule, applied centrally rather than in each of ~500 queries:

  * a tag is hidden when it is trashed, inactive, in the External Partner Test
    entity, or in a hidden lot;
  * a lot is hidden when it is trashed or its number starts with "EPT-"
    (External Partner Test lots).

How: while a request is in "hide" mode, every ORM SELECT gets
with_loader_criteria() for Device and Lot (do_orm_execute hook below), so
lists, counts, tiles, dropdowns and exports all leave those rows out.

Hide mode is ON for:
  * GET / HEAD requests (pages, data feeds, GET exports)
  * POST exports / downloads (paths in EXPORT_POSTS)
  * the Daily Stock Reports job (hidden_tags_scope())
and OFF for:
  * the External Partner section and partner portal — they must see ETP tags
  * actions that change data (POST / PATCH / DELETE), so a hidden tag can
    still be found, restored, moved or checked for duplicates
  * Trash, System Health / Data Fixes, machine APIs (/api/v1), login
  * one-tag lookups and duplicate checks (barcode lookup, "exists", "check")
  * opening one tag by its number (/devices/<tag>), e.g. from Trash or a link

Raw SQL text() queries are not ORM SELECTs; the few report queries that use
them add the same rule by hand (live_tag_sql()).

Escape hatch for a single query in hide mode:
    await db.execute(stmt.execution_options(include_hidden_tags=True))
"""
import contextvars
import re
from contextlib import contextmanager

from sqlalchemy import and_, event, or_
from sqlalchemy.orm import Session, with_loader_criteria

from models.device import Device
from models.lot import Lot
from models.master import EXTERNAL_PARTNER_TEST_ENTITY, EXTERNAL_PARTNER_TEST_LOT_PREFIX

_HIDE = contextvars.ContextVar("oxypc_hide_hidden_tags", default=False)

# Sections that must keep seeing everything (prefix match).
SHOW_ALL_PREFIXES = (
    "/trade-partner", "/partner", "/partner-payments",   # External Partner section + portal
    "/api/v1",                                           # machine APIs (OxyQC, integrations)
    "/trash", "/admin/system",                           # Trash, System Health / Data Fixes
    "/auth", "/static", "/health", "/favicon",
)

# One-tag lookups and duplicate checks: a hidden tag must still be found so it
# is never registered twice.
SHOW_ALL_PATTERNS = [re.compile(p) for p in (
    r"/lookup($|/)", r"/lookup-by-name$", r"-lookup$",
    r"/api/exists$", r"/api/check/", r"/lots/check$", r"precheck$",
    r"^/devices/(?!data$|export$|api/|barcodes$|recent-tags$|model-summary)[^/]+(/edit)?$",  # one tag by number
    r"^/devices/api/(brief|iqc-compare|asset-history|detail-cards)$",  # that tag page's own panels
    r"^/lots/(?!export|api/)[^/]+(/(edit|next-tag|register|line-items(/export)?))?$",  # one lot (next tag number counts all)
    r"^/api/device/[^/]+/next-stages$", r"^/locations/api/device-location/",
)]

# POST routes that only read data out (exports / downloads).
EXPORT_POSTS = re.compile(r"(export|download)", re.I)


def hide_for_request(method: str, path: str) -> bool:
    """Should this request run in hide mode?"""
    path = path or "/"
    if path.startswith(SHOW_ALL_PREFIXES):
        return False
    if any(p.search(path) for p in SHOW_ALL_PATTERNS):
        return False
    method = (method or "GET").upper()
    if method in ("GET", "HEAD"):
        return True
    return method == "POST" and bool(EXPORT_POSTS.search(path))


def set_hide(on: bool):
    """Set hide mode for the current context; returns a token for reset()."""
    return _HIDE.set(bool(on))


def reset_hide(token) -> None:
    _HIDE.reset(token)


def is_hiding() -> bool:
    return _HIDE.get()


@contextmanager
def hidden_tags_scope(on: bool = True):
    """Run a block (background job) with hide mode on/off."""
    tok = _HIDE.set(on)
    try:
        yield
    finally:
        _HIDE.reset(tok)


def _device_ok(cls):
    return and_(cls.is_trashed.isnot(True), cls.is_active.isnot(False),
                or_(cls.entity.is_(None), cls.entity != EXTERNAL_PARTNER_TEST_ENTITY))


def _lot_ok(cls):
    return and_(cls.is_trashed.isnot(True),
                or_(cls.lot_number.is_(None),
                    ~cls.lot_number.startswith(EXTERNAL_PARTNER_TEST_LOT_PREFIX)))


@event.listens_for(Session, "do_orm_execute")
def _hide_hidden_rows(state):
    if not _HIDE.get() or not state.is_select:
        return
    if state.execution_options.get("include_hidden_tags"):
        return
    # Relationship / column lazy loads inherit the criteria from the parent
    # statement already; only top-level statements need adding.
    if state.is_column_load or state.is_relationship_load:
        return
    state.statement = state.statement.options(
        with_loader_criteria(Device, _device_ok, include_aliases=True, track_closure_variables=False),
        with_loader_criteria(Lot, _lot_ok, include_aliases=True, track_closure_variables=False),
    )


def live_tag_sql(dev: str = "d", lot: str | None = None, trash: bool = True) -> str:
    """The same rule as SQL text for raw text() queries, e.g.
    f"... WHERE {live_tag_sql('d')}". Returns 'TRUE' when not in hide mode.
    lot: alias of a joined lots table; without one a sub-select is used.
    trash=False leaves the trashed / inactive part out, for "as of a date"
    reports that already judge trashing by trashed_at."""
    if not _HIDE.get():
        return "TRUE"
    etp, ept = EXTERNAL_PARTNER_TEST_ENTITY, EXTERNAL_PARTNER_TEST_LOT_PREFIX
    parts = [f"({dev}.entity IS NULL OR {dev}.entity <> '{etp}')"]
    if trash:
        parts += [f"{dev}.is_trashed IS NOT TRUE", f"{dev}.is_active IS NOT FALSE"]
    if lot:
        parts.append(f"({lot}.lot_number IS NULL OR {lot}.lot_number NOT LIKE '{ept}%')")
        if trash:
            parts.append(f"{lot}.is_trashed IS NOT TRUE")
    else:
        hidden_lot = f"lot_number LIKE '{ept}%'" + (" OR is_trashed IS TRUE" if trash else "")
        parts.append(f"{dev}.lot_id NOT IN (SELECT id FROM lots WHERE {hidden_lot})")
    return "(" + " AND ".join(parts) + ")"
