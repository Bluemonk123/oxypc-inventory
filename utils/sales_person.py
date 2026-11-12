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
