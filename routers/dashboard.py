import logging
import time as _time
import uuid
from templates_config import templates
from datetime import datetime, date
from utils.timezone import app_now
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_
from database import get_db
from utils.master_data import entity_values, report_year_values, master_values
from services.business_pl import compute_fy_totals, compute_fy_revenue, compute_month_profit, fy_label
from models.user import User, UserRole
from models.device import Device, DeviceStage, StageMovement, STAGE_LABELS, DROPDOWN_STAGES, COSMETIC_STAGES
from models.master import EXTERNAL_PARTNER_TEST_ENTITY
from models.engines import RepairAttempt
from models.lot import Lot
from models.sales import Sale
from models.spare_parts import SparePart, SparePartConsumption
from models.dealers import Dealer, DealerOrder, DealerCreditNote, DealerCall
from models.crm import CRMActivity, CRMContact, CRMPurchaseOrder, CRMSourcingDeal
from models.location import StorageLocation, ZoneType, ZONE_LABELS, DeviceLocationLog
from models.parts_grn import PartsGRN, PartsGRNLineItem
from models.part_request import PartSourcingRequest
from models.cost_config import CostConfig
from auth.dependencies import get_current_user
from routers.auth import NON_ADMIN_LANDING

_log = logging.getLogger("oxypc.dashboard")


router = APIRouter(tags=["dashboard"])

CATEGORIES = ["Laptop", "Desktop", "TFT Monitor", "Tablet", "Mini PC", "Server"]
KEY_STAGES = [
    DeviceStage.iqc,
    DeviceStage.stock_in,
    DeviceStage.l1,
    DeviceStage.l2,
    DeviceStage.l3,
    DeviceStage.qc_check,
    DeviceStage.ready_to_sale,
    DeviceStage.sold,
]

_OUTSTANDING_STATUSES = ("pending", "confirmed", "delivered")

# ── 30-second aggregate cache (stage counts + category counts) ────────────────
# These two GROUP-BY queries are the dashboard's most expensive and identical
# for every logged-in user. Cache the result for 30 s to avoid hammering the DB
# on every page refresh.
_AGG_CACHE: dict = {"stage": None, "cat": None, "pipeline": None, "lot_pl": None, "admin_analytics": None, "ts": 0.0, "lot_pl_ts": 0.0}
_AGG_TTL = 30  # seconds


async def _count(db: AsyncSession, *filters) -> int:
    result = await db.execute(select(func.count(Device.id)).where(*filters))
    return result.scalar() or 0


