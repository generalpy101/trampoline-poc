#!/usr/bin/env bash
# One-command setup. Creates venv, installs Django, migrates, seeds demo data,
# refreshes statistics, and pre-populates the dashboard with resolved predictions.
#
# After this, run:  source .venv/bin/activate && python manage.py runserver
set -euo pipefail

cd "$(dirname "$0")"

# Pick the newest Python available — Django 5.x needs 3.10+
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
fi

if [ -z "$PYTHON" ]; then
  echo "No suitable Python found. Install Python 3.10 or newer."
  exit 1
fi

echo "→ Using $($PYTHON --version)"

if [ ! -d ".venv" ]; then
  echo "→ Creating virtual environment in .venv/"
  $PYTHON -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "→ Installing dependencies"
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "→ Running migrations"
python manage.py migrate --noinput >/dev/null

echo "→ Creating cache table (idempotent)"
python manage.py createcachetable >/dev/null 2>&1 || true

echo "→ Seeding catalog, history, batches, and demo predictions"
python manage.py seed_data

echo "→ Computing lane statistics"
python manage.py refresh_lane_stats

cat <<'EOF'

──────────────────────────────────────────────────────
  Setup complete.

  source .venv/bin/activate
  python manage.py runserver

  Then open http://127.0.0.1:8000/
──────────────────────────────────────────────────────
EOF
