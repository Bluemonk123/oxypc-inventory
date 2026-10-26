"""Per-route Permission-Matrix classification of every write route (POST/PUT/PATCH/DELETE).
Value: (module, action) where module is a PERM_MODULES key or a tuple of candidate keys
(resolve at request time, see trailing comment / route_actions_notes.md); None = not gated.
Rows listed under slowapi/extension.py are rate-limited endpoints whose real source file the
route scan attributed to the slowapi decorator wrapper."""

ROUTE_ACTIONS = {
    # ===== routers/api_v1/devices.py =====
    ('PATCH', '/api/v1/devices/{barcode}/stage'): None,  # move_device_stage: external REST API v1 (API-key auth), not a UI module
    # ===== routers/api_v1/iqc.py =====
    ('POST', '/api/v1/iqc/register'): None,  # register_device: external REST API v1 (API-key auth)
    # ===== routers/api_v1/api_keys.py =====
    ('POST', '/api/v1/api-keys'): None,  # create_api_key: API key management, admin-only API internals
    ('DELETE', '/api/v1/api-keys/{key_id}'): None,  # revoke_api_key: API key management, admin-only API internals
    # ===== routers/api_v1/webhooks.py =====
    ('POST', '/api/v1/webhooks'): None,  # create_webhook: webhook registration, API internals
    ('DELETE', '/api/v1/webhooks/{webhook_id}'): None,  # delete_webhook: webhook registration, API internals
    # ===== routers/api_v1/telecalling.py =====
    ('POST', '/api/v1/telecalling/assignments'): None,  # create_assignments: external REST API v1
    ('POST', '/api/v1/telecalling/calls'): None,  # create_call: external REST API v1
    # ===== slowapi/extension.py =====
    ('POST', '/auth/login'): None,  # login: login
    ('POST', '/fieldops/api/setup'): None,  # fieldops_setup_create: FieldOps sub-app, own auth (FieldOpsUser)
    ('POST', '/fieldops/api/auth/login'): None,  # fieldops_login: FieldOps login
    ('POST', '/tickets/raise-public'): None,  # raise_ticket_public: public ticket raise
    ('POST', '/partner/login'): None,  # login: partner portal login (external partners)
    ('POST', '/care/api/v1/pair'): None,  # pair: care device-agent API (token auth)
    ('POST', '/care/api/v1/tickets'): None,  # create_ticket: care device-agent API
    ('POST', '/care/api/v1/diagnostics'): None,  # submit_diagnostics: care device-agent API
    ('POST', '/care/api/v1/token/rotate'): None,  # rotate_token: care device-agent API
    # ===== routers/auth.py =====
    ('POST', '/auth/logout'): None,  # logout: logout
    ('POST', '/auth/profile'): None,  # profile_update: own profile update
    ('POST', '/auth/change-password'): None,  # change_password: own password change
    # ===== routers/fieldops.py =====
    ('GET', '/fieldops/api/admin/export'): None,  # export_all: FieldOps sub-app admin export (own require_admin)
    ('POST', '/fieldops/api/auth/logout'): None,  # fieldops_logout: FieldOps logout
    ('POST', '/fieldops/api/auth/password'): None,  # fieldops_change_password: FieldOps own password
    ('POST', '/fieldops/api/admin/users'): None,  # save_user: FieldOps sub-app admin (own require_admin)
    ('POST', '/fieldops/api/admin/users/reset-password'): None,  # reset_password: FieldOps sub-app admin
    ('DELETE', '/fieldops/api/admin/users/{user_id}'): None,  # delete_user: FieldOps sub-app admin
    ('POST', '/fieldops/api/admin/import'): None,  # import_all: FieldOps sub-app admin
    ('POST', '/fieldops/api/sync'): None,  # fieldops_sync: FieldOps mobile sync, own auth
    # ===== routers/admin.py =====
    ('GET', '/admin/backup-status'): None,  # backup_status: JSON status feed, system internal admin-only
    ('GET', '/admin/backup-list'): None,  # backup_list: JSON list, system internal admin-only
    ('GET', '/admin/backup-download/{filename}'): None,  # backup_download: DB backup file, system internal admin-only
    ('POST', '/admin/users/new'): ('admin_users', 'add'),  # create_user
    ('POST', '/admin/users/{user_id}/edit'): ('admin_users', 'edit'),  # update_user
    ('POST', '/admin/users/{user_id}/delete'): ('admin_users', 'delete'),  # delete_user
    ('POST', '/admin/users/{user_id}/reset-password'): ('admin_users', 'edit'),  # reset_password
    ('POST', '/admin/users/{user_id}/permissions'): None,  # save_permissions: per-user permission grants: admin-only permission page (privilege escalation risk)
    ('POST', '/admin/cost-config'): ('cost_config', 'pricing'),  # cost_config_save: cost config prices; already require_module_perm('cost_config','edit')
    ('POST', '/admin/backup-delete/{filename}'): None,  # backup_delete: DB backup file delete, system internal admin-only
    ('POST', '/admin/backup-now'): None,  # backup_now: DB backup, system internal admin-only
    # ===== routers/iqc.py =====
    ('GET', '/iqc/export-csv'): ('iqc', 'export'),  # iqc_export_csv: IQC Line Items Export CSV
    ('POST', '/iqc/create-lot-from-selection'): ('iqc', 'add'),  # iqc_create_lot_from_selection: creates a new Lot from selected IQC tags
    ('POST', '/iqc/bulk-apply-grade-type'): (('iqc', 'devices', 'stock'), 'edit'),  # iqc_bulk_apply_grade_type: shared Customise modal; module from form field return_to (/iqc|/devices|/stock); also writes Device Price / Min Selling Price -> require 'pricing' too when those fields are non-blank
    ('POST', '/iqc/new'): ('iqc', 'add'),  # iqc_create
    # ===== routers/stock.py =====
    ('GET', '/lots/export-all-csv'): ('lots', 'export'),  # export_all_lots_csv: Lot Overview Export All
    ('GET', '/lots/{lot_id}/line-items/export'): ('lots', 'export'),  # export_line_items_csv: lot detail line-items export
    ('GET', '/trc-production/summary/{tile}'): ('production_manager', 'export'),  # trc_production_tile_summary: Production Manager tile summary CSV (missed by path regex)
    ('GET', '/trc-production/pna-summary'): (('production_manager', 'part_estimation'), 'export'),  # trc_production_pna_summary: PNA tag summary CSV; linked from Production Manager and Part Estimation (missed by path regex)
    ('POST', '/lots/export-csv'): ('lots', 'export'),  # export_selected_lots_csv: export/download
    ('POST', '/lots/new'): ('lots', 'add'),  # create_lot
    ('POST', '/lots/{lot_id}/edit'): ('lots', 'edit'),  # edit_lot
    ('POST', '/lots/{lot_id}/delete'): ('lots', 'delete'),  # delete_lot_permanent: permanent lot delete
    ('POST', '/stock/flag'): ('stock', 'edit'),  # stock_flag_save: form action=save adds/edits flag, action=remove clears flag (flag only, not a record delete)
    ('POST', '/stock/return-stock/assign-bucket'): ('stock', 'edit'),  # return_stock_assign_bucket
    ('POST', '/stock/return-stock/{barcode}/verify'): ('stock', 'edit'),  # return_stock_verify
    ('POST', '/stock/return-stock/{barcode}/complete'): ('stock', 'edit'),  # return_stock_complete
    ('POST', '/stock/validate'): ('stock', 'edit'),  # stock_validate: records qty received / validates stock-in (a write, not validate-only)
    ('POST', '/stock/bulk-transfer'): ('stock', 'edit'),  # stock_bulk_transfer
    ('POST', '/stock/move-to-stock'): ('stock', 'edit'),  # move_to_stock
    ('POST', '/stock/move-to-trc'): ('stock', 'edit'),  # move_to_trc: also posted from GRN Records page
    ('POST', '/stock/move-to-l1l2'): ('stock', 'edit'),  # move_to_l1l2
    ('POST', '/stock/bulk-move-to-l1l2'): ('stock', 'edit'),  # bulk_move_to_l1l2
    ('POST', '/devices/change-engineer'): ('production_manager', 'edit'),  # change_engineer: Production Manager Change Engineer modal (path resolves to 'devices' by prefix - override)
    ('POST', '/scrap-products/{barcode}/move-to-inventory'): ('scrap_products', 'edit'),  # scrap_move_to_inventory
    ('POST', '/scrap-products/{barcode}/replace'): ('scrap_products', 'edit'),  # scrap_replace_with_another
    ('POST', '/lots/{lot_id}/line-items'): ('lots', 'add'),  # add_lot_line_item
    ('PUT', '/lots/{lot_id}/line-items/{item_id}'): ('lots', 'edit'),  # update_lot_line_item
    ('DELETE', '/lots/{lot_id}/line-items/{item_id}'): ('lots', 'delete'),  # delete_lot_line_item
    ('POST', '/lots/{lot_id}/line-items/bulk-csv'): ('lots', 'upload'),  # bulk_import_line_items
    ('POST', '/lots/{lot_id}/register-device'): ('lots', 'add'),  # register_device_barcode
    ('DELETE', '/lots/{lot_id}/register-device/{barcode}'): ('lots', 'delete'),  # delete_registered_device
    ('POST', '/lots/{lot_id}/register-bulk-csv'): ('lots', 'upload'),  # register_bulk_csv
    ('POST', '/lots/{lot_id}/advance-grn-to-iqc'): ('lots', 'edit'),  # advance_grn_to_iqc
    # ===== routers/repair.py =====
    ('POST', '/repair/l1l2-start'): (('repair_l1', 'repair_l2'), 'edit'),  # l1l2_start: module = 'repair_' + device.current_stage (l1|l2)
    ('POST', '/repair/l1/pick'): ('repair_l1', 'edit'),  # l1_pick: already has_perm(repair_l1,'add') in body
    ('POST', '/repair/bulk-part-request'): (('repair_l1', 'repair_l2'), 'add'),  # bulk_part_request: raises part requests from L1/L2 page; module by device stage (l1|l2)
    ('POST', '/repair/request-l3l4'): (('repair_l1', 'repair_l2'), 'add'),  # request_l3l4: creates L3/L4 WorkOrder from L1/L2 page; module by device.current_stage
    ('POST', '/repair/l1l2-complete-to-stress'): (('repair_l1', 'repair_l2'), 'edit'),  # l1l2_complete_to_stress: module by device.current_stage (l1|l2)
    ('POST', '/repair/back-to-production'): (('repair_l1', 'repair_l2'), 'edit'),  # back_to_production: module by device.current_stage (l1|l2)
    ('POST', '/repair/l3l4-start'): ('repair_l3', 'edit'),  # l3l4_start: L3/L4 page; repair_l3 is the permission key (repair_l3l4 is label-only)
    ('POST', '/repair/l3l4-pick'): ('repair_l3', 'edit'),  # l3l4_pick: repair_l3 is the L3/L4 permission key
    ('POST', '/repair/l3l4-complete'): ('repair_l3', 'edit'),  # l3l4_complete: repair_l3 is the L3/L4 permission key
    ('POST', '/repair/l3l4-scrap'): ('repair_l3', 'edit'),  # l3l4_scrap: scrap decision = workflow move; repair_l3 key
    ('POST', '/repair/start'): (('repair_l1', 'repair_l2', 'repair_l3'), 'add'),  # start_repair: module = repair_l{LEVEL_MAP[form stage]} (code already does this)
    ('POST', '/repair/complete'): (('repair_l1', 'repair_l2', 'repair_l3'), 'edit'),  # complete_repair: module = 'repair_' + RepairJob.stage (code already does this)
    ('POST', '/repair/move'): ('move_device_internal', 'edit'),  # move_device: Move Device Internal page /repair/move/form
    # ===== routers/qc.py =====
    ('POST', '/qc/new'): ('qc_check', 'add'),  # qc_submit: already require_module_perm('qc_check','add')
    # ===== routers/sales.py =====
    ('GET', '/sales/export-selected'): ('sales_list', 'export'),  # export_selected_get: Sales List export selected
    ('GET', '/sales/export'): ('sales_list', 'export'),  # export_sales_filtered: Sales List Export
    ('GET', '/returns/{return_id}/receipt'): ('returns', 'export'),  # return_receipt: return receipt document
    ('POST', '/sales/ready/as-is-lot/price'): ('sales', 'pricing'),  # set_as_is_lot_price
    ('POST', '/sales/ready/as-is-lot/edit'): ('sales', 'edit'),  # edit_as_is_lot
    ('POST', '/sales/ready/as-is-lot/open'): ('sales', 'edit'),  # open_as_is_lot: releases sub-lot (clears Device.sub_lot_number)
    ('POST', '/sales/ready/set-price'): ('sales', 'pricing'),  # set_ready_tag_price
    ('POST', '/sales/ready/bulk-set-price'): ('sales', 'pricing'),  # bulk_set_ready_tag_price
    ('POST', '/sales/ready/upload-tags'): None,  # upload_ready_tags: read-only tag lookup to tick rows
    ('POST', '/sales/new-tag-sale/tag-info'): None,  # sale_new_tag_info: read-only lookup
    ('POST', '/sales/new-tag-sale/prefill'): None,  # sale_new_tag_form_prefill: renders prefilled form (POST twin of GET)
    ('POST', '/sales/new/prefill'): None,  # sale_new_form_prefill: renders prefilled form (POST twin of GET)
    ('POST', '/sales/new/block'): None,  # sale_new_from_block: renders prefilled New Sale form from a block; already gated sales/add; the real write is /sales/new
    ('POST', '/sales/new'): ('sales', 'add'),  # create_sale: create sale; existing code gates require_module_perm('sales','add') (path prefix would say sales_list)
    ('POST', '/sales/change-date'): ('sales_list', 'edit'),  # change_sale_date
    ('POST', '/sales/bulk-delete'): ('sales_list', 'delete'),  # bulk_delete_sales
    ('POST', '/sales/export-selected'): ('sales_list', 'export'),  # export_selected_sales: export
    ('POST', '/sales/parse-invoice-pdf'): None,  # parse_invoice_pdf: parse/preview helper for the New Sale form
    ('POST', '/sales/{sale_id}/transport'): ('sales_list', 'edit'),  # sale_update_transport
    ('POST', '/returns/new'): ('returns', 'add'),  # process_return
    ('POST', '/returns/new/external'): ('returns', 'add'),  # process_external_return
    ('POST', '/returns/new/credit-note'): ('returns', 'add'),  # process_credit_note
    ('POST', '/credit-note/complete-old-tags'): ('credit_note', 'edit'),  # cn_complete_old_tags_apply
    ('POST', '/credit-note/{return_id}/advance'): ('credit_note', 'edit'),  # credit_note_advance
    ('POST', '/credit-note/{return_id}/delete'): ('credit_note', 'delete'),  # credit_note_delete
    ('POST', '/credit-note/bulk-delete'): ('credit_note', 'delete'),  # credit_note_bulk_delete
    ('POST', '/credit-note/bulk-verify'): ('credit_note', 'edit'),  # credit_note_bulk_verify
    ('POST', '/credit-note/export'): ('credit_note', 'export'),  # credit_note_export_selected: export
    ('POST', '/returns/{return_id}/approve'): ('returns', 'edit'),  # approve_return
    ('POST', '/returns/{return_id}/reject'): ('returns', 'edit'),  # reject_return
    # ===== routers/spare_parts.py =====
    ('GET', '/spare-parts/bulk-template'): ('spare_parts', 'export'),  # download_bulk_template: Parts Manager bulk-upload template
    ('POST', '/spare-parts/new'): ('spare_parts', 'add'),  # create_part
    ('POST', '/spare-parts/{part_id}/edit'): ('spare_parts', 'edit'),  # update_part
    ('POST', '/spare-parts/{part_id}/set-type'): ('spare_parts', 'edit'),  # set_part_type
    ('POST', '/spare-parts/{part_id}/delete'): ('spare_parts', 'delete'),  # delete_part
    ('POST', '/spare-parts/bulk-delete'): ('spare_parts', 'delete'),  # bulk_delete_parts
    ('POST', '/spare-parts/bulk-upload'): ('spare_parts', 'upload'),  # bulk_upload_parts
    ('POST', '/spare-parts/purchase'): ('spare_parts_purchase', 'add'),  # record_purchase
    ('POST', '/spare-parts/consume'): ('parts_consumption', 'add'),  # record_consumption
    ('POST', '/ram-tracking'): ('parts_tracking', 'add'),  # record_ram
    ('POST', '/spare-parts/{part_id}/procure'): ('spare_parts', 'add'),  # procure_from_master: raises a procurement request
    # ===== routers/reports.py =====
    ('GET', '/reports/daily-stock/export/overall'): ('report_daily_stock', 'export'),  # daily_stock_export_overall
    ('GET', '/reports/daily-stock/export/tags'): ('report_daily_stock', 'export'),  # daily_stock_export_tags
    ('GET', '/reports/daily-stock/export/location'): ('report_daily_stock', 'export'),  # daily_stock_export_location
    ('GET', '/reports/daily-stock/export/daily-report'): ('report_daily_stock', 'export'),  # daily_stock_export_daily_report
    ('GET', '/reports/daily-stock/report/{report_date}/workbook'): ('report_daily_stock', 'export'),  # daily_stock_saved_workbook: saved 9 PM report workbook (missed by path regex)
    ('GET', '/reports/daily-stock/report/{report_date}/{kind}'): ('report_daily_stock', 'export'),  # daily_stock_saved_report: saved 9 PM report CSV (missed by path regex)
    ('GET', '/reports/export/lot-pl'): ('reports', 'export'),  # export_lot_pl: Lot P&L export
    ('GET', '/reports/lot-pl/{lot_id}/export'): ('reports', 'export'),  # lot_pl_export_lot: single-lot P&L export
    ('GET', '/reports/business-pl/export-month'): ('report_bizpl', 'export'),  # business_pl_export_month
    ('GET', '/reports/business-pl/export-fy'): ('report_bizpl', 'export'),  # business_pl_export_fy
    ('GET', '/reports/export/sales'): (('report_sales', 'sales_list'), 'export'),  # export_sales: Sales Report export; also linked from Sales List
    ('GET', '/reports/overdue/csv'): ('report_overdue', 'export'),  # overdue_csv: Overdue Devices CSV
    ('POST', '/reports/export/lot-pl'): ('reports', 'export'),  # export_lot_pl_selected: export
    ('POST', '/reports/export/sales'): (('report_sales', 'sales_list'), 'export'),  # export_sales_selected: export
    ('POST', '/reports/business-pl/override'): ('report_bizpl', 'edit'),  # save_business_pl_override: manual P&L override figures
    # ===== routers/master.py =====
    ('GET', '/admin/master/export/{category}'): ('admin_master', 'export'),  # export_category_csv: Master Data category export
    ('GET', '/admin/master/template/{category}'): ('admin_master', 'export'),  # download_category_template: Master Data / Bulk Upload master template
    ('POST', '/admin/master/global-visibility/toggle'): None,  # toggle_global_visibility: admin-only master data / visibility config
    ('POST', '/admin/master/add'): None,  # add_master_value: admin-only master data
    ('POST', '/admin/master/{item_id}/edit'): None,  # edit_master_value: admin-only master data
    ('POST', '/admin/master/{item_id}/toggle'): None,  # toggle_master_value: admin-only master data
    ('POST', '/admin/master/{item_id}/delete'): None,  # delete_master_value: admin-only master data
    ('POST', '/admin/master/bulk-upload/{category}'): None,  # bulk_upload_category: admin-only master data
    ('POST', '/admin/master/permissions/save'): None,  # save_role_permissions: permission matrix itself
    ('POST', '/admin/master/permissions/save-additional'): None,  # save_additional_permissions: permission matrix itself
    ('POST', '/admin/master/permissions/save-pricing-visibility'): None,  # save_pricing_visibility: permission matrix itself
    ('POST', '/admin/master/permissions/save-sub-admin'): None,  # save_sub_admin_permissions: permission matrix itself
    ('POST', '/admin/master/permissions/add-role'): None,  # add_custom_role: role management, admin-only
    ('POST', '/admin/master/permissions/edit-role/{role_id}'): None,  # edit_custom_role: role management, admin-only
    # ===== routers/bulk_upload.py =====
    ('GET', '/bulk-upload/template/{upload_type}'): (('lots', 'iqc', 'telecalling', 'spare_parts', 'trade_partner_manage_lots'), 'export'),  # download_template: Bulk Upload / IQC / Manage Lots CSV template (module by upload_type)
    ('POST', '/bulk-upload/lots'): ('lots', 'upload'),  # upload_lots: /bulk-upload page has no nav key (alias iqc/devices); data owner = lots
    ('POST', '/bulk-upload/devices'): ('iqc', 'upload'),  # upload_devices: /bulk-upload alias (iqc, devices); IQC device import
    ('POST', '/bulk-upload/duplicates/approve-move'): ('iqc', 'edit'),  # approve_duplicate_move: duplicate-review modal of the device upload
    ('POST', '/bulk-upload/duplicates/update'): ('iqc', 'edit'),  # update_duplicate_device
    ('POST', '/bulk-upload/duplicates/update-all'): ('iqc', 'edit'),  # update_all_duplicate_devices
    ('POST', '/bulk-upload/duplicates/add-lot'): ('lots', 'add'),  # add_duplicate_lot: creates lot shell
    ('POST', '/bulk-upload/spare-parts'): ('spare_parts', 'upload'),  # upload_spare_parts
    ('POST', '/bulk-upload/leads'): ('telecalling', 'upload'),  # upload_leads: telecalling leads import
    # ===== routers/cosmetic.py =====
    ('POST', '/cosmetic/flow-data/save'): None,  # cosmetic_flow_data_save: Flow Data config; hardcoded admin/cosmetic_manager only, no matrix module (see notes s3)
    ('POST', '/cosmetic/flow-data/delete'): None,  # cosmetic_flow_data_delete: Flow Data config; hardcoded admin/cosmetic_manager only
    ('POST', '/cosmetic/advance'): (('cosmetic_received', 'cosmetic_cleaning', 'cosmetic_putty', 'cosmetic_dry_sanding', 'cosmetic_masking', 'cosmetic_painting', 'cosmetic_water_sanding', 'cosmetic_completed', 'cosmetic_finalqc'), 'edit'),  # advance_stage: module = PERM_MODULE_BY_STAGE[device.current_stage], or cosmetic_finalqc when current_stage == final_qc
    ('POST', '/cosmetic/final-qc/move-failed'): ('cosmetic_finalqc', 'edit'),  # fqc_move_failed
    ('POST', '/cosmetic/final-qc/move-passed'): ('cosmetic_finalqc', 'edit'),  # fqc_move_passed
    ('POST', '/cosmetic/final-qc/edit/{barcode}'): ('cosmetic_finalqc', 'edit'),  # fqc_edit_revert: reverts device back to final_qc
    ('POST', '/cosmetic/send-to-cosmetic'): ('cosmetic_received', 'add'),  # send_to_cosmetic: already require_module_perm('cosmetic_received','add')
    ('POST', '/cosmetic/revalidate-iqc'): ('cosmetic_finalqc', 'edit'),  # revalidate_iqc: already require_module_perm('cosmetic_finalqc','edit')
    ('POST', '/cosmetic/{barcode}/fail'): (('cosmetic_received', 'cosmetic_cleaning', 'cosmetic_putty', 'cosmetic_dry_sanding', 'cosmetic_masking', 'cosmetic_painting', 'cosmetic_water_sanding', 'cosmetic_completed'), 'edit'),  # cosmetic_fail_assign: module = PERM_MODULE_BY_STAGE[device.current_stage] (code already computes it)
    ('POST', '/cosmetic/{barcode}/paint-fail'): ('cosmetic_water_sanding', 'edit'),  # cosmetic_paint_fail: only valid at water_sanding
    ('POST', '/cosmetic/bulk-assign'): (('cosmetic_received', 'cosmetic_cleaning', 'cosmetic_putty', 'cosmetic_dry_sanding', 'cosmetic_masking', 'cosmetic_painting', 'cosmetic_water_sanding'), 'edit'),  # bulk_assign: module = PERM_MODULE_BY_STAGE[device.current_stage] per device (Received + 6 mid stages)
    # ===== routers/scrap.py =====
    ('POST', '/scrap/{barcode}/verify'): ('scrap_products', 'edit'),  # verify_scrap: alias scrap -> scrap_products
    ('POST', '/scrap/bulk-verify'): ('scrap_products', 'edit'),  # bulk_verify_scrap
    ('POST', '/scrap-products/{barcode}/set-replacement'): ('scrap_products', 'edit'),  # set_scrap_replacement
    # ===== routers/scrap_for_sale.py =====
    ('POST', '/scrap-for-sale/custom-sale'): ('scrap_for_sale', 'add'),  # create_custom_scrap_sale
    ('POST', '/scrap-for-sale/{scrap_id}/price'): ('scrap_for_sale', 'pricing'),  # set_scrap_for_sale_price
    ('POST', '/scrap-for-sale/{scrap_id}/sell'): ('scrap_for_sale', 'add'),  # sell_scrap_for_sale: creates the scrap sale
    ('POST', '/scrap-for-sale/{scrap_id}/delete'): ('scrap_for_sale', 'delete'),  # delete_scrap_for_sale
    # ===== routers/devices.py =====
    ('GET', '/devices/export'): (('devices', 'sales'), 'export'),  # export_devices: Inventory Search export; also Ready to Sale
    ('POST', '/devices/upload-tags'): None,  # device_upload_tags: read-only tag lookup
    ('POST', '/devices/export'): (('devices', 'sales'), 'export'),  # export_devices_selected: export
    ('POST', '/devices/{barcode}/edit'): ('devices', 'edit'),  # device_edit_save
    # ===== routers/transfers.py =====
    ('GET', '/transfers/export'): ('transfers', 'export'),  # export_transfers: Move Device export
    ('POST', '/transfers/bulk-delete'): ('transfers', 'delete'),  # bulk_delete_transfers
    ('POST', '/transfers/new'): ('transfers', 'add'),  # create_transfer
    ('POST', '/transfers/new/parts'): ('transfers', 'add'),  # create_parts_transfer
    ('POST', '/transfers/new/bucket'): ('transfers', 'add'),  # create_bucket_transfer
    ('POST', '/transfers/new/lot'): ('transfers', 'add'),  # create_lot_transfer
    # ===== routers/part_requests.py =====
    ('POST', '/part-requests/create'): ('devices', 'add'),  # create_part_request: raised from Device Detail page by engineers (alias part-requests -> spare_parts/parts_consumption would wrongly gate engineers)
    ('POST', '/part-requests/multi-create'): ('devices', 'add'),  # create_part_requests_batch: Device Detail page
    ('POST', '/part-requests/{req_id}/handover'): ('spare_parts', 'edit'),  # handover_part: Parts Manager list
    ('POST', '/part-requests/{req_id}/validate-receiving'): ('devices', 'edit'),  # validate_receiving: engineer confirms receipt on Device Detail
    ('POST', '/part-requests/{req_id}/not-in-stock'): ('spare_parts', 'edit'),  # not_in_stock
    ('POST', '/part-requests/{req_id}/procure'): ('spare_parts', 'edit'),  # procure_part: moves request to procurement
    ('POST', '/part-requests/{req_id}/scrap'): ('spare_parts', 'edit'),  # scrap_part_request
    ('POST', '/part-requests/{req_id}/warranty-replace'): ('spare_parts', 'edit'),  # warranty_replace
    ('POST', '/part-requests/{req_id}/warranty-received'): ('spare_parts', 'edit'),  # warranty_received
    ('POST', '/part-sourcing/{sr_id}/close'): ('spare_parts', 'edit'),  # close_sourcing: Parts Manager list / Procure dashboard
    ('POST', '/part-sourcing/{sr_id}/verify'): ('spare_parts', 'edit'),  # verify_sourcing
    ('POST', '/part-sourcing/{sr_id}/confirm'): ('spare_parts', 'edit'),  # confirm_production_sourcing
    ('POST', '/part-sourcing/{sr_id}/request-reupload'): ('spare_parts', 'edit'),  # request_sourcing_reupload
    ('POST', '/spare-parts/requests/bulk-action'): ('spare_parts', 'edit'),  # bulk_request_action: bulk handover/status action
    ('POST', '/devices/{barcode}/pna/clear-all'): ('devices', 'edit'),  # clear_all_pna
    ('POST', '/devices/{barcode}/pna'): ('devices', 'edit'),  # mark_part_pna: mark part not available
    # ===== routers/dispatch.py =====
    ('POST', '/inventory-requests/upload-tags'): None,  # upload_request_tags: read-only tag lookup
    ('POST', '/dispatch/request'): ('sales', 'add'),  # create_dispatch_request: raised from Ready to Sale page (templates/sales/ready_list.html)
    ('POST', '/dispatch/{req_id}/approve'): ('inventory_requests', 'edit'),  # approve_dispatch: existing approve_perm = inventory_requests/edit
    ('POST', '/dispatch/bulk-approve'): ('inventory_requests', 'edit'),  # bulk_approve_dispatch
    ('POST', '/dispatch/{req_id}/reject'): ('inventory_requests', 'edit'),  # reject_dispatch
    # ===== routers/model_requests.py =====
    ('POST', '/model-requests/new'): ('model_requests', 'add'),  # create_request
    ('POST', '/model-requests/{req_id}/fulfil'): ('model_requests', 'edit'),  # fulfil_request
    ('POST', '/model-requests/{req_id}/cancel'): ('model_requests', 'edit'),  # cancel_request: cancel = status change, record kept
    # ===== routers/attendance.py =====
    ('POST', '/attendance/checkin'): None,  # checkin: self check-in
    ('POST', '/attendance/checkout'): None,  # checkout: self check-out
    ('POST', '/attendance/mark'): ('attendance_report', 'edit'),  # mark_attendance: admin marks attendance for others
    # ===== routers/attendance_group_config.py =====
    ('POST', '/admin/attendance-config/timezone'): ('attendance_config', 'edit'),  # save_timezone
    ('POST', '/admin/attendance-config/create'): ('attendance_config', 'add'),  # create_group
    ('POST', '/admin/attendance-config/{group_id}/update'): ('attendance_config', 'edit'),  # update_group
    ('POST', '/admin/attendance-config/{group_id}/delete'): ('attendance_config', 'delete'),  # delete_group
    # ===== routers/dealers.py =====
    ('GET', '/dealers/bulk-upload-template'): (('dealers', 'assign_dealer_leads'), 'export'),  # dealers_bulk_upload_template: Dealers bulk upload + Assign Dealer Leads 'Download Sample'
    ('GET', '/dealers/calls/bulk-upload-template'): ('dealers', 'export'),  # dealer_calls_bulk_upload_template
    ('GET', '/dealers/{dealer_id}/orders/{order_id}/invoice'): (('dealers', 'report_receivables'), 'export'),  # dealer_order_invoice: dealer order invoice document; Dealer profile + Receivables report
    ('GET', '/dealers/{dealer_id}/statement.csv'): ('dealers', 'export'),  # dealer_statement_csv
    ('POST', '/dealers/export'): ('dealers', 'export'),  # export_dealers: export
    ('POST', '/dealers/bulk-delete'): ('dealers', 'delete'),  # bulk_delete_dealers
    ('POST', '/dealers/{dealer_id}/delete'): ('dealers', 'delete'),  # delete_dealer
    ('POST', '/dealers/bulk-upload'): ('dealers', 'upload'),  # dealers_bulk_upload_submit
    ('POST', '/dealers/calls/bulk-upload'): ('dealers', 'upload'),  # dealer_calls_bulk_upload
    ('POST', '/dealers/new'): ('dealers', 'add'),  # create_dealer
    ('POST', '/dealers/{dealer_id}/credit-notes/{cn_id}/apply'): ('dealers', 'edit'),  # apply_credit_note: applies CN balance to order due
    ('POST', '/dealers/{dealer_id}/edit'): ('dealers', 'edit'),  # update_dealer
    ('POST', '/dealers/{dealer_id}/quick-edit'): ('dealers', 'edit'),  # quick_edit_dealer
    ('POST', '/dealers/quick-new'): ('dealers', 'add'),  # quick_new_dealer
    ('POST', '/dealers/{dealer_id}/call'): ('dealers', 'add'),  # log_call: log call
    ('POST', '/dealers/{dealer_id}/calls/{call_id}/edit'): ('dealers', 'edit'),  # update_call
    ('POST', '/dealers/{dealer_id}/orders/new'): ('dealers', 'add'),  # create_order
    ('POST', '/dealers/{dealer_id}/orders/{order_id}/pay'): ('dealers', 'edit'),  # record_order_payment: records payment
    ('POST', '/dealers/{dealer_id}/orders/{order_id}/credit-note'): ('dealers', 'add'),  # create_credit_note
    # ===== routers/telecalling.py =====
    ('GET', '/telecalling/export-csv'): ('telecalling', 'export'),  # export_calls_csv
    ('POST', '/telecalling/add'): ('telecalling', 'add'),  # add_record
    # ===== routers/grn.py =====
    ('GET', '/grn/download/{grn_id}'): (('grn', 'grn_post_iqc'), 'export'),  # grn_download: GRN document; GRN with Invoice and GRN post IQC
    ('POST', '/grn/post-iqc/export'): ('grn_post_iqc', 'export'),  # grn_post_iqc_export: export
    ('POST', '/grn/map'): ('grn_post_iqc', 'edit'),  # grn_map: only on GRN post-IQC page
    ('POST', '/grn/records/bulk-move-to-trc'): ('grn_records', 'edit'),  # grn_bulk_move_to_trc
    ('POST', '/grn/upload'): (('grn', 'grn_post_iqc'), 'upload'),  # grn_upload: form field source: 'post_iqc' -> grn_post_iqc, else grn
    ('POST', '/grn/{grn_id}/validate'): ('grn', 'edit'),  # grn_validate
    ('POST', '/grn/{grn_id}/delete'): (('grn', 'grn_post_iqc', 'trade_partner_manage_lots'), 'delete'),  # grn_delete: GRNImport.source: external-partner test source -> trade_partner_manage_lots, form source=='post_iqc' -> grn_post_iqc, else grn
    ('POST', '/grn/create-manual'): (('grn', 'grn_post_iqc', 'trade_partner_manage_lots'), 'add'),  # grn_create_manual: form field source: external-partner test source -> trade_partner_manage_lots, 'post_iqc' -> grn_post_iqc, else grn
    ('POST', '/grn/{grn_id}/edit'): (('grn', 'grn_post_iqc', 'trade_partner_manage_lots'), 'edit'),  # grn_edit: GRNImport.source / form source, same rule as delete
    ('POST', '/grn/{grn_id}/add-lot'): (('grn_post_iqc', 'trade_partner_manage_lots'), 'add'),  # grn_add_lot: GRNImport.source external test -> trade_partner_manage_lots else grn_post_iqc
    ('POST', '/grn/{grn_id}/as-is-lot'): ('grn_post_iqc', 'edit'),  # grn_as_is_lot: bulk sub-lot/assignee write
    ('POST', '/grn/submit'): ('grn', 'add'),  # submit_grn: GRN for a lot (lots list/detail + GRN form)
    # ===== routers/entity_movement.py =====
    ('POST', '/entity-movement/change-entities'): ('entity_movement', 'edit'),  # change_entities
    # ===== routers/part_estimation.py =====
    ('GET', '/part-estimation/{lot_id}/iqc-data.xlsx'): ('part_estimation', 'export'),  # lot_iqc_data_xlsx
    ('GET', '/part-estimation/download/{estimate_id}'): (('part_estimation', 'spare_parts', 'crm_dashboard'), 'export'),  # download_estimate: estimate file; Part Estimation, Parts Manager, Procure dashboard
    ('POST', '/part-estimation/{lot_id}/generate-model-estimate'): ('part_estimation', 'add'),  # generate_model_estimate: creates estimate record
    ('POST', '/part-estimation/{lot_id}/generate-checklist-estimate'): ('part_estimation', 'add'),  # generate_checklist_estimate
    ('POST', '/part-estimation/{lot_id}/generate-open-estimate'): ('part_estimation', 'add'),  # generate_open_estimate
    ('POST', '/part-estimation/{lot_id}/generate'): ('part_estimation', 'add'),  # generate_estimate: stores estimate + raises production sourcing request
    ('POST', '/part-estimation/{estimate_id}/delete-file'): ('part_estimation', 'delete'),  # delete_estimate_file
    # ===== routers/rentals.py =====
    ('GET', '/rentals/returns/{return_id}/receipt'): ('rentals', 'export'),  # rental_return_receipt
    ('GET', '/rentals/{rental_id}/print'): ('rentals', 'export'),  # rental_print: printable rental document
    ('GET', '/rentals/{rental_id}/tags.csv'): ('rentals', 'export'),  # rental_tags_csv
    ('POST', '/rentals/tags/details'): None,  # rental_tag_details: read-only tag lookup
    ('POST', '/rentals/tags/upload'): None,  # rental_tag_upload: parses tag list for lookup (read-only)
    ('POST', '/rentals/new'): ('rentals', 'add'),  # rental_create
    ('POST', '/rentals/return/submit'): ('rentals', 'edit'),  # rental_return_submit
    ('POST', '/rentals/{rental_id}/paid'): ('rentals', 'edit'),  # rental_mark_paid
    ('POST', '/rentals/{rental_id}/return'): ('rentals', 'edit'),  # rental_return
    ('POST', '/rentals/{rental_id}/cancel'): ('rentals', 'edit'),  # rental_cancel: status change
    ('POST', '/rentals/{rental_id}/delete'): ('rentals', 'delete'),  # rental_delete: currently gated by rentals/edit (can_delete)
    # ===== routers/system_health.py =====
    ('POST', '/admin/system/schema-fix'): None,  # system_schema_fix: system health internals, admin-only
    ('POST', '/admin/system/device-type-backfill'): None,  # device_type_backfill_apply: system health internals
    ('POST', '/admin/system/spec-fill'): None,  # spec_fill_apply: system health internals
    ('POST', '/admin/system/subcategory-fill'): None,  # subcategory_fill_apply: system health internals
    ('POST', '/admin/system/reassign-fix'): None,  # reassign_fix_apply: system health internals
    ('POST', '/admin/system/audit-split-fix'): None,  # audit_split_apply: system health internals
    ('POST', '/admin/system/audit-scrap-fix'): None,  # audit_scrap_apply: system health internals
    ('POST', '/admin/system/audit-pna-fix'): None,  # audit_pna_apply: system health internals
    ('POST', '/admin/system/hold-release-fix'): None,  # hold_release_apply: system health internals
    ('POST', '/admin/system/audit-missing-remove'): None,  # audit_missing_remove: system health internals
    # ===== routers/extended_warranty.py =====
    ('POST', '/extended-warranty/set'): ('extended_warranty', 'edit'),  # extended_warranty_set: sets warranty on a sold tag
    ('POST', '/extended-warranty/{sale_id}/update'): ('extended_warranty', 'edit'),  # extended_warranty_update
    # ===== routers/parts_grn.py =====
    ('POST', '/parts-grn/{grn_id}/delete'): ('spare_parts_purchase', 'delete'),  # grn_delete: alias parts-grn -> spare_parts/spare_parts_purchase
    ('POST', '/parts-grn/new'): ('spare_parts_purchase', 'add'),  # grn_create
    ('POST', '/parts-grn/harvest'): ('spare_parts', 'add'),  # harvest_part: harvest part from device into stock (Parts Manager list)
    # ===== routers/stage_control.py =====
    ('POST', '/stage-control/transition/add'): ('stage_control', 'add'),  # add_transition
    ('POST', '/stage-control/transition/remove'): ('stage_control', 'delete'),  # remove_transition
    ('POST', '/stage-control/aging/refresh'): None,  # trigger_aging_refresh: recompute aging cache, system internal
    # ===== routers/market.py =====
    ('POST', '/market/add'): ('market', 'add'),  # add_entry
    ('POST', '/market/{entry_id}/edit'): ('market', 'edit'),  # edit_entry_save
    ('POST', '/market/{entry_id}/toggle'): ('market', 'edit'),  # toggle_entry
    ('POST', '/market/from-message'): ('market', 'add'),  # add_from_message: creates entry from WhatsApp message
    ('POST', '/market/link-dealer'): ('market', 'edit'),  # link_group_dealer
    # ===== routers/inventory_location.py =====
    ('GET', '/locations/master/bulk-template'): ('location_master', 'export'),  # location_bulk_template
    ('POST', '/locations/master/bulk-upload'): ('location_master', 'upload'),  # location_bulk_upload
    ('POST', '/locations/master/create'): ('location_master', 'add'),  # create_location
    ('POST', '/locations/master/{loc_id}/edit'): ('location_master', 'edit'),  # edit_location
    ('POST', '/locations/master/{loc_id}/toggle'): ('location_master', 'edit'),  # toggle_location
    ('POST', '/locations/master/{loc_id}/delete'): ('location_master', 'delete'),  # delete_location
    ('POST', '/locations/device/{device_id}/pickup'): ('locations', 'edit'),  # pickup_device: device location page; also used from repair L2/L3 pages
    ('POST', '/locations/device/{device_id}/placeback'): ('locations', 'edit'),  # placeback_device
    ('POST', '/locations/device/{device_id}/assign'): ('locations', 'edit'),  # assign_device_location
    ('POST', '/locations/audit/create'): ('location_audit', 'add'),  # create_audit
    ('POST', '/locations/audit/{audit_id}/scan'): ('location_audit', 'add'),  # record_scan: records a scan
    ('POST', '/locations/audit/{audit_id}/close'): ('location_audit', 'edit'),  # close_audit
    # ===== routers/physical_audits.py =====
    ('GET', '/audits/records/{record_id}/download'): ('physical_audits', 'export'),  # download_record: audit attachment
    ('GET', '/audits/export/workbook'): ('physical_audits', 'export'),  # export_workbook
    ('POST', '/audits/submit'): ('physical_audits', 'add'),  # submit_audit
    ('POST', '/audits/no-tags'): ('physical_audits', 'add'),  # record_no_tags
    ('POST', '/audits/upload'): ('physical_audits', 'upload'),  # upload_tags: records audit rows from uploaded tag list
    ('POST', '/audits/records/{record_id}/notes'): ('physical_audits', 'edit'),  # update_record_notes
    ('POST', '/audits/records/{record_id}/delete'): ('physical_audits', 'delete'),  # delete_record
    ('POST', '/audits/export/zip'): ('physical_audits', 'export'),  # export_zip: export
    # ===== routers/block_tags.py =====
    ('GET', '/block-tags/export/all'): ('block_tags', 'export'),  # export_all
    ('GET', '/block-tags/{record_id}/barcode'): ('block_tags', 'export'),  # download_barcode: block barcode PDF (missed by path regex)
    ('POST', '/block-tags/match-upload'): None,  # match_upload: read-only match of uploaded ids
    ('POST', '/block-tags/create'): ('block_tags', 'add'),  # create_block
    ('POST', '/block-tags/{record_id}/edit'): ('block_tags', 'edit'),  # edit_block
    ('POST', '/block-tags/{item_id}/release'): ('block_tags', 'edit'),  # release_item
    ('POST', '/block-tags/bulk-release'): ('block_tags', 'edit'),  # bulk_release
    ('POST', '/block-tags/export/selected'): ('block_tags', 'export'),  # export_selected: export
    # ===== routers/product_master.py =====
    ('GET', '/product-master/pricing-calculator/template'): ('pm_pricing_calculator', 'export'),  # pricing_calculator_template
    ('POST', '/product-master/dropdown/save'): ('pm_product_master', 'edit'),  # dropdown_save: add (no item_id) or edit (item_id) -> use 'add' when item_id blank
    ('POST', '/product-master/dropdown/{item_id}/delete'): ('pm_product_master', 'delete'),  # dropdown_delete
    ('POST', '/product-master/products/save'): ('pm_product_master', 'edit'),  # product_save: add when product_id blank, else edit
    ('POST', '/product-master/products/{product_id}/delete'): ('pm_product_master', 'delete'),  # product_delete
    ('POST', '/product-master/products/{product_id}/discount'): ('pm_product_master', 'pricing'),  # product_discount: discount_pct
    ('POST', '/product-master/competitive/save'): ('pm_product_master', 'edit'),  # competitive_save: competitive list add/edit (add when item_id blank)
    ('POST', '/product-master/competitive/{item_id}/delete'): ('pm_product_master', 'delete'),  # competitive_delete
    ('POST', '/product-master/competitive/method'): ('pm_product_master', 'pricing'),  # competitive_set_method: changes avg/max/min method used to recompute sell prices
    ('POST', '/product-master/hardware/{kind}/save'): ('pm_hardware', 'pricing'),  # hardware_save: RAM/Storage capacity + price row (add when item_id blank)
    ('POST', '/product-master/hardware/{kind}/{item_id}/delete'): ('pm_hardware', 'delete'),  # hardware_delete
    ('POST', '/product-master/pricing-calculator/rows/delete'): ('pm_pricing_calculator', 'delete'),  # pricing_calculator_rows_delete
    ('POST', '/product-master/pricing-calculator/upload'): ('pm_pricing_calculator', 'upload'),  # pricing_calculator_upload
    ('POST', '/product-master/pricing/{product_id}/save'): ('pm_product_pricing', 'pricing'),  # pricing_save
    ('POST', '/product-master/spare-parts/{product_id}/save'): ('pm_spare_parts', 'pricing'),  # spare_parts_save: sets part prices for a product
    # ===== routers/iqc_api.py =====
    ('POST', '/iqc/api/submit'): None,  # submit_iqc: OxyQC EXE API (X-OxyQC-Key), not session UI
    # ===== routers/qa_uat.py =====
    ('GET', '/qa/stage-workflows/{workflow_key}/download'): ('qa', 'export'),  # stage_workflow_download
    ('GET', '/qa/srs/technical-spec/download'): ('qa', 'export'),  # srs_technical_spec_download
    ('GET', '/qa/srs/functional-spec/download'): ('qa', 'export'),  # srs_functional_spec_download
    ('GET', '/qa/srs/design-system/download'): ('qa', 'export'),  # srs_design_system_download
    ('GET', '/qa/srs/module-map/download'): ('qa', 'export'),  # srs_module_map_pdf
    ('GET', '/qa/srs/module-map/download.pptx'): ('qa', 'export'),  # srs_module_map_pptx
    ('POST', '/qa/requirements/new'): ('qa', 'add'),  # req_create
    ('POST', '/qa/requirements/{req_id}/status'): ('qa', 'edit'),  # req_status
    ('POST', '/qa/test-cases/new'): ('qa', 'add'),  # tc_create
    ('POST', '/qa/test-cases/{tc_id}/execute'): ('qa', 'edit'),  # tc_execute
    ('POST', '/qa/defects/new'): ('qa', 'add'),  # defect_create
    ('POST', '/qa/defects/{defect_id}/status'): ('qa', 'edit'),  # defect_status
    ('POST', '/qa/uat/new'): ('qa', 'add'),  # uat_create
    ('POST', '/qa/uat/{uat_id}/execute'): ('qa', 'edit'),  # uat_execute
    ('POST', '/qa/releases/new'): ('qa', 'add'),  # release_create
    ('POST', '/qa/releases/{rel_id}/status'): ('qa', 'edit'),  # release_status
    # ===== routers/crm_contacts.py =====
    ('GET', '/crm/contacts/team-mapping/sample-csv'): ('crm_contacts', 'export'),  # team_mapping_sample_csv
    ('GET', '/crm/contacts/import-csv'): None,  # import_csv_form: import form page, not a download
    ('GET', '/crm/contacts/export-csv'): ('crm_contacts', 'export'),  # export_contacts_csv
    ('GET', '/crm/contacts/calls/bulk-upload-template'): ('crm_contacts', 'export'),  # account_calls_bulk_upload_template
    ('POST', '/crm/contacts/upload'): ('crm_contacts', 'upload'),  # upload_contacts_csv
    ('POST', '/crm/contacts/team-mapping'): ('crm_contacts', 'edit'),  # save_team_mapping
    ('POST', '/crm/contacts/team-mapping/upload'): ('crm_contacts', 'upload'),  # team_mapping_upload_apply
    ('POST', '/crm/contacts/import-csv'): None,  # import_csv_preview: import preview only (writes nothing); import-confirm is the write
    ('POST', '/crm/contacts/import-confirm'): ('crm_contacts', 'upload'),  # import_csv_confirm
    ('POST', '/crm/contacts/calls/bulk-upload'): ('crm_contacts', 'upload'),  # account_calls_bulk_upload
    ('POST', '/crm/contacts/new'): ('crm_contacts', 'add'),  # create_contact
    ('POST', '/crm/contacts/{contact_id}/edit'): ('crm_contacts', 'edit'),  # update_contact
    ('POST', '/crm/contacts/{contact_id}/verify-kyc'): ('crm_contacts', 'edit'),  # verify_kyc
    ('POST', '/crm/contacts/{contact_id}/trash'): ('crm_contacts', 'delete'),  # trash_contact: soft delete
    ('POST', '/crm/contacts/{contact_id}/restore'): ('crm_contacts', 'edit'),  # restore_contact: restore from trash
    # ===== routers/crm_sourcing.py =====
    ('GET', '/crm/sourcing/{deal_id}/download-po'): ('crm_sourcing', 'export'),  # download_deal_po
    ('POST', '/crm/sourcing/new'): ('crm_sourcing', 'add'),  # create_deal
    ('POST', '/crm/sourcing/{deal_id}/stage'): ('crm_sourcing', 'edit'),  # move_stage
    ('POST', '/crm/sourcing/{deal_id}/edit'): ('crm_sourcing', 'edit'),  # update_deal
    ('POST', '/crm/sourcing/{deal_id}/upload'): ('crm_sourcing', 'edit'),  # upload_documents: attaches deal documents (not a bulk import)
    ('POST', '/crm/sourcing/{deal_id}/link-lot'): ('crm_sourcing', 'edit'),  # link_lot
    ('POST', '/crm/sourcing/{deal_id}/po-items/add'): ('crm_sourcing', 'add'),  # add_deal_po_item
    ('POST', '/crm/sourcing/{deal_id}/po-items/{item_id}/delete'): ('crm_sourcing', 'delete'),  # delete_deal_po_item
    ('POST', '/crm/sourcing/{deal_id}/po-items/{item_id}/offer'): ('crm_sourcing', 'pricing'),  # set_deal_po_item_offer: offer price
    ('POST', '/crm/sourcing/{deal_id}/generate-po'): ('crm_sourcing', 'add'),  # generate_po_from_deal: creates CRMPurchaseOrder
    # ===== routers/crm_sales.py =====
    ('POST', '/crm/sales/new'): ('crm_sales_opp', 'add'),  # create_opp
    ('POST', '/crm/sales/{opp_id}/stage'): ('crm_sales_opp', 'edit'),  # move_stage
    ('POST', '/crm/sales/{opp_id}/edit'): ('crm_sales_opp', 'edit'),  # update_opp
    ('POST', '/crm/sales/{opp_id}/link-sale'): ('crm_sales_opp', 'edit'),  # link_sale_submit
    # ===== routers/crm_quotes.py =====
    ('GET', '/crm/quotes/{quote_id}/print'): (('crm_quotes', 'crm_sales_opp', 'crm_contacts'), 'export'),  # quote_print: quote list/detail + shared quotes include (nq_perm_module)
    ('GET', '/crm/quotes/{quote_id}/download'): (('crm_quotes', 'crm_sales_opp', 'crm_contacts'), 'export'),  # download_quote_doc
    ('POST', '/crm/quotes/new'): ('crm_quotes', 'add'),  # create_quote: Quotes row in the matrix
    ('POST', '/crm/quotes/from-lot'): ('crm_quotes', 'add'),  # create_quote_from_lot: Quotes row in the matrix
    ('POST', '/crm/quotes/{quote_id}/generate'): (('crm_quotes', 'crm_sales_opp', 'crm_contacts'), 'export'),  # generate_quote_doc: generate + download document
    ('POST', '/crm/quotes/{quote_id}/status'): ('crm_quotes', 'edit'),  # update_status: Quotes row in the matrix
    # ===== routers/crm_activities.py =====
    ('POST', '/crm/activities/log'): ('crm_dashboard', 'add'),  # log_activity: activity log; path resolves to crm_dashboard (/crm/)
    ('POST', '/crm/activities/{activity_id}/done'): ('crm_dashboard', 'edit'),  # mark_done
    # ===== routers/crm_price_matrix.py =====
    ('POST', '/crm/price-matrix/new'): ('crm_price_matrix', 'pricing'),  # create_matrix_row: price matrix row
    ('POST', '/crm/price-matrix/{row_id}/edit'): ('crm_price_matrix', 'pricing'),  # update_matrix_row
    ('POST', '/crm/price-matrix/{row_id}/delete'): ('crm_price_matrix', 'delete'),  # delete_matrix_row
    ('POST', '/crm/price-matrix/upload'): ('crm_price_matrix', 'upload'),  # upload_matrix: bulk price import (consider also requiring pricing)
    # ===== routers/gate_pass.py =====
    ('POST', '/gate-pass/resolve-tags'): None,  # resolve_tags: read-only tag resolve
    ('POST', '/gate-pass/{sale_id}/exit'): ('gate_pass', 'edit'),  # confirm_exit
    ('POST', '/gate-pass/bulk-exit'): ('gate_pass', 'edit'),  # bulk_confirm_exit
    # ===== routers/part_sales.py =====
    ('GET', '/part-sales/export'): ('part_sales', 'export'),  # export_part_sales_filtered
    ('POST', '/ready-to-sale-parts/{part_id}/request'): ('ready_to_sale_parts', 'add'),  # raise_sale_request
    ('POST', '/parts-sale-request/{req_id}/approve'): ('parts_sale_request', 'edit'),  # approve_sale_request
    ('POST', '/parts-sale-request/{req_id}/reject'): ('parts_sale_request', 'edit'),  # reject_sale_request
    ('POST', '/part-sales/new'): ('part_sales', 'add'),  # create_part_sale
    # ===== routers/partner_payments.py =====
    ('POST', '/partner-payments/{bid_id}/verify'): ('partner_payments', 'edit'),  # verify_payment
    # ===== routers/crm_purchase_orders.py =====
    ('GET', '/crm/purchase-orders/{po_id}/print'): (('crm_purchase_orders', 'crm_sourcing', 'crm_contacts'), 'export'),  # print_po: PO list/detail + shared PO include (po_perm_module)
    ('POST', '/crm/purchase-orders/new'): ('crm_purchase_orders', 'add'),  # create_po
    ('POST', '/crm/purchase-orders/{po_id}/issue'): ('crm_purchase_orders', 'edit'),  # issue_po
    # ===== routers/deals.py =====
    ('POST', '/deals/new'): ('trade_partner_all_deals', 'add'),  # create_deal: deals.py MODULE
    ('POST', '/deals/{deal_id}/approve'): ('trade_partner_all_deals', 'edit'),  # approve_deal
    ('POST', '/deals/{deal_id}/payment'): ('trade_partner_all_deals', 'edit'),  # mark_deal_paid: mark paid
    ('POST', '/deals/{deal_id}/delete'): ('trade_partner_all_deals', 'delete'),  # delete_deal: currently gated as edit
    # ===== routers/accounts.py =====
    ('GET', '/accounts/customer-receipts'): None,  # customer_receipts: normal page
    ('GET', '/accounts/customer-receipts/data'): None,  # customer_receipts_data: JSON data feed
    ('GET', '/accounts/customer-receipts/bulk-upload-template'): ('finance_customer', 'export'),  # download_receipt_csv_template
    ('GET', '/accounts/customer-receipts/bulk-upload'): None,  # receipt_bulk_upload_form: upload form page
    ('POST', '/accounts/supplier-payments/new'): ('finance_supplier', 'add'),  # create_supplier_payment
    ('POST', '/accounts/customer-receipts/new'): ('finance_customer', 'add'),  # create_customer_receipt
    ('POST', '/accounts/customer-receipts/bulk-upload'): ('finance_customer', 'upload'),  # receipt_bulk_upload
    ('POST', '/accounts/po/{po_id}/complete-payment'): ('finance', 'edit'),  # complete_po_payment
    ('POST', '/accounts/po/{po_id}/upload-invoice'): ('finance', 'edit'),  # upload_po_invoice: single invoice attachment
    ('POST', '/accounts/quote/{quote_id}/complete-payment'): ('finance', 'edit'),  # complete_quote_payment
    ('POST', '/accounts/quote/{quote_id}/upload-invoice'): ('finance', 'edit'),  # upload_quote_invoice: single invoice attachment
    # ===== routers/crm_assign_leads.py =====
    ('GET', '/crm/assign-social-leads/sample'): ('crm_assign_leads', 'export'),  # download_sample: sample CSV (missed by path regex)
    ('POST', '/crm/assign-social-leads/group'): ('crm_assign_leads', 'add'),  # create_group
    ('POST', '/crm/assign-social-leads/group/{group_id}/edit'): ('crm_assign_leads', 'edit'),  # edit_group
    ('POST', '/crm/assign-social-leads/import/{group_id}'): ('crm_assign_leads', 'upload'),  # import_leads
    ('POST', '/crm/assign-social-leads/lead'): ('crm_assign_leads', 'add'),  # add_lead
    ('POST', '/crm/assign-social-leads/lead/{lead_id}/edit'): ('crm_assign_leads', 'edit'),  # edit_lead
    ('POST', '/crm/assign-social-leads/lead/{lead_id}/call'): ('crm_assign_leads', 'add'),  # log_call: log call
    # ===== routers/assign_dealer_leads.py =====
    ('POST', '/assign-dealer-leads/assign'): ('assign_dealer_leads', 'edit'),  # bulk_assign_dealers
    ('POST', '/assign-dealer-leads/{dealer_id}/delete'): ('assign_dealer_leads', 'delete'),  # delete_dealer_lead
    ('POST', '/assign-dealer-leads/export'): ('assign_dealer_leads', 'export'),  # export_dealer_leads: export
    # ===== routers/tickets.py =====
    ('POST', '/tickets/raise'): None,  # raise_ticket: ticket raise (any user)
    ('POST', '/tickets/{ticket_id}/close'): None,  # close_ticket: internal support tickets; no PERM_MODULES entry (see notes s3)
    # ===== routers/settings.py =====
    ('POST', '/settings'): ('company_settings', 'edit'),  # save_company: Company Setting page (/settings, label key company_settings); add when new company
    ('POST', '/settings/{company_id}/delete'): ('company_settings', 'delete'),  # delete_company
    # ===== routers/trash.py =====
    ('POST', '/trash/duplicates/delete'): ('location_trash', 'delete'),  # delete_duplicate_devices
    ('POST', '/trash/lots/{lot_id}'): ('lots', 'delete'),  # trash_lot: move lot to trash (soft delete) from Lot Overview
    ('POST', '/trash/lots/{lot_id}/restore'): ('location_trash', 'edit'),  # restore_lot: restore from trash
    ('POST', '/trash/lots/{lot_id}/delete'): ('location_trash', 'delete'),  # delete_lot_forever: permanent
    ('POST', '/trash/devices/{barcode}'): (('devices', 'iqc'), 'delete'),  # trash_device: trash device; posted from All Inventory (devices) and IQC list - pick by Referer path
    ('POST', '/trash/bulk/devices'): ('devices', 'delete'),  # trash_devices_bulk: All Inventory bulk trash
    ('POST', '/trash/devices/{barcode}/delete'): ('location_trash', 'delete'),  # delete_device_forever: permanent
    ('POST', '/trash/devices/{barcode}/restore'): ('location_trash', 'edit'),  # restore_device: restore
    # ===== routers/notifications.py =====
    ('POST', '/notifications/{notif_id}/read'): None,  # mark_read: mark read
    # ===== routers/manuals.py =====
    ('GET', '/manuals/all/download'): ('manuals', 'export'),  # download_all_manuals: zip
    ('GET', '/manuals/{mid}/download'): ('manuals', 'export'),  # download_manual
    ('POST', '/manuals/{mid}/share'): None,  # share_manual: share a static manual; no business record changed
    # ===== routers/landing_pages.py =====
    ('POST', '/admin/landing-pages/save'): ('landing_pages', 'edit'),  # save_landing_page_title
    ('POST', '/admin/landing-pages/reset'): ('landing_pages', 'edit'),  # reset_landing_page_title
    ('POST', '/admin/landing-pages/toggle-breadcrumb'): ('landing_pages', 'edit'),  # toggle_breadcrumb
    # ===== routers/procure_dashboard.py =====
    ('POST', '/procure-dashboard/device-sourcing/{request_id}/close'): ('spare_parts_purchase', 'edit'),  # close_device_sourcing: no own key: nav gated by crm_dashboard, alias procure-dashboard -> spare_parts_purchase (see notes s3)
    # ===== routers/terms_conditions.py =====
    ('POST', '/admin/terms-conditions/add'): ('terms_conditions', 'add'),  # add_term
    ('POST', '/admin/terms-conditions/{term_id}/edit'): ('terms_conditions', 'edit'),  # edit_term
    ('POST', '/admin/terms-conditions/{term_id}/toggle'): ('terms_conditions', 'edit'),  # toggle_term
    ('POST', '/admin/terms-conditions/{term_id}/delete'): ('terms_conditions', 'delete'),  # delete_term
    # ===== routers/dealer_quotations.py =====
    ('POST', '/dealers/{dealer_id}/quotations'): ('dealers', 'add'),  # create_dealer_quotation: already require_module_perm('dealers','add')
    ('POST', '/dealer-quotations/{quotation_id}/confirm'): (('dealers', 'quotations'), 'edit'),  # confirm_dealer_quotation: shared quotation table on Dealer page and Quotations list - pick by Referer (/dealers* -> dealers, /quotations -> quotations)
    ('POST', '/dealer-quotations/{quotation_id}/delete'): (('dealers', 'quotations'), 'delete'),  # delete_dealer_quotation: same as confirm
    # ===== routers/sidebar_config.py =====
    ('POST', '/admin/sidebar-config/save'): ('sidebar_config', 'edit'),  # save_sidebar_label
    ('POST', '/admin/sidebar-config/reset'): ('sidebar_config', 'edit'),  # reset_sidebar_label
    # ===== routers/stress_api.py =====
    ('POST', '/stress/{barcode}/run'): ('qc_check', 'edit'),  # run_stress: Stress Test page; starts in-memory run
    ('POST', '/stress/{barcode}/stop'): ('qc_check', 'edit'),  # stop_stress
    ('POST', '/stress/{barcode}/save'): ('qc_check', 'add'),  # save_results: stores StressTestResult
    ('POST', '/stress/{barcode}/save-agent'): ('qc_check', 'add'),  # save_agent_results: stores agent StressTestResult
    ('POST', '/stress/{barcode}/fail'): ('qc_check', 'edit'),  # stress_fail_assign: fail + reassign to engineer
    ('POST', '/stress/{barcode}/complete-to-paint'): ('qc_check', 'edit'),  # stress_complete_to_paint: moves to cosmetic_received
    # ===== routers/buckets.py =====
    ('POST', '/buckets/create'): ('stock', 'add'),  # create_bucket: Stock Inwards page
    ('POST', '/buckets/{bucket_id}/edit'): ('stock', 'edit'),  # edit_bucket
    ('POST', '/buckets/{bucket_id}/rename'): ('stock', 'edit'),  # rename_bucket
    ('POST', '/buckets/{bucket_id}/move-to-trc'): ('stock', 'edit'),  # move_bucket_to_trc
    ('POST', '/buckets/{bucket_id}/validate'): ('stock', 'edit'),  # validate_bucket
    ('POST', '/buckets/{bucket_id}/assign'): ('production_manager', 'edit'),  # assign_bucket: TRC Production page allocation assign
    ('POST', '/devices/{barcode}/assign'): ('production_manager', 'edit'),  # assign_device: Production Manager table (path prefix would say devices - override)
    ('POST', '/devices/bulk-assign-l1l2'): ('production_manager', 'edit'),  # bulk_assign_devices_l1l2: Production Manager (path prefix says devices - override)
    ('POST', '/buckets/{bucket_id}/assign-to-production'): ('stock', 'edit'),  # assign_bucket_to_production: Stock Inward Movement table
    ('POST', '/buckets/{bucket_id}/release'): ('stock', 'edit'),  # release_bucket: Inventory Manager Final QC Pass (Buckets) table
    ('POST', '/buckets/{bucket_id}/assign-to-engineer'): ('production_manager', 'edit'),  # assign_bucket_to_engineer
    ('POST', '/buckets/{bucket_id}/release-repair-line'): ('production_manager', 'edit'),  # release_bucket_from_repair_line
    # ===== routers/partner_portal.py =====
    ('POST', '/partner/logout'): None,  # logout: partner portal
    ('POST', '/partner/listings/{listing_id}/bid'): None,  # place_bid: partner portal (external)
    ('POST', '/partner/listings/{listing_id}/custom-price'): None,  # request_custom_price: partner portal (external)
    ('POST', '/partner/lots/{lot_id}/bid'): None,  # place_lot_bid: partner portal (external)
    ('POST', '/partner/lots/{lot_id}/custom-price'): None,  # request_lot_custom_price: partner portal (external)
    ('POST', '/partner/lots/{lot_id}/book'): None,  # book_lot: partner portal (external)
    ('POST', '/partner/lots/{lot_id}/withdraw'): None,  # withdraw_lot_booking: partner portal (external)
    ('POST', '/partner/listings/{listing_id}/book'): None,  # book_listing: partner portal (external)
    ('POST', '/partner/bids/{bid_id}/payment'): None,  # submit_bid_payment: partner portal (external)
    ('POST', '/partner/bookings/{booking_id}/proof'): None,  # upload_proof: partner portal (external)
    ('POST', '/partner/bookings/{booking_id}/cancel'): None,  # cancel_booking: partner portal (external)
    # ===== routers/partner_admin.py =====
    ('GET', '/trade-partner/bids/document/{doc_id}'): (('trade_partner_bids', 'partner_payments'), 'export'),  # download_bid_document: bid PO/document file; Bids Created + Partner Payments (missed by path regex)
    ('POST', '/trade-partner/partners/enable'): ('trade_partner_partners', 'add'),  # enable_partner
    ('POST', '/trade-partner/partners/{dealer_id}/update'): ('trade_partner_partners', 'edit'),  # update_partner
    ('POST', '/trade-partner/partners/{dealer_id}/toggle'): ('trade_partner_partners', 'edit'),  # toggle_partner
    ('POST', '/trade-partner/partners/{dealer_id}/reset-password'): ('trade_partner_partners', 'edit'),  # reset_partner_password
    ('POST', '/trade-partner/partners/{dealer_id}/delete'): ('trade_partner_partners', 'delete'),  # delete_partner
    ('POST', '/trade-partner/listings/create'): ('trade_partner_listings', 'add'),  # create_listing
    ('POST', '/trade-partner/listings/{listing_id}/publish'): ('trade_partner_listings', 'edit'),  # publish_listing
    ('POST', '/trade-partner/listings/{listing_id}/pause'): ('trade_partner_listings', 'edit'),  # pause_listing
    ('POST', '/trade-partner/listings/{listing_id}/reprice'): ('trade_partner_listings', 'pricing'),  # reprice_listing
    ('POST', '/trade-partner/bookings/{booking_id}/verify'): ('trade_partner_bookings', 'edit'),  # verify_token_proof
    ('POST', '/trade-partner/bookings/{booking_id}/reject'): ('trade_partner_bookings', 'edit'),  # reject_booking
    ('POST', '/trade-partner/bookings/{booking_id}/balance-verify'): ('trade_partner_bookings', 'edit'),  # verify_balance
    ('POST', '/trade-partner/bookings/{booking_id}/dispatched'): ('trade_partner_bookings', 'edit'),  # mark_dispatched
    ('POST', '/trade-partner/bookings/{booking_id}/extend'): ('trade_partner_bookings', 'edit'),  # extend_booking
    ('POST', '/trade-partner/floors/create'): ('trade_partner_floors', 'pricing'),  # create_floor: margin floor; also _is_floor_approver check
    ('POST', '/trade-partner/settings'): ('trade_partner_settings', 'edit'),  # settings_save
    ('POST', '/trade-partner/manage-lots/map-grn'): ('trade_partner_manage_lots', 'edit'),  # manage_lots_map_grn
    ('POST', '/trade-partner/manage-lots/move-ready-for-sale'): ('trade_partner_manage_lots', 'edit'),  # manage_lots_move_ready_for_sale
    ('POST', '/trade-partner/manage-lots/edit-device'): ('trade_partner_manage_lots', 'edit'),  # manage_lots_edit_device
    ('POST', '/trade-partner/manage-lots/assign-visibility'): ('trade_partner_manage_lots', 'edit'),  # assign_lot_visibility_bulk
    ('POST', '/trade-partner/manage-lots/set-restricted'): ('trade_partner_manage_lots', 'edit'),  # set_lots_restricted
    ('POST', '/trade-partner/manage-lots/{lot_id}/assign-visibility'): ('trade_partner_manage_lots', 'edit'),  # assign_lot_visibility
    ('POST', '/trade-partner/bids/{bid_id}/generate-po'): ('trade_partner_bids', 'add'),  # generate_bid_po: creates + attaches PO document
    ('POST', '/trade-partner/bids/{bid_id}/mark-won'): ('trade_partner_bids', 'edit'),  # mark_bid_won
    ('POST', '/trade-partner/bids/{bid_id}/upload'): ('trade_partner_bids', 'edit'),  # upload_bid_document: attach single bid document
    # ===== routers/care_api.py =====
    ('POST', '/care/api/v1/heartbeat'): None,  # heartbeat: care device-agent API
    ('POST', '/care/api/v1/deactivate'): None,  # deactivate: care device-agent API
    # ===== routers/care_internal.py =====
    ('POST', '/care/internal/pairings'): ('care_support', 'add'),  # create_pending_pairing: already care_support/add
    ('POST', '/care/internal/pairings/{pairing_id}/revoke'): ('care_support', 'edit'),  # revoke_pairing
    ('POST', '/care/internal/dispatch-exceptions'): ('care_support', 'add'),  # create_dispatch_exception
    # ===== routers/care_admin.py =====
    ('POST', '/care-support/tickets/{ticket_number}/assign'): ('care_support', 'edit'),  # assign_ticket
    ('POST', '/care-support/tickets/{ticket_number}/status'): ('care_support', 'edit'),  # change_status
    ('POST', '/care-support/tickets/{ticket_number}/priority'): ('care_support', 'edit'),  # change_priority
    ('POST', '/care-support/tickets/{ticket_number}/note'): ('care_support', 'edit'),  # add_note: adds note to ticket (code gates edit)
    ('POST', '/care-support/tickets/{ticket_number}/pickup'): ('care_support', 'edit'),  # set_pickup
    ('POST', '/care-support/pairings/{pairing_id}/revoke'): ('care_support', 'edit'),  # revoke_pairing_html
    ('POST', '/care-support/provision/{device_id}'): ('care_support', 'add'),  # provision_device
    ('POST', '/care-support/dispatch-exceptions'): ('care_support', 'add'),  # record_dispatch_exception_html
    ('POST', '/care-support/offers'): ('care_support', 'add'),  # create_offer
    ('POST', '/care-support/offers/{offer_id}/toggle'): ('care_support', 'edit'),  # toggle_offer
    ('POST', '/care-support/offers/{offer_id}/send'): ('care_support', 'edit'),  # send_offer
    # ===== routers/workid_status.py =====
    ('GET', '/workid-status/export'): ('workid_status', 'export'),  # workid_status_export
    # ===== routers/invoices.py =====
    ('GET', '/invoices/print/{sale_id}'): ('sales_list', 'export'),  # print_invoice: Sales detail Invoice PDF
    ('GET', '/invoices/waybill/{sale_id}'): ('sales_list', 'export'),  # print_waybill: Sales detail Waybill
}


# First path segment of every gated GET (export / download) route. The action
# middleware skips route matching for any GET whose first segment is not here,
# so ordinary page loads stay cheap.
GATED_GET_PREFIXES = frozenset(
    path.split("/", 2)[1] for (method, path), entry in ROUTE_ACTIONS.items()
    if method == "GET" and entry
)

def resolve_module(method: str, path: str, module, path_params: dict):
    """Which module(s) an incoming write is checked against.

    Single-module routes return that module. Routes shared by several modules
    (a list in ROUTE_ACTIONS: e.g. one repair endpoint used by L1/L2 and
    L3/L4, one cosmetic endpoint for every stage, GRN create used by GRN with
    Invoice / GRN post IQC / Manage Lots) return all candidates, and the
    action is allowed when the role has it on at least one of them. The
    page-level buttons are hidden per module, so this only matters for a
    request crafted outside the UI.
    """
    return (module,) if isinstance(module, str) else tuple(module)
