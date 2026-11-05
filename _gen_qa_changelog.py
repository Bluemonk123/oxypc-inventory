"""Write qa_docs/changelog.txt from git history (2026-11-04).

Production runs in Docker without .git, so the QA Dashboard's change history
and weekly releases read this file there. Also writes qa_docs/test_index.txt
(date added|test file|first docstring line) for the weekly QA Test Cases
(services/qa_weekly.py, 2026-11-05). Run it after committing, then amend:
    python _gen_qa_changelog.py && git add qa_docs/changelog.txt && git commit --amend --no-edit
"""
import os
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
out = subprocess.run(["git", "log", "--since=2026-07-01 00:00:00", "--format=%h|%ad|%s",
                      "--date=format:%Y-%m-%d", "--no-merges"],
                     cwd=ROOT, capture_output=True, text=True, check=True).stdout
lines = [ln for ln in out.splitlines() if ln.count("|") >= 2]
path = os.path.join(ROOT, "qa_docs", "changelog.txt")
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(lines) + "\n")
print(f"{len(lines)} commits -> qa_docs/changelog.txt")

# Test files: the date each was added + its first docstring line (2026-11-05).
import ast
added = {}
log = subprocess.run(["git", "log", "--diff-filter=A", "--name-only", "--format=@@%ad", "--date=format:%Y-%m-%d",
                      "--", "tests/"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
day = None
for ln in log.splitlines():
    if ln.startswith("@@"):
        day = ln[2:]
    elif ln.startswith("tests/test_") and ln.endswith(".py") and day:
        added[ln] = day                      # oldest add wins (log is newest first)
rows = []
for path, day in added.items():
    full = os.path.join(ROOT, path)
    if not os.path.exists(full):
        continue
    try:
        doc = ast.get_docstring(ast.parse(open(full, encoding="utf-8").read())) or ""
    except SyntaxError:
        doc = ""
    first = " ".join(doc.strip().split("\n\n")[0].split())[:300].replace("|", "/")
    rows.append(f"{day}|{path}|{first}")
rows.sort(reverse=True)
with open(os.path.join(ROOT, "qa_docs", "test_index.txt"), "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(rows) + "\n")
print(f"{len(rows)} test files -> qa_docs/test_index.txt")
