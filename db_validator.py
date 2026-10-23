"""
OxyPC Database Schema Validator
================================
Validates that the live PostgreSQL schema matches the SQLAlchemy ORM models,
and auto-fixes any gaps it can resolve safely.

Called from main.py startup_event() BEFORE the server starts serving requests.
This turns silent 500s into loud startup failures with clear fix instructions.

What it checks:
  1. Every table defined in Base.metadata exists in the DB
  2. Every column in every ORM model exists in the DB table
  3. stage_master contains all DeviceStage enum values
  4. allowed_transitions is not empty

What it auto-fixes:
  1. Missing tables       → Base.metadata.create_all (checkfirst=True)
  2. Missing columns      → ALTER TABLE ... ADD COLUMN IF NOT EXISTS
  3. Missing/wrong stages → INSERT ON CONFLICT DO UPDATE + full transition seed

Usage:
    from db_validator import validate_and_fix
    await validate_and_fix(engine)   # raises RuntimeError if unfixable

Run standalone (diagnose without fixing):
    python db_validator.py
"""

from __future__ import annotations

import asyncio
import sys
import os
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import text, inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncConnection

if TYPE_CHECKING:
    pass


# ── Stage data ────────────────────────────────────────────────────────────────
# Single source of truth — matches DeviceStage enum in models/device.py
# and DEFAULT_TRANSITIONS in models/stage_control.py

CANONICAL_STAGES = [
    ("grn",           "GRN Receipt",        0),
    ("iqc",           "IQC Inspection",      1),
    ("stock_in",      "Stock In",            2),
    ("trc_production","TRC Production",      2),
    ("l1",            "L1 Repair",           3),
    ("l2",            "L2 Repair",           4),
    ("l3",            "L3 Repair",           5),
    ("qc_check",      "QC Check",            6),
    ("cosmetic_received", "Cosmetic Received", 7),
    ("cleaning",      "Cleaning",            8),
    ("dry_sanding",   "Dry Sanding",         9),
    ("masking",       "Masking",            10),
    ("painting",      "Painting",           11),
    ("water_sanding", "Water Sanding",      12),
    ("cosmetic_completed", "Cosmetic Completed", 13),
    ("final_qc",      "Final QC",           14),
    ("ready_to_sale", "Ready to Sale",      15),
    ("sold",          "Sold",               16),
    ("returned",      "Returned",           17),
    ("on_rent",       "On Rent",            18),   # Rentals (2026-10-22)
    ("scrapped",      "Scrapped",           99),
]

CANONICAL_TRANSITIONS = [
    # IQC
    ("iqc",           "stock_in"),
    ("iqc",           "l1"),
    ("iqc",           "scrapped"),
    # Stock In
    ("stock_in",      "l1"),
    ("stock_in",      "qc_check"),
    ("stock_in",      "scrapped"),
    ("stock_in",      "trc_production"),
    # Repair escalation
    ("l1",            "l2"),
    ("l1",            "qc_check"),
    ("l1",            "scrapped"),
    ("l1",            "stock_in"),
    ("l2",            "l3"),
    ("l2",            "qc_check"),
    ("l2",            "scrapped"),
    ("l2",            "stock_in"),
    ("l3",            "qc_check"),
    ("l3",            "scrapped"),
    ("l3",            "stock_in"),
    # QC
    ("qc_check",      "cosmetic_received"),
    ("qc_check",      "ready_to_sale"),
    ("qc_check",      "l1"),
    ("qc_check",      "l2"),
    ("qc_check",      "l3"),
    ("qc_check",      "scrapped"),
    ("qc_check",      "stock_in"),
    # Cosmetic pipeline
    ("cosmetic_received", "cleaning"),
    ("cosmetic_received", "final_qc"),
    ("cosmetic_received", "l1"),
    ("cosmetic_received", "stock_in"),
    ("cleaning",      "dry_sanding"),
    ("cleaning",      "final_qc"),
    ("cleaning",      "l1"),
    ("cleaning",      "stock_in"),
    ("dry_sanding",   "masking"),
    ("dry_sanding",   "stock_in"),
    ("masking",       "painting"),
    ("masking",       "stock_in"),
    ("painting",      "water_sanding"),
    ("painting",      "stock_in"),
    ("water_sanding", "cosmetic_completed"),
    ("water_sanding", "l1"),
    ("water_sanding", "stock_in"),
    ("cosmetic_completed", "final_qc"),
    ("cosmetic_completed", "l1"),
    ("cosmetic_completed", "stock_in"),
    ("final_qc",      "ready_to_sale"),
    ("final_qc",      "cleaning"),
    ("final_qc",      "scrapped"),
    ("final_qc",      "stock_in"),
    # Sales
    ("ready_to_sale", "sold"),
    # Rentals (2026-10-22): out on rent and back to Ready to Sale on return / cancel
    ("ready_to_sale", "on_rent"),
    ("on_rent",       "ready_to_sale"),
    ("sold",          "returned"),
    ("returned",      "iqc"),
    ("returned",      "scrapped"),
    ("returned",      "stock_in"),
    # Corrective moves — Customise modal can send any in-pipeline device
    # straight back to Stock In (e.g. mis-scrapped, mis-graded)
    ("scrapped",      "stock_in"),
]

