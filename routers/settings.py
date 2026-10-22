# routers/settings.py
"""Company Setting — admin can define multiple company profiles, each usable
as the "Company Details" on Quote/PO generation (Account, Buyer Deal, Supplier
Deal detail pages). Application Timezone used to live on this page — it now
lives on Attendance Config (routers/attendance_group_config.py) only."""
import uuid
from urllib.parse import quote
from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, update, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from templates_config import templates
from database import get_db
from auth.dependencies import get_current_user, verify_csrf
from models.user import User, UserRole
from models.company import Company
from utils.master_data import entity_values

router = APIRouter(prefix="/settings", tags=["settings"])


async def get_company_settings(db: AsyncSession, company_id: str = None) -> dict:
    """Load a company profile dict. If company_id is given, fetch that row;
    otherwise fall back to the first active company (back-compat for any
    caller that hasn't been updated to pass a specific company_id yet)."""
    company = None
    if company_id:
        try:
            company = await db.get(Company, uuid.UUID(company_id))
        except (ValueError, TypeError):
            company = None
    if not company:
        company = (await db.execute(
            select(Company).where(Company.is_active == True).order_by(Company.created_at)
        )).scalars().first()
    if not company:
        return {"name": "", "address": "", "gstin": "", "state": "", "state_code": "", "phone": "", "email": ""}
    return {
        "name": company.company_name or "",
        "address": company.company_address or "",
        "gstin": company.company_gstin or "",
        "state": company.company_state or "",
        "state_code": company.company_state_code or "",
        "phone": company.company_phone or "",
        "email": company.company_email or "",
    }


