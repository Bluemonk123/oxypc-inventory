# OxyPC Inventory — Production Deployment Runbook

Target: a cloud VPS (DigitalOcean Bangalore recommended) reachable at `https://erp.oxypc.in`,
24/7 uptime, free SSL, auto-restart, nightly backups.

> Replace `erp.oxypc.in` with your real subdomain everywhere below.

---

## 0. What you provision (one-time, your accounts)

| Item | Where | Cost | Notes |
|---|---|---|---|
| VPS — 2GB RAM, 1 vCPU, 50GB SSD, Ubuntu 22.04, **Bangalore** | DigitalOcean | ~₹500/mo | Lowest latency from India |
| Domain (you have one) | Your registrar | — | Point a subdomain to the VPS |

Create the droplet, add your SSH key, note its public IP (e.g. `203.0.113.10`).

---

## 1. DNS — point the subdomain at the VPS

At your domain registrar (or Cloudflare if the domain is there), add an **A record**:

```
Type: A    Name: erp    Value: <VPS_PUBLIC_IP>    TTL: 300
```

Wait ~5 min, then `ping erp.oxypc.in` should resolve to the VPS IP.

---

## 2. Server base setup

SSH in as root:

```bash
ssh root@<VPS_PUBLIC_IP>

apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip postgresql nginx \
               certbot python3-certbot-nginx git ufw

# Firewall: allow SSH + web only
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
```

---

## 3. PostgreSQL

```bash
sudo -u postgres psql <<'SQL'
CREATE DATABASE oxypc_db;
CREATE USER oxypc_user WITH PASSWORD 'PUT_A_STRONG_PASSWORD_HERE';
GRANT ALL PRIVILEGES ON DATABASE oxypc_db TO oxypc_user;
ALTER DATABASE oxypc_db OWNER TO oxypc_user;
SQL
```

### Migrate your existing data (from the Windows laptop)

On the **laptop** (PowerShell), dump the current DB:

```powershell
$env:PGPASSWORD = "oxypc123"
& "C:\Program Files\PostgreSQL\18\bin\pg_dump.exe" -U oxypc -h localhost -d oxypc_db -Fc -f C:\Users\Pankaj.sehgal\oxypc_prod.dump
```

Copy it up and restore on the VPS:

```bash
# from laptop:
scp C:\Users\Pankaj.sehgal\oxypc_prod.dump root@<VPS_PUBLIC_IP>:/tmp/

# on VPS:
sudo -u postgres pg_restore -d oxypc_db --no-owner --role=oxypc_user /tmp/oxypc_prod.dump
```

(If starting fresh instead, the app creates tables on first run — skip the dump/restore.)

---

## 4. Deploy the app code

```bash
mkdir -p /opt/oxypc
# from laptop — upload the app (exclude venv, __pycache__, .env, uploads if large):
#   scp -r C:\Users\Pankaj.sehgal\Claude\Oxypc\oxypc-inventory\* root@<VPS_PUBLIC_IP>:/opt/oxypc/

cd /opt/oxypc
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install python-dotenv uvicorn   # ensure present
```

### Environment file

```bash
cp deploy/.env.example /opt/oxypc/.env
nano /opt/oxypc/.env          # fill DB password, generate SECRET_KEY, set OXYPC_COOKIE_SECURE=1
python3 -c "import secrets; print(secrets.token_hex(32))"   # use output for OXYPC_SECRET_KEY
chmod 600 /opt/oxypc/.env     # readable only by owner
chown -R www-data:www-data /opt/oxypc
```

### New columns (2026-11-10/11)

From Entity (accounts, deals, quotes, POs), Expected Close Date (Supplier Deals) and Credit Days (accounts) are added
automatically on startup (`OXYPC_AUTO_FIX=1`, the default), and the one-time startup steps map old quotation statuses
(shared → added, confirmed → approved) and fill From Entity from the creator's profile. Only if the server runs with
`OXYPC_AUTO_FIX=0`: run `python migrate_from_entity_credit_days.py` before starting the new version.

### Email notifications (optional, 2026-11-09)

