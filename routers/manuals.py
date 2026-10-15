"""
Learning Manuals
================
GET  /manuals/              — List all learning manuals
GET  /manuals/{mid}/download — Download PDF for a manual
POST /manuals/{mid}/share    — Share manual with a user (notification)
"""
from __future__ import annotations

import io
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user, verify_csrf
from database import get_db
from models.user import User
from models.notification import Notification
from templates_config import templates

router = APIRouter(prefix="/manuals", tags=["manuals"])

_MANUALS_DIR = Path(__file__).resolve().parent.parent / "static" / "manuals"
try:
    _MANUALS_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


# ── Manual content definitions ────────────────────────────────────────────────

MANUALS = [
    {
        "id": 1,
        "module": "Stock Intake & Lot Management",
        "icon": "bi-inbox-fill",
        "color": "primary",
        "description": "Create and manage sourcing lots, register devices into Stock Inward, and import GRNs from supplier invoices.",
        "steps": [
            ("Create a Lot", [
                "Go to Intake -> Lot Overview from the left navigation.",
                "Click the 'New Lot' button at the top right.",
                "Fill in Lot Number (must be unique -- a live duplicate check warns you immediately).",
                "Enter Source/Supplier name, Purchase Date, Expected Device Count, Buying Price per unit, and any Remarks.",
                "Click 'Create Lot'. The lot now appears in the list with status 'Open'.",
            ]),
            ("Register Devices via Stock Inward", [
                "Go to Intake -> Stock Inwards.",
                "Click 'Add Device' to register one device.",
                "Fill in Tag Number (barcode, must be unique system-wide), select the Lot, Brand, Model, Grade, and Quantity.",
                "Click 'Save'. The device enters the IQC queue immediately.",
                "Repeat for each device, or use CSV import for bulk registration.",
            ]),
            ("Import a GRN with Invoice", [
                "Go to Intake -> GRN with Invoice.",
                "Click 'New GRN' and upload the supplier invoice PDF.",
                "The system auto-parses the invoice to fill GRN Number (format: GRN-YYYYMMDD-XXXX), Invoice Date, Supplier, and Total Value.",
                "Review and correct any fields, then enter Per-Device Cost.",
                "Click 'Save GRN'. The GRN is linked to the lot for cost tracking.",
            ]),
            ("View Lot Detail and Export", [
                "Click any lot row to open its detail view.",
                "Lot detail shows: device count, stage-wise breakdown (IQC/Repair/QC/Ready/Sold), GRN status, and full device list.",
                "Click 'Export CSV' to download all devices in the lot as a spreadsheet.",
                "Close the lot once all devices are sold or scrapped.",
            ]),
        ],
        "tips": [
            "Always create the Lot first before registering devices -- devices must link to a lot.",
            "Lot Numbers must be unique. The live duplicate warning appears as you type.",
            "Tag Numbers (barcodes) must be globally unique across all lots.",
            "Complete GRN mapping before closing a lot to ensure accurate P&L.",
            "The lot status changes from Open to Closed automatically when you close it.",
        ],
    },
    {
        "id": 2,
        "module": "IQC: Incoming Quality Check",
        "icon": "bi-clipboard2-check",
        "color": "info",
        "description": "Inspect incoming devices, capture hardware specs, assess cosmetic condition, and assign grade, floor, and warehouse location.",
        "steps": [
            ("Open the IQC Queue", [
                "Go to IQC from the left navigation.",
                "The queue shows all devices registered in Stock Inward that have not yet been inspected.",
                "Click the 'IQC' button next to the device you want to inspect.",
            ]),
            ("Set Up the OxyQC Diagnose Agent", [
                "On the IQC form, click the blue 'Download Agent' button.",
                "Run Diagnose_Device_Agent.exe on the inspection station. No admin rights needed.",
                "The agent installs to the user's local app folder and persists across reboots.",
                "Once installed, the button turns green -- you only need to do this once per station.",
            ]),
            ("Auto-Detect Hardware with Diagnose", [
                "Plug the laptop in and connect to the same network as the inspection station.",
                "Click 'Diagnose this Device' on the IQC form.",
                "The system fills CPU, Generation, RAM GB, Storage GB, Battery Health %, and Screen Size automatically via WMI.",
                "Alternatively, plug in the OxyQC USB drive and click 'USB Import' to read offline scan results.",
                "Review all auto-filled values and correct any that the agent could not detect.",
            ]),
            ("Fill Hardware Fields", [
                "Device Type: select Laptop, Desktop, or Tablet.",
                "Brand, Model: select or type the manufacturer and model name.",
                "Serial Number: enter the serial number printed on the device (mandatory).",
                "CPU: enter the processor name as it appears in Device Manager.",
                "Generation: enter the CPU generation number (e.g., 8 for 8th Gen Intel).",
                "RAM GB: enter total installed RAM in gigabytes.",
                "Storage GB: enter the primary drive capacity; select Type (SSD/HDD/eMMC/NVMe).",
                "HDD Capacity GB: enter secondary HDD size if a second drive is present.",
                "Battery Health %: enter the battery wear level (100% = new).",
                "Screen Size: enter diagonal screen size in inches.",
                "Color: select the device body colour.",
                "BIOS Password: tick if a BIOS password is set and cannot be removed.",
            ]),
            ("Assess Screen Condition", [
                "Screen Functional: select Yes or No -- this is whether the screen works at all.",
                "Screen Defects: check all applicable defect boxes from 13 options:",
                "  Dot, Line, Discoloration, Patch, Broken, Flickering, Scratch, Loose, Missing,",
                "  Hinge Broken, Colour Spread, Keyboard Mark, Hard Press.",
                "Each checked defect is recorded for downstream QC reference.",
            ]),
            ("Assess Panels A to D", [
                "Panel A (Screen Lid): check Scratch / Broken / Missing / Dent / Colour Fade.",
                "Panel B (Bottom): same as A plus Rubber Cut.",
                "Panel C (Front Bezel): check Scratch / Broken / Missing / Dent / Colour Fade.",
                "Panel D (Keyboard Deck): same as C.",
                "Be precise -- Panel notes appear on the device card throughout the workflow.",
            ]),
            ("Assess Keyboard, Speaker, Touchpad, and Ports", [
                "Keyboard: check Working status; flag Colour Fade / Key Missing / Hard Press if present.",
                "Speaker: select OK / Not Working / Not Checked.",
                "Touchpad: check Working / Click Working; flag Scratch / Colour Fade / Missing.",
                "Ports: select HDMI status, USB Working status, Audio Jack status.",
                "Count the number of USB-A ports, USB-C ports, and Ethernet ports.",
                "Charging Port: select status (Working / Not Working / Missing).",
                "WiFi: select Detected / Not Detected / Not Tested.",
                "Webcam: select Working / Not Working / Not Present.",
                "HDD Connector and Casing: tick if the internal connector or casing has damage.",
            ]),
            ("Assign Grade, Floor, and Warehouse", [
                "Grade: select A (excellent, no significant defects), B (minor cosmetic issues), or C (notable cosmetic defects).",
                "Grade D is not used in this system.",
                "R2V3 Grade Category: select the environmental grade category per R2V3 standard.",
                "Floor: enter the physical floor number where the device will be stored.",
                "Warehouse: select the warehouse bin location.",
                "Click 'Save IQC'. The device moves to the next stage.",
            ]),
        ],
        "tips": [
            "Serial Number is mandatory -- IQC cannot be saved without it.",
            "Grade A = no significant defects; Grade B = minor cosmetic; Grade C = notable cosmetic damage.",
            "Battery Health below 80% should always be flagged in the notes for L1 review.",
            "The Diagnose agent persists per station -- install once and it works for all future inspections.",
            "USB Import is the backup method when the device cannot connect to the station network.",
        ],
    },
    {
        "id": 3,
        "module": "GRN Post IQC",
        "icon": "bi-receipt",
        "color": "warning",
        "description": "Map inspected devices to supplier invoice GRNs so each device carries an accurate per-unit purchase cost for P&L reporting.",
        "steps": [
            ("Open the Post-IQC GRN Queue", [
                "Go to Intake -> GRN Post IQC from the left navigation.",
                "This view lists all devices that have completed IQC but have not yet been mapped to an invoice GRN.",
            ]),
            ("Map a Device to a GRN", [
                "Click 'Map this GRN' next to the device.",
                "A dropdown shows all available GRNs for the same lot.",
                "Select the correct invoice GRN and confirm.",
                "Click 'Save Mapping'. The device is now linked to the per-unit purchase cost from that invoice.",
            ]),
            ("Review GRN Records", [
                "Go to Intake -> GRN Records to view all mapped and unmapped GRN entries.",
                "Records appear 5 per page, most recent first.",
                "Soft-delete a GRN record by clicking 'Delete' -- it is hidden, not erased, preserving the audit trail.",
                "Click 'Export' to download the full GRN list as a CSV.",
            ]),
        ],
        "tips": [
            "GRN mapping links each device to its per-unit purchase cost, which feeds directly into P&L.",
            "Devices without a GRN mapping show 'GRN Pending' in reports and the lot P&L.",
            "Complete all GRN mappings before closing a lot to ensure accurate profitability reporting.",
            "Soft-delete preserves the audit trail -- the record is still in the database but hidden from the list.",
        ],
    },
    {
        "id": 4,
        "module": "L1 Repair",
        "icon": "bi-tools",
        "color": "danger",
        "description": "Handle first-level repairs: component replacement, basic troubleshooting, and parts requests for assigned devices.",
        "steps": [
            ("View the L1 Repair Queue", [
                "Go to Repair -> L1 Repair from the left navigation.",
                "The queue shows all devices assigned to L1 with their Work ID, assigned engineer, days in queue,",
                "and a parts-alert badge if parts are pending.",
            ]),
            ("Start a Repair", [
                "Click 'Start' next to the device.",
                "Fill in Issue Description (what you observe), Problem Reported (what the inspector noted),",
                "Team Name, and Assigned Engineer (optional).",
                "Click 'Start Repair'. Status changes to In Progress and a Work ID is generated.",
            ]),
            ("Request Parts", [
                "On the device repair card, click 'New Parts Request'.",
                "Select the part category: RAM / HDD / SSD / Battery / Screen / Keyboard / Charger / Motherboard / Cable / Other.",
                "Enter the part name, quantity needed, and any notes.",
                "The spare parts manager receives the request and fulfils it from stock.",
                "When parts arrive, status updates to 'Handed Over'. Mark each part as 'Changed' once installed.",
            ]),
            ("Complete the Repair", [
                "Click 'Complete' on the device card.",
                "Fill in Resolution (what you did), Final Status, Cost (Rs.), and Time Spent (minutes).",
                "Check applicable action boxes: Dust Cleaning / CMOS Battery Change / Thermal Paste / RAM Updated / HDD Updated.",
                "Set 'Move to Next': Yes to escalate to L2, No to complete at L1, Complete to send to QC Check.",
                "Click 'Save'. The device moves to the next stage.",
            ]),
        ],
        "tips": [
            "Target 30 to 60 minutes per device at L1 -- escalate early if the issue is board-level.",
            "Log every part used -- this feeds directly into Parts Consumption and the device P&L.",
            "The scrap warning badge appears when cumulative repair cost approaches the device's resale value.",
            "Work ID tracks time and cost for each device across all repair stages.",
        ],
    },
    {
        "id": 5,
        "module": "L2 Repair",
        "icon": "bi-cpu",
        "color": "warning",
        "description": "Handle second-level repairs: advanced diagnostics, board-level work, display connectors, and escalation from L1.",
        "steps": [
            ("View the L2 Queue", [
                "Go to Repair -> L2 Repair from the left navigation.",
                "Devices arrive from L1 escalation or direct assignment by the inventory manager.",
            ]),
            ("Diagnose Before Starting", [
                "Click on the device row to expand the device card.",
                "Review L1 notes and the full IQC report shown in the card.",
                "Document your root cause assessment in the Issue Description before starting the repair.",
                "Good root cause notes prevent repeated failures on the same device.",
            ]),
            ("Perform the Repair", [
                "Start the repair and log all parts using the same Parts Request workflow as L1.",
                "L2 typically handles: charging circuit faults, RAM slot issues, display connector problems, and WiFi card replacement.",
                "Log time spent and cost accurately for each device.",
            ]),
            ("Complete, Escalate, or Scrap", [
                "Complete -> QC Check: device is repaired and ready for quality check.",
                "Escalate to L3: board-level damage beyond L2 capability. Add detailed escalation notes.",
                "Scrap: device is beyond economic repair. Scrap requires manager approval and a Scrap Reason.",
            ]),
        ],
        "tips": [
            "L2 handles charging circuits, RAM slots, display connectors, and WiFi card replacement.",
            "Always document root cause before starting -- it reduces repeat failures.",
            "Work ID tracks cumulative cost across L1 and L2 for the scrap-value warning.",
            "Escalate to L3 as soon as a motherboard trace or BGA issue is confirmed.",
        ],
    },
    {
        "id": 6,
        "module": "L3 Repair (Advanced)",
        "icon": "bi-motherboard",
        "color": "danger",
        "description": "Handle third-level repairs: board-level diagnosis, device replacement decisions, and final scrap authorisation.",
        "steps": [
            ("View the L3 Queue", [
                "Go to Repair -> L3 Repair from the left navigation.",
                "Devices arrive from L2 escalation or direct assignment.",
            ]),
            ("Assess the Device", [
                "Review the full repair history shown on the device card: all L1 and L2 actions, parts used, and cumulative cost.",
                "Check the P&L scrap warning -- if total repair cost approaches the estimated resale value, scrap is recommended.",
                "Decide: Repair / Device Replace / Scrap.",
            ]),
            ("Device Replace Workflow", [
                "If a device is to be replaced with a substitute unit, click 'Device Replace'.",
                "Select the replacement device from the available inventory.",
                "The original device exits the active workflow. Both devices are linked in the audit trail.",
                "The replacement device continues through QC Check before reaching Ready to Sale.",
            ]),
            ("Complete Repair or Scrap", [
                "Complete -> QC Check: repair is done and device is ready for quality check.",
                "Scrap: click 'Scrap', enter the Scrap Reason (mandatory), and confirm.",
                "Scrapped devices appear in Scrap Products with the L3 engineer's name and full cost history.",
            ]),
        ],
        "tips": [
            "Scrap Reason is mandatory -- it is required for compliance and audit reporting.",
            "Device Replace auto-links both the original and the replacement unit for traceability.",
            "The cumulative repair cost shown on the card includes L1 + L2 + L3 costs and all parts.",
            "L3 is the final escalation gate -- if a device cannot be repaired here, it must be scrapped.",
        ],
    },
    {
        "id": 7,
        "module": "Final QC / QC Check",
        "icon": "bi-patch-check",
        "color": "success",
        "description": "Score and grade devices after repair. Pass devices to Ready to Sale or route failures back to the appropriate repair stage.",
        "steps": [
            ("Open the QC Queue", [
                "Go to Quality -> QC Check from the left navigation.",
                "The list shows all devices awaiting QC with their fail count and previous QC history.",
                "Click 'Verify QC' next to the device you want to inspect.",
            ]),
            ("Score the Device (0 to 10 per category)", [
                "Battery Score (0-10): based on battery health % and real-world performance.",
                "Screen Score (0-10): based on screen defects found during this QC check.",
                "Keyboard Score (0-10): based on key function and cosmetic condition.",
                "Body Score (0-10): based on panel condition across all four panels.",
                "The system calculates the total score automatically.",
            ]),
            ("Grade Formula", [
                "Total score 85 or above: Grade A.",
                "Total score 70 to 84: Grade B.",
                "Total score 50 to 69: Grade C.",
                "Total score above 0 but below 50: Grade D (flag for review).",
                "The grade set here overrides the IQC grade for the Ready to Sale listing.",
            ]),
            ("Set the Result and Route the Device", [
                "Result: select Pass or Fail. This is the authoritative QC decision.",
                "Issues Found: describe any defects observed during this QC check.",
                "Notes: add any additional observations or recommendations.",
                "On Fail, select where to send the device: L1 / L2 / L3.",
                "On Pass, the device moves to Ready to Sale (or Cosmetic pipeline if cosmetic work is needed).",
            ]),
            ("Spec Corrections and Stress Test", [
                "If IQC specs were wrong, update Make, Model, CPU, Generation, or RAM directly on this form.",
                "Check the Stress Test result tab before finalising the QC decision.",
                "A third consecutive fail triggers a system recommendation to scrap the device.",
            ]),
        ],
        "tips": [
            "Use the 'Verify QC' button to open the form -- the old label was 'QC'.",
            "For Grade B or C devices, consider sending to the Cosmetic pipeline before marking as Pass.",
            "Check Stress Test results before scoring -- stress test failures affect the QC decision.",
            "A third consecutive QC fail generates an automatic scrap recommendation.",
        ],
    },
    {
        "id": 8,
        "module": "Cosmetic Refurbishment",
        "icon": "bi-brush",
        "color": "secondary",
        "description": "Manage the cosmetic pipeline for devices that need body refinishing before they can be sold.",
        "steps": [
            ("Access the Cosmetic Dashboard", [
                "Go to Quality -> Cosmetic Dashboard from the left navigation.",
                "The dashboard shows all devices at each stage of the cosmetic pipeline with device counts per stage.",
            ]),
            ("Understand the Cosmetic Pipeline", [
                "The pipeline has 7 stages in order:",
                "  QC Check -> Cleaning -> Dry Sanding -> Masking -> Painting -> Water Sanding -> Final QC -> Ready to Sale.",
                "Each device moves through each stage in sequence.",
                "A yellow badge on each stage header shows how many devices are currently waiting there.",
            ]),
            ("Advance a Device Through Stages", [
                "On the Cosmetic Dashboard, find the device at its current stage.",
                "Click 'Advance' to move it to the next stage in the pipeline.",
                "Each advance is logged with the operator name and timestamp.",
                "Continue until the device reaches Final QC, then it moves to Ready to Sale.",
            ]),
            ("Skip the Cosmetic Pipeline", [
                "From the QC Check decision form, select 'Skip Cosmetic' instead of sending to the pipeline.",
                "Alternatively, click 'Go Cleaning' on the QC list to start the pipeline for that device.",
                "The device jumps directly to Final QC, bypassing all cosmetic stages.",
                "Use Skip for Grade A devices or where cosmetic work is not economically justified.",
            ]),
        ],
        "tips": [
            "The cosmetic pipeline is mainly for Grade C devices or devices with significant body defects.",
            "A full pipeline run typically takes 1 to 3 days depending on workload.",
            "Plan cosmetic batches by grade to improve throughput.",
            "Use the 'Go Cleaning' button on the QC Check list to start the cosmetic pipeline for a device.",
        ],
    },
    {
        "id": 9,
        "module": "Ready to Sale",
        "icon": "bi-tag",
        "color": "success",
        "description": "View all QC-cleared devices available for sale. Manage dispatch requests, pricing, and direct sales.",
        "steps": [
            ("View the Ready to Sale List", [
                "Go to Sales -> Ready to Sale from the left navigation.",
                "The list shows all devices that have passed QC and are cleared for sale.",
                "Columns: Tag, Brand, Model, Grade, Asking Price, Warranty badge (30 days from last sale date), and Days in Queue.",
            ]),
            ("Raise a Dispatch Request (Telecaller)", [
                "Telecallers click 'Request Dispatch' next to a device they want to offer to a customer.",
                "The request appears on the TRC Dashboard for the Sales Manager to review.",
                "The 'Sell' button on the device is locked until the Sales Manager approves the dispatch request.",
            ]),
            ("Sell a Device (Admin or Sales role)", [
                "Admin and Sales roles can click 'Sell' directly without a dispatch request.",
                "Fill in: Customer Name, Sale Price, Discount, and Payment Type.",
                "If the sale price is below cost, a below-cost warning appears. This is advisory and does not block the sale.",
                "Confirm the sale. A Sale Number is generated automatically in the format SALE-XXXX.",
            ]),
            ("CRM Dealer Banner", [
                "Below the device list, a CRM banner shows dealers who have expressed interest in this device type or grade.",
                "Use this to contact interested dealers before marking a device for walk-in sale.",
            ]),
        ],
        "tips": [
            "The Days in Queue column shows how long a device has been in Ready to Sale -- prioritise ageing stock.",
            "The below-cost warning is advisory, not a hard block. The manager reviews below-cost sales.",
            "Telecallers always need a dispatch request approved before the Sell button unlocks.",
            "The 30-day warranty badge resets from the most recent sale date if a device is returned and resold.",
        ],
    },
    {
        "id": 10,
        "module": "Sales",
        "icon": "bi-bag-check",
        "color": "primary",
        "description": "View all completed sales, process returns, generate invoices, and import bulk sales via CSV.",
        "steps": [
            ("View the Sale List", [
                "Go to Sales -> Sale List from the left navigation.",
                "Filter by date range, sale status, or search by customer name or Tag Number.",
                "Each row shows: Sale Number (SALE-XXXX), Tag, Brand, Model, Customer, Sale Price, Date, and Status.",
            ]),
            ("Process a Return", [
                "Click 'Return' next to the sale record.",
                "Fill in Return Date, Reason, and Notes.",
                "The device automatically re-enters IQC regardless of whether it is within or out of warranty.",
                "Both within-warranty and out-of-warranty returns are tracked separately in the TRC Dashboard.",
            ]),
            ("Generate a Sale Invoice", [
                "Click 'Invoice' next to the sale record.",
                "A printable PDF invoice is generated with device details, buyer information, serial number, and sale price.",
                "Click 'Download Invoice' to save the PDF.",
            ]),
            ("Import Bulk Sales via CSV", [
                "Go to Sales -> Import (admin role only).",
                "Prepare a CSV with columns: tag_number, customer_name, price, sale_date.",
                "Upload the file. The system validates each row and imports valid sales.",
                "Use this for migrating historical sales data from external systems.",
            ]),
        ],
        "tips": [
            "Sale Numbers (SALE-XXXX) are generated from a database sequence and never reset.",
            "All returns re-enter IQC -- the device must pass QC again before it can be resold.",
            "Within-warranty returns are tracked for ESG and compliance reporting.",
            "Use CSV import only for bulk historical migration -- always verify the file format first.",
        ],
    },
    {
        "id": 11,
        "module": "TRC Dashboard",
        "icon": "bi-kanban",
        "color": "info",
        "description": "The Telecaller and Sales Manager dispatch interface. View ready-to-sale devices, manage dispatch approvals, and track returned devices.",
        "steps": [
            ("Access the TRC Dashboard", [
                "Go to Dispatch -> TRC Dashboard from the left navigation.",
                "Visible to roles: Admin, Inventory Manager, Sales, Sales Manager, and Telecaller.",
            ]),
            ("Browse Ready-to-Sale Devices", [
                "Grade A and Grade B devices appear side-by-side in two columns for quick comparison.",
                "Grade C devices appear full-width below the A/B section.",
                "Use the telecaller filter row to filter by product type or grade.",
                "Columns: Tag, Brand, Model, Grade, Price, Lot Number, Timeline (days in queue), and action buttons.",
            ]),
            ("Manage Dispatch Requests", [
                "Pending dispatch requests appear at the top of the dashboard as a priority list.",
                "Sales Manager and Admin can click 'Approve' to unlock the Sell button for the requesting telecaller.",
                "Sales Manager can 'Reject' a request with a note explaining the reason.",
                "Once approved, the telecaller sees the Sell button unlocked on their view.",
            ]),
            ("Devices Returned Section", [
                "Scroll down to see the 'Devices Returned' section below the main dispatch table.",
                "Two tables appear side by side: Within Warranty returns and Out of Warranty returns.",
                "Columns: Tag Number, Model, Returned On date, Customer Name, User who processed the return, Reason.",
                "Use this section to track return patterns and identify quality issues.",
            ]),
        ],
        "tips": [
            "Grade A and B are shown side-by-side to help telecallers compare grades for a customer.",
            "Only Admin and Sales Manager can approve dispatch requests.",
            "The timeline column helps identify devices that have been in Ready to Sale too long.",
            "Within-warranty returns in the Devices Returned section are useful for quality trend analysis.",
        ],
    },
    {
        "id": 12,
        "module": "Parts & Inventory",
        "icon": "bi-boxes",
        "color": "secondary",
        "description": "Manage spare parts stock, engineer part requests, and RAM replacement tracking using a double-entry ledger.",
        "steps": [
            ("View the Spare Parts List", [
                "Go to Parts -> Spare Parts from the left navigation.",
                "Parts are organised by category: RAM / HDD / SSD / Battery / Screen / Keyboard / Charger / Motherboard / Cable / Other.",
                "Each part has a unique Part Code in the format PART-XXXX.",
            ]),
            ("Add a New Part", [
                "Click 'Add Part' at the top.",
                "Fill in: Name, Category, Unit Cost, Description, and Minimum Stock Level.",
                "The Part Code is auto-generated. Click 'Save'.",
            ]),
            ("Record a Stock Purchase", [
                "On the part's card, click 'Purchase'.",
                "Enter: Quantity, Supplier, Purchase Price per unit, Date, and Invoice Reference.",
                "Click 'Save'. This creates an IN ledger entry. Current stock increases accordingly.",
            ]),
            ("Fulfil Part Requests from Engineers", [
                "Click the 'Part Requests' tab to see all pending requests from L1/L2/L3 engineers.",
                "For each request, click 'Fulfil' to hand the part over to the engineer.",
                "If the part is out of stock, click 'Not in Stock'. An alert badge then appears on the engineer's repair queue.",
                "Once the engineer installs the part, they mark it as 'Changed' in the repair form.",
            ]),
            ("Export and RAM Tracking", [
                "Use checkboxes on the parts list to select multiple parts, then click 'Export CSV' for the selection.",
                "Click the 'RAM Tracking' tab to see the per-device RAM change history for all repairs.",
                "RAM tracking is useful for warranty disputes and compliance audits.",
            ]),
        ],
        "tips": [
            "Stock is computed from the double-entry ledger: current stock = total IN minus total OUT.",
            "Negative stock is blocked -- you cannot issue more parts than what is in stock.",
            "The 'Not in Stock' alert badge appears on the engineer's repair queue to flag the delay.",
            "Set the Minimum Stock Level for each part to enable reorder planning.",
        ],
    },
    {
        "id": 13,
        "module": "Dealers",
        "icon": "bi-people",
        "color": "primary",
        "description": "Manage the dealer and buyer network: contacts, call logs, orders, ledger, credit notes, and follow-up scheduling.",
        "steps": [
            ("Browse and Filter Dealers", [
                "Go to Dealers from the left navigation.",
                "Use the filter bar to search by name, filter by status, assigned RSM, city, last order date, or next follow-up date.",
                "The list shows 50 dealers per page. Click any dealer to open their full profile.",
            ]),
            ("Add a New Dealer", [
                "Click 'New Dealer' at the top.",
                "Fill in: Company Name, Contact Name, Phone, Email, City, Category Preference, and Credit Limit.",
                "Assign an RSM (Regional Sales Manager) to the dealer.",
                "Click 'Save Dealer'. The dealer appears in the list with status 'Active'.",
            ]),
            ("Log a Call", [
                "Open the dealer profile and click 'Log Call'.",
                "Fill in: Call Date, Duration (minutes), Outcome (Interested / Callback / Not Interested / Placed Order), Notes, and Next Follow-up Date.",
                "Call logs appear in the dealer's activity timeline.",
            ]),
            ("Create and Track Orders", [
                "Click 'New Order' in the dealer profile.",
                "Add line items: product, quantity, and unit price. Fill in Payment Terms and Expected Delivery Date.",
                "Order status moves from Pending -> Confirmed -> Delivered as you update it.",
                "All orders feed into the dealer's ledger and outstanding balance.",
            ]),
            ("Ledger, Ageing, and Credit Notes", [
                "The Ledger tab shows all orders and payments for the dealer with an outstanding balance.",
                "The Ageing tab shows debt in buckets: 0-30 days, 31-60 days, 61-90 days, and over 90 days.",
                "Overdue accounts are flagged automatically.",
                "Click 'Credit Note' to create a credit offset against an outstanding balance.",
                "The Followups tab shows upcoming follow-up dates and auto-generated upsell suggestions.",
            ]),
        ],
        "tips": [
            "Upsell suggestions are auto-generated based on last purchase, outstanding balance, and category preferences.",
            "Only Admin and Sales Manager can confirm orders -- Sales and Telecaller can view only.",
            "Log every call -- the activity timeline is the key record for performance reviews.",
            "Set a follow-up date on every call so no dealer goes cold.",
        ],
    },
    {
        "id": 14,
        "module": "User Management",
        "icon": "bi-people-fill",
        "color": "dark",
        "description": "Create and manage user accounts, assign roles and module permissions, and review audit and login logs.",
        "steps": [
            ("Create a New User", [
                "Go to Admin -> Users from the left navigation.",
                "Click 'Add User'.",
                "Fill in: Username (unique, no spaces), Full Name, Email, WhatsApp Number, Role, and Password.",
                "Click 'Create User'. The user can log in immediately.",
            ]),
            ("Role Reference", [
                "Admin: full access to all modules, user management, reports, and settings.",
                "Inventory Manager: lots, stock inward, IQC, stage movement, and reports.",
                "IQC Handler: IQC entry and stage movement only.",
                "L1 / L2 / L3 Engineer: respective repair stages and parts requests.",
                "QC Handler: QC check, stress test, and QA dashboard.",
                "Sales: Ready to Sale, CRM, and returns.",
                "Sales Manager: all Sales functions plus dispatch request approval.",
                "Parts Manager: parts inventory and part request fulfilment.",
                "Telecaller: TRC Dashboard and dispatch requests only.",
            ]),
            ("Set Module Permissions", [
                "Click 'Permissions' next to a user or role.",
                "Permission groups: Inventory & Lots / IQC & Repair / Sales & Invoices / CRM - Contacts & Deals.",
                "Toggle each module on or off for the user. Changes take effect immediately.",
                "The 'Enable' switch is the master toggle -- disabling it blocks all access for that user",
                "regardless of individual module settings.",
            ]),
            ("Manage User Profile", [
                "Click the username in the top bar to open your own profile page.",
                "Admin can edit any user's Email, Password, and Status from the user list.",
                "Set Status to 'Inactive' to block a user from logging in without deleting their account.",
            ]),
            ("Review Audit and Login Logs", [
                "Admin -> Login Log: shows every login attempt with IP address, browser, and timestamp.",
                "Admin -> Audit Log: shows every data change (who changed what, and when).",
                "Admin -> Cost Config: set per-lot cost benchmarks used in P&L calculations.",
            ]),
        ],
        "tips": [
            "Never share admin credentials -- create individual accounts for each team member.",
            "The Enable switch is the master override -- disabling it blocks all access even if modules are toggled on.",
            "Deactivate accounts for departed staff; do not delete them, to preserve audit history.",
            "Run a quarterly access review to check for unnecessary permissions.",
        ],
    },
    {
        "id": 15,
        "module": "Reports & Analytics",
        "icon": "bi-bar-chart",
        "color": "dark",
        "description": "Access Business P&L, Lot P&L, and Receivables reports to analyse profitability and outstanding dealer balances.",
        "steps": [
            ("Business P&L Report", [
                "Go to Reports -> Business P&L from the left navigation.",
                "Select the date range and click 'Generate Report'.",
                "The report shows for the selected period: Total Revenue, COGS, Gross Profit, and Margin %.",
                "Per-device breakdown: Buying Cost + Repair Cost + Parts Cost + Selling Price + Gross Margin %.",
                "Click 'Export CSV' to download the full report.",
            ]),
            ("Lot P&L Report", [
                "Go to Reports -> Lot P&L.",
                "Select a lot from the dropdown.",
                "The report shows: Total Devices, Purchase Cost, Total Repair Cost, Sale Revenue, and Net Profit for the lot.",
                "Compare target margin vs actual margin to evaluate sourcing decisions.",
                "Use this report to identify which suppliers and lot types deliver the best returns.",
            ]),
            ("Receivables Report", [
                "Go to Reports -> Receivables.",
                "The report shows outstanding dealer balances with ageing buckets: 0-30 days, 31-60 days, and over 60 days.",
                "Filter by dealer or date range.",
                "Export as Excel for the accounts team to reconcile with their ledger.",
            ]),
        ],
        "tips": [
            "P&L cost for each device = lot buying price + all repair costs (L1 + L2 + L3) + all parts used.",
            "Complete GRN mapping for all devices in a lot before running Lot P&L for accurate results.",
            "Lot P&L is the best tool for evaluating which suppliers and device types are most profitable.",
            "Share Receivables with the finance team every Monday for cash flow planning.",
            "Business P&L is best reviewed monthly -- weekly fluctuations can be misleading.",
        ],
    },
]

