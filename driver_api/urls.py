from django.urls import include, path

from core.health import liveness_view, readiness_view
from drivers import urls as driver_urls

urlpatterns = [
    path('health/', liveness_view, name='health-liveness'),
    path('health/ready/', readiness_view, name='health-readiness'),
    path('api/v1/drivers/', include(driver_urls.urlpatterns)),
]
