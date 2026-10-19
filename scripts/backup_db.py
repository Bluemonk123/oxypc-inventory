#!/usr/bin/env python3
"""
OxyPC Database Backup Script
=============================
Usage:
    python scripts/backup_db.py           # backup + prune old backups
    python scripts/backup_db.py --prune-only  # only prune, no new backup

Saves to: backups/oxypc_YYYYMMDD_HHMMSS.sql.gz
Retention: 30 days (older files deleted automatically)

pg_dump must be on PATH (installed with PostgreSQL).
"""
import os
import sys
import gzip
import shutil
import subprocess
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, unquote

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).resolve().parent.parent  # repo root
BACKUP_DIR  = BASE_DIR / "backups"
RETENTION_DAYS = 30

# ── Resolve pg_dump — check PATH first, fall back to common Windows install ──
def _find_pg_dump() -> str:
    """Return pg_dump executable path, checking PATH then common install dirs."""
    if shutil.which("pg_dump"):
        return "pg_dump"
    candidates = [
        r"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe",
        r"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe",
        r"C:\Program Files\PostgreSQL\16\bin\pg_dump.exe",
        r"C:\Program Files\PostgreSQL\15\bin\pg_dump.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    print("ERROR: pg_dump not found. Install PostgreSQL or add its bin/ to PATH.", file=sys.stderr)
    sys.exit(1)

PG_DUMP = _find_pg_dump()

# ── Resolve DATABASE_URL ──────────────────────────────────────────────────────
sys.path.insert(0, str(BASE_DIR))
try:
    from config import DATABASE_URL as _RAW_URL
except ImportError:
    _RAW_URL = os.environ.get("OXYPC_DATABASE_URL", "")

if not _RAW_URL:
    print("ERROR: DATABASE_URL not found. Set OXYPC_DATABASE_URL env var.", file=sys.stderr)
    sys.exit(1)

# pg_dump uses postgresql:// not postgresql+asyncpg://
DB_URL = _RAW_URL.replace("postgresql+asyncpg://", "postgresql://")
_parsed = urlparse(DB_URL)
DB_HOST = _parsed.hostname or "localhost"
DB_PORT = str(_parsed.port or 5432)
# urlparse does NOT decode percent-encoded credentials (e.g. a password containing
# "%40" for "@") — must unquote explicitly, or pg_dump gets the wrong password.
DB_USER = unquote(_parsed.username or "postgres")
DB_PASS = unquote(_parsed.password or "")
DB_NAME = unquote((_parsed.path or "").lstrip("/"))


def prune_old_backups():
    """Delete .sql.gz backup files older than RETENTION_DAYS."""
    if not BACKUP_DIR.exists():
        return []
    cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
    deleted = []
    for f in BACKUP_DIR.glob("oxypc_*.sql.gz"):
        if datetime.utcfromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            deleted.append(f.name)
    if deleted:
        print(f"Pruned {len(deleted)} backup(s) older than {RETENTION_DAYS} days:")
        for name in deleted:
            print(f"  - {name}")
    return deleted


def run_backup() -> Path:
    """Run pg_dump, gzip the output, return the backup file path."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    # Local time, not UTC. The filename is how an operator picks a restore point
    # under pressure, so it must match the wall clock they are reading it by.
    # With utcnow() on an IST server the 23:00 nightly run was stamped 17:30 with
    # the PREVIOUS day's date - inviting a restore to the wrong day.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path  = BACKUP_DIR / f"oxypc_{timestamp}.sql.gz"

    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASS

    cmd = [
        PG_DUMP,
        "-h", DB_HOST,
        "-p", DB_PORT,
        "-U", DB_USER,
        "--format=plain",
        "--no-password",
        DB_NAME,
    ]

    print(f"Running: pg_dump -h {DB_HOST} -p {DB_PORT} -U {DB_USER} {DB_NAME}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    with gzip.open(out_path, "wb") as gz:
        shutil.copyfileobj(proc.stdout, gz)
    proc.wait()
    stderr_out = proc.stderr.read().decode("utf-8", errors="replace")

    if proc.returncode != 0:
        out_path.unlink(missing_ok=True)
        print(f"ERROR: pg_dump failed:\n{stderr_out}", file=sys.stderr)
        sys.exit(1)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"Backup saved: {out_path.name} ({size_mb:.2f} MB)")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="OxyPC database backup")
    parser.add_argument("--prune-only", action="store_true",
                        help="Only prune old backups, skip creating a new one")
    parser.add_argument("--no-prune", action="store_true",
                        help="Create a backup but delete nothing (used by the admin "
                             "'Run Backup Now' button, same policy as the nightly job)")
    args = parser.parse_args()
    if not args.prune_only:
        run_backup()
    if not args.no_prune:
        prune_old_backups()
    print("Done.")


if __name__ == "__main__":
    main()