_MANUAL_MAP = {m["id"]: m for m in MANUALS}


# ── PDF generation ────────────────────────────────────────────────────────────

def _generate_pdf(manual: dict) -> bytes:
    try:
        from fpdf import FPDF
        return _pdf_fpdf(manual)
    except Exception:
        return _pdf_text(manual)


def _pdf_fpdf(manual: dict) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_margins(15, 15, 15)

    # ── Header ──────────────────────────────────────────────────────────────
    pdf.set_fill_color(30, 30, 30)
    pdf.rect(0, 0, 210, 30, "F")
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(255, 255, 255)
    pdf.set_y(9)
    pdf.cell(0, 8, "OxyPC -- Learning Manual", align="C")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(180, 180, 180)
    pdf.cell(0, 5, "Operational Training Guide", align="C")
    pdf.ln(18)

    # ── Module title ─────────────────────────────────────────────────────────
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, manual["module"])
    pdf.ln()
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(0, 6, manual["description"])
    pdf.ln(4)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(4)

    # ── Steps ────────────────────────────────────────────────────────────────
    pdf.set_text_color(0, 0, 0)
    for step_idx, (step_title, step_items) in enumerate(manual["steps"], 1):
        # Step heading
        pdf.set_fill_color(240, 240, 240)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, f"  Step {step_idx}: {step_title}", fill=True)
        pdf.ln()
        pdf.ln(1)
        # Step sub-items
        pdf.set_font("Helvetica", "", 9)
        for item in step_items:
            if item.startswith("  "):
                pdf.set_x(25)
                pdf.cell(4, 6, "-", border=0)
                pdf.multi_cell(0, 6, item.strip())
            else:
                pdf.set_x(20)
                pdf.cell(4, 6, "-", border=0)
                pdf.multi_cell(0, 6, item)
        pdf.ln(3)

    # ── Tips ─────────────────────────────────────────────────────────────────
    if manual.get("tips"):
        pdf.set_fill_color(255, 243, 205)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, "  Tips & Best Practices", fill=True, ln=True)
        pdf.ln(1)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(100, 80, 0)
        for tip in manual["tips"]:
            pdf.set_x(20)
            pdf.cell(4, 6, "*", border=0)
            pdf.multi_cell(0, 6, tip)
        pdf.ln(3)

    # ── Footer ───────────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 5, "OxyPC Inventory Lifecycle -- Confidential Training Document", align="C")

    return bytes(pdf.output())


