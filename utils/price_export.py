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


# ── Pricing Add / Pricing View helpers (2026-11-03) ──────────────────────────
def can_add_prices(user, module) -> bool:
    """Pricing Add tick on `module` (or any of several). Admin always True."""
    from models.role_permissions import has_perm
    mods = (module,) if isinstance(module, str) else tuple(module)
    return any(has_perm(_role(user), m, "pricing_add") for m in mods)


def can_view_prices(user, module) -> bool:
    """Pricing View tick on `module` (or any of several). Admin always True."""
    from models.role_permissions import has_perm
    mods = (module,) if isinstance(module, str) else tuple(module)
    return any(has_perm(_role(user), m, "pricing") for m in mods)


def keep_price(user, module, new, old):
    """For records where a price is one field among many (device, lot, GRN,
    part, contact...): without Pricing Add the submitted value is ignored and
    the stored one kept (None on a new record). The rest of the form saves."""
    return new if can_add_prices(user, module) else old


def price_add_block(user, module, back_url: str):
    """For pages whose whole purpose is entering a price/amount (New Sale, New
    Parts Sale, New Rental, New Quote, New PO...): None when the user has Pricing
    Add on `module`, else a redirect back with a red notice. The matching POST
    is refused by the action guard as well (route_actions: ('add','pricing_add'))."""
    from urllib.parse import quote_plus
    from fastapi.responses import RedirectResponse
    if can_add_prices(user, module):
        return None
    sep = "&" if "?" in back_url else "?"
    return RedirectResponse(url=f"{back_url}{sep}error=" + quote_plus(
        "Your role does not have Pricing Add permission here. Ask an admin to tick Pricing Add in the Permission Matrix."),
        status_code=302)


def price_unchanged(user, module, raw) -> bool:
    """True when a submitted price field must NOT be applied (2026-11-03):
    no Pricing Add, or the field came back blank from a role that cannot see
    the current price (Add-only roles get an empty input; blank = keep)."""
    if not can_add_prices(user, module):
        return True
    blank = raw is None or str(raw).strip() == ""
    return blank and not can_view_prices(user, module)


def _num(raw, default=0.0):
    try:
        return float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def credit_new(user, module, raw):
    """Credit limit on a new contact/dealer: 0 without Pricing Add."""
    return _num(raw) if can_add_prices(user, module) else 0.0


def credit_edit(user, module, raw, current):
    """Credit limit on an edit: stored value kept without Pricing Add (or when an
    Add-only role leaves the hidden-value field blank)."""
    if price_unchanged(user, module, raw):
        return current
    return _num(raw)


_PRICE_KEY_WORDS = ("price", "amount", "cost", "invoice_value", "quote_given", "budget", "credit_limit", "outstanding")
_PRICE_KEYS_EXACT = {"sgst", "cgst", "igst", "value"}


def strip_price_keys(row: dict, user, module) -> dict:
    """Bulk uploads (2026-11-03): without Pricing Add, a row's price/amount
    columns are dropped before it is processed (the rest imports as usual)."""
    if can_add_prices(user, module):
        return row
    def _is_price(k):
        k = (k or "").strip().lower().replace(" ", "_")
        return k in _PRICE_KEYS_EXACT or any(w in k for w in _PRICE_KEY_WORDS)
    return {k: v for k, v in row.items() if not _is_price(k)}


def price_view_block(user, module, back_url: str = "/"):
    """Pages that are nothing but amounts (ageing, ledger, receivables, payments,
    printed invoices / receipts...): None with Pricing View on `module`, else a
    redirect back with a red notice instead of a page of blanks (2026-11-03)."""
    from urllib.parse import quote_plus
    from fastapi.responses import RedirectResponse
    if can_view_prices(user, module):
        return None
    sep = "&" if "?" in back_url else "?"
    return RedirectResponse(url=f"{back_url}{sep}error=" + quote_plus(
        "Your role cannot view prices here (Pricing View), so this page is not available."),
        status_code=302)


def blank_prices(user, module, obj, keys):
    """JSON feeds (2026-11-03): without Pricing View on `module`, the given keys
    are set to None everywhere in `obj` (dicts / lists, nested). Returns obj."""
    if can_view_prices(user, module):
        return obj
    want = set(keys)

    def walk(o):
        if isinstance(o, dict):
            for k in list(o):
                if k in want:
                    o[k] = None
                else:
                    walk(o[k])
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(obj)
    return obj
