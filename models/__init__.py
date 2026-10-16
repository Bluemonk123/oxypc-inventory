from .user import User, LoginLog, UserPermission
from .lot import Lot
from .device import Device, StageMovement
from .repair import RepairJob
from .qc import QCCheck
from .sales import Sale, Return
from .company import Company
from .spare_parts import SparePart, SparePartPurchase, SparePartConsumption, RAMTracking
from .master import MasterData
from .attendance import Attendance
from .dealers import Dealer, DealerAssignment, DealerCall, DealerOrder, DealerCreditNote
from .telecalling import TelecallingSession, TelecallingRecord
from .whatsapp import WhatsAppSession, WhatsAppMessage
# R-OS v2.0 engine models
from .stage_control import StageMaster, AllowedTransition
from .engines import RepairAttempt, DeviceCosting, SparePartsLedger, DeviceAging, AuditLog
# Location tracking
from .location import StorageLocation, DeviceLocationLog, InventoryAudit, AuditScanItem
# Previously missing from __init__ — Alembic needs ALL models imported to manage their tables
from .iqc_inspection import IQCInspection
from .stock_transfer import StockTransfer
from .work_order import WorkOrder
from .part_request import PartRequest, PartSourcingRequest
from .pna_part import DevicePNAPart
from .cosmetic_flow import CosmeticFlowRow
from .part_estimate import PartEstimate, PartEstimateLine, PartEstimateOpenLine
from .dispatch_request import TelecallerDispatchRequest
from .stock_validation import StockValidation
from .grn_import import GRNImport
from .market import MarketAvailability
# QA / UAT tracking module
from .qa_uat import (
    QARequirement, QATestCase, QATestExecution,
    QADefect, QAUATScenario, QARelease,
)
# CRM module
from .crm import (
    CRMContact, CRMSourcingDeal, CRMSalesOpportunity,
    CRMQuote, CRMQuoteItem, CRMActivity, GradePriceMatrix,
    CRMPurchaseOrder, CRMPOLineItem,
    SupplierPayment, CustomerReceipt,
)
from .crm_team import CRMContactTeamMember
from .scrap_for_sale import ScrapForSale
from .scrap_sale import ScrapSale, ScrapSaleItem
from .settings import AppSetting
from .api_key import APIKey
from .webhook import Webhook
from .event_log import EventLog
from .cost_config import CostConfig
from .notification import Notification
from .stress import StressTestResult
from .bucket import Bucket
# Trade Partner portal (dealer-facing B2B)
from .partner import (
    PartnerListing, PartnerListingDevice, PartnerFloorConfig,
    PartnerBooking, PartnerPaymentProof, PartnerSetting,
    PartnerLoginLog, PartnerListingView,
)
# Customer Care Agent (post-sale warranty/diagnostics/ticketing)
from .care import (
    CareDevicePairing, CareWarranty, CareSupportTicket, CareTicketEvent,
    CareDiagnosticSnapshot, CareOffer, CareOfferDelivery, CareAgentEvent,
    CareAuditLog, CareDispatchException,
)
# Physical Audits (Report Date x System Stage x System Location tag audits) —
# distinct from InventoryAudit/AuditScanItem above (the zone/scan-batch tool)
from .audit_record import AuditRecord, AuditRecordItem
from .block_tags import BlockRecord, BlockRecordItem, BlockItemStatus
from .as_is_lot_price import AsIsLotPrice
