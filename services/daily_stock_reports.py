"""Daily Report — the Daily Stock exports (Overall / Tag Based / Location Based)
generated automatically every day at 9:00 PM and kept as downloadable files.

Files live under uploads/daily_stock_reports/<YYYY-MM-DD>/{overall,tags,location}.csv
(uploads/ is the persistent bind mount in the Docker deployment, so reports
survive redeploys). No database table: the folder IS the index, which keeps
this out of the schema. A report is listed only when all three files exist.

The generated content is exactly what the Daily Stock page's own exports produce
for that date (routers.reports.build_*_csv), so it follows the same rules
(Opening / Closing = Opening - the day's sales, no sold / GRN rows, no
deactivated or trashed tags, sales by Sale Date).

A 9 PM file is a snapshot: sales dated that day but keyed in later (after 9 PM,
or back-dated entries made the next day) are not in it. Days before the feature
existed were generated afterwards from the Stage Movement log (see backfill).
"""
import asyncio
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from config import UPLOADS_DIR
from utils.timezone import app_now

REPORTS_DIR = Path(UPLOADS_DIR) / "daily_stock_reports"
KIND_FILES = {"overall": "overall.csv", "tags": "tags.csv", "location": "location.csv"}
FIRST_REPORT_DATE = date(2026, 9, 1)     # the Daily Report table starts here
GENERATE_AT_HOUR = 21                    # 9:00 PM app time
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CHECK_EVERY_SECONDS = 120


def report_file(report_date: str, kind: str) -> Path | None:
    """Path of one saved file, or None if the date/kind is invalid or missing.
    Both inputs are validated against fixed patterns, so no path can escape REPORTS_DIR."""
    if not _DATE_RE.match(report_date or "") or kind not in KIND_FILES:
        return None
    path = REPORTS_DIR / report_date / KIND_FILES[kind]
    return path if path.is_file() else None


def _is_complete(day: date) -> bool:
    return all(report_file(day.isoformat(), k) for k in KIND_FILES)


def list_reports() -> list[dict]:
    """Every complete saved report, latest date first."""
    out = []
    if REPORTS_DIR.is_dir():
        for child in REPORTS_DIR.iterdir():
            if child.is_dir() and _DATE_RE.match(child.name) and _is_complete(date.fromisoformat(child.name)):
                d = date.fromisoformat(child.name)
                out.append({"date": child.name, "label": d.strftime("%d-%b-%Y")})
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same folder, then rename into place, so a
    half-written file is never listed or downloaded."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)        # a failed write leaves only an ignored *.tmp behind


async def generate_report(db, day: date) -> None:
    """Build and save all three files for `day` (replaces any existing ones)."""
    from routers.reports import build_overall_csv, build_tags_csv, build_location_csv
    builders = {"overall": build_overall_csv, "tags": build_tags_csv, "location": build_location_csv}
    for kind, build in builders.items():
        text = await build(db, day, False, [])
        _write_atomic(REPORTS_DIR / day.isoformat() / KIND_FILES[kind], text)


def days_due(now: datetime) -> list[date]:
    """Dates whose report should exist right now: today once it is 9 PM, and the
    last two days as catch-up (the server was down at 9 PM, or a run failed)."""
    today = now.date()
    due = [today] if now.hour >= GENERATE_AT_HOUR else []
    due += [today - timedelta(days=1), today - timedelta(days=2)]
    return [d for d in due if d >= FIRST_REPORT_DATE]


async def generate_missing(now: datetime | None = None) -> list[str]:
    """Generate every due report that is not saved yet. Returns the dates done."""
    from database import AsyncSessionLocal
    done = []
    for day in days_due(now or app_now()):
        if _is_complete(day):
            continue
        try:
            async with AsyncSessionLocal() as db:
                await generate_report(db, day)
            done.append(day.isoformat())
            print(f"  [DailyReport] generated {day.isoformat()}")
        except Exception as exc:               # noqa: BLE001 — retried on the next check
            print(f"  [DailyReport] {day.isoformat()} failed (will retry): {exc}")
    return done


async def scheduler_loop() -> None:
    """Started once from main.py's startup. Checks every couple of minutes; the
    app runs a single uvicorn worker, and generation skips anything already saved,
    so an overlap could only ever repeat work, never duplicate a report."""
    await asyncio.sleep(30)                    # let startup finish first
    while True:
        try:
            # Same rule as the pages (2026-11-27): no ETP / trashed / inactive tags.
            from utils.hidden_tags import hidden_tags_scope
            with hidden_tags_scope():
                await generate_missing()
        except Exception as exc:               # noqa: BLE001
            print(f"  [DailyReport] scheduler error: {exc}")
        await asyncio.sleep(_CHECK_EVERY_SECONDS)


async def backfill(start: date | None = None, end: date | None = None, force: bool = False) -> list[str]:
    """One-off: generate reports for past days (default FIRST_REPORT_DATE .. yesterday)
    from the Stage Movement log. Skips days already saved unless force=True."""
    from database import AsyncSessionLocal
    start = start or FIRST_REPORT_DATE
    end = end or (app_now().date() - timedelta(days=1))
    done, day = [], start
    while day <= end:
        if force or not _is_complete(day):
            async with AsyncSessionLocal() as db:
                await generate_report(db, day)
            done.append(day.isoformat())
            print(f"  [DailyReport] backfilled {day.isoformat()}", flush=True)
        day += timedelta(days=1)
    return done
