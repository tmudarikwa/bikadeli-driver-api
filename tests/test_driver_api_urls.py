import sys
from pathlib import Path

from django.test import Client, SimpleTestCase
from django.urls import resolve

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))


class DriverApiUrlTests(SimpleTestCase):
    urlconf = 'driver_api.urls'

    def test_driver_profile_and_location_routes_resolve(self):
        self.assertIsNotNone(resolve('/api/v1/drivers/profile/', urlconf=self.urlconf))
        self.assertEqual(
            resolve('/api/v1/drivers/location/update/', urlconf=self.urlconf).url_name,
            'driver-location-update',
        )

    def test_health_routes_resolve(self):
        self.assertEqual(resolve('/health/', urlconf=self.urlconf).url_name, 'health-liveness')
        self.assertEqual(resolve('/health/ready/', urlconf=self.urlconf).url_name, 'health-readiness')
        self.assertEqual(Client().get('/health/').status_code, 200)
