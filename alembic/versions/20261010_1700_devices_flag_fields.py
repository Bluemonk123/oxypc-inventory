"""devices_flag_fields

Revision ID: 20261010_1700
Revises: 20261010_1500
Create Date: 2026-10-10 17:00:00

Urgent flag on each device, set in bulk from Inventory Manager's "Flag Items"
modal: authorized by, request date, target date, reason, plus who/when saved.
All nullable; cleared automatically whenever the tag moves back into Stock In.

As with every other column in this app, db_validator.py adds these on the next
server start; this migration keeps Alembic history accurate for a fresh
environment that runs `alembic upgrade` directly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20261010_1700'
down_revision: Union[str, None] = '20261010_1500'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS flag_authorized_by VARCHAR(100)")
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS flag_request_date DATE")
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS flag_target_date DATE")
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS flag_reason TEXT")
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS flagged_at TIMESTAMP")
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS flagged_by VARCHAR(50)")


def downgrade() -> None:
    for col in ("flagged_by", "flagged_at", "flag_reason", "flag_target_date",
                "flag_request_date", "flag_authorized_by"):
        op.drop_column('devices', col)
