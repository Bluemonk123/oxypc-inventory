# Pricing permission sweep (started 2026-11-03)

Pricing View / Pricing Add / Pricing Export per Permission Matrix module.
View = `can(user, module, 'pricing')` / `has_perm(role, module, "pricing")`.
Add = action `pricing_add` (route_actions + buttons). Export = `utils/price_export.strip_price_columns`.

## Done (batch 1)
- Three ticks in the matrix (can_pricing_add / can_pricing_export columns, NULL = follows Pricing View).
- route_actions: the 15 price-changing routes -> pricing_add; their buttons gated (Ready to Sale set price,
  As-Is lot price, scrap for sale price, listings reprice, floors create, sourcing offer price, price matrix add/edit,
  product master discount/method/hardware/pricing/spare parts save, cost config save).
- Exports stripped without Pricing Export: devices export, rental tags CSV, sales list export (filtered + selected),
  credit note export, lots export, part sales export, Lot P&L / Business P&L / lot tag exports, sales report export,
  receivables CSV, telecalling calls CSV.
- Leaks: /sales/ready/data Min/Max cells and /sales/data revenueFiltered now omitted without Pricing View.

## To do (from the 2026-11-03 inventory)
### View (ungated on screen)
- finance / finance_supplier / finance_customer: accounts/index, supplier_payments, customer_receipts (+ accounts.py:215, 367 feeds)
- cost_config page (no Pricing View check)
- crm_contacts profile (credit limit, outstanding, pipeline), crm/procure dashboards, crm analytics funnel
- crm_sales_opp tile + ready_list opportunities Budget/Unit; crm_sourcing tile + profitability calculator
- crm_quotes print + WhatsApp share text; crm_purchase_orders print
- dealers: ageing, ledger, list Outstanding tile, order invoice, overdue total, profile pay / credit-note modals; whatsapp compose Amount Due
- deals/list items data + view modal; trade partner bids/bookings/my desk/listings/partner payments/manage lots extras
- devices list lot buying_price in JS; devices.py search-tags device_price
- grn form/import/post_iqc data-amount; grn/_tabs uses 'devices' key (should be grn_post_iqc)
- iqc form line-item unit price; lots detail line items + grn_register; stock_in verify JS costs
- part_estimation whole page; parts sale_new autofill; spare parts Total Stock Value tile, parts_grn_form read-only values
- pm_* pages (no Pricing View anywhere); hardware client CSV
- rentals list/form/print/return receipt; returns list/receipt/form; sales new / new tag sale lot cost
- scrap list device price modal, scrap sale report popup; telecalling records budget
- report_receivables page; dashboard Weekly Sourcing Price chart
- JSON feeds: lots line-items, api lot-line-item(s), iqc-api lot, rentals tag payload/summary, part_sales new, part-requests handover-options

### Add (money-writing routes not on pricing_add / inputs ungated)
- devices edit, iqc new/customise/create-lot, lots new/edit/line-items/register-device, grn create/edit,
  stock verify, spare parts new/edit/upload/harvest, parts purchase / parts GRN, part sales, sales new,
  returns, rentals, scrap custom sale, crm sourcing/sales opp/quotes/PO/price matrix upload/contacts,
  dealers (credit limit, quote given, orders, pay, credit notes, quotations), deals new, listings create/publish,
  floors inputs, bids generate PO, market, telecalling add, accounts payments/receipts, repair complete cost

### Export (money columns not stripped)
- iqc export-csv (device_price), lots line-items export (unit_price), dealers ageing CSV + statement.csv,
  crm contacts export (Credit Limit, Outstanding), repair summary xlsx (spare parts + workid status),
  part estimation workbooks + download, quote print/download, PO print, sourcing PO, invoice print,
  rental print, hardware client CSV, parts consumption summary (uses View, should use Export)
