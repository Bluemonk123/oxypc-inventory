"""block_records_box_number

Revision ID: 20261010_1200
Revises: 20261007_1000
Create Date: 2026-10-10 12:00:00

Block Tags: optional Box Number on each block (block_records.box_number),
entered in the New/Edit Block modals, shown as a badge next to the Block ID
and printed on the barcode label. Nullable, so existing blocks are untouched.

As with every other column in this app, db_validator.py adds it on the next
server start; this migration keeps Alembic history accurate for a fresh
environment that runs `alembic upgrade` directly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20261010_1200'
down_revision: Union[str, None] = '20261007_1000'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE block_records ADD COLUMN IF NOT EXISTS box_number VARCHAR(50)")


def downgrade() -> None:
    op.drop_column('block_records', 'box_number')
