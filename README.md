# Delivery Estimate

> **The problem:** a D2C furniture brand shows every customer the same fixed "10 to 14 days" delivery promise — losing fast customers to competitors and angering slow ones with broken promises.
>
> **This service:** a Django + Alpine.js app that returns a personalized delivery date for every `(product, pincode)` request based on live inventory, scheduled production batches, and historical delivery performance. Includes shadow-logging and an accuracy dashboard.

![Architecture diagram](docs/architecture.svg)

For a deeper read, the app itself ships three documentation pages at `/overview/`, `/algorithm/`, and `/technical/`.

---

## Quick start

```bash
./setup.sh                          # creates venv, installs, migrates, seeds
source .venv/bin/activate
python manage.py runserver
```

Then open <http://127.0.0.1:8000/>.

| Page | What it shows |
|---|---|
| `/` — Estimator | Pick a product, enter a pincode, get a delivery date with a reason and confidence level. Live inventory controls let you flip stock to zero and watch the estimate adjust. |
| `/dashboard/` — Accuracy | Shadow log of every prediction. On-time rate overall and broken down by source. 60 auto-seeded resolved predictions so the dashboard has data on first load. |
| `/overview/` | High-level description of the system and its four cases. |
| `/algorithm/` | The decision tree, formulas, percentile choice, pincode resolution, tie-breaking. |
| `/technical/` | Service shape, data model, code excerpt, caching, SQLite tuning, observability, scaling roadmap. |
| `/admin/` — Django admin | Inspect every table directly. |

---

## How it works

A single Django app `core` exposes `GET /api/estimate?sku=…&pincode=…`. The view resolves the pincode (exact → district → state → region prefix match → unserviceable), looks up live inventory, and dispatches to one of seven branches in `core/services/estimator.py::compute_estimate()`. Transit times come from a pre-aggregated `LanePerformance` table populated by `python manage.py refresh_lane_stats` from historical `DeliveryRecord`s using the 80th-percentile transit per `(warehouse, cluster)` lane. Supplier slip statistics work the same way. Results are cached in Django's cache framework with a 15-minute TTL keyed by `(sku, cluster, match_quality, percentile)`. Inventory mutations invalidate affected entries. Every prediction is shadow-logged for the dashboard.

The architecture is **on-demand compute with a short cache, backed by statistical lookups refreshed nightly**. Precomputation, event-driven invalidation, and learned models are natural next steps when traffic and data volume justify them.

---

## Tier logic

`core/services/estimator.py` — one function, top-to-bottom readable in two minutes:

```python
def compute_estimate(product, pincode):
    cluster, match_quality = resolve_cluster(pincode)
    if cluster is None:
        return _not_serviceable(today, pincode)

    stocked = Inventory.objects.filter(product=product, quantity__gt=0)
    if stocked:
        return _estimate_in_stock(today, handling, stocked, cluster, match_quality)
    return _estimate_out_of_stock(today, handling, product, cluster, match_quality)
```

Branches in priority order:

| Tier | When it fires | Output | Confidence |
|---|---|---|---|
| `in_stock_nearest` | Stock at nearest warehouse, healthy lane | single date | high |
| `in_stock_farther` | Stock at a farther warehouse only | single date | high |
| `in_stock_no_stats` | Stock but no lane history yet | single date | medium |
| `awaiting_batch` | OOS but batch scheduled, known supplier | date range | medium / low |
| `awaiting_batch_no_slip` | OOS, batch known, supplier history unknown | wider range | low |
| `soft_fallback` | OOS, no batch scheduled | 3–5 week range | low |
| `not_serviceable` | Pincode prefix doesn't match any cluster | no date, clear refusal | — |

Confidence further downgrades when the pincode was matched by prefix instead of exact:
**district match** → no change, **state match** → one notch, **region match** → two notches. A new `110099` in the Delhi district stays high-confidence; a `120001` matched only at the region level drops to low.

---

## Design choices

**On-demand + cache instead of precomputation or event streaming.**
For furniture-brand traffic (thousands not millions of page views per day), precomputation is over-engineering. On-demand computation with a 15-minute cache covers the read load, keeps the architecture explainable, and ships fast. Precomputed hot-sets and event-driven refresh are the natural next steps when traffic and event volume justify them.

