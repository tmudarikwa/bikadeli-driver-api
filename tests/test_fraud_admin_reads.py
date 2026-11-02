from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from driver_profiles.models import DriverDevice, DriverRatingAnomaly

TOKEN = "d" * 40


@override_settings(DRIVER_FRAUD_REVIEW_SERVICE_ENABLED=True, DRIVER_ADMIN_TOKEN=TOKEN)
class FraudAdminReadTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        DriverRatingAnomaly.objects.create(
            owner_user_id=5, anomaly_type="SPIKE", rating_count=4, avg_rating_in_window=5
        )
        DriverDevice.objects.create(owner_user_id=5, device_id="a", device_fingerprint="f" * 64)
        DriverDevice.objects.create(owner_user_id=6, device_id="b", device_fingerprint="f" * 64)

    def test_requires_token(self):
        for path in ("rating-anomalies", "devices"):
            response = self.client.get(f"/internal/v1/admin/drivers/{path}/?driver_id=5")
            self.assertEqual(response.status_code, 404)

    def test_rating_anomalies(self):
        body = self.client.get(
            "/internal/v1/admin/drivers/rating-anomalies/", HTTP_X_DRIVER_ADMIN_TOKEN=TOKEN
        ).json()
        self.assertEqual(body["results"][0]["anomaly_type"], "SPIKE")

    def test_devices_report_sharing(self):
        url = "/internal/v1/admin/drivers/devices/"
        body = self.client.get(url, {"driver_id": 5}, HTTP_X_DRIVER_ADMIN_TOKEN=TOKEN).json()
        self.assertEqual(body["results"][0]["shared_driver_count"], 1)
        self.assertEqual(body["results"][0]["shared_with_drivers"][0]["user_id"], 6)
        bad = self.client.get(url, {"driver_id": "x"}, HTTP_X_DRIVER_ADMIN_TOKEN=TOKEN)
        self.assertEqual(bad.status_code, 400)
