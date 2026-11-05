"""QA weekly tracking (2026-11-05, Pankaj): Requirements / Test Cases / Defects
kept current from the commit history — one requirement per week (REQ-YYYY-Www,
Done when the week is over, In Progress for the current one), a test case per
test file added that week, a defect per bug fix. Idempotent."""
import asyncio
from datetime import date

from services.qa_weekly import _FIX, _code, _module, _split_fixes, _week_start, weekly_plan


def test_rules():
    assert _week_start(date(2026, 11, 5)) == date(2026, 11, 2) and _code(date(2026, 11, 2)) == "2026-W45"
    assert _split_fixes("Trash delete fix; Parts Edit modal") == ["Trash delete fix"]
    assert _split_fixes("Model Name data fix + save rule") == []      # "data fix" is a feature
    assert not _FIX.search("Data Fixes merge") and _FIX.search("Fix 500 on New Tag Sale")
    assert _module("WorkID Status: Final QC Pass export") == "Quality Check"
    assert _module("Spare parts crate rows") == "Spare Parts"


def test_plan_covers_every_week_since_cutoff():
    plan = weekly_plan(date(2026, 11, 5))
    assert plan and all(w["end"] > w["start"] for w in plan)
    assert [w["current"] for w in plan].count(True) <= 1
    codes = [w["code"] for w in plan]
    assert codes == sorted(set(codes))                                 # one entry per week, in order
    assert sum(len(w["commits"]) for w in plan) > 0


def test_idempotent_against_the_database():
    from database import AsyncSessionLocal
    from services.qa_weekly import auto_track_weekly_qa

    async def run():
        async with AsyncSessionLocal() as db:
            await auto_track_weekly_qa(db)
        async with AsyncSessionLocal() as db:
            return await auto_track_weekly_qa(db)
    again = asyncio.run(run())
    assert again == {"requirements": 0, "test_cases": 0, "defects": 0}
