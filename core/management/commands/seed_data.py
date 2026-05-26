"""
Wipes the database and seeds a realistic, demo-friendly dataset.

Seeding goals:
  - Three warehouses spread across India
  - Five pincode clusters
  - Eight products across categories with mixed stock states
  - Three suppliers with distinct reliability profiles
  - ~300 historical delivery records with deliberate per-lane variance
  - A handful of pending production batches

The variance is what makes the demo land: different lanes produce visibly
different P80 estimates, and out-of-stock items show real date ranges
because supplier slip distributions differ.

Usage:  python manage.py seed_data
"""

import random
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import (
    BatchStatus,
    DeliveryRecord,
    Inventory,
    PincodeCluster,
    PincodeMapping,
    Prediction,
    Product,
    ProductionBatch,
    Supplier,
    WeightClass,
    Warehouse,
)


# ----- Static catalog -----

WAREHOUSES = [
    ("WH-DEL", "Delhi", "110037", "north"),
    ("WH-MUM", "Mumbai", "400099", "west"),
    ("WH-BLR", "Bangalore", "560100", "south"),
]

CLUSTERS = [
    ("delhi_ncr", "Delhi NCR", "north"),
    ("mumbai_metro", "Mumbai Metro", "west"),
    ("bangalore_metro", "Bangalore Metro", "south"),
    ("chennai_metro", "Chennai Metro", "south"),
    ("kolkata_metro", "Kolkata Metro", "east"),
]

# Representative pincodes — extend with whatever ranges you want to support.
PINCODE_MAP = [
    *[(f"11000{i}", "delhi_ncr") for i in range(1, 10)],
    *[(f"40000{i}", "mumbai_metro") for i in range(1, 10)],
    *[(f"56000{i}", "bangalore_metro") for i in range(1, 10)],
    *[(f"60000{i}", "chennai_metro") for i in range(1, 10)],
    *[(f"70000{i}", "kolkata_metro") for i in range(1, 10)],
]

SUPPLIERS = [
    ("SUP-RELIABLE", "Anand Mfg (reliable)"),
    ("SUP-AVERAGE", "Coastal Woodworks (average)"),
    ("SUP-FLAKY", "Maharaja Crafts (flaky)"),
]

# (sku, name, category, weight_class, price, supplier)
PRODUCTS = [
    ("SOFA-001", "Linen 3-Seater Sofa", "sofa", WeightClass.LARGE, 38000, "SUP-RELIABLE"),
    ("SOFA-002", "Velvet 2-Seater", "sofa", WeightClass.LARGE, 32000, "SUP-AVERAGE"),
    ("BED-001", "Queen Bed Frame (Walnut)", "bed", WeightClass.XL, 45000, "SUP-RELIABLE"),
    ("BED-002", "King Bed Frame (Mango)", "bed", WeightClass.XL, 55000, "SUP-FLAKY"),
    ("TABLE-001", "Dining Table for 4", "dining", WeightClass.LARGE, 28000, "SUP-AVERAGE"),
    ("CHAIR-001", "Ergonomic Office Chair", "chair", WeightClass.MEDIUM, 12000, "SUP-RELIABLE"),
    ("SHELF-001", "Modular Bookshelf", "storage", WeightClass.LARGE, 18000, "SUP-AVERAGE"),
    ("LAMP-001", "Brass Floor Lamp", "lighting", WeightClass.SMALL, 6500, "SUP-RELIABLE"),
]


# ----- Lane variance profile -----
# (warehouse, cluster) -> (mean_days, stddev). Drives the percentile spread.
LANE_PROFILES = {
    # Delhi warehouse
    ("WH-DEL", "delhi_ncr"):       (3.0, 0.8),   # fast, tight
    ("WH-DEL", "mumbai_metro"):    (6.5, 1.5),
    ("WH-DEL", "bangalore_metro"): (8.0, 2.5),
    ("WH-DEL", "chennai_metro"):   (9.0, 3.0),   # slow, variable
    ("WH-DEL", "kolkata_metro"):   (7.0, 2.0),
    # Mumbai warehouse
    ("WH-MUM", "delhi_ncr"):       (6.0, 1.5),
    ("WH-MUM", "mumbai_metro"):    (2.5, 0.6),   # fast, tight
    ("WH-MUM", "bangalore_metro"): (5.5, 1.2),
    ("WH-MUM", "chennai_metro"):   (6.0, 1.8),
    ("WH-MUM", "kolkata_metro"):   (8.5, 2.5),
    # Bangalore warehouse
    ("WH-BLR", "delhi_ncr"):       (8.5, 2.8),
    ("WH-BLR", "mumbai_metro"):    (5.0, 1.4),
    ("WH-BLR", "bangalore_metro"): (2.0, 0.5),   # fast, tight
    ("WH-BLR", "chennai_metro"):   (3.5, 0.8),
    ("WH-BLR", "kolkata_metro"):   (9.5, 3.5),   # slow, variable
}


