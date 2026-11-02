from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import DatabaseError
from django.test import TestCase, override_settings
from driver_profiles.models import (
    DriverProfile,
    DriverSupportTicket,
    DriverSupportTicketMessage,
)
from rest_framework.test import APIClient


class DriverSupportTicketApiTests(TestCase):
    list_url = "/api/v1/drivers/support-tickets/"

    def setUp(self):
        self.client = APIClient()
        self.authenticate(41)
        self.profile = DriverProfile.objects.create(
            owner_user_id=41,
            vehicle_type="MOTORCYCLE",
        )

    def authenticate(self, user_id, user_type="DRIVER"):
        self.client.force_authenticate(
            user=SimpleNamespace(id=user_id, is_authenticated=True),
            token={"user_type": user_type},
        )

    def create_ticket(self, owner_user_id=41, **overrides):
        values = {
            "owner_user_id": owner_user_id,
            "order_id": 702,
            "category": "LONG_WAIT",
            "subject": "Waiting at pickup",
            "description": "The restaurant is still preparing the order.",
        }
        values.update(overrides)
        return DriverSupportTicket.objects.create(**values)

    def test_create_persists_ticket_and_initial_driver_message(self):
        response_body = {
            "order_id": 702,
            "owned": True,
        }
        client = Mock()
        client.get.return_value = Mock(status_code=200, json=lambda: response_body)
        with (
            override_settings(ORDERS_SERVICE_URL="http://orders.test"),
            patch("driver_profiles.views.httpx.Client") as http_client,
        ):
            http_client.return_value.__enter__.return_value = client
            response = self.client.post(
                self.list_url,
                {
                    "category": "LONG_WAIT",
                    "subject": "Waiting at pickup",
                    "description": "The restaurant is still preparing the order.",
                    "order_id": 702,
                },
                format="json",
                HTTP_AUTHORIZATION="Bearer signed-driver-token",
            )

        self.assertEqual(response.status_code, 201)
        client.get.assert_called_once()
        ticket = DriverSupportTicket.objects.get()
        self.assertEqual(ticket.owner_user_id, 41)
        self.assertEqual(ticket.order_id, 702)
        self.assertEqual(ticket.messages.count(), 1)
        self.assertTrue(ticket.messages.get().sender_is_driver)
        self.assertEqual(response.data["message_count"], 1)
        self.assertEqual(
            response.data["last_message"]["message"],
            "The restaurant is still preparing the order.",
        )

    def test_create_rejects_order_not_assigned_to_driver(self):
        client = Mock()
        client.get.return_value = Mock(status_code=404)
        with (
            override_settings(ORDERS_SERVICE_URL="http://orders.test"),
            patch("driver_profiles.views.httpx.Client") as http_client,
        ):
            http_client.return_value.__enter__.return_value = client
            response = self.client.post(
                self.list_url,
                {
                    "category": "LONG_WAIT",
                    "subject": "Waiting at pickup",
                    "description": "The restaurant is still preparing the order.",
                    "order_id": 702,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(DriverSupportTicket.objects.exists())

    def test_message_write_failure_rolls_back_ticket_creation(self):
        with patch(
            "driver_profiles.views.DriverSupportTicketMessage.objects.create",
            side_effect=DatabaseError("write failed"),
        ):
            response = self.client.post(
                self.list_url,
                {
                    "category": "OTHER",
                    "subject": "Need help",
                    "description": "Support ticket message should be atomic.",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(DriverSupportTicket.objects.exists())

    def test_list_is_owner_scoped_and_preserves_legacy_response_shape(self):
        own = self.create_ticket()
        other = self.create_ticket(
            owner_user_id=52,
            subject="Other driver's issue",
        )
        DriverSupportTicketMessage.objects.create(
            ticket=own,
            sender_is_driver=True,
            message="Initial driver message",
        )
        DriverSupportTicketMessage.objects.create(
            ticket=own,
            sender_is_driver=False,
            message="Support response",
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        item = response.data[0]
        self.assertEqual(item["id"], own.pk)
        self.assertEqual(item["message_count"], 2)
        self.assertEqual(item["last_message"]["message"], "Support response")
        self.assertNotEqual(item["id"], other.pk)

    def test_status_filter_and_invalid_status(self):
        self.create_ticket()
        self.create_ticket(status="RESOLVED", subject="Resolved issue")

        filtered = self.client.get(self.list_url, {"status": "resolved"})
        invalid = self.client.get(self.list_url, {"status": "NOT_A_STATUS"})

        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(len(filtered.data), 1)
        self.assertEqual(filtered.data[0]["status"], "RESOLVED")
        self.assertEqual(invalid.status_code, 400)

    def test_messages_and_reply_are_scoped_and_reopen_closed_ticket(self):
        ticket = self.create_ticket(status="CLOSED")
        other_ticket = self.create_ticket(owner_user_id=52)
        message_url = f"{self.list_url}{ticket.pk}/messages/"

        messages = self.client.get(message_url)
        reply = self.client.post(
            f"{self.list_url}{ticket.pk}/reply/",
            {"message": "I have an update."},
            format="json",
        )
        hidden_messages = self.client.get(
            f"{self.list_url}{other_ticket.pk}/messages/"
        )
        hidden_reply = self.client.post(
            f"{self.list_url}{other_ticket.pk}/reply/",
            {"message": "This should not be saved."},
            format="json",
        )

        self.assertEqual(messages.status_code, 200)
        self.assertEqual(messages.data, [])
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(reply.data["message"], "I have an update.")
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "IN_PROGRESS")
        self.assertEqual(ticket.messages.count(), 1)
        self.assertEqual(hidden_messages.status_code, 404)
        self.assertEqual(hidden_reply.status_code, 404)
        self.assertEqual(other_ticket.messages.count(), 0)

    def test_non_driver_and_missing_profile_cannot_create_tickets(self):
        self.authenticate(41, user_type="CUSTOMER")
        forbidden = self.client.post(
            self.list_url,
            {
                "category": "OTHER",
                "subject": "Help",
                "description": "Need assistance.",
            },
            format="json",
        )

        self.authenticate(99)
        missing_profile = self.client.post(
            self.list_url,
            {
                "category": "OTHER",
                "subject": "Help",
                "description": "Need assistance.",
            },
            format="json",
        )

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(missing_profile.status_code, 404)
        self.assertFalse(DriverSupportTicket.objects.exists())
