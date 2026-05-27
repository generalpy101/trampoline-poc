#!/usr/bin/env bash
# Pull latest code, install deps, migrate, collectstatic, reload service.
# Run on the VPS as the `delivery` user (or sudo into it).
#
# Usage:
#   cd /var/www/delivery-estimate && ./deploy/deploy.sh

set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/delivery-estimate}"
SERVICE="${SERVICE:-delivery-estimate}"

cd "$APP_DIR"

# Load env vars so manage.py uses the configured SQLITE_PATH and friends
if [ -f /etc/delivery-estimate.env ]; then
  set -a
  # shellcheck disable=SC1091
  . /etc/delivery-estimate.env
  set +a
fi

echo "→ git pull"
git pull --ff-only

echo "→ install dependencies"
.venv/bin/pip install -q -r requirements.txt

echo "→ migrate"
.venv/bin/python manage.py migrate --noinput

echo "→ create cache table (idempotent)"
.venv/bin/python manage.py createcachetable >/dev/null 2>&1 || true

echo "→ collectstatic"
.venv/bin/python manage.py collectstatic --noinput

echo "→ reload gunicorn (zero-downtime via SIGHUP)"
sudo systemctl reload "$SERVICE"

echo "✓ done"
