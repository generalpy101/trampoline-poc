from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("dashboard/", views.dashboard, name="dashboard"),

    # API used by Alpine.js
    path("api/estimate", views.api_estimate, name="api_estimate"),
    path("api/products", views.api_products, name="api_products"),
    path("api/inventory/<str:sku>", views.api_inventory, name="api_inventory"),
    path("api/inventory/<str:sku>/zero/<str:warehouse_code>",
         views.api_zero_inventory, name="api_zero_inventory"),
    path("api/inventory/<str:sku>/restore",
         views.api_restore_inventory, name="api_restore_inventory"),
]
