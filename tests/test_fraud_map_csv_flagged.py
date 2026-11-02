from django.test import TestCase, override_settings
from driver_profiles.models import DriverBehaviorScore, DriverLocationAnomaly
from rest_framework.test import APIClient

TOKEN = "d" * 40
HEADERS = {"HTTP_X_DRIVER_ADMIN_TOKEN": TOKEN}


@override_settings(DRIVER_FRAUD_REVIEW_SERVICE_ENABLED=True, DRIVER_ADMIN_TOKEN=TOKEN)
class FraudMapCsvFlaggedTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        DriverLocationAnomaly.objects.create(
            owner_user_id=5,
            anomaly_type="SPEED_EXCEEDED",
            severity="HIGH",
            reported_latitude=-17.8,
            reported_longitude=31.0,
            active_order_id=9,
        )
        DriverLocationAnomaly.objects.create(
            owner_user_id=6, anomaly_type="MIN_INTERVAL", reported_latitude=None, reported_longitude=None
        )
        DriverBehaviorScore.objects.create(owner_user_id=5, overall_score=20, is_flagged=True)
        DriverBehaviorScore.objects.create(owner_user_id=6, overall_score=90, is_flagged=False)

    def test_requires_token(self):
        for path in ("flagged", "location-anomalies/map", "location-anomalies/export-csv"):
            self.assertEqual(self.client.get(f"/internal/v1/admin/drivers/{path}/").status_code, 404)

    def test_flagged_only_flagged(self):
        body = self.client.get("/internal/v1/admin/drivers/flagged/", **HEADERS).json()
        self.assertEqual([r["driver_id"] for r in body["results"]], [5])

    def test_map_requires_coordinates(self):
        body = self.client.get("/internal/v1/admin/drivers/location-anomalies/map/", **HEADERS).json()
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["lat"], -17.8)
        self.assertEqual(body["results"][0]["anomaly_display"], "Speed exceeded")

    def test_csv_export(self):
        response = self.client.get("/internal/v1/admin/drivers/location-anomalies/export-csv/", **HEADERS)
        self.assertEqual(response["Content-Type"], "text/csv")
        lines = response.content.decode().strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("ID,Driver ID"))


@override_settings(DRIVER_FRAUD_REVIEW_SERVICE_ENABLED=True, DRIVER_ADMIN_TOKEN=TOKEN)
class RiskBreakdownTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        DriverBehaviorScore.objects.create(owner_user_id=5, overall_score=40, is_flagged=True)

    def test_requires_token(self):
        self.assertEqual(self.client.get("/internal/v1/admin/drivers/5/risk-breakdown/").status_code, 404)

    def test_missing_score_404(self):
        response = self.client.get("/internal/v1/admin/drivers/99/risk-breakdown/", **HEADERS)
        self.assertEqual(response.status_code, 404)

    def test_components_returned(self):
        response = self.client.get("/internal/v1/admin/drivers/5/risk-breakdown/", **HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["components"]), 5)


@override_settings(DRIVER_FRAUD_REVIEW_SERVICE_ENABLED=True, DRIVER_ADMIN_TOKEN=TOKEN)
class AnomalyPointsTests(TestCase):
    def test_points_token_and_filtering(self):
        client = APIClient()
        DriverLocationAnomaly.objects.create(
            owner_user_id=5, anomaly_type="SPEED_EXCEEDED", reported_latitude=-17.8, reported_longitude=31.0
        )
        DriverLocationAnomaly.objects.create(owner_user_id=6, anomaly_type="MIN_INTERVAL")
        url = "/internal/v1/admin/drivers/location-anomalies/points/"
        self.assertEqual(client.get(url).status_code, 404)
        body = client.get(url, **HEADERS).json()
        self.assertEqual([p["driver_id"] for p in body["results"]], [5])
