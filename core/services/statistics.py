"""
Percentile aggregation over historical delivery records and production batches.

Run by `manage.py refresh_lane_stats`. In a real system this would be a
nightly job (Airflow, dbt, or simply cron).

Why we do this in code rather than SQL: SQLite (our POC database) doesn't
have a native PERCENTILE_CONT. Postgres does, and in production the whole
aggregation would be one SQL statement run by the data warehouse — but the
shape of what we compute would be identical.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from core.models import (
    DeliveryRecord,
    LanePerformance,
    PincodeCluster,
    ProductionBatch,
    Supplier,
    SupplierReliability,
    Warehouse,
)


def percentile(sorted_values: list[float], pct: float) -> float:
    """
    Linear-interpolated percentile, matching what Postgres PERCENTILE_CONT does.
    """
    if not sorted_values:
        raise ValueError("Cannot compute percentile of empty list")
    if len(sorted_values) == 1:
        return float(sorted_values[0])

    k = (len(sorted_values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return float(sorted_values[f])
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return float(d0 + d1)


def refresh_lane_performance(min_samples: int = 5) -> int:
    """
    Recomputes LanePerformance from DeliveryRecord. Returns rows written.

    Buckets with fewer than `min_samples` records are dropped — not enough
    signal to trust. The estimator falls back to platform defaults for those.
    """
    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)

    qs = DeliveryRecord.objects.filter(
        delivered_at__isnull=False, transit_days__isnull=False
    ).values("warehouse_id", "cluster_id", "transit_days")

    for row in qs:
        key = (row["warehouse_id"], row["cluster_id"])
        buckets[key].append(row["transit_days"])

    written = 0
    with transaction.atomic():
        LanePerformance.objects.all().delete()
        for (wh_id, cl_id), values in buckets.items():
            if len(values) < min_samples:
                continue
            values.sort()
            LanePerformance.objects.create(
                warehouse_id=wh_id,
                cluster_id=cl_id,
                median_days=percentile(values, 0.50),
                p80_days=percentile(values, 0.80),
                p95_days=percentile(values, 0.95),
                sample_size=len(values),
                computed_at=timezone.now(),
            )
            written += 1

    return written


def refresh_supplier_reliability() -> int:
    """
    Recomputes how much each supplier slips relative to their promised date.
    For the POC we derive 'slip' synthetically from completed batches whose
    `updated_at - promised_date` we treat as the actual arrival delta.
    In a real system you'd track actual_arrival_date explicitly.
    """
    buckets: dict[str, list[int]] = defaultdict(list)

    qs = ProductionBatch.objects.filter(status="received").select_related("supplier")
    for batch in qs:
        slip_days = (batch.updated_at.date() - batch.promised_date).days
        if slip_days < 0:
            slip_days = 0
        buckets[batch.supplier_id].append(slip_days)

    written = 0
    with transaction.atomic():
        SupplierReliability.objects.all().delete()
        for supplier_id, values in buckets.items():
            if not values:
                continue
            values.sort()
            SupplierReliability.objects.create(
                supplier_id=supplier_id,
                median_slip_days=percentile(values, 0.50),
                p80_slip_days=percentile(values, 0.80),
                sample_size=len(values),
                computed_at=timezone.now(),
            )
            written += 1
    return written
