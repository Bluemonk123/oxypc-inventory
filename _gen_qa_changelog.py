"""Write qa_docs/changelog.txt from git history (2026-11-04).

Production runs in Docker without .git, so the QA Dashboard's change history
and weekly releases read this file there. Run it after committing, then amend:
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