CANONICAL_STAGE_NAMES = {name for name, _, _ in CANONICAL_STAGES}


# ── Column type map (ORM → SQL for ADD COLUMN) ────────────────────────────────

def _sql_type(col) -> str:
    """Convert SQLAlchemy column type to a safe ADD COLUMN SQL type string."""
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID
    t = col.type
    # UUID first. Falling through to the TEXT default below produced a text
    # column holding uuid-shaped strings, so every `Model.fk_id == some_uuid`
    # comparison had to cast and any real FK constraint was impossible. That is
    # the drift migrate_fix_prod_uuid_drift.py had to repair in production;
    # emitting the right type here stops it recurring on the next UUID column.
    if isinstance(t, PG_UUID) or getattr(t, "__visit_name__", "") == "uuid":
        return "UUID"
    # Enum must be tested before String: sa.Enum subclasses sa.String (same
    # pitfall as Text below), so the String branch would otherwise silently
    # claim every Enum column too — producing VARCHAR(<n>) instead of the
    # real Postgres enum type, which then fails at query time with
    # "operator does not exist: character varying = <enumtype>" the first
    # time that column is ever filtered on (ADD COLUMN itself succeeds, so
    # this was invisible until the column was actually queried against).
    if isinstance(t, sa.Enum):
        return t.name
    # Text must be tested before String: sa.Text subclasses sa.String, so the String
    # branch would otherwise claim every Text column and, finding no .length, cap it
    # at VARCHAR(255) — silently truncating long free-text on insert.
    if isinstance(t, sa.Text):
        return "TEXT"
    if isinstance(t, sa.String):
        length = t.length or 255
        return f"VARCHAR({length})"
    if isinstance(t, sa.Integer):
        return "INTEGER"
    if isinstance(t, sa.Boolean):
        return "BOOLEAN"
    if isinstance(t, sa.Numeric):
        return f"NUMERIC({t.precision or 12},{t.scale or 2})"
    if isinstance(t, sa.DateTime):
        return "TIMESTAMP"
    if isinstance(t, sa.Date):
        return "DATE"
    # Enum and everything else — use TEXT as safe default
    return "TEXT"


def _column_default_clause(col) -> str:
    """Build a ' DEFAULT ...' SQL fragment from the column's server_default.

    Without this, ADD COLUMN left every pre-existing row NULL even when the
    ORM model declared a default — e.g. dealers.portal_enabled ended up NULL
    on 472 rows instead of false, silently excluding them from any query
    filtering on `== False` (they don't match NULL either)."""
    sd = getattr(col, "server_default", None)
    if sd is None:
        return ""
    arg = getattr(sd, "arg", sd)
    try:
        val = str(arg.compile(compile_kwargs={"literal_binds": True}))
    except Exception:
        val = str(arg)
    return f" DEFAULT {val}"


# ── Core validator ────────────────────────────────────────────────────────────

