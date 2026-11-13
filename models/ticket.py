import uuid
from sqlalchemy import Column, String, Text, DateTime, Integer, Boolean, ForeignKey, BigInteger
from sqlalchemy.dialects.postgresql import UUID
from database import Base
from utils.timezone import app_now


# Ticket Board (2026-11-13, Pankaj — schema approved in chat): the tickets
# raised from Raise Ticket / the login pages are worked on the Ticket Board
# (routers/ticket_board.py) like a helpdesk. Status was Open | Closed; it is
# now new | open | pending | hold | solved. Old rows are moved over at
# startup (services/ticket_board.migrate_legacy_statuses): Open -> new,
# Closed -> solved. Tickets Raised and the partner portal still show just
# Open / Closed (simple_status).
STATUSES = ("new", "open", "pending", "hold", "solved")
PRIORITIES = ("urgent", "high", "normal")          # no "low", by spec
TYPES = ("new issue", "new feedback", "new feature", "new task")
CHANNELS = ("web", "email", "agent")


class Ticket(Base):
    __tablename__ = "tickets"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id = Column(String(10), unique=True, nullable=False, index=True)
    status = Column(String(10), nullable=False, default="new")  # new | open | pending | hold | solved
    raised_by = Column(String(50), nullable=False, index=True)
    raised_on = Column(DateTime, nullable=False, default=app_now)
    feedback = Column(Text, nullable=False)
    # Admin's closing note — captured on the Close action so a ticket is never
    # closed without a reason attached.
    notes = Column(Text, nullable=True)
    # Up to two screenshots attached when raising. Stored as /uploads/... paths,
    # not blobs, matching how every other attachment in this app is handled.
    # Additive and nullable, so db_validator provisions them at startup.
    photo1_path = Column(String(255), nullable=True)
    photo2_path = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)

    # ── Ticket Board fields (2026-11-13), all optional ─────────────────────
    subject = Column(String(200), nullable=True)            # blank → first line of feedback
    priority = Column(String(10), nullable=True)            # urgent | high | normal (blank = normal)
    ticket_type = Column(String(20), nullable=True)         # new issue | new feedback | new feature | new task
    group_name = Column(String(50), nullable=True)          # Master Data 'ticket_group' (Development, Support)
    channel = Column(String(10), nullable=True)             # web | email | agent (blank = web)
    assignee = Column(String(50), nullable=True, index=True)  # username of the engineer handling it
    requester_email = Column(String(150), nullable=True)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)  # linked asset (phase 6)
    tags = Column(String(500), nullable=True)               # comma-separated
    sla_due_at = Column(DateTime, nullable=True)
    hold_started_at = Column(DateTime, nullable=True)       # SLA clock paused since
    sla_paused_seconds = Column(Integer, nullable=True)
    solved_at = Column(DateTime, nullable=True)

    @property
    def simple_status(self) -> str:
        """Open / Closed for Tickets Raised and the partner portal."""
        return "Closed" if self.status in ("solved", "Closed") else "Open"

    @property
    def display_subject(self) -> str:
        if self.subject:
            return self.subject
        text = (self.feedback or "").strip()
        if text.startswith("[From ") and "] " in text:
            text = text.split("] ", 1)[1]
        line = text.splitlines()[0] if text else ""
        return (line[:97] + "...") if len(line) > 100 else (line or "(no subject)")


class TicketMessage(Base):
    """A reply, an internal note, or an activity-log event on a ticket.
    author_type: requester | user | system. The ticket's own feedback text
    is its first message and is not copied here."""
    __tablename__ = "ticket_messages"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id = Column(UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    author_type = Column(String(10), nullable=False, default="user")
    author_username = Column(String(50), nullable=True)
    author_name = Column(String(100), nullable=True)
    body = Column(Text, nullable=True)
    is_internal = Column(Boolean, nullable=False, default=False)
    email_message_id = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=app_now, index=True)


class TicketAttachment(Base):
    """A file on a ticket message (phase 3)."""
    __tablename__ = "ticket_attachments"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id = Column(UUID(as_uuid=True), ForeignKey("ticket_messages.id", ondelete="CASCADE"), nullable=False, index=True)
    file_name = Column(String(255), nullable=False)
    mime_type = Column(String(100), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    path = Column(String(255), nullable=False)
    created_at = Column(DateTime, nullable=False, default=app_now)


class TicketMacro(Base):
    """A saved reply (phase 2). body_template understands
    {customer_first_name}, {agent_first_name}, {ticket_id}."""
    __tablename__ = "ticket_macros"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    body_template = Column(Text, nullable=False)
    set_status = Column(String(10), nullable=True)
    set_group = Column(String(50), nullable=True)
    is_internal = Column(Boolean, nullable=False, default=False)
    active = Column(Boolean, nullable=False, default=True)
    display_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=app_now)
    updated_at = Column(DateTime, default=app_now, onupdate=app_now)
