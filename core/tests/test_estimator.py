"""
Tests for the tier logic. One test per tier — proves the function picks the
right branch under each condition.

Run:  python manage.py test core
"""

from datetime import date, datetime, timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import (
    BatchStatus,
    DeliveryRecord,
    Inventory,
    LanePerformance,
    PincodeCluster,
    PincodeMapping,
    Product,
    ProductionBatch,
    Supplier,
    SupplierReliability,
    Warehouse,
    WeightClass,
)
from core.services.estimator import Tier, compute_estimate
from core.services.statistics import percentile


class PercentileTests(TestCase):
    def test_median_of_odd(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 0.50), 3.0)

    def test_p80_known(self):
        vals = list(range(1, 11))  # 1..10
        # Linear interpolation: (10-1)*0.8 = 7.2 → 8 + 0.2*(9-8) = 8.2
        self.assertAlmostEqual(percentile(vals, 0.80), 8.2, places=2)

    def test_single_value(self):
        self.assertEqual(percentile([7.5], 0.95), 7.5)


@override_settings(
    DELIVERY_PROMISE_PERCENTILE=0.80,
    HANDLING_BUFFER_DAYS=1,
    MIN_SAMPLES_FOR_LANE=5,
    DEFAULT_TRANSIT_DAYS=10,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
)
class EstimatorTierTests(TestCase):
    """
    For each test we set up just enough state to force one specific tier.
    """

    def setUp(self):
        self.wh_del = Warehouse.objects.create(
            code="WH-DEL", name="Delhi", pincode="110037", region="north"
        )
        self.wh_mum = Warehouse.objects.create(
            code="WH-MUM", name="Mumbai", pincode="400099", region="west"
        )
        self.cluster = PincodeCluster.objects.create(
            code="delhi_ncr", name="Delhi NCR", region="north"
        )
        PincodeMapping.objects.create(pincode="110001", cluster=self.cluster)

        self.supplier = Supplier.objects.create(code="SUP-X", name="Test Supplier")
        self.product = Product.objects.create(
            sku="SKU-1", name="Test Sofa", category="sofa",
            weight_class=WeightClass.LARGE, supplier=self.supplier,
        )

    # ---- Tier: in stock at nearest warehouse ----

    def test_in_stock_nearest(self):
        Inventory.objects.create(product=self.product, warehouse=self.wh_del, quantity=10)
        LanePerformance.objects.create(
            warehouse=self.wh_del, cluster=self.cluster,
            median_days=3, p80_days=5, p95_days=8, sample_size=20,
        )
        est = compute_estimate(self.product, "110001")
        self.assertEqual(est.tier, Tier.IN_STOCK_NEAREST)
        self.assertEqual(est.confidence, "high")
        self.assertFalse(est.is_range)
        # today + handling(1) + p80(5) = today + 6
        self.assertEqual(est.earliest_date, timezone.localdate() + timedelta(days=6))

    # ---- Tier: in stock at farther warehouse ----

    def test_in_stock_farther(self):
        # Only Mumbai (different region) has stock
        Inventory.objects.create(product=self.product, warehouse=self.wh_mum, quantity=10)
        LanePerformance.objects.create(
            warehouse=self.wh_mum, cluster=self.cluster,
            median_days=5, p80_days=7, p95_days=10, sample_size=20,
        )
        est = compute_estimate(self.product, "110001")
        self.assertEqual(est.tier, Tier.IN_STOCK_FARTHER)
        self.assertEqual(est.confidence, "high")
        self.assertEqual(est.warehouse_code, "WH-MUM")

    # ---- Tier: in stock but no lane stats ----

    def test_in_stock_no_stats(self):
        Inventory.objects.create(product=self.product, warehouse=self.wh_del, quantity=5)
        # No LanePerformance row, OR insufficient samples
        LanePerformance.objects.create(
            warehouse=self.wh_del, cluster=self.cluster,
            median_days=2, p80_days=3, p95_days=4, sample_size=2,  # below threshold
        )
        est = compute_estimate(self.product, "110001")
        self.assertEqual(est.tier, Tier.IN_STOCK_NO_STATS)
        self.assertEqual(est.confidence, "medium")

    # ---- Tier: awaiting batch with known supplier ----

    def test_awaiting_batch_with_history(self):
        # No inventory anywhere
        promised = timezone.localdate() + timedelta(days=10)
        ProductionBatch.objects.create(
            batch_id="B1", product=self.product, warehouse=self.wh_del,
            supplier=self.supplier, quantity=20, promised_date=promised,
            status=BatchStatus.SCHEDULED,
        )
        SupplierReliability.objects.create(
            supplier=self.supplier, median_slip_days=2,
            p80_slip_days=4, sample_size=10,
        )
        LanePerformance.objects.create(
            warehouse=self.wh_del, cluster=self.cluster,
            median_days=3, p80_days=5, p95_days=8, sample_size=20,
        )
        est = compute_estimate(self.product, "110001")
        self.assertEqual(est.tier, Tier.AWAITING_BATCH)
        self.assertTrue(est.is_range)
        # earliest = promised + handling(1) + p80(5) = promised + 6
        self.assertEqual(est.earliest_date, promised + timedelta(days=6))
        self.assertEqual(est.latest_date, est.earliest_date + timedelta(days=4))

    # ---- Tier: awaiting batch but no supplier history ----

    def test_awaiting_batch_no_slip_data(self):
        promised = timezone.localdate() + timedelta(days=10)
        ProductionBatch.objects.create(
            batch_id="B1", product=self.product, warehouse=self.wh_del,
            supplier=self.supplier, quantity=20, promised_date=promised,
            status=BatchStatus.SCHEDULED,
        )
        LanePerformance.objects.create(
            warehouse=self.wh_del, cluster=self.cluster,
            median_days=3, p80_days=5, p95_days=8, sample_size=20,
        )
        # No SupplierReliability row at all
        est = compute_estimate(self.product, "110001")
        self.assertEqual(est.tier, Tier.AWAITING_BATCH_NO_SLIP)
        self.assertEqual(est.confidence, "low")
        self.assertTrue(est.is_range)

    # ---- Tier: soft fallback when nothing known ----

    def test_soft_fallback_when_nothing(self):
        # No inventory, no batch
        est = compute_estimate(self.product, "110001")
        self.assertEqual(est.tier, Tier.SOFT_FALLBACK)
        self.assertEqual(est.confidence, "low")
        self.assertTrue(est.is_range)

    # ---- Pincode resolution ----

    def test_truly_unserviceable_pincode(self):
        # 999999: first digit doesn't match any cluster prefix in our map
        Inventory.objects.create(product=self.product, warehouse=self.wh_del, quantity=10)
        est = compute_estimate(self.product, "999999")
        self.assertEqual(est.tier, Tier.NOT_SERVICEABLE)
        self.assertFalse(est.serviceable)

    def test_new_pincode_in_same_district_keeps_confidence(self):
        # 110099 isn't mapped but matches prefix 110 → delhi_ncr (district match)
        Inventory.objects.create(product=self.product, warehouse=self.wh_del, quantity=10)
        LanePerformance.objects.create(
            warehouse=self.wh_del, cluster=self.cluster,
            median_days=3, p80_days=5, p95_days=8, sample_size=20,
        )
        est = compute_estimate(self.product, "110099")
        self.assertEqual(est.tier, Tier.IN_STOCK_NEAREST)
        # Same district = no downgrade
        self.assertEqual(est.confidence, "high")
        self.assertIn("district", est.reason)

    def test_new_pincode_in_same_region_downgrades(self):
        # 120001 (Faridabad-like): no district match (no 120xxx in map),
        # no state match (no 12xxxx), but region match (1xxxxx → delhi_ncr).
        Inventory.objects.create(product=self.product, warehouse=self.wh_del, quantity=10)
        LanePerformance.objects.create(
            warehouse=self.wh_del, cluster=self.cluster,
            median_days=3, p80_days=5, p95_days=8, sample_size=20,
        )
        est = compute_estimate(self.product, "120001")
        self.assertEqual(est.tier, Tier.IN_STOCK_NEAREST)
        # Region match = two-notch downgrade from high → low
        self.assertEqual(est.confidence, "low")
        self.assertIn("region", est.reason)

    def test_malformed_pincode_is_not_serviceable(self):
        Inventory.objects.create(product=self.product, warehouse=self.wh_del, quantity=10)
        for bad in ("", "abc", "12345", "1234567"):
            est = compute_estimate(self.product, bad)
            self.assertEqual(est.tier, Tier.NOT_SERVICEABLE, f"failed for {bad!r}")
            self.assertFalse(est.serviceable)

    # ---- Picks the fastest warehouse, not just the first ----

    def test_picks_fastest_lane(self):
        Inventory.objects.create(product=self.product, warehouse=self.wh_del, quantity=10)
        Inventory.objects.create(product=self.product, warehouse=self.wh_mum, quantity=10)
        # Make Mumbai's lane faster than Delhi's (unusual but tests the logic)
        LanePerformance.objects.create(
            warehouse=self.wh_del, cluster=self.cluster,
            median_days=8, p80_days=10, p95_days=12, sample_size=20,
        )
        LanePerformance.objects.create(
            warehouse=self.wh_mum, cluster=self.cluster,
            median_days=2, p80_days=3, p95_days=4, sample_size=20,
        )
        est = compute_estimate(self.product, "110001")
        self.assertEqual(est.warehouse_code, "WH-MUM")
