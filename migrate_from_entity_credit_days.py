"""One-shot migration (2026-11-10/11, approved by Pankaj): From Entity,
Expected Close Date and Credit Days columns, and the quotation status rename
(shared -> added, confirmed -> approved).

OxyPC already does all of this by itself on startup (db_validator adds the
columns when OXYPC_AUTO_FIX=1, the default; main.py's _quotations_v2_once maps
the statuses and _backfill_from_entity_once fills From Entity). Run this only
if the server has OXYPC_AUTO_FIX=0. Safe to run more than once.

Run: python migrate_from_entity_credit_days.py
"""
import asyncio
from sqlalchemy import text
from database import engine

STATEMENTS = [
    "ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS from_entity VARCHAR(100)",
    "ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS credit_days INTEGER",
    "ALTER TABLE crm_sourcing_deals ADD COLUMN IF NOT EXISTS from_entity VARCHAR(100)",
    "ALTER TABLE crm_sourcing_deals ADD COLUMN IF NOT EXISTS expected_close_date DATE",
    "ALTER TABLE crm_sales_opportunities ADD COLUMN IF NOT EXISTS from_entity VARCHAR(100)",
    "ALTER TABLE crm_quotes ADD COLUMN IF NOT EXISTS from_entity VARCHAR(100)",
    "ALTER TABLE crm_purchase_orders ADD COLUMN IF NOT EXISTS from_entity VARCHAR(100)",
    "ALTER TABLE dealer_quotations ALTER COLUMN status SET DEFAULT 'added'",
]


async def main():
    async with engine.begin() as conn:
        for stmt in STATEMENTS:
            print(f"Running: {stmt}")
            await conn.execute(text(stmt))
    print("Migration complete. Restart OxyPC: the one-time status mapping and From Entity backfill run on startup.")

if __name__ == "__main__":
    asyncio.run(main())
