import uuid
from datetime import datetime
from utils.timezone import app_now
from sqlalchemy import Column, String, Integer, Boolean, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from database import Base


class MasterData(Base):
    __tablename__ = "master_data"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category = Column(String(50), nullable=False, index=True)
    value = Column(String(200), nullable=False)
    description = Column(String(500), nullable=True)
    display_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=app_now)

    __table_args__ = (UniqueConstraint("category", "value", name="uq_master_category_value"),)


# Dedicated Entity value for External Partner (Trade Partner) test-data intake
# (2026-09-22, see routers/partner_admin.py manage_lots): GRN/Lot/Device test
# records created for portal testing are tagged with this entity + the
# EXTERNAL_PARTNER_TEST_LOT_PREFIX lot-number prefix so they stay visually and
# programmatically distinguishable from live inventory. Devices on this
# entity are excluded by default (unless explicitly entity-filtered) from
# routers/dashboard.py, devices.py, stock.py and entity_movement.py's default
# views — see EXTERNAL_PARTNER_TEST_ENTITY usage in each.
EXTERNAL_PARTNER_TEST_ENTITY = "External Partner Test"
EXTERNAL_PARTNER_TEST_LOT_PREFIX = "EPT-"

# GRNImport.source value (models/grn_import.py, plain String(20) — "invoice"
# and "post_iqc" are the two existing values, this is a genuine third) for
# GRNs created from the Manage Lots page's embedded "GRN & Lot" tab. Reusing
# this existing column — rather than a lot-number-prefix inference, which
# would leave a freshly-created, not-yet-lot-mapped test GRN un-flagged —
# means the live GRN pages (routers/grn.py grn_import_list's `source !=
# "post_iqc"` and grn_post_iqc's `source == "post_iqc"`) exclude these test
# GRNs automatically, with no change needed to either query beyond the
# explicit `!= EXTERNAL_PARTNER_TEST_GRN_SOURCE` exclusion added to
# grn_import_list (grn_post_iqc's strict `== "post_iqc"` already excludes it).
EXTERNAL_PARTNER_TEST_GRN_SOURCE = "ext_partner_test"


