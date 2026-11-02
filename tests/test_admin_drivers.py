from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from driver_profiles.admin_audit_models import DriverAdminAuditLog
from driver_profiles.models import DriverLocation, DriverLocationHistory, DriverProfile

TOKEN = "d" * 40
DEMAND_TOKEN = "x" * 40
H = {"HTTP_X_DRIVER_ADMIN_TOKEN": TOKEN}
ACTOR = {"actor_id": 7, "actor_email": "a@example.com"}
BASE = "/internal/v1/admin/drivers/"


@override_settings(DRIVER_ADMIN_OPS_ENABLED=True, DRIVER_ADMIN_TOKEN=TOKEN)
class AdminDriverOpsTests(TestCase):
    def setUp(self):
        self.driver = DriverProfile.objects.create(owner_user_id=5)

    def post(self, path, body):
        return self.client.post(f"{BASE}{self.driver.id}/{path}/", body, content_type="application/json", **H)

    def test_gate_and_token(self):
        self.assertEqual(self.client.get(f"{BASE}ops/").status_code, 404)
        with override_settings(DRIVER_ADMIN_OPS_ENABLED=False):
            self.assertEqual(self.client.get(f"{BASE}ops/", **H).status_code, 404)

    def test_list_filters(self):
        DriverProfile.objects.create(owner_user_id=6, is_available=True, verification_status="VERIFIED")
        body = self.client.get(f"{BASE}ops/", {"is_available": "true"}, **H).json()
        self.assertEqual((body["total"], body["results"][0]["user_id"]), (1, 6))
        self.assertEqual(self.client.get(f"{BASE}ops/", {"page": "0"}, **H).status_code, 400)

    def test_toggle_available_audited(self):
        response = self.post("toggle-available", ACTOR)
        self.assertEqual(response.json(), {"status": "ok", "is_available": True})
        self.assertEqual(DriverAdminAuditLog.objects.get().action, "TOGGLE_AVAILABLE")
        self.assertEqual(self.post("toggle-available", {}).status_code, 400)

    def test_delivery_settings_validation(self):
        self.assertEqual(self.post("delivery-settings", {**ACTOR, "service_radius_km": 15}).json()["service_radius_km"], 15)
        for bad in (0, -1, "x", True, 5000):
            self.assertEqual(self.post("delivery-settings", {**ACTOR, "service_radius_km": bad}).status_code, 400)

    def test_verify(self):
        self.assertEqual(self.post("verify", {**ACTOR, "verification_status": "VERIFIED"}).status_code, 200)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.verification_status, "VERIFIED")
        self.assertEqual(self.post("verify", {**ACTOR, "verification_status": "NOPE"}).status_code, 400)
        missing = self.client.post(f"{BASE}999/verify/", {**ACTOR, "verification_status": "VERIFIED"}, content_type="application/json", **H)
        self.assertEqual(missing.status_code, 404)

    def test_location_privacy_hides_old_location(self):
        DriverLocation.objects.create(owner_user_id=self.driver.owner_user_id, latitude=-17.8, longitude=31.0)
        url = f"{BASE}{self.driver.pk}/location-privacy/"
        response = self.client.get(url, **H)
        self.assertTrue(response.json()["location_visible"])
        self.assertEqual(response.json()["location"]["lat"], -17.8)
        DriverLocation.objects.filter(owner_user_id=self.driver.owner_user_id).update(
            updated_at=timezone.now() - timedelta(hours=3)
        )
        response = self.client.get(url, **H)
        self.assertFalse(response.json()["location_visible"])
        self.assertIsNone(response.json()["location"])
        self.assertEqual(self.client.get(f"{BASE}999/location-privacy/", **H).status_code, 404)

    def test_location_history_purge_is_bounded_and_audited(self):
        old = timezone.now() - timedelta(days=31)
        recent = timezone.now() - timedelta(days=2)
        DriverLocationHistory.objects.create(
            owner_user_id=self.driver.owner_user_id, latitude=-17.8, longitude=31.0, recorded_at=old
        )
        DriverLocationHistory.objects.create(
            owner_user_id=self.driver.owner_user_id, latitude=-17.9, longitude=31.1, recorded_at=recent
        )
        url = f"{BASE}{self.driver.pk}/purge-location-history/"
        for invalid in (0, -1, 366, "x", True):
            self.assertEqual(
                self.client.post(url, {**ACTOR, "retention_days": invalid}, content_type="application/json", **H).status_code,
                400,
            )
        response = self.client.post(url, ACTOR, content_type="application/json", **H)
        self.assertEqual(response.json()["deleted_location_logs"], 1)
        self.assertEqual(DriverLocationHistory.objects.count(), 1)
        audit = DriverAdminAuditLog.objects.get(action="PURGE_LOCATION_HISTORY")
        self.assertEqual(audit.details["deleted_location_logs"], 1)
        self.assertEqual(self.client.post(url, {}, content_type="application/json", **H).status_code, 400)

    @override_settings(
        IDENTITY_SERVICE_URL="https://identity.example.test",
        DEMAND_SYNC_TOKEN=DEMAND_TOKEN,
    )
    @patch("driver_profiles.admin_drivers.httpx.post")
    def test_driver_eligibility_requires_active_verified_available_identity(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"active_user_ids": [self.driver.owner_user_id]}
        post.return_value = response
        url = f"{BASE}{self.driver.pk}/eligibility/"
        self.assertEqual(self.client.get(url, **H).status_code, 409)
        self.driver.is_available = True
        self.driver.verification_status = "VERIFIED"
        self.driver.save(update_fields=["is_available", "verification_status"])
        body = self.client.get(url, **H)
        self.assertEqual(body.status_code, 200)
        self.assertEqual(body.json()["owner_user_id"], self.driver.owner_user_id)
        post.assert_called_once()

        response.json.return_value = {"active_user_ids": []}
        self.assertEqual(self.client.get(url, **H).status_code, 409)
        response.json.return_value = {"active_user_ids": ["bad"]}
        self.assertEqual(self.client.get(url, **H).status_code, 502)

    @override_settings(
        IDENTITY_SERVICE_URL="https://identity.example.test",
        DEMAND_SYNC_TOKEN=DEMAND_TOKEN,
    )
    @patch("driver_profiles.admin_drivers.httpx.Client")
    def test_eligible_driver_list_requires_active_location(self, http_client):
        self.driver.is_available = True
        self.driver.verification_status = "VERIFIED"
        self.driver.save(update_fields=["is_available", "verification_status"])
        DriverLocation.objects.create(owner_user_id=self.driver.owner_user_id, latitude=-17.8, longitude=31.0)
        inactive = DriverProfile.objects.create(
            owner_user_id=6, is_available=True, verification_status="VERIFIED"
        )
        DriverLocation.objects.create(owner_user_id=inactive.owner_user_id, latitude=-17.9, longitude=31.1)
        response = Mock(status_code=200)
        response.json.return_value = {"active_user_ids": [self.driver.owner_user_id]}
        http_client.return_value.__enter__.return_value.post.return_value = response
        url = f"{BASE}eligible/"
        result = self.client.get(url, {"limit": 10}, **H)
        self.assertEqual(result.status_code, 200)
        self.assertEqual([row["driver_id"] for row in result.json()["results"]], [self.driver.pk])
        request = http_client.return_value.__enter__.return_value.post
        self.assertEqual(
            request.call_args.kwargs["headers"],
            {"X-Demand-Sync-Token": DEMAND_TOKEN},
        )
        self.assertEqual(self.client.get(url, {"limit": "201"}, **H).status_code, 400)
        response.json.return_value = {"active_user_ids": ["bad"]}
        self.assertEqual(self.client.get(url, **H).status_code, 502)
