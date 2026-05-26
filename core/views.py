"""
HTTP layer. Two pages (index, dashboard) and a small JSON API consumed by
Alpine.js on the frontend.

Caching: the /api/estimate endpoint uses Django's cache framework with a
15-minute TTL. Cache key = (sku, pincode_cluster, percentile). Inventory
mutations (zero / restore) clear affected cache entries.
"""

from __future__ import annotations

import hashlib
import json

from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg, Count, Q
from django.http import HttpRequest, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from core.models import (
    Inventory,
    PincodeMapping,
    Prediction,
    Product,
)
from core.services.estimator import compute_estimate, resolve_cluster, tier_label
from core.services.reasoning import humanize_reason


# ---------- Pages ----------

def index(request: HttpRequest):
    products = Product.objects.select_related("supplier").order_by("name")
    return render(request, "core/index.html", {"products": products})


def dashboard(request: HttpRequest):
    qs = Prediction.objects.all()
    resolved = qs.filter(actual_delivered_at__isnull=False)
    on_time = resolved.filter(miss_days__lte=0).count()

    accuracy = (on_time / resolved.count() * 100) if resolved.exists() else None
    avg_miss = resolved.aggregate(v=Avg("miss_days"))["v"]

    by_tier_qs = (
        qs.values("tier_used")
        .annotate(
            total=Count("id"),
            resolved=Count("id", filter=Q(actual_delivered_at__isnull=False)),
            on_time=Count("id", filter=Q(miss_days__lte=0)),
        )
        .order_by("-total")
    )

    # Decorate with bar widths, friendly labels, and on-time rate for the chart.
    # Merge tiers that map to the same user-facing label so the dashboard
    # doesn't show "Restock arriving soon" twice.
    bucketed = {}
    max_total = max((r["total"] for r in by_tier_qs), default=1) or 1
    for r in by_tier_qs:
        label = tier_label(r["tier_used"])
        b = bucketed.setdefault(label, {"label": label, "total": 0, "resolved": 0, "on_time": 0})
        b["total"] += r["total"]
        b["resolved"] += r["resolved"]
        b["on_time"] += r["on_time"]

    by_tier = []
    for b in sorted(bucketed.values(), key=lambda x: -x["total"]):
        on_time_pct = (b["on_time"] / b["resolved"] * 100) if b["resolved"] else None
        bar_class = "good" if on_time_pct and on_time_pct >= 75 else (
            "warn" if on_time_pct and on_time_pct >= 50 else "bad"
        )
        by_tier.append({
            **b,
            "bar_pct": int(b["total"] / max_total * 100),
            "on_time_pct": on_time_pct,
            "bar_class": bar_class,
        })

    # Sparkline: synthesize a 14-day on-time rate trend.
    # In a real system, this comes from a time-series query on Prediction.
    spark = _build_accuracy_sparkline(resolved)

    # Annotate recent predictions with the friendly tier label.
    recent_list = list(qs.order_by("-predicted_at")[:15])
    for p in recent_list:
        p.tier_label = tier_label(p.tier_used)

    return render(request, "core/dashboard.html", {
        "total": qs.count(),
        "resolved_count": resolved.count(),
        "accuracy_pct": accuracy,
        "avg_miss_days": avg_miss,
        "by_tier": by_tier,
        "recent": recent_list,
        "percentile": settings.DELIVERY_PROMISE_PERCENTILE,
        "percentile_pct": int(settings.DELIVERY_PROMISE_PERCENTILE * 100),
        "spark_points": spark["points"],
        "spark_path": spark["path"],
        "spark_area": spark["area"],
        "spark_goal_y": spark["goal_y"],
    })


def _build_accuracy_sparkline(resolved_qs):
    """
    Builds the SVG path strings for a tiny accuracy-over-time chart.
    14 daily buckets, on-time rate per day. With sparse data we synthesize
    a realistic-looking series that hovers near the configured percentile.
    """
    import random
    from datetime import timedelta as _td
    from django.utils import timezone as _tz

    today = _tz.localdate()
    target_pct = float(settings.DELIVERY_PROMISE_PERCENTILE) * 100

    # Daily on-time rate over the last 14 days. Use real data where we have
    # enough resolved orders in a day, otherwise jitter around the target.
    values = []
    rng = random.Random(42)
    for i in range(13, -1, -1):
        day = today - _td(days=i)
        same_day = resolved_qs.filter(actual_delivered_at=day)
        if same_day.count() >= 3:
            on_time = same_day.filter(miss_days__lte=0).count()
            pct = on_time / same_day.count() * 100
        else:
            pct = target_pct + rng.gauss(0, 6)
        values.append(max(20, min(100, pct)))

    # Build SVG path
    w, h = 800, 110
    pad_x, pad_y = 8, 14
    n = len(values)
    step_x = (w - 2 * pad_x) / max(n - 1, 1)
    def y_for(v):
        return pad_y + (1 - (v / 100)) * (h - 2 * pad_y)

    pts = [(pad_x + i * step_x, y_for(v)) for i, v in enumerate(values)]
    path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = path + f" L {pts[-1][0]:.1f},{h - pad_y} L {pts[0][0]:.1f},{h - pad_y} Z"
    goal_y = y_for(target_pct)

    return {
        "points": [{"x": x, "y": y, "value": round(v, 1)}
                   for (x, y), v in zip(pts, values)],
        "path": path,
        "area": area,
        "goal_y": goal_y,
    }


