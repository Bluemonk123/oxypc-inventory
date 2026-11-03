"""Pricing Export (2026-11-03): price columns in CSV downloads only for roles
with the module's "Pricing Export" tick in the Permission Matrix.

strip_price_columns() takes the finished CSV response of an export and, when
the user lacks Pricing Export on `module`, rebuilds it without the named
columns (matched on the header row, case-insensitive). Everything else in the
file — rows, order, filename, "# TRUNCATED" notes — stays as it was.
"""
import csv
import io

from fastapi.responses import Response, StreamingResponse


def _role(user) -> str:
    r = getattr(user, "role", "")
    return getattr(r, "value", None) or str(r)


def can_export_prices(user, module) -> bool:
    from models.role_permissions import has_perm
    mods = (module,) if isinstance(module, str) else tuple(module)
    return any(has_perm(_role(user), m, "pricing_export") for m in mods)


async def _body(resp) -> bytes:
    if isinstance(resp, StreamingResponse):
        chunks = []
        async for c in resp.body_iterator:
            chunks.append(c if isinstance(c, bytes) else str(c).encode("utf-8"))
        return b"".join(chunks)
    return resp.body


def drop_csv_columns(text: str, columns) -> str:
    want = {c.strip().lower() for c in columns}
    rows = list(csv.reader(io.StringIO(text)))
    hdr_i = next((i for i, r in enumerate(rows) if any((c or "").strip().lower() in want for c in r)), None)
    if hdr_i is None:
        return text
    drop = {j for j, c in enumerate(rows[hdr_i]) if (c or "").strip().lower() in want}
    out = io.StringIO()
    w = csv.writer(out)
    for i, r in enumerate(rows):
        # Rows above the header (titles) are kept as they are.
        w.writerow(r if i < hdr_i else [c for j, c in enumerate(r) if j not in drop])
    return out.getvalue()


async def strip_price_columns(resp, user, module, columns):
    """The export response, minus `columns`, unless the user may export prices."""
    if can_export_prices(user, module):
        return resp
    raw = await _body(resp)
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", errors="replace")
    new = drop_csv_columns(text, columns).encode("utf-8")
    if bom:
        new = b"\xef\xbb\xbf" + new
    headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-length",)}
    return Response(content=new, media_type=resp.media_type or "text/csv", headers=headers)


def price_export_block(user, module, back_url: str):
    """For downloads that are mostly prices (priced documents, statements,
    ageing): None when the user may export prices, else a redirect back to
    `back_url` with a red notice — the file is not produced at all."""
    from urllib.parse import quote_plus
    from fastapi.responses import RedirectResponse
    if can_export_prices(user, module):
        return None
    sep = "&" if "?" in back_url else "?"
    return RedirectResponse(url=f"{back_url}{sep}error=" + quote_plus(
        "Your role cannot export prices (Pricing Export), so this priced file is not available."),
        status_code=302)
