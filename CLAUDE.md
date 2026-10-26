# Memory — OxyPC Inventory

## Project
FastAPI + PostgreSQL web ERP for OxyPC Computers (laptop refurbishment).  
Server: `C:\Users\Pankaj.sehgal\Claude\Oxypc\oxypc-inventory\`  
Runs at `192.168.1.100:8000` on LAN. 30+ concurrent users.

## People
| Who | Role |
|-----|------|
| **Pankaj** | Director / owner. CEO Deshwal. Gives feature requests. |

## Key Terms
| Term | Meaning |
|------|---------|
| **Lot** | Purchased batch of devices from a supplier (e.g., LOT-042) |
| **GRN** | Goods Receipt Note — physical receipt verification of a lot |
| **IQC** | Incoming Quality Check — first hardware inspection |
| **QC** | Quality Check — post-repair final check |
| **Stage** | Device workflow state: IQC → L1/L2/L3 repair → QC → Ready → Sold |
| **OxyQC** | Standalone PyInstaller EXE for IQC inspectors (offline-capable) |
| **is_trashed** | Soft-delete flag on Lot (boolean, nullable — use `isnot(True)` not `== False`) |
| **RBAC** | Role-based access: admin, inventory_manager, sales, iqc_inspector, l1/l2/l3_engineer, qc_inspector |
| **CRM** | contacts + sourcing deals + sales opps + telecalling / dealer management modules |

## Architecture
- **Router files:** `routers/` — one file per feature area (stock.py, crm_contacts.py, sales.py…)
- **Models:** `models/` — SQLAlchemy ORM models
- **Templates:** `templates/` — Jinja2 HTML (Bootstrap 5 + DataTables + Bootstrap Icons)
- **Services:** `services/audit_engine.py`, `services/event_bus.py`
- **Auth:** `auth/dependencies.py` — `get_current_user`, `require_roles`, `verify_csrf`
- **Key pattern:** `await db.execute(select(...))` → `.scalar_one_or_none()` or `.scalars().all()`

## Current Sprint Focus
Lot Management cleanup: cascade delete just shipped. Next: verify in app, then move to device/sale page enhancements.

## Critical Rules (from global CLAUDE.md)
- Permission Matrix (2026-10-25): every new POST/PUT/PATCH/DELETE route must be added to `auth/route_actions.py` as (module, action) — action is add / upload / edit / delete / pricing / export (Download / Export — also add export/download GET links), or None if it must not be gated — and its buttons wrapped in `{% if can(current_user, '<module>', '<action>') %}`. Run `python _scan_action_coverage.py` before any push: it must print "0 unmapped, 0 stale, 0 bad". Make Active / Inactive toggles count as **edit**.
- Never delete files without explicit per-file user permission
- No schema changes without dbdiagram.io approval
- `is_trashed.isnot(True)` — always, not `== False`
- All outputs to `C:\Users\Pankaj.sehgal\Claude\output\`
- Audit log before `db.delete()` — record is gone after commit

## Project Skills (`.claude/skills/`)
Invoke these for this project's recurring workflows — they encode patterns
already proven to catch real bugs in this codebase:
| Skill | When to use |
|---|---|
| `verify-feature` | After any router/template change, before saying "done" — compile + Jinja-parse + `db_validator.py` + throwaway httpx functional script |
| `regression-sweep` | Before reporting a batch done if it touched `base.html`, a Jinja global, or a shared macro — full 159-template parse + `_scan_routes.py` (baseline: exactly 9 flagged routes) |
| `bulk-csv-import` | Any CSV import of devices/dealers/inventory — always ask local-vs-production, verify encoding fallback, report exact skip counts |
| `commit-deploy-gate` | Before any `git commit`/`push`/server restart — a prior "yes" never carries forward to a new batch |
| `custom-role-permissions` | Any button/section gated on `current_user.role` — use `role_allowed()` global, never hardcode a role list (custom roles like `trc_manager` will silently fail) |
| `ajax-partial-refresh` | User asks that a page "not fully reload" after a form submit — AJAX + DOMParser swap pattern, with rebind-handlers discipline |
| `server-side-financial-snapshot` | Any priced-document feature (quotations, invoices) — server-side Decimal totals, snapshot referenced records as columns, reuse `AppSetting` for settings |
