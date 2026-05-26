from django.contrib import admin

from .models import (
    DeliveryRecord,
    Inventory,
    LanePerformance,
    PincodeCluster,
    PincodeMapping,
    Prediction,
    Product,
    ProductionBatch,
    Supplier,
    SupplierReliability,
    Warehouse,
)


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "region")


@admin.register(PincodeCluster)
class PincodeClusterAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "region")


@admin.register(PincodeMapping)
class PincodeMappingAdmin(admin.ModelAdmin):
    list_display = ("pincode", "cluster")
    search_fields = ("pincode",)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("code", "name")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("sku", "name", "category", "weight_class", "supplier")
    list_filter = ("category", "weight_class")
    search_fields = ("sku", "name")


@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ("product", "warehouse", "quantity", "updated_at")
    list_filter = ("warehouse",)


@admin.register(ProductionBatch)
class ProductionBatchAdmin(admin.ModelAdmin):
    list_display = (
        "batch_id", "product", "warehouse", "supplier",
        "quantity", "promised_date", "status",
    )
    list_filter = ("status", "warehouse")


@admin.register(DeliveryRecord)
class DeliveryRecordAdmin(admin.ModelAdmin):
    list_display = (
        "order_id", "product", "warehouse", "cluster",
        "shipped_at", "delivered_at", "transit_days",
    )
    list_filter = ("warehouse", "cluster")


@admin.register(LanePerformance)
class LanePerformanceAdmin(admin.ModelAdmin):
    list_display = (
        "warehouse", "cluster", "median_days", "p80_days",
        "p95_days", "sample_size", "computed_at",
    )


@admin.register(SupplierReliability)
class SupplierReliabilityAdmin(admin.ModelAdmin):
    list_display = (
        "supplier", "median_slip_days", "p80_slip_days",
        "sample_size", "computed_at",
    )


@admin.register(Prediction)
class PredictionAdmin(admin.ModelAdmin):
    list_display = (
        "predicted_at", "product", "pincode", "earliest_date",
        "latest_date", "confidence", "tier_used", "actual_delivered_at",
        "miss_days",
    )
    list_filter = ("tier_used", "confidence")