**P80 statistics instead of an ML model.**
Statistics is self-correcting (next refresh picks up carrier degradation), interpretable ("80% of past deliveries on this lane arrived in this many days"), and needs no model lifecycle. A learned regression model becomes worth the complexity once the bucket-based approach plateaus on accuracy. For a first version, statistics is the right answer.

**A range for out-of-stock, not a single date.**
Production batches genuinely slip. Pretending the exact day is known is a broken promise waiting to happen. A calibrated range with reason text ("supplier typically arrives within N days of promised") sets correct customer expectations and lets the brand still differentiate by supplier reliability.

**Pincode prefix matching.**
A new pincode `110099` is almost certainly in the same delivery zone as `110001-110009` that we already serve. Refusing to commit just because the exact pincode is new would lose sales. Conversely, `880001` (Tripura, with no warehouse coverage anywhere nearby) produces a clear "not yet available" — not a fake low-confidence date.

---

## What's deliberately scoped out

| Concern | This version | At higher scale |
|---|---|---|
| Database | SQLite (with WAL tuning) | Postgres (native `PERCENTILE_CONT`) |
| Cache | Django locmem | Redis with replication |
| Stats refresh | Management command | Nightly Airflow / dbt job |
| Cache invalidation | Inline on inventory mutation | Event-driven via Kafka `inventory.threshold_crossed` |
| Carrier dimension | One carrier per lane | Multi-carrier with per-lane selection |
| Reason text | Templates, LLM-ready hook in `reasoning.py` | LLM-generated with template fallback |
| Auth on mutation endpoints | None | Required |
| Hot-set precomputation | None — lazy compute always | Add for top products × clusters once traffic justifies |
| Observability | Local dashboard | + Sentry, Prometheus, structured logging |

The top three rows are infrastructure swaps with zero application-logic change. The rest are features the architecture intentionally defers.

---

## Where AI fits

`core/services/reasoning.py` has the exact shape needed to plug in an LLM. Stubbed to use templates by default so the app works without an API key.

**Use AI for:**

- Generating customer-facing reason text from structured estimates
- Parsing supplier emails and PDFs about batch delays into the `ProductionBatch` table
- Drafting customer-support replies referencing the cached estimate

**Don't use AI for:**

- The date prediction itself — structured data, deterministic logic, faster and more reliable as plain code
- Cache invalidation, the tier routing, the percentile math

The customer-facing date is a commitment shown at checkout. It should never come from a system that can hallucinate.

---

## File layout

```
.
├── setup.sh                        # one-command local install + seed
├── manage.py
├── requirements.txt
├── README.md
├── docs/
│   └── architecture.svg            # diagram at the top of this file
├── delivery_estimate/              # Django project
│   ├── settings.py                 # env-driven; cache & percentile knobs
│   ├── urls.py
│   └── wsgi.py
├── core/                           # the app
│   ├── models.py                   # 11 tables, ~150 lines
│   ├── admin.py
│   ├── apps.py                     # SQLite WAL-mode signal handler
│   ├── urls.py
│   ├── views.py                    # page views + JSON API
│   ├── services/
│   │   ├── estimator.py            # tier logic (compute_estimate)
│   │   ├── statistics.py           # percentile aggregation
│   │   └── reasoning.py            # LLM hook with template fallback
│   ├── management/commands/
│   │   ├── seed_data.py            # deterministic seed + 60 demo predictions
│   │   ├── refresh_lane_stats.py
│   │   └── resolve_predictions.py
│   ├── templates/core/
│   │   ├── base.html
│   │   ├── index.html              # Alpine.js estimator UI
│   │   ├── dashboard.html
│   │   ├── docs_overview.html
│   │   ├── docs_algorithm.html
│   │   └── docs_technical.html
│   ├── static/core/styles.css
│   └── tests/
│       └── test_estimator.py       # 14 tests
└── deploy/                         # VPS deployment artifacts
    ├── delivery-estimate.service   # systemd unit
    ├── nginx.conf.example          # reverse proxy with TLS
    ├── nginx-ip.conf.example       # HTTP-only IP-based variant
    └── deploy.sh                   # zero-downtime update script
```

