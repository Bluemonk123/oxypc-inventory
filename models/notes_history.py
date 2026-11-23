"""Notes History (2026-11-23, Pankaj — approved new table).

Every note typed anywhere (TRC flow, returns, dealers, contacts, quotations,
orders, deals, telecalling) is added here as a new row, so a note that the
screen keeps as one "latest" value (Repair Notes, Stress Fail, Final QC
Fail / Pass, IQC Remarks, ...) never loses its earlier rounds. The original
columns keep working as before. Rows are written by services/notes_history.py
on every save — never edited.

record_type / record_id = the record the note belongs to, at the level people
look it up by: a tag's TRC notes are under its device, a dealer's call /
order / credit-note notes under the dealer, a deal's step notes under the
deal. device_id is filled whenever the note concerns a tag (returns,
telecalling rows with a tag) so a tag's whole history can be pulled.
"""
import uuid

from sqlalchemy import Boolean, Column, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID

from database import Base
from utils.timezone import app_now


class NotesHistory(Base):
    __tablename__ = "notes_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_type = Column(String(30), nullable=False)      # device / dealer / contact / quotation / ...
    record_id = Column(String(64), nullable=False)
    device_id = Column(UUID(as_uuid=True), nullable=True)   # tag-related notes (no FK: history outlives the row)
    note_type = Column(String(60), nullable=False)        # "Final QC Fail Notes", "Dealer Call Remark", ...
    note_text = Column(Text, nullable=False)
    category = Column(String(60), nullable=True)          # Hardware / Software / Cosmetic, Pass / Fail, ...
    stage = Column(String(40), nullable=True)             # tag's stage when written (TRC notes)
    written_by = Column(String(100), nullable=True)
    written_at = Column(DateTime, nullable=False, default=app_now)
    source = Column(String(120), nullable=True)           # screen / field it came from
    backfilled = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_notes_history_record", "record_type", "record_id"),
        Index("ix_notes_history_device", "device_id"),
        Index("ix_notes_history_written_at", "written_at"),
    )