@router.get("", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.admin:
        return RedirectResponse(url="/?error=Admin+only", status_code=302)
    companies = (await db.execute(
        select(Company).where(Company.is_active == True).order_by(Company.created_at.desc())
    )).scalars().all()

    edit_company = None
    edit_id = request.query_params.get("edit")
    if edit_id:
        try:
            edit_company = await db.get(Company, uuid.UUID(edit_id))
        except (ValueError, TypeError):
            edit_company = None

    return templates.TemplateResponse("admin/settings.html", {
        "request": request, "current_user": current_user,
        "companies": companies, "edit_company": edit_company,
        "entity_choices": await entity_values(db),
        "success": request.query_params.get("success"),
    })


@router.post("")
async def save_company(
    request: Request,
    _csrf: None = Depends(verify_csrf),
    company_id: str = Form(""),
    company_name: str = Form(...),
    company_entity: str = Form(""),
    company_address: str = Form(""),
    company_gstin: str = Form(""),
    company_state: str = Form(""),
    company_state_code: str = Form(""),
    company_phone: str = Form(""),
    company_email: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.admin:
        return RedirectResponse(url="/?error=Admin+only", status_code=302)

    company = None
    if company_id.strip():
        try:
            company = await db.get(Company, uuid.UUID(company_id.strip()))
        except (ValueError, TypeError):
            company = None
        if not company:
            raise HTTPException(404, "Company not found")

    synced = None
    if company:
        # Old values, to find dealer quotations (they keep no company link).
        old_name, old_gstin = company.company_name, company.company_gstin
        company.company_name = company_name.strip()
        company.company_entity = company_entity.strip() or None
        company.company_address = company_address.strip() or None
        company.company_gstin = company_gstin.strip() or None
        company.company_state = company_state.strip() or None
        company.company_state_code = company_state_code.strip() or None
        company.company_phone = company_phone.strip() or None
        company.company_email = company_email.strip() or None
        synced = await _sync_documents(db, company, old_name, old_gstin)
        msg = "Company updated"
        if synced and sum(synced.values()):
            msg += " — existing documents updated: " + ", ".join(
                f"{k} {v}" for k, v in synced.items() if v)
        msg = quote(msg)
    else:
        company = Company(
            company_name=company_name.strip(),
            company_entity=company_entity.strip() or None,
            company_address=company_address.strip() or None,
            company_gstin=company_gstin.strip() or None,
            company_state=company_state.strip() or None,
            company_state_code=company_state_code.strip() or None,
            company_phone=company_phone.strip() or None,
            company_email=company_email.strip() or None,
            created_by=current_user.username,
        )
        db.add(company)
        msg = "Company+added"

    if synced is not None:
        from services.audit_engine import audit
        await audit(db, user=current_user, action="COMPANY_UPDATED_DOCS_SYNCED", table_name="companies",
                    record_id=str(company.id),
                    new_value={"company": company.company_name, "documents_updated": synced},
                    request=request)
    await db.commit()
    return RedirectResponse(url=f"/settings?success={msg}", status_code=302)


async def _sync_documents(db: AsyncSession, company: Company, old_name, old_gstin) -> dict:
    """Copy this company's current details onto every existing document that
    carries its own copy of them (2026-10-22, on request: an edit in Company
    Setting must correct ALL documents, past ones included).

    Sales (Tax Invoice / Delivery Challan), Rentals and Scrap Sales are linked
    by company id; rows with no company id but the company's OLD name+GSTIN are
    included too. Dealer quotations have no company link, so they're matched
    on the old name + GSTIN. Returns {document type: rows updated}."""
    from models.sales import Sale
    from models.rental import RentalRecord
    from models.scrap_sale import ScrapSale
    from models.dealer_quotation import DealerQuotation

    c = company

    def _match(id_col, name_col, gstin_col):
        by_old = and_(id_col.is_(None), name_col == old_name,
                      gstin_col.is_(None) if old_gstin is None else gstin_col == old_gstin)
        return or_(id_col == c.id, by_old)

    counts = {}
    r = await db.execute(update(Sale).where(_match(Sale.company_id, Sale.company_name, Sale.company_gstin))
                         .values(company_id=c.id, company_name=c.company_name, company_address=c.company_address,
                                 company_gstin=c.company_gstin, company_state=c.company_state,
                                 company_state_code=c.company_state_code, company_phone=c.company_phone,
                                 company_email=c.company_email)
                         .execution_options(synchronize_session=False))
    counts["Sales"] = r.rowcount or 0
    r = await db.execute(update(RentalRecord)
                         .where(_match(RentalRecord.billing_company_id, RentalRecord.billing_entity_name,
                                       RentalRecord.billing_entity_gstin))
                         .values(billing_company_id=c.id, billing_entity_name=c.company_name,
                                 billing_entity_address=c.company_address, billing_entity_gstin=c.company_gstin,
                                 billing_entity_state=c.company_state, billing_entity_phone=c.company_phone,
                                 billing_entity_email=c.company_email)
                         .execution_options(synchronize_session=False))
    counts["Rentals"] = r.rowcount or 0
    r = await db.execute(update(ScrapSale)
                         .where(_match(ScrapSale.company_id, ScrapSale.entity_name, ScrapSale.entity_gstin))
                         .values(company_id=c.id, entity_name=c.company_name, entity_address=c.company_address,
                                 entity_gstin=c.company_gstin, entity_state=c.company_state,
                                 entity_phone=c.company_phone, entity_email=c.company_email)
                         .execution_options(synchronize_session=False))
    counts["Scrap Sales"] = r.rowcount or 0
    dq_match = and_(DealerQuotation.company_name == old_name,
                    DealerQuotation.company_gstin.is_(None) if old_gstin is None
                    else DealerQuotation.company_gstin == old_gstin)
    r = await db.execute(update(DealerQuotation).where(dq_match)
                         .values(company_name=c.company_name, company_address=c.company_address,
                                 company_gstin=c.company_gstin,
                                 company_phone=(c.company_phone or None) and c.company_phone[:20],
                                 company_email=c.company_email)
                         .execution_options(synchronize_session=False))
    counts["Dealer Quotations"] = r.rowcount or 0
    return counts


@router.post("/{company_id}/delete")
async def delete_company(
    company_id: str,
    request: Request,
    _csrf: None = Depends(verify_csrf),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.admin:
        return RedirectResponse(url="/?error=Admin+only", status_code=302)
    try:
        cid = uuid.UUID(company_id)
    except ValueError:
        raise HTTPException(404, "Company not found")
    company = await db.get(Company, cid)
    if not company:
        raise HTTPException(404, "Company not found")
    company.is_active = False
    await db.commit()
    return RedirectResponse(url="/settings?success=Company+removed", status_code=302)
