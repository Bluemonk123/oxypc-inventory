import configparser
import os
import secrets

# Load .env file if present (secrets management — never commit .env)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass  # python-dotenv not installed; rely on OS env vars

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")  # for file attachments

_cfg = configparser.ConfigParser()

if os.path.exists(CONFIG_FILE):
    _cfg.read(CONFIG_FILE)

def _get(section, key, default):
    try:
        return _cfg[section][key]
    except KeyError:
        return default

DATABASE_URL = os.environ.get("OXYPC_DATABASE_URL") or _get("database", "url", "postgresql+asyncpg://oxypc:oxypc123@localhost:5432/oxypc_db")
SECRET_KEY = os.environ.get("OXYPC_SECRET_KEY") or _get("security", "secret_key", secrets.token_hex(32))
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("OXYPC_TOKEN_EXPIRE_MINUTES") or _get("security", "access_token_expire_minutes", "43200"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.environ.get("OXYPC_REFRESH_DAYS") or _get("security", "refresh_token_expire_days", "7"))
APP_PORT = int(os.environ.get("OXYPC_PORT") or _get("app", "port", "8000"))
APP_HOST = os.environ.get("OXYPC_HOST") or _get("app", "host", "0.0.0.0")
APP_NAME = "OxyPC Inventory Lifecycle"

# Cookie Secure flag — set OXYPC_COOKIE_SECURE=1 in production (HTTPS) so auth
# cookies are never sent over plain HTTP. Defaults off for local LAN/HTTP testing.
COOKIE_SECURE = (os.environ.get("OXYPC_COOKIE_SECURE")
                 or _get("security", "cookie_secure", "0")).strip().lower() in ("1", "true", "yes")

# OxyQC machine-to-machine API key (used by standalone OxyQC app)
OXYQC_API_KEY = os.environ.get("OXYPC_API_KEY") or _get("oxyqc", "api_key", "oxyqc-default-key-change-me")

# Ecosystem CORS — comma-separated origins allowed to call /api/v1/*
# Example env: OXYPC_ALLOWED_ORIGINS=https://portal.oxypc.in,https://esg.oxypc.in
ALLOWED_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv(
        "OXYPC_ALLOWED_ORIGINS",
        "http://localhost:3000,http://localhost:8080,http://localhost:5173",
    ).split(",")
    if o.strip()
]

def write_default_config():
    cfg = configparser.ConfigParser()
    cfg["database"] = {"url": DATABASE_URL.replace("%", "%%")}
    cfg["security"] = {
        "secret_key": SECRET_KEY,
        "access_token_expire_minutes": str(ACCESS_TOKEN_EXPIRE_MINUTES),
        "refresh_token_expire_days": str(REFRESH_TOKEN_EXPIRE_DAYS),
    }
    cfg["app"] = {"port": str(APP_PORT), "host": APP_HOST}
    cfg["oxyqc"] = {"api_key": OXYQC_API_KEY}
    with open(CONFIG_FILE, "w") as f:
        cfg.write(f)
