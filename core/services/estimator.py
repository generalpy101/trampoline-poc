"""
The heart of the system: tier logic for producing a delivery estimate.

Designed to be read top-to-bottom in 30 seconds. Each tier is one clear
branch with a clear reason. No factories, no strategy patterns. If you
need to know "why did this customer see this date," you read this file.

The actual numerical inputs come from:
  - live Inventory and ProductionBatch tables (current operational state)
  - pre-aggregated LanePerformance table (refreshed nightly)
  - pre-aggregated SupplierReliability table (refreshed nightly)

This separation is what makes the POC scale-friendly later: replace the
percentile lookup with an ML prediction service and nothing else changes.

Pincode resolution:
  Exact match → use as-is.
  Otherwise progressively widen the prefix match (district → state → region).
  Why: Indian pincodes have geographic structure. "110099" with no exact
  mapping is almost certainly in the same delivery zone as "110001-110009"
  if we have those, so we should serve a real estimate (with a small
  confidence downgrade) rather than punting to "we don't know."
  Only when no prefix matches anywhere do we declare "not serviceable."
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Optional

from django.conf import settings
from django.utils import timezone

from core.models import (
    Inventory,
    LanePerformance,
    PincodeMapping,
    Product,
    ProductionBatch,
    SupplierReliability,
    Warehouse,
)


# ---------- Result type ----------

@dataclass
class BreakdownStep:
    """One row in the calculation breakdown — 'handling +1d, P80 transit +5d, ...'"""
    label: str
    days: int
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Estimate:
    """The customer-facing response. Same shape from every tier."""
    earliest_date: date
    latest_date: date
    confidence: str        # 'high' | 'medium' | 'low'
    tier: str              # for debugging and dashboards
    reason: str            # human-readable explanation
    warehouse_code: Optional[str] = None
    is_range: bool = False
    serviceable: bool = True   # False means: don't show a date, show "not serviced"
    breakdown: list = None     # list of BreakdownStep
    total_days_from_today: int = 0
    sample_size: int = 0       # how many historical orders backed the lane stat

    def __post_init__(self):
        if self.breakdown is None:
            self.breakdown = []

    def to_dict(self) -> dict:
        d = asdict(self)
        d["earliest_date"] = self.earliest_date.isoformat()
        d["latest_date"] = self.latest_date.isoformat()
        return d


# ---------- Tier identifiers (also used by dashboard) ----------

class Tier:
    IN_STOCK_NEAREST = "in_stock_nearest"
    IN_STOCK_FARTHER = "in_stock_farther"
    IN_STOCK_NO_STATS = "in_stock_no_stats"
    AWAITING_BATCH = "awaiting_batch"
    AWAITING_BATCH_NO_SLIP = "awaiting_batch_no_slip"
    SOFT_FALLBACK = "soft_fallback"
    NOT_SERVICEABLE = "not_serviceable"


# Pincode match quality, surfaced through confidence and reason text.
class PincodeMatch:
    EXACT = "exact"          # pincode is in our explicit map
    DISTRICT = "district"    # first 3 digits match a known pincode (~same district)
    STATE = "state"          # first 2 digits match (~same state)
    REGION = "region"        # first digit matches (~same region of India)


# ---------- Public entrypoint ----------

def compute_estimate(product: Product, pincode: str) -> Estimate:
    """
    Walks through the tiers in priority order and returns the first that fits.
    """
    today = timezone.localdate()
    handling = settings.HANDLING_BUFFER_DAYS

    cluster, match_quality = resolve_cluster(pincode)
    if cluster is None:
        return _not_serviceable(today, pincode)

    stocked = list(
        Inventory.objects
        .select_related("warehouse")
        .filter(product=product, quantity__gt=0)
    )

    if stocked:
        return _estimate_in_stock(today, handling, stocked, cluster, match_quality)

    return _estimate_out_of_stock(today, handling, product, cluster, match_quality)


# ---------- Tier implementations ----------

def _estimate_in_stock(today, handling, stocked, cluster, match_quality) -> Estimate:
    """
    Pick the warehouse with the fastest known lane to this cluster.
    If a warehouse has no lane stats yet, fall back to platform default transit.
    """
    best_warehouse = None
    best_days = None
    has_stats_for_best = False
    best_sample_size = 0

    for inv in stocked:
        days, has_stats, samples = _lane_transit_days(inv.warehouse, cluster)
        if best_days is None or days < best_days:
            best_warehouse = inv.warehouse
            best_days = days
            has_stats_for_best = has_stats
            best_sample_size = samples

    transit_days = int(round(best_days))
    total_days = handling + transit_days
    target_date = today + timedelta(days=total_days)

    is_nearest_match = (
        best_warehouse is not None and best_warehouse.region == cluster.region
    )

    if not has_stats_for_best:
        tier = Tier.IN_STOCK_NO_STATS
        confidence = "medium"
        reason = (
            f"In stock at our {best_warehouse.name} warehouse "
            f"(no lane history yet, using platform default)"
        )
    elif is_nearest_match:
        tier = Tier.IN_STOCK_NEAREST
        confidence = "high"
        reason = f"In stock at our {best_warehouse.name} warehouse"
    else:
        tier = Tier.IN_STOCK_FARTHER
        confidence = "high"
        reason = (
            f"In stock at our {best_warehouse.name} warehouse — "
            f"farther but fastest available"
        )

    # Apply pincode-match downgrade if we approximated the cluster.
    confidence, reason = _apply_match_quality(
        confidence, reason, match_quality, cluster
    )

    pct_label = int(settings.DELIVERY_PROMISE_PERCENTILE * 100)
    breakdown = [
        BreakdownStep(label="Warehouse handling", days=handling,
                      note="prep & dispatch"),
        BreakdownStep(
            label=f"Transit time (P{pct_label})",
            days=transit_days,
            note=(f"based on {best_sample_size} recent deliveries"
                  if has_stats_for_best else "platform default — no lane history yet"),
        ),
    ]

    return Estimate(
        earliest_date=target_date,
        latest_date=target_date,
        confidence=confidence,
        tier=tier,
        reason=reason,
        warehouse_code=best_warehouse.code if best_warehouse else None,
        is_range=False,
        breakdown=breakdown,
        total_days_from_today=total_days,
        sample_size=best_sample_size,
    )


def _estimate_out_of_stock(today, handling, product, cluster, match_quality) -> Estimate:
    """
    No stock anywhere. Look for the next scheduled production batch.
    Customer-facing answer is a date range because suppliers slip — and we
    know historically how much each supplier slips.
    """
    batch = (
        ProductionBatch.objects
        .select_related("warehouse", "supplier")
        .filter(
            product=product,
            status__in=("scheduled", "in_transit"),
            promised_date__gte=today,
        )
        .order_by("promised_date")
        .first()
    )

    if batch is None:
        return _soft_fallback_no_batch(today)

    days, has_stats, samples = _lane_transit_days(batch.warehouse, cluster)
    transit_days = int(round(days))
    earliest = batch.promised_date + timedelta(days=handling + transit_days)

    slip = _supplier_slip_days(batch.supplier_id)
    pct_label = int(settings.DELIVERY_PROMISE_PERCENTILE * 100)
    days_until_batch = max(0, (batch.promised_date - today).days)

    breakdown = [
        BreakdownStep(label="Until batch arrives", days=days_until_batch,
                      note=f"supplier {batch.supplier_id}"),
        BreakdownStep(label="Warehouse handling", days=handling, note="prep & dispatch"),
        BreakdownStep(
            label=f"Transit time (P{pct_label})",
            days=transit_days,
            note=(f"based on {samples} recent deliveries"
                  if has_stats else "platform default — no lane history yet"),
        ),
    ]

    if slip is None:
        latest = earliest + timedelta(days=7)
        confidence = "low"
        tier = Tier.AWAITING_BATCH_NO_SLIP
        reason = (
            f"Awaiting next production batch (arrives ~{batch.promised_date.isoformat()}). "
            f"Supplier history unknown — wider date range to be safe."
        )
        breakdown.append(BreakdownStep(
            label="Supplier slip buffer", days=7,
            note="default — no supplier history yet",
        ))
    else:
        slip_days = int(round(slip))
        latest = earliest + timedelta(days=slip_days)
        confidence = "medium" if slip <= 3 else "low"
        tier = Tier.AWAITING_BATCH
        reason = (
            f"Awaiting next production batch. Supplier typically arrives "
            f"within {slip_days} days of the promised date."
        )
        breakdown.append(BreakdownStep(
            label=f"Supplier slip (P{pct_label})",
            days=slip_days,
            note=f"supplier {batch.supplier_id}, historical",
        ))

    confidence, reason = _apply_match_quality(
        confidence, reason, match_quality, cluster
    )

    total_to_earliest = (earliest - today).days

    return Estimate(
        earliest_date=earliest,
        latest_date=latest,
        confidence=confidence,
        tier=tier,
        reason=reason,
        warehouse_code=batch.warehouse_id,
        is_range=earliest != latest,
        breakdown=breakdown,
        total_days_from_today=total_to_earliest,
        sample_size=samples,
    )


def _soft_fallback_no_batch(today) -> Estimate:
    """Out of stock with no scheduled batch — wide low-confidence range."""
    return Estimate(
        earliest_date=today + timedelta(weeks=3),
        latest_date=today + timedelta(weeks=5),
        confidence="low",
        tier=Tier.SOFT_FALLBACK,
        reason="Out of stock with no scheduled production batch.",
        is_range=True,
    )


def _not_serviceable(today, pincode: str) -> Estimate:
    """
    Genuinely unservicable pincode — no warehouse covers this region.
    Returns serviceable=False so the UI can show a different state instead
    of pretending to give an estimate.
    """
    return Estimate(
        # We still populate dates to keep the response shape uniform,
        # but the UI should check `serviceable` and render a special state.
        earliest_date=today,
        latest_date=today,
        confidence="low",
        tier=Tier.NOT_SERVICEABLE,
        reason=(
            f"Pincode {pincode} isn't yet in our delivery network. "
            f"We currently serve North, West, South, and East metro regions."
        ),
        serviceable=False,
    )


# ---------- Pincode resolution ----------

def resolve_cluster(pincode: str):
    """
    Returns (cluster, match_quality) where match_quality is one of the
    PincodeMatch constants, or (None, None) if no prefix matches at all.

    Tries progressively wider prefixes so a new pincode in a known region
    gets a real estimate rather than a fake "low confidence" punt.
    """
    pincode = pincode.strip()
    if not pincode.isdigit() or len(pincode) != 6:
        return None, None

    # 1. Exact match — the happy path
    exact = (
        PincodeMapping.objects
        .select_related("cluster")
        .filter(pincode=pincode)
        .first()
    )
    if exact:
        return exact.cluster, PincodeMatch.EXACT

    # 2. Progressively wider prefix matches: district (3) → state (2) → region (1)
    for prefix_len, quality in [
        (3, PincodeMatch.DISTRICT),
        (2, PincodeMatch.STATE),
        (1, PincodeMatch.REGION),
    ]:
        prefix = pincode[:prefix_len]
        match = (
            PincodeMapping.objects
            .select_related("cluster")
            .filter(pincode__startswith=prefix)
            .first()
        )
        if match:
            return match.cluster, quality

    return None, None


def _apply_match_quality(confidence: str, reason: str, match_quality: str, cluster):
    """
    Downgrade confidence and append a clarifying note when we approximated
    the pincode → cluster mapping. Exact matches pass through unchanged.

    Downgrade rules:
      district (3-digit prefix): no change — same delivery district is fine
      state    (2-digit prefix): one notch (high → medium, medium → low)
      region   (1-digit prefix): two notches (high → low)
    """
    if match_quality == PincodeMatch.EXACT:
        return confidence, reason

    levels = ["high", "medium", "low"]
    cur = levels.index(confidence) if confidence in levels else 2

    if match_quality == PincodeMatch.DISTRICT:
        suffix = f" (new pincode in this district — same delivery zone)"
        new_idx = cur
    elif match_quality == PincodeMatch.STATE:
        suffix = f" (new pincode — approximated to the nearest covered area)"
        new_idx = min(cur + 1, 2)
    else:  # REGION
        suffix = (
            f" (new pincode in the {cluster.region} region — "
            f"approximating from {cluster.name})"
        )
        new_idx = min(cur + 2, 2)

    return levels[new_idx], reason + "." + suffix if not reason.endswith(".") else reason + suffix


# ---------- Helpers ----------

def _lane_transit_days(warehouse: Warehouse, cluster) -> tuple[float, bool, int]:
    """
    Returns (days, has_stats, sample_size). When stats exist, uses the configured
    percentile from settings. Otherwise falls back to platform default.
    """
    try:
        lane = LanePerformance.objects.get(warehouse=warehouse, cluster=cluster)
    except LanePerformance.DoesNotExist:
        return (float(settings.DEFAULT_TRANSIT_DAYS), False, 0)

    if lane.sample_size < settings.MIN_SAMPLES_FOR_LANE:
        return (float(settings.DEFAULT_TRANSIT_DAYS), False, lane.sample_size)

    pct = settings.DELIVERY_PROMISE_PERCENTILE
    if pct >= 0.95:
        return (lane.p95_days, True, lane.sample_size)
    if pct >= 0.80:
        return (lane.p80_days, True, lane.sample_size)
    return (lane.median_days, True, lane.sample_size)


def _supplier_slip_days(supplier_id: str) -> Optional[float]:
    try:
        rel = SupplierReliability.objects.get(supplier_id=supplier_id)
    except SupplierReliability.DoesNotExist:
        return None
    if rel.sample_size < 3:
        return None
    pct = settings.DELIVERY_PROMISE_PERCENTILE
    return rel.p80_slip_days if pct >= 0.80 else rel.median_slip_days
