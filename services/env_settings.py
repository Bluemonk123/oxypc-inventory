"""Edit a few whitelisted .env settings from the UI (2026-11-11, Pankaj).

Only the email (SMTP) settings can be changed here — never the database URL,
secret key or anything else in .env. The rest of the file is kept exactly as
it is (other lines, comments, order). Values are written double-quoted so a
password with spaces, '#' or quotes is read back correctly by python-dotenv.

The new values are also put into os.environ, so the running app uses them at
once (OxyPC runs one worker and services/email_notify.py reads os.environ on
every send) — no restart needed.
"""
import os
import re
import tempfile

from config import BASE_DIR

ENV_PATH = os.path.join(BASE_DIR, ".env")

_FIELDS = ("HOST", "PORT", "SECURITY", "USER", "PASSWORD", "FROM")
EDITABLE_KEYS = tuple(f"{p}_{f}" for p in ("OXYPC_SMTP", "OXYPC_SMTP2") for f in _FIELDS) + ("OXYPC_PUBLIC_URL",)

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def update_env(changes: dict) -> list:
    """Write {KEY: value} into .env (value '' removes the line) and os.environ.
    Only EDITABLE_KEYS are accepted. Returns the keys that changed."""
    bad = [k for k in changes if k not in EDITABLE_KEYS]
    if bad:
        raise ValueError(f"Not editable here: {', '.join(bad)}")
    for k, v in changes.items():
        if any(c in (v or "") for c in "\r\n\x00"):
            raise ValueError(f"{k}: line breaks are not allowed")
    changed = [k for k, v in changes.items() if (os.environ.get(k) or "") != (v or "")]
    if not changed:
        return []

    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    done, out = set(), []
    for line in lines:
        m = _LINE.match(line)
        key = m.group(1) if m else None
        if key in changes:
            if key in done:                 # a duplicate line of the same key: drop it
                continue
            done.add(key)
            if changes[key]:
                out.append(f"{key}={_quote(changes[key])}")
            continue                        # '' = remove the line
        out.append(line)
    new = [k for k in changes if k not in done and changes[k]]
    if new:
        if out and out[-1].strip():
            out.append("")
        if not any("Email notifications" in ln for ln in out):
            out.append("# Email notifications — edited from Master Data → Email")
        out.extend(f"{k}={_quote(changes[k])}" for k in new)

    # atomic replace: a half-written .env would stop OxyPC starting
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=BASE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(out) + "\n")
        os.replace(tmp, ENV_PATH)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

    for k, v in changes.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    return changed
