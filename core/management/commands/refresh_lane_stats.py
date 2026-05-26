"""
Recomputes LanePerformance and SupplierReliability from history.

In production this would be a nightly job. Run manually for the demo.

Usage:  python manage.py refresh_lane_stats
"""

from django.core.cache import cache
from django.core.management.base import BaseCommand

from core.services.statistics import (
    refresh_lane_performance,
    refresh_supplier_reliability,
)


class Command(BaseCommand):
    help = "Recompute lane performance and supplier reliability percentiles."

    def handle(self, *args, **kwargs):
        lanes = refresh_lane_performance()
        suppliers = refresh_supplier_reliability()
        # Stats changed — invalidate all cached estimates.
        cache.clear()
        self.stdout.write(self.style.SUCCESS(
            f"Refreshed {lanes} lanes, {suppliers} suppliers. Cache cleared."
        ))
