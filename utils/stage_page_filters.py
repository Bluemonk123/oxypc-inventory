"""Shared "which tags a stage page lists" rule (2026-11-26, Pankaj).

The L1, L3/L4, Stress, Cosmetic (All Tags) and Final QC pages, and the WorkID
Status cards' Pending, never show trashed tags (the tag or its lot),
External Partner Test tags or inactive tags. Keep the pages and the cards on
this one rule so their counts match.
"""
from sqlalchemy import or_

from models.device import Device
from models.lot import Lot
from models.master import EXTERNAL_PARTNER_TEST_ENTITY


def live_tag_filters(with_lot: bool = True, skip_entity: bool = False) -> list:
    """WHERE conditions for a query on Device (joined to Lot when with_lot)."""
    conds = [Device.is_trashed.isnot(True), Device.is_active.isnot(False)]
    if not skip_entity:
        conds.append(or_(Device.entity.is_(None), Device.entity != EXTERNAL_PARTNER_TEST_ENTITY))
    if with_lot:
        conds.append(Lot.is_trashed.isnot(True))
    return conds
