#!/usr/bin/env bash
# First-time setup on the host (run once as root in /srv/school-api, after deploy.sh copied the files):
# folders with the right owners, the database password, and a .env with generated secrets. Fill in the SMTP settings,
# PUBLIC_URL and SIGNUP_ALLOWLIST in .env next; the admin is the first account confirmed in the web UI (/admin/signup).
set -euo pipefail
cd "$(dirname "$0")/.."
APP_UID=1000   # the containers run as this user (for a Unix socket: the reverse proxy's user on the host)

mkdir -p data/db data/files data/models run secrets backups
chown "$APP_UID:$APP_UID" data/files data/models run
chmod 750 run
chmod 700 secrets backups

if [ ! -s secrets/db_password ]; then
  openssl rand -base64 32 | tr -d '\n=/+' > secrets/db_password
  echo "created secrets/db_password"
fi
chown "$APP_UID:$APP_UID" secrets/db_password
chmod 400 secrets/db_password

if [ ! -f .env ]; then
  {
    echo "# the admin's scripts and crawlers (Authorization: Bearer); people log in with accounts"
    echo "INGEST_TOKEN=$(openssl rand -hex 32)"
    echo "SESSION_SECRET=$(openssl rand -hex 32)"
    echo "FILES_DIR=/data/files"
    echo "# the address people open (links in mails), e.g. https://api.example.ac.jp"
    echo "PUBLIC_URL="
    echo "# who may sign up: @domain and / or full addresses, comma-separated"
    echo "SIGNUP_ALLOWLIST="
    echo "# true: submitters' uploads are published at once; empty / false: the admin reviews them"
    echo "DIRECT_PUBLISH="
    echo "SMTP_HOST="
    echo "SMTP_PORT=587"
    echo "# starttls (port 587), ssl (port 465) or none"
    echo "SMTP_SECURITY=starttls"
    echo "SMTP_USER="
    echo "SMTP_PASSWORD="
    echo "SMTP_FROM="
  } > .env
  chmod 600 .env
  echo "created .env (fill in PUBLIC_URL, SIGNUP_ALLOWLIST and SMTP_* next)"
fi
echo "host ready: $(pwd)"
