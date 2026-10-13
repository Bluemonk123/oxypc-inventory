"""dealers_sub_category_website_cid

Revision ID: 20261013_1200
Revises: 20261010_1700
Create Date: 2026-10-13 12:00:00

Three nullable columns on dealers:
  * dealer_sub_category - finer grouping under Type, options from the Master
    Dropdown category "dealer_sub_category"
  * website             - from the Google Maps export
  * google_cid          - Google Maps place CID (digits only); also the
                          de-duplication key when the same export is uploaded
                          again

As with every other column in this app, db_validator.py adds them on the next
server start; this migration keeps Alembic history accurate for a fresh
environment that runs `alembic upgrade` directly.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '20261013_1200'
down_revision: Union[str, None] = '20261010_1700'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE dealers ADD COLUMN IF NOT EXISTS dealer_sub_category VARCHAR(150)")
    op.execute("ALTER TABLE dealers ADD COLUMN IF NOT EXISTS website VARCHAR(1000)")
    op.execute("ALTER TABLE dealers ADD COLUMN IF NOT EXISTS google_cid VARCHAR(30)")


def downgrade() -> None:
    op.drop_column('dealers', 'google_cid')
    op.drop_column('dealers', 'website')
    op.drop_column('dealers', 'dealer_sub_category')
