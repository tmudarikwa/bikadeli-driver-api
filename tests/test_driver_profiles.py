from datetime import timedelta
from unittest.mock import patch

import httpx
import jwt
from django.conf import settings
from django.db import DatabaseError, connection
from django.test import TestCase
from django.utils import timezone
from driver_profiles.models import DriverProfile
from rest_framework.test import APIClient


class DriverProfileApiTests(TestCase):
    def setUp(self):
        self.owner_user_id = 415
        self.client = APIClient()
        token = jwt.encode(
            {
                "token_type": "access",
                "user_id": self.owner_user_id,
                "iat": timezone.now(),
                "exp": timezone.now() + timedelta(minutes=5),
            },
            settings.JWT_SIGNING_KEY,
            algorithm="HS256",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_driver_can_create_and_read_only_their_own_profile(self):
        response = self.client.post(
            "/api/v1/drivers/profile/",
            {"vehicle_type": "BICYCLE", "vehicle_registration": "ZX-42"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["vehicle_type"], "BICYCLE")
        self.assertEqual(response.data["owner_user_id"], self.owner_user_id)
        self.assertEqual(response.data["verification_status"], "PENDING")
        self.assertFalse(response.data["is_available"])
        self.assertEqual(response.data["service_radius_km"], 10)
        self.assertNotIn("license_number", response.data)

        profile = DriverProfile.objects.get(owner_user_id=self.owner_user_id)
        self.assertEqual(profile.vehicle_registration, "ZX-42")
        self.assertEqual(
            self.client.get("/api/v1/drivers/profile/me/").status_code, 200
        )

        other = DriverProfile.objects.create(owner_user_id=9001)
        profile_response = self.client.get("/api/v1/drivers/profile/me/")
        self.assertEqual(profile_response.status_code, 200)
        self.assertEqual(profile_response.data["id"], profile.pk)
        self.assertEqual(profile_response.data["verification_status"], "PENDING")
        self.assertFalse(profile_response.data["is_available"])
        self.assertEqual(
            self.client.patch(
                "/api/v1/drivers/profile/",
                {"vehicle_registration": "NOT-MINE"},
                format="json",
            ).status_code,
            200,
        )
        other.refresh_from_db()
        self.assertEqual(other.vehicle_registration, "")

    def test_profile_updates_are_scoped_and_cannot_change_server_owned_fields(self):
        DriverProfile.objects.create(owner_user_id=self.owner_user_id)

        updated = self.client.put(
            "/api/v1/drivers/profile/update_profile/",
            {"vehicle_registration": "NEW-REG"},
            format="json",
        )
        forbidden = self.client.patch(
            "/api/v1/drivers/profile/",
            {"verification_status": "VERIFIED"},
            format="json",
        )

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data["vehicle_registration"], "NEW-REG")
        self.assertEqual(updated.data["service_radius_km"], 10)
        self.assertEqual(forbidden.status_code, 400)
        self.assertEqual(
            DriverProfile.objects.get(
                owner_user_id=self.owner_user_id
            ).verification_status,
            "PENDING",
        )

    def test_orders_compatible_profile_route_returns_not_found_without_profile(self):
        response = self.client.get("/api/v1/drivers/profile/me/")
        self.assertEqual(response.status_code, 404)

    def test_availability_requires_verified_profile(self):
        pending = DriverProfile.objects.create(owner_user_id=self.owner_user_id)
        rejected = self.client.post(
            "/api/v1/drivers/profile/toggle_online/",
            {"is_online": True},
            format="json",
        )
        self.assertEqual(rejected.status_code, 403)
        pending.refresh_from_db()
        self.assertFalse(pending.is_available)

        pending.verification_status = "VERIFIED"
        pending.save(update_fields=["verification_status"])
        accepted = self.client.post(
            "/api/v1/drivers/profile/toggle_online/",
            {"is_online": True},
            format="json",
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.data, {"is_online": True, "is_available": True})
        pending.refresh_from_db()
        self.assertTrue(pending.is_available)

    def test_profile_creation_rejects_external_identity_and_status_fields(self):
        response = self.client.post(
            "/api/v1/drivers/profile/",
            {
                "owner_user_id": 9001,
                "verification_status": "VERIFIED",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(DriverProfile.objects.exists())

    def test_profile_creation_is_idempotent_for_matching_onboarding_data(self):
        data = {
            "vehicle_type": "BICYCLE",
            "vehicle_registration": "ZX-42",
            "license_number": "DL-12345",
        }
        created = self.client.post("/api/v1/drivers/profile/", data, format="json")
        repeated = self.client.post("/api/v1/drivers/profile/", data, format="json")

        self.assertEqual(created.status_code, 201)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(created.data["id"], repeated.data["id"])
        profile = DriverProfile.objects.get(owner_user_id=self.owner_user_id)
        self.assertEqual(profile.license_number, "DL-12345")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT license_number FROM driver_profiles_driverprofile "
                "WHERE owner_user_id = %s",
                [self.owner_user_id],
            )
            stored_license = cursor.fetchone()[0]
        self.assertNotEqual(stored_license, "DL-12345")
        self.assertTrue(stored_license.startswith("gAAAAA"))

    def test_profile_creation_conflicts_when_retry_changes_onboarding_data(self):
        DriverProfile.objects.create(
            owner_user_id=self.owner_user_id,
            vehicle_type="BICYCLE",
            vehicle_registration="ZX-42",
        )

        response = self.client.post(
            "/api/v1/drivers/profile/",
            {"vehicle_type": "MOTORCYCLE", "vehicle_registration": "ZX-43"},
            format="json",
        )

        self.assertEqual(response.status_code, 409)

    def test_internal_demand_count_requires_secret_and_counts_only_verified_available_drivers(
        self,
    ):
        DriverProfile.objects.create(
            owner_user_id=51,
            verification_status="VERIFIED",
            is_available=True,
        )
        DriverProfile.objects.create(
            owner_user_id=52,
            verification_status="PENDING",
            is_available=True,
        )
        DriverProfile.objects.create(
            owner_user_id=53,
            verification_status="VERIFIED",
            is_available=False,
        )

        with self.settings(
            DEMAND_SYNC_TOKEN="d" * 40,
            IDENTITY_SERVICE_URL="http://identity-service",
        ), patch(
            "driver_profiles.views.httpx.Client.post",
            return_value=httpx.Response(
                200,
                json={"active_user_ids": [51]},
                request=httpx.Request("POST", "http://identity-service"),
            ),
        ) as identity_lookup:
            unauthorized = self.client.get("/internal/v1/drivers/demand/")
            authorized = self.client.get(
                "/internal/v1/drivers/demand/",
                HTTP_X_DEMAND_SYNC_TOKEN="d" * 40,
            )

        self.assertEqual(unauthorized.status_code, 404)
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.data, {"available_drivers": 1})
        self.assertEqual(identity_lookup.call_count, 1)
        self.assertEqual(
            identity_lookup.call_args.kwargs["json"],
            {"user_ids": [51]},
        )

    def test_internal_demand_count_fails_closed_when_identity_lookup_fails(self):
        DriverProfile.objects.create(
            owner_user_id=51,
            verification_status="VERIFIED",
            is_available=True,
        )

        with self.settings(
            DEMAND_SYNC_TOKEN="d" * 40,
            IDENTITY_SERVICE_URL="http://identity-service",
        ), patch(
            "driver_profiles.views.httpx.Client.post",
            side_effect=httpx.ConnectError("identity service is offline"),
        ):
            response = self.client.get(
                "/internal/v1/drivers/demand/",
                HTTP_X_DEMAND_SYNC_TOKEN="d" * 40,
            )

        self.assertEqual(response.status_code, 503)


class DriverRegistrationApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.payload = {
            "email": "driver@example.test",
            "first_name": "Driver",
            "last_name": "One",
            "phone_number": "+263771234567",
            "password": "StrongDriverPass123!",
            "confirm_password": "StrongDriverPass123!",
            "vehicle_type": "MOTORCYCLE",
            "license_number": "DL-12345",
            "vehicle_registration": "AB-123",
        }
        self.identity_result = {
            "user": {
                "id": 812,
                "email": "driver@example.test",
                "first_name": "Driver",
                "last_name": "One",
                "phone_number": "+263771234567",
                "user_type": "DRIVER",
            },
            "access": "driver-access-token",
            "refresh": "driver-refresh-token",
            "message": "Registration successful",
        }

    def identity_response(self, status_code=201, **kwargs):
        return httpx.Response(
            status_code,
            json=kwargs.get("json", self.identity_result),
            headers=kwargs.get("headers"),
            request=httpx.Request(
                "POST", "http://identity-service/api/v1/auth/register/"
            ),
        )

    def test_registration_creates_driver_profile_and_returns_compatible_tokens(self):
        identity_response = self.identity_response(
            headers={"set-cookie": "auth_active=1; Path=/; SameSite=Strict"}
        )
        with self.settings(IDENTITY_SERVICE_URL="http://identity-service"), patch(
            "driver_profiles.views.httpx.Client.post",
            return_value=identity_response,
        ) as identity_register:
            response = self.client.post(
                "/api/v1/drivers/auth/register/", self.payload, format="json"
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["access"], "driver-access-token")
        self.assertEqual(response.data["refresh"], "driver-refresh-token")
        self.assertEqual(response.data["user"]["user_type"], "DRIVER")
        self.assertEqual(response.data["profile"]["verification_status"], "PENDING")
        self.assertNotIn("license_number", response.data["profile"])
        self.assertEqual(
            response.data["onboarding_status"]["verification_status"], "PENDING"
        )
        self.assertEqual(response.cookies["auth_active"].value, "1")
        profile = DriverProfile.objects.get(owner_user_id=812)
        self.assertEqual(profile.vehicle_type, "MOTORCYCLE")
        self.assertEqual(profile.vehicle_registration, "AB-123")
        self.assertEqual(profile.license_number, "DL-12345")
        self.assertEqual(
            identity_register.call_args.kwargs["json"]["user_type"], "DRIVER"
        )
        self.assertNotIn("license_number", identity_register.call_args.kwargs["json"])

    def test_duplicate_identity_is_reported_without_creating_a_second_profile(self):
        duplicate = self.identity_response(
            status_code=400,
            json={
                "detail": (
                    "An account with this email or phone number already exists."
                )
            },
        )
        with self.settings(IDENTITY_SERVICE_URL="http://identity-service"), patch(
            "driver_profiles.views.httpx.Client.post",
            return_value=duplicate,
        ):
            response = self.client.post(
                "/api/v1/drivers/auth/register/", self.payload, format="json"
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("check your inbox", response.data["message"])
        self.assertFalse(DriverProfile.objects.exists())

    def test_identity_outage_fails_closed(self):
        with self.settings(IDENTITY_SERVICE_URL="http://identity-service"), patch(
            "driver_profiles.views.httpx.Client.post",
            side_effect=httpx.ConnectError("identity is offline"),
        ):
            response = self.client.post(
                "/api/v1/drivers/auth/register/", self.payload, format="json"
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["detail"], "Identity service is unavailable.")
        self.assertFalse(DriverProfile.objects.exists())

    def test_profile_database_failure_is_explicit_after_identity_succeeds(self):
        with (
            self.settings(IDENTITY_SERVICE_URL="http://identity-service"),
            patch(
                "driver_profiles.views.httpx.Client.post",
                return_value=self.identity_response(),
            ),
            patch(
                "driver_profiles.views.DriverProfile.objects.get_or_create",
                side_effect=DatabaseError("database unavailable"),
            ),
        ):
            response = self.client.post(
                "/api/v1/drivers/auth/register/", self.payload, format="json"
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("Log in and retry profile setup", response.data["detail"])
        self.assertFalse(DriverProfile.objects.exists())

    def test_registration_validates_password_confirmation_and_identity_minimum(self):
        self.payload["password"] = "short"
        self.payload["confirm_password"] = "short"
        with patch("driver_profiles.views.httpx.Client.post") as identity_register:
            response = self.client.post(
                "/api/v1/drivers/auth/register/", self.payload, format="json"
            )

        self.assertEqual(response.status_code, 400)
        identity_register.assert_not_called()