# Seed data for initial master data setup
MASTER_SEED = {
    "entity": [
        "Deshwal", "OxyPC Computers", "Renew Circuits",
    ],
    "l34_activity": [
        "Motherboard Repair", "Chip Level Repair", "Component Replacement",
        "Reballing", "Cleaning & Reflow", "Data Recovery", "Other",
    ],
    "brand": [
        "HP", "Dell", "Lenovo", "Apple", "Asus", "Acer", "Toshiba", "Sony",
        "Samsung", "MSI", "Microsoft", "LG", "Huawei", "Razer", "Compaq",
    ],
    "device_type": [
        "Laptop", "Desktop", "All-in-One", "Workstation", "Mini PC",
        "Tablet", "Server", "Chromebook",
    ],
    "storage_type": [
        "HDD", "SSD", "NVMe SSD", "eMMC", "SSHD (Hybrid)",
    ],
    "storage_size": [
        "64GB", "128GB", "240GB", "256GB", "320GB", "480GB", "500GB", "512GB",
        "1TB", "2TB",
    ],
    "ram_type": [
        "DDR3", "DDR3L", "DDR4", "DDR4L", "DDR5", "LPDDR4", "LPDDR5",
    ],
    "ram_size": ["2GB", "4GB", "8GB", "12GB", "16GB", "24GB", "32GB", "64GB"],
    "processor_brand": ["Intel", "AMD", "Apple Silicon"],
    "floor": ["Floor 1", "Floor 2", "Floor 3", "Warehouse", "Workshop", "Showroom"],
    "color": [
        "Black", "Silver", "White", "Grey", "Dark Grey", "Gold", "Rose Gold",
        "Blue", "Red", "Green",
    ],
    "repair_issue": [
        "Screen", "Battery", "Keyboard", "Touchpad", "Motherboard", "Hinge",
        "Port (USB/HDMI)", "RAM Slot", "Storage", "Fan / Cooling", "Power Jack",
        "Wi-Fi Card", "Camera", "Speaker", "Charging Issue", "No POST", "Other",
    ],
    "data_destruction_method": [
        "NIST 800-88 Clear", "NIST 800-88 Purge", "DoD 5220.22-M (3-pass)",
        "Gutmann (35-pass)", "Physical Shred", "Degauss", "Not Required",
    ],
    "cosmetic_grade": [
        "A — Like New (no visible marks)",
        "B — Good (minor scratches, no dents)",
        "C — Fair (visible scratches, minor dents)",
        "D — Poor (heavy marks, significant damage)",
        "Scrap — Parts only / non-cosmetic",
    ],
    "sub_category": [
        "Laptop", "Desktop", "All-in-One", "Workstation", "Mini PC",
        "Tablet", "Server", "Chromebook", "Thin Client",
    ],
    # Physical port counts per model, because Windows cannot report how many
    # sockets are on a chassis — UCSI exposes connector-manager nodes, not
    # connectors, and USB-A has no enumerable source at all. The agent's probe
    # is a form-factor guess; a match here overrides it.
    #
    # Format: "<Brand>|<Model>|A=<n>,C=<n>,E=<n>"  (A=USB-A, C=USB-C, E=RJ45)
    # Model matching is case-insensitive and prefix-based, so "Latitude 5420"
    # also matches "Latitude 5420 Rugged". Add rows from Master Data — no code
    # change and no agent rebuild needed.
    "port_profile": [
        "Dell|Latitude 5420|A=2,C=2,E=1",
        "Dell|Latitude 5430|A=2,C=2,E=1",
        "Dell|Latitude 7420|A=2,C=2,E=0",
        "Dell|Latitude 7430|A=2,C=2,E=0",
        "Dell|Precision 5570|A=0,C=3,E=0",
        "Dell|Precision 5560|A=0,C=3,E=0",
        "Dell|OptiPlex 7090|A=6,C=1,E=1",
        "Dell|OptiPlex 3080|A=6,C=0,E=1",
        "HP|EliteBook 840 G7|A=2,C=2,E=1",
        "HP|EliteBook 840 G8|A=2,C=2,E=1",
        "HP|EliteBook 830 G8|A=2,C=2,E=0",
        "HP|ProBook 440 G8|A=2,C=1,E=1",
        "HP|EliteDesk 800 G6|A=6,C=1,E=1",
        "Lenovo|ThinkPad T14|A=2,C=2,E=1",
        "Lenovo|ThinkPad T490|A=2,C=2,E=1",
        "Lenovo|ThinkPad X1 Carbon|A=2,C=2,E=0",
        "Lenovo|ThinkPad L14|A=2,C=2,E=1",
        "Lenovo|ThinkCentre M720|A=6,C=0,E=1",
        "Apple|MacBook Air|A=0,C=2,E=0",
        "Apple|MacBook Pro 13|A=0,C=2,E=0",
        "Apple|MacBook Pro 14|A=0,C=3,E=0",
        "Apple|MacBook Pro 16|A=0,C=3,E=0",
    ],
    "processor_series": [
        "Intel Core i3", "Intel Core i5", "Intel Core i7", "Intel Core i9",
        "Intel Pentium", "Intel Celeron", "Intel Xeon",
        "AMD Ryzen 3", "AMD Ryzen 5", "AMD Ryzen 7", "AMD Ryzen 9",
        "AMD A-Series", "AMD EPYC",
        "Apple M1", "Apple M2", "Apple M3", "Qualcomm Snapdragon",
    ],
    "generation": [
        "4th Gen", "5th Gen", "6th Gen", "7th Gen", "8th Gen", "9th Gen",
        "10th Gen", "11th Gen", "12th Gen", "13th Gen", "14th Gen",
        "Ryzen 1st Gen", "Ryzen 2nd Gen", "Ryzen 3rd Gen",
        "Ryzen 4th Gen", "Ryzen 5th Gen", "Ryzen 6th Gen",
    ],
    "screen_size": [
        '11.6"', '12.5"', '13.3"', '13.5"', '14.0"', '14.1"',
        '15.0"', '15.6"', '17.3"', '19.5"', '21.5"', '23.8"', '24.0"', '27.0"',
    ],
    "grade": [
        "Grade A — Like New",
        "Grade B — Good Condition",
        "Grade C — Average Condition",
        "Grade D — Poor Condition",
        "Scrap / Parts Only",
    ],
    # Transport (Sale Detail "Update Transport" + Gate Pass Confirm Exit)
    "transport_mode": [
        "Courier", "Hand Delivery", "Self Pickup", "Own Vehicle", "Transporter",
    ],
    "delivery_courier": [
        "Blue Dart", "DTDC", "Delhivery", "Professional Couriers", "India Post", "Ekart",
    ],
    "payment_mode": [
        "Cash", "UPI / GPay / PhonePe", "Bank Transfer (NEFT/RTGS/IMPS)",
        "Cheque", "Credit Card", "Debit Card", "Online Portal", "COD",
    ],
    "warehouse": [
        "TRC 1st Floor", "TRC 2nd Floor", "TRC 3rd Floor",
        "Main Warehouse", "Workshop", "Showroom", "Dispatch Area", "Holding Zone",
    ],
    "location_zone": [
        "Showroom", "Ground Floor", "1st Floor", "2nd Floor",
        "Workshop", "Dispatch Area", "Warehouse", "Holding Zone",
    ],
    "location_unit_type": [
        "Rack", "Crate", "Shelf", "Trolley", "Cabinet", "Floor Space",
    ],
    # ── Spare Part Names — the consolidated shop-floor part list. Order here is
    #    the order it shows on the IQC/repair forms: MAIN PARTS first, then
    #    ADDITIONAL PARTS. Renames already applied (Panel<-Display Panel,
    #    Screen<-Display, Camera<-Web Cam, DC Jack<-Charging Port,
    #    LAN Port<-Ethernet Port, Logic Card<-Touchpad, Wi-Fi Card<-Wi-Fi,
    #    Touchpad Cover<-Palm rest). Desktop-only items (DVD Drive, DVD Drive
    #    Cover) stay in the master list; visibility by tag type is a form
    #    concern, not a master-data one. ─────────────────────────────────────
    "part_category": [
        # MAIN PARTS
        "RAM", "Hard Drive", "Bezel", "Panel", "Screen", "Hinge", "Bottom Base",
        "Keyboard", "Internal Battery", "External Battery", "Camera", "DC Jack",
        "LAN Port", "USB Port", "HDMI Port", "Audio Jack", "Speaker",
        "Fan Working", "Motherboard", "Touchpad", "Logic Card", "Wi-Fi Card",
        "DVD Drive",
        # ADDITIONAL PARTS
        "RAM Cover", "DVD Drive Cover", "Hinge Cover", "Hard Drive Cover",
        "HDD Connector", "Touchpad Cover", "Touchpad Cable", "Camera Cable",
        "Battery Cable", "Click Button", "Battery Cover",
    ],
    # Seeded empty on purpose — brands are supplier-specific and get added by
    # the store manager from Dropdown Configuration rather than shipped as
    # defaults nobody buys from.
    "spare_part_brand": [],
    "supplier": [
        "ABC Traders", "XYZ Electronics", "Local Market", "Online Purchase",
        "Direct Brand", "Government Surplus", "Corporate Buyback",
    ],
    "repair_resolution": [
        "Replaced Component", "Repaired / Soldered", "Updated Firmware / Drivers",
        "OS Reinstalled", "Cleaned / Dusted", "Settings Changed",
        "No Fault Found", "Irreparable — Scrap", "Escalated to L2", "Escalated to L3",
    ],
    "l1_issue": [
        "Screen Damage", "Battery Not Charging", "Keyboard Not Working",
        "Trackpad Issue", "Hinge Broken", "USB Port Not Working", "HDMI Port Issue",
        "Wi-Fi Not Connecting", "Bluetooth Issue", "Speaker Issue", "Microphone Issue",
        "Webcam Not Working", "Power Button Issue", "Overheating", "Slow Performance",
        "OS Not Booting", "Physical Damage — Casing", "Dead on Arrival",
    ],
    "l2_issue": [
        "Motherboard Fault", "RAM Slot Issue", "Storage Failure",
        "Display Controller Issue", "Power Circuit Issue", "BIOS Corruption",
        "Liquid Damage", "Short Circuit", "Charging Circuit",
        "Battery Cell Replacement", "GPU Issue", "No POST",
    ],
    "l3_issue": [
        "Complex Motherboard Repair", "BGA Chip Replacement", "Data Recovery",
        "Component-Level Repair", "Custom Firmware", "Advanced BIOS Recovery",
    ],
    "qc_check_item": [
        "Screen Quality", "Battery Health %", "Keyboard All Keys",
        "Trackpad Sensitivity", "All Ports Functional", "Wi-Fi & Bluetooth",
        "Camera & Mic", "Speaker & Audio", "OS Clean Install",
        "Windows Activation", "Drivers Installed", "Performance Benchmark",
        "Cosmetic Grade", "Serial Number Match", "Bios Password Cleared", "Data Wiped",
    ],
    "return_reason": [
        "Dead on Arrival", "Wrong Specification", "Customer Changed Mind",
        "Performance Issue", "Cosmetic Damage Not Disclosed", "Overheating",
        "Battery Drain", "Warranty Claim", "Duplicate Order", "Other",
    ],
    "condition_on_return": [
        "Like New", "Good — Minor Wear", "Functional — Cosmetic Damage",
        "Partially Working", "Non-Functional", "Damaged Beyond Repair",
    ],
    "cosmetic_issue": [
        "Scratches on Lid", "Scratches on Base", "Dent on Corner",
        "Cracked Hinge", "Broken Bezel", "Chipped Key", "Screen Crack",
        "Screen Stain", "Faded Paint", "Missing Rubber Foot", "Broken Latch", "Body Flex",
    ],
    "battery_health": [
        "95-100% (Excellent)", "85-94% (Good)", "70-84% (Fair)",
        "50-69% (Weak)", "Below 50% (Replace)", "Not Tested", "No Battery",
    ],
    "os_version": [
        "Windows 11 Home", "Windows 11 Pro", "Windows 10 Home", "Windows 10 Pro",
        "Windows 7 Professional", "Ubuntu 22.04 LTS", "Ubuntu 20.04 LTS",
        "macOS Ventura", "macOS Sonoma", "Chrome OS", "No OS",
    ],
    "port_type": [
        "USB-A 2.0", "USB-A 3.0", "USB-A 3.1", "USB-C 3.1",
        "USB-C Thunderbolt 3", "USB-C Thunderbolt 4", "HDMI", "Mini HDMI",
        "DisplayPort", "VGA", "SD Card Reader", "3.5mm Audio",
        "RJ45 Ethernet", "DC Power Jack",
    ],
    # ── Telecalling deal-detail dropdowns (admin-manageable via /admin/master) ──
    "tc_category": [
        "Laptop", "Desktop", "Mobile", "Server", "Tablet", "Workstation",
    ],
    "tc_model": [
        "Dell Latitude", "Dell OptiPlex", "HP EliteBook", "HP ProBook",
        "Lenovo ThinkPad", "Lenovo ThinkCentre",
    ],
    "tc_configuration": [
        "i3/4GB/500GB HDD", "i5/8GB/256GB SSD", "i7/16GB/512GB SSD",
    ],
    "tc_deal_status": [
        "Open", "Negotiation", "Won", "Lost",
    ],
    "tc_whom_to_sell": [
        "Corporate", "End User", "Retail",
    ],
    "tc_deals_in": [
        "Indian", "Imported",
    ],
    "tc_stock_type": [
        "Ready Stock", "Lot",
    ],
    # ── Assign Social Leads: call log / filter Status (admin-manageable) ──────
    "asl_status": [
        "interested", "not_interested", "callback", "order_placed",
        "no_answer", "followup", "done", "rescheduled",
        "not_in_stock", "high_price", "invalid_no",
    ],
    # ── Dealer / Telecalling call log (unifies 3 previously-divergent copies) ──
    "call_outcome": [
        "interested", "order_placed", "callback", "not_interested",
        "no_answer", "followup", "do_not_call",
    ],
    "call_mode": ["phone", "whatsapp", "in_person"],
    "call_type": ["outbound", "inbound"],
    # ── QC / Cosmetic (unifies 3 previously-divergent copies) ─────────────────
    "qc_failure_reason": [
        "Hardware", "Software", "Cosmetic",
    ],
    # ── Repair L3 ──────────────────────────────────────────────────────────────
    "repair_action_taken": [
        "BIOS Programming", "CPU Socket PIN alignment", "Part Replaced",
        "Re-soldering", "Reflow", "Shorting removed", "Track Repair", "Others",
    ],
    "repair_received_from": ["L1/L2 Engineer", "L4 Support"],
    "repair_scrap_reason": [
        "CPU SHORT", "CPU Sorting", "Component Burn", "Component Damage",
        "Component Missing", "PCB Burn", "PCB Damage", "Internal PCB Short",
    ],
    "repair_source_type": ["Internal", "Customer Service"],
    # ── Sales ──────────────────────────────────────────────────────────────────
    "customer_state": [
        "Maharashtra", "Karnataka", "Tamil Nadu", "Uttar Pradesh",
        "Haryana", "Gujarat", "Rajasthan", "West Bengal", "Telangana",
        "Punjab", "Other State",
    ],
    "sale_warranty_type": ["none", "30_days", "6_months", "1_year"],
    "return_type": ["customer", "dealer"],
    "product_return_reason": [
        "Not working", "Wrong item", "Customer changed mind",
        "Dead on arrival", "Physical damage", "Other",
    ],
    "dealer_credit_reason": [
        "goods_returned", "damaged_goods", "wrong_item",
        "short_delivery", "price_adjustment", "other",
    ],
    # ── Transfers ──────────────────────────────────────────────────────────────
    "transfer_type": ["trc_to_showroom", "showroom_to_trc", "showroom_lot", "internal", "as_is_lot",
                      "ready_for_sale", "scrap_for_sale"],
    # ── WhatsApp ───────────────────────────────────────────────────────────────
    "whatsapp_message_type": ["Text", "Product Catalog", "Invoice", "Payment Reminder"],
    "whatsapp_group_category": ["dealer", "personal", "other"],
    # ── Market / Barter Board ──────────────────────────────────────────────────
    "market_trade_type": ["sell", "buy"],
    "market_item_category": [
        "Laptop", "Desktop", "Monitor / TFT", "Printer", "Server",
        "Tablet", "Projector", "Spare Parts", "Other",
    ],
    "market_condition": ["refurb", "new", "used", "as-is"],
    # ── Dealers ────────────────────────────────────────────────────────────────
    "dealer_dealer_type": ["retail", "wholesale", "corporate", "service"],
    "dealer_status": ["active", "inactive", "blacklisted"],
    # Filled from uploads (a Google Maps export adds its "Category" values here)
    # and by admins on /admin/master.
    "dealer_sub_category": [],
    # Dealer State dropdown (Add/Edit Dealer). Seeded into an existing database
    # on server start while the category is still empty — see
    # utils.master_data.ensure_master_defaults.
    "dealer_state": [
        "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
        "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
        "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
        "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim",
        "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
        "West Bengal", "Andaman and Nicobar Islands", "Chandigarh",
        "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir",
        "Ladakh", "Lakshadweep", "Puducherry",
    ],
    # ── CRM (previously plain Python constants in models/crm.py) ──────────────
    "crm_source_type": ["recycler", "refurb", "endcust", "trader", "indiv", "online"],
    "crm_material_type": [
        "as_is_untested", "as_is_tested", "as_is_graded",
        "partially_refurb", "refurb_full", "scrap_parts", "bulk_mix",
    ],
    "crm_buyer_type": ["corp_buyer", "dealer", "online_seller", "export", "retail", "gov"],
    "crm_priority": ["low", "medium", "high", "urgent"],
    "crm_activity_type": ["call", "whatsapp", "visit", "email", "meeting", "note"],
    "crm_activity_outcome": [
        "interested", "not_interested", "callback", "order_placed",
        "no_answer", "followup", "done", "rescheduled",
    ],
    # ── Spare Parts ────────────────────────────────────────────────────────────
    "spare_parts_ram_action": ["removed", "added", "cannibalized"],
    "spare_parts_ram_gb": ["2", "4", "8", "12", "16", "24", "32", "64"],
    "spare_parts_consume_stage": ["L1", "L2", "L3", "IQC"],
    # ── Attendance ─────────────────────────────────────────────────────────────
    "attendance_status": ["present", "absent", "half_day", "late", "wfh"],
    # ── QA ─────────────────────────────────────────────────────────────────────
    "qa_environment": ["QA", "Staging", "Dev", "Production"],
    # ── Cosmetic / Final QC ────────────────────────────────────────────────────
    "cosmetic_final_qc_status": ["pass", "fail"],
    # ── IQC ────────────────────────────────────────────────────────────────────
    "iqc_r2v3_grade_category": ["C0", "C3", "C4", "C5"],
    # ── Parts: Part Category — shared by Add New Part, Add Line Item, Add
    #    Harvest Part, and the Device Detail New Request/Replace modal, so
    #    all four always offer the exact same option set. ────────────────────
    "iqc_part_category": [
        "RAM", "HDD", "SSD", "Screen", "Battery", "Charging Port", "Keyboard",
        "Touchpad", "HDMI Port", "USB Port", "Ethernet Port", "Audio Jack",
        "Speaker", "Wi-Fi", "Webcam", "DVD Drive", "Fan", "Hinge",
        "Motherboard", "Motherboard Parts", "Other",
    ],
}
