"""GRN "Stocked" count (Pankaj, 2026-10-21).

Stocked = tags mapped to the GRN ("Map this GRN" / IQC upload set
devices.grn_number), plus tags with NO GRN of their own whose Invoice Number
matches the GRN's (case and surrounding spaces ignored). The count is ADD-ONLY: every tag counted
is remembered in grn_stocked_tags, so it stays counted after its invoice
changes — e.g. when a sale replaces the purchase invoice with the sale
invoice. A Tag Number counts once per GRN even if duplicate records exist
(barcode_key is the upper-cased Tag Number). Trashed / deactivated tags are
not counted (they are no longer stock); a restored tag counts again.

    remember_tags(db, device_ids=None)  add rows from tags' CURRENT invoice /
                                        GRN; call it before a tag's invoice is
                                        overwritten, and before counting.
    recover_from_audit_once(db)         one-time: tags whose purchase invoice
                                        was already replaced (old value kept
                                        in the audit log) are added too.
    stocked_counts(db, grns)            {grn_number: count} for the page.
"""
import json
import uuid

from sqlalchemy import bindparam, select, text

from models.engines import AuditLog
from models.settings import AppSetting
from services.audit_engine import audit
from utils.timezone import app_now

AUDIT_RECOVERY_MARKER = "grn_stocked_audit_recovery_2026_10_21"

# Random uuid without needing pgcrypto / PG13's gen_random_uuid().
_NEW_ID = "md5(random()::text || clock_timestamp()::text || d.id::text)::uuid"
_LIVE_TAG = "d.is_active IS TRUE AND d.is_trashed IS NOT TRUE"
_LIVE_GRN = "g.is_deleted IS NOT TRUE"
# A tag that belongs to a GRN (devices.grn_number of a live GRN) counts only
# under THAT GRN — invoice matching is for tags with no GRN of their own.
# Without this, two GRNs sharing one invoice number (older data) each counted
# the other's tags: GRN 58 showed 2,059 against a quantity of 137.
_NOT_OTHER_GRN = """NOT EXISTS (SELECT 1 FROM grn_imports g2 WHERE g2.grn_number = d.grn_number
                               AND g2.is_deleted IS NOT TRUE AND g2.id <> g.id)"""

_SQL_MAPPED = f"""
    INSERT INTO grn_stocked_tags (id, grn_id, device_id, barcode_key, matched_invoice, source, created_at)
    SELECT {_NEW_ID}, g.id, d.id, upper(d.barcode), g.invoice_number, 'grn_mapped', :now
    FROM devices d JOIN grn_imports g ON g.grn_number = d.grn_number
    WHERE {_LIVE_GRN} AND {_LIVE_TAG} {{only}}
    ON CONFLICT (grn_id, barcode_key) DO NOTHING
"""
_SQL_INVOICE = f"""
    INSERT INTO grn_stocked_tags (id, grn_id, device_id, barcode_key, matched_invoice, source, created_at)
    SELECT {_NEW_ID}, g.id, d.id, upper(d.barcode), g.invoice_number, 'invoice_match', :now
    FROM devices d JOIN grn_imports g
      ON upper(btrim(d.invoice_number)) = upper(btrim(g.invoice_number))
    WHERE btrim(coalesce(g.invoice_number, '')) <> '' AND {_LIVE_GRN} AND {_LIVE_TAG}
      AND {_NOT_OTHER_GRN} {{only}}
    ON CONFLICT (grn_id, barcode_key) DO NOTHING
"""


def _norm(v) -> str:
    return (v or "").strip().upper()


async def remember_tags(db, device_ids=None) -> int:
    """Add (never remove) Stocked rows from tags' current invoice / GRN number.
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
    for sql in (_SQL_MAPPED, _SQL_INVOICE):
        stmt = text(sql.format(only=only))
        if only:
            stmt = stmt.bindparams(bindparam("ids", expanding=True))
        added += (await db.execute(stmt, params)).rowcount or 0
    return added


async def recover_from_audit_once(db) -> int | None:
    """One-time: tags whose invoice was replaced before this feature existed
    (sale invoice, replacement, backfills, edits) are added under the GRN
    their OLD invoice belongs to — the old value is in the audit log.
    Returns rows added, or None when already done. Commits."""
    if (await db.execute(select(AppSetting).where(AppSetting.key == AUDIT_RECOVERY_MARKER))).scalar_one_or_none():
        return None
    grn_by_inv: dict = {}
    for gid, inv in (await db.execute(text(
            "SELECT id, invoice_number FROM grn_imports "
            "WHERE is_deleted IS NOT TRUE AND btrim(coalesce(invoice_number, '')) <> ''"))).all():
        grn_by_inv.setdefault(_norm(inv), []).append((gid, inv))

    pairs, seen = [], set()
    rows = (await db.execute(
        select(AuditLog.record_id, AuditLog.old_value)
        .where(AuditLog.old_value.like('%"invoice_number"%'))
    )).all()
    for record_id, old_value in rows:
        try:
            dev_id = uuid.UUID(str(record_id))
            old_inv = (json.loads(old_value) or {}).get("invoice_number")
        except (ValueError, TypeError, AttributeError):
            continue
        for gid, inv in grn_by_inv.get(_norm(old_inv), []):
            if (gid, dev_id) not in seen:
                seen.add((gid, dev_id))
                pairs.append({"g": gid, "dev": dev_id, "inv": inv, "now": app_now()})

    added = 0
    stmt = text(f"""
        INSERT INTO grn_stocked_tags (id, grn_id, device_id, barcode_key, matched_invoice, source, created_at)
        SELECT {_NEW_ID}, g.id, d.id, upper(d.barcode), :inv, 'audit_recovery', :now
        FROM devices d JOIN grn_imports g ON g.id = :g
        WHERE d.id = :dev AND {_LIVE_TAG} AND {_NOT_OTHER_GRN}
        ON CONFLICT (grn_id, barcode_key) DO NOTHING
    """)
    for i in range(0, len(pairs), 2000):
        for p in pairs[i:i + 2000]:
            added += (await db.execute(stmt, p)).rowcount or 0
    await audit(db, action="GRN_STOCKED_AUDIT_RECOVERY", user=None, table_name="grn_stocked_tags",
                record_id="backfill-2026-10-21",
                new_value={"tags_added": added, "audit_rows_checked": len(rows),
                           "grn_tag_pairs_found": len(pairs)})
    db.add(AppSetting(key=AUDIT_RECOVERY_MARKER, value=str(added), updated_by="system",
                      description="One-time: GRN Stocked rows recovered from old invoices in the audit log"))
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
