"""QA tracking kept current, one week at a time (2026-11-05, Pankaj).

The QA Requirements / Test Cases / Defects pages are database records that
used to be added by hand-run seed scripts, so they stopped at 16 July
(REQ-255) while the work went on. Releases already track themselves
(routers/qa_uat.py auto_track_releases). This does the same for the rest,
from the same real data:

  * qa_docs/changelog.txt — every commit (sha|date|subject), written by
    _gen_qa_changelog.py before each push (production has no .git);
  * qa_docs/test_index.txt — every test file with the date it was added and
    its first docstring line (date|path|summary), written by the same script.

Per week (Monday-Sunday) after CUTOFF:
  * Requirement  REQ-2026-W45   one per week — the week's changes listed;
                 Done once the week is over, In Progress for the current week
                 (refreshed on every startup until the week closes);
  * Test Cases   TC-2026-W45-01…  one per automated test file added that week,
                 linked to the week's requirement;
  * Defects      BUG-2026-W45-01… one per bug fix in that week's commits
                 (a commit listing several items is split on ";"), Closed.

Idempotent by code; called from main.py's startup event after the releases.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from datetime import date, datetime, timedelta

CUTOFF = "2026-07-16"            # the last hand-seeded requirement's date (REQ-255)
CREATED_BY = "system-auto"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_INDEX = os.path.join(_ROOT, "qa_docs", "test_index.txt")

_FIX = re.compile(r"(?<!data )(?<!-)\b(fix(es|ed)?|bug|broken|not (working|saving|showing|opening)|crash|error|500|wrong|missing|"
                  r"failed|failing|regression)\b", re.I)

# keyword -> module (first match wins; checked against the change text)
_MODULES = [
    (r"\bqa\b|requirement|test case|journey|uat|release", "QA"),
    (r"final qc|stress|\bqc\b|pre-sale", "Quality Check"),
    (r"\biqc\b|oxyqc|inspection", "IQC"),
    (r"cosmetic|paint", "Cosmetic"),
    (r"\bl1\b|\bl2\b|\bl3\b|\bl4\b|repair|engineer|workid|work id|production manager", "Repair & Production"),
    (r"spare|part[s ]|harvest|crate|\bpna\b|parts grn", "Spare Parts"),
    (r"\bgrn\b|lot\b|re-?grn|stock in", "GRN & Lots"),
    (r"sale|invoice|quotation|dealer|telecall|dispatch|block tag", "Sales & Dealers"),
    (r"crm|contact|sourcing|opportunit", "CRM"),
    (r"ticket|care|warranty|return", "Care & Returns"),
    (r"trash|inventory|stock|transfer|location|audit|bucket|scrap", "Inventory"),
    (r"pric|calculator|hardware|competitor", "Pricing"),
    (r"permission|role|rbac|login|user|attendance|csrf", "Users & Access"),
    (r"dashboard|tile|report|export|global table|date", "Dashboards & Reports"),
]


def _module(text: str) -> str:
    t = text.lower()
    for rx, name in _MODULES:
        if re.search(rx, t):
            return name
    return "Cross-module"


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _code(ws: date) -> str:
    y, w, _ = ws.isocalendar()
    return f"{y}-W{w:02d}"


def _commits(after: str) -> list:
    """[(date, subject)] oldest first, from git or qa_docs/changelog.txt."""
    from routers.qa_uat import _auto_commits_since
    # _auto_commits_since strips "fix:" style prefixes into a category; keep
    # the category so a "fix:" commit still counts as a fix.
    out = []
    for e in _auto_commits_since(after):
        if e["date"] <= after:
            continue
        msg = e["msg"]
        if e.get("category") == "Bug Fix" and not _FIX.search(msg):
            msg = "Fix: " + msg
        out.append((date.fromisoformat(e["date"]), msg))
    return sorted(out)


def _tests(after: str) -> list:
    """[(date, path, summary)] for test files added after `after`."""
    rows = []
    try:
        with open(TEST_INDEX, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("|", 2)
                if len(parts) == 3 and parts[0] > after:
                    rows.append((date.fromisoformat(parts[0]), parts[1], parts[2]))
    except (OSError, ValueError):
        return []
    return sorted(rows)


def _split_fixes(subject: str) -> list:
    parts = [p.strip(" .") for p in re.split(r";\s+|\s+\+\s+", subject) if p.strip(" .")]
    return [p for p in parts if _FIX.search(p)]


def _title_from_test(path: str, summary: str) -> str:
    if summary:
        return summary[:200]
    name = os.path.basename(path)[5:-3].replace("_", " ")
    return name[:1].upper() + name[1:200]


def weekly_plan(today: date | None = None) -> list:
    """One dict per week with commits after CUTOFF: {start, end, code,
    current, commits:[(date, subject)], tests:[(date, path, summary)],
    fixes:[(date, text)]}. Pure — no database."""
    today = today or date.today()
    weeks: dict = {}
    for d, s in _commits(CUTOFF):
        weeks.setdefault(_week_start(d), {"commits": [], "tests": []})["commits"].append((d, s))
    for d, p, summ in _tests(CUTOFF):
        weeks.setdefault(_week_start(d), {"commits": [], "tests": []})["tests"].append((d, p, summ))
    this_week = _week_start(today)
    plan = []
    for ws in sorted(weeks):
        w = weeks[ws]
        fixes = [(d, f) for d, s in w["commits"] for f in _split_fixes(s)]
        plan.append({"start": ws, "end": ws + timedelta(days=6), "code": _code(ws),
                     "current": ws >= this_week, "commits": w["commits"], "tests": w["tests"],
                     "fixes": fixes})
    return plan


async def auto_track_weekly_qa(db) -> dict:
    """Create / refresh the weekly Requirement, Test Cases and Defects.
    Returns {"requirements": n, "test_cases": n, "defects": n} created."""
    from sqlalchemy import select
    from models.qa_uat import (
        QADefect, QARelease, QARequirement, QATestCase, DefectPriority, DefectSeverity, DefectStatus,
        RequirementPriority, RequirementSource, RequirementStatus, TestCaseStatus, TestCaseType,
    )
    from utils.timezone import app_now

    plan = weekly_plan(app_now().date())
    if not plan:
        return {"requirements": 0, "test_cases": 0, "defects": 0}
    now = app_now()
    reqs = {r.req_code: r for r in (await db.execute(
        select(QARequirement).where(QARequirement.req_code.like("REQ-____-W%")))).scalars().all()}
    tcs = set((await db.execute(select(QATestCase.tc_code).where(
        QATestCase.tc_code.like("TC-____-W%")))).scalars().all())
    bugs = set((await db.execute(select(QADefect.defect_code).where(
        QADefect.defect_code.like("BUG-____-W%")))).scalars().all())
    releases = [(r.version, r.planned_date, r.release_date) for r in (await db.execute(
        select(QARelease).where(QARelease.release_date.isnot(None)))).scalars().all()]

    def _release_for(w):
        for v, p, r in releases:
            if r and w["start"] <= r.date() <= w["end"]:
                return v
        return None

    made = {"requirements": 0, "test_cases": 0, "defects": 0}
    for w in plan:
        code = w["code"]
        texts = [s for _d, s in w["commits"]]
        mods = Counter(_module(t) for t in texts) or Counter({"Cross-module": 1})
        top = [m for m, _n in mods.most_common(3) if m != "Cross-module"] or ["Cross-module"]
        label = f"{w['start'].strftime('%d %b')} – {w['end'].strftime('%d %b')}"
        rel = _release_for(w)
        title = (f"Week {code[-2:]} ({label}): {len(texts)} changes — {', '.join(top)}")[:200]
        lines = [f"Work delivered {label} {w['start'].year}"
                 + (f" (release {rel})" if rel else "") + ":"]
        lines += [f"• {d.strftime('%d %b')} — {s}" for d, s in w["commits"]]
        if w["tests"]:
            lines += ["", f"Automated tests added: {len(w['tests'])}"]
        if w["fixes"]:
            lines += [f"Bug fixes: {len(w['fixes'])} (see Defects BUG-{code}-…)"]
        desc = "\n".join(lines)
        status = RequirementStatus.in_progress if w["current"] else RequirementStatus.done
        created_at = datetime.combine(w["end"] if not w["current"] else w["start"], datetime.min.time()).replace(hour=18)
        rcode = f"REQ-{code}"
        req = reqs.get(rcode)
        if req is None:
            req = QARequirement(req_code=rcode, title=title, description=desc, module=top[0],
                                source=RequirementSource.change_req, priority=RequirementPriority.medium,
                                status=status, created_by=CREATED_BY, created_at=created_at, updated_at=now)
            db.add(req)
            await db.flush()
            reqs[rcode] = req
            made["requirements"] += 1
        elif req.created_by == CREATED_BY and (req.status != status or req.description != desc):
            # the week was still running last time — refresh it (never an edited one)
            req.title, req.description, req.module, req.status, req.updated_at = title, desc, top[0], status, now

        for i, (d, path, summ) in enumerate(w["tests"], 1):
            tcode = f"TC-{code}-{i:02d}"
            if tcode in tcs:
                continue
            db.add(QATestCase(
                tc_code=tcode, requirement_id=req.id, title=_title_from_test(path, summ),
                scenario=(summ or "") + f"\nAutomated test: {path}",
                steps=f"pytest -q {path}", expected_result="All tests in the file pass.",
                type=TestCaseType.regression, status=TestCaseStatus.active, is_automated=True,
                module=_module(path + " " + (summ or "")), created_by=CREATED_BY,
                created_at=datetime.combine(d, datetime.min.time()).replace(hour=12), updated_at=now))
            tcs.add(tcode)
            made["test_cases"] += 1

        for i, (d, text) in enumerate(w["fixes"], 1):
            bcode = f"BUG-{code}-{i:02d}"
            if bcode in bugs:
                continue
            at = datetime.combine(d, datetime.min.time()).replace(hour=12)
            db.add(QADefect(
                defect_code=bcode, title=text[:200], description=text,
                severity=DefectSeverity.medium, priority=DefectPriority.p3, status=DefectStatus.closed,
                module=_module(text), environment="Production", build_version=rel,
                resolution="Fixed in this week's deploy (from the commit history).",
                reported_by=CREATED_BY, reported_at=at, resolved_at=at, closed_at=at))
            bugs.add(bcode)
            made["defects"] += 1
    await db.commit()
    return made
