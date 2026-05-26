"""
Simulates real orders being delivered, so the dashboard has accuracy data.

For every Prediction without an actual_delivered_at, samples an "actual"
arrival date from the same distribution used to seed history (with some
extra noise to ensure interesting misses).

Run this after browsing the app a bit to populate the dashboard.

Usage:  python manage.py resolve_predictions
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Prediction


class Command(BaseCommand):
    help = "Fake-resolve outstanding predictions so the dashboard has data."

    def handle(self, *args, **kwargs):
        today = timezone.localdate()
        resolved = 0
        for pred in Prediction.objects.filter(actual_delivered_at__isnull=True):
            # Skew most actuals close to the predicted date with occasional misses.
            target = pred.latest_date
            jitter = int(round(random.normalvariate(0, 2)))
            actual = target + timedelta(days=jitter)
            if actual > today:
                continue  # not "delivered yet"
            pred.actual_delivered_at = actual
            pred.miss_days = (actual - pred.latest_date).days
            pred.save(update_fields=["actual_delivered_at", "miss_days"])
            resolved += 1
        self.stdout.write(self.style.SUCCESS(
            f"Resolved {resolved} predictions with synthetic delivery dates."
        ))
