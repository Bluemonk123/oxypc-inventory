"""Learning Manual content - one manual per sidebar page (2026-10-16).

Rendered by routers/manuals.py on the Manuals page (grouped by sidebar
section) and into each manual's downloadable PDF. Written from the code as it
stood on 2026-10-16. Keep text plain ASCII / Latin-1: the PDF uses a core
Latin-1 font. When a page changes, update its entry here - the PDF cache is
keyed on this content, so a changed manual gets a fresh PDF automatically.

Fields: id (stable int used in /manuals/{id}/download), key (the page's
module key, same as the sidebar/permission key), section (sidebar section),
module (page name), url, icon (Bootstrap icon), description,
steps [(group title, [instructions])], tips [..].
"""

MANUAL_SECTIONS = ['General',
 'Attendance',
 'Intake',
 'Inventory',
 'Repair',
 'Store Manager',
 'CRM',
 'External Partner',
 'Customer Care',
 'Sales & CRM',
 'Finance',
 'Product Master',
 'Reports',
 'Admin',
 'Support']

MANUALS = [{'id': 1,
  'key': 'dashboard',
  'section': 'General',
  'module': 'Admin Dashboard',
  'url': '/dashboard',
  'icon': 'bi-speedometer2',
  'description': 'One-page overview of stock in every stage, money figures and weekly trends. Used by the '
                 'director, admins and managers to check the health of the business at a glance.',
  'steps': [('Open the dashboard',
             ["Click 'Admin Dashboard' at the top of the left menu.",
              'The page only shows in the menu if your role has the Dashboard permission.',
              "If you are an IQC, L1/L2/L3, QC, Sales, Spare Parts or Inventory Manager user, a 'My Work "
              "Queue' banner at the top shows your own pending count with a shortcut button (for example 'Go "
              "to IQC' or 'Go to QC')."]),
            ('Read the Device Stage Pipeline',
             ["The 'Device Stage Pipeline' card shows live counts for IQC, Stock In, Production, L1/L2, "
              'L3/L4, Stress/QC, Cosmetic, Final QC, Ready to Sale and Sold.',
              'Click any tile to open Inventory Search filtered to the devices in that stage.',
              "Click 'Split Entity' to see each count broken down by entity (OxyPC Computers, Renew "
              'Circuits, Deshwal). Click again to hide it.',
              "Click 'Split Zone' to see each count broken down by storage zone.",
              'Returned, Scrapped and Scrap for Sale are shown on a separate line under the pipeline. They '
              'are not counted in any total.']),
            ('Check the money figures',
             ["The 'Financial Summary' card shows Total Investment, Parts Spent, Labour Spent, Total Revenue "
              'and Net Profit for one Financial Year (April to March).',
              "Pick a different year from the 'FY' dropdown in the card header. The page reloads with that "
              'year.',
              'These figures match the Business P&L report for the same year.',
              "The four summary cards show 'Total Inventory', 'In Repair', 'Revenue This Month' and 'Profit "
              "This Month'. Profit is shown in red when it is negative."]),
            ('Use the category tiles and charts',
             ['The coloured tiles (Laptops, Desktops, TFT Monitor, Tablet, Mini PC, Server) show active '
              'stock for each category.',
              "Each tile shows 'In TRC' (still being worked on) and 'In Ready' (Ready to Sale). Click a tile "
              'to open that category in Inventory Search.',
              "Scroll down for the 'Analytics' charts: Stage Distribution, Inventory by Category & Stage and "
              'the weekly charts for sourcing price, products in IQC / GRN / In Stock, and spare parts.',
              "The 'Weekly Sales Price' chart only shows if your role is allowed to see prices."]),
            ('Filter the dashboard',
             ["Use the filter bar at the top: 'Filter by Stage', 'Entity', 'Device Type', 'Zone', and the "
              "'P&L From' / 'P&L To' dates.",
              "Click 'Apply' to reload the page with your filters.",
              "Click 'Clear' to go back to the full, unfiltered view.",
              'The chosen Financial Year is kept when you apply other filters.'])],
  'tips': ['The dashboard is read-only. Nothing you click here changes any device.',
           'Category tiles leave out devices in GRN Receipt, Sold, Returned, Scrapped and Scrap for Sale, so '
           'they show only active stock.',
           'With no Entity filter, test data from the External Partner test entity is hidden from the '
           'counts.',
           "Counts without filters may be up to about 30 seconds old. Click 'Apply' or refresh the page to "
           'update.']},
 {'id': 2,
  'key': 'devices',
  'section': 'General',
  'module': 'Inventory Search',
  'url': '/devices',
  'icon': 'bi-search',
  'description': 'Search every Tag Number in the company across all stages, with model-wise and lot-wise '
                 'summaries. Used by everyone who needs to find a device, check its stage or export a stock '
                 'list.',
  'steps': [('Search for devices',
             ["Click 'Inventory Search' in the left menu.",
              'Type in the search box: Tag Number, make, model, CPU, RAM, storage or invoice number (for '
              "example 'Dell i5 8GB').",
              "Narrow the list with the 'Entity', 'Device Type', 'Grade', 'Lot', 'Stock Type' and 'Stage' "
              'dropdowns. You can tick more than one value in each.',
              "Use 'Date From' / 'Date To' to filter on the date the tag reached its current stage.",
              "'Active Stock Only' hides GRN Receipt, Sold, Returned, Scrapped and Scrap for Sale. It is "
              'ticked by default.',
              "Click 'Search'. Click 'Reset' to clear all filters."]),
            ('Read the All Tags Inventory table',
             ['Columns: Tag Number, Lot, Location ID, Brand, Model, Device Type, CPU, RAM, Storage, Grade, '
              'Stock Type, Stage and Updated.',
              "Stock Type is 'As-Is' (tag has a Sub-Lot Number), 'Finish Good' (Ready to Sale or Sold) or "
              "'In Progress' (everything else).",
              'For a sold tag, the sale number shows under the Stage. Click it to open the sale.',
              "Click the entity badges above the table (for example 'OxyPC Computers: 120') to filter by "
              'that entity.',
              "If a tag has no location, click 'Assign' in the Location ID column to set one.",
              'Use the eye icon to view a device, the pencil icon to edit it.']),
            ('Select tags and act on them',
             ['Tick the box at the start of each row, or tick the header box to select every tag that '
              'matches your filters (not only this page).',
              'You can also scan Tag Numbers into the table search box to select them.',
              "Click 'Upload Tags' and choose a CSV with a 'tag_number' (or 'barcode') column, then click "
              "'Select Tags' to tick those tags.",
              "Click 'Customise (n)' to change Device Type, Entity, Stage, Grade, Invoice Number, Bin "
              'Location, CPU, RAM, Storage, Touch Type or Device Price for all selected tags. Leave a field '
              'blank to keep it unchanged.',
              "Click 'Delete Selected (n)' or the red bin icon on a row to move tags to Trash. They can be "
              'restored from the Trash page.']),
            ('Export',
             ["Click 'Export CSV'.",
              'If you have ticked tags, only those tags are exported.',
              'If nothing is ticked, every tag matching your current filters is exported.']),
            ('Use the Model and Lot summaries',
             ["Scroll down to 'Model Based Summary'. It groups the same tags by Model, Make, Device Type, "
              'CPU, RAM, Storage, Touch Type, Grade and Stock Type, with a Total Count.',
              'Use its own filters (Entity, Make, Device Type, Touch Type, Stock Type, Grade) and click '
              "'Reset' to clear them.",
              'Click the eye icon on a model row to see every Tag Number in that group with its location and '
              'grade.',
              "'Lot Based Summary' lists lots with supplier, quantity and condition. Tick lots and click "
              "'Customise' to update all tags in those lots, or click the eye icon to see the lot's tags."])],
  'tips': ['Price columns (Total Price, Buying Price, Selling Price) only show if your role is allowed to '
           'see prices.',
           "Edit and Trash are for admin and inventory manager by default. Other roles get an 'Access "
           "denied' message.",
           'Customise is gated by the IQC / Devices edit permission in the Permission Matrix.',
           'The header select-all box picks every matching tag, even on other pages. Check the count on the '
           'buttons before you delete or customise.']},
 {'id': 3,
  'key': 'dispatch',
  'section': 'General',
  'module': 'TRC Dashboard',
  'url': '/dispatch',
  'icon': 'bi-truck',
  'description': 'Read-only view of all Ready to Sale stock by grade, with dispatch status, ageing and '
                 'warranty, plus the list of returned devices. Used by admins, inventory managers and the '
                 'sales team.',
  'steps': [('Open the page',
             ["Click 'TRC Dashboard' in the left menu.",
              'By default it is open to admin, inventory manager, sales, sales manager and telecaller '
              'roles.']),
            ('Read the count cards',
             ['There is one card each for Grade A, Grade B and Grade C. The big number is the count of Ready '
              'to Sale devices of that grade.',
              "Under each number: 'Dispatched' (quantity approved on Inventory Requests), 'Pending' (not yet "
              "dispatched) and 'Sold'.",
              "The 'Total Ready to Sale' card splits all Ready to Sale devices by how many days they have "
              'been ready: up to 15 days, 16-30, 31-45, 46-60 and more than 60 days.']),
            ('Look at devices by grade',
             ["Click 'A Grade Devices', 'B Grade Devices' or 'C Grade Devices' to open that list.",
              'Columns: Tag Number, Model Name, Quantities, Dispatched, Pending, Sold, Aging (days since '
              'Ready to Sale) and Warranty.',
              'Click a Tag Number to open that device.',
              'Each list shows 12 rows per page. Use the page buttons at the bottom to move through it.']),
            ('Check returned devices',
             ["Scroll down to 'Devices Returned'.",
              "'Within Warranty' lists devices returned while their warranty was still active.",
              "'Out of Warranty' lists devices returned after the warranty ended.",
              'Columns: Tag Number, Model, Returned On, Customer, User (who sold it) and Reason.'])],
  'tips': ['This page does not change anything. To approve or reject sales requests, use the Inventory '
           'Requests page.',
           'Ungraded and C grade devices are both counted under Grade C.',
           'Warranty is counted as 30 days from the latest sale of the device.',
           'Devices waiting more than 60 days are shown in red in the ageing card - push these for sale '
           'first.']},
 {'id': 4,
  'key': 'attendance',
  'section': 'Attendance',
  'module': 'My Attendance',
  'url': '/attendance',
  'icon': 'bi-person-check',
  'description': 'Check in and check out for the day and see your attendance. Used by every employee; admins '
                 "and inventory managers also see everyone's records and can mark attendance by hand.",
  'steps': [('Check in',
             ["Click 'My Attendance' under ATTENDANCE in the left menu. Most users land on this page after "
              'login.',
              "If you have not checked in today, a yellow box says 'You haven't checked in today'.",
              "Click 'Check In Now'.",
              "Choose 'In-Office' or 'Work From Home'.",
              "You see 'Checked in successfully' and your check-in time."]),
            ('Check out',
             ["At the end of the day open 'My Attendance' again.",
              "Click the red 'Check Out' button.",
              "You see 'Checked out successfully' and your check-out time."]),
            ('See your records',
             ['The table shows Check In, Check Out, Duration and Status for the selected day.',
              "Pick another date in the date box and click 'Go' to see that day. Click 'Today' to come back.",
              "Click 'History' to see a monthly calendar with your present, absent, late, WFH and half-day "
              'counts.']),
            ('Mark attendance for others (admin / inventory manager)',
             ['Admins and inventory managers see all employees for the selected day, with Employee, Role, '
              'Marked By and Notes columns.',
              "Click 'Mark Attendance'.",
              "Choose the Employee, Date and Status. Optionally enter 'Check In Time', 'Check Out Time' and "
              "'Notes'.",
              "Click 'Save'. If the employee already has a record for that day, it is updated."])],
  'tips': ['Status is set at check-in time: after 10:00 AM it is Late, after 1:00 PM Half Day, after 2:00 PM '
           'Absent.',
           'Duration is counted from check-in to check-out, or to 7:00 PM if that is earlier.',
           "You can check in only once a day. A second try shows 'Already checked in today'.",
           'You must check in before you can check out.']},
 {'id': 5,
  'key': 'attendance_report',
  'section': 'Attendance',
  'module': 'Attendance Report',
  'url': '/attendance/report',
  'icon': 'bi-file-earmark-bar-graph',
  'description': 'Attendance records for many employees over a date range, with totals and CSV export. Used '
                 'by admins and by team managers set up in Attendance Group Config.',
  'steps': [('Open the report',
             ["Click 'Attendance Report' under ATTENDANCE in the left menu.",
              'Admins see every employee. A team manager sees only the members of their group (and the '
              'groups under them).',
              'By default the report shows today only.']),
            ('Filter the report',
             ["Set 'From' and 'To' dates for the period you need.",
              "Choose one person in 'Employee', or leave 'All Employees'.",
              "Choose a 'Status' (Present, Absent, Half Day, Late, Wfh) or leave 'All'.",
              "Click 'Filter'."]),
            ('Read the results',
             ['Coloured badges show the count for each status, the Total records and Total Hours.',
              'The table shows Date, Employee, Role, Check In, Check Out, Duration, Status, Notes and Marked '
              'By.',
              "'Marked By' shows who marked the record by hand, if it was not a normal check-in."]),
            ('Export',
             ["Set your filters first and click 'Filter'.",
              "Click 'CSV' to download the same filtered records as a spreadsheet file. It has Date, "
              'Username, Full Name, Check In, Check Out, Duration, Status, Notes and Marked By.'])],
  'tips': ["If you are not an admin and not set up as a group manager, the page shows 'Access denied'.",
           'Group managers are set by an admin in Application Settings -> Attendance Config.',
           'A manager whose group has no members sees an empty report.',
           "To correct a record, use 'Mark Attendance' on the My Attendance page (admin / inventory "
           'manager).']},
 {'id': 6,
  'key': 'grn',
  'section': 'Intake',
  'module': 'GRN with Invoice',
  'url': '/grn',
  'icon': 'bi-receipt',
  'description': 'Create a GRN by uploading a supplier invoice PDF and track how many devices have been '
                 'stocked against it. Used by admins and inventory managers.',
  'steps': [('Upload an invoice',
             ['Go to Intake -> GRN with Invoice.',
              "In the 'Upload Invoice' box, choose the invoice PDF.",
              "Click 'Upload Invoice'.",
              'A new 12-digit GRN number is created and the invoice number, date, vendor, quantity and '
              'amount are read from the PDF.',
              "Check the new row. If a field was read wrongly, fix it with 'Edit'."]),
            ('Read the GRN table',
             ['Columns: GRN, Invoice Number, Date of Invoice, Vendor Name, Quantity, Stocked, Amount, '
              'Download and Action.',
              "'Stocked' is the number of Tag Numbers mapped to this GRN. It is green when it reaches the "
              'Quantity, amber when part-stocked and grey when zero.',
              "The cards at the top show 'Total GRN Quantities', 'Total Devices Added' and 'Total Devices "
              "Pending' for the rows you can see."]),
            ('Search and filter',
             ["Type in 'Search GRN', 'Search Invoice' or 'Search Vendor Name'. The table filters as you "
              'type.',
              "Set 'From' and 'To' to filter on Date of Invoice.",
              "Click 'Reset Filters' to clear them. The count cards follow the filters."]),
            ('Work with a GRN',
             ['Click the download icon to open the stored invoice PDF.',
              "Click 'Edit' to change Invoice Number, Date of Invoice, Vendor Name, Quantity or Amount, then "
              "click 'Save'.",
              "Click '+ Lot' to open the New Lot form with this GRN's number, date and supplier already "
              'filled.',
              'Click the red bin icon to delete the GRN. It is removed from the list but kept for audit.'])],
  'tips': ['Only PDF files can be uploaded.',
           "If the invoice number read from the PDF already exists, the upload is refused with 'Invoice "
           "number ... already exists as GRN ...'.",
           'The same file can be uploaded again if no invoice number was read from it - check for duplicates '
           'yourself.',
           "Saving 'Edit' needs the 'edit_grn' additional permission and takes you to the GRN post IQC page "
           'afterwards.',
           'By default only admin and inventory manager can open this page.']},
 {'id': 7,
  'key': 'grn_post_iqc',
  'section': 'Intake',
  'module': 'GRN post IQC',
  'url': '/grn/post-iqc',
  'icon': 'bi-clipboard2-check',
  'description': 'Create GRNs and map them to Tag Numbers that have finished IQC, which moves those tags to '
                 'Stock In. Used by admins and inventory managers.',
  'steps': [('Create a GRN',
             ["Go to Intake -> GRN post IQC (the page title is 'Create GRN').",
              "Click 'Add GRN' in the 'Create GRNs' card.",
              'Fill Date of Invoice, Purchase Date, GRN Date, Invoice Number, Invoice Price, Invoice '
              'Quantity, PO Number, Vehicle Number, E-Way Bill Number, Vendor Name and Notes as needed.',
              "Optionally attach the invoice in 'Upload Invoice'.",
              "Click 'Create GRN'. A new 12-digit GRN number is given."]),
            ('Link lots to a GRN',
             ["In the GRN row click 'Add Lot' (or 'Edit Lot' if lots are already linked).",
              "Type one or more Lot Numbers. Click 'Add another Lot' for more rows.",
              "If a Lot Number already exists, tick 'Are you sure want to add tags in this Lot' for that "
              'row.',
              "Click 'Save'. New lots are created from the GRN details. The 'LOT' column then shows how many "
              'lots are linked - click it to see them.']),
            ('Map the GRN to Tag Numbers',
             ["Scroll to 'GRN Pending for these Tag Numbers'. It lists devices in IQC that have no GRN yet.",
              "Find the tags with 'Search invoice number', 'Search PO number', the Device Type and Entity "
              'filters, or scan / paste Tag Numbers separated by commas into the table search box.',
              'When a search is active, all matching rows are ticked for you. You can also tick rows by '
              'hand.',
              "Click 'Map this GRN' on the GRN row and confirm.",
              'The tags get the GRN number and move from IQC to Stock In. Your filters stay in place for the '
              'next batch.']),
            ('Other actions in the GRN table',
             ["'Stocked' shows how many tags are mapped. The cards at the top total Quantities, Devices "
              'Added and Devices Pending for the visible rows.',
              "Click 'Yes As-Is' to give one Sub-Lot Number (and optionally an 'Assigned To' employee) to "
              "every tag mapped to this GRN. The Sub-Lot Number then shows in 'As-Is Lot'.",
              "Click 'Edit' to change the GRN details. Changes are copied to every linked lot.",
              'Use the download icon for the invoice, and the red bin icon to delete the GRN (kept for '
              'audit).']),
            ('Search and export',
             ["Use 'Search GRN', 'Search invoice number', 'Search vendor' and the Date of Invoice From / To "
              "boxes at the top to filter the GRN table. 'Reset Filters' clears them.",
              "In the pending section, click 'Export (n)' to download the ticked rows, or all rows matching "
              'the filters when nothing is ticked.'])],
  'tips': ["A tag that has no entity is set to 'OxyPC Computers' when mapped. An existing entity is never "
           'changed.',
           "'Add GRN' needs the 'add_grn' additional permission and 'Edit' needs 'edit_grn'.",
           'An invoice number that already exists on another GRN is refused.',
           "'Yes As-Is' only appears once at least one tag is mapped, and it overwrites the Sub-Lot Number "
           'of all mapped tags.',
           'By default only admin and inventory manager can open this page.']},
 {'id': 8,
  'key': 'lots',
  'section': 'Intake',
  'module': 'Lot Overview',
  'url': '/lots',
  'icon': 'bi-box-seam',
  'description': 'List of all purchased lots with supplier, invoice, quantity, registered and sold counts. '
                 'Used by inventory managers and admins to create, edit, export and delete lots.',
  'steps': [('Find a lot',
             ["Go to Intake -> Lot Overview (page title 'Lot Management').",
              "Type a supplier name or lot number in 'Search Supplier / Lot'.",
              "Set 'Purchase From' and 'Purchase To' to filter by purchase date.",
              "Click 'Filter'. Click 'Clear' to remove the filters.",
              "The number of lots is shown above the table, with a 'Filtered' badge when filters are on."]),
            ('Read the table',
             ['Columns: Lot #, GRN #, Supplier, Created, Purchase Date, Rcvd Date, Invoice No., Qty, '
              'Registered, Sold and Actions.',
              'Invoice Value, SGST, CGST, IGST and Buying Price only show if your role can see prices.',
              "'Registered' is the number of Tag Numbers created in the lot. 'Sold' is how many of them are "
              'sold.',
              "Click a Lot # to open the lot's detail page."]),
            ('Add or edit lots',
             ["Click 'New Lot' to create one lot.",
              "Click 'Bulk Upload Lots' to create many lots from a CSV file.",
              "Click 'Edit' on a row to change the lot details."]),
            ('Record a GRN for a lot',
             ["Click 'Validate GRN' on the lot row.",
              "Check 'Expected Qty' and enter the 'Received Qty' you actually counted.",
              "Optionally enter a 'GRN Reference No.' and 'Notes / Remarks'. If you leave the reference "
              'empty, one is created.',
              "Click 'Submit GRN'. The lot quantity is updated to the received quantity. If Expected and "
              "Received differ, a 'QTY MISMATCH' warning is shown."]),
            ('Export and delete',
             ["Click 'Export All CSV' (or 'Export Filtered CSV' when filters are on) to download the lot "
              'list.',
              "Tick lots and click 'Export Selected' to download all devices in those lots.",
              'Click the red bin icon to delete a lot. Confirm the message.'])],
  'tips': ['Deleting a lot is permanent. It also deletes every device in the lot and its history. It cannot '
           'be undone.',
           "Lot numbers must be unique. A duplicate shows 'Lot number already exists'.",
           "'New Lot' needs the 'add_lot' permission and saving an edit needs 'edit_lot'.",
           "'Validate GRN' is for admin and inventory manager only by default.",
           "Test lots starting with 'EPT-' are hidden unless you search for them."]},
 {'id': 9,
  'key': 'iqc',
  'section': 'Intake',
  'module': 'IQC Line Items',
  'url': '/iqc',
  'icon': 'bi-clipboard-check',
  'description': 'List of devices in the IQC stage, with model and lot summaries, where inspectors add new '
                 'assets, group tags into lots and export. Used by IQC inspectors and inventory managers.',
  'steps': [('Open and filter the list',
             ["Go to Intake -> IQC Line Items (page title 'IQC Inspection').",
              'The list shows devices in IQC by default.',
              "Search by Tag Number, model, brand or serial number, or use the 'Stage', 'Grade', 'Lot' and "
              "'Device Type' filters.",
              "Pick another stage in 'Stage' to search devices in that stage instead.",
              "Click 'Search'. Click 'Reset' to go back to the IQC list."]),
            ('Use the three tabs',
             ["'Tag Number' shows one row per device: Serial Number, Tag Number, Lot, Stage, Brand, Model, "
              'Device Type, CPU, RAM, Storage and Grade.',
              "'Model Summary' groups the filtered devices by model with Total Count, Total Price and "
              'Repaired.',
              "'Lot Numbers' shows the same lot list as Lot Overview. From here you can download a 'Sample "
              "File', 'Upload Lots', 'Add Lot' or 'Edit' a lot."]),
            ('Add devices',
             ["Click 'Add New Asset' to open the IQC entry form for one device.",
              "Click 'Bulk Upload IQC' to add many devices from a CSV file."]),
            ('Group tags into a new lot',
             ['Tick the devices in the Tag Number tab. Your ticks are kept when you change page or search.',
              "Click 'Create/Add Lot (n)'.",
              "Fill 'Lot Number', 'Purchase / GRN Date' and 'Supplier Name' (required) and any other fields "
              'such as Total Quantity, Condition, Buying Price and Remarks.',
              "Click 'Create Lot'. The selected devices are moved into the new lot."]),
            ('Edit, trash and export',
             ['Click the pencil icon on a row to edit the device. Click the red bin icon to move it to '
              'Trash.',
              "Click 'Export Selected (n)' to download only the ticked devices.",
              "Click 'Export All' to download every device matching the current filters."])],
  'tips': ["'Create/Add Lot' only creates a new lot. If the Lot Number already exists you get 'Lot Number "
           "... already exists'.",
           'Creating a lot from selection needs the IQC add permission.',
           'Moving a device to Trash is allowed for admin and inventory manager by default.',
           'After IQC, devices get a GRN on the GRN post IQC page, which moves them to Stock In.']},
 {'id': 10,
  'key': 'grn_records',
  'section': 'Intake',
  'module': 'GRN Records',
  'url': '/grn/records',
  'icon': 'bi-collection',
  'description': 'Board of all Tag Numbers split by GRN status, with a bulk move for Deshwal tags waiting in '
                 'IQC. Used by admins and inventory managers.',
  'steps': [('Open the board',
             ['Go to Intake -> GRN Records.',
              "Use 'Date From' / 'Date To' and click 'Filter' to limit the board to tags created in that "
              "period. Click 'Reset' to clear.",
              'The page has three tabs, each with a count badge.']),
            ('Understand the tabs',
             ["'Pending for TRC' lists Deshwal entity tags that are still at Stage IQC.",
              "'GRN Assigned' lists tags that already have a GRN number.",
              "'GRN Not Mapped' lists tags with no GRN number yet.",
              "Columns: Tag Number, Lot, Brand, Model, Grade, Stage, GRN Number (not on 'GRN Not Mapped') "
              'and Updated.',
              'Use the table search box to find a tag by Tag Number, brand or model.']),
            ('Bulk move Pending for TRC tags',
             ["Open 'Pending for TRC'.",
              'Tick the tags, or choose a CSV / text file of Tag Numbers to tick matching rows on the page.',
              "Click 'Bulk Move to TRC (n)'.",
              "Choose 'Sending for Sale' to move the tags to Stock In (Inventory Manager table) and keep the "
              'GRN.',
              "Or choose 'Sold to OxyPC' to keep the tags in IQC and clear the GRN, so they wait for a fresh "
              'GRN on GRN post IQC.',
              "Click 'Confirm & Move'. A message shows how many moved and lists any that could not move."]),
            ('Export',
             ['Open the tab you need and set the page size so all the rows you want are on screen.',
              "Click 'Export page' to download the rows shown on the current page as a CSV file."])],
  'tips': ["'Sold to OxyPC' is selected by default in the move window - check your choice before you "
           'confirm.',
           'Tags that are not allowed to move from their current stage are skipped and listed in a red '
           'message.',
           "The CSV tick and 'Export page' only work on rows on the current page. Show more rows per page "
           'first if needed.',
           'By default only admin and inventory manager can open this page.']},
 {'id': 11,
  'key': 'entity_movement',
  'section': 'Intake',
  'module': 'Entity Movement',
  'url': '/entity-movement',
  'icon': 'bi-arrow-left-right',
  'description': 'Move a batch of Tag Numbers from one company entity to another, either sold or sent, with '
                 'an optional invoice. Used by admins and inventory managers who handle transfers between '
                 'entities.',
  'steps': [('Open the page',
             ['Go to Intake -> Entity Movement.',
              "The cards at the top show how many active tags each entity has, plus 'Unassigned' tags with "
              'no entity.']),
            ('Enter the Tag Numbers',
             ["In 'Bulk Upload Tags - Change Entities', paste or type Tag Numbers, separated by commas or "
              'new lines.',
              "Or choose a CSV / text file (one Tag Number per line, or a 'barcode' column). Its tags are "
              'added to the box.',
              "Check the count under the box ('n tag(s) entered').",
              "Click 'Change Entities'."]),
            ('Choose the movement',
             ["Under 'SOLD TO (GRN removed)', pick the entity if the tags are sold to it. The GRN is cleared "
              'and the tags go back to IQC for re-inspection.',
              "Under 'SEND TO (GRN unchanged)', pick the entity if the tags are only sent to it. The GRN "
              'stays and the tags move to Stock In.',
              "Optionally enter an 'Invoice Number'. It is saved on every tag.",
              "Optionally attach the invoice in 'Upload Invoice PDF'.",
              "Click 'Apply'."]),
            ('Check the result',
             ['A message shows how many tags changed and lists any Tag Numbers that were not found.',
              "The 'Tag Number Movement' table records each batch: Movement ID, Date of Movement, Total Tag "
              'Counts, From Entity, To Entity, By User and Invoice PDF.',
              'Click the PDF icon to open the attached invoice.'])],
  'tips': ['Choose SOLD TO or SEND TO carefully: SOLD TO removes the GRN, so those tags must be mapped to a '
           'new GRN on GRN post IQC.',
           "If the tags came from more than one entity, the history shows 'mixed source entities'.",
           'Tag Numbers not found are skipped; the rest are still moved.',
           'Only the last 200 movements are shown in the history table.',
           'Access is controlled by the Entity Movement permission in the Permission Matrix.']},
 {'id': 12,
  'key': 'stock',
  'section': 'Inventory',
  'module': 'Stock Inwards',
  'url': '/stock',
  'icon': 'bi-box-arrow-in-down',
  'description': 'Stock Inwards lists every device that is now at the Stock In stage, after GRN and IQC. '
                 'Inventory managers use it to flag urgent tags, group tags into buckets, send devices to '
                 'L1/L2 repair, and handle customer return stock.',
  'steps': [('Open the page and read the summary',
             ['Go to Inventory -> Stock Inwards.',
              "The four tiles at the top show 'Devices in IQC', 'Devices in Stock', 'Devices in TRC' and "
              "'Devices in Ready to Sell'. These counts are for the whole inventory, not only this page.",
              "Below the tiles you see the 'Inventory Stock' table. It shows only devices at the Stock In "
              'stage.',
              'Columns: Tag Number (with the entity name under it), Lot, Location ID, Brand, Model, Device '
              'Type, Grade, Inside Bucket, Returned and Action.',
              'Small badges next to the Tag Number show if the tag is flagged as urgent.']),
            ('Search and filter',
             ["Type in the 'GRN / Invoice / Tag Number / Model' box to search the table at once, without "
              'reloading.',
              "Use the 'All Device Types', 'All Lot Numbers' and 'All Entities' dropdowns and the From / To "
              "dates, then click 'Filter'.",
              "Click 'Reset' to clear all filters.",
              'You can also scan a Tag Number into the scan box at the top right of the table. The matching '
              'row is found and ticked.']),
            ('Move devices to L1/L2 repair',
             ["For one device, click 'Move to L1/L2' in the Action column.",
              "For many devices, tick the checkboxes and click 'Bulk Move to L1/L2 (n)'. Confirm the "
              'message.',
              'The tags leave this table and go to the L1/L2 Repair queue with no engineer assigned. An '
              "engineer then picks them with 'Pick This'.",
              "If the Location ID column shows 'Assign', click it to give the device a storage location."]),
            ('Flag urgent tags and edit details in bulk',
             ["Tick one or more rows and click 'Flag Items (n)'.",
              "Fill 'Flag Authorized By', 'Flag Request Date', 'Flag Target Date' and 'Flag Reason'. All "
              "four are required. Click 'Save Flag'.",
              "To remove a flag, select the same tags, open 'Flag Items' and click 'Remove Flag'.",
              "Click 'Customise (n)' to change Device Type, Entity, Grade, Stage, Location ID and other "
              'fields for all selected tags at once.']),
            ('Work with buckets / cartons',
             ["Tick rows and click 'Assign Bucket (n)'. Type a 'Bucket Name' (for example Carton A1) and "
              "click 'Create Bucket'.",
              "If the name already exists, a warning shows how many tags are already in it. Click 'Clear "
              "Bucket' first if you want to empty it.",
              "The 'Buckets / Cartons' table lists buckets at Stock In. Click the bucket number or name to "
              "see its tags. Use 'Edit' to rename it.",
              "Click 'Move to Production' on a bucket to send the bucket and all its tags to the Production "
              'Manager (TRC Production stage).',
              "The 'Final QC Pass (Buckets)' table shows buckets that came back from Final QC as Ready to "
              "Sale. Click 'Release Bucket' to un-link the tags from the bucket."]),
            ('Handle Return Stock',
             ["The 'Return Stock' table lists tags returned by customers, with Reason, Condition, Complaint "
              'and costs.',
              "Click 'Charges' to enter or correct 'Repair Cost' and 'Labour Cost', then click 'Save'.",
              "Click 'Complete' to sell the tag again to the same customer. A new sale is created with the "
              'earlier customer details and the tag moves to Sold.',
              "Tick tags and click 'Assign Bucket (n)' to put return tags into a carton. The bucket is "
              "marked 'Customer Return'."])],
  'tips': ['Only admin, inventory manager, QC inspector and sales manager roles can open this page.',
           "'Move to L1/L2' works only for tags that are still at Stock In. If another user moved the tag "
           "first, you will see 'Device is not at Stock In stage'.",
           'The Flag Target Date cannot be before the Flag Request Date.',
           'A flag clears by itself when the tag moves back into Stock In.',
           'Part Cost in Return Stock is added only for out-of-warranty returns.']},
 {'id': 13,
  'key': 'scrap_products',
  'section': 'Inventory',
  'module': 'Scrap Products',
  'url': '/scrap-products',
  'icon': 'bi-trash3',
  'description': 'Scrap Products lists devices graded Scrap that are waiting to be sold as scrap, plus spare '
                 'parts that were scrapped. Inventory, L3 and sales staff use it to verify scrap, record '
                 'replacements and start a scrap sale.',
  'steps': [('Open the page',
             ['Go to Inventory -> Scrap Products.',
              'The count above the first table shows how many scrapped products there are.',
              'Columns: Scrapped Tag Numbers, Brand, Model, Grade, Received User (the engineer who sent it '
              'to scrap), Amount (total cost of the device in Rs), Updated On, Notes, Received and Action.',
              "'Received' shows 'Yes' when the scrap was verified and 'No' when it is still waiting."]),
            ('Verify a scrapped device',
             ["Click 'Verify' in the Action column of a row where Received is 'No'.",
              "Enter the 'Verified Quantity' and any 'Notes' (condition, remarks).",
              "Click 'Submit'. Received changes to 'Yes' and the 'Sell' button appears."]),
            ('Sell or view a device',
             ["After verification, click 'Sell' to open the new sale form with this Tag Number already "
              'filled in.',
              "Click 'View' to open the full device page.",
              'Click the Tag Number itself to open the device page too.']),
            ('Record a replacement scrap',
             ["Rows marked as 'Replacement Scrap' show a 'Replace' button instead of Verify / Sell.",
              "Click 'Replace' and type at least 2 characters in 'Search Tag Number'.",
              "Pick the replacement tag from the list and click 'Submit'.",
              "The table then shows '(Replace with <tag>)' under the scrapped Tag Number. The replacement "
              'tag itself is not changed.']),
            ('Check scrapped spare parts',
             ['The second table lists spare parts that were marked Scrap on the Parts Dashboard.',
              'Columns: Tag Number, Part Name, Category, Qty, Requested By, Scrapped By and Scrapped On.',
              'This table is read-only.'])],
  'tips': ['You cannot sell a scrap device until it is verified.',
           'Only admin, inventory manager, L3 engineer, sales and sales manager roles can open this page.',
           'If the device has no cost record, Amount uses the device price x quantity.']},
 {'id': 14,
  'key': 'inventory_requests',
  'section': 'Inventory',
  'module': 'Inventory Request',
  'url': '/inventory-requests',
  'icon': 'bi-inbox',
  'description': 'Inventory Request shows the dispatch requests that telecallers and sales staff raise from '
                 'the Ready to Sale page, for single tags and for whole lots. Inventory and sales managers '
                 'approve or reject them here.',
  'steps': [('Open the page',
             ['Go to Inventory -> Inventory Request.',
              "The top table, 'Tag Number Requests', lists requests for single Tag Numbers.",
              "The second table, 'Lot Number Requests', lists requests raised for a lot (with Model and "
              'Make).',
              'Each table shows a yellow badge with the number of pending requests.',
              'Columns include Telecaller Name, Request Date, Tag Number, Quantity Requested, Quantity '
              'Available and Action.']),
            ('Find requests',
             ["In 'Tag Number Requests', choose a name in 'All Telecallers' to see only that person's "
              'requests.',
              "Type in 'Search Tag Number...' to find a tag.",
              'Click a Tag Number to open the device page.']),
            ('Approve or reject one request',
             ["Click 'Approve' in the Action column. The status changes to 'Approved'.",
              "Click 'Reject' to open the 'Reject Request' box. Type the reason in 'Notes' (required) and "
              "click 'Reject'.",
              "Rejected requests show a red 'Rejected' badge. Move the mouse over it to read the reason."]),
            ('Approve many requests at once',
             ['Tick the checkboxes of pending rows. Only pending rows have a checkbox.',
              "Click 'Approve Selected (n)'.",
              'The message shows how many were approved and how many were skipped (for example already '
              'approved).']),
            ('Select tags from a file',
             ["Click 'Upload Tags' in 'Tag Number Requests'.",
              "Choose a CSV or TXT file with one column, header 'tag_number' (or 'barcode'), one tag per "
              'row.',
              "Click 'Select Tags'. Tags that have a pending request are ticked in the table. The box also "
              'lists tags with no pending request, tags with no request and tags not found.',
              "Nothing is approved by the upload. Check the ticked rows, then click 'Approve Selected'."])],
  'tips': ['Requests are raised from Sales & CRM -> Ready to Sale, not on this page.',
           'Admin, inventory manager, sales, sales manager and telecaller roles can open the page. Approve '
           'and Reject can be limited per role in the Permission Matrix.',
           'A request that is already approved or rejected cannot be approved again.',
           'Approving a request does not move the device or change its stage. It only marks the request as '
           'approved.']},
 {'id': 15,
  'key': 'model_requested',
  'section': 'Inventory',
  'module': 'Model Requested',
  'url': '/model-requested',
  'icon': 'bi-graph-up-arrow',
  'description': 'Model Requested is a read-only view of the models and configurations that Sales and '
                 'Telecalling have asked for, added up per configuration. TRC and inventory staff use it to '
                 'see what to prepare first.',
  'steps': [('Open the page and read the totals',
             ['Go to Inventory -> Model Requested.',
              "'Outstanding Units' shows how many units are still needed in total.",
              "'Total Requested' shows all units asked for.",
              "'Configs In Demand' shows how many configurations still have units outstanding."]),
            ('Read the demand table',
             ['Each row is one configuration: brand, model, CPU, RAM, storage and screen size, plus Category '
              'and Grade.',
              "'Requested', 'Fulfilled' and 'Outstanding' show unit counts. Outstanding = Requested minus "
              'Fulfilled.',
              "'Open Reqs' shows how many requests for this configuration are still open or partly "
              'fulfilled.',
              "'Requested By' lists the people who asked.",
              'Rows with the highest Outstanding number are shown first.']),
            ('Filter by status',
             ["Use the 'Status' dropdown: 'Open only', 'Partially Fulfilled' or 'Fulfilled'. The page "
              'reloads at once.',
              'With the default choice, all requests except cancelled ones are counted.',
              "Click 'Clear' to go back to the default."]),
            ('Change a request',
             ['This page has no edit buttons.',
              "Click 'Manage Requests' to open the Model Requests page, where requests are added, fulfilled "
              'or cancelled.'])],
  'tips': ['Cancelled requests are never counted here.',
           'Any signed-in user can open this page.',
           'Requests with the same configuration and grade are added together into one row.']},
 {'id': 16,
  'key': 'production_manager',
  'section': 'Inventory',
  'module': 'Production Manager',
  'url': '/trc-production',
  'icon': 'bi-gear-wide-connected',
  'description': 'Production Manager is the TRC control page. The production manager assigns tags and '
                 'buckets to engineers, follows every tag through repair, stress, cosmetic and Final QC, and '
                 'handles scrap from the repair line.',
  'steps': [('Read the summary tiles',
             ['Go to Inventory -> Production Manager.',
              "Tiles show 'Total Tags at You', 'Total Tags in L1/L2', 'Total Tags in L3/L4', 'Total Tags in "
              "PNA', 'Total Tags in Stress', 'Total Tags in Cosmetic', 'Total Tags in Final QC' and 'Tags "
              "Returned'.",
              "Click 'Summary' on any tile to download a CSV of exactly those tags.",
              "The PNA 'Summary' lists every part marked PNA for tags in L1/L2 or L3/L4, one row per part.",
              "The From / To date filter with 'Filter' and 'Reset' applies to the tables, not to the "
              'tiles.']),
            ('Assign tags to engineers (Tag Number Allocation)',
             ["The 'Tag Number Allocation' tab lists tags at TRC Production with Lot, Bucket Name, Brand, "
              'Model, RAM, Storage, Grade and Assigned User.',
              "Tags with a red 'Failed from Final QC' badge came back from Final QC. Move the mouse over the "
              "info icon to read the fail note. Tick 'Failed from Final QC' above the table to see only "
              'these.',
              "Click 'Assign' on a row, choose the engineer in 'Select Engineer' and click 'Assign'. A "
              'WorkID is created and the tag goes to L1/L2 repair.',
              "To assign many tags, tick them (or scan them into the search box) and click 'Bulk Assign "
              "(n)'."]),
            ('Assign buckets and As-Is lots',
             ["The 'Bucket Allocation' tab lists buckets sent from Stock Inwards. Click the bucket number to "
              'see its tags.',
              "Click 'Assign', choose the 'Repair Type' (L1/L2 Repair, QC or Stress, Cosmetic Repair) and, "
              "for L1/L2, the engineer. Click 'Assign'.",
              "The 'As-Is Lots in TRC' tab lists tags that carry a Sub-Lot Number, with their current stage "
              'and assigned user.',
              "Tick tags in that tab and click 'Export (n)' to download them as CSV."]),
            ('Change engineer or check history',
             ["Click 'Change Engineer'. Search a Tag Number from any stage (at least 2 characters) and pick "
              'it.',
              "The box shows the tag's stage history. Choose 'User Display Name' and 'Stage', then submit.",
              'The tag moves to the chosen stage, the old WorkID is closed, a new WorkID is created and the '
              'user gets a notification.',
              "Click 'Search Tags' to view the asset history of any tag without changing it."]),
            ('Use the Repair Line section',
             ["Click 'Repair Line' to open it. It has four tabs.",
              "'Final QC Fail (Bucket)': buckets that failed Final QC. Click 'Assign' to send them to L1/L2, "
              'QC or Stress, or Cosmetic.',
              "'Tag Number in Repair Line': tags now in L1, L2 or L3 repair, with the assigned engineer's "
              'name.',
              "'Buckets in Repair Line': assigned buckets with Total Pass and Total Fail. Click 'Release "
              "Bucket' to un-link all tags from that bucket.",
              "'Scrap from Repair Line': scrapped tags. Click 'Move to Inventory' to send the tag to Scrap "
              "for Sale, or 'Replace With Another' to record which tag replaced it."])],
  'tips': ['Only admin, inventory manager, QC inspector and sales manager roles can open this page.',
           "The 'Assign' engineer list shows only L1 and L2 engineers. 'Change Engineer' shows all active "
           'users.',
           'Release Bucket cannot be undone from this page - you must map the tags to a bucket again.',
           'The PNA tile counts only L1/L2 tags, but its Summary file also includes L3/L4, so the numbers '
           'can differ.']},
 {'id': 17,
  'key': 'extended_warranty',
  'section': 'Inventory',
  'module': 'Extended Warranty',
  'url': '/extended-warranty',
  'icon': 'bi-shield-plus',
  'description': 'Extended Warranty lets staff set or extend the warranty on a device that is already sold. '
                 'It also shows a register of all sold devices with a warranty and how many days are left.',
  'steps': [('Open the page',
             ['Go to Inventory -> Extended Warranty.',
              "The top card is 'Set Warranty'. The table below is the 'Warranty Register'."]),
            ('Set a warranty type for a sold tag',
             ["Scan or type the Tag Number in 'Tag Number' and click the search button.",
              "'Lot Number', 'Warranty Starts' (the sale date) and 'Warranty Stops' fill in by themselves. A "
              "badge shows days left, 'Expired' or 'Not Set'.",
              "Choose a value in 'Warranty (in days)'. 'Warranty Stops' updates to show the new end date.",
              "Click the yellow tick button to save. The message 'Warranty set' appears."]),
            ('Use the Warranty Register',
             ['Columns: Tag Number, Lot Number, Warranty Left, Warranty (in days), Warranty Starts, Warranty '
              'Stops and Action.',
              "'Warranty Left' is green when more than 7 days remain, yellow for 7 days or less, and red "
              "when 'Expired'.",
              'Tags that expire soonest are listed first. Use the table search box to find a tag.']),
            ('Extend a warranty to a new date',
             ["Click 'Update' on the row.",
              "In 'Extend Warranty', pick the new end date in 'Extended Warranty'.",
              "Add 'Notes' if needed and click 'Save'.",
              "The extra days are added to 'Warranty (in days)' and the note is saved on the sale with your "
              'name and the date.'])],
  'tips': ["The tag must have a sale. If not, you will see 'No sale found for' the tag.",
           "'Set Warranty' always counts from the sale date. It replaces the old end date, it does not add "
           'to it.',
           "In 'Update', the new date must be after the current Warranty Stops date.",
           'Access to this page is controlled by the Permission Matrix.']},
 {'id': 18,
  'key': 'part_estimation',
  'section': 'Inventory',
  'module': 'Part Estimation',
  'url': '/part-estimation',
  'icon': 'bi-calculator',
  'description': 'Part Estimation is used to cost the spare parts and repair charges a lot needs before '
                 'production starts. The estimate is saved as an Excel file on the lot and sent to the Parts '
                 'team as a sourcing request.',
  'steps': [('Open the page',
             ['Go to Inventory -> Part Estimation.',
              "The 'Lots Summary' table lists every lot: Lot Number, Date of Purchase, Total Products, Total "
              'Parts Required, IQC Data, Download and Action.',
              'Click a Lot Number to open that lot in Lot Overview.',
              "The red button at the top right, 'Recent PNA under L1/L2/L3/L4', shows how many tags have PNA "
              'parts. Click it to download the PNA parts list.']),
            ('Download IQC data',
             ["Click 'IQC Data' on a lot row.",
              'An Excel file downloads with every device in the lot and its full IQC inspection record.']),
            ('Create an Open Estimate',
             ["Click 'Open Estimate' on the lot row.",
              'A grid opens. Rows are parts. Columns are the models in the lot, with the tag count in '
              'brackets.',
              'Type a quantity in any Part / Model cell that needs it. Leave other cells empty.',
              "In the 'Repair Charge' row, type the repair charge per unit for each model.",
              "'Total Parts' and 'Total Estimate Value' update as you type. Each value is multiplied by that "
              "model's tag count."]),
            ('Generate the file',
             ["Choose a grade in 'Select Grade'.",
              "Click 'Generate Estimate'. An Excel file is made for that grade and a sourcing request goes "
              'to the Parts team.',
              "The box stays open. To make a file for another grade, change the grade and click 'Generate "
              "Estimate' again.",
              "When you close the box, the page reloads and the new files show in the 'Download' column, "
              'named by grade.']),
            ('Download or remove an estimate file',
             ["In the 'Download' column, click the grade name to download that Excel file.",
              'Click the small trash icon next to it to remove the file. Confirm the message.',
              'Removing the file also withdraws its sourcing request. The figures you typed are kept and '
              'fill in again next time.'])],
  'tips': ["'Open Estimate' is disabled for lots that have no tags.",
           "You must fill at least one cell and select a grade, or you will see 'No cells were filled in' or "
           "'Select a Grade'.",
           "Tag counts per model always come from the lot's current tags, not from the screen.",
           'Access to this page is controlled by the Permission Matrix.']},
 {'id': 19,
  'key': 'credit_note',
  'section': 'Inventory',
  'module': 'Credit Note',
  'url': '/credit-note',
  'icon': 'bi-receipt-cutoff',
  'description': 'Credit Note tracks returned tags through the credit note steps, from Return Received to CN '
                 'Complete. Accounts and inventory staff move each record to the next step, export records '
                 'and delete wrong entries.',
  'steps': [('Understand the steps',
             ["Records come here from the 'Credit Note' tab of the Returns page, where the Debit Note Number "
              'and Amount are entered.',
              'The steps are: Return Received -> Debit Note Verified -> Verify Payment -> Payment Invoice '
              'Done -> Restock Pending -> CN Complete.',
              'The tiles at the top show how many records are at each step.']),
            ('Find records',
             ['Go to Inventory -> Credit Note.',
              "Type in 'Tag Number / Sale ID / Payment Invoice' to search.",
              "Use 'All Lots', 'All Sub-Lots' and 'All Statuses' (tag return status), then click 'Filter'. "
              "Click 'Reset' to clear.",
              'Columns: Tag Number, Tag Return Status, Lot Number, Sub-Lot Number, Device Price, Sale Date, '
              'Debit Amount, Debit Number, Sale ID, Payment Invoice, Sender, Status and Action.',
              "Click 'Contact' in the Sender column to see the sender's name, phone and email."]),
            ('Move one record to the next step',
             ["In the Action column, click the button for the next step. On the first step it says 'Verify "
              "Debit Note'.",
              "When the next step is 'Payment Invoice Done', type the invoice number in 'Invoice #' first, "
              'then click the button.',
              'The Status column changes to the new step.']),
            ('Work on many records',
             ['Tick the checkboxes of the rows you want.',
              "Click 'Verify (n)' to move each row one step forward from its own current step.",
              "Click 'Export (n)' to download the selected rows as a CSV file.",
              "Click 'Delete (n)' to delete the selected records."]),
            ('Delete a record',
             ['Click the red trash icon in the Action column and confirm.',
              'The return record is deleted for good. It also leaves the Return Stock table on Stock '
              'Inwards.',
              'If the tag has no other return, its return flags are cleared.'])],
  'tips': ["'Verify Debit Note' fails if the Debit Note Number was not set on the Returns page.",
           "Bulk 'Verify' skips rows whose next step is 'Payment Invoice Done'. Do those one by one, so you "
           'can type the invoice number.',
           'Delete cannot be undone.',
           'Rows at CN Complete cannot be ticked.',
           'Viewing, and moving or deleting records, are controlled separately in the Permission Matrix.']},
 {'id': 20,
  'key': 'transfers',
  'section': 'Inventory',
  'module': 'Move Device',
  'url': '/transfers',
  'icon': 'bi-arrow-left-right',
  'description': 'Move Device records floor movements of tags, lots and spare parts, and assigns them to an '
                 'employee. It also keeps a log of every movement made from this page.',
  'steps': [('Open the movement log',
             ['Go to Inventory -> Move Device.',
              "The table 'Floor Movements done so far' lists every movement saved from this page.",
              'Columns: Date, Tag Number, Location ID, Make / Model, Lot, Transferred By, Received By, Type '
              'and Stage.',
              "Lot and Stage show the device's current values, not the values at the time of the move."]),
            ('Search and export',
             ["Type in 'Search Tag Number...', or choose 'All Types', 'All Transferred By', 'All Locations' "
              "and a date range. Click 'Search'.",
              "Click 'Clear' to remove the filters.",
              "Click 'Export' to download the rows you see as a CSV file. The file also has Serial Number, "
              'CPU, GEN, RAM and Storage.']),
            ('Move tags (Move Item)',
             ["Click 'New Transfer'. The 'Move Item' tab opens.",
              "Scan or type each Tag Number and press Enter or click 'Add'. Remove a wrong tag with its red "
              'button.',
              "Choose 'Transfer Type' and 'Assign To Employee' (both required). Choose a 'Location ID' if "
              "the tags change place. Add 'Notes' if needed.",
              "For 'As-Is Lot', choose 'Current Lot' or 'New Sub-Lot'. With 'New Sub-Lot', type the sub-lot "
              'and it is saved on all scanned tags.',
              "Click 'Assign this'. The employee gets a notification, and the device location changes if you "
              'chose a Location ID.']),
            ('Move a lot or parts',
             ["'Move Lot' tab: scan or type one or more Lot Numbers and click 'Add'. Fill the same fields "
              "and click 'Assign this'. Every active device in the lot is assigned to the employee.",
              "'Move Parts' tab: choose 'Part Name', type 'Quantities' (at least 1), choose the type and "
              'employee, then submit.']),
            ('Delete wrong records',
             ["Tick the rows and click 'Delete Selected (n)'. Confirm the message.",
              'The movement records are deleted for good. This button shows only if your role has edit '
              'rights for this page.'])],
  'tips': ['A move does not change the device stage. It records the movement and assigns the tag to the '
           'employee.',
           "Transfer Type 'Scrap for Sale' creates one new scrap batch for the whole submission. It then "
           'shows on the Scrap for Sale page.',
           "'Finish Good' and 'As-Is Lot' also save the employee as 'Assigned To' on each device.",
           'Any signed-in user can see the log. Only admin, inventory manager, QC inspector and sales '
           'manager can make a new transfer or export.',
           'Tags that are not found are skipped. The success message shows how many were not found. If none '
           'of the scanned tags are found, nothing is saved.']},
 {'id': 21,
  'key': 'repair_l1',
  'section': 'Repair',
  'module': 'L1 Repair',
  'url': '/repair/l1',
  'icon': 'bi-tools',
  'description': 'The L1/L2 repair queue. L1 and L2 engineers pick tags, start repair, request parts, send '
                 'hard cases to L3/L4 and move finished tags to Stress Test.',
  'steps': [('Open the page and read the table',
             ["Go to REPAIR -> L1 Repair. The page title is 'L1/L2 Repair Stage' and the table is 'Devices "
              "in L1/L2'.",
              'Engineers see the tags assigned to them plus all unclaimed tags. Admin and Inventory Manager '
              'see every tag in the queue.',
              'Columns: Tag Number, WorkID, Date Assigned, Bucket, Parts Required, Parts Requested, PNA '
              'Parts, Repair Notes, Stress Notes, Aging, Status, L3/L4 Status, Action.',
              "Newest assignment is shown first. Aging turns yellow at 1 day and red at 3 days. 'Paused' "
              'means a required part is out of stock, so the aging clock is frozen.',
              "A red 'Failed from Final QC' badge under the Tag Number means the tag came back from Final "
              'QC. Hover the info icon to read the fail note.',
              "A red 'Scrap Risk Detected' box at the top lists tags whose repair cost is close to the "
              'expected sale value.']),
            ('Find tags with the filters',
             ["Use the boxes in the table header: 'Search Tag / GRN / Model', CPU, RAM, Hard Drive and the "
              "'All Lots' dropdown.",
              "Tick 'PNA' to show only tags with parts marked Not Available. Tick 'Failed from Final QC' to "
              'show only returned tags.',
              'The filters also update the Bulk Part Request table below, so it only counts the tags you can '
              'see.',
              "Click 'Search Tags' to look up any Tag Number in the system and view its current stage and "
              'recent stage history. This does not change anything.']),
            ('Pick and start a repair',
             ["If a tag has no WorkID yet, click 'Pick This' in the WorkID column. A new WorkID is created "
              'in your name and the tag becomes yours.',
              "Click 'Start Repair'. The Status changes to 'Repair Started'.",
              "Click 'Request Part' (yellow button) at any time to open the Device Detail page and raise a "
              'part request for that tag.',
              'Click the Tag Number to open the full Device Detail page.']),
            ('Finish the repair or send it to L3/L4',
             ["When the repair is done, click 'Mark Complete'. The tag moves to Stress Test and gets a new "
              'WorkID there (unassigned).',
              "If the tag still has open part requests, an 'Open Part Requests' box appears first. Parts "
              'already handed over are auto-verified as changed and their cost is added. Parts never handed '
              "over are cancelled with no cost. Click 'Resolve & Complete' to continue or 'Go Back' to stop.",
              "If the fault needs a higher level, click 'Request to L3/L4', choose the 'L3/L4 Engineer', add "
              "optional 'Repair Notes' and click 'Request'.",
              "The tag leaves this list and appears on the L3/L4 engineer's page. The engineer gets a "
              'notification.',
              "When L3/L4 finishes, the tag comes back here with Status 'Returned from L3/L4' and the L3/L4 "
              'Status filled in.']),
            ('Handle a scrap decision from L3/L4',
             ["If L3/L4 Status shows 'Normal Scrap' or 'Replacement Scrap', the Action column shows 'Back to "
              "Inventory'.",
              "Click 'Back to Inventory'. The tag gets grade Scrap and moves to the Tags Scrapped page."]),
            ('Raise part requests for many tags at once',
             ["Scroll to 'Bulk Part Request'. Each row is one part with Total Quantity (tags that need it), "
              'Total Requested and Total Changed.',
              "Click 'New Request' or 'Replace Request'. For RAM and Hard Drive you can also click "
              "'Downgrade'.",
              'In the popup choose Part Name, Part Make and Part Model (and Normal or Replace for a '
              "downgrade), then click 'Raise Requests'.",
              'One request is raised for every listed tag that needs the part. Tags that already have an '
              'open request for that part are skipped.'])],
  'tips': ["You need the Add permission on L1 Repair to see the 'Pick This' button.",
           'Pick a tag before you start it, so the work is counted against your name on WorkID Status.',
           "Always check the 'Open Part Requests' warning before you confirm Mark Complete - unhanded parts "
           'will be cancelled.',
           'A tag sent to L3/L4 disappears from this page until L3/L4 marks it Complete or Scrap.',
           'The Bulk Part Request is safe to press twice - tags with an open request for the same part are '
           'skipped.']},
 {'id': 22,
  'key': 'repair_l3l4',
  'section': 'Repair',
  'module': 'L3/L4 Repair',
  'url': '/repair/l3l4',
  'icon': 'bi-cpu-fill',
  'description': 'Work queue for L3/L4 engineers. It lists tags sent up from L1/L2, where the engineer '
                 'starts the repair, then marks it complete or scraps it.',
  'steps': [('Open the page',
             ["Go to REPAIR -> L3/L4 Repair. The table is 'L3/L4 Work Orders'.",
              'Engineers see only the tags assigned to them. Admin and Inventory Manager see all open L3/L4 '
              'work.',
              'Columns: Tag Number, WorkID, Date Assigned, Bucket, Repair Notes, Aging, Status, PNA Parts, '
              'Action.',
              "The WorkID starts with 'L3L4-'. Repair Notes shows what the L1/L2 engineer wrote. Newest "
              'assignment is shown first.']),
            ('Search and filter',
             ['Use the table search box to find a Tag Number or WorkID.',
              "Tick 'Only show PNA' to list only tags with parts marked Not Available. Hover the red PNA "
              'number to see which parts.',
              "Click 'Search Tags' to view the stage history of any tag in the system."]),
            ('Start the repair',
             ["Click 'Start Repair' on the row. The Status changes to 'Repair Started'.",
              "Click 'Request Part' (yellow button) to open the Device Detail page and raise a part "
              'request.']),
            ('Complete the repair',
             ["Click 'Mark Complete'. In the popup choose 'Activities' and type 'Repair Notes' (both "
              'optional).',
              "Click 'Confirm Complete'. The WorkID is closed and the tag goes back to the L1/L2 queue with "
              "Status 'Returned from L3/L4'.",
              "The L1/L2 engineer then does 'Mark Complete' there to send it to Stress Test."]),
            ('Scrap a device',
             ["Click 'Scrap' (only shown after the repair is started).",
              "Choose 'Normal Scrap' or 'Replacement Scrap' and click 'Confirm Scrap'.",
              "The tag goes back to the L1/L2 queue showing the scrap type. From there someone clicks 'Back "
              "to Inventory' to send it to Tags Scrapped."])],
  'tips': ["'Mark Complete' and 'Scrap' only appear after you click 'Start Repair'.",
           'Scrap here does not scrap the tag at once - it records the decision and returns the tag to L1/L2 '
           "for the final 'Back to Inventory' step.",
           'The Activities list comes from Master Data. Ask an admin if the activity you need is missing.',
           'If a tag is moved out of L3 by a manual stage move, its L3/L4 WorkID is closed automatically and '
           'it leaves this list.']},
 {'id': 23,
  'key': 'qc_check',
  'section': 'Repair',
  'module': 'Stress Test',
  'url': '/qc',
  'icon': 'bi-patch-check',
  'description': 'Checklist test for every repaired tag. QC inspectors mark each item Pass or Fail, save a '
                 'PDF report, then send the tag to Cosmetic or back to an L1/L2 engineer.',
  'steps': [('Open the page',
             ["Go to REPAIR -> Stress Test. The left table is 'Devices in Stress Test'. The right panel is "
              "the 'Stress Test' checklist.",
              'Columns: Tag Number, Lot, L1/L2 Engineer (who last repaired it), Brand, Model, Grade, Report, '
              'Actions.',
              "If a report was saved before, the Report column shows a 'PDF' button (green = PASS, red = "
              'FAIL). Click it to download.',
              "Tick 'Failed from Final QC' to show only tags returned from Final QC. Click 'Search Tags' to "
              "view any tag's stage history."]),
            ('Run the checklist',
             ["Click 'Stress' on a row. The row turns yellow and the checklist opens on the right with the "
              'Tag Number at the top.',
              "All items start as Pass. Untick and tick 'Fail' for any item that failed. Only one choice is "
              'allowed per item.',
              'Items include CPU, RAM, Hard Drive, Screen, Keyboard, Battery, Wi-fi, ports, Thermal, Touch '
              'Screen, Cosmetic Check, Software/License Key and more.',
              "'Touch Screen (if exists)' also has 'N/A'. It starts as N/A unless the tag is recorded as a "
              'Touch device.',
              "Use the 'All Pass' box to set every item to Pass in one click."]),
            ('Save the report',
             ["Click 'Save'. The checklist is saved and the PDF report downloads. The tag does not move.",
              'Saving again in the same visit updates the same report. A tag that returns to Stress Test '
              'later gets a new report.']),
            ('Pass the tag - Complete',
             ["Click 'Complete'. If any item is marked Fail you are asked to confirm.",
              'The tag moves to Cosmetic Received (Cosmetic & Paint) and leaves this list. The checklist and '
              'PDF are saved with it.',
              'Any old stress fail note on the tag is cleared.']),
            ('Fail the tag',
             ["Mark the failed items, then click 'Fail'. The popup 'Fail - Assign to L1/L2 Engineer' shows "
              'the failed items.',
              "Choose the 'L1/L2 Engineer', add 'Notes' and click 'Assign'.",
              'The tag goes back to L1/L2 Repair with a new WorkID for that engineer, who gets a '
              'notification.',
              "The failed items and notes appear in the 'Stress Notes' column on L1 Repair."])],
  'tips': ['Every item must have Pass, Fail or N/A before Save, Fail or Complete will work.',
           'Use Fail, not Complete, when hardware still has a fault - Complete sends the tag to Cosmetic.',
           "You must select an engineer before the Fail 'Assign' button will work.",
           'Both Fail and Complete record a WorkID in your name on WorkID Status for the test you did.']},
 {'id': 24,
  'key': 'cosmetic_received',
  'section': 'Repair',
  'module': 'Cosmetic & Paint',
  'url': '/cosmetic/cosmetic_received',
  'icon': 'bi-droplet',
  'description': 'Landing page of the cosmetic line. Tags that passed Stress Test wait here in Cosmetic '
                 'Received until they are assigned to Cleaning, sent straight to Final QC, or failed back to '
                 'L1/L2.',
  'steps': [('Open the page',
             ["Go to REPAIR -> Cosmetic & Paint. It opens on 'Cosmetic Received'.",
              'Managers see a row of stage buttons at the top: Cosmetic Received, Cleaning, Putty, Dry '
              'Sanding, Masking, Painting, Water Sanding, Cosmetic Completed and All Tags. Click one to open '
              'that stage.',
              'Admin, Cosmetic Manager and Group Config team managers see every tag. Other users see only '
              'tags assigned to them on this stage, and do not see the stage buttons.',
              'Columns: Tag Number, WorkID, L1/L2 Engineer, Lot, Grade, Brand, Model, Aging, Assigned to, '
              'Assigned Date, Action.']),
            ('Search and filter',
             ['Type in the table search box to find a tag.',
              "Tick 'Failed from Final QC' to see only tags that came back from Final QC. Hover the info "
              'icon next to the red badge to read the fail note.']),
            ('Move a tag to Cleaning or Final QC',
             ["Click 'Move to Cleaning'. The popup 'Move - Assign to User' opens.",
              "Choose the user in 'Assign to', add optional 'Notes' and click 'Move'.",
              'The tag moves to Cleaning with a new WorkID for that user, who gets a notification.',
              "If the tag needs no cosmetic work, click 'Move to Final QC' instead. No user is needed and "
              'the tag goes straight to the Final QC page.']),
            ('Fail a tag back to repair',
             ["Click 'Fail'. Choose the 'L1/L2 Engineer', type the reason in 'Notes' and click 'Assign'.",
              'The tag goes back to L1/L2 Repair with a new WorkID for that engineer. The note is saved in '
              'Repair Notes.']),
            ('Assign many tags at once (Admin / Cosmetic Manager)',
             ['Tick the boxes on the left of the rows, or tick the header box to select all rows that match '
              'the search.',
              'You can also scan or type a Tag Number in the search box and press Enter to tick it.',
              "Click 'Assign', choose the user in 'Assign to', add optional notes and click 'Assign'.",
              'Every selected tag moves to Cleaning with its own new WorkID for that user.']),
            ('All Tags and Flow Data',
             ["Click 'All Tags' to see every tag in the cosmetic line in one read-only table, with a Stage "
              "column and an 'All Stages' filter.",
              "Admin and Cosmetic Manager see a 'Flow Data' table under it. Click 'Add Row', give a Flow "
              'Name, pick one user per stage (Cleaning to Water Sanding) and click the save tick.',
              'When a user in a saved flow moves a tag, it is assigned automatically to the next user in the '
              'same row, with no popup.'])],
  'tips': ['Moving a tag needs the Edit permission on that cosmetic stage.',
           'If you are not a manager, a tag only shows here once it is assigned to you.',
           "Tags arrive here from the Stress Test 'Complete' button without an assigned user - assign them "
           "with 'Move to Cleaning' or 'Assign'.",
           "The bulk 'Assign' button and the checkboxes are only for Admin and Cosmetic Manager."]},
 {'id': 25,
  'key': 'cosmetic_page',
  'section': 'Repair',
  'module': 'Cosmetic Stage',
  'url': '/cosmetic/cleaning',
  'icon': 'bi-droplet-half',
  'description': 'The work page for one cosmetic stage: Cleaning, Putty, Dry Sanding, Masking, Painting, '
                 'Water Sanding or Cosmetic Completed. Cosmetic workers see their own tags and move each one '
                 'to the next stage when done.',
  'steps': [('Open your stage page',
             ['Go to REPAIR -> Cosmetic Stage. It opens the first stage your role is allowed to work on, in '
              'line order.',
              'Managers can also reach every stage from the stage buttons on the Cosmetic & Paint page.',
              'The line order is: Cosmetic Received -> Cleaning -> Putty -> Dry Sanding -> Masking -> '
              'Painting -> Water Sanding -> Cosmetic Completed -> Final QC.',
              'Admin, Cosmetic Manager and Group Config managers see every tag on the stage. Other users see '
              'only tags assigned to them.']),
            ('Read the table',
             ["The table is 'Devices in <stage name>'.",
              'Columns: Tag Number, WorkID, L1/L2 Engineer, Brand, Model, CPU, Grade, Lot, Since, Action.',
              "WorkID is the work number given to the person assigned on this stage. 'Since' is when the tag "
              'last changed.',
              'Use the table search box to find a tag.']),
            ('Move a tag to the next stage',
             ["When your work is done, click 'Move to <next stage>' (for example 'Move to Putty').",
              'If your name is in a saved Flow Data row, the tag moves at once and is assigned to the next '
              'person in that row.',
              "If not, the popup 'Move - Assign to User' opens. Choose the user in 'Assign to', add optional "
              "'Notes' and click 'Move'.",
              'The tag gets a new WorkID for the next user and your WorkID is closed. The page reloads and '
              'the tag leaves your list.',
              'From Water Sanding the tag moves to Cosmetic Completed with no user to choose.']),
            ('Cosmetic Completed stage',
             ['This holding stage shows extra columns: Cosmetic Stage, Aging, Assigned to and Assigned Date.',
              "Click 'Move to Final QC' to send the tag to Final QC. No user is needed.",
              "Click 'Fail' to send it back to L1/L2: choose the 'L1/L2 Engineer', add 'Notes' and click "
              "'Assign'."]),
            ('Reassign tags (Admin only)',
             ["Admin sees checkboxes on each row. Tick the tags, click 'Assign', choose the user and click "
              "'Assign'.",
              'Each tag gets a new WorkID for that user on the same stage. The tags do not change stage.'])],
  'tips': ['Moving a tag needs the Edit permission for your stage in the Permission Matrix.',
           'If the Move popup opens, it means no Flow Data row matched you - just pick the next user by '
           'hand.',
           'Fail to L1/L2 is only on Cosmetic Received and Cosmetic Completed, not on the middle stages.',
           'A tag only shows on your page once it is assigned to you on that stage. Ask your manager if a '
           'tag is missing.']},
 {'id': 26,
  'key': 'cosmetic_finalqc',
  'section': 'Repair',
  'module': 'Final QC',
  'url': '/cosmetic/final_qc',
  'icon': 'bi-shield-check',
  'description': 'Last check before a tag is sold. The Final QC inspector reviews IQC, repair history, parts '
                 'and price, records Pass or Fail, then moves passed tags to Ready to Sale and routes failed '
                 'tags back for rework.',
  'steps': [('Open the page and pick a tag',
             ['Go to REPAIR -> Final QC. The badge at the top shows how many devices are awaiting Final QC.',
              'The left list shows each tag with serial number, brand and model. Click a tag to open its '
              'details on the right.',
              "Use 'Scan / search Tag Number' to jump to a tag, or the 'All Entities' dropdown to filter by "
              'entity.',
              "The header shows Lot, L1/L2 Engineer, Grade and a 'Stress/QC Report' button to download the "
              'latest stress report.']),
            ('Review the tag',
             ["'Full IQC' tab: the full incoming inspection report, hardware and cosmetic.",
              "'Repair History & Parts' tab: repair jobs, Repair Notes, and each required part marked "
              "'Changed' or 'Not Changed'.",
              'Users allowed to see pricing also see Pricing: Current Unit Price (or Avg Lot Price), Labour '
              'Price, changed parts, After Repair Price and Updated Price.']),
            ('Record the decision',
             ["Open the 'Final QC' tab. Type a 'Bucket Name' if needed (a new name creates a new bucket; "
              'blank removes the bucket).',
              "Choose 'Touch Type': Touch or Non-Touch.",
              "For a pass: set 'Final Status' to Pass, choose 'Grade' (A, B, C or No change), add 'Pass "
              "Notes' and click 'Complete'.",
              "For a fail: set 'Final Status' to Fail, choose the 'Failure Reason' (Hardware, Software or "
              "Cosmetic), add a 'Fail Note' and click 'Fail'.",
              "The tag moves to the 'Devices Passed' or 'Devices Failed' tab below. A WorkID is recorded in "
              'your name.']),
            ('Devices Passed - move to inventory',
             ["Open 'Devices Passed'. Columns: Tag Number, Bucket Name, Stage, Pass Notes, Action.",
              "Click 'Move to Inventory' for one tag, or tick several and click 'Bulk Move to Inventory'. "
              'Confirm the message.',
              'The tag moves to Ready to Sale.']),
            ('Devices Failed - send back for rework',
             ["Open 'Devices Failed'. Columns: Tag Number, Bucket Name, Stage, Engineer Name, Failure "
              'Reason, Fail Note, Action.',
              "Click 'Move' for one tag or tick several and click 'Bulk Move'. Tick 'No Engineer' to see "
              'tags with no engineer found.',
              'Hardware goes to L1/L2 Repair, to the engineer who last repaired it. If no engineer is found '
              'it goes to the Tag Number Allocation queue in Production Manager.',
              'Software goes to Stress Test. Cosmetic goes to Cosmetic Received.',
              'A message shows how many tags moved and which were skipped and why.']),
            ('Correct a decision',
             ["In either tab click 'Edit' and confirm. The tag goes back to the pending list at the top so "
              'you can decide again.',
              'The old status, reason and notes are cleared.'])],
  'tips': ['Recording a decision, moving and Edit all need the Edit permission on Final QC.',
           'Always pick a Failure Reason on a fail - a tag with no reason cannot be moved from Devices '
           'Failed.',
           'On the first pass the device price is finalized as purchase price plus parts consumed plus parts '
           'changed. A second pass does not add the costs again.',
           'Verified RAM and Hard Drive changes are written onto the device specification when it passes.',
           "Passed tags do not go to Ready to Sale until you click 'Move to Inventory'."]},
 {'id': 27,
  'key': 'workid_status',
  'section': 'Repair',
  'module': 'WorkID Status',
  'url': '/workid-status',
  'icon': 'bi-clipboard-check',
  'description': 'One list of every WorkID (job) given to engineers and workers across repair, stress test '
                 'and cosmetic stages, with aging, completion and PNA details. Used by engineers to track '
                 'their work and by managers to track their team.',
  'steps': [('Open the page',
             ['Go to REPAIR -> WorkID Status.',
              "Admin sees every WorkID. Group Config managers see their own and their team's WorkIDs. "
              'Everyone else sees only their own.',
              "The table 'Work ID Details' shows: Tag Number, WorkID, Model, Stage, Aging, Assigned Date, "
              'Completed Date, Notes, Assigned Engineer, PNA, PNA History.',
              'Rows are sorted by Assigned Date, newest first. Click a Tag Number to open the Device Detail '
              'page.']),
            ('Read the count tiles',
             ['Open WorkIDs and Closed WorkIDs count jobs.',
              'Assigned Tags (not started), Ongoing Tags (started) and Completed Tags count tags.',
              'Total PNA counts rows where a part is marked Not Available.',
              'The tiles follow the same filters as the table.']),
            ('Filter the list',
             ["Use 'Search WorkID', 'Search Tag Number', the engineer dropdown, 'Stage', and the Completed "
              'From / Completed To dates.',
              "Tick 'Exclude Admin' to hide jobs done by admin users.",
              "Click 'Search' to apply. Click 'Reset' to clear all filters.",
              'The engineer dropdown only appears when more than one engineer is in your view.']),
            ('Understand the columns',
             ['Completed Date stays blank while the WorkID is still open with that person.',
              'Aging counts days from the Assigned Date. It keeps counting while open and stops when the '
              'WorkID is completed.',
              'PNA History lists parts marked Not Available while that person held the tag, with who marked '
              'and cleared them.']),
            ('Export',
             ["Click 'Export' to download a CSV of exactly the rows shown with your current filters.",
              'The file has Tag Number, Lot Number, Make, Model, Engineer Name, Stage, Assigned Date, '
              'Completed Date, PNA and PNA History.'])],
  'tips': ['The page shows at most the 3,000 most recent WorkIDs. A blue note appears when more match - '
           'narrow by WorkID, Tag, Engineer or a date range.',
           'A Completed date filter only shows WorkIDs that are already completed.',
           'This page is read-only. WorkIDs are created and closed by actions on the repair, stress test and '
           'cosmetic pages.',
           'Export never includes rows you are not allowed to see on screen.']},
 {'id': 28,
  'key': 'pm_product_master',
  'section': 'Product Master',
  'module': 'Product Master',
  'url': '/product-master',
  'icon': 'bi-box-seam',
  'description': 'Master list of sellable product configurations (Make/Model/CPU/CPU Gen) with their prices, '
                 'plus the dropdown values and competitor lists used by the other Product Master pages. Used '
                 'by admins and pricing staff.',
  'steps': [('Open the page',
             ['Go to PRODUCT MASTER -> Product Master.',
              'Tiles at the top count All Records, Product Price Pending, Parts Price Pending, Pricing Done, '
              'Active and Inactive.',
              "There are three tabs: 'Product Master Table', 'Dropdown Configuration' and 'Competitive "
              "Pricings'."]),
            ('Set up dropdown values first',
             ["Open 'Dropdown Configuration'. There is one card each for Make, Model, CPU, CPU Gen, RAM "
              'Types, RAM Sizes, Storage Types, Storage Sizes and Discount %.',
              "Click 'Add' on a card, type the value and click 'Save'. For Model you must also choose its "
              'Make.',
              "CPU Gen is saved as typed, for example '8th Gen'. Discount % must be a number from 0 to 100.",
              'Use the pencil to edit and the bin to delete. A Make or CPU that still has Models or CPU Gens '
              'under it cannot be deleted.']),
            ('Add or edit a product',
             ["Open 'Product Master Table' and click 'Add Product'.",
              'Choose Make, Model (only models of that Make are listed), CPU and CPU Gen. The Product Name '
              'preview shows Make/Model/CPU/CPU Gen.',
              "Click 'Save'. The same Product Name cannot be added twice.",
              'Use the pencil to edit or the bin to delete a product. Deleting also removes its prices.']),
            ('Read prices and set a discount',
             ['Columns show RAM, Storage, RAM Price, Storage Price, Product Price, the 8 part prices, Base '
              'Price, Discount %, Selling Price, Status and Date.',
              'RAM, Storage and Product Price come from the Product Pricing page. Part prices come from the '
              "Spare Parts page. Missing values show 'Pending'.",
              'Choose a value in the Discount % dropdown on a row. The discount amount and Selling Price '
              'update at once.',
              'Base Price = Product Price - RAM Price - Storage Price. Selling Price = Base Price - discount '
              'amount + RAM Price + Storage Price.',
              "Status shows 'Product Price Pending', 'Parts Price Pending', or 'Pricing Done' and 'Active' "
              'when everything is filled.']),
            ('Competitive Pricings',
             ["Open 'Competitive Pricings' and click 'Add Competitive List' to add a competitor, for example "
              'Amazon or Flipkart.',
              'Each list becomes a price column on the Product Pricing page.',
              "Under 'Apply Formula Method' choose Average, Maximum or Minimum. The Sell Price of every "
              'product is recalculated from its competitor prices.',
              'Deleting a list removes its column and its prices, and Sell Prices are recalculated.'])],
  'tips': ['This page is OFF by default for every role except Admin. An admin must enable it in Master Data '
           '-> Permission Matrix.',
           'Fill Dropdown Configuration before adding products, Hardware prices or Product Pricing.',
           'All price totals are calculated by the server - you cannot type a Base or Selling Price.',
           'Every add, edit and delete is recorded in the audit log.']},
 {'id': 29,
  'key': 'pm_hardware',
  'section': 'Product Master',
  'module': 'Hardware',
  'url': '/product-master/hardware',
  'icon': 'bi-memory',
  'description': 'Price list for RAM and Storage options. Each Type and Size pair from Dropdown '
                 'Configuration gets one price, which Product Pricing and Product Master then use.',
  'steps': [('Open the page',
             ['Go to PRODUCT MASTER -> Hardware.',
              "There are two tabs: 'RAM Details' and 'Storage Details'. The badge shows how many rows each "
              'has.',
              "Every Type x Size combination from Dropdown Configuration is listed, for example 'DDR4/8GB'.",
              'Columns: Name, Type, Size, Price, Action.']),
            ('Enter a price',
             ["On a row with no price, type the amount in the Rs box ('Enter price').",
              "Click 'Save'. The price is shown and the box closes."]),
            ('Change or remove a price',
             ["Click the pencil (Edit) to open the price box again, change the amount and click 'Save'.",
              "Click the bin ('Remove price') and confirm to remove a saved price.",
              'A price cannot be removed while products use that RAM or Storage. Change or delete those '
              'products first.'])],
  'tips': ['This page is OFF by default for every role except Admin. Enable it in Master Data -> Permission '
           'Matrix.',
           'If the tab is empty, add RAM / Storage Types and Sizes in Product Master -> Dropdown '
           'Configuration first.',
           'Prices must be a number, not negative.',
           'Product Pricing cannot use a RAM or Storage option until its price is saved here.']},
 {'id': 30,
  'key': 'pm_spare_parts',
  'section': 'Product Master',
  'module': 'Spare Parts',
  'url': '/product-master/spare-parts',
  'icon': 'bi-tools',
  'description': 'Spare part price list per product. Enter the price of 8 parts for each Make/Model/CPU/CPU '
                 'Gen product so Product Master shows the full pricing.',
  'steps': [('Open the page',
             ['Go to PRODUCT MASTER -> Spare Parts.',
              "Two tabs: 'Pending' (some part prices missing) and 'Completed' (all 8 filled). The badges "
              'show the counts.',
              'Columns: Product Name, Make, Model, CPU, CPU Gen, Bazel, Screen, Hinge, Hinge Cover, Touch '
              'Pad, Base, Keyboard, Battery, Status, Action.',
              'Use the table search box to find a product.']),
            ('Enter part prices',
             ['Type the price in each part box on the product row.',
              "The 'Save' button turns on once you enter or change a price.",
              "Click 'Save'. A message confirms the prices are saved."]),
            ('Check the status',
             ["When all 8 prices are filled, the Status shows 'Pricing Done' and the row moves to the "
              "'Completed' tab.",
              "If some prices are still empty, it shows 'Pricing Pending' and stays in 'Pending'.",
              'The part prices also appear in the Product Master table.'])],
  'tips': ['This page is OFF by default for every role except Admin. Enable it in Master Data -> Permission '
           'Matrix.',
           'You must enter at least one part price to save. Prices cannot be negative.',
           'Products are added on the Product Master page, not here.']},
 {'id': 31,
  'key': 'pm_product_pricing',
  'section': 'Product Master',
  'module': 'Product Pricing',
  'url': '/product-master/pricing',
  'icon': 'bi-tags',
  'description': 'Sets the RAM and Storage of each product and records competitor prices. The Sell Price is '
                 'calculated from those competitor prices.',
  'steps': [('Open the page',
             ['Go to PRODUCT MASTER -> Product Pricing.',
              "Two tabs: 'Pending' and 'Completed'. The badges show the counts.",
              'Columns: Product Name, Make, Model, CPU, CPU Gen, RAM Types, RAM Sizes, Storage Types, '
              'Storage Sizes, one column per competitor list, Sell Price, Status, Action.',
              'The note at the top says which Formula Method is used (Average, Maximum or Minimum).']),
            ('Set RAM and Storage',
             ['Choose RAM Types and RAM Sizes on the row. Then choose Storage Types and Storage Sizes.',
              'You must pick both Type and Size of a part, or leave both empty.']),
            ('Enter competitor prices',
             ['Type the price in Rs under each competitor column (for example Amazon, Flipkart).',
              'Sell Price fills in by itself while you type. You cannot type in Sell Price.',
              'Leave a box empty if you have no price from that competitor.']),
            ('Save',
             ["Click 'Save' (it turns on when something changed). The server recalculates the Sell Price.",
              "When RAM, Storage and Sell Price are all set, the Status shows 'Pricing Done' and the row "
              "moves to 'Completed'.",
              'The Sell Price becomes the Product Price on the Product Master page.'])],
  'tips': ['This page is OFF by default for every role except Admin. Enable it in Master Data -> Permission '
           'Matrix.',
           "A RAM or Storage choice with no price on the Hardware page gives the error '... has no price "
           "yet' - save its price on Hardware first.",
           'With no Competitive Lists, there is no Sell Price. Add lists in Product Master -> Competitive '
           'Pricings.',
           'Changing the Formula Method on Product Master recalculates every Sell Price.']},
 {'id': 32,
  'key': 'parts_dashboard',
  'section': 'Store Manager',
  'module': 'Parts Dashboard',
  'url': '/parts-dashboard',
  'icon': 'bi-speedometer2',
  'description': 'A read-only summary of spare parts stock and weekly part activity. Used by the Spare Parts '
                 'Manager, Sales and admins to see stock health at a glance.',
  'steps': [('Open the page',
             ['Go to Store Manager -> Parts Dashboard.',
              'The page has two parts: summary cards at the top and four weekly bar charts below.',
              'Nothing on this page can be edited. Use Parts Manager to make changes.']),
            ('Read the summary cards',
             ["'Total Quantities' - total pieces in stock across all active parts.",
              "'Part Types' - number of active parts in Part Master.",
              "'Below Min Stock' - parts that still have stock but are at or below their Low Stock Alert "
              'level.',
              "'Out of Stock' - parts with zero (or less) stock left.",
              "'Total New / Harvest' - how many parts were added as New and how many as Harvest.",
              "'Total Stock Value' - stock quantity x unit price, in Rs. Only shown if your role can view "
              'pricing.']),
            ('Read the weekly charts',
             ['Each chart shows the last 8 weeks. Each bar is one week, starting on Monday.',
              "'Weekly: Part Requests' - New / Replace / Downgrade requests raised by engineers.",
              "'Weekly: Faulty Requests' - faulty part requests raised from Device Detail.",
              "'Weekly: Sourcing Requests' - requests sent to sourcing for buying parts.",
              "'Weekly: Marked Sold' - spare part sales completed in that week.",
              'Hover on a bar to see the exact number.'])],
  'tips': ['Stock here = stock received minus parts handed over to engineers minus parts already sold.',
           'The Parts Manager tiles also subtract quantities on approved sale requests, so the two pages can '
           'show slightly different stock until the sale is completed.',
           "A high 'Below Min Stock' count means you should check Parts Manager and raise Procure for those "
           'parts.',
           "If you cannot see 'Total Stock Value', your role does not have the pricing permission."]},
 {'id': 33,
  'key': 'spare_parts',
  'section': 'Store Manager',
  'module': 'Parts Manager',
  'url': '/spare-parts',
  'icon': 'bi-gear',
  'description': 'The main working page for the Spare Parts Manager: the Part Master stock list, engineer '
                 'part requests, faulty part requests, parts consumed per Tag Number and sourcing requests.',
  'steps': [('Open the page and use the filter bar',
             ['Go to Store Manager -> Parts Manager.',
              "At the top, set 'Added From', 'Added To', 'Category', 'Part Name' and/or 'Engineer', then "
              "click 'Filter'. Click 'Clear' to remove all filters.",
              'Category, Part Name and Added dates filter Part Master, Part Requests and Faulty Request, and '
              'the summary tiles. Added dates also filter Parts Consumption.',
              "'Engineer' filters only the Part Requests and Faulty Request tabs.",
              "The tiles show 'Total Quantities', 'Total New / Harvest', 'Part Types', 'Below Min Stock', "
              "'Out of Stock', 'Requested / Sourced', 'Consumed' and 'Total Stock Value'.",
              "Inside each tab, tick the 'Added As' boxes (for example New or Harvest) to show only those "
              'rows.']),
            ('Part Master tab - add and maintain parts',
             ["Click 'Add Part' and choose 'Add New Part' (opens a new Parts GRN) or 'Add Harvest Part'.",
              "For 'Add Harvest Part', fill Lot Number, Part Category, Part Name, Part Brand, Part Model, "
              "Price, Physical Qty and Low Stock Alert, then click 'Save Harvest Part'. A Part ID is created "
              'automatically.',
              "To load many parts at once, click 'Upload New' or 'Upload Harvest', click 'Download Sample "
              "File', fill the CSV and click 'Upload'. A result page shows rows added and row errors.",
              "Click 'Edit' on a row to change name, category, make, model, crate number, unit price, In "
              "Stock quantity or Low Stock Alert, then 'Save Changes'.",
              "Click 'Procure' on a row to send that part to sourcing (quantity 1). It appears in the "
              'Sourcing Requests tab.',
              "Click the red bin icon to delete one part, or tick rows and click 'Delete Selected'. Deleted "
              'parts are hidden from the list.',
              'Rows in red are at or below their Min Alert level.']),
            ('Part Requests tab - hand over parts to engineers',
             ['This tab lists New, Replace and Downgrade part requests raised by engineers, with WorkID, Tag '
              "Number, Engineer, Part Name, Qty Requested, Qty Handover and 'QTY Available'.",
              "'QTY Available' adds New and Harvest stock of the same part name.",
              "Click 'Handover', check the part, engineer and quantity, then click 'Confirm Handover'. The "
              'quantity handed over is always the requested quantity.',
              "'Handover' is greyed out when there is no stock. Click 'Procure' to send the request to "
              "sourcing; status becomes 'Sent for Sourcing'.",
              "Tick several rows and click 'Handover Selected' or 'Delete Selected' to act in bulk. Rows "
              'already handed over are skipped and the count is shown.',
              'After handover, the engineer confirms receipt on Device Detail; the part then counts as '
              'Consumed.']),
            ('Faulty Request tab - handle faulty parts',
             ['This tab lists parts that engineers marked as faulty from Device Detail.',
              "Use 'Handover' to give a replacement part from stock, the same way as Part Requests.",
              "Click 'Procure' to send it for buying, 'Warranty Replace' to send the faulty part back to the "
              "supplier, or 'Scrap' to mark it scrapped.",
              "When the supplier sends the replacement, click 'Replacement Received'. The part is added back "
              'to stock.']),
            ('Parts Consumption and Sourcing Requests tabs',
             ["'Parts Consumption' shows, per Tag Number and part, how many parts were changed, unit price "
              'and total price. Only parts the engineer has verified as changed are shown.',
              "Use 'Tags Summary' or 'Parts Summary' to download this data.",
              "'Sourcing Requests' shows requests sent to sourcing, with Status, Qty Requested, Qty Sourced, "
              'Raised By, Source Deal ID and document download icons (Purchase Invoice, Purchase Order, '
              'E-way Bill).',
              "When the deal is closed by Sourcing, click 'Verify', check the documents and quantity, then "
              "click 'Confirm Verify'. The sourced quantity is added to stock. Click 'Send to Reupload' if "
              'documents are wrong.',
              "For whole-lot requests from Part Estimation (Status 'Production'), click 'Confirm Request' to "
              'acknowledge them.']),
            ('Export data',
             ["Every tab has 'Export All' to download all visible rows as a CSV file.",
              "Tick rows and click 'Export Selected' to download only those rows."])],
  'tips': ['In Stock shown in the table = stock received minus parts handed over minus quantities on '
           'approved sale requests.',
           "Changing 'In Stock (qty)' in Edit records a stock adjustment in the stock history. Use it only "
           'to correct real count differences.',
           'Uploading the same CSV again sets the stock to the file quantity for matching parts (same name, '
           'make and model); it does not add on top.',
           "'Verify' only works after the sourcing deal is closed. Before that the row shows 'Pending at "
           "Sourcing'.",
           "'Delete Selected' on requests does not remove handed-over requests; they are kept for the device "
           'history.',
           'Unit Price and Stock Value columns are shown only to roles with the pricing permission.']},
 {'id': 34,
  'key': 'parts_grn',
  'section': 'Store Manager',
  'module': 'Parts GRN',
  'url': '/parts-grn',
  'icon': 'bi-receipt',
  'description': 'Goods Receipt Notes for spare parts received from vendors. Saving a GRN adds each line '
                 'item to Part Master as a New part. Used by the Spare Parts Manager and admins.',
  'steps': [('Open the GRN list',
             ['Go to Store Manager -> Parts GRN.',
              'The table shows GRN Number, PO Number, Vendor, Invoice No, Date Received, Total Phys Qty, '
              'Invoice Value and Created By.',
              'Use the search box and column sorting to find a GRN.',
              "Click 'View' to open a saved GRN. Saved GRNs are read-only."]),
            ('Create a new GRN - header',
             ["Click 'New GRN' (or 'Add Part' -> 'Add New Part' on Parts Manager).",
              "In 'Section 1 - GRN Header', enter PO Number and PO Date, Invoice Number and Invoice Date, "
              'E-way Bill Number and Date, Vehicle Number, Date of Material Received and Vendor Name.',
              'Upload the PO, Invoice, E-way Bill, Vehicle Seal and Vehicle Image files if you have them.',
              'Enter Invoice Value, SGST, CGST, IGST, Tax Amount, Total PO Qty, Total Invoice Qty, Total '
              'Physical Qty and Total Amount Invoice in Rs.',
              'The GRN Number is created automatically.']),
            ('Add line items',
             ["In 'Section 2 - Line Items', click 'Add'.",
              'PO Number, Invoice Number, Vendor Name and Vehicle Number are copied from the header. A Part '
              'ID is created automatically.',
              'Fill Lot Number, Product Description, Part Category, Part Name, Part Brand, Part Model, Low '
              'Stock Alert, Invoice Qty, Physical Qty, Price and Remarks.',
              "Click 'Add to List'. The item appears in the Line Items table.",
              'Click the bin icon on a line to remove it before saving.']),
            ('Save the GRN',
             ["Click 'Save GRN'. The saved GRN opens in read-only view.",
              'Each line item is added to Part Master as a New part with the Physical Qty (or Invoice Qty if '
              'Physical Qty is empty) as stock.',
              'On Parts Manager, the GRN column of the part links back to this GRN.']),
            ('Delete a GRN',
             ['Click the red bin icon on the GRN list and confirm.',
              'The GRN is hidden from the list. Parts already added to Part Master are not removed.'])],
  'tips': ["Line items are kept only on the screen until you click 'Save GRN'. If you leave the page first, "
           'they are lost.',
           'Always enter Physical Qty - it is the quantity added to stock.',
           'Fill Part Brand and Part Model so stock and requests match the right part.',
           'Only admins and Spare Parts Managers can open this page by default.']},
 {'id': 35,
  'key': 'parts_sale_request',
  'section': 'Store Manager',
  'module': 'Parts Sale Request',
  'url': '/parts-sale-request',
  'icon': 'bi-inbox',
  'description': 'Approve or reject requests to sell spare parts. Sales staff raise the requests from Ready '
                 'to Sale Parts; the Spare Parts Manager or an admin approves them here.',
  'steps': [('Open the page',
             ['Go to Store Manager -> Parts Sale Request.',
              'The table shows Code, Part Name, Make, Model, Requested Quantity, In Stock, Requested By, '
              'Requested On, Status and Action.',
              "Status is 'Pending', 'Approved' or 'Rejected'. Approved requests that have been sold also "
              "show 'Sold'.",
              'Use the search box to find a part or a requester.']),
            ('Approve a request',
             ["Check 'In Stock' against 'Requested Quantity'.",
              "Click 'Approve'. The status changes to 'Approved'.",
              'The approved quantity is reserved at once - it is shown as Sold on Parts Manager and removed '
              'from available stock.',
              'Sales can now complete the sale for this part.']),
            ('Reject a request',
             ["Click 'Reject'.",
              "Optionally type a reason in the window, then click 'Reject Request'.",
              "The status changes to 'Rejected'. Hover on the 'Rejected' badge to see the reason."])],
  'tips': ['Only one pending request is allowed per part. A new request for the same part is blocked until '
           'the first one is approved or rejected.',
           'Approve only when there is enough stock - the page does not stop you from approving more than '
           "'In Stock'.",
           'Approve and Reject buttons appear only on Pending requests. Decisions cannot be changed from '
           'this page.']},
 {'id': 36,
  'key': 'locations',
  'section': 'Store Manager',
  'module': 'Location Map',
  'url': '/locations/dashboard',
  'icon': 'bi-boxes',
  'description': 'A live map of which devices are in which storage location (rack, crate, shelf and so on). '
                 'Any staff member can use it to find a device or see what is in a location.',
  'steps': [('Open the page',
             ['Go to Store Manager -> Inventory Locations -> Location Map.',
              'Each card is one active location. The card shows the Location ID, zone, device count (and '
              'capacity if set) and the list of Tag Numbers with brand, model and stage.',
              "Empty locations show 'Empty'.",
              "A yellow banner shows devices that are 'in-hand' (picked up but not placed back)."]),
            ('Filter and search',
             ["Choose a 'Location Zone' and/or 'Location Type' to show only those locations. Click 'Clear' "
              'to reset.',
              "Type in 'Search Location ID...' or 'Search Bin Location...' to filter the cards instantly."]),
            ('Find one device',
             ["In 'Quick Device Lookup', scan or type the Tag Number and press Enter or click 'Find'.",
              'You see the device, its stage, its current location and who moved it last.',
              "Click 'Manage Location' to open the device location page."]),
            ('Move a device (Manage Location page)',
             ["Click 'Pick Up Device' when you take a device out of its location. It shows as in-hand.",
              "When you put it back, choose the destination and click 'Place Back'.",
              "To put a device directly in a location, choose it and click 'Assign Location' or 'Move to New "
              "Location'.",
              'The page also shows the device location history: date, action, location, user and notes.']),
            ('Other links',
             ["Click 'Gaps' to open Gap Alerts. The red badge shows how many devices are unaccounted for.",
              "Click 'Manage Locations' to add or edit locations."])],
  'tips': ['Sold, Scrapped and Scrap for Sale devices are not shown on the cards.',
           "Always 'Place Back' a device you picked up, or it will appear on Gap Alerts.",
           'Inactive locations are not shown. Activate them on Manage Locations.',
           'Click a Tag Number on a card to open the Device Detail page.']},
 {'id': 37,
  'key': 'physical_audits',
  'section': 'Store Manager',
  'module': 'Physical Audits',
  'url': '/audits',
  'icon': 'bi-clipboard2-check',
  'description': 'Tag-by-tag physical stock audit. The auditor scans each device found in a stage and '
                 'location; the system compares the physical count with the system count and records '
                 'differences.',
  'steps': [('Open the page',
             ['Go to Store Manager -> Inventory Locations -> Physical Audits.',
              "The top card is 'Perform Audit'. The bottom card is 'Audit Records'."]),
            ('Set up the audit',
             ["Check 'Report Date' (today by default).",
              "Choose the 'Audit Stage' where you are counting, for example Ready to Sale.",
              "Choose the 'Audit Location' where you are standing, or leave '- No Location -'."]),
            ('Scan tags',
             ["Scan or type a Tag Number and press Enter or click 'Validate'.",
              "The tag is recorded at once. A message says 'Tag audited. Ready for the next one.'",
              'A receipt panel shows your Audit Stage and Audit Location next to the System Stage and System '
              'Location, plus Assigned To and Category.',
              'Stage and location stay selected, so you can scan the next tag in the same place.',
              'If the tag does not exist, or was already audited for this Report Date, a red message is '
              'shown and nothing is saved.']),
            ('Review Audit Records',
             ['Each row groups tags by Report Date, Audit Stage, Audit Location and Category.',
              "'Current Count' is how many devices the system expects there. 'Physical Count' is how many "
              "you scanned. 'Difference' = Current Count minus Physical Count.",
              'Pick a date in the date box to show only that date.',
              "Type in the 'Notes' box of a row; the note saves when you leave the box.",
              'Click the download icon to get the tag list of that row, with a Yes/No Difference column for '
              'stage or location mismatch.',
              'Click the red bin icon to delete a record and all its scanned tags.']),
            ('Export summaries',
             ["'Stage Summary' - totals per Audit Stage (all dates).",
              "'Location Summary' - totals per Audit Location (all dates).",
              "'Tags Summary' - every scanned tag; uses the selected date if one is chosen."])],
  'tips': ['Choose the stage and location before scanning - every scan is saved immediately with the current '
           'selection.',
           'The same tag can be audited only once per Report Date.',
           'A positive Difference means devices are missing from the physical count; a negative one means '
           'extra devices were found.',
           'Deleting a record cannot be undone. This page is separate from Zone Audit.']},
 {'id': 38,
  'key': 'location_gaps',
  'section': 'Store Manager',
  'module': 'Gap Alerts',
  'url': '/locations/gaps',
  'icon': 'bi-exclamation-triangle',
  'description': 'End-of-day list of active devices whose location is unknown: devices picked up and not '
                 'placed back, and devices that never had a location. Used by store and inventory staff to '
                 'close location gaps.',
  'steps': [('Open the page',
             ["Go to Store Manager -> Inventory Locations -> Gap Alerts (or click 'Gaps' on Location Map).",
              "Three cards show 'Total Gaps', 'In Hand (Not Returned)' and 'Never Located'.",
              "If there are no gaps you see 'All Clear!'."]),
            ('Fix In Hand devices',
             ["The 'In Hand - Not Returned' table shows Tag Number, Device, Stage, Picked Up By, When and "
              'Hours Ago.',
              'Hours Ago is blue up to 4 hours, yellow after 4 hours and red after 8 hours.',
              "Find the device, then click 'Place Back', choose the location and confirm."]),
            ('Fix Never Located devices',
             ["The 'Never Located' table shows active devices with no location record: Tag Number, Device, "
              'Stage, Grade and In System Since.',
              "Click 'Assign Location', choose where the device is kept and click 'Assign Location'.",
              'Refresh Gap Alerts - fixed devices disappear from the list.'])],
  'tips': ['Only devices in active stages (IQC to Ready to Sale) are checked. Sold and scrapped devices are '
           'ignored.',
           'Check this page at the end of every day so no device is left without a location.',
           'The Never Located table shows at most 200 devices at a time; keep assigning and refresh to see '
           'more.']},
 {'id': 39,
  'key': 'location_audit',
  'section': 'Store Manager',
  'module': 'Zone Audit',
  'url': '/locations/audit',
  'icon': 'bi-clipboard-check',
  'description': 'Scan-based audit of a whole zone (or all zones): scan every device you find and the system '
                 'shows Found, Missing and Extra devices with an accuracy score. Started by admins and '
                 'inventory managers.',
  'steps': [('Open the page',
             ['Go to Store Manager -> Inventory Locations -> Zone Audit.',
              'The table lists past audits: Audit #, Zone Scope, Status, Initiated By, Date, Expected, '
              'Found, Missing, Extra and Accuracy.']),
            ('Start a new audit',
             ["Click 'Initiate New Audit'.",
              "Choose a 'Zone Scope', or 'All Zones (Full Audit)'.",
              "Add Notes if needed, for example the month, then click 'Start Audit'.",
              'The system counts how many devices are expected in that scope and opens the audit page.']),
            ('Scan devices',
             ["In 'Scan a Tag Number', scan or type the Tag Number.",
              "Choose the 'Location' where you found it and click the tick button.",
              'If the device is recorded at that location it counts as Found. Otherwise (different location '
              'or unknown tag) it counts as Extra / Unknown.',
              "'Scanned Items' lists every scan. 'Missing Devices' lists expected devices not yet scanned.",
              "To continue later, open the audit again with 'Continue' on the list."]),
            ('Close the audit',
             ["When all locations are scanned, click 'Close Audit' and confirm.",
              'The audit is marked completed and the final Found, Missing and Extra counts are saved.',
              "'Audit Accuracy' = Found / Expected. Green is 95% or more, yellow 80% or more, red below "
              '80%.'])],
  'tips': ['Closing an audit is final. No more scans can be added after that.',
           'Scanning does not move the device. Fix wrong locations from Location Map -> Manage Location.',
           'Only admins and inventory managers can start or close an audit by default.',
           'This is separate from Physical Audits, which counts by stage and location per day.']},
 {'id': 40,
  'key': 'location_master',
  'section': 'Store Manager',
  'module': 'Manage Locations',
  'url': '/locations/master',
  'icon': 'bi-geo-alt',
  'description': 'The master list of storage locations (racks, crates, shelves, trolleys, cabinets, floor '
                 'space) in each zone. Admins and inventory managers create, edit, activate or delete '
                 'locations here.',
  'steps': [('Open the page',
             ['Go to Store Manager -> Inventory Locations -> Manage Locations.',
              'Cards at the top show each zone with its number of locations and devices.',
              'The table shows Location ID, Zone, Type, Bin Location, Capacity, Current Devices, Description '
              'and Status.']),
            ('Add one location',
             ["Click 'Add Location'.",
              "Choose 'Zone' and 'Location Type', and type the 'Location ID' (for example RACK-A1). These "
              'three are required.',
              "Optionally fill 'Bin Location' and 'Max Capacity'.",
              "Click 'Create Location'. The Location ID is saved in capital letters."]),
            ('Bulk upload locations',
             ["In 'Bulk Upload Locations', click 'Download Template'.",
              'Fill one row per location: zone, unit type, location ID, and optional slot, description and '
              'capacity.',
              "Choose the CSV file and click 'Upload'. A message shows how many were added and skipped, and "
              'any row errors.']),
            ('Edit, deactivate or delete',
             ["Click the pencil icon, change the details and click 'Save Changes'.",
              "Click 'Deactivate' to hide a location from Location Map and from location dropdowns. Click "
              "'Activate' to bring it back.",
              'Click the red bin icon to delete permanently. This works only for a location that has never '
              'been used.'])],
  'tips': ['In the CSV, zone must be one of: showroom, ground_floor, first_floor, second_floor, workshop '
           '(TRC Floor), dispatch, warehouse, holding (TRC Store).',
           'In the CSV, unit type must be one of: rack, crate, shelf, trolley, cabinet, floor.',
           'Rows whose Location ID already exists are skipped, not updated.',
           "A location with any devices, history or audit records cannot be deleted - use 'Deactivate' "
           'instead.',
           'Only admins and inventory managers can open this page by default.']},
 {'id': 41,
  'key': 'location_trash',
  'section': 'Store Manager',
  'module': 'Trash',
  'url': '/trash',
  'icon': 'bi-trash3',
  'description': 'Holds lots and devices that were moved to trash. From here you can restore them or delete '
                 'them permanently. Only admins and inventory managers can use it.',
  'steps': [('Open the page',
             ['Go to Store Manager -> Inventory Locations -> Trash.',
              "There are two tabs: 'Trashed Lots' and 'Trashed Devices'. The red badge shows how many items "
              'each has.',
              'Trashed Lots shows Lot #, Supplier, Purchase Date, Qty, Buying Price (if you can view '
              'pricing) and Trashed On.',
              'Trashed Devices shows Tag Number, Lot, Brand / Model, Grade, Stage and Trashed On.']),
            ('Restore an item',
             ["Click 'Restore' on the lot or device row.",
              'The item comes back to the main lists (Lot Management or Inventory) right away.']),
            ('Delete permanently',
             ["Click 'Delete' on the row.",
              'Read the warning. Type the Lot number or Tag Number exactly as shown.',
              "Click 'Delete permanently'. The button works only after the text matches.",
              'Deleting a lot also deletes all its devices, with their inspection, stage history, parts '
              'consumption and scan records.'])],
  'tips': ['Items in Trash are hidden from all main lists until restored.',
           'Permanent delete has no undo. Restore is always the safer choice if you are not sure.',
           'Records that only mention the item, such as supplier payments or sourcing deals, are kept but '
           'unlinked.',
           'If a delete is blocked, the message names the related record that still uses the item.']},
 {'id': 42,
  'key': 'spare_parts_purchase',
  'section': 'Admin',
  'module': 'Parts Purchased',
  'url': '/spare-parts/purchase',
  'icon': 'bi-bag-plus',
  'description': 'Record a direct purchase of an existing spare part and see the full purchase history. Each '
                 'purchase adds stock to the part.',
  'steps': [('Open the page',
             ['Go to Admin -> Application Settings -> Parts Purchased.',
              "The top card is 'Record Purchase'. The bottom card is 'Purchase History'."]),
            ('Record a purchase',
             ["Choose the 'Part' (Part Code - Name).",
              "Enter 'Qty', 'Unit Price' in Rs and the purchase 'Date'. These are required.",
              "Optionally enter 'Supplier' and 'Invoice No.'.",
              "Click 'Record Purchase'."]),
            ('What happens after saving',
             ["The quantity is added to the part's In Stock on Parts Manager.",
              "The part's Unit Price is changed to the price you entered.",
              'A new row appears in Purchase History with Date, Code, Part, Qty, Unit Price, Total, '
              'Supplier, Invoice and By.']),
            ('Search history',
             ['Use the search box and sorting in Purchase History to find past purchases. Newest is shown '
              'first.',
              'Click a column header (for example Date or Part) to sort by it.'])],
  'tips': ['The part must already exist in Part Master. For new parts or vendor deliveries with documents, '
           'use Parts GRN instead.',
           "Check the unit price carefully - it replaces the part's current price and changes stock value.",
           'A purchase cannot be edited or deleted from this page.']},
 {'id': 43,
  'key': 'parts_tracking',
  'section': 'Admin',
  'module': 'Parts Tracking',
  'url': '/ram-tracking',
  'icon': 'bi-memory',
  'description': 'Log RAM removed from, added to, or moved between devices, and see the RAM movement '
                 'history. The device RAM size is updated automatically.',
  'steps': [('Open the page',
             ['Go to Admin -> Application Settings -> Parts Tracking.',
              "The page title is 'RAM Tracking'. The top card is 'Log RAM Movement'; the bottom card is 'RAM "
              "Movement Log'."]),
            ('Log a RAM movement',
             ["Choose the 'Action': 'Removed from device', 'Added to device' or 'Cannibalized "
              "(device->device)'.",
              "For Removed, scan the 'Source Device Tag Number'.",
              "For Added, scan the 'Destination Tag Number'.",
              'For Cannibalized, scan both the source and the destination Tag Number.',
              "Choose 'RAM (GB)', optionally 'RAM Type' and 'Notes', then click 'Log RAM'."]),
            ('What happens after saving',
             ['Removed: the source device RAM is reduced by the GB entered.',
              'Added or Cannibalized: the destination device RAM is increased by the GB entered.',
              'The entry appears in the RAM Movement Log with Date, Action, Source Device, RAM, Type, By and '
              'Notes.'])],
  'tips': ["For Cannibalized, the source device RAM is not reduced automatically. Log a separate 'Removed' "
           'entry for the source if needed.',
           'Check Tag Numbers before saving - an unknown Tag Number is saved in the log but no device RAM is '
           'changed.',
           'Entries cannot be edited or deleted from this page.']},
 {'id': 44,
  'key': 'parts_consumption',
  'section': 'Admin',
  'module': 'Parts Consumption',
  'url': '/spare-parts/consume',
  'icon': 'bi-dash-circle',
  'description': 'Manually record spare parts used on a device, lot or stage, and see the consumption log. '
                 'Each entry takes the part out of stock and adds its cost to the device.',
  'steps': [('Open the page',
             ['Go to Admin -> Application Settings -> Parts Consumption.',
              "The top card is 'Record Parts Used'. The bottom card is 'Consumption Log'."]),
            ('Record parts used',
             ["Choose the 'Part'. Only parts with stock are listed, with the quantity left.",
              "Enter 'Qty Used'.",
              "Optionally scan the 'Device Tag Number', choose the 'Lot' and the 'Stage', and add 'Notes'.",
              "Click 'Record Consumption'."]),
            ('What happens after saving',
             ["The quantity is taken out of the part's stock.",
              "If a Tag Number is given, the cost is added to that device's parts cost and linked to its "
              'open repair job.',
              'The entry appears in the Consumption Log with Date, Part, Qty, Unit Cost, Total Cost, Device, '
              'Stage and By.'])],
  'tips': ["You cannot use more than the stock available. If you try, an 'Insufficient stock' error is shown "
           'and nothing is saved.',
           "Unit cost is the part's current Unit Price in Part Master.",
           'Normal engineer part requests are handled on Parts Manager; use this page only for manual '
           'consumption entries.',
           'Entries cannot be edited or deleted from this page.']},
 {'id': 45,
  'key': 'crm_dashboard',
  'section': 'CRM',
  'module': 'Procure Dashboard',
  'url': '/procure-dashboard',
  'icon': 'bi-speedometer2',
  'description': 'One-screen summary of the CRM pipelines, due follow-ups and the pending device and part '
                 'sourcing requests. Used by procurement, the Sales Manager and admins to see what needs '
                 'buying or closing today.',
  'steps': [('Open the page and read the cards',
             ['Go to CRM -> Procure Dashboard.',
              "The top row shows four cards: 'Sourcing Deals Open' (with the open pipeline value in Rs), "
              "'Sales Opportunities Open' (with pipeline value), 'Follow-ups Overdue' (with how many are due "
              "today) and 'Total Contacts' (suppliers and buyers).",
              "Click 'View All' on any card to open the full list for that card.",
              'Closed deals (Won or Lost) are not counted in the open numbers or pipeline value.']),
            ('Check the funnels',
             ["'Sourcing Funnel' shows how many open sourcing deals are at each stage: Lead, Contacted, "
              'Inspection, Negotiation, Agreed, PO Raised.',
              "'Sales Funnel' shows open sales opportunities at Lead, Contacted, Quoted, Negotiation, "
              'Confirmed, Invoiced.',
              "Click 'New Deal' or 'New Opp' on a funnel card to create a new sourcing deal or sales "
              'opportunity.']),
            ('Work on follow-ups and quick actions',
             ["The 'Follow-ups Due' card lists up to 8 follow-ups that are overdue (red 'OVERDUE' badge) or "
              "due later today (yellow 'TODAY' badge).",
              'Click the deal title to open the linked sourcing deal or sales opportunity.',
              "After you finish the follow-up, click 'Mark Done'. It disappears from the list.",
              "Click 'See All' to open the full follow-up list.",
              "Use the quick buttons to jump straight to 'Add Contact', 'New Sourcing Deal', 'New Sales "
              "Opportunity', 'New Quote' or 'All Follow-ups'."]),
            ('Close a device sourcing request',
             ["Scroll to 'Pending Requests' and open the 'Device Sourcing' tab. It lists model requests "
              'raised by telecallers and other staff that are not yet closed or cancelled.',
              'Each row shows Requested date, By, Config (category, brand, model, CPU, RAM, storage, '
              'screen), Grade, Qty (fulfilled / requested), Status and Notes.',
              "Click 'Close Deal' on an Open or Partially Fulfilled row.",
              "In the 'Close Sourcing Deal' window pick the 'Sourcing Deal' that brought in the stock, enter "
              "the 'Sourced Quantity' and optional 'Sourcing Notes'.",
              "Click 'Close Deal'. The request status becomes Closed and it leaves this tab."]),
            ('Close a parts sourcing request',
             ["Open the 'Parts Sourcing' tab. It shows Status, Part Code (or the lot number for requests "
              'raised from Part Estimation), Part Name (or the estimate file to download), Qty Requested, '
              'Qty Sourced, Raised By and Action.',
              'Requests raised from Part Estimation appear here only after the Spare Parts Manager has '
              'confirmed them.',
              "Click 'Close Deal', pick the 'Sourcing Deal', enter the 'Sourced Quantity' and click 'Close "
              "Deal'.",
              "The row shows a green 'Closed' badge and the linked deal number appears next to Qty Sourced. "
              'If the Spare Parts Manager had already verified the request, the sourced quantity is added to '
              'part stock.'])],
  'tips': ["Only admin and Sales Manager see the 'Close Deal' buttons. Other users see the lists read-only.",
           'Create the sourcing deal first (CRM -> Sourcing Deals). Only open deals (not Won or Lost) appear '
           "in the 'Sourcing Deal' dropdown.",
           'Closing a device sourcing request does not add devices to stock. Devices come in through a lot '
           'and GRN as usual.',
           'A follow-up counts as overdue as soon as its date and time has passed.']},
 {'id': 46,
  'key': 'crm_contacts',
  'section': 'CRM',
  'module': 'Contact Leads',
  'url': '/crm/contacts',
  'icon': 'bi-person-lines-fill',
  'description': 'The master list of all CRM accounts - suppliers we buy from and buyers we sell to - with '
                 'their locations, contact numbers, KYC and account team. Used by sales, telecallers, the '
                 'Sales Manager and inventory managers.',
  'steps': [('Open the page and read the list',
             ["Go to CRM -> Contact Leads. The page heading is 'CRM Contacts'.",
              'Summary cards show Total, Suppliers, Buyers and Active accounts for the current filter.',
              "The 'List of Accounts' table shows Company (with GSTIN and a 'Contacted' or 'Not Yet' badge), "
              'Type, Category, Locations, Contacts, KYC, Team, Created By, Status and Actions.',
              "'Contacted' means at least one activity has been logged for the account. 'Status' shows the "
              'outcome of the latest activity (for example Interested, Callback, No Answer).',
              'Click the number in Locations, Contacts or Team to see the full addresses, people and phone '
              'numbers, or the mapped team.']),
            ('Search and filter',
             ["Type in 'Search' to find by company name, contact person, phone, city, state or GSTIN.",
              "Use 'Account Type' (Supplier, Buyer, Both), 'Source Type', 'Buyer Type' and 'Contacted' "
              '(Contacted / Not Yet).',
              "Use the team filters 'VP/AVP', 'Manager', 'AM/DM/GM' and 'Executive/Sr Exec' to see accounts "
              'mapped to selected people.',
              "Admins also get an 'Agent' filter to see accounts added by one user.",
              "Click 'Filter' to apply and 'Clear' to reset."]),
            ('Add or edit an account',
             ["Click 'Add Account'.",
              "Fill 'Company Details': 'Contact Type' (Supplier, Buyer or Both) and 'Company / Business "
              "Name' are required. Add Supplier Type or Buyer Type, email, address, city, state and pincode.",
              "In 'KYC Documents' enter GSTIN, PAN, Bank Cheque, MSME/UDHYOG Certificate and Director 1 / "
              'Director 2 Aadhar/PAN numbers, and upload the documents.',
              "In 'Locations' click 'Add Location' for each site or branch, and add the contact people "
              '(role, name, phone, email) under each location.',
              'Set Credit Limit, Assigned To, Status (Active, Inactive, Blacklisted), Tags and Notes, then '
              "click 'Create Account'.",
              "To change an account, click the pencil (Edit) icon in the Actions column and click 'Update "
              "Account'."]),
            ('Work with an account profile',
             ['Click the company name or the eye (Profile) icon to open the account.',
              "Use 'Log Activity' to record a call, WhatsApp, visit, email, meeting or note. Pick the type "
              "and outcome, write 'What was discussed?', and optionally set a next follow-up date and the "
              'person it is assigned to.',
              "In the 'KYC' card click 'Verify KYC' and confirm once documents are checked. The KYC column "
              "then shows 'Verified'.",
              "The profile also lists the account's Sourcing Deals, Sales Opportunities, Lost Opportunities "
              'and the Activity Timeline.',
              "From the list Actions you can also start a 'New Sourcing Deal' or 'New Sales Opp' for that "
              'account.']),
            ('Map the account team',
             ['Click the person-badge (Map Team) icon on a row to set who handles the account in each level: '
              'VP/AVP, Manager, AM/DM/GM, Executive/Sr Executive. Leaving a level blank clears it for that '
              'account.',
              "For many accounts at once, tick the row checkboxes and click 'Bulk Team Mapping'. Only the "
              'levels where you pick at least one person are changed.',
              "To load team mapping from a file, click 'Team Mapping CSV'."]),
            ('Bulk upload, export and trash',
             ["Click 'Bulk Upload CSV' to create accounts from a CSV file. The file must have a company_name "
              'column. Extra rows with the same company add more locations and contact people.',
              "Admins can click 'Export CSV' to download the accounts that match the current filter.",
              "Click the bin icon to move an account to Trash. Open the 'Trash' section at the bottom and "
              "click 'Restore' to bring it back."])],
  'tips': ['Accounts with the same GSTIN are matched during bulk upload, so a GST number never creates a '
           'second account.',
           'Map Team, Bulk Team Mapping and the row checkboxes appear only if your role has edit permission '
           'for Contact Leads.',
           "'Export CSV' and the 'Agent' filter are admin only.",
           'Adding an account needs add permission for Contact Leads.',
           'Trash is a soft delete - nothing is lost and the account can be restored at any time.']},
 {'id': 47,
  'key': 'crm_sourcing',
  'section': 'CRM',
  'module': 'Sourcing Deals',
  'url': '/crm/sourcing',
  'icon': 'bi-box-arrow-in-down',
  'description': 'Tracks every deal to buy stock from a supplier, from first lead to stock received, '
                 'including PO line items, purchase orders and documents. Used by the procurement and sales '
                 'team.',
  'steps': [('Open the list',
             ['Go to CRM -> Sourcing Deals.',
              'Cards show Total Deals, Open Deals, Pipeline Value (Rs) and Overdue Follow-ups.',
              'The table shows Deal #, Title (with supplier), Source Type, Stage, Qty, Value, Priority, '
              "Assigned To, Download and Actions. Qty and Value come from the deal's PO line items.",
              "Use the 'Download' menu to open the Generated PO, Purchase Invoice, Purchase Order, E-way "
              'Bill or Quote Received for a deal.']),
            ('Search and filter',
             ["Type in 'Search deals' to find by deal title.",
              "Filter by stage, source type and priority, then click 'Filter'. Click 'Clear' to reset.",
              'Users with the sales role see only the deals assigned to them.']),
            ('Create a deal',
             ["Click 'New Deal'.",
              "Enter 'Title' (required) and pick the 'Contact (Supplier)'.",
              'Fill Source Type, Device Type, Est. Quantity, Condition Type, asking price, our offer, '
              'Payment Advance %, Payment Terms, Priority, Assigned To and Notes.',
              "Optionally attach 'Product Records' (.xls, .xlsx or .csv).",
              "Save. The deal gets a new deal number and starts at stage 'Lead Identified'. If you enter "
              'asking price per unit and quantity, the total is calculated for you.']),
            ('Move the deal through stages',
             ['Open the deal (eye icon or title).',
              "In 'Stage Progress' choose 'Move to Stage', optionally enter 'Final Price / Unit' and "
              "'Win/Loss Reason', and click 'Update Stage'.",
              'Stages are: Lead Identified, Initial Contact Made, Inspection Arranged, Quote Received, '
              'Negotiation, Deal Agreed, PO Raised, Stock Received, Closed - Won, Closed - Lost.',
              "Each stage change is saved automatically in the 'Activity Log'.",
              "Use 'Log Activity' to record calls and set follow-ups; click 'Mark Done' when a follow-up is "
              'finished.']),
            ('Add PO line items and generate a PO',
             ["In 'PO Line Items' click 'Add PO Items'. Enter 'Model Name' (required), Description, PO "
              "Category, Qty and Unit Price, then click 'Add Item'.",
              "Click 'Offer Price' on a line (or 'Edit' if one is already set) to enter 'Our Offer Price "
              "(Rs)' and click 'Save Offer'. The card shows Gross Price and Offer Price totals.",
              "Click 'Generate PO', choose which sections to include (Account details, Company details, PO "
              'line items), pick Company Details and the Payment Terms, Delivery Terms and Conditions, then '
              "click 'Generate & Download'.",
              'A new PO number is created as a Draft purchase order and the PDF downloads. It also appears '
              'in CRM -> Purchase Orders.']),
            ('Upload documents and close the deal',
             ["In 'Upload Documents' attach the Purchase Invoice, Purchase Order, E-way Bill or Quote "
              "Received and click 'Upload'.",
              "Click 'Create Lot & Mark WON' to open the new lot form filled with this deal's supplier, "
              'quantity, price and deal number. When the lot is saved, the deal is linked to it and marked '
              'Closed - Won.',
              "Use 'Mark WON (no lot)' or 'Mark LOST' if no lot is needed.",
              "The 'Deal Profitability Calculator' lets you test margin with acquisition, pickup, transport, "
              'processing and expected sell prices. It does not save anything.'])],
  'tips': ['Creating a deal needs add permission for Sourcing Deals.',
           "Use 'Create Lot & Mark WON' instead of creating the lot separately, so the lot stays linked to "
           'the deal.',
           "Re-downloading a PO from the 'Download' menu rebuilds it from the PO's own saved lines, not the "
           'current deal lines.',
           "Issuing a draft PO in Purchase Orders moves an early-stage deal to 'PO Raised' automatically."]},
 {'id': 48,
  'key': 'crm_sales_opp',
  'section': 'CRM',
  'module': 'Sales Opportunities',
  'url': '/crm/sales',
  'icon': 'bi-graph-up-arrow',
  'description': 'Tracks every chance to sell stock to a buyer, from enquiry to payment, with quotes and '
                 'linked sale records. Used by sales, telecallers and the Sales Manager.',
  'steps': [('Open the list',
             ['Go to CRM -> Sales Opportunities.',
              'Cards show Open Opportunities, Pipeline Value (Rs) and Overdue Follow-ups.',
              'The table shows Opp #, Title (with buyer), Lot Number, Buyer Type, Stage, Qty Req., Est. '
              'Value, Grade, Priority, Assigned To, Download and Actions.',
              'Lot Number is filled when the opportunity came from a won partner bid on a lot.',
              "Use 'Download' to get the product records file attached to the opportunity."]),
            ('Search and filter',
             ["Type in 'Search opportunities' to find by title.",
              "Filter by stage, buyer type and priority, then click 'Filter'. Click 'Clear' to reset.",
              'Users with the sales or telecaller role see only the opportunities assigned to them.']),
            ('Create an opportunity',
             ["Click 'New Opportunity'.",
              "Enter 'Title' (required) and pick the 'Contact (Buyer)'.",
              'Fill Buyer Type, Device Type, Required Qty, Material Type, Grade Required, Budget Per Unit, '
              'Estimated Value, Expected Close Date, Priority, Assigned To and Notes. Optionally attach '
              "'Product Records'.",
              "Save. The opportunity gets an OPP number and starts at 'Lead / Enquiry'. If Estimated Value "
              'is empty, it is Required Qty x Budget Per Unit.']),
            ('Move stages and log activity',
             ["Open the opportunity and use 'Stage Progress' -> 'Move to Stage', with an optional 'Win/Loss "
              "Reason', then click 'Update'.",
              'Stages are: Lead / Enquiry, Contacted, Requirement Understood, Availability Confirmed, Quote '
              'Sent, Negotiation, Order Confirmed, Invoice Raised, Delivered, Payment Received, Closed - '
              'Won, Closed - Lost.',
              "Use 'Mark WON' or 'Mark LOST' for a quick close.",
              "Use 'Log Activity' to record calls and follow-ups. Stage changes are logged automatically."]),
            ('Create a quote and link sales',
             ["Click 'Create Quote' to open a new quote for this buyer. If the opportunity came from a won "
              'lot, the lines are filled from that lot. Saving the quote moves an early-stage opportunity to '
              "'Quote Sent'.",
              "If a quote already exists, the button shows 'View Quote' and the 'Linked Quote' card shows "
              'its number.',
              "Click 'Link Sale' to attach real sale records. Search by Tag Number, brand, model or sale "
              'number, tick the sales and save.',
              "Linking sales moves an opportunity that is still at Order Confirmed or earlier to 'Invoice "
              "Raised'."])],
  'tips': ['Creating an opportunity needs add permission for Sales Opportunities.',
           'Linked sales are added to the ones already linked - linking again does not remove earlier sales.',
           'Only open opportunities (not Won or Lost) count in the pipeline value.']},
 {'id': 49,
  'key': 'crm_price_matrix',
  'section': 'CRM',
  'module': 'Price Matrix',
  'url': '/crm/price-matrix',
  'icon': 'bi-table',
  'description': 'Reference table of buy and sell price ranges by device type, grade, material and brand, '
                 'used to check offers and quotes. Everyone can view it; admin, Sales Manager and Inventory '
                 'Manager can change it.',
  'steps': [('Read the matrix',
             ['Go to CRM -> Price Matrix.',
              'Columns are Device Type, Grade, Material, Brand, Min Buy Rs, Max Buy Rs (red), Target Sell Rs '
              '(green), Min Margin %, Notes and Updated (date and user).',
              'The Min Margin % badge is green at 20% or more, yellow at 10% to 19%, and red below 10%.']),
            ('Filter',
             ['Choose a device type and a grade (A, B, C, D, Mix, As-Is, Scrap).',
              "Click 'Filter'. Click 'Clear' to show all rows."]),
            ('Add or edit a row',
             ["Click 'Add Row'.",
              "Pick 'Device Type' and 'Grade' (required). Add Material Type, Brand (optional), Min Buy "
              'Price, Max Buy Price, Target Sell Price, Min Margin % and Notes / Context.',
              'Save. If Min Margin % is left empty it is set to 15%.',
              'Use the pencil icon to edit a row or the bin icon to delete it (you will be asked to '
              'confirm).']),
            ('Bulk upload from CSV or Excel',
             ["Click 'Upload CSV/XLSX'.",
              "Click 'Download Sample CSV' to get the correct columns. device_type and grade are required; "
              'material_type, brand, min_buy_price, max_buy_price, target_sell, min_margin_pct and notes are '
              'optional.',
              "Choose your .csv or .xlsx file under 'Upload your completed file'.",
              "Keep 'Skip rows where device_type + grade + brand already exist' ticked to avoid duplicates.",
              "Click 'Upload & Import'. The message tells you how many rows were imported, skipped as "
              'duplicates, or missing required fields.'])],
  'tips': ["Only admin, Sales Manager and Inventory Manager see 'Add Row', 'Upload CSV/XLSX', edit and "
           'delete. Other users see the table read-only.',
           'Column names in the upload file are not case sensitive, and common short names (for example '
           "'sell' or 'margin') are understood.",
           'If the upload says a column was not found, check that the first row of the file has device_type '
           'and grade headers.',
           'If a CSV will not read, save it again as UTF-8.']},
 {'id': 50,
  'key': 'crm_quotes',
  'section': 'CRM',
  'module': 'Quotes',
  'url': '/crm/quotes',
  'icon': 'bi-file-earmark-ruled',
  'description': 'Price quotations sent to buyers, with line items, validity and status. Used by sales staff '
                 'and the Sales Manager.',
  'steps': [('Open the list',
             ['Go to CRM -> Quotes.',
              'The table shows Quote #, Contact, Opportunity, Date, Valid Until, Total Amount, Status and '
              'Actions.',
              'Click the eye icon to open a quote, or the printer icon to open the print view in a new '
              'tab.']),
            ('Create a quote',
             ["Click 'New Quote' (or 'Create Quote' on a sales opportunity).",
              "Pick 'Contact (Buyer)' and 'Quote Date'. 'Valid Until' defaults to 15 days ahead. Add Payment "
              'Terms and Special Conditions if needed.',
              "Under line items click 'Add Line' for each item and enter Model Name, Grade, PO Category, Qty "
              'and Unit Price (Rs). Total is calculated for each line.',
              "Use the bin icon to remove a line, then click 'Create Quote'.",
              'When opened from an opportunity that came from a won lot, the lines are filled from that '
              "lot's stock."]),
            ('Review and print',
             ["Open the quote to see the buyer, dates, payment terms, 'Line Items' and 'Special Conditions'.",
              'A Valid Until date shown in red means the quote has expired and is not yet accepted or '
              'rejected.',
              "Click 'Print Quote' to open a printable copy.",
              "Click 'Back to Opportunity' or 'All Quotes' to return."]),
            ('Update the status',
             ["In 'Update Status' pick Draft, Sent, Negotiating, Accepted, Rejected or Expired and click "
              "'Update'.",
              'Choosing Sent records the time the quote was sent.'])],
  'tips': ['A quote must have at least one line with a Model Name, otherwise it will not save.',
           "Only active buyer accounts (type Buyer or Both) appear in the 'Contact (Buyer)' list.",
           "Linking a quote to an opportunity moves an early-stage opportunity to 'Quote Sent'.",
           'Editing a saved quote is not available yet - the Edit button is disabled. Create a new quote if '
           'prices change.',
           'Quotes appear in the sidebar only for roles that have Sales Opportunities enabled.']},
 {'id': 51,
  'key': 'crm_purchase_orders',
  'section': 'CRM',
  'module': 'Purchase Orders',
  'url': '/crm/purchase-orders',
  'icon': 'bi-file-earmark-text',
  'description': 'Purchase orders raised to suppliers, created by hand or generated from a sourcing deal. '
                 'Admin, Sales Manager and Inventory Manager create and issue POs; sales and telecaller '
                 'staff can view them.',
  'steps': [('Open the list',
             ['Go to CRM -> Purchase Orders.',
              'Use the status buttons at the top to filter: All, Draft, Issued, Acknowledged, Received, '
              'Cancelled.',
              "The table shows PO #, Supplier, Deal, Date, Delivery, Amount, Status, Download and a 'View' "
              'button.',
              "Click 'Generate PO' or the printer icon to open the printable PO in a new tab."]),
            ('Create a new PO',
             ["Click 'New PO'. The PO Number is filled in automatically.",
              "Enter 'PO Date', Expected Delivery and Advance Amount.",
              "Pick the 'Supplier Contact'. Fill Supplier GSTIN, Linked Deal, Delivery Address, City, State, "
              'Pincode and Notes.',
              "Click 'Add PO Line Items'. Enter 'Item Name' (required), Description, PO Category, Lot No, "
              "Qty and Unit Price, then click 'Add to List'. Repeat for each item.",
              "Optionally enter an 'Offer Total', then click 'Create PO'. The PO is saved as Draft."]),
            ('View and issue a PO',
             ["Click 'View' to open a PO. You see the PO details, the Linked Deal and the Line Items.",
              "Click 'Print PO' to print it.",
              "When the PO is final, click 'Issue PO'. The status changes from Draft to Issued and the "
              'issuing user and time are saved.',
              'If the PO is linked to a sourcing deal at an early stage (up to Deal Agreed), that deal moves '
              "to 'PO Raised'."]),
            ('POs from sourcing deals',
             ["POs made with 'Generate PO' on a sourcing deal also appear here as Draft, linked to that "
              'deal.',
              'Open them here to print or issue them like any other PO.'])],
  'tips': ["A PO needs at least one line item - otherwise you see 'At least one line item is required.'",
           'Only a Draft PO can be issued. Trying to issue any other status shows an error.',
           "Only admin, Sales Manager and Inventory Manager see 'New PO' and 'Issue PO'.",
           'Unit Price and Total columns on the PO detail are shown only to users allowed to see pricing.']},
 {'id': 52,
  'key': 'crm_analytics',
  'section': 'CRM',
  'module': 'CRM Analytics',
  'url': '/crm/reports',
  'icon': 'bi-bar-chart',
  'description': 'Read-only reports on the CRM: deal counts, win rates, pipeline funnels, win/loss '
                 'breakdowns and staff activity. Used by managers to review sales and sourcing performance.',
  'steps': [('Read the summary',
             ['Go to CRM -> CRM Analytics.',
              'The top cards show Sourcing Deals (with how many won), Sourcing Win Rate, Sales Opps (with '
              'how many won), Sales Win Rate and Total Activities.',
              'Win rate is won deals divided by all deals, as a percentage.']),
            ('Pipeline Funnel',
             ["In the 'Pipeline Funnel' card click 'Sourcing Funnel' or 'Sales Funnel'.",
              'The table shows every stage with its Count and Pipeline Value.',
              "Switch between the two using the 'Sourcing' and 'Sales' buttons at the top."]),
            ('Win / Loss Analysis',
             ["Click 'View Analysis'.",
              "See won and lost counts in three tables: 'Sourcing by Source Type', 'Sales by Buyer Type' and "
              "'Sourcing by User'."]),
            ('Activity Leaderboard',
             ["Click 'Leaderboard'.",
              'Pick the period: 7d, 14d, 30d, 60d or 90d (30 days by default).',
              'The table ranks users by total activities, with counts for Calls, WhatsApp, Visits, Emails, '
              'Meetings and Notes.'])],
  'tips': ['All figures are live - they change as soon as deals and activities are updated.',
           "Sourcing pipeline value in the funnel uses 'Our Offer Total' of each deal; sales uses 'Estimated "
           "Value'.",
           'Automatic stage-change notes count as Notes on the leaderboard.']},
 {'id': 53,
  'key': 'crm_assign_leads',
  'section': 'CRM',
  'module': 'Assign Social Leads',
  'url': '/crm/assign-social-leads',
  'icon': 'bi-megaphone',
  'description': 'Manage leads from social media ad campaigns (Facebook, Instagram, Google Ads): group them '
                 'by ad, assign them to callers, and log every call. Used by telecallers and their managers.',
  'steps': [('Open the page and read the summary',
             ["Go to CRM -> Assign Social Leads. The page heading is 'Assign Leads'.",
              'The summary badges show leads by platform (FB, IG, Google), by result (Interested, No Answer, '
              'Not Interested, Callback, Followup, Order Placed), total Purchase and Sale quantity, and '
              'counts by dealing grade and device category.',
              "The 'Social Media Leads' table lists every lead from all groups, newest first: Lead ID, "
              'Status, Name, Phone, Email, Address, Calling Date, Require Quantity, Interested Quantity, '
              'Deals In, Purchase Quantity, Selling Quantity, Full Remark, Group and Action.',
              'Click a column heading to sort the table.']),
            ('Search and filter',
             ["'Search Ad Name' filters by lead group (ad campaign) name.",
              "'Search Customer' finds a lead by name, phone or email.",
              "Choose an assigned user and a status, then click 'Filter'. Click 'Clear' to reset."]),
            ('Create a lead group',
             ["Click 'Add Lead Group'.",
              "Enter the 'Group / Ad Name' (for example the campaign name) and click 'Create Group'.",
              "To rename, choose the group in 'Lead Group' and click 'Edit Group Name', then 'Save'."]),
            ('Add or import leads',
             ["Choose the group in the 'Lead Group' dropdown.",
              "Click 'Add Lead' and fill Lead Date, Platform, Device Category, Purchase / Selling Quantity, "
              'Planning to Buy, Whom to Sale, Deals In, Contact Mode, Dealing Grades, Customer Name, Phone, '
              "Email, Address and 'Assign To'. Click 'Add Lead'.",
              "To import many leads, click 'Sample' to download the CSV format, fill it, then click "
              "'Import', choose the file and click 'Import'.",
              'The import message shows how many leads were imported and how many rows had errors. Each lead '
              'gets its own Lead ID.']),
            ('Log calls and view history',
             ['Click the phone icon (Log Call) on a lead.',
              "Enter 'Calling Date', optional 'Follow-up Date', 'Status', Require Quantity, Device Category "
              '(Interested In), Purchase / Selling Quantity, Whom to Sale, Deals In (Indian/Imported) and '
              "'Full Remarks'. Click 'Submit'.",
              "The lead's Status changes to the status you picked, and the latest call details show in the "
              'table.',
              'Click the clock icon (Call History) to see every call logged for the lead, with who logged '
              'it.',
              "Click the pencil icon (Edit) to correct the lead's details or reassign it, then click 'Save "
              "Changes'."])],
  'tips': ['The Status list is managed by the admin in Master Data (Assign Social Leads section). Ask admin '
           'to add a new status if needed.',
           "Select the right group in 'Lead Group' before clicking 'Add Lead' or 'Import' - the leads go "
           'into the selected group.',
           'Dates in the import file can be YYYY-MM-DD, DD-MM-YYYY or DD/MM/YYYY. Put several device '
           'categories in one cell separated by commas.',
           "The assigned_to column in the import file must be the user's login name."]},
 {'id': 54,
  'key': 'care_support',
  'section': 'Customer Care',
  'module': 'Ticket Queue',
  'url': '/care-support/tickets',
  'icon': 'bi-ticket-perforated',
  'description': 'Queue of after-sale support tickets raised by customers from the care app on their device, '
                 'with device diagnostics, warranty and a full timeline. Used by customer care staff.',
  'steps': [('Open the queue',
             ['Go to CUSTOMER CARE -> Ticket Queue.',
              'The table shows Ticket (number and raised time), Device, Category, Priority, Status, '
              "Assigned, Updated and an 'Open' button.",
              'Up to the latest 500 tickets are shown, newest first.',
              "Click 'Device Pairings' to see which customer devices are paired with the care app."]),
            ('Filter and search',
             ['Choose a status (Open, Assigned, In Progress, Awaiting Customer, Needs Pickup, Resolved, '
              'Closed, Cancelled, Reopened) or a priority (Low, Medium, High, Critical). The list refreshes '
              'at once.',
              'Type a ticket number, Tag Number, serial number or model in the search box and click '
              "'Filter'."]),
            ('Open a ticket',
             ["Click 'Open' or the ticket number.",
              'The top shows the category, when it was raised and how the customer wants to be contacted.',
              "'Diagnostic Snapshot' shows the hardware details captured from the customer's device.",
              "'Device & Customer' shows the brand, model, Tag Number, serial, customer name and phone, and "
              'the warranty status with days left.',
              "'Timeline' lists every action on the ticket. 'Previous Tickets on this Device' links to older "
              'tickets for the same device.']),
            ('Assign, change status and priority',
             ["In 'Assign / Status' pick a staff member and click 'Assign'. An Open ticket becomes Assigned.",
              "Pick a new status, add an optional note and click 'Set Status'. Resolved records who resolved "
              'it and when; Closed records the close time.',
              "Pick a priority and click 'Set Priority'."]),
            ('Add notes and schedule pickup',
             ["In 'Add Note' type the note and click 'Add Note'.",
              "Tick 'Also show to customer in the app' if the customer should see this note. Otherwise it "
              'stays internal.',
              "In 'Schedule Pickup' enter the pickup reference or GRN and click 'Mark Pickup Required'. The "
              'ticket status changes to Needs Pickup unless it is already Resolved, Closed or Cancelled.'])],
  'tips': ['You need the Customer Care permission to see this page, and edit permission to assign, change '
           'status or priority, add notes or schedule pickup.',
           'Tickets are created only by customers from the care app; staff cannot create them from this '
           'page.',
           'A note cannot be empty. Notes are limited to 2000 characters.',
           'Every assignment, status change and pickup is recorded in the Timeline and the audit log.']},
 {'id': 55,
  'key': 'trade_partner_partners',
  'section': 'External Partner',
  'module': 'Partner Accounts',
  'url': '/trade-partner/partners',
  'icon': 'bi-shop',
  'description': 'Give CRM Accounts a login to the External (Trade) Partner portal and manage those logins. '
                 'Used by sales managers and admins who onboard dealers to the portal.',
  'steps': [('Open the page and read the table',
             ['Go to External Partner -> Partner Accounts.',
              "The table 'Accounts as External Partner' lists every account that has portal access.",
              "Columns: 'Business' (with dealer code and city), 'Login Mobile', 'Segment', 'Sales Owner', "
              "'Score', 'Last Login', 'Status' and 'Actions'.",
              "'Score' is a 0-100 behaviour score from bookings, payment speed and logins. A red "
              "'Token-only' badge means the dealer is limited after too many expired bookings.",
              "'Last Login' shows 'Never' if the dealer has not signed in yet."]),
            ('Filter and search',
             ["Use 'All Locations', 'All Segments' and 'All Sales Owners' to filter. The list refreshes as "
              'soon as you pick a value.',
              "Type a business name, login mobile or dealer code in 'Search partners...' and click 'Search'.",
              "Click 'Clear' to remove all filters."]),
            ('Give portal access (Generate Access)',
             ["Click 'Generate Access'.",
              "In 'Accounts', search and select one or more CRM Accounts. Each account gets its own login.",
              "If you select only one account, you can type a different 10-digit number in 'Login Mobile'. "
              'With more than one account this box is disabled and each account uses its own mobile.',
              "Choose the 'Price Segment' (New Dealer, Verified, High Volume, City Captain or Trader) and "
              "the 'Sales Owner'.",
              "Click 'Enable & Generate Password'.",
              "A yellow box 'Temporary passwords - shown once only' lists each account, dealer code, login "
              'mobile and password. Copy them now and share them with the partner login link shown below the '
              'box.',
              "Accounts that could not be enabled are listed under 'Not enabled' with the reason."]),
            ('Edit a partner',
             ["Click the pencil icon in the 'Actions' column.",
              "Change 'Login Mobile (10 digits)', 'Partner Type' (Dealer, Trader, Repair Shop), 'Price "
              "Segment' or 'Sales Owner'.",
              "Leave 'Login Mobile' as it is if you do not want to change it.",
              "Click 'Save'."]),
            ('Reset password, disable or delete',
             ["Key icon: 'Reset Password'. A new temporary password is shown once and the dealer is logged "
              'out at once.',
              "Circle icon: 'Disable' (or 'Enable'). A disabled dealer is logged out and cannot sign in "
              'until enabled again. The dealer record itself is not changed.',
              "Bin icon: 'Delete'. This is permanent. If the partner has orders, credit notes, quotations or "
              'receipts, the record is archived instead of deleted and a yellow message tells you so.'])],
  'tips': ["Temporary passwords are shown only once. If you reload the page they are gone - use 'Reset "
           "Password' to make a new one.",
           'The account must have a valid 10-digit mobile number. One mobile number can be the login for '
           'only one partner.',
           "An account that already has portal access is skipped and shown under 'Not enabled'.",
           "If an account has no matching dealer yet, a new dealer is created for it (marked 'new dealer' in "
           'the password box).',
           "The 'Sales Owner' decides whose 'My Desk' the dealer appears on.",
           'Admin always has access. Other roles need the Partner Accounts permission (add / edit) switched '
           'on by admin; it is off by default.']},
 {'id': 56,
  'key': 'trade_partner_listings',
  'section': 'External Partner',
  'module': 'Listings Manager',
  'url': '/trade-partner/listings',
  'icon': 'bi-megaphone',
  'description': 'Create and manage the stock offers (listings) that partners see in the portal catalog: '
                 'price, token, minimum order and hold time. Used by sales and inventory staff who sell to '
                 'trade partners.',
  'steps': [('Read the page',
             ['Go to External Partner -> Listings Manager.',
              "The four cards at the top show stock ageing: 'Fresh 0-7d', 'Push 8-15d', 'Discount 16-30d' "
              "and 'Liquidation 30d+', with the count and Rs value of draft, published and paused listings.",
              "The table shows 'Code', 'Title', 'Type', 'Qty' (available / total), 'Price/unit', "
              "'Token/unit', 'Ageing', 'Status' and 'Actions'.",
              "A red 'Stale price' badge means a published listing's price was not reviewed in the last 48 "
              "hours. A 'Floor override' badge means it was published below the margin floor."]),
            ('Filter the list',
             ["Choose a status in 'All Status' (Draft, Published, Paused, Sold Out).",
              "Choose a type in 'All Types' (Finished Goods, Bulk Lot, Functional Ok, As Is).",
              "Click 'Filter'."]),
            ('Create a new listing',
             ["Click 'New Listing'.",
              "Choose the 'Listing Type' and enter a 'Title'.",
              "For Finished Goods: in the ready-stock picker enter Brand (optional), grade and 'Qty of "
              "devices'. The system picks the oldest matching devices in Ready to Sale that are not already "
              'listed.',
              "For other types: select the 'Source lot' and the quantity (blank = full lot quantity).",
              "Enter 'Dealer price/unit (Rs)'. In 'Token' enter a value and choose '%' (of price) or 'Rs' "
              '(flat per unit). Blank uses the default token % from Portal Settings.',
              "Set 'MOQ', 'Hold hours' (blank uses the default), 'Visible to' (All or one price segment), "
              'and add photos if needed.',
              "Click 'Create as Draft'. The listing is saved as Draft and partners do not see it yet."]),
            ('Publish or pause',
             ["Click the green broadcast icon on a Draft or Paused listing, then 'Publish'.",
              'Publishing checks the margin floor. If the price is below the floor, only an admin can '
              "publish, and must type a 'Floor override reason'.",
              'Click the pause icon on a Published listing to pause it. Partners stop seeing it.']),
            ('Reprice or confirm the price',
             ["Click the Rs icon to open 'Reprice'.",
              "Change 'Dealer price/unit', 'Token/unit', 'MOQ' or 'Hold hours' and click 'Save'.",
              "If the price is still correct, click 'Price still OK'. This only stamps the 48-hour price "
              "review and removes the 'Stale price' badge.",
              'Existing bookings keep their old price. New prices apply to future bookings only.'])],
  'tips': ['For Finished Goods, if fewer ready devices match than the quantity you asked for, you get the '
           "error 'Only N matching ready devices available'.",
           'Photos must be jpg, png or webp and up to 5MB each; other files are ignored.',
           'Lowering the price of a published listing below the floor is blocked for non-admin users.',
           "Review published prices at least every 48 hours - stale listings also appear on 'My Desk'.",
           "Creating needs the 'add' permission; publish, pause and reprice need 'edit'. Off by default for "
           'non-admin roles.']},
 {'id': 57,
  'key': 'trade_partner_bids',
  'section': 'External Partner',
  'module': 'Bids Created',
  'url': '/trade-partner/bids',
  'icon': 'bi-hammer',
  'description': 'See all bids partners have placed on lots and listings, award a lot to the best bid, '
                 "generate the Purchase Order and track the partner's payment. Used by sales managers "
                 'handling partner lot sales.',
  'steps': [('Total Bids Created (one row per lot)',
             ['Go to External Partner -> Bids Created.',
              "The top table 'Total Bids Created' shows each lot with live bids: 'Lot No', 'Total Bids', "
              "'Lowest Bid', 'Highest Bid', the highest bidder's account and account manager.",
              'Lots are sorted with the highest bid first.',
              "Click a 'Lot No' to open 'Bids Captured' for that lot, where each bid has a 'Generate PO' "
              'button.']),
            ('Open, Won and Lost tabs',
             ["'Open' lists all live bids, highest amount first. It includes custom-price requests (marked "
              "'custom').",
              "Each bid shows the amount, and under it the base price and the gain or loss in Rs and %. 'No "
              "base set' means no base price was saved.",
              "Other columns: 'Bid ID', 'Lot Number', 'Account Name', 'Account Contact', 'Account Category', "
              "'Manager Name' and 'Download'.",
              "'Won' lists awarded bids with 'Payment Status', who marked it won and when.",
              "'Lost' lists runner-up bids on lots already awarded, marked 'Outbid'. Use these if a won deal "
              'does not complete.']),
            ('Mark a bid as won',
             ["In the top table click 'Mark Won' to award the highest bid on that lot, or in the 'Open' tab "
              "click 'Mark Won' on a specific bid.",
              'Confirm the message.',
              "The bid moves to 'Won', a Buyer Deal (sales opportunity, stage Won) is created for the "
              "account, and all other live bids on the same lot move to 'Lost'."]),
            ('Generate the Purchase Order',
             ["In the 'Won' tab click 'Generate PO' (or click the Lot Number, then 'Generate PO' on the "
              'bid).',
              "The 'PO Preview' shows the account details, the lot's models with make, device type and total "
              "quantity, and the 'Total Bid Won Price'.",
              "Select the company in 'Company Details', and optionally 'Payment Terms' and 'Delivery Terms'.",
              "Click 'Generate and attach this PO'. The PO PDF is attached to the bid and the partner can "
              'download it from the portal.']),
            ('Documents and payment',
             ["Click the paperclip icon in 'Download' to attach a QUOTE, PO or INVOICE file, then click "
              "'Upload'.",
              "Click a download button (e.g. 'PO') to download an attached file.",
              "'Payment Status' shows 'PO Pending' (no PO yet), 'Payment Pending' (PO sent, no payment), "
              "'Payment Verifying' (partner submitted payment) or 'Payment Done' (verified).",
              "In the 'Won' tab click the 'Bid ID' to see the payment date, mode, UTR / reference, amount "
              'and whether finance has verified it.'])],
  'tips': ['Mark Won cannot be undone from this page. Check the lot and amount before you confirm.',
           'A bid that is already won or lost cannot be marked won again.',
           'Per-item prices are not printed on the PO - only the total won value.',
           'Payment verification is done by the finance team, not on this page.',
           "Mark Won, Generate PO and upload need the 'edit' permission. Off by default for non-admin "
           'roles.']},
 {'id': 58,
  'key': 'trade_partner_manage_lots',
  'section': 'External Partner',
  'module': 'Manage Lots',
  'url': '/trade-partner/manage-lots',
  'icon': 'bi-box-seam',
  'description': 'A separate area for TEST data used to try out the External Partner portal: test GRNs, test '
                 'lots and test devices. Nothing created here appears on the live GRN pages, dashboards or '
                 'dealer catalog.',
  'steps': [('Understand the page',
             ['Go to External Partner -> Manage Lots.',
              'The blue note explains that this is test data only.',
              "Cards show 'Total GRNs', 'Total LOTs' and 'Total TAGs' (test devices).",
              "There are two tabs: 'GRN & Lot' and 'Asset IQC'."]),
            ('Add a test GRN',
             ["On the 'GRN & Lot' tab click 'Add GRN'.",
              'Fill in the invoice dates, invoice number, price, quantity, PO number, vehicle number, e-way '
              'bill, vendor name and notes. Uploading the invoice PDF is optional.',
              "Click 'Create GRN'. A 12-digit GRN number is created and the GRN stays test-only.",
              "Use 'Search GRN or Lot Number...' to find a row. 'Stocked' shows how many devices are mapped "
              'to the GRN.',
              "Use 'Edit' to change GRN details, or the bin icon to delete the GRN."]),
            ('Add or edit lots on a GRN',
             ["Click 'Add Lot' (or 'Edit Lot') on the GRN row.",
              "Enter the Lot Number. It must start with 'EPT-' so it stays test-only.",
              "Click 'Add another Lot' to add more lot numbers to the same GRN.",
              "If a Lot Number already exists, a warning appears. Tick 'Are you sure want to add tags in "
              "this Lot' to merge, then click 'Save'.",
              "Click the 'N Lots' link in 'Lot Number(s)' to see each lot and how many devices are mapped to "
              'it.']),
            ('Upload test devices (Asset IQC tab)',
             ["Open the 'Asset IQC' tab.",
              "Click 'Download CSV Template' and fill it in. Required columns: lot_number (must already "
              'exist), barcode, brand, model.',
              "Choose the CSV file and click 'Upload Devices'. The entity is set to 'External Partner Test' "
              'automatically.',
              "The 'Test Assets' table shows Tag Number, Lot Number, Brand / Model, CPU, Gen, RAM, Storage, "
              'Stage, upload date and GRN.']),
            ('Map, edit and move test devices',
             ["In the 'GRN' column, 'Unmapped' means the device's lot belongs to a GRN but the device is not "
              "yet linked. Click 'Map' to link it.",
              "Click the Tag Number to view device details, or 'Edit' to change serial no, brand, model, "
              "type, CPU, generation, RAM, storage, grade and notes, then 'Save'.",
              "Tick devices (or the top box to select all) and click 'Move to Ready for Sale'. The selected "
              'tags move straight to Ready to Sale.',
              "Use 'Search Tag Number or Lot Number...' to find devices."])],
  'tips': ["Use this page only for test data. Lots starting with 'EPT-' are automatically restricted and "
           'never shown in the live dealer catalog.',
           "Upload a device only after its Lot Number has been created on the 'GRN & Lot' tab.",
           "'Move to Ready for Sale' skips IQC, repair and QC. It is meant for test devices only.",
           'Editing a device never changes its lot, GRN or stage.',
           "Changes need the Manage Lots 'edit' permission. Off by default for non-admin roles."]},
 {'id': 59,
  'key': 'trade_partner_all_deals',
  'section': 'External Partner',
  'module': 'All Deals',
  'url': '/deals',
  'icon': 'bi-briefcase',
  'description': 'A simple list of deals made with CRM Accounts, each with a list of items and a total '
                 'amount, plus approval and payment status. Used by sales staff and managers.',
  'steps': [('Read the list',
             ['Go to External Partner -> All Deals.',
              'The badge at the top shows how many deals exist.',
              "Columns: 'Deal ID', 'Account Name', 'Account Type', 'Account Location', 'Amount', 'Created "
              "By', 'Approved By', 'Payment Status', 'Status' and 'Action'.",
              "Click a 'Deal ID' to see its items with quantity, unit price and total."]),
            ('Create a deal',
             ["Click 'Create Deal'.",
              "Select the 'Account' (required). Then pick one of that account's addresses in 'Location' "
              '(optional).',
              "Under 'Items' enter 'Item Name', 'Quantity' and 'Unit Price'. 'Total Price' and 'Total "
              "Amount' are worked out for you.",
              "Click 'Add Item' for more rows, or the bin icon to remove a row.",
              "Click 'Create Deal'. A Deal ID like DEAL-2026-0001 is given, with Status 'Pending' and "
              "Payment Status 'Pending'."]),
            ('Approve, mark paid or delete',
             ["Click the green tick to approve a Pending deal. Your name appears in 'Approved By'.",
              "Click the cash icon to mark the deal as paid. Payment Status changes to 'Paid'.",
              'Click the bin icon to delete a deal permanently.'])],
  'tips': ['At least one item with a name is required; rows without an item name are ignored.',
           'The amount is always recalculated by the system from quantity x unit price.',
           'Approve and Paid cannot be reversed from this page, and delete cannot be undone.',
           "Creating needs the 'add' permission; approve, paid and delete need 'edit'. Off by default for "
           'non-admin roles.']},
 {'id': 60,
  'key': 'trade_partner_bookings',
  'section': 'External Partner',
  'module': 'Bookings Queue',
  'url': '/trade-partner/bookings',
  'icon': 'bi-bookmark-check',
  'description': 'The work queue for bookings partners make in the portal: check token and balance payment '
                 'proofs, reject, extend holds and mark dispatch. Used by sales (follow-up) and finance '
                 '(payment checks).',
  'steps': [('Open and filter the queue',
             ['Go to External Partner -> Bookings Queue.',
              "Pick a status in 'All Status' (the list refreshes), or type a booking number, dealer or "
              "listing in the search box and click 'Filter'.",
              "Bookings whose hold time has passed are marked 'Expired' automatically when the page opens, "
              'and their stock goes back to the listing.']),
            ('Lot Bookings (from Catalog)',
             ['The first table lists booking requests partners made directly on a lot in the catalog.',
              "It shows 'Lot Number' (click to open the lot), 'Dealer', 'Contact', 'Qty Requested', 'Status' "
              "and 'Requested On'.",
              'This table is for viewing only; there are no actions here.']),
            ('Listing Bookings - check the token',
             ["The 'Listing Bookings' table shows booking number and date, dealer (with score), listing, "
              "quantity, 'Token', 'Balance', 'Proofs' and 'Status'.",
              "Click the UTR / 'view' link under 'Proofs' to see the payment screenshot.",
              "If status is 'Proof Uploaded', check the proof and click 'Verify'. Status becomes 'Confirmed "
              "Token'.",
              "If the payment is wrong, click the red X, type a 'Reason (shown to the dealer)' and click "
              "'Reject Booking'. The stock goes back to the listing."]),
            ('Follow up on pending payments',
             ["For 'Pending Payment' bookings, the expiry time is shown under the booking number (red if "
              'already passed).',
              'Click the clock icon to extend the hold by 24 hours.',
              'Click the WhatsApp icon to send the dealer a payment reminder with the upload link.']),
            ('Balance and dispatch',
             ["When the status is 'Confirmed Token' or 'Balance Pending', check the balance payment and "
              "click 'Balance OK'. Status becomes 'Ready For Dispatch'.",
              "For 'Confirmed Token' bookings you can click the WhatsApp icon to ask the dealer for the "
              'balance.',
              "After the goods leave, click 'Dispatched'. Status becomes 'Dispatched'."])],
  'tips': ["'Verify', 'Reject' and 'Balance OK' need the payment-verify permission (the 'upload' right on "
           "this page), normally given to finance. 'Extend' and 'Dispatched' need 'edit'.",
           "Only 'Pending Payment' or 'Proof Uploaded' bookings can be rejected, and a reason is required.",
           "'Balance OK' can be used on a 'Confirmed Token' booking when the balance was paid offline "
           'without a proof; this is recorded in the audit log.',
           "Dealers with 3 expired bookings in 30 days become 'Token-only'; with 5 their portal access is "
           'switched off automatically.',
           'Off by default for non-admin roles.']},
 {'id': 61,
  'key': 'trade_partner_my_desk',
  'section': 'External Partner',
  'module': 'My Desk',
  'url': '/trade-partner/my-desk',
  'icon': 'bi-person-workspace',
  'description': 'A daily work list for a sales owner: their portal dealers, proofs to verify, holds about '
                 'to expire, expired bookings to call and listings with stale prices. Admin sees all '
                 'dealers.',
  'steps': [('Check the summary cards',
             ['Go to External Partner -> My Desk.',
              "Cards show 'Proofs to verify', 'Balance waiting', 'Expiring < 6h' and 'Never logged in'.",
              "You see only dealers where you are the 'Sales Owner'. Admin sees all portal dealers."]),
            ('Work the action lists',
             ["'Proofs waiting for verification': click 'Open queue' to go to the Bookings Queue filtered on "
              'Proof Uploaded.',
              "'Holds expiring in < 6 hours - call now': call the dealer, or click the WhatsApp icon to send "
              'a payment reminder.',
              "'Expired bookings - follow-up call needed': click the WhatsApp icon to offer to re-book the "
              'stock.',
              "'Stale prices - review overdue (48h rule)': click 'Review' to open the Listings Manager and "
              'confirm or change the price.']),
            ('Your dealers',
             ["The bottom table lists 'My assigned dealers' (or 'All portal dealers' for admin) with score, "
              'last login and segment.',
              'Click the WhatsApp icon to send the launch message from Portal Settings. The password is not '
              "included - it says '(shared separately)'."])],
  'tips': ['Sections with nothing to do are hidden.',
           "If your dealer list is empty, ask admin to set you as 'Sales Owner' on Partner Accounts.",
           "Dealers who 'Never logged in' need a call to help them sign in for the first time.",
           'This page is read-only; actions are done on the Bookings Queue and Listings Manager. Off by '
           'default for non-admin roles.']},
 {'id': 62,
  'key': 'trade_partner_floors',
  'section': 'External Partner',
  'module': 'Margin Floors',
  'url': '/trade-partner/floors',
  'icon': 'bi-shield-lock',
  'description': 'Set the minimum selling price (margin floor) for each listing type. A listing priced below '
                 'the floor cannot be published without an admin override. Admin only.',
  'steps': [('Read the floor history',
             ['Go to External Partner -> Margin Floors.',
              "The table shows 'Listing Type', 'Rule', 'Floor %', 'Floor Rs', 'Effective From', 'By' and "
              "'Status'.",
              "The newest row for each listing type is 'Effective'; older rows are 'Superseded'.",
              'If the table is empty, no floor is set and listings can be published at any price.']),
            ('Add a new floor version',
             ["Under 'Add Floor Version' choose the 'Listing Type'.",
              "Choose the 'Rule Type' (a label for the kind of rule).",
              "Enter 'Floor % (over cost)', 'Floor value Rs/unit', or both.",
              "Click 'Add Version'. It takes effect at once for that listing type."])],
  'tips': ['Floors are never edited. To change a floor, add a new version - history is kept.',
           'If both % and value are set, the higher of the two is the floor. A listing with no cost basis '
           'uses the Rs value only.',
           "At least one of 'Floor %' or 'Floor value' is required.",
           'Only admin can open this page and publish below the floor; others get an access error.']},
 {'id': 63,
  'key': 'trade_partner_settings',
  'section': 'External Partner',
  'module': 'Portal Settings',
  'url': '/trade-partner/settings',
  'icon': 'bi-gear',
  'description': 'General settings for the External Partner portal: default hold time and token, payment '
                 'details shown to partners, and the WhatsApp launch message. Used by admins.',
  'steps': [('Change the settings',
             ['Go to External Partner -> Portal Settings.',
              "'Default hold hours': how long a booking is held when the listing does not set its own "
              '(default 24).',
              "'Default token %': token as % of dealer price when a listing does not set one (default 10).",
              "'UPI ID' and 'Bank details (shown to dealers)': shown to partners when they pay for a "
              'booking.',
              "'WhatsApp launch message template': the welcome message. You can use {dealer}, {url}, "
              '{phone}, {password} and {video}.',
              "'First-booking incentive text': an offer line shown to partners on the catalog.",
              "Click 'Save Settings'."])],
  'tips': ['New defaults apply only to listings and bookings created after you save.',
           'Check UPI and bank details carefully - partners pay to these details.',
           "Saving needs the 'edit' permission on Portal Settings. Off by default for non-admin roles."]},
 {'id': 64,
  'key': 'telesales_dashboard',
  'section': 'Sales & CRM',
  'module': 'TeleSales Dashboard',
  'url': '/telesales-dashboard',
  'icon': 'bi-speedometer2',
  'description': 'A one-page Business Snapshot of dealers, device requests, sales, returns and shared '
                 'quotations. Telecallers and sales staff see their own numbers; Admin and Sales Manager see '
                 'the whole team.',
  'steps': [('Open the dashboard',
             ['Go to Sales & CRM -> TeleSales Dashboard.',
              "Look at the badge next to 'Business Snapshot': 'My Data Only' means you see only your own "
              "dealers, requests and sales. 'All Agents' means you see everyone (Admin and Sales Manager "
              'only).',
              'The page is read-only. Nothing can be edited here.']),
            ('Read the four snapshot cards',
             ["'My Dealers' shows how many dealers are assigned to you, and how many are active. Click it to "
              'open the Dealers page filtered to your dealers.',
              "'Device Requests' shows dispatch requests raised from Ready to Sale, with the count still "
              'pending and the count approved.',
              "'Today's Sales' shows the number of sales today. If your role can see prices, it also shows "
              "today's amount and the month-to-date (MTD) amount in Rs. Click it to open the Sales list.",
              "'Returns (MTD)' shows returns this month on sales made by you (or by everyone, for managers). "
              'Click it to open the Returns page.']),
            ('Check recent activity',
             ["'Recent Device Requests' lists the last 8 requests with Tag Number, status and who raised it. "
              'Click a Tag Number to open that device.',
              "'Recent Sales' lists the last 8 sales with customer, amount (if you can see prices) and date.",
              "'Recent Returns' lists the last 8 returns with sale number, reason and date."]),
            ('Review shared quotations',
             ["The 'Quotation Shared' table at the bottom shows the latest 20 dealer quotations.",
              'Each row shows Customer Name, Customer Type, Total Quantity, Total Quotation Price (if you '
              "can see prices) and Status ('Shared' or 'Confirmed').",
              "To confirm or delete a quotation, use the Dealers page, the dealer's own page or the "
              'Quotations page.'])],
  'tips': ['Access to this page is controlled by a module permission. If you cannot open it, ask an Admin.',
           'Prices and amounts are hidden if your role is not allowed to view pricing.',
           'Numbers are based on records linked to your username (dealers assigned to you, requests you '
           'raised, sales you made).']},
 {'id': 65,
  'key': 'dealers',
  'section': 'Sales & CRM',
  'module': 'Dealers',
  'url': '/dealers',
  'icon': 'bi-building',
  'description': 'Dealer Management is the master list of dealers (B2B customers) with their latest call '
                 'status, follow-ups, outstanding amounts and quotations. Used by telecallers, sales staff '
                 'and sales managers.',
  'steps': [('Open the page and read the cards',
             ["Go to Sales & CRM -> Dealers. The page title is 'Dealer Management'.",
              "The top cards show 'Total Dealers', 'Active', 'Follow-ups Due' (latest call is Follow-up and "
              "the date is today or past) and 'Outstanding' (unpaid order amount in Rs).",
              "The second row of cards counts dealers by their latest call outcome: 'Interested', 'Not "
              "Interested', 'Detail Sent', 'Callback', 'Not Connected' and 'No Requirement'.",
              'All cards follow the filters you apply.']),
            ('Filter and search',
             ["Use 'Search' to find a dealer by company, city, state, phone or email.",
              "Use 'Status' (Active, Inactive, Blacklisted), 'Dealer Type', 'Sub Category', 'Calling From' / "
              "'Calling To', 'Req Qty', 'Purch Qty' and 'Sell Qty' as needed.",
              "Admin and Sourcing Sales see an 'Assigned To' list with every sales executive. A Sales "
              "Manager can choose 'All My Team' or 'Mine Only'.",
              "Click 'Filter' to apply. Click 'Clear' to remove all filters."]),
            ('Add or edit a dealer',
             ["Click 'Add Dealer'. Fill 'Business Name' (required) and any other fields such as Type, Sub "
              'Category, Person Name, Phone, City, Interested In, Next Followup and Status.',
              "Click 'Create Dealer'. A new dealer gets a dealer code and is assigned to you.",
              "To change a dealer, click the pencil ('Edit') button in the 'Action' column, update the "
              "fields and click 'Save Changes'.",
              "Click the eye ('View') button to see the dealer details in a pop-up, or click the Business "
              'Name to open the full dealer page.']),
            ('Log calls and follow-ups',
             ["Click the phone ('Log Call') button on a row to record a call.",
              "Choose the 'Outcome' (required), and fill Call Mode, Call Type, Next Follow-up Date, Products "
              'Discussed, quantities and Remarks.',
              "After saving, the dealer's Status badge and the outcome cards update from this latest call.",
              "Click 'Today Follow-ups' to see dealers whose next follow-up is today. Click 'Follow-ups Due' "
              'to see all due and overdue follow-ups.']),
            ('Create and manage quotations',
             ["Click the 'Quotation' button on a dealer row. The 'Create Quotation' pop-up shows Dealer "
              'Details and Company Detail.',
              'Add line items with PO Category, Product Name, Model Name, Quantity and Unit Price. The '
              'totals are calculated for you.',
              "Add 'Payment Terms' and 'Additional Notes' if needed, then click 'Create Quotation'. At least "
              'one line item is required.',
              "The new quotation appears in the 'Quotation Shared' table with status 'Shared'. Click the "
              "tick ('Confirm') button when the dealer agrees, or the bin ('Delete') button to remove it."]),
            ('Bulk upload, export and delete',
             ["Click 'Bulk Upload' to add many dealers from a CSV or Excel file. Download the sample file on "
              "that page first. A 'business_name' column is required.",
              "Click 'Bulk Upload Call Records' to upload call history. Each row must match an existing "
              'dealer by phone or business name. Unmatched rows are skipped and reported.',
              "Tick rows and click 'Export' to download them. If no rows are ticked, all dealers matching "
              'your filters are exported.',
              "Managers can tick rows and click 'Delete Selected'. Dealers that have orders, credit notes, "
              'quotations or receipts are kept (archived) and not deleted.'])],
  'tips': ['Telecallers see only the dealers assigned to them. Admin, Sales Manager and Sourcing Sales see '
           'all dealers.',
           "'Delete Selected' and the row 'Delete' button are shown only to Admin, Sales Manager and "
           'Attendance Group managers.',
           'Deleting a dealer also removes its call history. This cannot be undone.',
           "Interested and Detail Sent are counted together on the 'Interested' card."]},
 {'id': 66,
  'key': 'telecalling',
  'section': 'Sales & CRM',
  'module': 'Telecalling',
  'url': '/telecalling',
  'icon': 'bi-telephone',
  'description': "The Telecalling Dashboard shows call counts, today's follow-ups and the list of calls "
                 'logged against dealers. Used by telecallers and sales managers to track daily calling '
                 'work.',
  'steps': [('Open the dashboard',
             ["Go to Sales & CRM -> Telecalling. The page title is 'Telecalling Dashboard'.",
              "By default it shows today's calls only.",
              "The cards show 'Total Calls', 'Interested', 'Callback', 'Not Interested', 'Not Connected' and "
              "'Connected'.",
              "Click 'Interested', 'Callback', 'Not Interested' or 'Not Connected' to filter the call list "
              'to that outcome.']),
            ('Filter the calls',
             ["Use 'Search' for dealer name or phone, and 'City' for the city.",
              "Admin, Sales Manager and Sourcing Sales can pick an 'Agent' to see one person's calls.",
              "Choose an 'Outcome', set 'Call Date From' / 'Call Date To', or 'Follow-up From' / 'Follow-up "
              "To'.",
              "Click 'Filter' to apply. Click 'Clear' to go back to today's calls."]),
            ("Work on today's follow-ups",
             ["Click the red bar 'Follow-ups Due Today' to open the list.",
              'It shows Dealer, Phone, Last Call, Outcome, Items Discussed and User.',
              "Click 'Call Now' to open the Log Call form for that dealer.",
              'Dealers whose last outcome is Not Interested are not shown here.']),
            ('Read and export the Recent Calls list',
             ["The 'Recent Calls' table shows every call logged from Dealer Management that matches your "
              'filters.',
              'Columns include Time, Dealer, Location, Phone, Agent, Mode, Outcome, Model, QTY, Asking '
              'Price, Whom To Sale, Follow-up and Remarks.',
              'Click a dealer name to open the dealer page.',
              "Click 'Export CSV' to download all matching calls with full dealer details."]),
            ('Other buttons',
             ["'Log Call' opens a quick call form. Enter 'Phone Number' and 'Outcome' (both required), and "
              'optionally Dealer, Product Interest, Qty Required, Budget, Next Follow-up and Notes.',
              "'All Records' shows the calls logged with this quick form.",
              "'Performance' shows call performance per agent."])],
  'tips': ['Telecallers see only their own calls and follow-ups. Admin and Sales Manager see everyone.',
           "Calls logged with the quick 'Log Call' form are saved under 'All Records', not in the Recent "
           "Calls table. To update a dealer's call status, log the call from the dealer row on the Dealers "
           "page (or 'Call Now').",
           'Calls for deleted dealers are hidden from this page.',
           "'Connected' counts calls that were answered (Detail Sent, Language Barrier, No Requirement, "
           'Callback).']},
 {'id': 67,
  'key': 'quotations',
  'section': 'Sales & CRM',
  'module': 'Quotations',
  'url': '/quotations',
  'icon': 'bi-file-earmark-text',
  'description': 'A single list of all dealer quotations in the system. Sales staff use it to find, confirm '
                 'or delete quotations that were created from the Dealers page.',
  'steps': [('Open the list',
             ['Go to Sales & CRM -> Quotations.',
              "The 'Quotation Shared' table shows every quotation, newest first, with a count badge.",
              'Each row shows Customer Name (with the quote number), Customer Type, Total Quantity, Total '
              'Quotation Price in Rs, Status and Action.']),
            ('Filter the list',
             ["Use 'Search' to find by customer name, quote number or phone.",
              "Choose 'Status': Shared or Confirmed.",
              "Choose 'Created By' to see quotations made by one user.",
              "Set 'From' and 'To' dates, then click 'Filter'. Click 'Reset' to clear."]),
            ('Confirm or delete a quotation',
             ["Click the tick ('Confirm') button when the customer accepts. The status changes to "
              "'Confirmed' and the button disappears.",
              "Click the bin ('Delete') button to remove a quotation. Confirm the message. This cannot be "
              'undone.']),
            ('Create a new quotation',
             ['Quotations are not created on this page.',
              "Go to Sales & CRM -> Dealers and click the 'Quotation' button on the dealer row, or open the "
              "dealer's page."])],
  'tips': ["This page shows the same quotations as the 'Quotation Shared' table on the Dealers page and the "
           'TeleSales Dashboard.',
           'Access is controlled by a module permission. If you cannot open the page, ask an Admin.',
           'Totals are calculated by the system from the line items when the quotation is created.']},
 {'id': 68,
  'key': 'model_requests',
  'section': 'Sales & CRM',
  'module': 'Model Requests',
  'url': '/model-requests',
  'icon': 'bi-clipboard-plus',
  'description': 'Sales and telecalling staff use this page to ask the inventory team for a model or '
                 'configuration in a given quantity and grade. The inventory team (TRC) uses it to see '
                 'demand and mark requests fulfilled.',
  'steps': [('Open the page',
             ['Go to Sales & CRM -> Model Requests.',
              "The cards show 'Total', 'Open', 'Partially Fulfilled' and 'Fulfilled'.",
              'The table shows Requested (date), By, Config, Grade, Qty, Status, Notes and Action.',
              'Sales users see only their own requests. Admin and Inventory Manager see all requests.']),
            ('Raise a new request',
             ["Click 'New Request'.",
              'Fill what you need: Category, Brand, Model, CPU, RAM (GB), Storage (GB), Storage Type, Screen '
              "Size and 'Grade / Condition' (Any, A, B or C).",
              "Enter 'Quantity' (required) and any Notes.",
              "Click 'Submit Request'. It appears in the list with status 'Open'."]),
            ('Filter the list',
             ["Choose a 'Status' (Open, Partially Fulfilled, Fulfilled, Cancelled) and click 'Filter'.",
              "Admin and Inventory Manager can tick 'My requests only'.",
              "Click 'Clear' to remove the filter."]),
            ('Fulfil a request (Admin / Inventory Manager)',
             ["Check the 'Matching Stock' column. It shows how many Ready to Sale devices match the request. "
              'This is only a hint, nothing is allocated automatically.',
              "Click 'Update' on an open request.",
              "Enter 'Quantity Fulfilled' and Notes (for example the Tag Numbers you allocated). Click "
              "'Save'.",
              "If the full quantity is fulfilled the status becomes 'Fulfilled'. If only part, it becomes "
              "'Partially Fulfilled'."]),
            ('Cancel a request',
             ['Click the red cancel button on an open or partially fulfilled request and confirm.',
              "The status changes to 'Cancelled'."])],
  'tips': ['Only Sales, Sales Manager, Telecaller and Admin can raise requests.',
           'Only Admin and Inventory Manager can update fulfilment.',
           'You can cancel your own request. Admin and Inventory Manager can cancel any request.',
           'Quantity fulfilled can never be more than the quantity requested.']},
 {'id': 69,
  'key': 'block_tags',
  'section': 'Sales & CRM',
  'module': 'Block Tags',
  'url': '/block-tags',
  'icon': 'bi-lock',
  'description': 'Block Tags holds specific Ready to Sale tags for a pending deal or customer without '
                 'selling them. Blocked tags stay in stock but cannot be sold until they are released or '
                 'sold from the block.',
  'steps': [('Open the page',
             ['Go to Sales & CRM -> Block Tags.',
              "The 'Tags Blocked' table shows one row per block: Block ID, Box/Bundle Number, Customer, "
              'Tags, Models, Block Date, Block For and Action.',
              'Click the tags count to see the tags in the block (Tag Number, Lot Number, Model, CPU, RAM, '
              'Storage). Click the models count to see a model summary.']),
            ('Block new tags',
             ["Click 'New Block Tags'.",
              "Scan or type a Tag Number or model in 'Search + Select Tags' and pick the tags. You can "
              'select many tags.',
              "Choose 'Container' (required), and optionally Location, Box/Bundle Number, 'Block For' (the "
              'user it is held for) and Customer Name.',
              "Click 'Save'. A Block ID like 01102026-SHELF-0001 is created with today's date.",
              "On Ready to Sale the tags now show a red 'Blocked' badge and the 'Block For' user in "
              "'Assigned To'."]),
            ('Filter and search',
             ['Use the search box for Block ID, Box, Tag Number, Lot or Customer.',
              "Use 'All Containers', 'All Block For' and 'All Locations' to narrow the list.",
              "Click 'Clear' to remove filters."]),
            ('Edit, print, sell or release',
             ["Click the pencil ('Edit') button to change Block Date, Container, Box/Bundle Number, "
              "Location, Block For or Customer Name. Click 'Save'.",
              "Click 'barcode' next to the Block ID to preview the block label. Use 'Download' or 'Print'.",
              "Click 'Sell' to open New Sale with all tags in the block. Tags that are sold are released "
              'from the block.',
              'Click the unlock button to release all tags in a block. Inside the tags pop-up you can '
              'release one tag at a time. Released tags become sellable again.']),
            ('Bulk actions',
             ['Tick rows (or the top box to select all matching rows).',
              "Click 'Export Selected' to download the selected blocks.",
              "Click 'Bulk Release' to release every tag in the selected blocks."])],
  'tips': ['Only tags in Ready to Sale that are not already blocked can be blocked. If you scan a tag that '
           'is not allowed, the reason is shown under the search box.',
           'A blocked tag cannot be sold from Ready to Sale until it is released.',
           "The 'Sell' button is shown only if your role can add sales.",
           'Adding blocks and editing or releasing need the matching Block Tags permission (add / edit).']},
 {'id': 70,
  'key': 'sales',
  'section': 'Sales & CRM',
  'module': 'Ready to Sale',
  'url': '/sales/ready',
  'icon': 'bi-cart-check',
  'description': 'Ready to Sale lists every device that has passed QC and can be sold, as single tags and as '
                 'As-Is lots, with prices. Sales staff use it to find stock, set prices and start a sale.',
  'steps': [('Open the page and choose a tab',
             ['Go to Sales & CRM -> Ready to Sale.',
              "If there are open CRM opportunities for the device types in stock, a yellow 'Interested "
              "Buyer(s)' box shows them at the top.",
              "The first tab set has 'Ready for Sale Tags' and 'Ready for Sale Model Summary'. The badge "
              'shows how many tags or models there are.',
              "The second tab set lower down has 'As-Is Lot' and 'As-Is Model Summary' for tags grouped in a "
              'Sub-Lot.']),
            ("Find tags on 'Ready for Sale Tags'",
             ["The filters sit on the right of the card header: Touch Type, 'Device Types', 'Entity', "
              "'Search' and 'Reset'. The table reloads as soon as you change a filter. 'Reset' clears all "
              'three.',
              'Scan a Tag Number or Serial, or type a brand or model, in the Scan/Search box above the '
              'table. A scanned tag is ticked automatically.',
              'Columns show Tag Number, Lot, Location ID, Brand, Model, RAM, Storage, Grade, Unit Cost (if '
              "you can see prices), Assigned To (with an 'Open' or 'Blocked' badge), Min Selling Price and "
              'Max Selling Price.',
              'Click a Tag Number to open the device. Click the Lot to see all devices in that lot.']),
            ('Sell tags',
             ["Click 'Sell' on a row to open New Sale for that one tag.",
              'To sell many tags, tick them (or tick the header box to select all tags matching the '
              'filters). The bulk buttons sit just left of the Scan/Search box.',
              "Click 'Multi-Sell with selected' to open the New Sale page with the selected tags.",
              "Click 'New Bulk Sale' to open the New Tag Sale page with the selected tags (per-tag prices, "
              'no quantity limit).',
              "Click 'Bulk Upload Tags' and choose a CSV with a 'tag_number' column, then 'Select Tags'. "
              'Matching tags are ticked, and a message lists tags not found or not in Ready to Sale.']),
            ('Set selling prices',
             ["Click 'Set price' on a row, enter Min Selling Price and/or Max Selling Price, and click "
              "'Save'.",
              "To price many tags, tick them and click 'Bulk Set price'.",
              "A price marked 'auto' is calculated by the system (unit price + other rates + margin). A "
              'saved price always replaces it.',
              'Leave a box empty to go back to the calculated price.']),
            ('Use the Model Summary tabs',
             ["'Ready for Sale Model Summary' shows one row per configuration (Model, Make, Device Type, "
              'CPU, Gen, RAM, Storage) with Total Quantity, grade split and total Min Selling Price.',
              "Filter by Touch Type, 'Grades', 'Device Types' and 'Entity'. The Total row at the bottom adds "
              'up the rows shown.',
              "Click 'Sell' on a row to open New Sale with all tags of that configuration.",
              "'As-Is Model Summary' works the same way for As-Is tags."]),
            ('Work with As-Is Lots',
             ["The 'As-Is Lot' tab shows one row per Lot + Sub-Lot with Device Type, Model Name, Total "
              'Quantities, Availability, Lot Price, Min and Max Selling Price and Assigned To.',
              'Click the Device Type or Model Name button to see the breakdown.',
              "Click 'Set price' to set the lot's Min / Max Selling Price. Admin can click the pencil to "
              'change the Sub-Lot Number or Assigned To.',
              "Click 'Sell' (New Sale) or 'New Sell' (New Tag Sale) to sell the whole lot.",
              "Click 'Open Tag' and confirm to release all tags of the Sub-Lot back into the 'Ready for Sale "
              "Tags' table as single tags."])],
  'tips': ["As-Is tags (with a Sub-Lot) never appear in the 'Ready for Sale Tags' table until you use 'Open "
           "Tag'.",
           "A tag with a red 'Blocked' badge is held on the Block Tags page. Its sale is refused until it is "
           'released.',
           "'Set price' and 'Bulk Set price' are visible only to roles allowed to set prices (Master Data -> "
           'Pricing Visibility).',
           'Unit Cost and Lot Price columns are hidden if your role cannot view pricing.',
           'Your ticked tags stay selected when you change pages in the table.']},
 {'id': 71,
  'key': 'ready_to_sale_parts',
  'section': 'Sales & CRM',
  'module': 'Ready to Sale Parts',
  'url': '/ready-to-sale-parts',
  'icon': 'bi-cpu',
  'description': 'Lists new spare parts that are in stock and can be sold. Sales staff raise a sale request '
                 'here, and once it is approved they sell the part.',
  'steps': [('Open the page',
             ['Go to Sales & CRM -> Ready to Sale Parts.',
              'The table shows Code, Name, Make, Model, In Stock, Requested Quantity and Action.',
              'Only parts added as new with stock left are shown. Parts with zero stock drop off the list.',
              "The 'In Stock' badge is green when stock is fine, yellow when it is at or below the low-stock "
              'level.']),
            ('Search and filter',
             ["Type in 'Search Code' (for example PART-0001) or 'Search Part Name'.",
              "Choose 'All Makes' or 'All Models' to pick one make or model.",
              "Click 'Search'. Click 'Reset' to clear."]),
            ('Raise a sale request',
             ["Click 'Request' on the part.",
              "In 'Raise Sale Request', enter 'Requested Quantity'. It cannot be more than the stock shown.",
              "Click 'Raise Request'. A yellow 'Pending' badge appears on the row.",
              'The request is then approved or rejected on the Parts Sale Request page.']),
            ('Sell the part',
             ["When the request is approved, a green 'Approved' badge shows and the 'Sell' button becomes "
              'active.',
              "Click 'Sell' to open New Parts Sale for that part and complete the sale.",
              "If the request was rejected, a red 'Rejected' badge shows. Raise a new request if needed."])],
  'tips': ['You cannot raise a second request for a part while one is still pending.',
           'One approved request allows exactly one sale.',
           'This page is open to Admin, Sales, Sales Manager and Spare Parts Manager.',
           'Stock shown = quantity in stock minus quantity handed over minus quantity sold.']},
 {'id': 72,
  'key': 'scrap_for_sale',
  'section': 'Sales & CRM',
  'module': 'Scrap for Sale',
  'url': '/scrap-for-sale',
  'icon': 'bi-recycle',
  'description': 'Lists every Scrap ID batch of scrap Tag Numbers and spare parts waiting to be sold, and '
                 'lets you record a stand-alone Custom Sale of scrap material. Used by admins, inventory '
                 'managers and sales managers.',
  'steps': [('Open the page and read the batch table',
             ['Go to Sales & CRM -> Scrap for Sale.',
              'Each row is one Scrap ID batch. A batch is created from Inventory -> Move Device when the '
              "Transfer Type is 'Scrap for Sale'. You cannot create a batch on this page.",
              "Columns: 'Scrap ID', 'Device Type', 'Total Quantities', 'Min Selling Price', 'Max Selling "
              "Price', 'Assigned To' and 'Action'.",
              "In 'Device Type', click the 'Tags' button to see the Tag Numbers grouped by Model Name, or "
              "the 'Parts' button to see the spare parts grouped by Category.",
              "Use the search box and 'Page view' list above the table to find a batch."]),
            ('Set the selling price of a batch',
             ["Click 'Edit' in the 'Action' column of the batch.",
              "Enter the 'Min Selling Price' and 'Max Selling Price' in Rs. You can leave a box empty to "
              'clear it.',
              "Click 'Save'. The message 'Selling price updated' appears and the new prices show in the "
              'table.']),
            ('Mark a batch Sold or remove it',
             ["Click 'Sell' to mark the batch as Sold. The 'Sell' button changes to a green 'Sold' badge.",
              "Click 'Delete' and confirm to remove the batch from this list.",
              'Both actions are saved in the audit log.']),
            ('Record a Custom Sale',
             ["Click 'Custom Sale' (green button, top right). A form opens.",
              "Select the 'Entity'. Its company details (name, address, GSTIN, state, phone, email) appear "
              'next to it from Company Settings.',
              "Check the 'Sale Date' (today by default). Optionally fill 'Sales Person' and 'Invoice "
              "Number'.",
              "Enter the customer: 'Company Name' is required. 'Phone Number', 'Email Address', 'Address', "
              "'City' and 'State' are optional.",
              "Under 'Items', fill one row per item: 'Item Name', optional 'Item Category', then EITHER "
              "'Total Weight (kg)' and 'Unit Price (Weight)' OR 'Quantity' and 'Unit Price'. Click 'Add "
              "Item' for more rows; click the red X to remove a row.",
              "Check the 'Total Price' of each row and the 'Total Sale Price' at the bottom, then click "
              "'Save Sale'.",
              "The sale gets a number like SCS-0001 and the message 'Custom Sale ... saved' shows the total. "
              'Open it later on Sales & CRM -> Scrap Sale Report.']),
            ('Check devices not yet in a batch',
             ["Scroll down to 'Devices Not Yet in a Batch'. It lists Tag Numbers that are at the Scrap for "
              'Sale stage but were moved there by a bulk stage move, so they have no Scrap ID.',
              "Columns: 'Tag Number', 'Brand', 'Model', 'Grade' and 'Moved' (date). Click a Tag Number to "
              'open the device.',
              'To put these devices in a proper batch, move them through Inventory -> Move Device with '
              "Transfer Type 'Scrap for Sale'."])],
  'tips': ["The 'Sell' button only marks the batch as Sold. It does not record a buyer, a price or a sale "
           'record, and it does not change the Tag Numbers in the batch. Keep your invoice details elsewhere '
           "or use 'Custom Sale'.",
           'A Custom Sale is stand-alone: it does not change any Tag Number or Scrap ID batch, and it is not '
           'counted in Business P&L.',
           'In each item row use only one pricing pair. When you type in the weight boxes the quantity boxes '
           'lock, and the other way round. Quantity must be a whole number and amounts cannot be negative.',
           'The total shown in the form is only a preview. The system recalculates every total when you '
           "click 'Save Sale'.",
           "If the Entity shows 'No Company Setting is tagged with this entity', the sale still saves but "
           'without entity details. Ask an admin to tag a Company Setting with that entity.',
           "The 'Scrap Sale Report' button appears in the page header only if you have permission for that "
           'report.']},
 {'id': 73,
  'key': 'scrap_sale_report',
  'section': 'Sales & CRM',
  'module': 'Scrap Sale Report',
  'url': '/scrap-sale-report',
  'icon': 'bi-receipt-cutoff',
  'description': 'Read-only list of all Custom Sales saved from the Scrap for Sale page, with the full item '
                 'details of each sale. It is OFF by default for every role except admin until it is enabled '
                 'in the Permission Matrix.',
  'steps': [('Open the report',
             ["Go to Sales & CRM -> Scrap Sale Report, or click 'Scrap Sale Report' in the header of the "
              'Scrap for Sale page.',
              "If you cannot see it, ask an admin to enable 'Scrap Sale Report' for your role in Master Data "
              '-> Permission Matrix.',
              "The header shows the 'Total Sale Price' of all saved custom scrap sales. Click 'Scrap for "
              "Sale' to go back."]),
            ('Read the table',
             ['Each row is one Custom Sale, newest Sale Date first.',
              "Columns: 'Sale No.', 'Sale Date', 'Entity', 'Invoice Number', 'Sales Person', 'Customer', "
              "'Phone', 'City / State', 'Items' (number of item rows), 'Total Sale Price', 'Created By' and "
              "'Action'.",
              'Use the search box to find a sale by number, customer, invoice or any other visible value. '
              'Click a column heading to sort.']),
            ('View one sale',
             ["Click 'View' in the 'Action' column.",
              'The window shows the Entity and company name, Sale Date and Invoice, Sales Person, Customer '
              'contact, Address and Created By.',
              'Below it, the item table shows each item with its Category and either Total Weight (kg) and '
              "Unit Price (Weight), or Quantity and Unit Price, plus the line Total Price and the 'Total "
              "Sale Price'."])],
  'tips': ["This page only shows sales. To add a new one, use 'Custom Sale' on Sales & CRM -> Scrap for "
           'Sale.',
           'There is no edit, delete or export button on this report.',
           'These sales are stand-alone records. They are not part of Business P&L and do not change any Tag '
           'Number or Scrap ID batch.',
           "Batches marked Sold with the 'Sell' button on Scrap for Sale do not appear here - only Custom "
           'Sales do.']},
 {'id': 74,
  'key': 'sales_list',
  'section': 'Sales & CRM',
  'module': 'Sales List',
  'url': '/sales',
  'icon': 'bi-receipt',
  'description': 'Shows every sold Tag Number with its sale number, invoice, customer and payment, with '
                 'filters, CSV exports and links to the invoice and waybill. Used by sales staff, sales '
                 'managers, telecallers and admins.',
  'steps': [('Open the page and read the summary',
             ['Go to Sales & CRM -> Sales List.',
              "The four cards at the top show 'Total Registered' devices, 'Sold', 'Available' and 'Filtered "
              "Sales' (the number of sales that match your filters).",
              "The 'Sold Tags' table lists each sale, newest first. Columns include 'Sale#', 'Invoice', "
              "'Date', 'Tag Number', 'Grade', 'Stock Type' (Finish Good or As-Is Lot), 'Payment', 'By', "
              "'Sales Person', 'Download' and 'Action'.",
              "If your role can see pricing, you also see 'Cost/Unit', 'Price', 'Margin' and, under the "
              "table, 'Total Revenue (all filtered rows)'.",
              "A 'Replaced' badge means the tag was given at Rs 0 as a replacement for a returned tag."]),
            ('Filter and search',
             ["Use the filter bar: 'Tag Number / Brand / Model', 'Sale#', 'Customer name', 'All Users' (sold "
              "by), 'All Grades', 'All Lots', and the two date boxes (from and to).",
              "Click 'Search' to apply. Click 'Reset' to clear all filters.",
              "You can also type in the table's own search box; it searches all matching sales, not only the "
              'page on screen.']),
            ('Open a sale, invoice or waybill',
             ["Click the 'Sale#' or the eye icon in 'Action' to open the sale detail page.",
              'Click the receipt icon to print the Invoice, or the truck icon to print the Waybill (both '
              'open in a new tab).',
              "If an invoice file was uploaded at sale time, click the download icon in the 'Download' "
              'column to get it.',
              "On the sale detail page, click 'Update Transport' to set Transport Mode, Delivery Courier, "
              'Tracking Number, Dispatch Date and Delivery Status (Pending, Dispatched or Delivered), then '
              "click 'Save'."]),
            ('Export sales',
             ["Tick the boxes of the sales you need, then click 'Export Selected (n)' to download them as "
              'CSV.',
              'Tick the box in the header to select every sale that matches the current filters and search, '
              'on all pages.',
              "Click 'Export' to download all sales that match the current filters as CSV.",
              "Click 'Export All CSV' to download all sales, ignoring filters (up to the newest 5000)."]),
            ('Delete sales (admin only)',
             ["Admins see 'Delete Selected'. Tick the sales and click it, then confirm.",
              'This PERMANENTLY deletes the sale records. It cannot be undone.',
              'A Tag Number left with no sale goes back to the stage it was sold from (Ready to Sale if that '
              'is unknown).',
              'Sales linked to credit notes, receipts or warranty records are skipped and reported in a '
              'message.'])],
  'tips': ["Apply filters with 'Search' before using 'Export' - the export follows the filter bar.",
           'Up to 2000 sales can be deleted in one go.',
           "Use the 'Overdue Orders' box at the bottom ('View All Overdue Orders') to see dealer orders past "
           'their expected delivery date.',
           'Pricing columns and Total Revenue are hidden for roles that are not allowed to see pricing.',
           'New sales are not made on this page. Sell from Ready to Sale.']},
 {'id': 75,
  'key': 'part_sales',
  'section': 'Sales & CRM',
  'module': 'Spare Part Sales',
  'url': '/part-sales',
  'icon': 'bi-cart-check',
  'description': 'Lists all spare parts that have been sold and is the starting point to record a new parts '
                 'sale. Used by sales staff, sales managers, spare parts managers and admins.',
  'steps': [('Open the page and read the table',
             ['Go to Sales & CRM -> Spare Part Sales.',
              "The 'Sold Spare Parts' table shows 'Sale ID' (PS-0001...), 'Invoice', 'Date', 'Part Name', "
              "'Part Make', 'Part Model', 'Qty', 'Payment Mode' and 'Sales Person' (the user who recorded "
              'the sale).',
              "If your role can see pricing, you also see 'Stock Unit Price', 'Sale Unit Price' and 'Margin' "
              '(green for profit, red for loss).']),
            ('Filter by date and export',
             ["Pick a start and end date in the filter bar and click 'Search'. Click 'Reset' to clear.",
              'Use the table search box to find a part, sale or invoice.',
              "Click 'Export' to download the rows that match your date filter as CSV (maximum 5000 rows)."]),
            ('Before you can sell: get an approved request',
             ['A part can be sold only after a sale request for it is approved.',
              "Raise the request on Ready to Sale Parts with the 'Request' button.",
              'A manager approves or rejects it on Parts Sale Request.',
              'One approval allows exactly one sale.']),
            ('Record a new parts sale',
             ["Click 'New Parts Sale'.",
              "Select the 'Part Name'. Only parts with an approved request and stock on hand are listed. "
              "'Part Make', 'Part Model' and 'Available Quantity' fill in.",
              "Enter 'Sale Quantity' and 'Sale Price' (per unit, in Rs). 'Total Sale Price' is calculated "
              "for you. 'Sale Date' defaults to today and cannot be in the future.",
              "Choose 'Payment Mode' and fill the customer details, 'Sales Person Name', 'Invoice No.', "
              "'Address' and 'Notes' as needed.",
              "Click 'Record Sale'. You return to this page with the message 'Sale PS-.... recorded'. The "
              "part's Sold count goes up and the approval is used up."])],
  'tips': ["If New Parts Sale says 'No parts are available to sell', the part has no approved request or no "
           'stock.',
           'Sale Quantity cannot be more than the Available Quantity; the system rejects it.',
           'Margin = (Sale Unit Price - Stock Unit Price) x Qty.',
           'There is no edit or delete button for a recorded parts sale on this page.']},
 {'id': 76,
  'key': 'gate_pass',
  'section': 'Sales & CRM',
  'module': 'Gate Pass',
  'url': '/gate-pass',
  'icon': 'bi-shield-check',
  'description': 'Security gate view of all sold Tag Numbers. The guard checks the goods against the '
                 'Delivery Challan and confirms that they physically left the premises. Used by admins, '
                 'sales managers, sales staff and inventory managers.',
  'steps': [('Open the page and read the table',
             ['Go to Sales & CRM -> Gate Pass.',
              "Columns: 'Sale ID', 'Date', 'Invoice', 'Tag Number', 'Lot', 'Device', 'Customer', 'Status', "
              "'Delivery Challan' and 'Action'.",
              "Status 'Ready to Exit' (yellow) means the goods have not left yet. 'Dispatched' or "
              "'Delivered' means the exit is already done.",
              "A 'Customer Return' badge under the Tag Number means the device was returned by a customer at "
              'some time.']),
            ('Find the goods',
             ['Scan or type a Tag Number, sale number or invoice number in the box at the top right. The '
              'table filters as you type.',
              "Use 'Sale Date From', 'Sale Date To' and 'Lot Number', then click 'Filter'. Click 'Reset' to "
              'clear.']),
            ('Check and confirm one exit',
             ['Match the physical device with the Tag Number on the row.',
              "Click 'DC' in 'Delivery Challan' to open and print the Delivery Challan.",
              "Click 'Confirm Exit' on the row. Optionally pick 'Transport Mode' and 'Delivery Courier' and "
              "type a 'Tracking Number'.",
              "Click 'Confirm Exit' in the window. The status changes to 'Dispatched' and today is saved as "
              'the dispatch date (if none was set).']),
            ('Confirm many exits at once',
             ['Tick the rows to exit, or tick the header box to select all visible rows.',
              "Or click 'Upload Tags', choose a CSV/XLSX file with one Tag Number per row, or paste Tag "
              "Numbers, then click 'Select Tags'. The window tells you how many were selected, already "
              'exited and not found.',
              "Click the green 'Confirm Exit' button at the top, add optional transport details, and "
              'confirm.',
              "Click 'Clear' to remove the selection."])],
  'tips': ['Rows already Dispatched or Delivered cannot be selected again; in a bulk exit they are skipped '
           'and counted in the message.',
           'Transport details are optional. Blank fields keep what the sale already has.',
           'Confirming an exit only changes the delivery status of the sale. It does not move the Tag Number '
           'to another stage.',
           'The upload file may have a header named tag_number, barcode, Tag No or Tag Number, or no header '
           '(first column is used).']},
 {'id': 77,
  'key': 'returns',
  'section': 'Sales & CRM',
  'module': 'Returns',
  'url': '/returns',
  'icon': 'bi-arrow-return-left',
  'description': 'Lists all product returns and replacements, and is where staff log a new return (Internal '
                 'Tags, External Tag or Credit Note). Sales managers and admins approve or reject pending '
                 'returns.',
  'steps': [('Open the page and read the list',
             ['Go to Sales & CRM -> Returns.',
              "Columns: 'Return Date', 'Tag Number', 'Serial Number', 'Device', 'Sale No.', 'Sale Price', "
              "'Paid Repair', 'Reason', 'Condition', 'Status', 'Processed By' and 'Receipt'.",
              "Status is 'Pending', 'Approved' or 'Rejected'. Rows with status 'Replacement' are the tags "
              "given at Rs 0 as a replacement; they show a 'Replaced with ...' badge.",
              "Filter with 'From' and 'To' dates and 'Replacement: All / Yes / No'. Click 'Reset' to clear, "
              "'Export' to download the shown rows as CSV."]),
            ('Log a return for a tag we sold (Internal Tags)',
             ["Click 'Process Return' and stay on the 'Internal Tags' tab.",
              "Scan the Tag Number or Serial Number in 'Customer Tag' and click 'Check'. The sale and "
              'warranty details appear.',
              "If the device is Out of Warranty, a 'Paid Repair' charge (default from Cost Config, e.g. Rs "
              '1500) is added.',
              "Choose 'Replace Now': No, or Yes and scan a 'Replace With - Tag Number' that is in Ready to "
              'Sale.',
              "Select 'Return Reason', 'Condition on Return' and 'Return Received By' (all required), add "
              "the complaint and notes, then click 'Process Return'.",
              'The tag moves to Stock In at once and the return waits for manager approval. With Replace Now '
              '= Yes, the replacement tag is sold at Rs 0 to the same customer.']),
            ('Log a walk-in return (External Tag)',
             ["Open the 'External Tag' tab for an item with no sale in this system.",
              "Enter 'Tag Number' (required), Entity, Serial Number, Quantity, Make, Model and customer "
              'details.',
              "Select 'Reason', 'Condition' and 'Return Received By' (required). Set 'Paid Repair' to 0 if "
              'no charge applies.',
              "Click 'Submit External Return'. A new tag is created at IQC if it does not exist, and the "
              'return waits for approval.']),
            ('Send tags to Credit Note',
             ["Open the 'Credit Note' tab.",
              "Scan each Tag Number and press Enter or click 'Add'.",
              "Enter the 'Debit Note Number' (required), 'Debit Note Amount' and sender details, and select "
              "'Return Received By'.",
              "Click 'Submit (n tag(s))'. The tags enter the Credit Note pipeline; follow them on the Credit "
              'Note page.']),
            ('Approve or reject (managers)',
             ["Admins and sales managers click 'Pending Approvals'.",
              'Approve a return to move the tag to IQC for repair.',
              'Reject a return with a reason. Rejecting does not move the tag.']),
            ('Print a return receipt',
             ["Click 'Receipt' on a return row to see entity, customer, reason, condition, Tag Number, "
              'serial, make, model and Paid Repair.',
              "Click 'Download / Print' to open a printable receipt. Use the browser's Print -> Save as PDF "
              'to keep a copy.'])],
  'tips': ['Only one return is allowed per sale. A second one for the same sale is blocked.',
           'The replacement tag must be in Ready to Sale and must be different from the returned tag.',
           "'Return Received By' must be an active user; it is shown as Sales Person on the Rs 0 sale it "
           'creates.',
           "Processing a return needs the 'add' permission for Returns.",
           "The 'Pending Approvals' button is shown only to admins and sales managers."]},
 {'id': 78,
  'key': 'assign_dealer_leads',
  'section': 'Sales & CRM',
  'module': 'Assign Dealer Leads',
  'url': '/assign-dealer-leads',
  'icon': 'bi-person-lines-fill',
  'description': 'Shows all dealers (the same records as Dealer Management) so a manager can assign them to '
                 "users in bulk, add or upload dealers, and export the list. Access depends on the 'Assign "
                 "Dealer Leads' permission.",
  'steps': [('Open the page and read the cards',
             ['Go to Sales & CRM -> Assign Dealer Leads.',
              "The cards show 'Active Dealers', 'Inactive Dealers' (inactive and blacklisted), 'Unassigned "
              "Dealers' and 'Assigned Dealers' for the current filters.",
              "The 'Dealer Leads' table shows Dealer Name, Deals In, Dealer Type, Sub Category, Person Name, "
              'Phone, Email, City, State, Address, Status, Assigned To, Notes and Action.']),
            ('Filter the list',
             ["Use 'Search' (name, code, phone, city, sub category), 'City / State / Address', 'Status', "
              "'Assigned To', 'Dealer Type' and 'Sub Category'.",
              "Click 'Filter' to apply, 'Clear' to reset.",
              "Next to the table search box, tick 'Unassigned Dealers' or 'Assigned Dealers' to show only "
              'those rows on all pages.']),
            ('Assign dealers to a user',
             ['Tick the dealers you want, or tick the header box to select all rows the table is showing.',
              "Click 'Assign User'; the badge shows how many are selected.",
              "In 'Select User', pick a user, or pick 'Unassigned (remove user)' to clear the owner.",
              "Click 'Assign'. The table and cards refresh without reloading the page.",
              "To assign one dealer, click the person icon in its 'Action' column."]),
            ('Add, upload and edit dealers',
             ["Click 'Add Dealer' to create one dealer in a form, then 'Create Dealer'.",
              "Click 'Download Sample' to get the upload template, fill it, then click 'Upload Bulk' to "
              'import it.',
              "Click the pencil icon in 'Action' to edit a dealer."]),
            ('Export or delete',
             ["Click 'Export' to download the selected rows, or every row the table is showing if nothing is "
              'selected, as CSV.',
              'Click the trash icon on a row and confirm to move that dealer to Trash. It disappears from '
              'this page and from Dealer Management.',
              "Managers also see 'Delete Selected (n)'. It PERMANENTLY deletes the selected dealers and "
              'their call logs. Dealers with orders, credit notes, quotations or receipts are archived '
              'instead.'])],
  'tips': ["Assigning and deleting need the 'edit' permission for Assign Dealer Leads.",
           "'Delete Selected' is shown only to admins, sales managers and attendance group managers (or "
           'roles granted that right).',
           "The single-row delete message says 'Deactivate', but the dealer is actually moved to Trash and "
           'hidden from both dealer pages.',
           'Assigning a dealer replaces the old owner; the change is saved in the audit log.']},
 {'id': 79,
  'key': 'finance',
  'section': 'Finance',
  'module': 'Accounts',
  'url': '/accounts',
  'icon': 'bi-wallet2',
  'description': 'A quick money summary: total paid to suppliers, total received from customers, and the 10 '
                 'latest entries of each. Used by admins, inventory managers and sales managers.',
  'steps': [('Open the page',
             ['Go to Finance -> Accounts.',
              'Only Admin, Inventory Manager and Sales Manager can open this page. Other users are sent back '
              "to the home page with 'Access denied'."]),
            ('Read the summary cards',
             ["'Total Paid to Suppliers' (red) is the sum of every supplier payment ever recorded.",
              "'Total Received from Customers' (green) is the sum of every customer receipt ever recorded.",
              "Click 'View All' under a card to open Supplier Payments or Customer Receipts."]),
            ('Check recent entries',
             ["'Recent Supplier Payments' shows the 10 newest payments: Date, Supplier, Mode and Amount.",
              "'Recent Customer Receipts' shows the 10 newest receipts: Date, Contact, Mode and Amount.",
              'A dash in a column means the value was not entered.'])],
  'tips': ['This page is read-only. To add a payment or receipt, use Supplier Payments or Customer Receipts.',
           'The totals are all-time totals. There is no date filter on this page.',
           "Receipts added from the 'Record New Receipt' form or by bulk CSV have no contact, so the Contact "
           'column shows a dash for them.']},
 {'id': 80,
  'key': 'finance_supplier',
  'section': 'Finance',
  'module': 'Supplier Payments',
  'url': '/accounts/supplier-payments',
  'icon': 'bi-arrow-up-circle',
  'description': 'Record money paid to suppliers, see the full payment history, and close payment on '
                 'Purchase Orders. Used by admins, inventory managers and sales managers.',
  'steps': [('Open the page',
             ['Go to Finance -> Supplier Payments.',
              'Only Admin, Inventory Manager and Sales Manager can open it.',
              "The page has three parts: 'Record New Payment', 'Payment History' and 'List of PO "
              "Generated'."]),
            ('Record a new payment',
             ["In 'Record New Payment', pick the 'Supplier' (active suppliers only).",
              "Optional: pick a 'Linked Lot' (latest 50 lots) and a 'Supplier Deal ID' (latest 100 sourcing "
              'deals).',
              "Enter 'Payment Date' and 'Amount (Rs)'. Both are required.",
              "Pick 'Payment Mode' (CASH, UPI, NEFT, RTGS, CHEQUE or CARD) and type the 'Reference / UTR'.",
              "Tick 'Advance Payment' if this is an advance.",
              "Optional: add 'Notes', an 'Invoice PDF' and a 'Payment Photo'.",
              "Click 'Record Payment'. You see 'Payment recorded' and the entry appears in Payment "
              'History.']),
            ('View payment history',
             ["'Payment History' shows Date, Supplier, Lot, Mode, Ref, Advance, Amount, Document and By (who "
              'recorded it), newest first.',
              "Use the 'All Suppliers' drop-down to see one supplier only. The 'Total' row at the bottom "
              'changes to match.',
              'In the Document column, click the PDF icon to open the invoice, or the picture icon to open '
              'the payment photo.']),
            ('Complete payment on a Purchase Order',
             ["'List of PO Generated' shows the latest 100 POs with PO Number, Supplier, Date, Amount and "
              "Payment Status ('Pending' or 'Paid').",
              "Click 'Complete Payment' on a Pending PO.",
              "Enter the 'UTR Number' (required) and, if you have it, upload a 'Payment Snapshot' image.",
              "Click 'Complete Payment'. The PO status changes to 'Paid' and the button disappears."]),
            ('Upload a PO invoice',
             ["Click 'Upload Invoice' on any PO row.",
              "Choose the invoice PDF and click 'Upload'. You see 'Invoice uploaded'.",
              'The PDF icon then appears in the Document column of that PO.'])],
  'tips': ["Amount must be more than 0, otherwise you get 'Invalid amount'.",
           'Payments cannot be edited or deleted from this page. Check the amount and supplier before you '
           "click 'Record Payment'.",
           "'Complete Payment' on a PO only marks the PO as Paid. It does not add a line to Payment History "
           '- record the payment in the form above if you need it there.',
           'Uploading a new invoice on a PO replaces the old one.']},
 {'id': 81,
  'key': 'finance_customer',
  'section': 'Finance',
  'module': 'Customer Receipts',
  'url': '/accounts/customer-receipts',
  'icon': 'bi-arrow-down-circle',
  'description': 'Record money received from customers and dealers, upload many receipts at once from a CSV '
                 'file, and close payment on Quotes. Used by admins, inventory managers and sales managers.',
  'steps': [('Open the page',
             ['Go to Finance -> Customer Receipts.',
              'Only Admin, Inventory Manager and Sales Manager can open it.',
              "The page has 'Record New Receipt', 'Receipt History' and 'List of Quotes Sent'."]),
            ('Record a new receipt',
             ["Pick the 'Dealer', or leave '- Walk-in / No Dealer -'.",
              "Optional: pick an 'Opportunity ID' (latest 100 sales opportunities).",
              "Optional: paste a 'Dealer Order ID'. The order's due balance is reduced by this amount, and "
              "the order becomes 'paid' when nothing is left due.",
              "Enter 'Receipt Date' and 'Amount (Rs)' (both required), then 'Payment Mode', 'Reference' and "
              "'Notes'.",
              "Optional: attach an 'Invoice PDF' and a 'Payment Photo'.",
              "Click 'Record Receipt'. You see 'Receipt recorded'."]),
            ('Upload receipts in bulk (CSV)',
             ["Click 'Bulk Upload CSV'.",
              "Click 'Download Sample Template' to get the correct columns: amount, payment_date, "
              'payment_mode, reference_no, notes, customer_name.',
              'Fill one receipt per row. Date format is YYYY-MM-DD. Mode must be cash, upi, neft, rtgs, '
              'cheque or card.',
              "Drag the file into the box (or click it to browse) and click 'Upload & Process'.",
              "The 'Upload Results' table shows each row as 'Added' (green) or 'Error' (red) with the "
              'reason.',
              "Click 'Back to Receipts' when done."]),
            ('View receipt history',
             ["'Receipt History' shows Date, Contact, Dealer, Mode, Ref, Amount, Document and By, newest "
              'first.',
              "Use the 'All Dealers' drop-down to see one dealer only. The 'Total' row changes to match.",
              'Click the PDF or picture icon in Document to open the attached file.']),
            ('Complete payment on a Quote',
             ["'List of Quotes Sent' shows the latest 100 quotes with Quote Number, Contact, Date, Amount "
              'and Payment Status.',
              "Click 'Complete Payment', enter the 'UTR Number' (required), optionally add a 'Payment "
              "Snapshot', and confirm. The quote becomes 'Paid'.",
              "Click 'Upload Invoice' to attach the invoice PDF to a quote."])],
  'tips': ["Amount must be more than 0, otherwise you get 'Invalid amount'.",
           'In the CSV, rows with a wrong amount or wrong payment mode are skipped; the other rows are still '
           'added.',
           "In the CSV, a missing or wrong date is saved as today's date - check dates before uploading.",
           'Bulk CSV receipts are not linked to any dealer or contact. The customer_name column is only '
           'shown in the results table, it is not saved.',
           'Receipts cannot be edited or deleted from this page.',
           "'Complete Payment' on a quote only marks the quote Paid; it does not add a line to Receipt "
           'History.']},
 {'id': 82,
  'key': 'partner_payments',
  'section': 'Finance',
  'module': 'Partner Payments',
  'url': '/partner-payments',
  'icon': 'bi-cash-coin',
  'description': 'Finance check of payments for trade partner bids that were marked Won. You open what the '
                 'partner submitted (date, mode, UTR, amount) and confirm the money was received. Used by '
                 'admins and sales managers.',
  'steps': [('Open the page',
             ['Go to Finance -> Partner Payments.',
              "The table 'Marked Won - Payment Verification' lists every won partner bid, newest win first. "
              'The badge shows how many.']),
            ('Read the table',
             ['Columns: Bid Amount, Bid ID, Lot Number (click to open the lot), Account Name, Manager Name, '
              'Payment Status, Download and Action.',
              "'PO Pending' (grey): no PO document has been uploaded for the bid yet.",
              "'Payment Pending' (yellow): PO is there, but the partner has not submitted payment details.",
              "'Payment Verifying' (blue): the partner has submitted payment details and they are waiting "
              'for you.',
              "'Payment Done' (green): finance has verified the payment.",
              "Use the 'Download' buttons to open the bid documents (for example the PO)."]),
            ('Verify a payment',
             ["Click 'Verify Payment' on the row.",
              'The window shows Payment Date, Payment Mode, UTR / Reference, Amount and Submitted By.',
              'Match these with your bank statement.',
              "Optional: type a 'Verification Note', e.g. 'matched against bank statement'.",
              "Click 'Confirm Payment Received'. You see 'Payment verified' and the status becomes 'Payment "
              "Done' on every page that shows this bid."])],
  'tips': ['If the partner has not sent payment details yet, the window says so and the confirm button stays '
           'disabled.',
           "Once a payment is verified, 'Verify Payment' is disabled for that row. Verification cannot be "
           'undone from this page.',
           'Account Name shows a dash when the dealer has no CRM account linked.',
           'By default only Admin and Sales Manager can open this page.']},
 {'id': 83,
  'key': 'terms_conditions',
  'section': 'Finance',
  'module': 'Terms & Conditions',
  'url': '/admin/terms-conditions',
  'icon': 'bi-file-text',
  'description': 'Admin list of reusable policy texts - Payment Terms, Delivery Terms and Disclaimer - that '
                 'are printed on generated Purchase Order and Quote documents.',
  'steps': [('Open the page',
             ['Go to Finance -> Terms & Conditions.',
              'This page is for Admin only (unless an admin has granted it to your role).',
              "You see three cards: 'Payment Terms', 'Delivery Terms' and 'Disclaimer'. Each shows Title, "
              'Content (first 120 characters), Status and Actions.']),
            ('Add a policy',
             ["Click 'Add Policy'.",
              "Choose the 'Type', enter a 'Title' and the full 'Content'. All three are required.",
              "Click 'Save Policy'. It is added as 'Active'."]),
            ('Edit a policy',
             ['Click the pencil icon on the row.',
              "Change the 'Title' or 'Content' and click 'Update'.",
              'The type cannot be changed. To move a policy to another type, add a new one and delete the '
              'old one.']),
            ('Turn a policy on or off',
             ["Click the toggle icon to switch between 'Active' and 'Inactive'.",
              'Only Active policies can be chosen when you generate a PO or Quote document.']),
            ('Delete a policy',
             ["Click the red bin icon and confirm 'Delete this policy?'.",
              'The policy is removed for good. Use the toggle instead if you may need it later.'])],
  'tips': ['PO and Quote documents print the policy text as it is when the document is generated or '
           'downloaded, so check the wording before sending a document.',
           'If you do not pick a specific policy when generating a document, all Active policies of that '
           'type are printed.',
           "Keep titles short and clear (e.g. 'Standard 30-day payment') so they are easy to pick from the "
           'list.']},
 {'id': 84,
  'key': 'cost_config',
  'section': 'Finance',
  'module': 'Cost Config',
  'url': '/admin/cost-config',
  'icon': 'bi-calculator',
  'description': 'Set the standard rates the system uses for costs and pricing: labour rate, other rates, '
                 'GST rates, default paid repair cost, and Ready to Sale margins. Admin only by default.',
  'steps': [('Open the page',
             ['Go to Finance -> Cost Config.',
              "Admin can always open it. Other roles need the 'Cost Config' permission, and 'edit' "
              'permission to save.',
              "Under each field you see a short hint and 'Last updated by' with the date."]),
            ('Set the cost rates',
             ["'Labour Rate per Repair Attempt (Rs)': used when an engineer leaves the cost blank. Business "
              'P&L also charges this amount once per sold tag that went through repair, Stress Test, '
              'Cosmetic or Final QC.',
              "'Other Rates per Device (Rs)': added per device that went through cleaning, rework or other "
              'stages.',
              "'Default Paid Repair Cost (Rs)': shown on Process Return when the device is Out of "
              'Warranty.']),
            ('Set the GST rates',
             ["'GST Rate - Intra-State (% total)': enter the total, e.g. 18. The system splits it equally "
              'into CGST and SGST.',
              "'GST Rate - Inter-State IGST (%)': the IGST rate for out-of-state sales, usually 18.",
              'GST values must be between 0 and 100.']),
            ('Set Ready to Sale margins',
             ["'Margin for Individual Item' is added to each Ready to Sale tag's Min Selling Price.",
              "'Margin for As-Is Lot' is added to each As-Is Lot's Min Selling Price.",
              "For each margin choose 'Amount (Rs)' or 'Percentage (%)', then enter the value. Use 0 for no "
              'margin.',
              "A percentage is worked out on the tag's unit price (or the lot price for As-Is)."]),
            ('Save',
             ["Click 'Save Rates'. You see 'Rates saved'.",
              "New rates apply immediately to new calculations. Click 'Cancel' to leave without saving."])],
  'tips': ['All five rate fields are required; the form will not submit with a blank field.',
           'A margin is either an Amount or a Percentage, never both. Switching the type clears the other '
           'one.',
           'Changing the Labour Rate changes the Labour Cost of every month on Business P&L (unless that '
           'month has a manual override).',
           'Every save is written to the audit log with your name.']},
 {'id': 85,
  'key': 'report_bizpl',
  'section': 'Reports',
  'module': 'Business P&L',
  'url': '/reports/business-pl',
  'icon': 'bi-graph-up-arrow',
  'description': 'Company profit and loss for a Financial Year (April to March), month by month: revenue, '
                 'device, parts and labour cost, and gross profit. For management (Admin, Inventory Manager, '
                 'Sales Manager).',
  'steps': [('Open the page and pick a year',
             ['Go to Reports -> Business P&L.',
              "It opens on the current Financial Year, e.g. 'FY 2026-27' (April 2026 to March 2027).",
              "Click another year button at the top (e.g. '2025-26') to see that year."]),
            ('Read the KPI tiles',
             ["'Total Revenue', 'Gross Profit' and 'Gross Margin' for the year. Profit and margin turn red "
              'when low or negative.',
              "'Devices Sold' and 'Avg Sale Price' for the year.",
              "'Inventory @ Cost': Device Price of all tags not yet sold or scrapped (today, not for the "
              'year).',
              "'Parts Consumed': total Parts Cost for the year.",
              "'Lots Sold': lots where every active device is sold, scrapped or replaced."]),
            ('Read the chart and Monthly Breakdown',
             ['The chart shows Revenue, Total Cost and Gross Profit for each month.',
              'The table has one row per month, April first: Revenue, Device Cost, Parts Cost, Labour Cost, '
              'Total Cost, Gross Profit and Margin %.',
              'Revenue = sale price of tags sold that month. Device Cost = Device Price of those tags.',
              'Parts Cost = parts verified as changed on those tags (qty x unit price). Labour Cost = Labour '
              'Rate from Cost Config x sold tags that went through repair, Stress Test, Cosmetic or Final '
              'QC.',
              'The last row is the total for the Financial Year.']),
            ('Export the tags of a month',
             ['Click the green download icon before the month name.',
              'A CSV file downloads with every tag sold in that month: Entity, Tag Number, Lot Number, Make '
              'Model, CPU, Gen, RAM, Storage, Grade, Location ID, Zone, Stage, Device Price and Sale Price.',
              "Use it to check which tags make up that month's Revenue and Device Cost."]),
            ('Correct a month (Admin only)',
             ["Click 'Edit' at the end of the month row. Only Admin sees this column.",
              'The window opens filled with the current Revenue, Device Cost, Parts Cost and Labour Cost.',
              "Change the figure you want to fix and click 'Save'. You see 'Row updated' and the month shows "
              "an 'edited' badge.",
              'To remove a correction, clear that field (leave it blank) and save. The computed value comes '
              'back.',
              'Totals, Gross Profit, the chart and the KPI tiles all use the corrected figures.'])],
  'tips': ['When you save Edit, every field that still has a number is saved as a fixed value - including '
           'ones you did not change. Clear the fields you do not want to fix, otherwise they will not update '
           'with new sales.',
           'A fixed value also stops future sales in that month from changing that figure, so use Edit only '
           'for real corrections.',
           'The month export always lists the real sold tags; it does not reflect manual Edit values.',
           'If your role has pricing visibility turned off, the page shows a lock message instead of '
           'figures.',
           'Every Edit is written to the audit log with the old and new values.']},
 {'id': 86,
  'key': 'reports',
  'section': 'Reports',
  'module': 'Lot P&L',
  'url': '/reports/lot-pl',
  'icon': 'bi-graph-up',
  'description': 'Profit and loss for each purchased Lot: what it cost, what it sold for, returns, and '
                 'profit on the whole lot and on the units sold so far. For management (Admin, Inventory '
                 'Manager, Sales Manager).',
  'steps': [('Open the page',
             ['Go to Reports -> Lot P&L.',
              'One row per Lot. Trashed lots and trashed devices are left out.',
              "Rows are sorted by 'Last Updated' (newest activity first). Green row = profit, red row = "
              'loss.']),
            ('Read the columns',
             ["'Qty' is the lot quantity, 'Devices' the tags in the system, 'Sold' the tags sold.",
              "'Buying Price' + 'Parts Cost' + 'Labour Cost' = 'Total Cost'.",
              "'Gross Sales' minus 'Returns' (customer refunds) = 'Net Revenue'.",
              "'Profit' = Net Revenue - Total Cost for the whole lot. 'Margin' is Profit as a % of Net "
              'Revenue.',
              "'Cost of Sold Units' = buying, parts and labour of only the sold tags. 'Profit on Sold Units' "
              '= Net Revenue - Cost of Sold Units.',
              "The dark 'TOTAL' row at the bottom adds up all lots."]),
            ('Search, sort and open a lot',
             ['Type in the search box to find a Lot number or supplier.',
              'Click a column header to sort.',
              'Click the Lot number to open the Lot page.']),
            ('Export',
             ['Click the green download icon before a Lot number to download all tags of that lot (not '
              'trashed), with Device Price and the latest Sale Price for sold tags.',
              'The tag file has Entity, Tag Number, Lot Number, Make Model, CPU, Gen, RAM, Storage, Grade, '
              'Location ID, Zone, Stage, Device Price and Sale Price.',
              "Click 'Export CSV' at the top for a one-row-per-lot summary of all lots."])],
  'tips': ["For a lot that is only partly sold, look at 'Profit on Sold Units' - whole-lot 'Profit' will "
           'look negative until most units are sold.',
           "When a sold tag has no Device Price, its buying cost is taken as the lot's Buying Price divided "
           'by its number of devices.',
           "The top 'Export CSV' file is a simpler summary: its Revenue is before returns and it does not "
           'hide trashed lots, so its numbers can differ from the screen.',
           'If your role has pricing visibility turned off, the page shows a lock message instead of '
           'figures.']},
 {'id': 87,
  'key': 'report_sales',
  'section': 'Reports',
  'module': 'Sales Report',
  'url': '/reports/sales',
  'icon': 'bi-bar-chart',
  'description': 'List of all sales in a date range, with tag, lot, grade, price, customer and seller. Used '
                 'by management, sales and inventory staff.',
  'steps': [('Open the page',
             ['Go to Reports -> Sales Report.',
              'By default it shows sales of the last 90 days, newest first.',
              "The header shows the 'Total' sale value for the range."]),
            ('Filter by date',
             ['Pick the from date and to date (Sale Date) at the top.',
              "Click 'Filter'. Click 'Reset' to go back to the last 90 days."]),
            ('Read the table',
             ['Columns: Sale#, Date, Tag Number, Brand/Model, Lot, Grade, Price, Customer, Payment and By.',
              'Use the search box to find a Tag Number, customer or lot, and click a header to sort.',
              "Yellow rows marked 'No Sale Record' are tags moved to Sold by a bulk stage change without a "
              'real sale. They have no price or customer and are not in the Total.']),
            ('Export',
             ["Click 'Export CSV' to download the sales file.",
              'The file also has Invoice Number, Phone, Sales Person, Category, Stage and Remarks.'])],
  'tips': ["'Export CSV' does not follow the date filter - it gives all sales (newest first), up to 5,000 "
           'rows. Filter the file in Excel if you need a date range.',
           'Price is the sale price; customer returns are not subtracted here.',
           "Fix 'No Sale Record' tags by checking them with your admin; they should normally be sold through "
           'the sales page.',
           'If your role has pricing visibility turned off, the page shows a lock message instead of the '
           'list.']},
 {'id': 88,
  'key': 'report_daily_stock',
  'section': 'Reports',
  'module': 'Daily Stock',
  'url': '/reports/daily-stock',
  'icon': 'bi-box-seam',
  'description': 'Stock count per stage at the start (Opening) and end (Closing) of any day, with exports '
                 'and the automatic 9 PM daily reports. Used by inventory and management staff.',
  'steps': [('Open the page and pick a day',
             ['Go to Reports -> Daily Stock. It opens on today.',
              "Pick a 'Date' and click 'View'. Click 'Today' to go back.",
              "Optional: choose one or more 'Entity' values to count only those tags.",
              "Optional: tick 'Exclude Admin User' to ignore stage moves done by admin and test users."]),
            ('Read the stock table',
             ["One row per stage: 'Opening Stock' (at 00:00 that day), 'Closing Stock' and 'Net Change'. "
              'Stages with no stock are hidden.',
              "The line above the table shows: Opening - Sold on that day = Closing. 'Other' appears if "
              'something else changed (e.g. a tag trashed that day).',
              "Closing does not add tags received during the day; it is Opening minus that day's sales.",
              "For a past date you see 'Historical - reconstructed from the Stage Movement log'."]),
            ('Export the selected day',
             ["'Overall Export': the table as shown (Stage, Opening, Closing, Net Change).",
              "'Tag Based Export': one row per tag for Opening and Closing, with Tag Number, Category, Make, "
              'Model, CPU, RAM, Storage, Grade and Entity.',
              "'Location Export': the counts split by Stage and Zone.",
              "Exports use the Date, Entity and 'Exclude Admin User' you have selected."]),
            ('Download saved Daily Reports',
             ["The 'Daily Report' table lists saved reports, newest date first.",
              'They are made automatically every day at 9:00 PM.',
              "Click 'Overall', 'Tag Based' or 'Location Based' to download that day's file."])],
  'tips': ['Sold tags, GRN Receipt rows, deactivated and trashed tags are never counted as stock.',
           'A sale back-dated to an earlier day is counted on its Sale Date, not the day it was entered.',
           'The 9 PM saved reports always include all entities and admin moves. Sales entered after 9 PM for '
           'that day are not in that file - use the live export for the latest numbers.']},
 {'id': 89,
  'key': 'report_stage',
  'section': 'Reports',
  'module': 'Stage Log',
  'url': '/reports/stage-movement',
  'icon': 'bi-shuffle',
  'description': 'Read-only log of every stage change of every tag: when, from which stage, to which stage, '
                 "by whom, with notes. Used to trace a device's history.",
  'steps': [('Open the page',
             ['Go to Reports -> Stage Log.', 'The newest movements are shown first, 12 per page.']),
            ('Read the log',
             ['Columns: Date/Time, Tag Number, Device (brand and model), From, To, By and Notes.',
              "'From' shows 'New' for the first entry of a tag.",
              'Stages are shown with their system names (for example iqc, l1, qc_check, ready_to_sale, '
              'sold).']),
            ("Find a tag's history",
             ['Type the Tag Number in the search box. All moves of that tag are listed.',
              'You can also search by user name, stage or note text (e.g. a sale number like SALE-1234).',
              'Click a column header to sort, and use the page buttons at the bottom to move through the '
              'list.'])],
  'tips': ['This page only shows history; you cannot change stages here.',
           'The page loads the full history of all tags, so it can be slow to open. Wait for it to finish '
           'before searching.']},
 {'id': 90,
  'key': 'admin_users',
  'section': 'Admin',
  'module': 'Users',
  'url': '/admin/users',
  'icon': 'bi-people',
  'description': 'Create and manage staff login accounts: role, designation, reporting manager, password and '
                 'active status. Also shows the database backup list. Admin only by default.',
  'steps': [('Open the page and read the list',
             ['Go to Admin Settings -> Users.',
              "The badge 'Today Login' shows how many users logged in today.",
              'The table shows Full Name, Role, Designation, Reports To, Last Login, Username, Created By, '
              'Status and Actions.',
              "By default only active users are listed. Tick 'Show Inactive' to also see deactivated users.",
              "Use 'Role' and 'Last Login' (a date), then click 'Filter'. Click 'Clear' to remove filters.",
              "Click 'Login Log' to see the full login history of all users."]),
            ('Add a new user',
             ["Click 'Add User'.",
              "Fill 'Username' (unique, no spaces), 'Full Name', 'Role' and 'Password' (minimum 6 "
              'characters).',
              "Optional: 'Email Address', 'WhatsApp Phone Number' (country code, no +), 'Designation' and "
              "'Report To'.",
              "Click 'Create User'. The user can log in at once with this username and password.",
              "If the username is already used you see 'Username already exists'."]),
            ('Edit a user',
             ["Click 'Edit' on the user's row.",
              "Change name, username, role, contact details, designation or 'Report To'.",
              "Use the 'Account Active' switch to turn the login on or off.",
              "Click 'Update User'. A changed username must not be taken by another user."]),
            ('Reset a password',
             ["Click 'Reset Pwd' on the user's row.",
              "Type the new password (minimum 6 characters) and click 'Reset'.",
              'Tell the user the new password. The old password stops working at once.']),
            ('Delete (deactivate) a user',
             ["Click 'Delete' on the row and confirm.",
              'The account is only deactivated, not erased. Login history and audit records stay.',
              "To bring the user back: tick 'Show Inactive', click 'Edit', switch on 'Account Active' and "
              'save.',
              'You cannot delete your own account.']),
            ('Database Backup',
             ["The 'Database Backup' card at the bottom lists every backup file with Taken At, Age, Size and "
              'File.',
              "Click 'Run Backup Now' to make a new backup. Each click creates a new file; old files are "
              'kept.',
              'Use the download button on a row to save that backup to your computer.',
              'Use the delete button to remove a backup file. This cannot be undone.',
              'An age shown in red means the latest backup is more than 25 hours old.'])],
  'tips': ['The role decides what the user sees. Custom roles made in Master Data -> Module Permissions also '
           "appear in the 'Role' list.",
           "Use 'Report To' to build the reporting chain; it is shown in the 'Reports To' column.",
           'Deactivate staff who leave instead of sharing their login with someone else.',
           "The 'Role Access Guide' under the user form gives a short summary of each built-in role.",
           'Every create, update and deactivate is written to the audit log.']},
 {'id': 91,
  'key': 'admin_master',
  'section': 'Admin',
  'module': 'Master Data',
  'url': '/admin/master',
  'icon': 'bi-database-gear',
  'description': 'Central admin page for all dropdown lists and for role access: Module Permissions, Role '
                 'Additional Permissions, Pricing Visibility and Global Visibility. Admin only by default.',
  'steps': [('Open the page and pick a tab',
             ['Go to Admin Settings -> Master Data.',
              "Tabs at the top: 'Dropdown Configuration', 'Module Permissions', 'Role Additional "
              "Permissions', 'Pricing Visibility' and 'Global Visibility'.",
              'Green and red messages at the top confirm a save or show an error.']),
            ('Dropdown Configuration - manage list values',
             ["Lists are grouped in sections such as 'Device & Laptop', 'Repair & QC', 'Inventory & "
              "Logistics', 'Sales & Returns', 'Telecalling', 'Dealer Management' and 'CRM'.",
              "Type in 'Search across all dropdowns' to find a list or a value quickly.",
              "Click 'Add Value' (top) or the + button on a list card. Choose 'Category', type 'Value' and "
              "optional 'Description', then save.",
              'Use the pencil button to edit a value, the eye button to disable or enable it, and the bin '
              'button to delete it.',
              'A disabled value is shown crossed out and no longer appears in dropdowns on other pages.',
              "Adding a value that already exists in the same list gives the error 'Value already exists'."]),
            ('Dropdown Configuration - export and bulk import',
             ['Click the download button on a list card to export that list as CSV.',
              "Click the upload button on a list card to open 'Bulk Import'.",
              "Click 'Download Template' to get a sample file. The CSV must have a 'value' column; "
              "'description' is optional.",
              "Choose the file and click 'Import'. Values that already exist are skipped.",
              'The message shows how many values were added and how many were skipped.']),
            ('Module Permissions - control what each role sees',
             ["Select a role in 'Select Role'. The 'Permission Matrix' lists every module with an 'Enable' "
              'tick box.',
              'Ticked = the module is visible and usable for that role. Unticked = hidden and blocked.',
              "Use 'Grant All' or 'Revoke All' to tick or untick everything, then fix single rows.",
              "Click 'Save Permissions for <role>'. The change works at once, no restart needed.",
              'Some modules start OFF for every role until you tick them: Product Master, Hardware, Spare '
              'Parts, Product Pricing and Scrap Sale Report.',
              'Admin pages (Users, Master Data, Move Device Internal, Sidebar Config, Module Page Titles, '
              'Group Config and others) appear for a non-admin role only when ticked here.',
              'The Admin role always has full access; the matrix does not apply to it.']),
            ('Add or rename a custom role',
             ["In 'Add New Role' type 'Role Name' (lowercase letters, numbers and underscores, e.g. "
              "warehouse_manager) and 'Display Name'.",
              "Click 'Create Role'. The new role then appears in the role lists here and on the Users page.",
              'A name that already exists (built-in or custom) is refused.',
              "To rename, click the pencil next to the role under 'Custom Roles', change the names and click "
              "'Save Changes'.",
              'Renaming the role name also moves all users and permissions of that role to the new name. '
              'Built-in roles cannot be renamed.']),
            ('Additional, Pricing and Global Visibility tabs',
             ["'Role Additional Permissions': select a role and tick 'File Upload', 'File Download', 'File "
              "Export', 'Print Page', 'Add Lot', 'Add IQC', 'Add GRN', 'Devices Edit', 'IQC Edit', 'Lot "
              "Edit', 'GRN Edit'. Then click 'Save Additional Permissions for <role>'.",
              "'Pricing Visibility': for each role tick 'Pricing Visible' (prices, cost and sale values "
              "shown everywhere) and 'Set Price' (Set price and Bulk Set price buttons on Ready to Sale). "
              "Click 'Save Pricing Visibility'.",
              "'Global Visibility': switch a module to 'Hidden' to remove it from the sidebar for every "
              'user, including admin.',
              'Global Visibility switches save immediately. Master Data itself cannot be hidden.'])],
  'tips': ['Delete a dropdown value only if it was a mistake. To retire a value that was already used, '
           'disable it with the eye button instead.',
           'A role that was never configured gets all normal modules except the default-off ones. Save the '
           'matrix once to control it exactly.',
           'For a new role, tick only what the person needs, then test by logging in as that user.',
           'Additional permissions and pricing are ON by default for a role until you untick them and save.',
           'Global Visibility is stronger than Module Permissions: a hidden module is hidden from everyone. '
           "Switch it back to 'Visible' to restore it.",
           'Sidebar names shown in the matrix follow any rename done in Sidebar Config.']},
 {'id': 92,
  'key': 'move_device_internal',
  'section': 'Admin',
  'module': 'Move Device Internal',
  'url': '/repair/move/form',
  'icon': 'bi-arrow-left-right',
  'description': 'Manually move one device (by Tag Number) to another workflow stage, for corrections and '
                 'special cases. Admin only by default; other roles only if enabled in Module Permissions.',
  'steps': [('Find the device',
             ['Go to Admin Settings -> Move Device Internal.',
              "Scan or type the Tag Number in 'Device Tag Number'.",
              "When you leave the field, the page shows 'Found' with brand, model and the current stage, or "
              "'Device not found'."]),
            ('Choose the new stage and move',
             ["Select the target stage in 'Move To Stage'.",
              "Type the reason in 'Notes'. If empty, the system records 'Manual move'.",
              "Click 'Move Device'.",
              "The page reloads with the message 'Device moved to <stage>'."]),
            ('What happens after a move',
             ["The device's current stage changes at once and it appears on the queue of the new stage.",
              "The move is saved in the device's stage history with your name and notes, and in the audit "
              'log.',
              'If the device leaves L3, its open L3/L4 work order is closed automatically.',
              'If the device is moved into Final QC Pass Hold or Ready to Sale, verified RAM and Hard Drive '
              'changes are written to the device specification.'])],
  'tips': ['Admin can move a device to any stage. Other users can only make moves that are allowed in Stage '
           "Control; otherwise the move is refused with a 'not permitted' message.",
           'L2 and Sold are not in the stage list. Sell devices from the sales pages, not from here.',
           "Always write a clear reason in 'Notes' - it is the only explanation others will see in the "
           'history.',
           'Prefer the normal stage pages (IQC, repair, QC) for daily work; use this page only for '
           'corrections.']},
 {'id': 93,
  'key': 'sidebar_config',
  'section': 'Admin',
  'module': 'Sidebar Config',
  'url': '/admin/sidebar-config/',
  'icon': 'bi-list-ul',
  'description': 'Rename the menu items shown in the left sidebar. The new name is also used in the Module '
                 'Permissions tab of Master Data. Admin only by default.',
  'steps': [('Open the list',
             ['Go to Admin Settings -> Sidebar Config.',
              "The 'Sidebar Items' table shows 'Module Key', 'Sidebar Label' and 'Actions'.",
              "A blue 'Custom' badge means the label was renamed from its default.",
              'Use the table search box and pages to find an item.']),
            ('Rename a menu item',
             ["Click 'Edit' on the row.",
              'Type the new label in the text box.',
              "Click 'Save' to keep it, or 'Cancel' to go back.",
              'The new name shows in the sidebar for all users right away.']),
            ('Go back to the default name',
             ["On a row with the 'Custom' badge, click the reset (round arrow) button.",
              'The label returns to its original default name.'])],
  'tips': ['Renaming only changes the text. It does not change who can see the page - use Master Data -> '
           'Module Permissions for that.',
           'The label cannot be empty; saving an empty label gives an error.',
           'Keep names short so they fit in the sidebar.',
           'To change the title shown at the top of a page, use Module Page Titles instead.']},
 {'id': 94,
  'key': 'landing_pages',
  'section': 'Admin',
  'module': 'Module Page Titles',
  'url': '/admin/landing-pages',
  'icon': 'bi-layout-text-sidebar',
  'description': 'Set the title shown in the top bar of each module page, and switch the breadcrumb trail on '
                 'or off per page. Admin only by default.',
  'steps': [('Open the list',
             ['Go to Admin Settings -> Module Page Titles.',
              "The 'Module Landing Page Titles' table shows 'Navigation Label', 'Page Title', 'Breadcrumb' "
              "and 'Actions'.",
              'Click the small link icon next to a navigation label to open that page in a new tab.']),
            ('Change a page title',
             ["Click 'Edit' on the row.",
              'Type the new title.',
              "Click 'Save' to keep it, or 'Cancel' to go back.",
              'The new title appears in the top bar of that page for all users.']),
            ('Reset a title',
             ['Click the reset (round arrow) button on a row that has a custom title.',
              'The page goes back to its default title.']),
            ('Breadcrumb on or off',
             ["Use the switch in the 'Breadcrumb' column. 'On' shows a Home > ... trail above the page "
              'content.',
              'The switch saves immediately; no Save button is needed.',
              "Breadcrumbs are 'Off' by default for every page."])],
  'tips': ['The title cannot be empty; saving an empty title gives an error.',
           'This page changes the page heading only. To rename the sidebar menu item, use Sidebar Config.',
           "If a switch does not change, you see 'Could not update breadcrumb setting.' - reload the page "
           'and try again.']},
 {'id': 95,
  'key': 'attendance_config',
  'section': 'Admin',
  'module': 'Group Config',
  'url': '/admin/attendance-config',
  'icon': 'bi-people-fill',
  'description': 'Set the application timezone and create attendance groups, so a manager can see the '
                 "Attendance Report for only their group's members. Admin only by default.",
  'steps': [('Set the Application Timezone',
             ['Go to Admin Settings -> Group Config.',
              "In 'Application Timezone' choose the 'Timezone' (default Asia/Kolkata - IST).",
              "Click 'Save Timezone'. All new timestamps are recorded in this timezone."]),
            ('See existing groups',
             ["The 'Attendance Groups' table shows 'Group Name', 'Manager', 'Members' and 'Action'.",
              'The Members column shows the count and the usernames in the group.']),
            ('Create a group',
             ["Click 'New Group'.",
              "Type 'Group Name' and choose the 'Manager'.",
              "Tick the 'Members' who belong to this group.",
              "Click 'Create Group'.",
              'The manager can now open Attendance Report and see only these members.']),
            ('Edit or delete a group',
             ["Click 'Edit' to change the name, manager or members, then click 'Save Changes'.",
              'Click the bin button and confirm to delete the group.',
              'Delete is permanent; the group name can then be used again.'])],
  'tips': ["Two groups cannot have the same name ('Group name already exists').",
           'Only active users appear in the Manager and Members lists.',
           'Changing the timezone affects new records only; it does not change old timestamps.',
           'Group create, update and delete are recorded in the audit log.']},
 {'id': 96,
  'key': 'qa',
  'section': 'Admin',
  'module': 'QA Dashboard',
  'url': '/qa/',
  'icon': 'bi-speedometer2',
  'description': 'Software quality and user acceptance testing (QA / UAT) area for the ERP itself: '
                 'requirements, test cases, defects, UAT scenarios, releases and specification documents. '
                 'The sidebar link is shown to admin only.',
  'steps': [('Read the dashboard',
             ["Click 'QA Dashboard' in the ADMIN section of the sidebar.",
              "Top cards show 'Requirements', 'Test Cases', 'Pass Rate' and 'Open Defects'.",
              "'UAT Progress' shows Total Scenarios, Passed, Failed, Blocked and Pending.",
              "'Recent Releases' and 'Open Critical & High Defects' list the latest items.",
              "'Recent Changes - Full History' lists every recorded change with Date, Type and Change "
              'Description.',
              "If no test runs are logged yet, 'Pass Rate' is worked out from the change history (bug fixes "
              'count as fails).']),
            ('Download specification documents',
             ["In 'SRS Documents' download the 'Technical Specification', 'Functional Specification' and "
              "'Design System'.",
              "'Module & Interconnectivity Map' can be downloaded as PDF or PowerPoint."]),
            ('Requirements and Test Cases',
             ["Use the tabs at the top: 'Requirements', 'Test Cases', 'Defects', 'UAT Board', 'Releases', "
              "'RTM', 'Workflow', 'Stage Workflows'.",
              "On 'Requirements' click 'New Requirement'; filter by priority and status; change a "
              "requirement's status from its row.",
              "On 'Test Cases' click 'New Test Case' and link it to a requirement.",
              "Click 'Run' on a test case, choose the result and click 'Log Result' to record an "
              'execution.']),
            ('Defects and UAT',
             ["On 'Defects' click 'Log Defect' and fill title, severity, priority, module and steps.",
              "Click 'Update' on a defect to change its status (New, Assigned, In Progress, Fixed, Retest, "
              "Closed, Reopened, Won't Fix) and add root cause or resolution.",
              "On 'UAT Board' click 'New UAT Scenario'. Scenarios sit in columns Pending Execution, Pass, "
              'Fail and Blocked.',
              "Click 'Record Result', choose the status and tick 'Sign off this scenario' for business "
              "approval, then 'Save UAT Result'."]),
            ('Releases and traceability',
             ["On 'Releases' click 'New Release' with version, planned date and rollback plan.",
              "Click 'Advance Status' to move a release through Planned, In QA, QA Done, In UAT, UAT Done, "
              'Approved, Deployed or Rolled Back.',
              'QA Done, UAT Done and Deployed record who signed off and when.',
              "'RTM' shows each requirement with its test cases, latest results and UAT scenarios, and lists "
              'requirements without any test case.'])],
  'tips': ['Any logged-in user can view the QA pages. Adding and updating requirements, test cases, defects '
           'and UAT results is allowed for Admin, Inventory Manager and QC Inspector.',
           'Creating and advancing releases is allowed for Admin and Inventory Manager only.',
           'Link every test case to a requirement, otherwise the requirement shows as uncovered in RTM.',
           'This area tracks testing of the software, not device QC. Device quality checks are done on the '
           'QC pages.']},
 {'id': 97,
  'key': 'stage_control',
  'section': 'Admin',
  'module': 'Stage Control',
  'url': '/stage-control',
  'icon': 'bi-diagram-3',
  'description': 'Define which stage-to-stage moves are allowed for devices. The rules are checked whenever '
                 'a non-admin user moves a device. Admin only by default; other roles if enabled in Module '
                 'Permissions.',
  'steps': [('Open the page',
             ['Go to Application Settings -> Stage Control.',
              "The right side shows 'Allowed Transitions' with the number of rules, each as From Stage -> To "
              'Stage.',
              "Buttons at the top open the 'Aging Dashboard' and the 'Audit Log'."]),
            ('Add an allowed move',
             ["In 'Add Allowed Transition' choose 'From Stage' and 'To Stage'.",
              "Click 'Add'.",
              'The rule appears in the list and takes effect immediately.',
              "If the same rule already exists you see 'Transition already exists'."]),
            ('Remove an allowed move',
             ["Click the bin button on the rule's row.",
              "Confirm 'Remove this transition?'.",
              'From now on, non-admin users cannot make that move.'])],
  'tips': ['Admin can always move a device to any stage; these rules limit other users only.',
           'Removing a rule can block daily work on the stage pages. Check with the team before removing '
           'one.',
           'Every add and remove is recorded in the audit log with your username.',
           'Staying in the same stage is never blocked.']},
 {'id': 98,
  'key': 'aging_tracker',
  'section': 'Admin',
  'module': 'Aging Tracker',
  'url': '/stage-control/aging',
  'icon': 'bi-hourglass-split',
  'description': 'Shows devices that are stuck too long in one stage, old dead stock, and devices whose cost '
                 'is close to their expected sale value. Admin by default; other roles if enabled in Module '
                 'Permissions.',
  'steps': [('Read the summary cards',
             ['Go to Application Settings -> Aging Tracker.',
              "'Stuck Devices' = more than 30 days in the current stage.",
              "'Dead Stock' = more than 90 days in total since the device was created.",
              "'Cost Warnings' = total cost is more than 70% of the expected sale value."]),
            ('Refresh the numbers',
             ["Click 'Refresh Aging' to recalculate all active devices.",
              'The message shows how many devices were updated, how many are stuck and how many are dead '
              'stock.',
              'Sold and scrapped devices are not counted.']),
            ('Work through the lists',
             ["'Stuck Devices' table: Tag Number, Brand/Model, Stage, Days in Stage, Total Days (longest "
              'first).',
              "'Dead Stock' table: Tag Number, Brand/Model, Stage, Total Days.",
              "'Cost Warnings' table: Tag Number, Brand/Model, Total Cost, Expected Sale and Cost %. Red "
              'means cost is 100% or more.',
              'Click a Tag Number to open the device page and take action.'])],
  'tips': ["The lists only change after 'Refresh Aging' is run, so refresh before you review.",
           "'Refresh Aging' is allowed for admin (or roles granted Stage Control access in the matrix).",
           'Cost Warnings only appear for devices that have an expected sale value recorded.',
           'Use this page in weekly reviews to push stuck devices to the next stage or to sale.']},
 {'id': 99,
  'key': 'stage_audit_log',
  'section': 'Admin',
  'module': 'Stage Audit Log',
  'url': '/stage-control/audit',
  'icon': 'bi-journal-text',
  'description': 'A read-only list of every action recorded in the system audit trail, newest first. Admins '
                 'use it to quickly check who did what and when.',
  'steps': [('Open the page',
             ['Go to ADMIN -> Application Settings -> Stage Audit Log.',
              "The page title reads 'Audit Log - Last 500 Actions'.",
              "Click 'Back' at the top right to return to the Stage Control page."]),
            ('Read the table',
             ["'Timestamp' shows the date and time of the action (day, month, hour:minute:second).",
              "'User' shows the username who did the action.",
              "'Action' shows the action name as a coloured badge.",
              "'Table' shows which part of the system was changed.",
              "'Record' shows the first 8 characters of the changed record's ID.",
              "'Notes' shows the first 80 characters of the note saved with the action."]),
            ('Understand the badge colours',
             ['Red badge: a scrap action (the action name contains SCRAP).',
              'Yellow badge: a failed check (the action name contains FAIL).',
              'Green badge: a pass or a sale (the action name contains PASS or SALE).',
              'Blue badge: all other actions, for example stage transitions added or removed.'])],
  'tips': ['This page is admin-only by default. An admin can grant it to other roles in the module '
           'permissions.',
           'The page is read-only. You cannot edit or delete log entries.',
           'There is no filter or search here. To filter by user, action or table, use ADMIN -> Application '
           'Settings -> System Audit Log.',
           "Despite the 'Last 500 Actions' title, the list shows all entries, so it can be long. Use the "
           "browser's Find (Ctrl+F) to locate a Tag Number or user."]},
 {'id': 100,
  'key': 'system_audit_log',
  'section': 'Admin',
  'module': 'System Audit Log',
  'url': '/admin/audit-log',
  'icon': 'bi-shield-check',
  'description': 'A searchable record of every important action in the system - orders, payments, stage '
                 'moves, scrap, settings changes - with user, time and IP address. Used by admins to '
                 'investigate changes.',
  'steps': [('Open the page',
             ['Go to ADMIN -> Application Settings -> System Audit Log.',
              "The count above the table shows how many log entries match your filters, for example '1250 "
              "log entries'.",
              'Entries are listed newest first.']),
            ('Filter the log',
             ["'Username': type part of a username to show only that user's actions.",
              "'Action': type part of an action name, for example ORDER_CREATED, PAYMENT_RECORDED or "
              'STAGE_MOVED.',
              "'Table': choose one area of the system from the list, or leave 'All Tables'.",
              "Click 'Filter' to apply. Click 'Clear' to remove all filters."]),
            ('Read the columns',
             ["'Timestamp': date and time in day-month-year hour:minute:second.",
              "'User' and 'Action': who did it and what was done. Colours: green = order created, blue = "
              'payment recorded, yellow = credit note issued, red = scrap.',
              "'Table' and 'Record ID': the area changed and the first 12 characters of the record ID.",
              "'Notes / Summary': the saved note, or the new value if there is no note.",
              "'IP': the computer address the action came from."]),
            ('Search, sort and page',
             ["Use the 'Search' box at the top right to search inside the loaded rows.",
              'Click a column header to sort by that column.',
              "Use 'Show entries' at the top left to show more rows per page (default 12).",
              'Use the page buttons at the bottom right to move between pages.'])],
  'tips': ['This page is admin-only by default. An admin can grant it to other roles in the module '
           'permissions.',
           'The log is read-only. Nobody can edit or delete entries from this page.',
           "Use the 'Username' and 'Action' filters first, then the 'Search' box, to find an entry quickly "
           'in a large log.',
           'Deleted records still have their audit entry here, so this is the place to check who removed '
           'something.']},
 {'id': 101,
  'key': 'report_aging',
  'section': 'Admin',
  'module': 'Stock Aging',
  'url': '/reports/stock-aging',
  'icon': 'bi-hourglass-split',
  'description': 'Shows how long devices have been in stock, grouped by stage and age bracket, with their '
                 'buying cost. Managers use it to spot old stock that needs to be pushed through repair or '
                 'sold.',
  'steps': [('Open the page and read the summary cards',
             ['Go to ADMIN -> Application Settings -> Stock Aging.',
              "'Devices in Stock': all devices that are not Sold, Scrapped or Returned.",
              "'Total Cost @ Buying Price': the total buying price of those devices in Rs.",
              "'Aged 61+ Days' (orange) and 'Aged 90+ Days' (red): how many devices are that old. They turn "
              'green when the count is zero.']),
            ("Use the 'Aging by Stage' table",
             ['Each row is a device stage, for example IQC, L1 Repair or Ready to Sale.',
              'Columns are age brackets: 0-7 days, 8-30 days, 31-60 days, 61-90 days and 90+ days.',
              'Each cell shows the device count and, below it, the buying cost in Rs.',
              'Counts in the 61-90 and 90+ columns are shown in red.',
              "'Total' and 'Cost' on the right give the totals for each stage. The bottom row gives totals "
              'for each bracket.']),
            ("Check the '50 Oldest Devices in Stock' list",
             ['This list shows the 50 oldest devices, oldest first.',
              "Columns: 'Tag Number', 'Brand / Model', 'Stage', 'Grade', 'Age (Days)' and 'Cost'.",
              'Rows older than 60 days are shaded yellow. Rows older than 90 days are shaded red.',
              "Click a Tag Number to open that device's detail page and take action."])],
  'tips': ['Age is counted from the day the device was first entered in the system, not from the day it '
           'entered its current stage. For time in the current stage, use Overdue Devices.',
           "Cost uses the device's buying price only. Parts and labour are not included.",
           'The page is read-only and has no filter or export.',
           'Who can open this page depends on the permissions the admin has given your role.']},
 {'id': 102,
  'key': 'report_overdue',
  'section': 'Admin',
  'module': 'Overdue Devices',
  'url': '/reports/overdue',
  'icon': 'bi-exclamation-triangle',
  'description': 'Lists devices that have not been touched for a set number of days, so managers can find '
                 'devices stuck in a stage. Sold and Scrapped devices are not included.',
  'steps': [('Open the page',
             ['Go to ADMIN -> Application Settings -> Overdue Devices.',
              'By default the page shows devices in all active stages that have not been updated for 3 days '
              'or more.',
              "The line above the table shows how many devices match, for example 'Showing 42 device(s) "
              "waiting >= 3 day(s)'."]),
            ('Filter the list',
             ["'Filter by Stage': choose one stage, or leave 'All Active Stages'.",
              "'Min Days in Stage': enter the minimum number of days a device must be waiting (0 or more).",
              "Click 'Filter' to apply. Click 'Clear' to go back to the default view."]),
            ('Read the table',
             ['Devices are listed with the longest waiting first.',
              "Columns: '#', 'Tag Number', 'Brand / Model', 'Stage', 'Days Waiting', 'Lot' and 'Last "
              "Updated'.",
              "'Days Waiting' is orange above 7 days and red above 14 days.",
              'Click a Tag Number to open the device page and move it forward.']),
            ('Export the list',
             ['Set your filters first.',
              "Click 'Export CSV'. The file uses the same stage and days filters you see on screen.",
              'The file is named overdue devices and opens in Excel.'])],
  'tips': ["'Days Waiting' is counted from the last time the device record was changed. Any update to the "
           'device resets it, not only a stage move.',
           "If no device matches, you see the green message 'No overdue devices found with current filters.'",
           'The CSV export is limited to 5,000 rows. If the last row says TRUNCATED, narrow your filters and '
           'export again.',
           'Who can open this page depends on the permissions the admin has given your role.']},
 {'id': 103,
  'key': 'report_receivables',
  'section': 'Admin',
  'module': 'Receivables',
  'url': '/reports/receivables',
  'icon': 'bi-cash-coin',
  'description': 'Shows money still owed by dealers on their orders, grouped by how many days past the '
                 'payment due date it is. Used by sales managers, inventory managers and accounts to follow '
                 'up on payments.',
  'steps': [('Open the page',
             ['Go to ADMIN -> Application Settings -> Receivables.',
              "The heading 'Receivables Ageing' shows the date and time the figures were calculated ('As "
              "of').",
              'Only dealer orders with an amount still due and status Pending, Confirmed or Delivered are '
              'included.']),
            ('Read the summary cards',
             ["'Current (not overdue)': amount not yet past its due date, or orders with no due date.",
              "'1-30 days', '31-60 days', '61-90 days' and '90+ days': amounts that many days past the due "
              'date.',
              "'Grand Total': total amount due from all dealers, in Rs."]),
            ('Use the detail table',
             ["Each row is one dealer order: 'Dealer', 'Order #', 'Order Date', 'Due Date'.",
              "The due amount appears in one column only - '0-30', '31-60', '61-90', '90+' or 'Current' - "
              'based on how late it is.',
              "'Total Due' shows the full amount still owed on that order.",
              "The bottom 'Totals' row adds up each column.",
              "Click the dealer name to open the dealer profile. Click the 'Order #' to open the invoice in "
              'a new tab.']),
            ('Export',
             ["Click 'Export CSV' at the top right.",
              'The file lists Dealer, Order #, Order Date, Due Date, Bucket and Due Amount for every row.'])],
  'tips': ["Orders with no payment due date are always counted as 'Current', even if they are old. Set a due "
           'date on dealer orders so ageing is correct.',
           "The '0-30' column in the table is the same bucket as the '1-30 days' card.",
           "If every order is paid, you see 'No outstanding receivables - all dealer orders are settled.'",
           'This page is read-only. Record payments from the dealer order, not here.',
           'Access is limited: admin, sales manager and inventory manager by default, or roles the admin has '
           'granted it to.']},
 {'id': 104,
  'key': 'market',
  'section': 'Admin',
  'module': 'Market Intel',
  'url': '/market',
  'icon': 'bi-graph-up-arrow',
  'description': 'A shared list of what other dealers in the market are selling or looking to buy, with '
                 'model, spec, quantity and price. Sales and purchase staff use it for price checks and to '
                 'find buyers or stock.',
  'steps': [('Open the page',
             ['Go to ADMIN -> Application Settings -> Market Intel.',
              "'Available to Buy (Sellers)' shows how many active entries are sellers offering stock.",
              "'Dealers Buying' shows how many active entries are dealers who need stock.",
              'Below the filters, the table shows all entries, newest first.']),
            ('Add an entry',
             ["Click 'Add Entry'.",
              "Choose 'Trade Type': 'Selling (I have)' or 'Buying (I need)'.",
              "Fill 'Model' (required), and as much as you know of Category, Brand, Processor, Generation, "
              "RAM, Storage, Condition, Grade, Qty, 'Price / Unit' in Rs and 'Warranty (months)'.",
              "'Price Negotiable' is ticked by default. Untick it if the price is fixed.",
              "Optionally pick the 'Dealer', paste the original message in 'Source Message / Notes', and add "
              "'Internal Notes'.",
              "Click 'Save Entry'. You see 'Entry added'."]),
            ('Search and filter',
             ['Type in the search box to match model, brand, processor, dealer, notes or the source message.',
              "Use 'All Brands', 'All Categories' and 'Buy & Sell' (or 'Selling (Available)' / 'Buying "
              "(Required)') to narrow the list.",
              "The last dropdown shows 'Active' entries only by default. Choose 'All' to include closed "
              'entries.',
              "Click 'Search'. Click the cross button to clear all filters.",
              "You can also use the table's own 'Search' box and click column headers to sort."]),
            ('Read the table',
             ["'Trade' shows a green Sell or blue Buy badge.",
              "Other columns: 'Brand / Model', 'Spec', 'Cond / Grade', 'Qty', 'Price/unit' ('neg.' means "
              "negotiable), 'Wty' in months, 'Dealer / Group' and 'Posted'.",
              "If an entry has a source message, click 'src' to show or hide it."]),
            ('Edit, close or reopen an entry',
             ["Click the pencil button to open the edit form. Change the fields and click 'Save Changes'.",
              "When a deal is done or the offer is gone, click 'Close'. The row turns grey and leaves the "
              "'Active' view.",
              "To bring a closed entry back, choose 'All' in the filter and click 'Reopen' on that row."])],
  'tips': ["'Close' does not delete the entry. It stays in the list when you filter by 'All'.",
           'Keep prices per unit, not per lot, so entries can be compared.',
           "The WhatsApp group features shown on this page (WhatsApp Group field and the 'Link WhatsApp "
           "Groups to Dealers & Categories' table) depend on WhatsApp integration, which is currently "
           'switched off, so the group lists are normally empty.',
           'Who sees this page in the sidebar depends on the permissions the admin has given your role.']},
 {'id': 105,
  'key': 'manuals',
  'section': 'Support',
  'module': 'Manuals',
  'url': '/manuals/',
  'icon': 'bi-book',
  'description': 'The Learning Manual library: one step-by-step guide for every page in the left sidebar, '
                 'grouped by sidebar section. All staff can read, download and share these guides.',
  'steps': [('Open the Manuals page',
             ['Go to SUPPORT -> Manuals in the left sidebar. Every user can open this page.',
              'Manuals are grouped under the same section headings as the sidebar, in the same order.',
              'Each card shows the page name, a short description, and how many steps and tips the manual '
              'has.']),
            ('Find a manual',
             ["Type in 'Search manuals...' at the top, for example a page name or a word like 'scrap' or "
              "'invoice'.",
              "Or pick a section in the 'All Sections' dropdown to see only that part of the menu.",
              "The count next to 'Learning Manuals' shows how many manuals match. Clear the search to see "
              'all again.']),
            ('Read a manual',
             ["Click 'View' on a card. The manual opens in a window with every step and the tips.",
              "Click 'Open page' in that window to go straight to the page the manual explains.",
              "Click 'Download PDF' in that window, or 'Download' on the card, to save the manual as a "
              'PDF.']),
            ('Download every manual at once',
             ["Click 'Download All (PDF)' at the top right.",
              'You get one PDF with a contents page and every manual, grouped by sidebar section - useful '
              'for printing a full training binder.']),
            ('Share a manual with a colleague',
             ["Click 'Share' on the manual you want to send.",
              "In the 'Share Manual' window, pick the person from 'Select Team Member'. Only active users "
              'are listed, and you are not in the list.',
              "Click 'Share'. A green message confirms the manual was shared.",
              "The colleague gets a notification titled 'Learning Manual Shared' with the manual name.",
              "Click 'Cancel' to close the window without sharing."])],
  'tips': ['Share is the quickest way to train a new joiner: send them the manual for each page they will '
           'use.',
           'Sharing only sends a notification. It does not give the colleague access to a page their role '
           'cannot open.',
           'Manuals describe the standard screens. If your role cannot see a button mentioned in a manual, '
           'ask your admin about permissions.',
           "PDFs are rebuilt automatically whenever a manual's text is updated, so a download is always the "
           'latest version.']}]
