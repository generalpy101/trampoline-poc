"""
Gunicorn configuration. Loaded by `gunicorn --config gunicorn.conf.py`.

Tuned for a small Django service behind a load balancer. Adjust workers
and threads to your CPU count and memory budget.
"""

import os

# Network
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
forwarded_allow_ips = "*"   # trust X-Forwarded-* from the load balancer

# Concurrency
# Sized for SQLite as the production database. SQLite serializes writes
# at the file level, so spawning many worker processes mostly adds memory
# pressure without throughput. A couple of processes with threads inside
# is a better fit: threads share the SQLite connection cleanly and the
# WAL mode set in core/apps.py lets reads proceed during writes.
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("WEB_THREADS", "4"))
worker_class = "gthread"

# Timeouts
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "30"))
graceful_timeout = 30
keepalive = 5

# Logs to stdout/stderr — let your platform aggregate them.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(L)ss'

# Reload only matters for local dev. Off by default in production.
reload = False
preload_app = True
