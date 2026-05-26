"""
WSGI entry point for production deployment.

Use with any WSGI server, e.g.:
    gunicorn delivery_estimate.wsgi:application --bind 0.0.0.0:8000

Environment variables read from settings.py:
    DJANGO_SECRET_KEY   (required in production)
    DJANGO_DEBUG        (default "false")
    DJANGO_ALLOWED_HOSTS (comma-separated, default "localhost,127.0.0.1")
    DATABASE_URL        (optional; falls back to SQLite if unset)
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "delivery_estimate.settings")

application = get_wsgi_application()
