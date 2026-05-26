from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _tune_sqlite(sender, connection, **kwargs):
    """
    Enable WAL mode on every new SQLite connection.

    WAL (Write-Ahead Logging) lets readers proceed in parallel with a
    single writer instead of blocking on the global database lock. This
    is the difference between SQLite being a toy in production and
    being genuinely usable for a small service.

    `synchronous=NORMAL` is the WAL-safe sweet spot: durable across
    application crashes, only vulnerable to losing the last few
    committed transactions on a hard OS-level power loss. Fine for
    this app's data.

    `busy_timeout` is the connection-level wait; the per-query timeout
    in DATABASES.OPTIONS handles the Python side.
    """
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.execute("PRAGMA busy_timeout = 5000;")
        cursor.execute("PRAGMA foreign_keys = ON;")


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        connection_created.connect(_tune_sqlite)