---

## Deployment (VPS)

Targets a single VPS running modern Linux (Ubuntu / Debian / etc.) with systemd. SQLite is the production database, gunicorn is the app server, nginx (optional) is the reverse proxy.

**Why SQLite in production.** At this scale, SQLite is fast, dependency-free, and avoids an entire class of operational concerns. `core/apps.py` enables WAL mode on every connection so reads proceed in parallel with the single writer. `synchronous=NORMAL` and `busy_timeout=5000ms` round it out.

The `deploy/` directory contains:

- `delivery-estimate.service` — systemd unit (runs gunicorn under a dedicated unprivileged user with filesystem hardening)
- `nginx.conf.example` — reverse proxy config with TLS and static-file passthrough
- `nginx-ip.conf.example` — HTTP-only variant for IP-based deployments without a domain
- `deploy.sh` — one-command update script for subsequent deploys

### First-time setup

```bash
# As root, create the system user and directories
sudo useradd --system --shell /usr/sbin/nologin --home /var/www/delivery-estimate delivery
sudo mkdir -p /var/www/delivery-estimate /var/lib/delivery-estimate
sudo chown -R delivery:delivery /var/www/delivery-estimate /var/lib/delivery-estimate

# Clone the repo into place
sudo -u delivery git clone <your-repo-url> /var/www/delivery-estimate
cd /var/www/delivery-estimate

# Bootstrap venv + dependencies
sudo -u delivery python3 -m venv .venv
sudo -u delivery .venv/bin/pip install -r requirements.txt
sudo -u delivery .venv/bin/python manage.py migrate
sudo -u delivery .venv/bin/python manage.py seed_data            # optional, demo data
sudo -u delivery .venv/bin/python manage.py refresh_lane_stats   # optional
sudo -u delivery .venv/bin/python manage.py collectstatic --noinput

# Drop the env file (copy .env.example, fill in real values)
sudo install -m 640 -o root -g delivery .env.example /etc/delivery-estimate.env
sudo vim /etc/delivery-estimate.env   # set DJANGO_SECRET_KEY, ALLOWED_HOSTS, etc.

# Install and start the service
sudo install -m 644 deploy/delivery-estimate.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now delivery-estimate
sudo systemctl status delivery-estimate
```

### Nginx + TLS (optional but recommended)

```bash
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/delivery-estimate
sudo vim /etc/nginx/sites-available/delivery-estimate   # set your domain
sudo ln -s /etc/nginx/sites-available/delivery-estimate /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# TLS via Let's Encrypt
sudo certbot --nginx -d your-domain.com
```

With nginx in front, set `DJANGO_SSL_REDIRECT=false` in `/etc/delivery-estimate.env` (nginx does the redirect) and reload: `sudo systemctl reload delivery-estimate`.

### Subsequent deploys

```bash
cd /var/www/delivery-estimate
sudo -u delivery ./deploy/deploy.sh
```

That does `git pull → pip install → migrate → collectstatic → systemctl reload`. Gunicorn reloads workers gracefully on SIGHUP, so requests in flight finish without disruption.

### Without nginx

Gunicorn can face the internet directly. WhiteNoise handles static files. Keep `DJANGO_SSL_REDIRECT=true` and terminate TLS somewhere (Cloudflare, Caddy, or `gunicorn --certfile/--keyfile`).

---

## Running the tests

```bash
source .venv/bin/activate
python manage.py test core
```

14 tests pass. One per tier branch, plus pincode resolution edge cases (exact, district, state, region, unserviceable, malformed) and percentile math sanity.

---

## Runtime tuning

Two knobs in `delivery_estimate/settings.py`:

```python
DELIVERY_PROMISE_PERCENTILE = 0.80   # Promise at the 80th percentile
HANDLING_BUFFER_DAYS = 1             # Warehouse processing time
```

Change the percentile to `0.95` and restart — every estimate becomes more conservative. This is the central business knob: trade faster promised dates against more broken promises.
