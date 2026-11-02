#!/bin/bash
# ============================================================================
#  OxyPC STAGING (Mac) - load a PRODUCTION backup into the staging database
#  (2026-11-01)  Double-click this file (or: Terminal -> type  bash  + space,
#  drag it in, Enter).
#
#  1. On production: Admin > Users > Backups > download the newest
#     oxypc_YYYYMMDD_HHMMSS.sql.gz  (it lands in Downloads).
#  2. Double-click this file. It takes the newest oxypc_*.sql.gz from this
#     folder or from ~/Downloads and REPLACES the staging database with it.
#  Code, uploads, .env and config.ini are not touched; production is never
#  touched. Run RESTORE_ON_MAC.command once first, so staging exists here.
# ============================================================================
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HOME/Documents/OxyPC-Staging"
APP="$ROOT/app"
PORT=5433
PGDATA="$ROOT/pgdata"
step() { echo; echo "==> $1"; }
ok()   { echo "    OK  $1"; }
fail() { echo; echo "FAILED: $1"; echo; read -r -p "Press Enter to close" _; exit 1; }

[ -f "$APP/.env" ] && [ -f "$PGDATA/PG_VERSION" ] || fail "Staging is not on this Mac yet. Run RESTORE_ON_MAC.command from a backup zip first."
SUPERFILE="$ROOT/staging_db_superuser_password.txt"
[ -f "$SUPERFILE" ] || fail "staging_db_superuser_password.txt not found in $ROOT"

# newest production backup in this folder or Downloads
GZ="$(ls -t "$HERE"/oxypc_*.sql.gz "$HOME"/Downloads/oxypc_*.sql.gz 2>/dev/null | head -1)"
[ -n "$GZ" ] || fail "No oxypc_*.sql.gz found here or in Downloads. Download one from production (Admin > Users > Backups) first."

# staging database name / user from app/.env
URL="$(grep -E '^OXYPC_DATABASE_URL=' "$APP/.env" | head -1 | cut -d= -f2- | tr -d '\r')"
REST="${URL#*://}"; CREDS="${REST%%@*}"; HOSTPART="${REST#*@}"
DBUSER="${CREDS%%:*}"; DB="${HOSTPART#*/}"; DB="${DB%%\?*}"
[ -n "$DBUSER" ] && [ -n "$DB" ] || fail "Could not read the staging database from app/.env"
case "$DB" in *staging*) ;; *) fail "app/.env points at '$DB', not a staging database - refusing.";; esac

echo "OxyPC STAGING - load production backup"
echo "  Backup  : $(basename "$GZ")  ($(du -h "$GZ" | cut -f1))  from $(dirname "$GZ")"
echo "  Into    : staging database $DB on this Mac (port $PORT)"
echo
echo "All current STAGING data on this Mac will be replaced. Production is not touched."
read -r -p "Type YES to continue: " ans
[ "$ans" = "YES" ] || fail "Cancelled. Nothing was changed."

gzip -t "$GZ" 2>/dev/null || fail "$(basename "$GZ") is incomplete or damaged - download it again."

PGBIN=""
for d in "$(dirname "$(command -v pg_ctl 2>/dev/null)" 2>/dev/null)" \
         /opt/homebrew/opt/postgresql@1{8,7,6,5}/bin /usr/local/opt/postgresql@1{8,7,6,5}/bin \
         /opt/homebrew/opt/postgresql/bin /usr/local/opt/postgresql/bin \
         /Applications/Postgres.app/Contents/Versions/latest/bin; do
  [ -n "$d" ] && [ -x "$d/pg_ctl" ] && { PGBIN="$d"; break; }
done
[ -n "$PGBIN" ] || fail "PostgreSQL is not installed. Install it with:  brew install postgresql@17"

step "Stopping staging app if running"
PIDS="$(lsof -ti tcp:8001 2>/dev/null)"; [ -n "$PIDS" ] && kill $PIDS 2>/dev/null; sleep 1
ok "Stopped (or was not running)"

step "Staging database server (port $PORT)"
if ! "$PGBIN/pg_isready" -h localhost -p $PORT -q; then
  "$PGBIN/pg_ctl" -D "$PGDATA" -l "$ROOT/pgdata.log" -o "-p $PORT -k /tmp" -w start >/dev/null || fail "Staging database would not start. See $ROOT/pgdata.log"
fi
export PGPASSWORD="$(sed 's/.*: //' "$SUPERFILE" | tr -d '\r\n ')"
PSQL=("$PGBIN/psql" -U postgres -h localhost -p $PORT)
"${PSQL[@]}" -tAc "SELECT 1" >/dev/null 2>&1 || fail "Could not sign in to the staging database server."
ok "Running"

step "Recreating empty staging database"
"${PSQL[@]}" -q -c "DROP DATABASE IF EXISTS \"$DB\" WITH (FORCE);"
"${PSQL[@]}" -q -c "CREATE DATABASE \"$DB\" OWNER \"$DBUSER\" ENCODING 'UTF8' TEMPLATE template0;" || fail "Could not create the staging database."
ok "Empty database ready"

step "Loading the backup (a few minutes)"
LOG="$ROOT/load_backup_log.txt"
# SET ROLE: tables are owned by the staging user; production's OWNER lines then
# fail harmlessly. Newer pg_dump \restrict lines are dropped for older psql.
gzip -dc "$GZ" | grep -v -E '^\\(un)?restrict ' | \
  "${PSQL[@]}" -d "$DB" -q -v ON_ERROR_STOP=0 -c "SET ROLE \"$DBUSER\";" -f - > "$LOG" 2>&1
grep "ERROR" "$LOG" | grep -v -E "role .* does not exist|must be able to SET ROLE|must be owner of|transaction_timeout" | head -15 | sed 's/^/    /'
ok "Loaded (details in load_backup_log.txt; warnings above, if any, are usually harmless)"

step "Safety: outbound webhooks off in STAGING"
"${PSQL[@]}" -d "$DB" -q -c "DO \$\$ BEGIN IF to_regclass('public.webhooks') IS NOT NULL THEN UPDATE webhooks SET is_active = false; END IF; END \$\$;"
"${PSQL[@]}" -d "$DB" -q -c "ANALYZE;"
for t in users lots devices sales; do
  n="$("${PSQL[@]}" -d "$DB" -tAc "SELECT count(*) FROM $t" 2>/dev/null)"; printf "    %-8s %s\n" "$t" "${n:-n/a}"
done
unset PGPASSWORD
ok "Done"

echo
echo "DONE. Staging now has production data from $(basename "$GZ")."
echo "Sign in with your PRODUCTION username and password."
if [ -x "$ROOT/START_STAGING.command" ]; then
  echo "Starting staging - open http://localhost:8001 (keep this window open)."
  exec "$ROOT/START_STAGING.command"
fi
read -r -p "Press Enter to close" _