def _pdf_text(manual: dict) -> bytes:
    lines = [
        "OxyPC Learning Manual",
        "=" * 60,
        f"Module: {manual['module']}",
        f"Description: {manual['description']}",
        "",
    ]
    for idx, (title, items) in enumerate(manual["steps"], 1):
        lines.append(f"Step {idx}: {title}")
        lines.append("-" * 40)
        for item in items:
            lines.append(f"  - {item.strip()}")
        lines.append("")
    if manual.get("tips"):
        lines.append("Tips & Best Practices")
        lines.append("-" * 40)
        for tip in manual["tips"]:
            lines.append(f"  * {tip}")
    return "\n".join(lines).encode("utf-8")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def manuals_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Fetch all active users for the share dropdown
    res = await db.execute(
        select(User).where(User.status == True).order_by(User.full_name)
    )
    users = res.scalars().all()

    shared = request.query_params.get("shared")
    return templates.TemplateResponse("manuals/index.html", {
        "request": request,
        "current_user": current_user,
        "manuals": MANUALS,
        "users": users,
        "shared": shared,
    })


@router.get("/{mid}/download")
async def download_manual(
    mid: int,
    current_user: User = Depends(get_current_user),
):
    manual = _MANUAL_MAP.get(mid)
    if not manual:
        raise HTTPException(404, "Manual not found")

    # Serve from cache if available
    cache_path = _MANUALS_DIR / f"manual_{mid}.pdf"
    if cache_path.exists():
        pdf_bytes = cache_path.read_bytes()
    else:
        pdf_bytes = _generate_pdf(manual)
        try:
            cache_path.write_bytes(pdf_bytes)
        except Exception:
            pass

    slug = manual["module"].lower().replace(" ", "_").replace("&", "and").replace("/", "_")
    # Sniff actual bytes — fpdf2 may silently fall back to plain text
    if pdf_bytes[:5] == b"%PDF-":
        media_type = "application/pdf"
        filename = f"OxyPC_Manual_{slug}.pdf"
    else:
        media_type = "text/plain; charset=utf-8"
        filename = f"OxyPC_Manual_{slug}.txt"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{mid}/share", dependencies=[Depends(verify_csrf)])
async def share_manual(
    mid: int,
    request: Request,
    target_user_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manual = _MANUAL_MAP.get(mid)
    if not manual:
        raise HTTPException(404, "Manual not found")

    # Verify target user exists
    res = await db.execute(select(User).where(User.id == target_user_id))
    target = res.scalar_one_or_none()
    if not target:
        raise HTTPException(404, "User not found")

    notif = Notification(
        user_id=target.id,
        title=f"Learning Manual Shared: {manual['module']}",
        message=(
            f"{current_user.full_name or current_user.username} shared the "
            f"'{manual['module']}' learning manual with you. "
            f"Download it from Manuals in the left navigation."
        ),
        notification_type="info",
        stage="Manuals",
    )
    db.add(notif)
    await db.commit()

    return RedirectResponse(f"/manuals/?shared={manual['module']}", status_code=303)
