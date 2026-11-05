"""QA › Journey Diagrams (2026-11-05): database diagrams + user journeys.

MODULES: one entry per module, in display order — its diagram SVG under
static/qa/journeys/ and its user journey (steps left to right).
COMBINED_PHASES: the single end-to-end journey; each phase lists
(module_key, step_index) pairs pointing into MODULES' steps.

Each step: (step, touchpoint, actions, outputs, data carried forward).
Generated from the app models at commit 7b2eda1; edit the text here.
"""

MODULES = [
    {"key": 'inv', "name": 'Inventory & Lots', "color": '#2F6DB5', "svg": 'inventory.svg', "steps": [
        ('Purchase lot', 'Stock Inwards › Lots › Add Lot', 'Create the lot from the supplier invoice; add line items (model, CPU, RAM, storage, qty)', 'Lot LOT-xxx with its line items', 'lots.id, lot_number, supplier_name, invoice_no, lot_line_items'),
        ('Goods receipt (GRN)', 'GRN › Add / Edit GRN', 'Check physical receipt against the invoice; upload invoice and e-way bill; vendor state sets tax', 'GRN linked to the lot', 'grn_imports.id, grn_number, invoice_number, quantity, location_id'),
        ('Tag devices', 'GRN Detail › Upload Tag / Stocked Tags', 'Create one barcode tag per unit and match it to the invoice', 'Device records, stage = IQC', 'devices.barcode, lot_id, lot_line_item_id, entity'),
        ('Store', 'Location Master / scan', 'Put the device on a rack or crate in a zone', 'Location log; device location set', 'devices.location_id, storage_locations.zone'),
        ('Move stock', 'Transfers / Buckets', 'Move devices between warehouses, stages or buckets; receiver validates', 'Transfer and validation records', 'stock_transfers.transfer_type, to_location_id, received_by'),
        ('Block or dispatch', 'Block Tags / Telecaller Dispatch', 'Block tags for a customer; approve telecaller dispatch requests', 'Block record; dispatch approved', 'block_id, customer_name, dispatch status'),
        ('Audit or scrap', 'Location Audit / Scrap for Sale', 'Scan-audit a zone; move unrecoverable units to scrap and sell them', 'Audit scans; scrap sale invoice', 'scan_status, scrap_id, sale_number'),
    ]},
    {"key": 'q', "name": 'Quality & Repair', "color": '#C9771E', "svg": 'quality.svg', "steps": [
        ('Incoming check (IQC)', 'OxyQC app / IQC page', 'Inspect power, BIOS, screen, keyboard, battery and body against the checklist', 'IQC inspection; device routed to L1/L2/L3 repair or Ready', 'iqc_inspections.device_id, all_ok, defect fields'),
        ('Estimate', 'Part Estimate', 'Estimate parts and labour for the lot', 'Estimate with part, checklist and open lines', 'part_estimates.lot_id, parts_total_cost, labour_cost, total_estimation'),
        ('Assign work', 'Production Manager', 'Create a work order for an engineer and stage', 'Work order WORK-ID', 'work_orders.work_id, barcode, stage, assigned_user_id'),
        ('Repair', 'Repair (L1 / L2 / L3)', 'Diagnose, fix, request parts', 'Repair job; part requests', 'repair_jobs.engineer_id, issue_description, resolution, part_requests.part_id'),
        ('Source missing parts', 'Part Sourcing', 'Raise a sourcing request when the part is out of stock', 'Sourcing request', 'part_sourcing_requests.qty_requested, status, source_deal_id'),
        ('Stress test', 'Stress test tool', 'Run a burn-in test', 'Test result with PDF report', 'stress_test_results.barcode, overall_status, pdf_path'),
        ('Final QC', 'QC page', 'Score battery, screen, keyboard and body; set the grade', 'QC check; device moves to Ready to Sale', 'qc_checks.total_score, result, grade'),
    ]},
    {"key": 'p', "name": 'Spare Parts', "color": '#3E8E41', "svg": 'parts.svg', "steps": [
        ('Parts master', 'Spare Parts', 'Maintain part codes, prices and minimum stock', 'Spare part record', 'spare_parts.part_code, unit_price, qty_in_stock'),
        ('Receive parts', 'Parts GRN', 'Receive purchased parts against the PO and invoice', 'Parts GRN with line items; stock up', 'parts_grn.grn_number, po_number, line part_id, qty'),
        ('Issue to repair', 'Part Requests (from Repair)', 'Issue a part to a device on a repair job', 'Consumption and ledger entry; stock down', 'spare_parts_consumption.device_id, repair_job_id, qty_used, total_cost'),
        ('Mark PNA / harvest', 'Device PNA', 'Mark parts not available or harvested on a device', 'PNA record until cleared', 'device_pna_parts.barcode, part_name, source, cleared_at'),
        ('Move RAM', 'RAM tracking', 'Move RAM from one device to another', 'RAM movement record', 'ram_tracking.device_id, destination_device_id, ram_gb'),
        ('Sell parts', 'Part Sale Requests / Part Sales', 'Request and sell parts to a buyer', 'Part sale invoice', 'part_sales.sale_number, qty, sale_unit_price'),
    ]},
    {"key": 'pr', "name": 'Products & Pricing', "color": '#7B4FB0', "svg": 'product.svg', "steps": [
        ('Hardware prices', 'Hardware page', 'Set RAM and storage prices', 'RAM / storage price list', 'hw_ram.ram_price, hw_storage.storage_price'),
        ('Product master', 'Product Master', 'Define each SKU (make, model, CPU, RAM, storage)', 'Product with 5-digit SKU', 'pm_products.sku, ram_id, storage_id'),
        ('New model request', 'Model Requests', 'Ask for a new model to be added; admin approves', 'Request, then product', 'model_requests.brand, model, cpu, status'),
        ('Price a list', 'Pricing Calculator (upload)', 'Upload a sheet; the app computes prices', 'Calculated price rows', 'pm_calc_uploads.id, pm_calc_rows.sku, price'),
        ('Competitor prices', 'Competitive Lists', 'Record competitor website prices per product', 'Competitor price records', 'pm_competitor_prices.product_id, list_id, price'),
        ('Market and P&L', 'Market Availability / Business P&L', 'Capture dealer stock from WhatsApp; adjust monthly P&L figures', 'Market rows; P&L overrides', 'market_availability.dealer_id, model; business_pl_overrides.revenue_override'),
    ]},
    {"key": 'crm', "name": 'CRM', "color": '#1F7A93', "svg": 'crm.svg', "steps": [
        ('Create account', 'CRM Contacts › Add Account', 'Add a supplier or buyer with locations, persons and team mapping', 'Account, status Inactive', 'crm_contacts.id, contact_type, source / buyer type, gstin'),
        ('KYC and credit', 'Account page / Accounts Dashboard', 'Finance verifies KYC; Management verifies credit', 'Account becomes Active', 'kyc_verified, credit_verified, status'),
        ('Sourcing deal', 'Sourcing Deals', 'Create deal; inspection; supplier quote; negotiation with L1/L2 approvals', 'Sourcing deal SD-xxx with steps and quotes', 'crm_sourcing_deals.deal_number, stage, crm_quotes.sourcing_deal_id'),
        ('Purchase order', 'Sourcing Deal › PO Line Items › Generate PO', 'Build the PO from the vendor document', 'PO-xxx with line items; Accounts verifies', 'crm_purchase_orders.po_number, total_amount, offer_total'),
        ('Buyer deal and quote', 'Buyer Deals / Quotes', 'Create deal; raise quotes; Level 1 and Level 2 approval', 'Buyer deal OPP-xxx with quotes', 'crm_sales_opportunities.opp_number, crm_quotes.quote_number, total_amount'),
        ('Sales order', 'Automatic on Quotation Level 2 approval', 'Snapshot the latest quote as a Sales Order', 'SO-xxx; Accounts verifies', 'crm_sales_orders.so_number, quote_id, deal_id'),
        ('Payment and close', 'Accounts › Supplier Payments / Customer Receipts', 'Record payment against PO or SO; Accounts verifies; close deal Won or Lost', 'Verified payment; closed deal', 'supplier_payments.po_id, customer_receipts.so_id, amount, verified'),
        ('Leads and calls', 'Assign Leads / Telecalling', 'Call leads, log outcomes, set follow-ups', 'Lead calls; activities', 'crm_leads.lead_id, crm_lead_calls.outcome, followup_date'),
    ]},
    {"key": 's', "name": 'Sales & Dealers', "color": '#B24A6D', "svg": 'sales.svg', "steps": [
        ('Onboard dealer', 'Dealers', 'Add a dealer, link the CRM account, assign to a telecaller', 'Dealer DLR-xxx; assignment', 'dealers.dealer_code, crm_contact_id, dealer_assignments.assigned_to'),
        ('Call dealer', 'Dealer Calls', 'Log the call, items discussed and quote given', 'Call record', 'dealer_calls.call_outcome, quote_given'),
        ('Quote dealer', 'Dealer Quotations', 'Generate a quotation PDF', 'Dealer quotation with items', 'dealer_quotations.quote_number, items total'),
        ('Sell a tag', 'Ready to Sale › Sell / New Tag Sale', 'Sell a device; issue the invoice from the company entity', 'Sale and invoice; device Sold', 'sales.sale_number, device_id, sale_price, invoice_no'),
        ('Orders and credit', 'Dealer Orders', 'Track dealer orders, dues and credit notes', 'Order; credit note', 'dealer_orders.order_number, due_amount; dealer_credit_notes.credit_number'),
        ('Rent', 'Rentals', 'Rent devices; receive returns; bill extra days', 'Rental record; return', 'rental_records.rental_number, rental_returns.daily_rate, outstanding_amount'),
        ('Process a return', 'Returns', 'Receive a sale return, re-enter the stage, refund', 'Return record; device back in stock', 'returns.sale_id, action_taken, reentered_stage, refund_amount'),
    ]},
    {"key": 'tp', "name": 'Trade Partners', "color": '#A7801A', "svg": 'partner.svg', "steps": [
        ('Publish listing', 'Partner Admin › Listings', 'Publish a lot or devices for partners with a floor price', 'Listing with devices', 'partner_listings.listing_code, qty_total, partner_floor_config'),
        ('Choose dealers', 'Lot Dealer Visibility', 'Pick which dealers can see the lot', 'Visibility rows', 'lot_dealer_visibility.lot_id, dealer_id'),
        ('Log in and browse', 'Partner Portal (OTP login)', 'Dealer signs in and views listings', 'Login log; listing views', 'partner_login_logs.dealer_id, partner_listing_views.viewed_at'),
        ('Bid or book', 'Partner Portal', 'Place a bid, or book a quantity with a token amount', 'Bid; booking', 'partner_bids.bid_amount, partner_bookings.booking_number, token_total'),
        ('Mark winner', 'Partner Admin › Bids › Mark Won', 'Mark the winning bid; upload documents', 'Won bid; opens a Buyer Deal', 'partner_bids.status, won_by, partner_bid_documents.doc_type'),
        ('Pay', 'Partner Portal / Admin verify', 'Partner submits UTR or proof; admin verifies', 'Verified bid payment', 'partner_bid_payments.payment_utr, payment_amount, verified'),
    ]},
    {"key": 'c', "name": 'Care & Tickets', "color": '#2E7D6B', "svg": 'care.svg', "steps": [
        ('Warranty starts', 'On sale', 'Warranty is created for the sold device', 'Warranty record', 'care_warranties.device_id, sale_id, expiry_date'),
        ('Pair device', 'OxyPC Care agent (Windows)', "Customer's device pairs using a one-time token", 'Device pairing', 'care_device_pairings.serial_no, device_token, paired_at'),
        ('Raise support ticket', 'Care agent / Care admin', 'Customer raises a ticket; diagnostics are captured', 'Support ticket with diagnostic snapshot', 'care_support_tickets.ticket_number, category, priority'),
        ('Resolve', 'Care admin', 'Assign, update status, add notes', 'Ticket events; resolved ticket', 'care_ticket_events.old_status, new_status, actor_id'),
        ('Send offers', 'Care Offers', 'Push offers to paired devices', 'Offer deliveries', 'care_offers.target_type, care_offer_deliveries.delivery_status'),
        ('Staff tickets', 'Raise Ticket (top bar)', 'Staff report an app issue with photos', 'Internal ticket', 'tickets.ticket_id, status, feedback'),
    ]},
    {"key": 'u', "name": 'Users & Access', "color": '#55566A', "svg": 'people.svg', "steps": [
        ('Create user', 'Admin › User Management', 'Add the user with role, manager and reports-to', 'User account', 'users.username, role, manager_username, reports_to'),
        ('Set permissions', 'Admin › Permission Matrix', 'Set Enable, Pricing, Add, Edit, Delete, Upload, Export per module and role', 'Role permissions; custom roles', 'role_module_permissions.module, can_* flags'),
        ('Per-user grants', 'Admin › User Permissions', 'Grant one person an extra permission', 'User permission', 'user_permissions.permission, granted_by'),
        ('Sign in', 'Login page', 'User signs in; session starts', 'Login log', 'login_logs.user_id, ip_address, timestamp'),
        ('Attendance', 'Attendance', 'Check in and out; group managers review', 'Attendance rows', 'attendance.check_in, check_out, status'),
    ]},
    {"key": 'sy', "name": 'System & Settings', "color": '#7A6A45', "svg": 'system.svg', "steps": [
        ('Dropdown lists', 'Admin › Master Data', 'Maintain dropdown lists (zones, warehouses, payment modes...)', 'Master data values used by forms', 'master_data.category, value, is_active'),
        ('Company and terms', 'Company Settings / Terms & Conditions', 'Set billing entities and document terms', 'Companies; terms', 'companies.company_gstin; terms_conditions.term_type'),
        ('App settings', 'Admin settings', 'Feature flags, sidebar visibility, one-time migrations', 'App setting keys', 'app_settings.key, value'),
        ('Audit trail', 'Every add / edit / delete', 'Write an audit entry before the change', 'Audit log', 'audit_logs.user_id, action, table_name, old_value, new_value'),
        ('Notify and integrate', 'Notifications / Webhooks / API keys', 'Notify users; publish events to webhooks', 'Notifications; event log', 'notifications.user_id; event_log.event_type, payload'),
        ('WhatsApp', 'WhatsApp', 'Sessions, groups, broadcasts and messages to dealers', 'Messages; feeds market availability', 'whatsapp_messages.recipient_phone, dealer_id, reference_id'),
        ('Stock audit and costing', 'Audit Records / device costing', 'Count stock physically; track cost and age per device', 'Audit record; costing; aging', 'audit_records.physical_count; device_costing.total_cost; device_aging.days_in_stage'),
        ('QA / UAT', 'QA page', 'Requirements, test cases, defects and releases', 'QA records', 'qa_requirements.req_code, qa_test_cases.tc_code, status'),
    ]},
]

