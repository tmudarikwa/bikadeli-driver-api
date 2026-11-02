from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from driver_profiles.models import DriverBehaviorScore

TOKEN = "d" * 40


@override_settings(DRIVER_FRAUD_REVIEW_SERVICE_ENABLED=True, DRIVER_ADMIN_TOKEN=TOKEN)
class BehaviorScoreTests(TestCase):
    def setUp(self):
        DriverBehaviorScore.objects.create(owner_user_id=1, overall_score=30, is_flagged=True)
        DriverBehaviorScore.objects.create(owner_user_id=2, overall_score=90)

    def test_requires_token(self):
        response = APIClient().get("/internal/v1/admin/drivers/behavior-scores/")
        self.assertEqual(response.status_code, 404)

    def test_lists_sorted_and_filters_flagged(self):
        client = APIClient()
        url = "/internal/v1/admin/drivers/behavior-scores/"
        body = client.get(url, HTTP_X_DRIVER_ADMIN_TOKEN=TOKEN).json()
        self.assertEqual([r["driver_id"] for r in body["results"]], [1, 2])
        self.assertEqual(body["results"][0]["risk_level"], "CRITICAL")
        flagged = client.get(url, {"flagged": "true"}, HTTP_X_DRIVER_ADMIN_TOKEN=TOKEN).json()
        self.assertEqual(flagged["count"], 1)
