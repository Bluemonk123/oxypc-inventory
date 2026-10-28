"""Bulk QC report download (2026-10-28): one zip holding each tag's latest
Stress / QC report PDF — the same report the single "QC Report" / PDF buttons
give (routers/stress_api.download_report, built fresh from the saved results).

Used by Ready for Sale (Tags) "Bulk QC" for the ticked tags and by Block Tags
for every tag in a block. Tags with no saved report are listed in
"no_qc_report.txt" inside the zip rather than failing the whole download.
"""
import io
import zipfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

MAX_TAGS = 2000          # one request; a bigger selection should be split


async def build_qc_zip(db: AsyncSession, barcodes) -> tuple[bytes, list[str], list[str]]:
    """(zip bytes, tags included, tags without a report). Barcodes are matched
    exactly, in the order given, duplicates dropped."""
    from models.stress import StressTestResult
    from routers.stress_api import _generate_pdf

    tags = list(dict.fromkeys(b.strip() for b in barcodes if b and b.strip()))
    latest = {}
    for i in range(0, len(tags), 1000):
        chunk = tags[i:i + 1000]
        for rec in (await db.execute(
            select(StressTestResult).where(StressTestResult.barcode.in_(chunk))
            .order_by(StressTestResult.run_at.desc(), StressTestResult.id.desc())
        )).scalars().all():
            latest.setdefault(rec.barcode, rec)          # newest run per tag

    found, missing = [], []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for tag in tags:
            rec = latest.get(tag)
            if rec is None:
                missing.append(tag)
                continue
            try:
                pdf = _generate_pdf(rec)
            except Exception:
                missing.append(tag)
                continue
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tag)
            zf.writestr(f"QC_Report_{safe}.pdf", pdf)
            found.append(tag)
        if missing:
            zf.writestr("no_qc_report.txt",
                        "No saved Stress / QC report for these tags:\r\n" + "\r\n".join(missing) + "\r\n")
    return buf.getvalue(), found, missing


def zip_response(data: bytes, name: str, found: int, missing: int):
    from fastapi.responses import Response
    return Response(content=data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "X-QC-Found": str(found), "X-QC-Missing": str(missing),
        "Access-Control-Expose-Headers": "X-QC-Found, X-QC-Missing, Content-Disposition",
    })