class SchemaValidator:
    """Validates and auto-repairs the DB schema."""

    def __init__(self, conn: AsyncConnection):
        self.conn   = conn
        self.issues: list[str] = []
        self.fixed:  list[str] = []
        self.failed: list[str] = []

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _db_tables(self) -> set[str]:
        r = await self.conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ))
        return {row[0] for row in r.fetchall()}

    async def _db_columns(self, table: str) -> set[str]:
        r = await self.conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ), {"t": table})
        return {row[0] for row in r.fetchall()}

    # ── check 1: missing tables ───────────────────────────────────────────────

    async def check_tables(self, base_metadata) -> None:
        db_tables = await self._db_tables()
        for tbl_name in base_metadata.tables:
            if tbl_name not in db_tables:
                self.issues.append(f"TABLE MISSING: {tbl_name}")

    # ── fix 1: create missing tables ─────────────────────────────────────────

    async def fix_missing_tables(self, engine: AsyncEngine) -> None:
        from database import Base
        import models  # noqa — ensure all models are registered

        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            self.fixed.append("Created all missing tables via Base.metadata.create_all")
        except Exception as e:
            self.failed.append(f"Could not create missing tables: {e}")

    # ── check 2: missing columns ──────────────────────────────────────────────

    async def check_columns(self, base_metadata) -> list[tuple]:
        """Returns list of (table_name, column_name, sql_type) to add."""
        missing = []
        db_tables = await self._db_tables()

        for tbl_name, tbl in base_metadata.tables.items():
            if tbl_name not in db_tables:
                continue   # table itself is missing — handled by check_tables
            db_cols = await self._db_columns(tbl_name)
            for col in tbl.columns:
                if col.name not in db_cols:
                    sql_t = _sql_type(col)
                    default_clause = _column_default_clause(col)
                    # NOT NULL is only safe to add together with a DEFAULT —
                    # Postgres backfills existing rows from the default before
                    # enforcing NOT NULL, so this never fails on a non-empty table.
                    not_null = bool(default_clause) and not col.nullable
                    missing.append((tbl_name, col.name, sql_t, default_clause, not_null))
                    self.issues.append(
                        f"COLUMN MISSING: {tbl_name}.{col.name} ({sql_t}{default_clause})"
                    )
        return missing

    # ── fix 2: add missing columns ────────────────────────────────────────────

    async def fix_missing_columns(self, missing: list[tuple]) -> None:
        for tbl, col, sql_t, default_clause, not_null in missing:
            try:
                null_clause = " NOT NULL" if not_null else ""
                await self.conn.execute(text(
                    f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {sql_t}{default_clause}{null_clause}"
                ))
                self.fixed.append(f"Added column {tbl}.{col} ({sql_t}{default_clause}{null_clause})")
            except Exception as e:
                self.failed.append(f"Could not add {tbl}.{col}: {e}")

    # ── check 2b: VARCHAR columns narrower in the DB than in the model ────────

    async def check_column_widths(self, base_metadata) -> list[tuple]:
        """Return (table, column, current_len, wanted_len) for every VARCHAR the
        model has widened since the table was created.

        ADD COLUMN only covers columns that do not exist yet; a column whose
        length grew in the model stays narrow in the database forever, and the
        overflow only surfaces at INSERT time as a StringDataRightTruncation —
        which, on a bulk import, aborts the whole commit rather than one row.

        Widening is non-destructive and rewrites no data. Narrowing is never
        emitted: if the DB column is wider than the model, it is left alone,
        since shrinking could truncate rows that are already stored.
        """
        widen = []
        db_tables = await self._db_tables()

        for tbl_name, tbl in base_metadata.tables.items():
            if tbl_name not in db_tables:
                continue
            rows = (await self.conn.execute(text(
                "SELECT column_name, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "AND data_type = 'character varying'"
            ), {"t": tbl_name})).all()
            db_len = {name: length for name, length in rows if length is not None}

            for col in tbl.columns:
                want = getattr(col.type, "length", None)
                have = db_len.get(col.name)
                if want and have and want > have:
                    widen.append((tbl_name, col.name, have, want))
                    self.issues.append(
                        f"COLUMN TOO NARROW: {tbl_name}.{col.name} "
                        f"varchar({have}) -> varchar({want})"
                    )
        return widen

    # ── fix 2b: widen those columns ──────────────────────────────────────────

    async def fix_column_widths(self, widen: list[tuple]) -> None:
        for tbl, col, have, want in widen:
            try:
                await self.conn.execute(text(
                    f"ALTER TABLE {tbl} ALTER COLUMN {col} TYPE VARCHAR({want})"
                ))
                self.fixed.append(f"Widened {tbl}.{col} varchar({have}) -> varchar({want})")
            except Exception as e:
                self.failed.append(f"Could not widen {tbl}.{col}: {e}")

    # ── check 3: stage_master correctness ────────────────────────────────────

    async def check_stages(self) -> None:
        try:
            r = await self.conn.execute(text("SELECT name FROM stage_master"))
            db_stages = {row[0] for row in r.fetchall()}
        except Exception:
            self.issues.append("stage_master table not accessible — skipping stage check")
            return

        missing = CANONICAL_STAGE_NAMES - db_stages
        stale   = db_stages - CANONICAL_STAGE_NAMES

        for name in sorted(missing):
            self.issues.append(f"STAGE MISSING: stage_master.name='{name}'")
        for name in sorted(stale):
            # Old wrong names (e.g. l1_repair) — warn but don't fail
            self.issues.append(f"STAGE STALE (wrong name): stage_master.name='{name}' "
                               f"— not in DeviceStage enum, may break transitions")

    # ── fix 3: re-seed stage_master and allowed_transitions ──────────────────

    async def fix_stages(self) -> None:
        try:
            # Remove stale entries (wrong names that will break FK lookups)
            valid = "', '".join(CANONICAL_STAGE_NAMES)
            await self.conn.execute(text(
                f"DELETE FROM allowed_transitions "
                f"WHERE from_stage NOT IN ('{valid}') OR to_stage NOT IN ('{valid}')"
            ))
            await self.conn.execute(text(
                f"DELETE FROM stage_master WHERE name NOT IN ('{valid}')"
            ))

            # Upsert all canonical stages
            for name, label, seq in CANONICAL_STAGES:
                await self.conn.execute(text("""
                    INSERT INTO stage_master (id, name, label, sequence)
                    VALUES (gen_random_uuid(), :name, :label, :seq)
                    ON CONFLICT (name) DO UPDATE
                      SET label=EXCLUDED.label, sequence=EXCLUDED.sequence
                """), {"name": name, "label": label, "seq": seq})

            # Upsert all canonical transitions
            for from_s, to_s in CANONICAL_TRANSITIONS:
                await self.conn.execute(text("""
                    INSERT INTO allowed_transitions (id, from_stage, to_stage)
                    VALUES (gen_random_uuid(), :f, :t)
                    ON CONFLICT (from_stage, to_stage) DO NOTHING
                """), {"f": from_s, "t": to_s})

            self.fixed.append(
                f"Reseeded stage_master ({len(CANONICAL_STAGES)} stages) "
                f"and allowed_transitions ({len(CANONICAL_TRANSITIONS)} transitions)"
            )

            # ── fix 4: seed cost_config defaults ─────────────────────────────────────
            await self.conn.execute(sa.text("""
                INSERT INTO cost_config (id, key, value, description, updated_by, updated_at)
                VALUES
                  (gen_random_uuid(), 'repair_labour_rate', 150.00,
                   'Labour cost per repair attempt when engineer enters no cost (Rs)',
                   'system', NOW()),
                  (gen_random_uuid(), 'cosmetic_rate', 50.00,
                   'Cosmetic rework cost per device that passed through cleaning stage (Rs)',
                   'system', NOW())
                ON CONFLICT (key) DO NOTHING
            """))

        except Exception as e:
            self.failed.append(f"Could not fix stages: {e}")

    # ── check 4: transitions completeness ────────────────────────────────────

    async def check_transitions(self) -> None:
        try:
            r = await self.conn.execute(
                text("SELECT from_stage, to_stage FROM allowed_transitions")
            )
            db_transitions = {(row[0], row[1]) for row in r.fetchall()}
            canonical = set(CANONICAL_TRANSITIONS)
            missing = canonical - db_transitions
            if missing:
                missing_list = ", ".join(f"{f}→{t}" for f, t in sorted(missing))
                self.issues.append(
                    f"STAGE: allowed_transitions missing {len(missing)} canonical row(s): {missing_list}"
                )
        except Exception:
            self.issues.append("allowed_transitions table not accessible")


