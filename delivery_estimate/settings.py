"""
Django settings for the delivery estimate POC.

Optimized for demonstration: SQLite, local-memory cache, debug on.
For production, swap to Postgres, Redis, set DEBUG=False, etc.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "poc-not-for-production-do-not-use-this-key-in-real-life"
DEBUG = True
ALLOWED_HOSTS = ["*"]

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

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ----- POC-specific cache (Solution 1 + 4 stack) -----
# In prod this would be Redis. In-memory is fine for the demo.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "delivery-estimate-cache",
        "TIMEOUT": 900,  # 15 minutes — the central freshness/load tradeoff
    }
}

# ----- Delivery estimate tuning -----
# The percentile we promise to customers. Raise for fewer broken promises,
# lower for faster promised dates.
DELIVERY_PROMISE_PERCENTILE = 0.80

# Handling buffer (today → ready to ship). Realistic for a furniture warehouse.
HANDLING_BUFFER_DAYS = 1

# Minimum samples needed before we trust a lane's statistics.
MIN_SAMPLES_FOR_LANE = 10

# Fallback transit if a lane has no statistics yet (new warehouse, new region).
DEFAULT_TRANSIT_DAYS = 10
