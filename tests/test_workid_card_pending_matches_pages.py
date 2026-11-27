"""WorkID Status cards (2026-11-26, Pankaj): Pending never counts trashed,
External Partner Test or inactive tags; the Started line is hidden."""
import asyncio
from pathlib import Path

import routers.workid_status as ws
from models.master import EXTERNAL_PARTNER_TEST_ENTITY


class _Res:
    def __init__(self, rows): self._rows = rows
    def all(self): return self._rows


class _DB:
    def __init__(self, rows): self.rows = rows
    async def execute(self, q): return _Res(self.rows)


def test_page_pending_rules(monkeypatch):
    devs = {  # id: (stage, active, trashed, entity)
        "a": ("l1", True, False, "OxyPC"),       # counted L1/L2
        "b": ("l1", False, False, "OxyPC"),      # inactive
        "c": ("l2", True, False, "OxyPC"),       # L2 retired
        "d": ("l3", True, False, None),          # counted L3/L4
        "e": ("l3", True, True, "OxyPC"),        # trashed
        "f": ("qc_check", True, False, EXTERNAL_PARTNER_TEST_ENTITY),  # ETP
        "g": ("qc_check", True, False, "OxyPC"),
        "h": ("painting", True, True, "OxyPC"),  # trashed
        "k": ("cleaning", True, False, "OxyPC"),
        "i": ("final_qc", True, False, EXTERNAL_PARTNER_TEST_ENTITY),
        "j": ("final_qc", True, False, None),
    }

    async def fake_stages_at(db, when, ids=None):   # trashed lots already left out
        return {k: v[0] for k, v in devs.items()}
    monkeypatch.setattr(ws, "_stages_at", fake_stages_at)
    db = _DB([(k, v[1], v[2], v[3]) for k, v in devs.items()])
    out = asyncio.run(ws._page_pending(db, None))
    assert out == {"l1l2": 1, "l3l4": 1, "stress": 1, "cosmetic": 1, "final_qc": 1}


def test_started_line_hidden():
    src = Path("templates/workid_status/list.html").read_text(encoding="utf-8")
    assert "'Started', 'data-k=\"started\"" not in src


def test_stage_pages_use_shared_rule():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy import and_
    from utils.stage_page_filters import live_tag_filters
    sql = str(and_(*live_tag_filters()).compile(dialect=postgresql.dialect(),
                                                compile_kwargs={"literal_binds": True}))
    assert "devices.is_trashed IS NOT true" in sql
    assert "devices.is_active IS NOT false" in sql
    assert "External Partner Test" in sql and "lots.is_trashed IS NOT true" in sql
    for f, n in (("routers/repair.py", 3), ("routers/qc.py", 1), ("routers/cosmetic.py", 2),
                 ("routers/sales.py", 2)):
        assert Path(f).read_text(encoding="utf-8").count("live_tag_filters(") == n, f


def test_etp_never_shown_even_with_entity_filter():
    src = Path("routers/cosmetic.py").read_text(encoding="utf-8")
    assert "live_tag_filters(skip_entity=True)" not in src