@router.get("/", response_class=HTMLResponse)
async def home(current_user: User = Depends(get_current_user)):
    """Application home page — My Attendance for everyone except admin, who
    keeps Inventory Search. Matches where login drops each role, so the app
    opens on the same page whether you sign in or hit the bare URL.
    no-store so browsers never cache this redirect across deploys/logins."""
    role_value = getattr(current_user.role, "value", None) or str(current_user.role)
    target = "/devices" if role_value == "admin" else NON_ADMIN_LANDING
    resp = RedirectResponse(url=target, status_code=302)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    stage_filter: str = Query(default=""),
    pl_from: str = Query(default=""),
    pl_to: str = Query(default=""),
    # Comma-separated, same convention as the All Inventory multi-selects.
    entity: str = Query(default=""),
    # Device Type filter — wired to Master Data's "device_type" category
    # (labelled "Device Form Factors" in Master Data's own UI; there is no
    # separate Form Factor concept, see routers/devices.py's own device_type
    # filter for the same source).
    device_type: str = Query(default=""),
    # Zone filter — narrows to any device whose resolved current location
    # (see the DeviceLocationLog-first / Device.location_id-fallback
    # resolution below) sits in a StorageLocation belonging to this zone
    # (models.location.ZoneType). Replaces the old single-location "Location
    # ID" filter, which was too granular for a dashboard-level view.
    zone: str = Query(default=""),
    # Financial Summary card's Financial Year (Apr–Mar), by its starting
    # year: 2025 = "2025-26" (2026-10-15; was a calendar Year in the top bar).
    fy: int = Query(default=None),
):
    _t0 = _time.perf_counter()
    _marks = []                      # (section, seconds) — logged when slow
    def _mark(name):
        _marks.append((name, _time.perf_counter()))
    today = app_now().date()
    if not fy:
        fy = today.year if today.month >= 4 else today.year - 1

    # ── Filter parsing (Entity / Device Type / Location) ─────────────────────
    # Moved ahead of the stage/category aggregate so that block can decide
    # whether to serve the shared 30 s cache (no filters active — the common
    # case) or run a filtered query fresh (see _dash_filters_active below).
    entity_vals = [e.strip() for e in (entity or "").split(",") if e.strip()]
    # No explicit Entity filter -> exclude the External Partner test-data
    # entity by default (models/master.py EXTERNAL_PARTNER_TEST_ENTITY), so
    # test GRN/Lot/Device records created via /trade-partner/manage-lots
    # never inflate the default dashboard view. An admin who explicitly picks
    # that entity still sees it (no exclusion added in that branch). Kept out
    # of _dash_filters_active below (keyed off the raw parsed inputs, not
    # _ent) so this baseline exclusion doesn't defeat the shared 30s cache.
    # NULL-safe: `entity != X` alone silently drops every entity-less device
    # too, since SQL NULL != X evaluates to NULL, not TRUE.
    _ent = ([Device.entity.in_(entity_vals)] if entity_vals
            else [or_(Device.entity.is_(None), Device.entity != EXTERNAL_PARTNER_TEST_ENTITY)])

    device_type_vals = [d.strip() for d in (device_type or "").split(",") if d.strip()]
    _dtype = [Device.device_type.in_(device_type_vals)] if device_type_vals else []

    zone_val = None
    if zone:
        try:
            zone_val = ZoneType(zone)
        except ValueError:
            zone_val = None
    # Device.location_id is essentially unpopulated for real devices (only a
    # handful of records ever get it set directly) — every page that shows a
    # device's actual current Location ID reads it from the LATEST
    # DeviceLocationLog row instead, falling back to Device.location_id only
    # when no log exists at all (see _build_location_map in routers/devices.py
    # and the same fix applied to Transfers). Filtering on the raw column
    # alone is why this filter looked like it did nothing — it matched almost
    # no rows regardless of which location was picked. Same resolution
    # approach here, just matched against any StorageLocation belonging to
    # the selected zone rather than one specific location id.
    _loc = []
    if zone_val:
        _zone_location_ids = select(StorageLocation.id).where(StorageLocation.zone == zone_val)
        # DISTINCT ON (2026-10-16): one pass over the log instead of
        # max()-per-device then a join back — ~6x faster on a large log.
        _latest_log = (
            select(DeviceLocationLog.device_id, DeviceLocationLog.location_id)
            .distinct(DeviceLocationLog.device_id)
            .order_by(DeviceLocationLog.device_id, DeviceLocationLog.logged_at.desc())
            .subquery()
        )
        _loc = [or_(
            Device.id.in_(select(_latest_log.c.device_id)
                          .where(_latest_log.c.location_id.in_(_zone_location_ids))),
            and_(
                ~Device.id.in_(select(_latest_log.c.device_id)),
                Device.location_id.in_(_zone_location_ids),
            ),
        )]
    # Keyed off the raw parsed inputs, not _ent (which is now never empty —
    # see the EXTERNAL_PARTNER_TEST_ENTITY exclusion above), so the baseline
    # test-entity exclusion doesn't itself defeat the shared 30s cache.
    _dash_filters_active = bool(entity_vals or _dtype or _loc)

    # ── P&L From/To — scoped by each device's own "stage completed" date ────
    # A device is "completed" once it reaches a terminal stage: Sold (its
    # completed date is the Sale's own sold_at) or Scrapped/Scrap for Sale
    # (completed date is the moved_at of the StageMovement into that stage).
    # Devices still mid-pipeline have no completed date and are never counted
    # once this filter is active. Only active when at least one of pl_from/
    # pl_to is set — with neither set, every P&L figure below stays exactly
    # the same all-time computation it always was.
    _pl_from_dt = _pl_to_dt = None
    try:
        if pl_from:
            _pl_from_dt = datetime.strptime(pl_from, "%Y-%m-%d")
        if pl_to:
            _pl_to_dt = datetime.strptime(pl_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        _pl_from_dt = _pl_to_dt = None
    _pl_active = bool(_pl_from_dt or _pl_to_dt)

    device_completed_at: dict = {}
    completed_device_ids: set = set()
    if _pl_active:
        _sold_completed = dict((await db.execute(
            select(Sale.device_id, func.max(Sale.sold_at)).group_by(Sale.device_id)
        )).fetchall())
        _scrap_completed = dict((await db.execute(
            select(StageMovement.device_id, func.max(StageMovement.moved_at))
            .where(StageMovement.to_stage.in_([DeviceStage.scrapped, DeviceStage.scrap_for_sale]))
            .group_by(StageMovement.device_id)
        )).fetchall())
        _terminal_devices = (await db.execute(
            select(Device.id, Device.current_stage)
            .where(Device.is_trashed == False, Device.current_stage.in_(
                [DeviceStage.sold, DeviceStage.scrapped, DeviceStage.scrap_for_sale]))
        )).all()
        for _did, _stage in _terminal_devices:
            _dt = _sold_completed.get(_did) if _stage == DeviceStage.sold else _scrap_completed.get(_did)
            if _dt:
                device_completed_at[_did] = _dt

        def _in_pl_range(dt):
            if _pl_from_dt and dt < _pl_from_dt:
                return False
            if _pl_to_dt and dt > _pl_to_dt:
                return False
            return True

        completed_device_ids = {did for did, dt in device_completed_at.items() if _in_pl_range(dt)}

    # Tags in these 3 dead-end stages are excluded from every dashboard total
    # (Total Devices, category cards, Stage Pipeline's own totals feed off
    # per-stage matches that already can't include them) — they no longer
    # move through the funnel, so counting them inflates "how much is
    # actually in flight" figures. They keep their own individual line
    # ("Returned: X · Scrapped: Y" under the pipeline) since that's a
    # deliberate, separate callout, not a total.
    EXCLUDED_STAGES = {DeviceStage.returned.value, DeviceStage.scrapped.value,
                        DeviceStage.scrap_for_sale.value}

    # Category quick-view tiles (dashboard.html's category_quick_view_cards
    # macro) additionally drop GRN Receipt and Sold — GRN because those tags
    # haven't cleared IQC yet, and Sold because the tile counts "how much of
    # this category is still active stock," not the full historical total.
    # Kept separate from EXCLUDED_STAGES so Total Inventory / Stage Pipeline
    # totals above are unaffected.
    CATEGORY_EXCLUDED_STAGES = EXCLUDED_STAGES | {DeviceStage.grn.value, DeviceStage.sold.value}

    _mark("filters+P&L range")
    # ── Stage + category counts ───────────────────────────────────────────────
    # Cached 30 s, but only when no Entity/Device Type/Location filter is
    # active — that cache is keyed on nothing, so serving it under an active
    # filter would hand back another viewer's (unfiltered) numbers.
    _now = _time.monotonic()
    if (not _dash_filters_active and _AGG_CACHE["ts"]
            and (_now - _AGG_CACHE["ts"]) < _AGG_TTL and _AGG_CACHE["stage"]):
        stage_counts = _AGG_CACHE["stage"]
        category_counts = _AGG_CACHE["cat"]
    else:
        try:
            stage_result = await db.execute(
                select(Device.current_stage, func.count(Device.id))
                .where(Device.is_trashed == False, *_ent, *_dtype, *_loc)
                .group_by(Device.current_stage)
            )
            stage_counts = {
                row[0].value: row[1]
                for row in stage_result.fetchall()
                if row[0] is not None
            }
            for stage in DeviceStage:
                stage_counts.setdefault(stage.value, 0)
        except Exception:
            _log.exception("stage_counts failed")
            stage_counts = {stage.value: 0 for stage in DeviceStage}

        try:
            cat_stage_result = await db.execute(
                select(Device.sub_category, Device.current_stage, func.count(Device.id))
                .where(Device.is_trashed == False, *_ent, *_dtype, *_loc)
                .group_by(Device.sub_category, Device.current_stage)
            )
            category_counts: dict = {cat: {"total": 0} for cat in CATEGORIES}
            for sub_cat, stage, cnt in cat_stage_result.fetchall():
                if sub_cat in category_counts and stage is not None:
                    if stage.value not in CATEGORY_EXCLUDED_STAGES:
                        category_counts[sub_cat]["total"] += cnt
                    category_counts[sub_cat][stage.value] = cnt
        except Exception:
            _log.exception("category_counts failed")
            category_counts = {cat: {"total": 0} for cat in CATEGORIES}

        if not _dash_filters_active:
            _AGG_CACHE.update({"stage": stage_counts, "cat": category_counts, "ts": _now})

    _mark("stage counts")
    # ── Stage Pipeline ───────────────────────────────────────────────────────
    # Built here rather than read off stage_counts, because two of the ten
    # steps are not a single "devices whose current_stage is X":
    #   GRN      - devices mapped to a GRN, which is what "Total Devices Added"
    #              on /grn/post-iqc counts. A device leaves the `grn` stage the
    #              moment it is stocked, so that stage count read near zero.
    #   Cosmetic - the six paint-line stages plus putty, counted together.
    # Deliberately not served from _AGG_CACHE: see _dash_filters_active above.
    async def _pipe_count(*where):
        return (await db.execute(
            select(func.count(Device.id))
            .where(Device.is_trashed == False, *_ent, *_dtype, *_loc, *where)
        )).scalar() or 0

    # Fixed display order for the Stage Pipeline's "Split Entity" breakdown —
    # GROUP BY has no defined row order, so without this the 3 entity lines
    # under each of the 10 cards came back in whatever order Postgres felt
    # like, differing card to card and request to request.
    ENTITY_DISPLAY_ORDER = ["OxyPC Computers", "Renew Circuits", "Deshwal"]

    async def _pipe_count_by_entity(*where):
        rows = (await db.execute(
            select(Device.entity, func.count(Device.id))
            .where(Device.is_trashed == False, *_ent, *_dtype, *_loc, *where)
            .group_by(Device.entity)
        )).all()
        counts = {(e or "Unassigned"): c for e, c in rows if c}
        ordered = {}
        for name in ENTITY_DISPLAY_ORDER:
            if name in counts:
                ordered[name] = counts.pop(name)
        for name in sorted(counts):
            ordered[name] = counts[name]
        return ordered

    # Same "latest DeviceLocationLog row, falling back to Device.location_id
    # only when no log exists at all" resolution as the Zone filter (_loc)
    # above, but unconditional/unfiltered by any specific zone — a separate
    # subquery so this doesn't disturb that already-working filter.
    # Used only for the Split Zone per-stage breakdown below.
    # 2026-10-16: DISTINCT ON instead of max()-then-join — one pass over the
    # log table, and a device can't be counted twice when two of its log rows
    # share the same logged_at.
    _loc_latest = (
        select(DeviceLocationLog.device_id, DeviceLocationLog.location_id)
        .distinct(DeviceLocationLog.device_id)
        .order_by(DeviceLocationLog.device_id, DeviceLocationLog.logged_at.desc())
        .subquery()
    )
    _resolved_location_id = func.coalesce(_loc_latest.c.location_id, Device.location_id)

    # Split Zone breakdown — the SQL still groups by resolved location id
    # (cheapest way to reuse the DeviceLocationLog-first/Device.location_id-
    # fallback resolution above), then the per-location counts are rolled up
    # into per-zone counts in Python via _zone_by_location_id (built just
    # below, inside the pipeline try block). Now that the primary filter
    # (_loc) is zone-based rather than location-based, this breakdown
    # matches it.
    async def _pipe_count_by_location(*where):
        rows = (await db.execute(
            select(_resolved_location_id, func.count(Device.id))
            .select_from(Device)
            .outerjoin(_loc_latest, _loc_latest.c.device_id == Device.id)
            .where(Device.is_trashed == False, *_ent, *_dtype, *_loc, *where)
            .group_by(_resolved_location_id)
        )).all()
        counts = {}
        for loc_id, cnt in rows:
            if not cnt:
                continue
            zone_of_loc = _zone_by_location_id.get(str(loc_id)) if loc_id else None
            label = ZONE_LABELS.get(zone_of_loc, getattr(zone_of_loc, "value", zone_of_loc)) if zone_of_loc else "Unassigned"
            counts[label] = counts.get(label, 0) + cnt
        return dict(sorted(counts.items()))

    FINAL_QC_STAGES = [
        DeviceStage.final_qc, DeviceStage.final_qc_pass_hold,
        DeviceStage.final_qc_fail_hold,
    ]
    # Order follows DeviceStage's own declaration order (grn, iqc, ..., l3,
    # trc_production, qc_check, ...) — IQC and Production are new additions
    # slotted into that same sequence rather than tacked on at the end.
    #
    # 2026-09-30 count-sync verification: each tile's link in dashboard.html
    # (~line 244) is the actual destination page whose own count this
    # _pipe_count() should match. Checked every one of them against this
    # module's own filter (is_trashed==False, *_ent, *_dtype, *_loc, plus the
    # step's own stage clause):
    #   - iqc / stock_in / production / l1l2 / qc_check / cosmetic /
    #     final_qc / ready_to_sale / sold all link to `/devices?stage=...`
    #     (routers/devices.py's Inventory Search list, via _device_search_filters
    #     + the /data DataTables feed's `total`). That page applies the exact
    #     same is_trashed==False and default EXTERNAL_PARTNER_TEST_ENTITY
    #     exclusion (byte-for-byte identical NULL-safe `or_` clause) as _ent
    #     above, has NO is_active check at all, and its "Active Stock Only"
    #     default-on exclusion (grn/sold/returned/scrapped/scrap_for_sale)
    #     never actually removes anything for these queries — the stage each
    #     tile requests is either outside that excluded set already, or (for
    #     "sold") is explicitly selected and therefore carved back out of the
    #     exclusion. Net result: _pipe_count() already matches these 9 tiles'
    #     destination pages exactly. No per-step filter override was needed
    #     or added.
    #   - "grn" links to /grn/post-iqc and is intentionally a different kind
    #     of count (has-a-GRN, not current_stage==grn) — left as-is per
    #     design, not touched.
    #   - "l3l4" links to /repair/l3l4, which counts open `L3L4-%` WorkOrders
    #     joined to Device (not a plain current_stage==l3 match), has no
    #     entity/is_active filtering of its own, and — for non-admin/non-
    #     "full queue" roles — further restricts to WorkOrder.assigned_user_id
    #     == the *viewing* user. That last part makes it impossible for a
    #     single dashboard-wide count to replicate exactly (it depends on who
    #     is looking, not just device state). Left unchanged and flagged
    #     here rather than guessed at.
    GRN_WHERE = [Device.grn_number.isnot(None), Device.grn_number != "",
                 Device.is_active == True]  # noqa: E712
    # Stage-based steps as stage lists (used by the grouped query below; must
    # stay in sync with PIPELINE_STEPS).
    STEP_STAGES = {
        "iqc": [DeviceStage.iqc], "stock_in": [DeviceStage.stock_in],
        "l1l2": [DeviceStage.l1, DeviceStage.l2], "l3l4": [DeviceStage.l3],
        "production": [DeviceStage.trc_production], "qc_check": [DeviceStage.qc_check],
        "cosmetic": list(COSMETIC_STAGES), "final_qc": list(FINAL_QC_STAGES),
        "ready_to_sale": [DeviceStage.ready_to_sale], "sold": [DeviceStage.sold],
    }
    PIPELINE_STEPS = [
        ("grn", [Device.grn_number.isnot(None), Device.grn_number != "",
                 Device.is_active == True]),
        ("iqc", [Device.current_stage == DeviceStage.iqc]),
        ("stock_in", [Device.current_stage == DeviceStage.stock_in]),
        ("l1l2", [Device.current_stage.in_([DeviceStage.l1, DeviceStage.l2])]),
        # 2026-09-19: request_l3l4() now moves the device to DeviceStage.l3
        # for the duration of the L3/L4 repair (routers/repair.py), so this
        # is a real stage match again, not an l1l2_status slice of L1.
        ("l3l4", [Device.current_stage == DeviceStage.l3]),
        ("production", [Device.current_stage == DeviceStage.trc_production]),
        ("qc_check", [Device.current_stage == DeviceStage.qc_check]),
        ("cosmetic", [Device.current_stage.in_(COSMETIC_STAGES)]),
        ("final_qc", [Device.current_stage.in_(FINAL_QC_STAGES)]),
        ("ready_to_sale", [Device.current_stage == DeviceStage.ready_to_sale]),
        ("sold", [Device.current_stage == DeviceStage.sold]),
    ]
    # Pipeline strip: 11 steps x 3 queries each = 33 round-trips. Cached
    # under the same 30 s window/gate as stage_counts/category_counts above
    # (reuses _now from that check) — this was previously recomputed on
    # every single dashboard load regardless of the cache.
    # Own timestamp (2026-10-16): this used to check the shared "ts", which
    # the stage-count block above refreshes first — so once filled, the
    # pipeline was never recomputed until a server restart (stale numbers).
    if (not _dash_filters_active and _AGG_CACHE.get("pipeline")
            and (_now - _AGG_CACHE.get("pipeline_ts", 0.0)) < _AGG_TTL):
        pipeline_counts, pipeline_by_entity, pipeline_by_location = _AGG_CACHE["pipeline"]
    else:
        try:
            # Maps a resolved location id -> its ZoneType, for the Split Zone
            # breakdown in _pipe_count_by_location above.
            _zone_by_location_id = {
                str(loc.id): loc.zone
                for loc in (await db.execute(select(StorageLocation))).scalars().all()
            }
            # 2026-10-16 speed-up: was 3 queries x 11 steps = 33 round-trips,
            # each by-zone one re-scanning the whole location log (~2 s on a
            # production-sized DB). Now ONE grouped query (stage x entity x
            # resolved location) for the 10 stage-based steps plus one for
            # GRN, rolled up in Python. Same filters, same numbers.
            _step_of_stage = {}
            for _k, _sts in STEP_STAGES.items():
                for _st in _sts:
                    _step_of_stage[_st] = _k
            _base = [Device.is_trashed == False, *_ent, *_dtype, *_loc]  # noqa: E712

            def _zone_label(loc_id):
                z = _zone_by_location_id.get(str(loc_id)) if loc_id else None
                return ZONE_LABELS.get(z, getattr(z, "value", z)) if z else "Unassigned"

            _cnt = {k: 0 for k, _ in PIPELINE_STEPS}
            _ent_c = {k: {} for k, _ in PIPELINE_STEPS}
            _zone_c = {k: {} for k, _ in PIPELINE_STEPS}

            def _add(key, ent, loc_id, n):
                _cnt[key] += n
                e = ent or "Unassigned"
                _ent_c[key][e] = _ent_c[key].get(e, 0) + n
                zl = _zone_label(loc_id)
                _zone_c[key][zl] = _zone_c[key].get(zl, 0) + n

            for st, ent, loc_id, n in (await db.execute(
                select(Device.current_stage, Device.entity, _resolved_location_id, func.count(Device.id))
                .select_from(Device)
                .outerjoin(_loc_latest, _loc_latest.c.device_id == Device.id)
                .where(*_base, Device.current_stage.in_(list(_step_of_stage)))
                .group_by(Device.current_stage, Device.entity, _resolved_location_id)
            )).all():
                _add(_step_of_stage[st], ent, loc_id, n)
            for ent, loc_id, n in (await db.execute(
                select(Device.entity, _resolved_location_id, func.count(Device.id))
                .select_from(Device)
                .outerjoin(_loc_latest, _loc_latest.c.device_id == Device.id)
                .where(*_base, *GRN_WHERE)
                .group_by(Device.entity, _resolved_location_id)
            )).all():
                _add("grn", ent, loc_id, n)

            def _order_entities(counts):
                counts = {k: v for k, v in counts.items() if v}
                ordered = {n: counts.pop(n) for n in ENTITY_DISPLAY_ORDER if n in counts}
                ordered.update({n: counts[n] for n in sorted(counts)})
                return ordered

            pipeline_counts = _cnt
            pipeline_by_entity = {k: _order_entities(v) for k, v in _ent_c.items()}
            pipeline_by_location = {k: dict(sorted(v.items())) for k, v in _zone_c.items()}
        except Exception:
            _log.exception("pipeline_counts failed")
            pipeline_counts = {k: 0 for k, _ in PIPELINE_STEPS}
            pipeline_by_entity = {k: {} for k, _ in PIPELINE_STEPS}
            pipeline_by_location = {k: {} for k, _ in PIPELINE_STEPS}

        if not _dash_filters_active:
            _AGG_CACHE["pipeline"] = (pipeline_counts, pipeline_by_entity, pipeline_by_location)
            _AGG_CACHE["pipeline_ts"] = _now

    entity_choices = await entity_values(db)
    device_type_choices = await master_values(db, "device_type")
    storage_locations = (await db.execute(
        select(StorageLocation).where(StorageLocation.is_active == True)
        .order_by(StorageLocation.zone, StorageLocation.unit_id)
    )).scalars().all()

    # Total Inventory now matches the category tiles' "Total" definition
    # exactly (excludes GRN/Sold/Returned/Scrapped/Scrap for Sale) rather
    # than the narrower EXCLUDED_STAGES, so the two numbers agree.
    total_devices = sum(v for k, v in stage_counts.items() if k not in CATEGORY_EXCLUDED_STAGES)
    # "In Repair" summary card: TRC Production, L1/L2, L3/L4, Stress
    # (QC Check), the 8 cosmetic-line stages, and Final QC (incl. its
    # pass/fail hold sub-stages) — everything actively in the repair/
    # refurb pipeline between Stock In and Ready to Sale. Reuses the
    # already-cached pipeline_counts rather than a fresh query.
    in_repair_count = (pipeline_counts.get("production", 0)
                        + pipeline_counts.get("l1l2", 0)
                        + pipeline_counts.get("l3l4", 0)
                        + pipeline_counts.get("qc_check", 0)
                        + pipeline_counts.get("cosmetic", 0)
                        + pipeline_counts.get("final_qc", 0))
    laptops_available = category_counts.get("Laptop", {}).get("ready_to_sale", 0)
    desktops_available = category_counts.get("Desktop", {}).get("ready_to_sale", 0)
    tft_available = category_counts.get("TFT Monitor", {}).get("ready_to_sale", 0)
    tablets_available = category_counts.get("Tablet", {}).get("ready_to_sale", 0)
    minipc_available = category_counts.get("Mini PC", {}).get("ready_to_sale", 0)
    server_available = category_counts.get("Server", {}).get("ready_to_sale", 0)

    # ── From/To set: counts show tags MOVED in the range (2026-10-22) ───────
    # No dates: every count is live stock ("where tags are now") — unchanged.
    # Dates set (user's choice, "moved in range"): every count is the tags that
    # moved into that stage between the dates; a tag counts once per stage,
    # however often it moved there. Sold = tags with a sale dated in the range.
    #   Pipeline tiles + Split Entity / Split Zone  -> moved into that step
    #   Returned / Scrapped / Scrap for Sale / On Rent -> moved into that stage
    #   Category cards: total = moved into any active step (IQC..Ready to
    #     Sale); In Ready = moved into Ready to Sale; In TRC = the rest
    #   Total Inventory = moved into any active step; In Repair = moved into
    #     any repair step; charts follow the same counts
    #   Revenue / Profit tiles -> sales dated in the range (below)
    # Never cached: it depends on the dates.
    side_counts = None          # None -> template uses live stage_counts
    range_moves = None          # step -> {device_id: (entity, zone, sub_category)}
    if _pl_active:
        try:
            def _rng(col):
                w = []
                if _pl_from_dt:
                    w.append(col >= _pl_from_dt)
                if _pl_to_dt:
                    w.append(col <= _pl_to_dt)
                return w

            _zone_of = {str(l.id): l.zone for l in (await db.execute(select(StorageLocation))).scalars().all()}

            def _zl(loc_id):
                z = _zone_of.get(str(loc_id)) if loc_id else None
                return ZONE_LABELS.get(z, getattr(z, "value", z)) if z else "Unassigned"

            _abase = [Device.is_trashed == False, *_ent, *_dtype, *_loc]  # noqa: E712
            _side_key = {DeviceStage.returned: "returned", DeviceStage.scrapped: "scrapped",
                         DeviceStage.scrap_for_sale: "scrap_for_sale", DeviceStage.on_rent: "on_rent"}
            _step_of = {st: k for k, sts in STEP_STAGES.items() if k != "sold" for st in sts}
            _tags: dict = {k: {} for k in STEP_STAGES}
            _side: dict = {k: set() for k in _side_key.values()}
            for to_stage, did, ent, loc_id, subcat in (await db.execute(
                select(StageMovement.to_stage, Device.id, Device.entity, _resolved_location_id, Device.sub_category)
                .select_from(StageMovement).join(Device, Device.id == StageMovement.device_id)
                .outerjoin(_loc_latest, _loc_latest.c.device_id == Device.id)
                .where(*_abase, StageMovement.to_stage.in_(list(_step_of) + list(_side_key)),
                       *_rng(StageMovement.moved_at))
                .distinct()
            )).all():
                if to_stage in _side_key:
                    _side[_side_key[to_stage]].add(did)
                else:
                    _tags[_step_of[to_stage]][did] = (ent, _zl(loc_id), subcat)
            # Sold by the sale's own date (Change Sale Date moves it; the
            # stage move into Sold keeps its original timestamp).
            for did, ent, loc_id, subcat in (await db.execute(
                select(Device.id, Device.entity, _resolved_location_id, Device.sub_category)
                .select_from(Sale).join(Device, Device.id == Sale.device_id)
                .outerjoin(_loc_latest, _loc_latest.c.device_id == Device.id)
                .where(*_abase, *_rng(Sale.sold_at)).distinct()
            )).all():
                _tags["sold"][did] = (ent, _zl(loc_id), subcat)

            def _ordered(c):
                c = {k: v for k, v in c.items() if v}
                out = {n: c.pop(n) for n in ENTITY_DISPLAY_ORDER if n in c}
                out.update({n: c[n] for n in sorted(c)})
                return out

            pipeline_counts = {k: len(_tags.get(k, {})) for k, _ in PIPELINE_STEPS}
            pipeline_by_entity, pipeline_by_location = {}, {}
            for k, _w in PIPELINE_STEPS:
                e, z = {}, {}
                for ent, zone, _sc in _tags.get(k, {}).values():
                    e[ent or "Unassigned"] = e.get(ent or "Unassigned", 0) + 1
                    z[zone] = z.get(zone, 0) + 1
                pipeline_by_entity[k] = _ordered(e)
                pipeline_by_location[k] = dict(sorted(z.items()))
            side_counts = {k: len(v) for k, v in _side.items()}

            _active_steps = [k for k in STEP_STAGES if k != "sold"]
            _active = {}
            for k in _active_steps:
                _active.update(_tags[k])
            _repair = set().union(*(_tags[k].keys() for k in
                                    ("production", "l1l2", "l3l4", "qc_check", "cosmetic", "final_qc")))
            total_devices = len(_active)
            in_repair_count = len(_repair)
            category_counts = {cat: {"total": sum(1 for v in _active.values() if v[2] == cat)}
                               for cat in CATEGORIES}
            _ready = {cat: sum(1 for v in _tags["ready_to_sale"].values() if v[2] == cat) for cat in CATEGORIES}
            laptops_available = _ready.get("Laptop", 0)
            desktops_available = _ready.get("Desktop", 0)
            tft_available = _ready.get("TFT Monitor", 0)
            tablets_available = _ready.get("Tablet", 0)
            minipc_available = _ready.get("Mini PC", 0)
            server_available = _ready.get("Server", 0)
            range_moves = _tags
        except Exception:
            _log.exception("moved-in-range counts failed")
            side_counts = range_moves = None

    _mark("pipeline")
    # ── Role-based user queue ─────────────────────────────────────────────────
    role = current_user.role
    user_queue: dict = {}

    try:
        if role == UserRole.iqc_inspector:
            user_queue["iqc_pending"] = stage_counts.get(DeviceStage.iqc.value, 0)

        elif role == UserRole.l1_engineer:
            user_queue["l1_count"] = stage_counts.get(DeviceStage.l1.value, 0)

        elif role == UserRole.l2_engineer:
            user_queue["l2_count"] = stage_counts.get(DeviceStage.l2.value, 0)

        elif role == UserRole.l3_engineer:
            user_queue["l3_count"] = stage_counts.get(DeviceStage.l3.value, 0)

        elif role == UserRole.qc_inspector:
            user_queue["qc_pending"] = stage_counts.get(DeviceStage.qc_check.value, 0)

        elif role in (UserRole.sales, UserRole.sales_manager, UserRole.telecaller):
            user_queue["ready_to_sale"] = stage_counts.get(DeviceStage.ready_to_sale.value, 0)
            ts_result = await db.execute(
                select(func.count(Sale.id)).where(func.date(Sale.sold_at) == today)
            )
            user_queue["today_sales"] = ts_result.scalar() or 0
            mr_result = await db.execute(
                select(func.coalesce(func.sum(Sale.sale_price), 0)).where(
                    func.date(Sale.sold_at) >= date(today.year, today.month, 1)
                )
            )
            user_queue["month_revenue"] = float(mr_result.scalar() or 0)

            # Dealer outstanding for sales roles
            out_res = await db.execute(
                select(func.coalesce(func.sum(DealerOrder.due_amount), 0))
                .where(DealerOrder.status.in_(_OUTSTANDING_STATUSES))
            )
            user_queue["dealer_outstanding_total"] = float(out_res.scalar() or 0)

            overdue_res = await db.execute(
                select(func.count(DealerOrder.id))
                .where(
                    DealerOrder.due_amount > 0,
                    DealerOrder.payment_due_date.isnot(None),
                    DealerOrder.payment_due_date < app_now(),
                )
            )
            user_queue["dealer_overdue_count"] = int(overdue_res.scalar() or 0)

        elif role == UserRole.spare_parts_manager:
            ls_result = await db.execute(
                select(func.count(SparePart.id)).where(SparePart.qty_in_stock <= SparePart.min_stock_alert)
            )
            user_queue["low_stock_count"] = ls_result.scalar() or 0
            pv_result = await db.execute(
                select(func.coalesce(func.sum(SparePart.qty_in_stock * SparePart.unit_price), 0))
            )
            user_queue["total_parts_value"] = float(pv_result.scalar() or 0)
            tc_result = await db.execute(
                select(func.coalesce(func.sum(SparePartConsumption.qty_used), 0)).where(
                    func.date(SparePartConsumption.used_at) == today
                )
            )
            user_queue["today_consumption"] = int(tc_result.scalar() or 0)

        elif role == UserRole.inventory_manager:
            user_queue["stock_in_count"] = stage_counts.get(DeviceStage.stock_in.value, 0)
            lot_res = await db.execute(select(func.count(Lot.id)))
            user_queue["lot_count"] = lot_res.scalar() or 0

        elif role == UserRole.admin:
            user_queue["iqc_pending"]    = stage_counts.get(DeviceStage.iqc.value, 0)
            user_queue["l1_count"]       = stage_counts.get(DeviceStage.l1.value, 0)
            user_queue["l2_count"]       = stage_counts.get(DeviceStage.l2.value, 0)
            user_queue["l3_count"]       = stage_counts.get(DeviceStage.l3.value, 0)
            user_queue["qc_pending"]     = stage_counts.get(DeviceStage.qc_check.value, 0)
            user_queue["ready_to_sale"]  = stage_counts.get(DeviceStage.ready_to_sale.value, 0)
            ts_result = await db.execute(
                select(func.count(Sale.id)).where(func.date(Sale.sold_at) == today)
            )
            user_queue["today_sales"] = ts_result.scalar() or 0
            mr_result = await db.execute(
                select(func.coalesce(func.sum(Sale.sale_price), 0)).where(
                    func.date(Sale.sold_at) >= date(today.year, today.month, 1)
                )
            )
            user_queue["month_revenue"] = float(mr_result.scalar() or 0)
            ls_result = await db.execute(
                select(func.count(SparePart.id)).where(SparePart.qty_in_stock <= SparePart.min_stock_alert)
            )
            user_queue["low_stock_count"] = ls_result.scalar() or 0
            user_queue["stock_in_count"] = stage_counts.get(DeviceStage.stock_in.value, 0)
            lot_res = await db.execute(select(func.count(Lot.id)))
            user_queue["lot_count"] = lot_res.scalar() or 0

            # Dealer financial KPIs
            out_res = await db.execute(
                select(func.coalesce(func.sum(DealerOrder.due_amount), 0))
                .where(DealerOrder.status.in_(_OUTSTANDING_STATUSES))
            )
            user_queue["dealer_outstanding_total"] = float(out_res.scalar() or 0)

            overdue_res = await db.execute(
                select(func.count(DealerOrder.id))
                .where(
                    DealerOrder.due_amount > 0,
                    DealerOrder.payment_due_date.isnot(None),
                    DealerOrder.payment_due_date < app_now(),
                )
            )
            user_queue["dealer_overdue_count"] = int(overdue_res.scalar() or 0)

            cn_res = await db.execute(
                select(func.count(DealerCreditNote.id))
                .where(DealerCreditNote.created_at >= datetime(today.year, today.month, 1))
            )
            user_queue["dealer_credit_notes_month"] = int(cn_res.scalar() or 0)

    except Exception:
        _log.exception("user_queue failed for role=%s", role)
        # user_queue keeps whatever populated before the exception

    # ── Chart data: category × stage breakdown ────────────────────────────────
    # X-axis = the Stage Pipeline's own steps (2026-10-16), same order and
    # grouping as the pipeline strip (minus Sold); each bar sums its raw stages.
    def _sv(st):
        return st.value if hasattr(st, "value") else st
    chart_groups = [
        ("IQC",           [DeviceStage.iqc]),
        ("Stock In",      [DeviceStage.stock_in]),
        ("Production",    [DeviceStage.trc_production]),
        ("L1/L2",         [DeviceStage.l1, DeviceStage.l2]),
        ("L3/L4",         [DeviceStage.l3]),
        ("Stress/QC",     [DeviceStage.qc_check]),
        ("Cosmetic",      list(COSMETIC_STAGES)),
        ("Final QC",      list(FINAL_QC_STAGES)),
        ("Ready to Sale", [DeviceStage.ready_to_sale]),
        # "Sold" bar removed by request (2026-10-16) - the chart shows stock
        # still in the building; sold tags dwarfed every other stage.
    ]
    chart_labels = [lbl for lbl, _ in chart_groups]
    chart_data: dict = {}
    if range_moves is not None:
        # Date range set: tags of the category that moved into each step.
        _chart_steps = ["iqc", "stock_in", "production", "l1l2", "l3l4",
                        "qc_check", "cosmetic", "final_qc", "ready_to_sale"]
        for cat in CATEGORIES:
            chart_data[cat] = [sum(1 for v in range_moves[k].values() if v[2] == cat)
                               for k in _chart_steps]
    else:
        for cat in CATEGORIES:
            chart_data[cat] = [sum(category_counts[cat].get(_sv(st), 0) for st in sts)
                               for _, sts in chart_groups]

    # ── Lot P&L (10 queries; cached 30 s when no P&L From/To filter is
    # active — the common case, and identical for every viewer) ───────────────
    async def _compute_lot_pl():
        lot_pl: list = []
        try:
            lots_result = await db.execute(select(Lot).order_by(Lot.created_at.desc()))
            lots = lots_result.scalars().all()


            # Batch 1: device count per lot (always the lot's full, all-time
            # device count — never date-scoped, it's descriptive of the lot
            # itself, not of P&L activity within a period)
            lot_device_counts = dict((await db.execute(
                select(Device.lot_id, func.count(Device.id))
                .where(Device.is_trashed == False)
                .group_by(Device.lot_id)
            )).fetchall())

            # When P&L From/To is active, every cost/revenue batch below is
            # additionally restricted to devices that actually completed (Sold /
            # Scrapped / Scrap for Sale) within the selected range — see
            # completed_device_ids above. With no filter set, these are the exact
            # same all-time queries this table always ran.
            _completed_filter = ([Device.id.in_(completed_device_ids)] if _pl_active else [])
            _completed_filter_spc = ([SparePartConsumption.device_id.in_(completed_device_ids)] if _pl_active else [])

            # Batch 2: revenue per lot (join through Device)
            lot_revenue = dict((await db.execute(
                select(Device.lot_id, func.coalesce(func.sum(Sale.sale_price), 0))
                .join(Sale, Sale.device_id == Device.id)
                .where(Device.is_trashed == False, *_completed_filter)
                .group_by(Device.lot_id)
            )).fetchall())

            # Batch 3: parts cost per lot — attributed to the DEVICE the part was
            # consumed on, not the consumption's own date (device_id, not lot_id,
            # so the completed-devices restriction can apply directly).
            lot_parts_cost = dict((await db.execute(
                select(SparePartConsumption.lot_id, func.coalesce(func.sum(SparePartConsumption.total_cost), 0))
                .where(SparePartConsumption.lot_id.isnot(None), *_completed_filter_spc)
                .group_by(SparePartConsumption.lot_id)
            )).fetchall())

            # Batch 4: sold device count per lot
            lot_sold_counts = dict((await db.execute(
                select(Device.lot_id, func.count(Device.id))
                .where(Device.is_trashed == False, Device.current_stage == DeviceStage.sold, *_completed_filter)
                .group_by(Device.lot_id)
            )).fetchall())

            # Batch 5 (2026-10-29): repair cost per lot — Labour (Labor Rates by
            # device type, With L3/L4 when the tag had L3/L4, worked-on tags)
            # and Packing + Freight (sold tags, where they apply).
            # services/repair_cost.py. Replaced repair-attempt cost / count ×
            # Labour Rate and cleaning-stage count × Other Rates.
            from services.repair_cost import lot_repair_costs
            lot_repair = await lot_repair_costs(db, device_where=_completed_filter)

            # Batch 8: completed-device count per lot, for attributing each lot's
            # buying_price per-unit (buying_price / qty) only to the devices that
            # actually completed within the selected range — only run when the
            # filter is active; otherwise the full lot buying_price is used as-is
            # (all-time behavior, unchanged).
            lot_completed_counts = {}
            if _pl_active:
                lot_completed_counts = dict((await db.execute(
                    select(Device.lot_id, func.count(Device.id))
                    .where(Device.is_trashed == False, *_completed_filter)
                    .group_by(Device.lot_id)
                )).fetchall())

            for lot in lots:
                revenue      = float(lot_revenue.get(lot.id, 0) or 0)
                parts_cost   = float(lot_parts_cost.get(lot.id, 0) or 0)
                if _pl_active:
                    # Per-unit cost basis × only the devices that completed
                    # in-range — proper cost/revenue matching for a period,
                    # instead of the whole lot's purchase cost.
                    per_unit = (float(lot.buying_price or 0) / lot.qty) if lot.qty else 0.0
                    buying = per_unit * int(lot_completed_counts.get(lot.id, 0) or 0)
                else:
                    buying = float(lot.buying_price or 0)

                # Repair cost (2026-10-29): Labour + Packing/Freight. The
                # "cosmetic_cost" key now carries Packing + Freight.
                _lab, _pf, _ = lot_repair.get(lot.id, (0, 0, 0))
                labour_cost    = float(_lab)
                cosmetic_cost  = float(_pf)

                total_cost = buying + parts_cost + labour_cost + cosmetic_cost
                profit     = revenue - total_cost
                margin     = (profit / revenue * 100) if revenue > 0 else 0

                # When the filter is active, a lot with nothing that completed
                # in-range contributes nothing to the period's P&L — skip it
                # rather than list a noisy all-zero row.
                if _pl_active and not (revenue or parts_cost or labour_cost or cosmetic_cost or buying):
                    continue

                lot_pl.append({
                    "lot_number": lot.lot_number,
                    "supplier": lot.supplier_name,
                    "qty": lot.qty,
                    "devices_count": lot_device_counts.get(lot.id, 0),
                    "devices_sold": lot_sold_counts.get(lot.id, 0),
                    "buying_price": buying,
                    "parts_cost": parts_cost,
                    "labour_cost": labour_cost,
                    "cosmetic_cost": cosmetic_cost,
                    "total_cost": total_cost,
                    "revenue": revenue,
                    "profit": profit,
                    "margin": round(margin, 1),
                    "lot_id": str(lot.id),
                })
        except Exception:
            _log.exception("lot_pl failed")
        return lot_pl

    _mark("user queue")
    _lot_pl_now = _time.monotonic()
    if (not _pl_active and _AGG_CACHE["lot_pl"] is not None
            and (_lot_pl_now - _AGG_CACHE["lot_pl_ts"]) < _AGG_TTL):
        lot_pl = _AGG_CACHE["lot_pl"]
    else:
        lot_pl = await _compute_lot_pl()
        if not _pl_active:
            _AGG_CACHE["lot_pl"] = lot_pl
            _AGG_CACHE["lot_pl_ts"] = _lot_pl_now

    # ── Financial totals ───────────────────────────────────────────────────────
    month_revenue = 0.0
    total_revenue = 0.0
    total_investment = 0.0
    total_parts_cost = 0.0
    total_labour_cost = 0.0
    total_cosmetic_cost = 0.0
    overall_profit = 0.0
    yearly_parts_cost = 0.0
    yearly_labour_cost = 0.0
    yearly_device_cost = 0.0
    fy_revenue = 0.0
    month_profit = 0.0
    _mark("lot P&L")

    async def _compute_financials():
        """(month_revenue, total_revenue, total_investment, total_parts_cost,
        total_labour_cost, yearly_parts_cost, yearly_labour_cost)."""
        month_revenue = float((await db.execute(
            select(func.coalesce(func.sum(Sale.sale_price), 0))
            .where(func.date(Sale.sold_at) >= date(today.year, today.month, 1))
        )).scalar() or 0)

        if _pl_active:
            # Scoped to devices whose own stage-completed date falls in the
            # selected P&L From/To range (see completed_device_ids above) —
            # Total Investment/Revenue/Net Profit stop being all-time the
            # moment either bound is set, matching what lot_pl now does.
            _cd = list(completed_device_ids)
            total_revenue = float((await db.execute(
                select(func.coalesce(func.sum(Sale.sale_price), 0))
                .where(Sale.device_id.in_(_cd))
            )).scalar() or 0)
            # Investment: each lot's buying_price attributed per-unit
            # (buying_price / qty) to only the devices that completed
            # in-range — same logic as the Lot P&L table's per-lot buying.
            total_investment = sum(r["buying_price"] for r in lot_pl)
            total_parts_cost = float((await db.execute(
                select(func.coalesce(func.sum(SparePartConsumption.total_cost), 0))
                .where(SparePartConsumption.device_id.in_(_cd))
            )).scalar() or 0)
            total_labour_cost = float((await db.execute(
                select(func.coalesce(func.sum(RepairAttempt.cost), 0))
                .where(RepairAttempt.device_id.in_(_cd))
            )).scalar() or 0)
        else:
            total_revenue = float((await db.execute(
                select(func.coalesce(func.sum(Sale.sale_price), 0)))).scalar() or 0)
            total_investment = float((await db.execute(
                select(func.coalesce(func.sum(Lot.buying_price), 0)))).scalar() or 0)
            total_parts_cost = float((await db.execute(
                select(func.coalesce(func.sum(SparePartConsumption.total_cost), 0)))).scalar() or 0)
            total_labour_cost = float((await db.execute(
                select(func.coalesce(func.sum(RepairAttempt.cost), 0)))).scalar() or 0)

        # Parts Spent / Labour Spent (Financial Summary card) stay scoped to
        # the selected Year dropdown, not P&L From/To — deliberately kept in
        # sync with Business P&L's Monthly Breakdown table (Parts Cost /
        # Labour Cost columns) via the exact same computation, so the two
        # pages never disagree on the selected year's numbers.
        # Total Investment / Parts Spent / Labour Spent (card): Business P&L's
        # Device / Parts / Labour Cost columns summed over the selected
        # Financial Year Apr–Mar (2026-10-15), same shared computation.
        fy_device, fy_parts, fy_labour = await compute_fy_totals(db, fy)
        # Total Revenue (card) = Business P&L's FY Revenue total (2026-10-15).
        fy_revenue = await compute_fy_revenue(db, fy)
        # Profit This Month (stat card) = Business P&L's Gross Profit for the
        # current month (2026-10-15; replaces the old all-time Overall Profit).
        month_profit = await compute_month_profit(db, today.year, today.month)
        return (month_revenue, total_revenue, total_investment, total_parts_cost,
                total_labour_cost, fy_parts, fy_labour, fy_device, fy_revenue, month_profit)

    try:
        # Cached on the same 30 s cycle as the sections above (2026-10-15 —
        # these 7 sums ran on every load); keyed by Year, and never cached
        # while a P&L From/To range is set.
        _fin_key = ("fin", fy)
        _fin = _AGG_CACHE.get("fin")
        if (not _pl_active and _fin and _fin[0] == _fin_key
                and (_time.monotonic() - _fin[1]) < _AGG_TTL):
            fin_vals = _fin[2]
        else:
            fin_vals = await _compute_financials()
            if not _pl_active:
                _AGG_CACHE["fin"] = (_fin_key, _time.monotonic(), fin_vals)
        (month_revenue, total_revenue, total_investment, total_parts_cost,
         total_labour_cost, yearly_parts_cost, yearly_labour_cost,
         yearly_device_cost, fy_revenue, month_profit) = fin_vals
        total_cosmetic_cost = sum(r["cosmetic_cost"] for r in lot_pl)
        overall_profit = total_revenue - total_investment - total_parts_cost - total_labour_cost - total_cosmetic_cost
    except Exception:
        _log.exception("financials failed")
    _mark("financials")

    # ── Admin analytics: 12 stat cards + 5 weekly/pie charts ──────────────────
    # Only computed for admin (the section is admin-gated in the template) —
    # skips the extra query load for every other role's dashboard render.
    admin_analytics: dict = {}
    admin_charts: dict = {}
    # Computed for every role that can reach this page. The dashboard is gated
    # by the permission matrix, and a role with the page enabled is meant to see
    # the same figures an admin does — previously these queries ran only for
    # UserRole.admin, so everyone else got an empty analytics section.
    #
    # 24+ sequential queries live in this block — previously uncached, so it
    # ran fresh on every single dashboard load (and for every role, per the
    # note above), dominating page-load time. Cached on the same 30s/_now
    # cycle as stage/category/pipeline above, since it's gated by the same
    # Entity/Device Type/Location filters (_dash_filters_active).
    # Own timestamp — same stale-forever issue as the pipeline cache above.
    if (not _dash_filters_active and not _pl_active and _AGG_CACHE.get("admin_analytics") is not None
            and (_now - _AGG_CACHE.get("analytics_ts", 0.0)) < _AGG_TTL):
        admin_analytics, admin_charts = _AGG_CACHE["admin_analytics"]
    else:
        try:
            def _week_key(dt):
                # ISO 8601 week (isocalendar()) already runs Monday-Sunday, so
                # this is the grouping key — see _week_range_label below for the
                # human-readable Monday-Sunday range shown on the chart axis.
                if not dt:
                    return None
                iso = dt.isocalendar()
                return f"{iso[0]}-W{iso[1]:02d}"

            def _week_range_label(dt):
                from datetime import timedelta
                iso_weekday = dt.isocalendar()[2]
                monday = dt - timedelta(days=iso_weekday - 1)
                sunday = monday + timedelta(days=6)
                if monday.month == sunday.month:
                    return f"{monday.day}-{sunday.day} {monday.strftime('%b')}"
                return f"{monday.strftime('%d %b')} - {sunday.strftime('%d %b')}"

            def _last_n_week_keys(n=8):
                from datetime import timedelta
                keys = []
                labels = []
                d = today
                seen = set()
                while len(seen) < n:
                    dd = datetime(d.year, d.month, d.day)
                    k = _week_key(dd)
                    if k not in seen:
                        seen.add(k)
                        keys.append(k)
                        labels.append(_week_range_label(dd))
                    d -= timedelta(days=7)
                return list(reversed(keys)), list(reversed(labels))

            week_keys, week_labels = _last_n_week_keys(8)

            # Every weekly-chart query below only ever buckets into these 8 weeks
            # (_weekly_series discards anything outside week_keys) — cutting each
            # query off at 9 weeks back (1 week of safety margin) avoids pulling
            # a table's entire history into Python just to plot 8 bars.
            from datetime import timedelta as _td
            _weekly_cutoff = datetime(today.year, today.month, today.day) - _td(days=63)

            def _weekly_series(rows, date_getter, value_getter=lambda r: 1):
                buckets = {wk: 0 for wk in week_keys}
                for r in rows:
                    wk = _week_key(date_getter(r))
                    if wk in buckets:
                        buckets[wk] += value_getter(r)
                return [buckets[wk] for wk in week_keys]

            # ── Charts ─────────────────────────────────────────────────────────
            admin_charts["week_labels"] = week_labels

            # a. Weekly: Products in IQC / GRN / In Stock (via StageMovement into that stage)
            sm_rows = (await db.execute(
                select(StageMovement.to_stage, StageMovement.moved_at)
                .where(StageMovement.to_stage.in_([DeviceStage.iqc, DeviceStage.grn, DeviceStage.stock_in]),
                       StageMovement.moved_at >= _weekly_cutoff)
            )).all()
            admin_charts["products_iqc_weekly"] = _weekly_series([r for r in sm_rows if r[0] == DeviceStage.iqc], lambda r: r[1])
            admin_charts["products_grn_weekly"] = _weekly_series([r for r in sm_rows if r[0] == DeviceStage.grn], lambda r: r[1])
            admin_charts["products_stock_weekly"] = _weekly_series([r for r in sm_rows if r[0] == DeviceStage.stock_in], lambda r: r[1])

            # a2. Weekly: Spare Parts in Sourcing Request / GRN / Harvest
            psr_rows = (await db.execute(
                select(PartSourcingRequest.created_at).where(PartSourcingRequest.created_at >= _weekly_cutoff)
            )).scalars().all()
            admin_charts["parts_sourcing_weekly"] = _weekly_series(psr_rows, lambda r: r)
            grn_li_rows = (await db.execute(
                select(PartsGRNLineItem.is_harvest, PartsGRNLineItem.created_at)
                .where(PartsGRNLineItem.created_at >= _weekly_cutoff)
            )).all()
            admin_charts["parts_grn_weekly"] = _weekly_series([r for r in grn_li_rows if not r[0]], lambda r: r[1])
            admin_charts["parts_harvest_weekly"] = _weekly_series([r for r in grn_li_rows if r[0]], lambda r: r[1])

            # b. Pie: stage distribution — sourced from pipeline_counts (the
            # same grouped counts the Stage Pipeline strip uses), not raw
            # stage_counts, so L1/L2 and L3/L4 show as the same combined
            # buckets used everywhere else on this page (e.g. the In Repair
            # card), and Cosmetic (8 cosmetic stages combined) is included.
            admin_charts["stage_pie_labels"] = ["IQC", "Stock In", "Production", "L1/L2", "L3/L4", "Stress", "Cosmetic", "Final QC"]
            admin_charts["stage_pie_values"] = [
                pipeline_counts.get("iqc", 0),
                pipeline_counts.get("stock_in", 0),
                pipeline_counts.get("production", 0),
                pipeline_counts.get("l1l2", 0),
                pipeline_counts.get("l3l4", 0),
                pipeline_counts.get("qc_check", 0),
                pipeline_counts.get("cosmetic", 0),
                pipeline_counts.get("final_qc", 0),
            ]

            # c. Weekly: Sales Price — Ready to Sale (moved-in value proxy via count) vs Product Sold (₹)
            rts_rows = (await db.execute(
                select(StageMovement.moved_at).where(StageMovement.to_stage == DeviceStage.ready_to_sale,
                                                      StageMovement.moved_at >= _weekly_cutoff)
            )).scalars().all()
            admin_charts["ready_to_sale_weekly"] = _weekly_series(rts_rows, lambda r: r)
            sold_rows = (await db.execute(
                select(Sale.sold_at, Sale.sale_price).where(Sale.sold_at >= _weekly_cutoff)
            )).all()
            admin_charts["product_sold_price_weekly"] = _weekly_series(sold_rows, lambda r: r[0], lambda r: float(r[1] or 0))

            # e. Weekly: Sourcing Price — Buyer PO (DealerOrder, dealers buying from
            # OxyPC) vs Seller PO (CRMPurchaseOrder, OxyPC buying from suppliers)
            buyer_po_rows = (await db.execute(
                select(DealerOrder.order_date, DealerOrder.total_amount)
                .where(DealerOrder.order_date >= _weekly_cutoff)
            )).all()
            admin_charts["buyer_po_weekly"] = _weekly_series(buyer_po_rows, lambda r: r[0], lambda r: float(r[1] or 0))
            seller_po_rows = (await db.execute(
                select(CRMPurchaseOrder.created_at, CRMPurchaseOrder.total_amount)
                .where(CRMPurchaseOrder.created_at >= _weekly_cutoff)
            )).all()
            admin_charts["seller_po_weekly"] = _weekly_series(seller_po_rows, lambda r: r[0], lambda r: float(r[1] or 0))
        except Exception:
            _log.exception("admin_analytics failed")
        if not _dash_filters_active and not _pl_active:   # range counts are per-request
            _AGG_CACHE["admin_analytics"] = (admin_analytics, admin_charts)
            _AGG_CACHE["analytics_ts"] = _now

    _mark("analytics")
    # ── Apply stage filter to stage_counts display ───────────────────────────
    if stage_filter:
        filtered_stage_counts = {stage_filter: stage_counts.get(stage_filter, 0)}
    else:
        filtered_stage_counts = stage_counts

    # (lot_pl is already scoped to P&L From/To above, by each device's own
    # stage-completed date — see completed_device_ids near the top.)

    # (The Location Gaps widget was removed from the dashboard on 2026-10-15
    # — its device-id scan of every location log ran on every load. The full
    # list stays on /locations/gaps.)
    # ── My Work Queue — actual devices in the user's active stages ───────────
    ROLE_STAGE_MAP = {
        "l1_engineer":       [DeviceStage.l1],
        "l2_engineer":       [DeviceStage.l2],
        "l3_engineer":       [DeviceStage.l3],
        "qc_inspector":      [DeviceStage.qc_check, DeviceStage.final_qc],
        "inventory_manager": [DeviceStage.grn, DeviceStage.iqc, DeviceStage.stock_in],
        "sales":             [DeviceStage.ready_to_sale],
        "sales_manager":     [DeviceStage.ready_to_sale, DeviceStage.sold],
    }
    role_val = current_user.role.value if current_user.role else ""
    wq_stages = ROLE_STAGE_MAP.get(role_val, [])
    # Admin sees every stage, so no stage filter at all. It used to send the
    # full DeviceStage list, and a stage value the database type doesn't have
    # yet (on_rent on production, 2026-10-22) failed the query.
    is_admin_wq = bool(current_user.role and current_user.role.value == "admin")

    work_queue_devices = []
    if wq_stages or is_admin_wq:
        q_wq = select(Device).where(Device.is_trashed == False)
        if not is_admin_wq:
            q_wq = q_wq.where(Device.current_stage.in_(wq_stages))
        try:
            # Savepoint: a failure here must not abort the page's transaction,
            # or every query after it fails and the dashboard returns 500.
            async with db.begin_nested():
                wq_result = await db.execute(
                    q_wq.order_by(Device.updated_at.asc()).limit(15)
                )
                work_queue_devices = wq_result.scalars().all()
        except Exception:
            _log.exception("work_queue_devices failed for role=%s", role_val)

    # ── Today's follow-ups (dealer calls + CRM activities due today) ──────────
    try:
        dealer_followup_count = (await db.execute(
            select(func.count(DealerCall.id))
            .where(
                DealerCall.next_followup_date.isnot(None),
                func.date(DealerCall.next_followup_date) <= today,
            )
        )).scalar() or 0
        crm_followup_count = (await db.execute(
            select(func.count(CRMActivity.id))
            .where(
                CRMActivity.next_followup.isnot(None),
                CRMActivity.followup_done == False,
                func.date(CRMActivity.next_followup) <= today,
            )
        )).scalar() or 0
        today_followups = dealer_followup_count + crm_followup_count
    except Exception:
        today_followups = 0

    # The "Pending for IQC — by Lot" card was removed on request, and its
    # grouped query with it — this dashboard already issues a lot of queries,
    # so there is no reason to keep computing a breakdown nothing renders.

    # Revenue / Profit tiles: This Month by default; with a date range set,
    # sales dated in the range (same Gross Profit definition as Business P&L).
    tile_revenue, tile_profit, tile_period = month_revenue, month_profit, "This Month"
    if _pl_active:
        try:
            from services.business_pl import compute_range_revenue_profit
            tile_revenue, tile_profit = await compute_range_revenue_profit(
                db, _pl_from_dt, _pl_to_dt, device_where=[*_ent, *_dtype, *_loc])
            _d = lambda dt: dt.strftime("%d %b") if dt else "…"
            tile_period = f"{_d(_pl_from_dt)} – {_d(_pl_to_dt)}"     # "01 Oct – 22 Oct"
            if _pl_from_dt and _pl_to_dt and _pl_from_dt.year != _pl_to_dt.year:
                tile_period = f"{_pl_from_dt:%d %b %y} – {_pl_to_dt:%d %b %y}"
        except Exception:
            _log.exception("range revenue/profit failed")
            tile_revenue, tile_profit, tile_period = month_revenue, month_profit, "This Month"

    _year_choices = await report_year_values(db)
    _mark("work queue+follow-ups")
    _resp = templates.TemplateResponse("dashboard.html", {
        "request": request,
        "current_user": current_user,
        "now": app_now(),
        "work_queue_devices": work_queue_devices,
        "stage_counts": filtered_stage_counts,
        "pipeline_counts": pipeline_counts,
        "pipeline_by_entity": pipeline_by_entity,
        "pipeline_by_location": pipeline_by_location,
        "entity_choices": entity_choices,
        "f_entity": entity,
        "device_type_choices": device_type_choices,
        "f_device_type": device_type,
        "storage_locations": storage_locations,
        "zone_choices": list(ZoneType),
        "zone_labels": ZONE_LABELS,
        "f_zone": zone,
        "stage_filter": stage_filter,
        "pl_from": pl_from,
        "pl_to": pl_to,
        "all_stages": DROPDOWN_STAGES,
        "stage_labels": STAGE_LABELS,
        "category_counts": category_counts,
        "total_devices": total_devices,
        "in_repair_count": in_repair_count,
        "laptops_available": laptops_available,
        "desktops_available": desktops_available,
        "tft_available": tft_available,
        "tablets_available": tablets_available,
        "minipc_available": minipc_available,
        "server_available": server_available,
        "user_queue": user_queue,
        "chart_data": chart_data,
        "chart_labels": chart_labels,
        "lot_pl": lot_pl,
        "pl_active": _pl_active,
        # Returned / Scrapped / Scrap for Sale / On Rent row: moved-in-range
        # counts when dates are set, else the live (stage-filtered) counts.
        "side_counts": side_counts if side_counts is not None else filtered_stage_counts,
        "tile_revenue": tile_revenue,
        "tile_profit": tile_profit,
        "tile_period": tile_period,
        "month_revenue": month_revenue,
        "total_revenue": total_revenue,
        "total_investment": total_investment,
        "yearly_device_cost": yearly_device_cost,
        # Financial Summary card: FY Revenue and Net Profit = Revenue − Device
        # − Parts − Labour, i.e. Business P&L's Gross Profit for the same FY.
        "fy_revenue": fy_revenue,
        "month_profit": month_profit,
        "fy_net_profit": fy_revenue - yearly_device_cost - yearly_parts_cost - yearly_labour_cost,
        "total_parts_cost": total_parts_cost,
        "total_labour_cost": total_labour_cost,
        "total_cosmetic_cost": total_cosmetic_cost,
        "overall_profit": overall_profit,
        "fy": fy,
        # Financial Years offered: one per configured report year (2025 ->
        # "2025-26"), plus the current FY, newest first.
        "fy_choices": [(y, fy_label(y)) for y in sorted(
            {int(v) for v in _year_choices if str(v).isdigit()}
            | {today.year if today.month >= 4 else today.year - 1, fy}, reverse=True)],
        "yearly_parts_cost": yearly_parts_cost,
        "yearly_labour_cost": yearly_labour_cost,
        "admin_analytics": admin_analytics,
        "admin_charts": admin_charts,
        "today": today,
        "today_followups": today_followups,
    })
    # Slow-load breakdown (2026-10-15): one console line per dashboard load
    # that takes over 1 s, so the slow section can be found from real data.
    _mark("render")
    _total = _time.perf_counter() - _t0
    if _total > 1.0:
        _prev, _parts = _t0, []
        for _name, _t in _marks:
            _parts.append(f"{_name} {(_t - _prev) * 1000:.0f}ms")
            _prev = _t
        _log.warning("Dashboard slow load %.1fs (%s): %s", _total,
                     "filtered" if _dash_filters_active else "default", " | ".join(_parts))
    return _resp


# Pre-compile the dashboard template (and the base layout it extends) when
# the server starts (2026-10-16). Jinja compiles a template on its first use,
# which cost ~250 ms on the first dashboard visit after every restart — and
# the dashboard is the landing page, so that first visitor was usually a
# manager. A failure here is harmless: the template just compiles on first use.
try:
    templates.env.get_template("dashboard.html")
except Exception:  # pragma: no cover
    pass
