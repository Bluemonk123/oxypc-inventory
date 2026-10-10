"""devices_touch_type

Revision ID: 20261010_1500
Revises: 20261010_1200
Create Date: 2026-10-10 15:00:00

Touch Type on each device (devices.touch_type: "touch" / "non_touch"), set by
the Final QC form's radio buttons and shown as a T/NT badge next to the Tag
Number on Ready for Sale and All Inventory. Nullable; blank shows as NT, so
existing devices are untouched.

As with every other column in this app, db_validator.py adds it on the next
server start; this migration keeps Alembic history accurate for a fresh
environment that runs `alembic upgrade` directly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20261010_1500'
down_revision: Union[str, None] = '20261010_1200'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS touch_type VARCHAR(10)")


def downgrade() -> None:
    op.drop_column('devices', 'touch_type')
