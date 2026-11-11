"""Download column on CRM lists (2026-11-10, Pankaj): documents show only once
approved; one file = direct button, several = dropdown; prints use the From
Entity's Company Setting company."""
import uuid

from tests.test_iqc_new_user import _login, make_user  # noqa: F401  (fixture)
from tests.test_quotations_v2 import _sql


def test_download_rules_and_entity_company(app_client, make_user):  # noqa: F811
    t = uuid.uuid4().hex[:6].upper()
    comp, qid, did, poid, soid = (str(uuid.uuid4()) for _ in range(5))
    _sql("insert into companies (id, company_name, company_entity, company_gstin, is_active, created_at) "
         "values (:i, :n, 'Renew Circuits', :g, true, now())", {"i": comp, "n": f"IT Co {t}", "g": f"GST{t}"})
    _sql("insert into crm_quotes (id, quote_number, quote_date, total_amount, status, payment_status, from_entity) "
         "values (:i, :n, current_date, 10, 'added', 'pending', 'Renew Circuits')", {"i": qid, "n": f"QT-DEAL-IT{t}"})
    _sql("insert into crm_sourcing_deals (id, deal_number, title, stage, from_entity) values (:i, :n, 'IT', 'purchase_order', 'Renew Circuits')",
         {"i": did, "n": f"SD-IT{t}"})
    _sql("insert into crm_purchase_orders (id, po_number, po_date, total_amount, status, payment_status, deal_id, from_entity) "
         "values (:i, :n, current_date, 10, 'issued', 'pending', :d, 'Renew Circuits')", {"i": poid, "n": f"PO-IT{t}", "d": did})
    _sql("insert into crm_sales_orders (id, so_number, quote_id, so_date, total_amount, status, verified) "
         "values (:i, :n, :q, current_date, 10, 'open', false)", {"i": soid, "n": f"SO-IT{t}", "q": qid})
    try:
        username, password = make_user("admin")
        _login(app_client, username, password)
        # not approved yet: no download link for the quote / PO
        page = app_client.get("/crm/quotes").text
        assert f"/crm/quotes/{qid}/print\" class=\"btn btn-sm btn-outline-success" not in page
        page = app_client.get("/crm/purchase-orders").text
        assert f"/crm/purchase-orders/{poid}/print\" class=\"btn btn-sm btn-outline-success" not in page
        # approve: quote status + the deal's PO Level 1
        _sql("update crm_quotes set status = 'approved' where id = :i", {"i": qid})
        _sql("insert into crm_sourcing_steps (id, deal_id, stage, step_key, status, created_at, updated_at) "
             "values (gen_random_uuid(), :d, 'purchase_order', 'po_l1', 'approved', now(), now())", {"d": did})
        page = app_client.get("/crm/quotes").text
        assert f"/crm/quotes/{qid}/print" in page and "Download Quote" in page
        page = app_client.get("/crm/purchase-orders").text
        assert f"/crm/purchase-orders/{poid}/print" in page and "Download Generated PO" in page
        page = app_client.get("/crm/sales-orders").text
        assert f"/crm/sales-orders/{soid}/print" in page
        # prints carry the From Entity's company
        for url in (f"/crm/quotes/{qid}/print", f"/crm/purchase-orders/{poid}/print", f"/crm/sales-orders/{soid}/print"):
            r = app_client.get(url)
            assert r.status_code == 200 and f"IT Co {t}" in r.text and f"GST{t}" in r.text, url
    finally:
        _sql("delete from crm_sales_orders where id = :i", {"i": soid})
        _sql("delete from crm_purchase_orders where id = :i", {"i": poid})
        _sql("delete from crm_sourcing_steps where deal_id = :i", {"i": did})
        _sql("delete from crm_sourcing_deals where id = :i", {"i": did})
        _sql("delete from crm_quotes where id = :i", {"i": qid})
        _sql("delete from companies where id = :i", {"i": comp})
