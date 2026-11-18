"""Shared salesperson helpers."""
async def sales_person_options(db) -> list[str]:
    """Distinct salespeople already recorded, for the New Sale datalist.

    A suggestion list, not a constraint: the field stays free text so a new
    starter can be credited immediately. Its job is to stop the same person
    being entered as "Manoj", "manoj" and "Manoj " and fragmenting reports.
    Draws from BOTH sale types so either form suggests the full set.
    """
    from sqlalchemy import select, union
    from models.sales import Sale
    from models.part_sales import PartSale
    a = (await db.execute(select(Sale.sales_person).where(Sale.sales_person.isnot(None)).distinct())).scalars().all()
    b = (await db.execute(select(PartSale.sales_person).where(PartSale.sales_person.isnot(None)).distinct())).scalars().all()
    return sorted({x.strip() for x in list(a) + list(b) if x and x.strip()})


async def sales_person_user_options(db) -> list[tuple[str, str]]:
    """Active users for the Sales Person dropdown (2026-11-12):
    [(username, "Full Name (username)")], by name."""
    from sqlalchemy import select
    from models.user import User
    rows = (await db.execute(select(User.username, User.full_name).where(User.status.isnot(False)))).all()
    out = [(u, f"{(f or u).strip()} ({u})" if (f or "").strip() and f.strip() != u else u) for u, f in rows]
    return sorted(out, key=lambda x: x[1].lower())


async def resolve_sales_person(db, username: str, typed: str = ""):
    """(name, username) to store on a sale. A picked user wins (name = their
    full name); otherwise the typed text, unlinked."""
    from sqlalchemy import select
    from models.user import User
    username = (username or "").strip()
    if username:
        u = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if u:
            return ((u.full_name or u.username).strip()[:100], u.username)
    t = (typed or "").strip()
    return (t[:100] or None, None)


async def linked_name_map(db) -> dict:
    """{username: full name} for showing a linked Sales Person (2026-11-12)."""
    from sqlalchemy import select
    from models.user import User
    return {u: (f or u).strip() for u, f in (await db.execute(select(User.username, User.full_name))).all()}


def sales_person_display(sale, names: dict) -> str:
    """A linked sale shows the user's full name; an unlinked one its typed text."""
    u = getattr(sale, "sales_person_username", None)
    if u:
        return names.get(u, u)
    return (getattr(sale, "sales_person", None) or "").strip()


# ── Internal Only (2026-11-18, Pankaj) ───────────────────────────────────────
# A sale recorded with "Internal Only" ticked is an inter-company sale to a
# sister company: the Sale row is saved (invoice from the selling entity) but
# the tag is NOT marked Sold, so after a ReGRN to the buyer's GRN the buyer
# sells it on. Marker: sales_person == "Internal Only" with no linked user.
INTERNAL_SALES_PERSON = "Internal Only"


def is_internal_sale(sale) -> bool:
    return ((getattr(sale, "sales_person", None) or "").strip() == INTERNAL_SALES_PERSON
            and not getattr(sale, "sales_person_username", None))


def not_internal_sale():
    """SQL: the sale is not an Internal Only (inter-company) sale."""
    from sqlalchemy import func, or_
    from models.sales import Sale
    return or_(Sale.sales_person.is_(None), func.trim(Sale.sales_person) != INTERNAL_SALES_PERSON,
               Sale.sales_person_username.isnot(None))


async def internal_buyer_options(db) -> list[dict]:
    """Buyer entities for an Internal Only sale (2026-11-18, Pankaj): each
    Master Data entity with its Company Settings row (matched by
    company_entity) so the sale form can autofill the customer's phone,
    address and state. An entity without a company row is still listed, with
    blank details. Saved as Customer Name: the company name, else the entity."""
    from sqlalchemy import select
    from models.company import Company
    from utils.master_data import entity_values
    companies = {}
    for c in (await db.execute(select(Company).where(Company.is_active == True)
                               .order_by(Company.created_at))).scalars().all():
        if (c.company_entity or "").strip():
            companies.setdefault(c.company_entity.strip().lower(), c)
    out = []
    for ent in await entity_values(db):
        c = companies.get((ent or "").strip().lower())
        out.append({
            "entity": ent,
            "name": ((c.company_name if c else "") or ent).strip(),
            "phone": (c.company_phone if c else "") or "",
            "address": (c.company_address if c else "") or "",
            "state": (c.company_state if c else "") or "",
        })
    return out
