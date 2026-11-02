#!/bin/bash
# ============================================================================
#  OxyPC STAGING (Mac) - update the CODE from GitHub   (2026-11-01)
#  Double-click this file (or: Terminal -> type  bash  + space, drag it in, Enter).
#
#  Replaces the code in ~/Documents/OxyPC-Staging/app with the latest code of
#  the GitHub branch you choose (Enter = staging). It does NOT touch:
#    .env and config.ini (this Mac's settings), uploads/, venv/, the database.
#  Run RESTORE_ON_MAC.command once first, so staging exists on this Mac.
#  First time only: GitHub asks you to sign in (username + a Personal Access
#  Token as the password, or use GitHub Desktop / "gh auth login").
# ============================================================================
ROOT="$HOME/Documents/OxyPC-Staging"
APP="$ROOT/app"
REPO_DEFAULT="https://github.com/Bluemonk123/oxypc-inventory.git"
step() { echo; echo "==> $1"; }
ok()   { echo "    OK  $1"; }
fail() { echo; echo "FAILED: $1"; echo; read -r -p "Press Enter to close" _; exit 1; }

[ -f "$APP/main.py" ] || fail "Staging is not on this Mac yet ($APP). Run RESTORE_ON_MAC.command from a backup first."
command -v git >/dev/null || fail "git is not installed. Run:  xcode-select --install   then run this file again."

read -r -p "Branch to pull [staging]: " BRANCH
BRANCH="${BRANCH:-staging}"

cd "$APP" || fail "Cannot open $APP"
if [ ! -d .git ]; then
  step "Linking this folder to GitHub (first time)"
  git init -q && git remote add origin "$REPO_DEFAULT" || fail "git init failed"
  ok "Linked to $REPO_DEFAULT"
fi
BEFORE="$(git rev-parse --short HEAD 2>/dev/null || echo none)"

step "Downloading branch '$BRANCH' from GitHub"
git fetch -q origin "$BRANCH" || fail "Could not download '$BRANCH'. Check the internet, the branch name, and your GitHub sign-in."
AFTER="$(git rev-parse --short "origin/$BRANCH")"
ok "Latest on GitHub: $AFTER  $(git log -1 --format='%s (%cd)' --date=short "origin/$BRANCH")"

step "Stopping staging app if running"
PIDS="$(lsof -ti tcp:8001 2>/dev/null)"; [ -n "$PIDS" ] && kill $PIDS 2>/dev/null; sleep 1
ok "Stopped (or was not running)"

step "Updating code  $BEFORE -> $AFTER"
# keep this Mac's own settings even if a branch ever ships its own copy
TMP="$(mktemp -d)"; for f in .env config.ini; do [ -f "$f" ] && cp "$f" "$TMP/"; done
git checkout -q -B "$BRANCH" "origin/$BRANCH" -f || { rm -rf "$TMP"; fail "Could not switch the code to $AFTER"; }
git reset -q --hard "origin/$BRANCH"
for f in .env config.ini; do [ -f "$TMP/$f" ] && cp "$TMP/$f" "$f"; done; rm -rf "$TMP"
ok "Code is now $AFTER (files you added yourself, uploads, venv, .env, config.ini are kept)"

step "Python packages"
VPY="$APP/venv/bin/python"
[ -x "$VPY" ] || fail "No Python environment in $APP/venv - run RESTORE_ON_MAC.command once first."
HASH="$(shasum requirements.txt | cut -d' ' -f1)"
if [ "$(cat venv/.requirements_hash 2>/dev/null)" != "$HASH" ]; then
  "$VPY" -m pip install -q -r requirements.txt || fail "pip install failed - check the internet and try again."
  echo "$HASH" > venv/.requirements_hash
  ok "Packages updated"
else
  ok "No package changes"
fi

echo
echo "DONE. Code $AFTER from GitHub '$BRANCH'. New database columns are added automatically on start."
if [ -x "$ROOT/START_STAGING.command" ]; then
  echo "Starting staging - open http://localhost:8001 (keep this window open)."
  exec "$ROOT/START_STAGING.command"
fi
read -r -p "Start staging with ~/Documents/OxyPC-Staging/START_STAGING.command. Press Enter to close" _
