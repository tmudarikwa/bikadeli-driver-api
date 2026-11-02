from datetime import timedelta
from unittest.mock import patch

import httpx
import jwt
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from driver_profiles.models import (
    DriverLocation,
    DriverLocationAnomaly,
    DriverLocationHistory,
    DriverProfile,
)


@override_settings(ORDERS_SERVICE_URL="http://orders.test")
class DriverLocationApiTests(TestCase):
    def setUp(self):
        self.owner_user_id = 415
        self.client = APIClient()
        self.assignment_client_patcher = patch("driver_profiles.views.httpx.Client")
        self.assignment_http_client = self.assignment_client_patcher.start()
        self.addCleanup(self.assignment_client_patcher.stop)
        self.assignment_http_client.return_value.__enter__.return_value.get.return_value = (
            httpx.Response(200, json={"assignment": None})
        )
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

    @patch(
        "driver_profiles.views.publish_driver_location_events",
        return_value=True,
    )
    def test_authenticated_update_persists_latest_location_without_exposing_coordinates(
        self, publish_events
    ):
        response = self.client.post(
            "/api/v1/drivers/location/update/",
            {"latitude": -17.825, "longitude": 31.033},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"success": True, "buffered": True})
        location = DriverLocation.objects.get(owner_user_id=self.owner_user_id)
        self.assertEqual(location.latitude, -17.825)
        self.assertEqual(location.longitude, 31.033)
        history = DriverLocationHistory.objects.get(owner_user_id=self.owner_user_id)
        self.assertEqual(history.latitude, -17.825)
        self.assertEqual(history.longitude, 31.033)
        self.assertIsNone(history.active_order_id)
        self.assertNotIn("latitude", response.data)
        self.assertNotIn("longitude", response.data)
        publish_events.assert_called_once_with(
            self.owner_user_id,
            None,
            -17.825,
            31.033,
        )

        updated = self.client.post(
            "/api/v1/drivers/location/update/",
            {"latitude": -17.825, "longitude": 31.033},
            format="json",
        )

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(DriverLocation.objects.count(), 1)
        location.refresh_from_db()
        self.assertEqual(location.latitude, -17.825)
        self.assertEqual(location.longitude, 31.033)
        self.assertEqual(
            DriverLocationHistory.objects.filter(
                owner_user_id=self.owner_user_id
            ).count(),
            2,
        )
        self.assertEqual(publish_events.call_count, 2)

    @patch("driver_profiles.views.publish_driver_location_events", return_value=True)
    def test_location_uses_orders_service_active_assignment(
        self, publish_events
    ):
        DriverProfile.objects.create(
            owner_user_id=self.owner_user_id,
            current_order_id=999,
        )
        client = self.assignment_http_client.return_value.__enter__.return_value
        client.get.return_value = httpx.Response(
            200,
            json={
                "assignment": {
                    "work_type": "FOOD_ORDER",
                    "work_id": 123,
                }
            },
        )

        response = self.client.post(
            "/api/v1/drivers/location/update/",
            {"latitude": -17.825, "longitude": 31.033},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        history = DriverLocationHistory.objects.get(owner_user_id=self.owner_user_id)
        self.assertEqual(history.active_order_id, 123)
        publish_events.assert_called_once_with(
            self.owner_user_id,
            123,
            -17.825,
            31.033,
        )
        client.get.assert_called_once()
        self.assertEqual(
            client.get.call_args.args[0],
            "http://orders.test/internal/v1/drivers/active-assignment/",
        )

    @patch("driver_profiles.views.publish_driver_location_events")
    def test_location_fails_closed_when_assignment_cannot_be_verified(
        self, publish_events
    ):
        client = self.assignment_http_client.return_value.__enter__.return_value
        client.get.side_effect = httpx.ConnectError("unavailable")

        response = self.client.post(
            "/api/v1/drivers/location/update/",
            {"latitude": -17.825, "longitude": 31.033},
            format="json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(DriverLocationHistory.objects.count(), 0)
        self.assertEqual(DriverLocation.objects.count(), 0)
        publish_events.assert_not_called()

    @patch("driver_profiles.views.publish_driver_location_events", return_value=True)
    def test_location_update_records_legacy_anomalies_in_service_storage(
        self, _publish
    ):
        for offset in range(4):
            DriverLocationHistory.objects.create(
                owner_user_id=self.owner_user_id,
                latitude=-17.825,
                longitude=31.033,
                recorded_at=timezone.now() - timedelta(seconds=30 + offset),
            )

        response = self.client.post(
            "/api/v1/drivers/location/update/",
            {"latitude": -17.825, "longitude": 31.033},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("STATIC_LOCATION", response.data["anomaly_types"])
        self.assertEqual(response.data["anomalies_detected"], 1)
        anomaly = DriverLocationAnomaly.objects.get()
        self.assertEqual(anomaly.anomaly_type, "STATIC_LOCATION")
        self.assertEqual(anomaly.severity, "MEDIUM")
        self.assertEqual(anomaly.details["same_location_count"], 5)

    @patch("driver_profiles.views.publish_driver_location_events", return_value=True)
    def test_critical_speed_anomaly_is_persisted_and_location_rejected(
        self, _publish
    ):
        DriverLocationHistory.objects.create(
            owner_user_id=self.owner_user_id,
            latitude=-17.825,
            longitude=31.033,
            recorded_at=timezone.now() - timedelta(minutes=1),
        )

        response = self.client.post(
            "/api/v1/drivers/location/update/",
            {"latitude": -17.7, "longitude": 31.033},
            format="json",
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.data,
            {
                "error": "Location rejected: impossible travel detected",
                "success": False,
            },
        )
        self.assertTrue(
            DriverLocationAnomaly.objects.filter(
                anomaly_type="SPEED_EXCEEDED",
                severity="CRITICAL",
            ).exists()
        )
        self.assertEqual(DriverLocationHistory.objects.count(), 2)

    @patch("driver_profiles.views.publish_driver_location_events", return_value=True)
    def test_outside_service_boundary_is_recorded_without_rejecting_ping(
        self, _publish
    ):
        response = self.client.post(
            "/api/v1/drivers/location/update/",
            {"latitude": 37.4219, "longitude": -122.084},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["anomaly_types"], ["GEOBOUNDARY_EXIT"])
        anomaly = DriverLocationAnomaly.objects.get()
        self.assertEqual(anomaly.severity, "HIGH")
        self.assertEqual(anomaly.details["bounds"]["lat_min"], -18.05)

    @patch("driver_profiles.views.publish_driver_location_events", return_value=True)
    def test_impossible_travel_is_recorded_and_rejected(
        self, _publish
    ):
        DriverLocationHistory.objects.create(
            owner_user_id=self.owner_user_id,
            latitude=-18.05,
            longitude=30.8,
            recorded_at=timezone.now() - timedelta(hours=1),
        )

        response = self.client.post(
            "/api/v1/drivers/location/update/",
            {"latitude": -17.65, "longitude": 31.25},
            format="json",
        )

        self.assertEqual(response.status_code, 422)
        anomaly = DriverLocationAnomaly.objects.get(
            anomaly_type="IMPOSSIBLE_TRAVEL"
        )
        self.assertEqual(anomaly.severity, "CRITICAL")
        self.assertGreater(anomaly.distance_km, 50)

    @patch("driver_profiles.views.publish_driver_location_events", return_value=True)
    def test_too_frequent_update_is_recorded_but_remains_allowed(
        self, _publish
    ):
        DriverLocationHistory.objects.create(
            owner_user_id=self.owner_user_id,
            latitude=-17.825,
            longitude=31.033,
            recorded_at=timezone.now() - timedelta(seconds=1),
        )

        response = self.client.post(
            "/api/v1/drivers/location/update/",
            {"latitude": -17.825, "longitude": 31.033},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["anomaly_types"], ["MIN_INTERVAL"])
        anomaly = DriverLocationAnomaly.objects.get()
        self.assertEqual(anomaly.severity, "LOW")
        self.assertLess(anomaly.details["elapsed_seconds"], 5)

    def test_invalid_coordinates_are_rejected_without_persisting_location(self):
        out_of_range = self.client.post(
            "/api/v1/drivers/location/update/",
            {"latitude": 90.0001, "longitude": 0},
            format="json",
        )

        self.assertEqual(out_of_range.status_code, 400)
        self.assertEqual(out_of_range.data, {"error": "Invalid coordinates range"})

        invalid_payloads = [
            {"latitude": 0, "longitude": -180.0001},
            {"latitude": "not-a-number", "longitude": 0},
            {"latitude": "NaN", "longitude": 0},
            {"longitude": 0},
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/api/v1/drivers/location/update/",
                    payload,
                    format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(DriverLocation.objects.exists())

        self.assertEqual(
            response.data,
            {"latitude": ["This field is required."]},
        )

    def test_location_update_requires_authentication(self):
        unauthenticated = APIClient().post(
            "/api/v1/drivers/location/update/",
            {"latitude": 37.42, "longitude": -122.08},
            format="json",
        )

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertFalse(DriverLocation.objects.exists())

    @patch("driver_profiles.location_events.get_channel_layer")
    def test_location_update_publishes_admin_and_customer_tracking_events(
        self, get_channel_layer
    ):
        from asgiref.sync import async_to_sync
        from channels.layers import InMemoryChannelLayer

        channel_layer = InMemoryChannelLayer()
        get_channel_layer.return_value = channel_layer
        driver_channel = async_to_sync(channel_layer.new_channel)()
        order_channel = async_to_sync(channel_layer.new_channel)()
        async_to_sync(channel_layer.group_add)(
            f"driver_{self.owner_user_id}", driver_channel
        )
        async_to_sync(channel_layer.group_add)("order_721", order_channel)

        DriverProfile.objects.create(
            owner_user_id=self.owner_user_id,
            current_order_id=721,
        )
        self.assignment_http_client.return_value.__enter__.return_value.get.return_value = (
            httpx.Response(
                200,
                json={
                    "assignment": {
                        "work_type": "FOOD_ORDER",
                        "work_id": 721,
                    }
                },
            )
        )
        response = self.client.post(
            "/api/v1/drivers/location/update/",
            {"latitude": -17.825, "longitude": 31.033},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        admin_event = async_to_sync(channel_layer.receive)(driver_channel)
        order_event = async_to_sync(channel_layer.receive)(order_channel)
        self.assertEqual(
            admin_event,
            {
                "type": "driver_location",
                "latitude": -17.825,
                "longitude": 31.033,
            },
        )
        self.assertEqual(
            order_event,
            {
                "type": "driver_location_update",
                "lat": -17.825,
                "lng": 31.033,
            },
        )

    @patch("driver_profiles.views.publish_driver_location_events", return_value=False)
    def test_location_update_reports_unavailable_event_broker(self, _publish):
        response = self.client.post(
            "/api/v1/drivers/location/update/",
            {"latitude": -17.825, "longitude": 31.033},
            format="json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.data,
            {"error": "Location event could not be published", "success": False},
        )
        self.assertEqual(
            DriverLocationHistory.objects.filter(
                owner_user_id=self.owner_user_id
            ).count(),
            1,
        )

    def test_location_history_is_authenticated_and_owner_scoped(self):
        DriverProfile.objects.create(
            owner_user_id=self.owner_user_id,
            current_order_id=721,
        )
        DriverLocationHistory.objects.create(
            owner_user_id=self.owner_user_id,
            active_order_id=721,
            latitude=-17.825,
            longitude=31.033,
        )
        DriverLocationHistory.objects.create(
            owner_user_id=self.owner_user_id + 1,
            active_order_id=722,
            latitude=0,
            longitude=0,
        )

        response = self.client.get("/api/v1/drivers/location/history/")
        anonymous = APIClient().get("/api/v1/drivers/location/history/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["active_order_id"], 721)
        self.assertEqual(response.data["results"][0]["latitude"], -17.825)
        self.assertEqual(anonymous.status_code, 401)
