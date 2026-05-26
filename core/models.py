"""
Data model for the delivery estimate POC.

Three groups of tables:

1. Catalog & operations (source of truth):
   Warehouse, Product, PincodeCluster, PincodeMapping, Supplier,
   Inventory, ProductionBatch.

2. History (the data we learn from):
   DeliveryRecord — every completed delivery, used to compute statistics.

3. Derived (refreshed by management commands):
   LanePerformance — pre-aggregated percentiles per (warehouse, cluster).
   SupplierReliability — pre-aggregated batch slip per supplier.

4. Observability:
   Prediction — every estimate served, for the accuracy dashboard.
"""

from django.db import models


class WeightClass(models.TextChoices):
    SMALL = "small", "Small"
    MEDIUM = "medium", "Medium"
    LARGE = "large", "Large"
    XL = "xl", "Extra Large"


class BatchStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    IN_TRANSIT = "in_transit", "In Transit"
    RECEIVED = "received", "Received"
    DELAYED = "delayed", "Delayed"


# ----- Catalog & operations -----

class Warehouse(models.Model):
    code = models.CharField(max_length=20, primary_key=True)
    name = models.CharField(max_length=100)
    pincode = models.CharField(max_length=10)
    region = models.CharField(max_length=40)

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class PincodeCluster(models.Model):
    """
    Pincodes are grouped into clusters so we don't track stats per pincode.
    A real system would have ~2,000 clusters for India's ~150k pincodes.
    """
    code = models.CharField(max_length=40, primary_key=True)
    name = models.CharField(max_length=100)
    region = models.CharField(max_length=40)

    def __str__(self) -> str:
        return f"{self.code} ({self.region})"


class PincodeMapping(models.Model):
    pincode = models.CharField(max_length=10, primary_key=True)
    cluster = models.ForeignKey(
        PincodeCluster, on_delete=models.CASCADE, related_name="pincodes"
    )

    def __str__(self) -> str:
        return f"{self.pincode} → {self.cluster_id}"


class Supplier(models.Model):
    code = models.CharField(max_length=20, primary_key=True)
    name = models.CharField(max_length=100)

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class Product(models.Model):
    sku = models.CharField(max_length=30, primary_key=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=40)
    weight_class = models.CharField(
        max_length=10, choices=WeightClass.choices, default=WeightClass.LARGE
    )
    price_inr = models.PositiveIntegerField(default=0)
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name="products", null=True
    )

    def __str__(self) -> str:
        return f"{self.sku} · {self.name}"


class Inventory(models.Model):
    """Current stock per (product, warehouse). Updated by warehouse system."""
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="inventory"
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.CASCADE, related_name="inventory"
    )
    quantity = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("product", "warehouse")]
        indexes = [models.Index(fields=["product", "quantity"])]

    def __str__(self) -> str:
        return f"{self.product_id} @ {self.warehouse_id}: {self.quantity}"


class ProductionBatch(models.Model):
    """A pending or in-flight production batch."""
    batch_id = models.CharField(max_length=30, primary_key=True)
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="batches"
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.CASCADE, related_name="incoming_batches"
    )
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    promised_date = models.DateField()
    status = models.CharField(
        max_length=20, choices=BatchStatus.choices, default=BatchStatus.SCHEDULED
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["product", "status", "promised_date"])]

    def __str__(self) -> str:
        return f"{self.batch_id} · {self.product_id} → {self.warehouse_id}"


# ----- History -----

class DeliveryRecord(models.Model):
    """One row per completed (or in-flight) delivery. The truth we learn from."""
    order_id = models.CharField(max_length=40, primary_key=True)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT)
    cluster = models.ForeignKey(PincodeCluster, on_delete=models.PROTECT)
    shipped_at = models.DateTimeField()
    delivered_at = models.DateTimeField(null=True, blank=True)
    promised_date = models.DateField(null=True, blank=True)
    transit_days = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["warehouse", "cluster"]),
            models.Index(fields=["shipped_at"]),
        ]


# ----- Derived statistics -----

class LanePerformance(models.Model):
    """
    Refreshed by `manage.py refresh_lane_stats`. Reads delivery history,
    bucket by (warehouse, cluster), compute percentiles.
    """
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    cluster = models.ForeignKey(PincodeCluster, on_delete=models.CASCADE)
    median_days = models.FloatField()
    p80_days = models.FloatField()
    p95_days = models.FloatField()
    sample_size = models.PositiveIntegerField()
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("warehouse", "cluster")]


class SupplierReliability(models.Model):
    """How much each supplier's batches typically slip vs. their promised date."""
    supplier = models.OneToOneField(
        Supplier, on_delete=models.CASCADE, related_name="reliability"
    )
    median_slip_days = models.FloatField()
    p80_slip_days = models.FloatField()
    sample_size = models.PositiveIntegerField()
    computed_at = models.DateTimeField(auto_now=True)


# ----- Observability -----

class Prediction(models.Model):
    """
    Shadow log: every estimate served, with the inputs that produced it.
    Joined to a DeliveryRecord later to measure accuracy.
    """
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    pincode = models.CharField(max_length=10)
    predicted_at = models.DateTimeField(auto_now_add=True)
    earliest_date = models.DateField()
    latest_date = models.DateField()
    confidence = models.CharField(max_length=10)
    tier_used = models.CharField(max_length=40)
    reason = models.CharField(max_length=200, blank=True)
    # Filled in when a real order from this prediction actually delivers
    actual_delivered_at = models.DateField(null=True, blank=True)
    miss_days = models.IntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["predicted_at"]),
            models.Index(fields=["tier_used"]),
        ]