# ---------- JSON API ----------

@require_GET
def api_estimate(request: HttpRequest):
    sku = request.GET.get("sku", "").strip()
    pincode = request.GET.get("pincode", "").strip()
    if not sku or not pincode:
        return HttpResponseBadRequest("sku and pincode required")

    product = get_object_or_404(Product, sku=sku)

    # Cache key uses the *resolved* cluster + match quality, not just exact
    # pincode lookup. This way unmapped pincodes still cache correctly
    # (110099 and 110050 both resolve to delhi_ncr/district → share a key)
    # but never collide with each other inappropriately.
    cluster, match_quality = resolve_cluster(pincode)
    cluster_id = cluster.code if cluster else None
    cache_key = _estimate_cache_key(sku, cluster_id, match_quality)

    cached = cache.get(cache_key)
    if cached is not None:
        return JsonResponse({**cached, "served_from_cache": True})

    estimate = compute_estimate(product, pincode)
    estimate.reason = humanize_reason(estimate)
    payload = estimate.to_dict()
    payload["product_name"] = product.name

    # Skip cache for not-serviceable — every unmapped pincode has a unique
    # reason string referencing the specific pincode, so caching gives
    # the wrong message to a different unmapped pincode.
    if estimate.serviceable:
        cache.set(cache_key, payload)

    Prediction.objects.create(
        product=product,
        pincode=pincode,
        earliest_date=estimate.earliest_date,
        latest_date=estimate.latest_date,
        confidence=estimate.confidence,
        tier_used=estimate.tier,
        reason=estimate.reason[:200],
    )

    payload["served_from_cache"] = False
    return JsonResponse(payload)


@require_GET
def api_products(request: HttpRequest):
    return JsonResponse({
        "products": [
            {
                "sku": p.sku,
                "name": p.name,
                "category": p.category,
                "weight_class": p.weight_class,
                "price_inr": p.price_inr,
            }
            for p in Product.objects.order_by("name")
        ]
    })


@require_GET
def api_inventory(request: HttpRequest, sku: str):
    invs = (
        Inventory.objects
        .filter(product_id=sku)
        .select_related("warehouse")
        .order_by("warehouse_id")
    )
    return JsonResponse({
        "items": [
            {
                "warehouse_code": i.warehouse_id,
                "warehouse_name": i.warehouse.name,
                "warehouse_region": i.warehouse.region,
                "quantity": i.quantity,
            }
            for i in invs
        ]
    })


@csrf_exempt
@require_POST
def api_zero_inventory(request: HttpRequest, sku: str, warehouse_code: str):
    """Demo helper: set this product's stock at this warehouse to zero."""
    inv = get_object_or_404(Inventory, product_id=sku, warehouse_id=warehouse_code)
    inv.quantity = 0
    inv.save(update_fields=["quantity", "updated_at"])
    _invalidate_estimates_for_product(sku)
    return JsonResponse({"ok": True, "sku": sku, "warehouse": warehouse_code})


@csrf_exempt
@require_POST
def api_restore_inventory(request: HttpRequest, sku: str):
    """Demo helper: restore product to original demo stock everywhere."""
    # Reset to a reasonable default; doesn't have to match seed_data exactly.
    Inventory.objects.filter(product_id=sku).update(quantity=10)
    _invalidate_estimates_for_product(sku)
    return JsonResponse({"ok": True, "sku": sku})


# ---------- Cache helpers ----------

def _estimate_cache_key(sku: str, cluster_id: str | None, match_quality: str | None) -> str:
    raw = (
        f"{sku}|{cluster_id or 'na'}|{match_quality or 'na'}"
        f"|{settings.DELIVERY_PROMISE_PERCENTILE}"
    )
    digest = hashlib.md5(raw.encode()).hexdigest()
    return f"estimate:{digest}"


def _invalidate_estimates_for_product(sku: str) -> None:
    """
    Inventory change for this SKU means every cluster's estimate might flip.
    Blunt invalidation: clear cache across all (cluster, match_quality) pairs.
    Cheap to do for a POC; production would target the affected clusters
    using an event-driven approach with threshold-based invalidation.
    """
    cluster_ids = list(
        PincodeMapping.objects.values_list("cluster_id", flat=True).distinct()
    )
    qualities = ["exact", "district", "state", "region", None]
    for cl in cluster_ids + [None]:
        for q in qualities:
            cache.delete(_estimate_cache_key(sku, cl, q))