# ── Public API ────────────────────────────────────────────────────────────────

# (table, column) pairs whose NOT NULL was removed by an approved schema
# change — applied at startup (and by /admin/system/schema-fix).
RELAX_NOT_NULL = [
    ("returns", "sale_id"),   # 2026-10-22: Credit Note sale created at Payment Invoice Done
    # 2026-10-23: Physical Audit records an unknown Tag Number as Missing
    # (no device, no system stage) — approved ("i approve dbdiagram").
    ("audit_record_items", "device_id"),
    ("audit_record_items", "system_stage"),
]


async def validate_and_fix(engine: AsyncEngine, auto_fix: bool = True) -> dict:
    """
    Run all schema checks, auto-fix what's fixable.
    Returns summary dict.
    Raises RuntimeError if any issue could not be fixed.
    Set auto_fix=False (via OXYPC_AUTO_FIX=0 env var) to check-only without DDL mutations.
    """
    from database import Base
    import models  # noqa

    # ── Phase 0: reconcile Postgres enum TYPES with the Python enums ─────────
    # ADD COLUMN IF NOT EXISTS covers new columns, but a new *value* on an
    # existing enum (e.g. DeviceStage.putty) silently ships in code while the
    # DB type stays stale → InvalidTextRepresentationError 500s in production.
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction, so this phase
    # uses its own AUTOCOMMIT connection before the main transactional pass.
    enum_fixes: list[str] = []
    enum_errors: list[str] = []
    if auto_fix:
        wanted: dict[str, set] = {}
        for table in Base.metadata.tables.values():
            for col in table.columns:
                t = col.type
                if hasattr(t, "enums") and getattr(t, "name", None):
                    wanted.setdefault(t.name, set()).update(t.enums)
        async with engine.connect() as raw:
            ac = await raw.execution_options(isolation_level="AUTOCOMMIT")
            for enum_name, values in sorted(wanted.items()):
                rows = await ac.execute(text(
                    "SELECT e.enumlabel FROM pg_enum e "
                    "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :n"
                ), {"n": enum_name})
                existing = {r[0] for r in rows}
                if not existing:
                    continue  # type doesn't exist yet — create_all will make it complete
                for val in sorted(values - existing):
                    # One refused ALTER TYPE (e.g. "must be owner of type", when
                    # the type belongs to another DB role) must not abort the
                    # whole validation — tables, columns and stage rows below
                    # still get created. Reported in `remaining` instead
                    # (2026-10-22: production lost the Rentals tables this way).
                    try:
                        await ac.execute(text(
                            f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{val}'"))
                        enum_fixes.append(f"Added enum value {enum_name}.'{val}'")
                    except Exception as exc:  # noqa: BLE001
                        enum_errors.append(
                            f"ENUM VALUE NOT ADDED: {enum_name}.'{val}' — "
                            f"{type(getattr(exc, 'orig', exc)).__name__}: "
                            f"{str(getattr(exc, 'orig', exc)).splitlines()[0][:200]}")

    async with engine.begin() as conn:
        v = SchemaValidator(conn)
        v.fixed.extend(enum_fixes)
        v.issues.extend(enum_errors)   # reported, not fatal (see Phase 0)

        # ── Phase 1: check + fix missing tables ───────────────────────────────
        await v.check_tables(Base.metadata)
        if auto_fix and any("TABLE MISSING" in i for i in v.issues):
            await v.fix_missing_tables(engine)
            # Re-check after fix
            v.issues = [i for i in v.issues if "TABLE MISSING" not in i]
            await v.check_tables(Base.metadata)

        # ── Phase 2: check + fix missing columns ──────────────────────────────
        # Widening runs first: a column that exists but is too narrow is not
        # "missing", so ADD COLUMN never touches it, and the mismatch would
        # otherwise stay invisible until an INSERT overflowed it.
        # Approved NOT NULL -> NULL relaxations (explicit list; never generic).
        # A refusal (e.g. "must be owner of table") is reported, not fatal.
        for _tbl, _col in RELAX_NOT_NULL:
            try:
                _r = await conn.execute(text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"), {"t": _tbl, "c": _col})
                _row = _r.first()
                if _row and _row[0] == "NO":
                    if auto_fix:
                        async with conn.begin_nested():
                            await conn.execute(text(f'ALTER TABLE "{_tbl}" ALTER COLUMN "{_col}" DROP NOT NULL'))
                        v.fixed.append(f"Made {_tbl}.{_col} optional (DROP NOT NULL)")
                    else:
                        v.issues.append(f"COLUMN STILL NOT NULL: {_tbl}.{_col}")
            except Exception as _exc:  # noqa: BLE001
                v.issues.append(f"COLUMN NOT RELAXED: {_tbl}.{_col} — "
                                f"{str(getattr(_exc, 'orig', _exc)).splitlines()[0][:200]}")

        widen_cols = await v.check_column_widths(Base.metadata)
        if auto_fix and widen_cols:
            await v.fix_column_widths(widen_cols)

        missing_cols = await v.check_columns(Base.metadata)
        if auto_fix and missing_cols:
            await v.fix_missing_columns(missing_cols)
            # Re-check
            still_missing = []
            for tbl, col, sql_t, default_clause, not_null in missing_cols:
                db_cols = await v._db_columns(tbl)
                if col not in db_cols:
                    still_missing.append((tbl, col, sql_t))
            if still_missing:
                for tbl, col, sql_t in still_missing:
                    v.failed.append(f"Column still missing after fix: {tbl}.{col}")
            else:
                v.issues = [i for i in v.issues if "COLUMN MISSING" not in i]

        # ── Phase 3: check + fix stage data ───────────────────────────────────
        await v.check_stages()
        await v.check_transitions()
        if auto_fix and any("STAGE" in i or "transitions" in i for i in v.issues):
            await v.fix_stages()
            v.issues = [i for i in v.issues if "STAGE" not in i and "transitions" not in i]

        # ── Commit the auto-fixes ──────────────────────────────────────────────
        # (engine.begin() auto-commits on exit)

    summary = {
        "issues_found":  len(v.issues) + len(v.failed),
        "issues_fixed":  len(v.fixed),
        "still_failing": v.failed,
        "fixed":         v.fixed,
        "remaining":     v.issues,
    }

    if v.failed:
        detail = "\n  ".join(v.failed)
        raise RuntimeError(
            f"\n{'='*60}\n"
            f"  SCHEMA VALIDATION FAILED — server cannot start safely.\n"
            f"  {len(v.failed)} issue(s) could not be auto-fixed:\n\n"
            f"  {detail}\n\n"
            f"  Run:  python upgrade_db.py\n"
            f"  Then: python -m alembic upgrade head\n"
            f"  Then restart the server.\n"
            f"{'='*60}"
        )

    return summary


# ── Standalone diagnostic ─────────────────────────────────────────────────────

async def _diagnose():
    """Run as script: report schema issues without fixing."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from sqlalchemy.ext.asyncio import create_async_engine
    from database import Base
    from config import DATABASE_URL
    import models  # noqa

    engine = create_async_engine(DATABASE_URL, echo=False)
    print("\nOxyPC Schema Diagnostic\n" + "=" * 40)

    async with engine.connect() as conn:
        v = SchemaValidator(conn)
        await v.check_tables(Base.metadata)
        await v.check_columns(Base.metadata)
        await v.check_stages()
        await v.check_transitions()

    if v.issues:
        print(f"\n{len(v.issues)} issue(s) found:")
        for issue in v.issues:
            print(f"  [FAIL]  {issue}")
        print(f"\nFix with:  python upgrade_db.py  then  python -m alembic upgrade head")
    else:
        print("\n[OK]  Schema is in sync with ORM models -- no issues found.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_diagnose())