# ----- Supplier slip profiles (days) -----
SUPPLIER_SLIP = {
    "SUP-RELIABLE": (0.5, 0.6),
    "SUP-AVERAGE":  (3.0, 1.4),
    "SUP-FLAKY":    (7.0, 3.5),
}


def _sample_positive(mean: float, sd: float) -> int:
    """Sample a non-negative integer from N(mean, sd), bounded to [1, 60]."""
    v = random.normalvariate(mean, sd)
    return max(1, min(60, int(round(v))))


def _sample_slip(mean: float, sd: float) -> int:
    v = random.normalvariate(mean, sd)
    return max(0, min(30, int(round(v))))


class Command(BaseCommand):
    help = "Wipe and reseed the database with demo data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--orders-per-lane",
            type=int,
            default=22,
            help="Historical delivery records to generate per lane.",
        )

    @transaction.atomic
    def handle(self, *args, orders_per_lane: int, **kwargs):
        random.seed(42)  # deterministic for demos

        self.stdout.write("Wiping existing data…")
        for m in (
            Prediction, DeliveryRecord, ProductionBatch, Inventory, Product,
            Supplier, PincodeMapping, PincodeCluster, Warehouse,
        ):
            m.objects.all().delete()

        self.stdout.write("Seeding warehouses, clusters, pincode map, suppliers…")
        warehouses = {
            code: Warehouse.objects.create(code=code, name=n, pincode=p, region=r)
            for code, n, p, r in WAREHOUSES
        }
        clusters = {
            code: PincodeCluster.objects.create(code=code, name=n, region=r)
            for code, n, r in CLUSTERS
        }
        for pin, cl_code in PINCODE_MAP:
            PincodeMapping.objects.create(pincode=pin, cluster=clusters[cl_code])
        suppliers = {
            code: Supplier.objects.create(code=code, name=n)
            for code, n in SUPPLIERS
        }

        self.stdout.write("Seeding products and inventory…")
        products = {}
        for sku, name, cat, wc, price, sup_code in PRODUCTS:
            products[sku] = Product.objects.create(
                sku=sku, name=name, category=cat, weight_class=wc,
                price_inr=price, supplier=suppliers[sup_code],
            )

        # Inventory: deliberately varied so the demo has interesting cases.
        # Some products in stock everywhere, some only at one warehouse,
        # some out of stock to demonstrate the batch-await tier.
        inventory_plan = {
            "SOFA-001":  {"WH-DEL": 12, "WH-MUM": 8,  "WH-BLR": 0},
            "SOFA-002":  {"WH-DEL": 0,  "WH-MUM": 0,  "WH-BLR": 0},   # out everywhere
            "BED-001":   {"WH-DEL": 5,  "WH-MUM": 0,  "WH-BLR": 4},
            "BED-002":   {"WH-DEL": 0,  "WH-MUM": 0,  "WH-BLR": 0},   # out everywhere
            "TABLE-001": {"WH-DEL": 0,  "WH-MUM": 6,  "WH-BLR": 2},
            "CHAIR-001": {"WH-DEL": 30, "WH-MUM": 25, "WH-BLR": 28},
            "SHELF-001": {"WH-DEL": 0,  "WH-MUM": 3,  "WH-BLR": 0},
            "LAMP-001":  {"WH-DEL": 50, "WH-MUM": 40, "WH-BLR": 35},
        }
        for sku, by_wh in inventory_plan.items():
            for wh_code, qty in by_wh.items():
                Inventory.objects.create(
                    product=products[sku], warehouse=warehouses[wh_code], quantity=qty,
                )

        self.stdout.write("Seeding production batches for out-of-stock items…")
        today = date.today()
        ProductionBatch.objects.create(
            batch_id="BATCH-SOFA002-001",
            product=products["SOFA-002"],
            warehouse=warehouses["WH-MUM"],
            supplier=suppliers["SUP-AVERAGE"],
            quantity=40,
            promised_date=today + timedelta(days=8),
            status=BatchStatus.IN_TRANSIT,
        )
        ProductionBatch.objects.create(
            batch_id="BATCH-BED002-001",
            product=products["BED-002"],
            warehouse=warehouses["WH-DEL"],
            supplier=suppliers["SUP-FLAKY"],
            quantity=20,
            promised_date=today + timedelta(days=12),
            status=BatchStatus.SCHEDULED,
        )
        # Receive some past batches so supplier reliability has data
        for i in range(8):
            ProductionBatch.objects.create(
                batch_id=f"BATCH-HIST-RELIABLE-{i}",
                product=products["SOFA-001"],
                warehouse=warehouses["WH-DEL"],
                supplier=suppliers["SUP-RELIABLE"],
                quantity=30,
                promised_date=today - timedelta(days=30 + i * 5),
                status=BatchStatus.RECEIVED,
            )
        for i in range(8):
            ProductionBatch.objects.create(
                batch_id=f"BATCH-HIST-AVERAGE-{i}",
                product=products["SOFA-002"],
                warehouse=warehouses["WH-MUM"],
                supplier=suppliers["SUP-AVERAGE"],
                quantity=30,
                promised_date=today - timedelta(days=30 + i * 5),
                status=BatchStatus.RECEIVED,
            )
        for i in range(5):
            ProductionBatch.objects.create(
                batch_id=f"BATCH-HIST-FLAKY-{i}",
                product=products["BED-002"],
                warehouse=warehouses["WH-DEL"],
                supplier=suppliers["SUP-FLAKY"],
                quantity=15,
                promised_date=today - timedelta(days=40 + i * 8),
                status=BatchStatus.RECEIVED,
            )

        # Synthetically backdate received batches so updated_at - promised_date
        # encodes a realistic supplier slip distribution.
        for batch in ProductionBatch.objects.filter(status=BatchStatus.RECEIVED):
            mean, sd = SUPPLIER_SLIP[batch.supplier_id]
            slip = _sample_slip(mean, sd)
            batch.updated_at = timezone.make_aware(
                datetime.combine(batch.promised_date + timedelta(days=slip),
                                 datetime.min.time())
            )
            ProductionBatch.objects.filter(pk=batch.pk).update(
                updated_at=batch.updated_at,
            )

        self.stdout.write(f"Seeding ~{orders_per_lane * len(LANE_PROFILES)} delivery records…")
        all_products = list(products.values())
        order_idx = 0
        for (wh_code, cl_code), (mean, sd) in LANE_PROFILES.items():
            wh = warehouses[wh_code]
            cl = clusters[cl_code]
            for _ in range(orders_per_lane):
                transit = _sample_positive(mean, sd)
                shipped = today - timedelta(days=random.randint(5, 80))
                delivered = shipped + timedelta(days=transit)
                product = random.choice(all_products)
                DeliveryRecord.objects.create(
                    order_id=f"ORD-{order_idx:06d}",
                    product=product,
                    warehouse=wh,
                    cluster=cl,
                    shipped_at=timezone.make_aware(
                        datetime.combine(shipped, datetime.min.time())
                    ),
                    delivered_at=timezone.make_aware(
                        datetime.combine(delivered, datetime.min.time())
                    ),
                    promised_date=shipped + timedelta(days=int(mean + 2)),
                    transit_days=transit,
                )
                order_idx += 1

        # Demo predictions for the dashboard. Without these, the dashboard
        # is empty on first load and reviewers don't see the on-time metric.
        self.stdout.write("Seeding demo predictions for the dashboard…")
        TIERS_AND_OUTCOMES = [
            # (tier, confidence, on_time_probability) — calibrated so the
            # in-stock tiers come in near 80% on-time (matching P80 promise).
            ("in_stock_nearest", "high", 0.85),
            ("in_stock_farther", "high", 0.80),
            ("in_stock_no_stats", "medium", 0.65),
            ("awaiting_batch", "medium", 0.70),
            ("awaiting_batch_no_slip", "low", 0.55),
            ("soft_fallback", "low", 0.50),
        ]
        sample_pincodes = ["110001", "400001", "560001", "600001", "700001"]
        for i in range(60):
            tier, conf, on_time_p = random.choice(TIERS_AND_OUTCOMES)
            product = random.choice(all_products)
            pincode = random.choice(sample_pincodes)
            predicted_offset = random.randint(3, 14)
            promised = today - timedelta(days=random.randint(2, 30))
            earliest = promised + timedelta(days=predicted_offset)
            latest = earliest + (timedelta(days=random.randint(2, 6))
                                 if "batch" in tier or tier == "soft_fallback"
                                 else timedelta(days=0))
            # Bias the actual delivery to land before the promised date with prob `on_time_p`
            on_time = random.random() < on_time_p
            if on_time:
                miss = random.randint(-3, 0)
            else:
                miss = random.randint(1, 5)
            actual = latest + timedelta(days=miss)
            Prediction.objects.create(
                product=product,
                pincode=pincode,
                earliest_date=earliest,
                latest_date=latest,
                confidence=conf,
                tier_used=tier,
                reason=f"Demo prediction for {tier}",
                actual_delivered_at=actual,
                miss_days=miss,
            )

        self.stdout.write(self.style.SUCCESS(
            f"Done. {order_idx} delivery records, "
            f"{len(products)} products, "
            f"{len(warehouses)} warehouses, "
            f"{len(clusters)} clusters, "
            f"60 demo predictions."
        ))
        self.stdout.write("Run `python manage.py refresh_lane_stats` next.")
