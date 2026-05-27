"""
Django settings.

Local development uses sensible defaults (SQLite, debug on, in-memory cache).
For production, set these environment variables:

    DJANGO_SECRET_KEY        required, any long random string
    DJANGO_DEBUG             default "false"
    DJANGO_ALLOWED_HOSTS     comma-separated, default "localhost,127.0.0.1"
    SQLITE_PATH              optional; absolute path to the SQLite file
                             (set this to a mounted persistent volume in prod)

For local dev you can ignore all of these — defaults make the app run.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: str) -> list[str]:
    return [h.strip() for h in os.environ.get(name, default).split(",") if h.strip()]


# ----- Core ---------------------------------------------------------------

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "dev-only-key-do-not-use-in-production-set-DJANGO_SECRET_KEY-instead",
)
DEBUG = _env_bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0")

# Whether this deployment is served over HTTPS. Drives HSTS, secure cookies,
# SSL redirect, and the proxy SSL header. Defaults to enabled when DEBUG=False.
# Set DJANGO_USE_HTTPS=false for IP-only or staging deployments without TLS,
# otherwise Django sends HSTS and the browser refuses HTTP on subsequent visits.
USE_HTTPS = _env_bool("DJANGO_USE_HTTPS", default=not DEBUG)

if USE_HTTPS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = _env_bool("DJANGO_SSL_REDIRECT", default=True)
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# Clickjacking protection is HTTP-safe; always on outside dev.
if not DEBUG:
    X_FRAME_OPTIONS = "DENY"

# CSRF: trust the configured hosts using whichever scheme this deployment uses.
_csrf_scheme = "https" if USE_HTTPS else "http"
CSRF_TRUSTED_ORIGINS = [
    f"{_csrf_scheme}://{host}" for host in ALLOWED_HOSTS
    if host not in ("localhost", "127.0.0.1", "0.0.0.0")
]


# ----- Apps & middleware --------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves static files without needing nginx/CDN. Must come
    # right after SecurityMiddleware. No-op if whitenoise isn't installed.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "delivery_estimate.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "delivery_estimate.wsgi.application"


# ----- Database -----------------------------------------------------------
# SQLite, including in production. Fine for the traffic this POC sees.
# Set SQLITE_PATH to an absolute path on the production filesystem
# (e.g. /var/lib/delivery-estimate/db.sqlite3 on a VPS).
#
# WAL mode + reasonable busy_timeout is set in core/apps.py so concurrent
# reads don't block on a single writer.

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH") or (BASE_DIR / "db.sqlite3"),
        "OPTIONS": {
            "timeout": 30,                # seconds to wait if DB is busy
            "transaction_mode": "IMMEDIATE",  # Django 5.1+; ignored on older
        },
    }
}


# ----- Static files -------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"  # destination for `collectstatic`

# Compressed, hashed static files served by WhiteNoise.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}


# ----- Misc ---------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ----- Cache --------------------------------------------------------------
# Default: database cache backed by the same SQLite file. Works correctly
# across multiple gunicorn workers (locmem would silo cache state per process,
# which breaks invalidation when one worker mutates inventory but the next
# request lands on another worker).
#
# Create the cache table once with:   python manage.py createcachetable
# Optionally swap to Redis by setting REDIS_URL.

if os.environ.get("REDIS_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": os.environ["REDIS_URL"],
            "TIMEOUT": 900,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "delivery_estimate_cache",
            "TIMEOUT": 900,  # 15 minutes — central freshness/load tradeoff
        }
    }


# ----- Logging ------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}


# ----- Delivery estimate tuning ------------------------------------------
# The percentile we promise to customers. Raise for fewer broken promises,
# lower for faster promised dates.
DELIVERY_PROMISE_PERCENTILE = float(os.environ.get("DELIVERY_PROMISE_PERCENTILE", "0.80"))

# Handling buffer (today → ready to ship). Realistic for a furniture warehouse.
HANDLING_BUFFER_DAYS = int(os.environ.get("HANDLING_BUFFER_DAYS", "1"))

# Minimum samples needed before we trust a lane's statistics.
MIN_SAMPLES_FOR_LANE = int(os.environ.get("MIN_SAMPLES_FOR_LANE", "10"))

# Fallback transit if a lane has no statistics yet (new warehouse, new region).
DEFAULT_TRANSIT_DAYS = int(os.environ.get("DEFAULT_TRANSIT_DAYS", "10"))
