"""Permission coverage check (2026-10-25). Run: python _scan_action_coverage.py

Lists every write route (POST/PUT/PATCH/DELETE) of the live app and checks it
against auth/route_actions.py:
  - UNMAPPED : a write route with no entry  -> must be added (gated or None)
  - STALE    : an entry whose route no longer exists
  - BAD      : unknown module key or action
Exit code 1 if anything is reported. Expected output: "0 unmapped, 0 stale, 0 bad".
"""
import sys
from fastapi.routing import APIRoute

from main import app
from auth.route_actions import ROUTE_ACTIONS
from routers.master import PERM_MODULES

VALID_MODULES = {k for k, _ in PERM_MODULES}
VALID_ACTIONS = {"add", "upload", "edit", "delete", "pricing", "export"}

import re
# GET routes that look like exports / downloads must be mapped too (2026-10-26)
EXPORT_GET = re.compile(r"export|download|csv|xlsx|excel|pdf|print|template|zip|invoice|waybill|receipt", re.I)
live = set()
live_get = set()
for r in app.routes:
    if isinstance(r, APIRoute):
        for m in sorted(set(r.methods) - {"GET", "HEAD", "OPTIONS"}):
            live.add((m, r.path))
        if "GET" in r.methods:
            live_get.add(("GET", r.path))
            if EXPORT_GET.search(r.path):
                live.add(("GET", r.path))
live |= {k for k in ROUTE_ACTIONS if k in live_get}   # extra GETs mapped by hand

unmapped = sorted(live - set(ROUTE_ACTIONS))
stale = sorted(set(ROUTE_ACTIONS) - live)
bad = []
for key, entry in ROUTE_ACTIONS.items():
    if entry is None:
        continue
    mods, action = entry
    for m in ((mods,) if isinstance(mods, str) else mods):
        if m not in VALID_MODULES:
            bad.append((key, f"unknown module {m}"))
    if action not in VALID_ACTIONS:
        bad.append((key, f"unknown action {action}"))

for k in unmapped: print("UNMAPPED", *k)
for k in stale:    print("STALE   ", *k)
for k, why in bad: print("BAD     ", *k, why)
gated = sum(1 for e in ROUTE_ACTIONS.values() if e)
print(f"{len(live)} write + export routes: {gated} gated, {len(ROUTE_ACTIONS) - gated} not gated | "
      f"{len(unmapped)} unmapped, {len(stale)} stale, {len(bad)} bad")
sys.exit(1 if (unmapped or stale or bad) else 0)
