from bikadeli_common.gateway import (
    liveness,
    proxy_to_finance_service,
    proxy_to_identity_service,
    proxy_to_monolith,
    proxy_to_orders_service,
    readiness,
    service_metadata_view,
)
from django.urls import include, path, re_path

urlpatterns = [
    path('internal/service-metadata/', service_metadata_view, name='driver-service-metadata'),
    path('health/', liveness, name='health-liveness'),
    path('health/ready/', readiness, name='health-readiness'),
    path('', include('driver_profiles.urls')),
    re_path(
        r'^api/v1/(?:auth/(?:register|login|logout|logout-all|token/refresh)/?|profile(?:/.*)?)$',
        proxy_to_identity_service,
        name='identity-service-proxy',
    ),
    re_path(
        r'^api/v1/orders/parcels(?:/.*)?$',
        proxy_to_orders_service,
        name='orders-service-parcel-proxy',
    ),
    re_path(
        r'^api/v1/drivers/(?:parcel-earnings|earnings)(?:/.*)?$',
        proxy_to_finance_service,
        name='driver-earnings-finance-proxy',
    ),
    re_path(
        r'^api/v1/drivers/orders(?:/.*)?$',
        proxy_to_orders_service,
        name='driver-food-orders-service-proxy',
    ),
    re_path(r'^api/v1/drivers(?:/.*)?$', proxy_to_monolith, name='driver-api-proxy'),
]
