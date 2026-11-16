"""KYC documents of an Account, checked one by one (2026-11-15, Pankaj).

- KYC_ITEMS: the documents in Account-form order. MSME, Director 1, Director 2
  and Vendor Form are not asked of Buyers (hidden on the form, skipped here).
- Accounts' Verify KYC pop-up verifies / rejects each document
  (crm_contact_kyc_items). Reject needs a note and notifies whoever added the
  Account; uploading that document again puts it back to pending.
- KYC can be verified once every document the Account has (number or file)
  is verified — Accounts' Confirm and the Account page's Verify KYC both check.
- Sample files: an admin uploads one per document (app_settings
  "kyc_sample_<item>", file under uploads/crm/kyc_samples/); everyone can
  download it from the icon next to the upload box.
"""
import os
import uuid as _uuid

from sqlalchemy import select

from config import UPLOADS_DIR
from models.crm import CRMContactKYCItem
from models.settings import AppSetting
from utils.timezone import app_now

KYC_ITEMS = [   # key, label, number attr, file attr, asked of Buyers
    ("gstin", "GSTIN", "gstin", "gstin_doc_path", True),
    ("pan", "PAN", "pan", "pan_doc_path", True),
    ("bank_cheque", "Bank Cheque", "bank_cheque_number", "bank_cheque_doc_path", True),
    ("msme", "MSME/UDHYOG Certificate", "msme_number", "msme_doc_path", False),
    ("director1", "Director 1 Aadhar/PAN", "director1_id_number", "director1_doc_path", False),
    ("director2", "Director 2 Aadhar/PAN", "director2_id_number", "director2_doc_path", False),
    ("vendor_form", "Vendor Form", "vendor_form_number", "vendor_form_doc_path", False),
]
ITEM = {k: (k, l, n, p, b) for k, l, n, p, b in KYC_ITEMS}
SAMPLE_PREFIX = "kyc_sample_"
SAMPLE_DIR = os.path.join(UPLOADS_DIR, "crm", "kyc_samples")


def applicable(contact):
    """Items asked of this Account (Buyers skip MSME / Directors / Vendor Form)."""
    buyer = (getattr(contact, "contact_type", "") or "") == "buyer"
    return [it for it in KYC_ITEMS if it[4] or not buyer]


def has_data(contact, it) -> bool:
    return bool((getattr(contact, it[2], None) or "").strip() or getattr(contact, it[3], None))


async def status_map(db, contact_ids) -> dict:
    """{contact_id(str): {item: CRMContactKYCItem}}"""
    ids = [i for i in contact_ids if i]
    if not ids:
        return {}
    out = {}
    for r in (await db.execute(select(CRMContactKYCItem).where(CRMContactKYCItem.contact_id.in_(ids)))).scalars().all():
        out.setdefault(str(r.contact_id), {})[r.item] = r
    return out


async def outstanding(db, contact) -> list[str]:
    """Labels of documents the Account has that are not verified yet."""
    st = (await status_map(db, [contact.id])).get(str(contact.id), {})
    return [it[1] for it in applicable(contact) if has_data(contact, it)
            and (st.get(it[0]) is None or st[it[0]].status != "verified")]


async def set_status(db, contact, item, status, user, note=None):
    row = (await db.execute(select(CRMContactKYCItem).where(CRMContactKYCItem.contact_id == contact.id,
                                                            CRMContactKYCItem.item == item))).scalar_one_or_none()
    if not row:
        row = CRMContactKYCItem(contact_id=contact.id, item=item)
        db.add(row)
    old = row.status
    row.status, row.note = status, (note or None)
    row.checked_by, row.checked_at = (getattr(user, "username", None), app_now()) if user else (None, None)
    return old


async def reset_to_pending(db, contact, items):
    """A new upload / number of these documents goes back to pending (and the
    Account's KYC is no longer verified if one of its documents changed)."""
    if not items:
        return
    rows = (await db.execute(select(CRMContactKYCItem).where(CRMContactKYCItem.contact_id == contact.id,
                                                             CRMContactKYCItem.item.in_(list(items))))).scalars().all()
    for r in rows:
        if r.status != "pending":
            r.status, r.note, r.checked_by, r.checked_at = "pending", None, None, None


async def samples(db) -> dict:
    """{item: stored file name} for the sample files an admin uploaded."""
    rows = (await db.execute(select(AppSetting).where(AppSetting.key.like(SAMPLE_PREFIX + "%")))).scalars().all()
    return {r.key[len(SAMPLE_PREFIX):]: r.value for r in rows if r.value}


async def save_sample(db, item, upload, user):
    ext = os.path.splitext(upload.filename or "")[1].lower()[:10]
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    name = "%s_%s%s" % (item, _uuid.uuid4().hex[:8], ext)
    with open(os.path.join(SAMPLE_DIR, name), "wb") as f:
        f.write(await upload.read())
    key = SAMPLE_PREFIX + item
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    old = row.value if row else None
    if row:
        row.value, row.updated_by = name, user.username
    else:
        db.add(AppSetting(key=key, value=name, description="KYC sample file — %s" % ITEM[item][1], updated_by=user.username))
    return old, name


def sample_path(name):
    if not name:
        return None
    p = os.path.join(SAMPLE_DIR, os.path.basename(name))
    return p if os.path.isfile(p) else None


def doc_list(contact, st=None) -> list[dict]:
    """[{key, label, number, url, has, status, note}] — the documents asked of this
    Account, for the Account card and Accounts' Verify KYC pop-up. url is None when
    no file was uploaded or it is missing on disk."""
    st = st or {}
    out = []
    for it in applicable(contact):
        key, label, num_attr, path_attr, _b = it
        name = getattr(contact, path_attr, None)
        on_disk = bool(name) and os.path.isfile(os.path.join(UPLOADS_DIR, "crm", os.path.basename(name)))
        row = st.get(key)
        out.append({"key": key, "label": label, "number": getattr(contact, num_attr, None) or "",
                    "url": ("/uploads/crm/%s" % os.path.basename(name)) if on_disk else None,
                    "has": has_data(contact, it), "status": row.status if row else "pending",
                    "note": (row.note or "") if row else ""})
    return out
