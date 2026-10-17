import uuid
from datetime import datetime
from utils.timezone import app_now
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Enum as SAEnum, UniqueConstraint
from sqlalchemy.types import TypeDecorator
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum
from database import Base



class UserRole(str, enum.Enum):
    admin = "admin"
    inventory_manager = "inventory_manager"
    iqc_inspector = "iqc_inspector"
    l1_engineer = "l1_engineer"
    l2_engineer = "l2_engineer"
    l3_engineer = "l3_engineer"
    qc_inspector = "qc_inspector"
    sales = "sales"
    spare_parts_manager = "spare_parts_manager"
    telecaller = "telecaller"
    sales_manager = "sales_manager"
    cosmetic_manager = "cosmetic_manager"


ROLE_LABELS = {
    UserRole.admin: "Admin",
    UserRole.inventory_manager: "Inventory Manager",
    UserRole.iqc_inspector: "IQC Handler",
    UserRole.l1_engineer: "L1/L2 Engineer",
    UserRole.l2_engineer: "L2 Engineer",
    UserRole.l3_engineer: "L3/L4 Engineer",
    UserRole.qc_inspector: "QC Handler",
    UserRole.sales: "Sourcing Sales",
    UserRole.spare_parts_manager: "Parts Manager",
    UserRole.telecaller: "Telecaller Sales",
    UserRole.sales_manager: "Sales Manager",
    UserRole.cosmetic_manager: "Cosmetic Manager",
}


class RoleValue(str):
    """A role string that also exposes `.value`, so existing code using
    `current_user.role.value` and `role == UserRole.x` keeps working now that
    roles are free-text (custom roles allowed)."""
    @property
    def value(self):
        return str(self)


class RoleType(TypeDecorator):
    """Persist role as text (so custom roles are allowed) but read it back as a
    RoleValue, preserving `.value` access and equality with UserRole members."""
    impl = String(60)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return getattr(value, "value", None) or str(value)

    def process_result_value(self, value, dialect):
        return RoleValue(value) if value is not None else None


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint('username', name='uq_users_username'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(50), nullable=False, index=True)
    full_name = Column(String(100), nullable=False)
    password_hash = Column(String(200), nullable=False)
    role = Column(RoleType(), nullable=False, default="sales")
    status = Column(Boolean, default=True)
    created_at = Column(DateTime, default=app_now)
    created_by = Column(String(50), nullable=True)
    last_login = Column(DateTime, nullable=True)

    # SaaS tenant identifier — "oxypc_internal" for all current users.
    # When multi-tenancy is added, each customer gets their own value
    # (e.g. "customer_a") so their users are isolated at the data layer.
    tenant = Column(String(50), nullable=True, default="oxypc_internal", index=True)

    # Reporting hierarchy for sales_manager team rollups (used by mobile telecalling).
    manager_username = Column(String(50), ForeignKey("users.username"), nullable=True, index=True)

    # WhatsApp number this user links in the WhatsApp module (per-user session verify/sync).
    whatsapp_number = Column(String(20), nullable=True)

    email = Column(String(150), nullable=True)

    # Free-text job title, and org-chart "reports to" (distinct from
    # manager_username above, which drives Attendance Group rollups only —
    # kept separate so this field never silently changes that feature).
    designation = Column(String(100), nullable=True)
    reports_to = Column(String(50), ForeignKey("users.username"), nullable=True, index=True)

    # Entity the user belongs to (Master Data entity name, same values as
    # devices.entity). Approved 2026-10-17. Nullable in the DB so existing
    # users load; the Add/Edit User form makes it required. Added by
    # db_validator (ADD COLUMN IF NOT EXISTS) at startup.
    entity = Column(String(50), nullable=True, index=True)

    login_logs = relationship("LoginLog", back_populates="user", lazy="select")

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)

    @property
    def is_active(self):
        return self.status


class LoginLog(Base):
    __tablename__ = "login_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    action = Column(String(50), nullable=False)
    ip_address = Column(String(50), nullable=True)
    timestamp = Column(DateTime, default=app_now)
    notes = Column(String(200), nullable=True)

    user = relationship("User", back_populates="login_logs")


class UserPermission(Base):
    __tablename__ = "user_permissions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    permission = Column(String(100), nullable=False)  # e.g. "can_approve_sales", "can_view_reports"
    granted = Column(Boolean, default=True)
    granted_by = Column(String(50), nullable=True)
    granted_at = Column(DateTime, default=app_now)

    user = relationship("User", foreign_keys=[user_id])
