"""GRN "Stocked" count (Pankaj, 2026-10-21).

Stocked = tags mapped to the GRN (devices.grn_number — "Map this GRN", IQC
upload, ReGRN). The tag's invoice number does not matter. (Earlier the same
day the count also took unmapped tags carrying the GRN's invoice number, which
showed GRN 68 = 237 instead of its 9 mapped tags; requiring GRN AND invoice
dropped GRNs whose mapped tags carry another invoice to 0.)

The count is ADD-ONLY: every tag counted is remembered in grn_stocked_tags, so
a tag stays counted under a GRN after it moves on — ReGRN to another GRN,
cleared GRN, edits. Before any code changes a tag's grn_number it calls
remember_safely(). One count per Tag Number per GRN (barcode_key is the
upper-cased Tag Number). Trashed / deactivated tags are not counted; a
restored tag counts again.

    rebuild_for_rule_once(db)           one-time: clear rows from the earlier
                                        rules, so the table is refilled.
    remember_tags(db, device_ids=None)  add rows for tags' CURRENT GRN.
    remember_safely(db, device_ids)     same, in a savepoint — call it before
                                        a tag's GRN number is overwritten.
    recover_from_audit_once(db)         one-time: tags already moved off a GRN
                                        (old grn_number kept in the audit log,
                                        e.g. REGRN_TAG) are added to it.
    stocked_counts(db, grns)            {grn_number: count} for the page.
"""
import json
import uuid

from sqlalchemy import bindparam, select, text

from models.engines import AuditLog
from models.settings import AppSetting
from services.audit_engine import audit
from utils.timezone import app_now

AUDIT_RECOVERY_MARKER = "grn_stocked_regrn_recovery_2026_10_21"
RULE_MARKER = "grn_stocked_rule_grn_mapped_2026_10_21"
_OLD_MARKERS = ("grn_stocked_audit_recovery_2026_10_21",
                "grn_stocked_rule_grn_and_invoice_2026_10_21")

# Random uuid without needing pgcrypto / PG13's gen_random_uuid().
_NEW_ID = "md5(random()::text || clock_timestamp()::text || d.id::text)::uuid"
_LIVE_TAG = "d.is_active IS TRUE AND d.is_trashed IS NOT TRUE"
_LIVE_GRN = "g.is_deleted IS NOT TRUE"

_SQL_MATCH = f"""
    INSERT INTO grn_stocked_tags (id, grn_id, device_id, barcode_key, matched_invoice, source, created_at)
    SELECT {_NEW_ID}, g.id, d.id, upper(d.barcode), g.invoice_number, 'grn_mapped', :now
    FROM devices d JOIN grn_imports g ON g.grn_number = d.grn_number
    WHERE {_LIVE_GRN} AND {_LIVE_TAG} {{only}}
    ON CONFLICT (grn_id, barcode_key) DO NOTHING
"""


async def remember_tags(db, device_ids=None) -> int:
    """Add (never remove) Stocked rows for tags' current GRN number.
    device_ids=None -> every tag. Caller commits. Returns rows added."""
    added = 0
    params = {"now": app_now()}
    only = ""
    if device_ids is not None:
        ids = [i for i in device_ids if i is not None]
        if not ids:
            return 0
        only = "AND d.id IN :ids"
        params["ids"] = ids
    stmt = text(_SQL_MATCH.format(only=only))
    if only:
        stmt = stmt.bindparams(bindparam("ids", expanding=True))
    added += (await db.execute(stmt, params)).rowcount or 0
    return added


async def remember_safely(db, device_ids) -> None:
    """remember_tags in a savepoint: call right before code overwrites a
    tag's grn_number (or invoice). A failure here never blocks that change."""
    try:
        async with db.begin_nested():
            await remember_tags(db, list(device_ids))
    except Exception as e:  # noqa: BLE001
        print(f"  [GRNStocked] could not remember tags: {e}")


