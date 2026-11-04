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

## Done (batch 2 — remaining exports)
- Exports: IQC export, lot line items, CRM contacts, repair summary xlsx, hardware client CSV, parts consumption summary.
- Priced documents blocked without Pricing Export: dealer statement + ageing CSV, quote print/download, PO print,
  sourcing PO, invoice print, rental print, part estimate download.
- One-time reset `pricing_lockdown_v1` (main.py): all three ticks off for every role except admin;
  Pricing Add on for spare_parts_manager, inventory_manager, counter_sales.

## Done (batch 3 — Pricing Add + Pricing View sweeps, 2026-11-03 night)
### Pricing Add
- route_actions may hold several ticks: ('sales', ('add', 'pricing_add')) — action_guard checks each.
  Money transactions need both: New Sale, Parts Sale, Rental, Scrap custom sale / sell, supplier payment,
  customer receipt (+ bulk), dealer order / payment / credit note / quotation, CRM quote (+ from lot), CRM PO,
  sourcing PO item, price-matrix upload, part estimates (4 generate routes), trade-partner listing create,
  deals create, market add/edit/from-message, spare parts purchase.
- templates_config `can()`: a module action whose every route needs pricing_add also needs it for the button
  (`_price_bound_actions`), so `can(u, 'part_sales', 'add')` hides New Parts Sale without Pricing Add.
  Sale buttons on Ready to Sale / As-Is lots / Block Tags, dealer order/pay/credit/quotation, sourcing PO items,
  quote/PO add buttons gated by hand. GET pages of New Sale / Parts Sale / Rental / Quote / PO / market edit
  redirect with a notice (`price_add_block`).
- Price-as-one-field records save without Pricing Add; the price is ignored (`price_unchanged` /
  `keep_price` / `credit_new` / `credit_edit`): device edit, IQC new + bulk apply + create lot, lot new/edit,
  line items add/edit/bulk CSV, register device (+ CSV), GRN create/edit (Invoice Price then not required),
  Parts GRN (Invoice Price + line prices), harvest, spare part new/edit/bulk upload, contact + dealer credit
  limit, dealer call quote/asking price, telecalling budget, CRM opp budget/value, sourcing asking/offer/final,
  return-stock verify costs, repair cost, returns paid repair / debit note (default amount used),
  bulk uploads (lots, devices, spare parts, leads, dealer calls) drop price columns (`strip_price_keys`).
- Inputs: `price_field_shown()` + `price_input_attrs()` — View+Add editable; View only read-only;
  Add only empty ("Hidden — type a new price"; blank keeps the stored value); neither: not shown / disabled.
  Hidden inputs no longer carry stored prices into the page.
### Pricing View
- Pages that are only amounts redirect back with a notice (`price_view_block`): dealer ageing, ledger, order
  invoice, overdue; receivables report; invoice print; customer receipts, supplier payments, receipts bulk
  upload; rental print; quote print; PO print; scrap sale report; pricing calculator; product pricing;
  PM spare parts; cost config; part estimation.
- Amounts show "—": lot line items, accounts tiles/tables, contact profile, dealer profile modals,
  GRN register, rental return receipt / list due, CRM + procure dashboard tiles, quote WhatsApp text,
  CRM funnel, product master money, sale page lot cost, Ready to Sale opportunities budget, bids/listings,
  opp/sourcing tiles, return receipt / list, Total Stock Value tile, telecalling budget, WhatsApp compose,
  dealer Outstanding tile, deals item modal, dashboard weekly price charts (data not sent either),
  parts GRN read-only values, data-* attributes (GRN amount, stock-in costs, sourcing items).
- JSON feeds (`blank_prices`): api lot-line-item(s), lots line-items, devices search-tags, market search,
  part-estimation matrices, trade-partner captured bids / bid payment / PO preview, partner payment details,
  rental summary / tag search / details / upload / return lookup + prefill, parts sale options,
  accounts payments/receipts data, hardware page data. GRN detail uses the GRN Post IQC key.

## Still open / decided against
- Partner portal pages (/partner/*) keep prices — they are the dealer-facing catalogue.
- Quote / PO / sale entry forms still prefill prices for roles with Pricing Add (they are entering them).
- iqc_api (OxyQC EXE, API key) unchanged.

## To do (from the 2026-11-03 inventory) — superseded by batch 3 above, kept for reference

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
