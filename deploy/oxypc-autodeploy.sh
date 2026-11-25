#!/bin/bash
# OxyPC auto-deploy — install to /usr/local/bin/oxypc-autodeploy
#
#   sudo cp deploy/oxypc-autodeploy.sh /usr/local/bin/oxypc-autodeploy
#   sudo chmod 755 /usr/local/bin/oxypc-autodeploy
#
# Polls origin/main and deploys when it moves. Polling rather than a webhook
# because this box has a private address (10.199.206.109) that GitHub cannot
# reach; the poll is an OUTBOUND fetch, so it works behind NAT with no inbound
# firewall rule and no public exposure.
#
# Exits silently and cheaply when there is nothing to do — a `git fetch` with
# no new commits is a few KB, so a short poll interval costs nothing.
#
# Watch it:  journalctl -u oxypc-autodeploy -f
set -uo pipefail

APP=/opt/oxypc
cd "$APP" || { echo "FATAL: $APP missing"; exit 1; }

git fetch --quiet origin main 2>/dev/null || { echo "fetch failed — network or deploy key problem"; exit 1; }

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0                      # up to date — say nothing, keep the journal readable
fi

# Refuse to deploy over uncommitted local edits. Someone hand-editing a file on
# the server is either debugging or has made a change that exists nowhere else;
# silently overwriting it would destroy work and hide the divergence.
if [ -n "$(git status --porcelain)" ]; then
    echo "REFUSING TO DEPLOY: working tree has uncommitted changes."
    git status --porcelain | head -10
    echo "Resolve by committing, or discarding deliberately, then the next poll will deploy."
    exit 1
fi

echo "deploying ${LOCAL:0:7} -> ${REMOTE:0:7}"
git log --oneline "$LOCAL..$REMOTE" | sed 's/^/  incoming: /'

# Record the previous SHA so a rollback is one obvious command rather than
# archaeology. Deliberately NOT auto-rolling-back on failure: a rollback loop
# would flap every poll and mask the real fault. Fail loudly instead.
echo "$LOCAL" > /var/lib/oxypc-last-good-sha 2>/dev/null || true

git merge --ff-only origin/main --quiet || { echo "FATAL: fast-forward failed"; exit 1; }

# No separate pip-install or schema-reconcile step here (the bare-metal
# version of this script had both). Both are handled automatically now:
#  - dependency changes: `docker compose build` COPYs requirements.txt before
#    RUN pip install, so Docker's own layer cache already skips the reinstall
#    when it's unchanged and redoes it when it is — no manual diffing needed.
#  - schema reconciliation (db_validator.validate_and_fix): runs on every
#    app startup inside the container itself (see main.py), so it happens
#    automatically whenever the new container starts below.
#
# chown is also gone. The bare-metal version chowned the whole tree to
# www-data because that's who ran the process. The container runs as its own
# appuser (uid 1000) baked into the image — code files are COPYd in at build
# time regardless of host ownership. The only host ownership that matters is
# on the bind-mounted uploads/backups/static/stress_reports dirs, and those
# only need to be uid 1000 once, not on every deploy (set during the Docker
# cutover on 2026-10-01 — see docs/superpowers/specs/2026-10-01-docker-containerization-design.md).

# private_uploads (partner payment proofs) is bind-mounted since 2026-11-25 and
# must be owned by the container's appuser (uid 1000), or new proofs can't be saved.
mkdir -p private_uploads && chown 1000:1000 private_uploads

echo "building image"
docker compose build || { echo "FATAL: docker build failed"; exit 1; }

echo "starting container"
docker compose up -d || { echo "FATAL: docker compose up failed"; exit 1; }

# Poll instead of a single flat sleep+check — a fresh container (image export/
# unpack, cgroup setup, app cache warm-up) is slower to answer than the old
# bare-metal process restart this replaced, and a single early check races
# the app's own startup. 20 x 1s covers every cold start seen in testing
# (worst case observed: ~9s) with headroom, while still failing fast on a
# genuinely broken deploy instead of waiting the full budget every time.
CODE=000
for i in $(seq 1 20); do
    sleep 1
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/health || echo 000)
    [ "$CODE" = "200" ] && break
done

if [ "$CODE" = "200" ]; then
    echo "DEPLOYED OK: now at $(git log -1 --format='%h %s')"
else
    echo "DEPLOY UNHEALTHY: /health returned $CODE after restart"
    echo "Last known-good commit: $LOCAL"
    echo "Roll back with:  cd $APP && git reset --hard $LOCAL && docker compose up -d --build"
    docker compose logs --tail=20 oxypc-app
    exit 1
fi
