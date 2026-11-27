"""WorkID Status dashboard (2026-11-27, Pankaj): counts tags (each once), uses
the View Data cards' Pending / Scrap / Returned, and leaves out External
Partner Test, trashed and inactive tags."""
from pathlib import Path

import services.workid_dash as wd

SRC = Path("services/workid_dash.py").read_text(encoding="utf-8")


def test_uses_cards_and_live_tags():
    assert "_build_dash(db, stage_tiles)" in Path("routers/workid_status.py").read_text(encoding="utf-8")
    assert "Pending by stage" in SRC and "Open WorkIDs by stage" not in SRC
    assert SRC.count("live_tag_filters()") >= 2            # tags in work + WorkID counts
    assert "func.count(func.distinct(WorkOrder.device_id))" in SRC   # Completed today = tags


def test_card_groups_match_the_cards():
    assert [g[1] for g in wd.CARD_GROUPS] == ["l1l2", "l3l4", "stress", "cosmetic", "final_qc"]
    stages = {s for g in wd.CARD_GROUPS for s in g[4]}
    assert stages == {"l1", "l3", "qc_check", "final_qc", "cosmetic_received", "cleaning", "putty",
                      "dry_sanding", "masking", "painting", "water_sanding", "cosmetic_completed"}