Every personal notification is also emailed to the user's email: the
Assignee, the Assigner (who did the action), and both of their Report To
managers — each person once. Broadcasts stay in-app only. One company mailbox sends them all and
can reach any address (Gmail, Outlook, company mail). Easiest (2026-11-11): **Master Data → Email** — fill in the
mailbox, Save (written to `.env`, works at once, no restart), then **Send test email**. Or add these to `.env` by hand
and restart OxyPC. Leave these out and no email is sent.

```bash
OXYPC_SMTP_HOST=smtp.office365.com      # or smtp.gmail.com (needs an App Password)
OXYPC_SMTP_PORT=587                     # 465 = SSL
OXYPC_SMTP_USER=erp@oxypc.in
OXYPC_SMTP_PASSWORD=...                 # stays in .env, never in the database
OXYPC_SMTP_FROM=OxyPC ERP <erp@oxypc.in>   # optional, defaults to SMTP_USER
OXYPC_SMTP_SECURITY=starttls            # optional: starttls | ssl | none
OXYPC_PUBLIC_URL=http://192.168.1.100:8000   # optional link in each email

# Optional backup mailbox — used automatically when the main one fails
OXYPC_SMTP2_HOST=smtp.gmail.com
OXYPC_SMTP2_PORT=587
OXYPC_SMTP2_USER=oxypc.erp.backup@gmail.com
OXYPC_SMTP2_PASSWORD=...
OXYPC_SMTP2_FROM=OxyPC ERP <oxypc.erp.backup@gmail.com>
```

---

## 5. Run as a service

```bash
sudo cp deploy/oxypc.service /etc/systemd/system/oxypc.service
sudo systemctl daemon-reload
sudo systemctl enable --now oxypc
sudo systemctl status oxypc          # should be active (running)
curl -s http://127.0.0.1:8000/health # should return 200
```

If it fails: `journalctl -u oxypc -n 50 --no-pager`

---

## 6. nginx + SSL

```bash
sudo cp deploy/nginx-oxypc.conf /etc/nginx/sites-available/oxypc
sudo ln -s /etc/nginx/sites-available/oxypc /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# Free SSL (auto-renews via systemd timer)
sudo certbot --nginx -d erp.oxypc.in
```

Now open `https://erp.oxypc.in` — the login page should load over HTTPS.

---

## 7. Nightly backups (Linux cron)

```bash
sudo -u postgres crontab -e
# add:
0 23 * * * pg_dump oxypc_db -Fc -f /var/backups/oxypc_$(date +\%Y\%m\%d).dump && find /var/backups -name 'oxypc_*.dump' -mtime +30 -delete
```

Then copy off-server weekly (S3 / Google Drive / another box) so a VPS loss isn't total loss.

---

## 8. Point OxyQC standalone EXEs at the new server

In each inspection laptop's OxyQC `config.ini` (or Settings dialog):

```
server_url = https://erp.oxypc.in
api_key    = <the OXYPC_API_KEY you set in .env>
```

---

## Updating the app later

```bash
cd /opt/oxypc
# upload changed files, then:
source venv/bin/activate
pip install -r requirements.txt        # if deps changed
alembic upgrade head                   # if migrations added
sudo systemctl restart oxypc
```

---

## MUST-HARDEN before/right after going public

Going internet-facing exposes the app to the world. Do these:

1. **Cookie Secure flag** — set `OXYPC_COOKIE_SECURE=1` (needs the small code change to read it; see hardening task). Without it the auth cookie can be sent over plain HTTP.
2. **Rotate ALL credentials** — DB password, `OXYPC_SECRET_KEY`, `OXYPC_API_KEY`. The LAN defaults (`oxypc123`, `oxyqc-default-key-change-me`) must NOT survive to production.
3. **Reset every user password** — assume LAN passwords are weak. Force a change.
4. **Rate limiting** — login already limited (5/min). Add limits to other write routes (audit flagged this).
5. **Session expiry** — `OXYPC_TOKEN_EXPIRE_MINUTES=480` (8h) instead of 1440 (24h) for internet exposure.
6. **fail2ban** — `apt install fail2ban` to block brute-force SSH and login attempts.
7. **DB not public** — confirm PostgreSQL listens only on localhost (default). `ufw` already blocks 5432.
8. **Off-site backups** — see step 7; a single VPS is a single point of failure.
9. **Admin MFA** — the audit recommended 2FA for admin; consider before exposing financial data.

These map to the open items in the 2026-04-26 enterprise audit (L5 Security).