COMBINED_PHASES = [
    {"label": 'Setup', "name": 'Runs before and alongside everything', "steps": [('u', 0), ('u', 1), ('u', 2), ('u', 3), ('u', 4), ('sy', 0), ('sy', 1), ('sy', 2), ('sy', 3), ('sy', 4), ('sy', 5), ('sy', 6), ('sy', 7)]},
    {"label": 'Phase 1', "name": 'Source the stock', "steps": [('crm', 0), ('crm', 1), ('crm', 2), ('crm', 3)]},
    {"label": 'Phase 2', "name": 'Receive and store', "steps": [('inv', 0), ('inv', 1), ('inv', 2), ('inv', 3)]},
    {"label": 'Phase 3', "name": 'Set up products', "steps": [('pr', 0), ('pr', 1), ('pr', 2)]},
    {"label": 'Phase 4', "name": 'Inspect, repair and QC', "steps": [('q', 0), ('q', 1), ('q', 2), ('q', 3), ('q', 4), ('p', 0), ('p', 1), ('p', 2), ('p', 3), ('p', 4), ('q', 5), ('q', 6)]},
    {"label": 'Phase 5', "name": 'Handle stock', "steps": [('inv', 4), ('inv', 5), ('inv', 6)]},
    {"label": 'Phase 6', "name": 'Price the Ready stock', "steps": [('pr', 3), ('pr', 4), ('pr', 5)]},
    {"label": 'Phase 7', "name": 'Find buyers: leads and dealers', "steps": [('crm', 7), ('s', 0), ('s', 1), ('s', 2)]},
    {"label": 'Phase 8', "name": 'Sell through trade partners', "steps": [('tp', 0), ('tp', 1), ('tp', 2), ('tp', 3), ('tp', 4), ('tp', 5)]},
    {"label": 'Phase 9', "name": 'Sell: buyer deal to invoice', "steps": [('crm', 4), ('crm', 5), ('s', 3), ('s', 4), ('s', 5)]},
    {"label": 'Phase 10', "name": 'Payment and close', "steps": [('crm', 6)]},
    {"label": 'Phase 11', "name": 'After-sales', "steps": [('c', 0), ('c', 1), ('c', 2), ('c', 3), ('c', 4), ('c', 5), ('s', 6), ('p', 5)]},
]


def module_map():
    return {m["key"]: m for m in MODULES}


def combined_columns():
    """Flatten COMBINED_PHASES into numbered columns for the combined table."""
    mm, cols, n = module_map(), [], 0
    for pi, ph in enumerate(COMBINED_PHASES):
        for j, (mk, si) in enumerate(ph["steps"]):
            n += 1
            m = mm[mk]
            cols.append({"n": n, "phase": pi, "first": j == 0, "module": m["name"],
                         "color": m["color"], "step": m["steps"][si]})
    return cols