async def rebuild_for_rule_once(db) -> int | None:
    """One-time (2026-10-21): clear rows counted under the earlier rules so
    the table is refilled with mapped tags only. Only this derived count
    table is touched — no tag, GRN or sale data. Resets the recovery marker
    so the audit-log recovery re-runs under this rule.
    Returns rows cleared, or None when already done. Commits."""
    if (await db.execute(select(AppSetting).where(AppSetting.key == RULE_MARKER))).scalar_one_or_none():
        return None
    cleared = (await db.execute(text("DELETE FROM grn_stocked_tags"))).rowcount or 0
    await db.execute(text("DELETE FROM app_settings WHERE key IN :ks").bindparams(
        bindparam("ks", expanding=True)), {"ks": [AUDIT_RECOVERY_MARKER, *_OLD_MARKERS]})
    await audit(db, action="GRN_STOCKED_RULE_REBUILD", user=None, table_name="grn_stocked_tags",
                record_id="rule-2026-10-21",
                new_value={"rows_cleared": cleared, "rule": "tags mapped to the GRN; kept after ReGRN"})
    db.add(AppSetting(key=RULE_MARKER, value=str(cleared), updated_by="system",
                      description="One-time: GRN Stocked recounted as mapped tags (kept after ReGRN)"))
    await db.commit()
    return cleared


async def recover_from_audit_once(db) -> int | None:
    """One-time: tags already moved off a GRN before this feature existed
    (ReGRN, cleared or edited GRN number) are added to that GRN — the old
    grn_number is in the audit log (e.g. REGRN_TAG old_value).
    Returns rows added, or None when already done. Commits."""
    if (await db.execute(select(AppSetting).where(AppSetting.key == AUDIT_RECOVERY_MARKER))).scalar_one_or_none():
        return None
    grn_ids = {n: gid for gid, n in (await db.execute(text(
        "SELECT id, grn_number FROM grn_imports WHERE is_deleted IS NOT TRUE"))).all()}

    pairs, seen = [], set()
    rows = (await db.execute(
        select(AuditLog.record_id, AuditLog.old_value)
        .where(AuditLog.old_value.like('%"grn_number"%'))
    )).all()
    for record_id, old_value in rows:
        try:
            dev_id = uuid.UUID(str(record_id))
            old_grn = ((json.loads(old_value) or {}).get("grn_number") or "").strip()
        except (ValueError, TypeError, AttributeError):
            continue
        gid = grn_ids.get(old_grn)
        if gid and (gid, dev_id) not in seen:
            seen.add((gid, dev_id))
            pairs.append({"g": gid, "dev": dev_id, "now": app_now()})

    added = 0
    stmt = text(f"""
        INSERT INTO grn_stocked_tags (id, grn_id, device_id, barcode_key, matched_invoice, source, created_at)
        SELECT {_NEW_ID}, g.id, d.id, upper(d.barcode), g.invoice_number, 'audit_recovery', :now
        FROM devices d JOIN grn_imports g ON g.id = :g
        WHERE d.id = :dev AND {_LIVE_TAG}
        ON CONFLICT (grn_id, barcode_key) DO NOTHING
    """)
    for p in pairs:
        added += (await db.execute(stmt, p)).rowcount or 0
    await audit(db, action="GRN_STOCKED_AUDIT_RECOVERY", user=None, table_name="grn_stocked_tags",
                record_id="backfill-2026-10-21",
                new_value={"tags_added": added, "audit_rows_checked": len(rows),
                           "grn_tag_pairs_found": len(pairs), "from": "old grn_number (ReGRN etc.)"})
    db.add(AppSetting(key=AUDIT_RECOVERY_MARKER, value=str(added), updated_by="system",
                      description="One-time: GRN Stocked rows for tags already moved off a GRN (audit log)"))
    await db.commit()
    return added


async def stocked_counts(db, grns) -> dict:
    """{grn_number: live tags counted} — remembers current matches first."""
    by_id = {g.id: g.grn_number for g in grns if g.grn_number}
    if not by_id:
        return {}
    await remember_tags(db)
    await db.commit()
    stmt = text(f"""
        SELECT s.grn_id, count(*) FROM grn_stocked_tags s JOIN devices d ON d.id = s.device_id
        WHERE s.grn_id IN :ids AND {_LIVE_TAG}
        GROUP BY s.grn_id
    """).bindparams(bindparam("ids", expanding=True))
    return {by_id[gid]: c for gid, c in (await db.execute(stmt, {"ids": list(by_id)})).all()}


async def stocked_device_ids(db, grn_id) -> list:
    """Device ids counted as Stocked for one GRN (live tags only) — the tags
    behind the Stocked number, used by GRN Detail and the Stocked popup."""
    await remember_tags(db)
    rows = (await db.execute(text(f"""
        SELECT d.id FROM grn_stocked_tags s JOIN devices d ON d.id = s.device_id
        WHERE s.grn_id = :gid AND {_LIVE_TAG}
    """), {"gid": grn_id})).all()
    return [r[0] for r in rows]
