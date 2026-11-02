from unittest.mock import Mock, patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from driver_profiles.models import (
    DriverLocationAnomaly,
    DriverProfile,
    DriverSupportTicket,
    DriverSupportTicketAttachment,
    DriverSupportTicketAudit,
    DriverSupportTicketInternalNote,
    DriverSupportTicketMessage,
)


@override_settings(
    DRIVER_SUPPORT_SERVICE_ENABLED=True,
    DRIVER_ADMIN_TOKEN="s" * 40,
    IDENTITY_SERVICE_URL="https://identity.example.test",
    IDENTITY_USER_LOOKUP_TOKEN="p" * 40,
)
class InternalDriverSupportTicketTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.ticket = DriverSupportTicket.objects.create(
            owner_user_id=41,
            category="SAFETY_CONCERN",
            subject="Unsafe pickup",
            description="The pickup location is unsafe.",
            status="CLOSED",
            resolved_at="2026-10-29T10:00:00Z",
        )
        DriverSupportTicketMessage.objects.create(
            ticket=self.ticket,
            sender_is_driver=True,
            message="The pickup location is unsafe.",
        )
        self.headers = {
            "HTTP_X_DRIVER_ADMIN_TOKEN": "s" * 40,
            "HTTP_X_DRIVER_ADMIN_ACTOR_ID": "7",
            "HTTP_X_DRIVER_ADMIN_ACTOR_EMAIL": "agent@example.test",
        }

    def test_staff_list_and_detail_are_internal_token_protected(self):
        denied = self.client.get(
            "/internal/v1/admin/drivers/support-tickets/"
        )
        listed = self.client.get(
            "/internal/v1/admin/drivers/support-tickets/",
            {"status": "OPEN"},
            **self.headers,
        )
        detail = self.client.get(
            f"/internal/v1/admin/drivers/support-tickets/{self.ticket.pk}/",
            **self.headers,
        )

        self.assertEqual(denied.status_code, 404)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["results"], [])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["ticket_type"], "driver")
        self.assertEqual(detail.data["messages"][0]["sender"], "driver")
        self.assertEqual(detail.data["status"], "CLOSED")

    def test_staff_reply_reopens_ticket_and_records_audit(self):
        response = self.client.post(
            f"/internal/v1/admin/drivers/support-tickets/{self.ticket.pk}/reply/",
            {"message": "We have escalated this safety concern."},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["sender"], "support")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "IN_PROGRESS")
        self.assertIsNone(self.ticket.resolved_at)
        self.assertEqual(self.ticket.messages.count(), 2)
        self.assertEqual(
            DriverSupportTicketAudit.objects.get().action,
            "REPLY",
        )

    def test_staff_reply_rejects_external_delivery_channel(self):
        response = self.client.post(
            f"/internal/v1/admin/drivers/support-tickets/{self.ticket.pk}/reply/",
            {"message": "Send an email", "channel": "EMAIL"},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.ticket.messages.count(), 1)

    def test_assignment_note_escalation_and_status_are_persisted(self):
        categorized = self.client.patch(
            f"/internal/v1/admin/drivers/support-tickets/{self.ticket.pk}/",
            {"category": "PAYMENT_ISSUE"},
            format="json",
            **self.headers,
        )
        assigned = self.client.post(
            f"/internal/v1/admin/drivers/support-tickets/{self.ticket.pk}/assign/",
            {"assigned_to": "agent@example.test"},
            format="json",
            **self.headers,
        )
        noted = self.client.post(
            f"/internal/v1/admin/drivers/support-tickets/{self.ticket.pk}/internal-notes/",
            {"message": "Contact the driver before closing."},
            format="json",
            **self.headers,
        )
        escalated = self.client.post(
            f"/internal/v1/admin/drivers/support-tickets/{self.ticket.pk}/escalate/",
            {"team": "SAFETY", "reason": "Potential physical danger"},
            format="json",
            **self.headers,
        )
        updated = self.client.patch(
            f"/internal/v1/admin/drivers/support-tickets/{self.ticket.pk}/",
            {"status": "RESOLVED", "resolution_notes": "Safety team contacted."},
            format="json",
            **self.headers,
        )

        self.assertEqual(categorized.status_code, 200)
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(noted.status_code, 200)
        self.assertEqual(escalated.status_code, 200)
        self.assertEqual(updated.status_code, 200)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.assigned_to_email, "agent@example.test")
        self.assertEqual(self.ticket.category, "PAYMENT_ISSUE")
        self.assertTrue(self.ticket.escalated)
        self.assertEqual(self.ticket.resolution_notes, "Safety team contacted.")
        self.assertEqual(
            DriverSupportTicketInternalNote.objects.get().message,
            "Contact the driver before closing.",
        )
        self.assertTrue(
            DriverSupportTicketAudit.objects.filter(action="CATEGORY").exists()
        )
        detail = self.client.get(
            f"/internal/v1/admin/drivers/support-tickets/{self.ticket.pk}/",
            **self.headers,
        )
        self.assertEqual(len(detail.data["internal_notes"]), 1)
        self.assertEqual(detail.data["status_history"][0]["status"], "RESOLVED")

    def test_bulk_assignment_updates_all_tickets_and_records_audit(self):
        second = DriverSupportTicket.objects.create(
            owner_user_id=42,
            category="PAYMENT_ISSUE",
            subject="Missing payment",
            description="A payout is missing.",
        )

        response = self.client.post(
            "/internal/v1/admin/drivers/support-tickets/bulk/",
            {
                "ids": [self.ticket.pk, second.pk],
                "action": "assign",
                "assigned_to": "agent@example.test",
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"action": "assign", "updated": 2})
        for ticket in (self.ticket, second):
            ticket.refresh_from_db()
            self.assertEqual(ticket.assigned_to_email, "agent@example.test")
            self.assertEqual(ticket.assigned_to_user_id, 7)
        self.assertEqual(
            DriverSupportTicketAudit.objects.filter(action="ASSIGN").count(), 2
        )

    def test_bulk_export_is_audited_and_rejects_missing_ticket_atomically(self):
        exported = self.client.post(
            "/internal/v1/admin/drivers/support-tickets/bulk/",
            {"ids": [self.ticket.pk], "action": "export"},
            format="json",
            **self.headers,
        )
        missing = self.client.post(
            "/internal/v1/admin/drivers/support-tickets/bulk/",
            {"ids": [self.ticket.pk, 99999], "action": "assign", "assigned_to": ""},
            format="json",
            **self.headers,
        )

        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.data, {"action": "export", "updated": 1})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            DriverSupportTicketAudit.objects.filter(action="EXPORT").count(), 1
        )
        self.assertFalse(DriverSupportTicketAudit.objects.filter(action="ASSIGN").exists())

    @patch("driver_profiles.views.httpx.Client")
    def test_staff_create_resolves_driver_identity_and_records_audit(self, client_factory):
        DriverProfile.objects.create(owner_user_id=88)
        upstream = Mock(status_code=200)
        upstream.json.return_value = {
            "user_id": 88,
            "email": "driver@example.test",
            "is_active_driver": True,
        }
        client = Mock()
        client.post.return_value = upstream
        client_factory.return_value.__enter__.return_value = client

        response = self.client.post(
            "/internal/v1/admin/drivers/support-tickets/create/",
            {
                "driver_email": "driver@example.test",
                "subject": "Unable to go online",
                "description": "The driver app is unable to change availability.",
                "category": "GPS_APP_ISSUE",
                "priority": "HIGH",
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201)
        ticket = DriverSupportTicket.objects.get(owner_user_id=88)
        self.assertEqual(ticket.owner_user_id, 88)
        self.assertIsNone(ticket.order_id)
        self.assertEqual(ticket.assigned_to_user_id, 7)
        self.assertEqual(ticket.assigned_to_email, "agent@example.test")
        self.assertEqual(DriverSupportTicketAudit.objects.get().action, "CREATE")
        self.assertEqual(
            client.post.call_args.args,
            (
                "https://identity.example.test/internal/v1/identity/drivers/validate/",
            ),
        )
        self.assertEqual(
            client.post.call_args.kwargs["json"],
            {"email": "driver@example.test"},
        )

    @patch("driver_profiles.views.httpx.Client")
    def test_staff_create_rejects_identity_response_without_driver_profile(
        self, client_factory
    ):
        upstream = Mock(status_code=200)
        upstream.json.return_value = {
            "user_id": 88,
            "email": "driver@example.test",
            "is_active_driver": True,
        }
        client = Mock()
        client.post.return_value = upstream
        client_factory.return_value.__enter__.return_value = client

        response = self.client.post(
            "/internal/v1/admin/drivers/support-tickets/create/",
            {
                "driver_email": "driver@example.test",
                "subject": "Unable to go online",
                "description": "The driver app is unable to change availability.",
                "category": "GPS_APP_ISSUE",
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(DriverSupportTicket.objects.filter(owner_user_id=88).exists())

    @patch("driver_profiles.views.httpx.Client")
    def test_staff_create_does_not_store_unverified_order_reference(self, client_factory):
        response = self.client.post(
            "/internal/v1/admin/drivers/support-tickets/create/",
            {
                "driver_email": "driver@example.test",
                "subject": "Unable to go online",
                "description": "The driver app is unable to change availability.",
                "category": "GPS_APP_ISSUE",
                "order_id": 702,
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 503)
        client_factory.assert_not_called()
        self.assertFalse(DriverSupportTicket.objects.filter(owner_user_id=88).exists())

    @patch(
        "driver_profiles.models.driver_kyc_storage.save",
        return_value="drivers/support/test/evidence.pdf",
    )
    def test_staff_upload_validates_and_audits_private_attachments(self, _save):
        uploaded = SimpleUploadedFile(
            "evidence.pdf",
            b"%PDF-1.4 test document",
            content_type="application/pdf",
        )

        response = self.client.post(
            f"/internal/v1/admin/drivers/support-tickets/{self.ticket.pk}/attachments/",
            {"file": uploaded},
            format="multipart",
            **self.headers,
        )

        self.assertEqual(response.status_code, 201)
        attachment = DriverSupportTicketAttachment.objects.get(ticket=self.ticket)
        self.assertEqual(attachment.file_name, "evidence.pdf")
        self.assertEqual(attachment.uploaded_by_user_id, 7)
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(
            DriverSupportTicketAudit.objects.get(action="ATTACHMENT_UPLOAD").details,
            {"attachment_ids": [attachment.pk]},
        )

    def test_staff_upload_rejects_mismatched_file_content(self):
        uploaded = SimpleUploadedFile(
            "evidence.pdf",
            b"not a PDF",
            content_type="application/pdf",
        )

        response = self.client.post(
            f"/internal/v1/admin/drivers/support-tickets/{self.ticket.pk}/attachments/",
            {"file": uploaded},
            format="multipart",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(DriverSupportTicketAttachment.objects.exists())

    @patch(
        "driver_profiles.models.driver_kyc_storage.open",
        return_value=ContentFile(b"%PDF-1.4 private evidence"),
    )
    def test_staff_download_is_scoped_audited_and_not_cacheable(self, _open):
        attachment = DriverSupportTicketAttachment.objects.create(
            ticket=self.ticket,
            file="drivers/support/test/evidence.pdf",
            file_name="evidence.pdf",
            file_size=25,
            mime_type="application/pdf",
            uploaded_by_user_id=7,
            uploaded_by_email="agent@example.test",
        )

        response = self.client.get(
            (
                f"/internal/v1/admin/drivers/support-tickets/{self.ticket.pk}/"
                f"attachments/{attachment.pk}/content/"
            ),
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 private evidence")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(
            DriverSupportTicketAudit.objects.get(action="ATTACHMENT_VIEW").details,
            {"attachment_id": attachment.pk},
        )


@override_settings(
    DRIVER_FRAUD_REVIEW_SERVICE_ENABLED=True,
    DRIVER_ADMIN_TOKEN="f" * 40,
)
class InternalDriverLocationAnomalyTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.anomaly = DriverLocationAnomaly.objects.create(
            owner_user_id=41,
            active_order_id=721,
            anomaly_type="IMPOSSIBLE_TRAVEL",
            severity="CRITICAL",
            details={"distance_km": 75.2},
            reported_latitude=-17.7,
            reported_longitude=31.03,
            previous_latitude=-17.9,
            previous_longitude=30.9,
            distance_km=75.2,
            time_delta_seconds=30,
        )
        self.headers = {
            "HTTP_X_DRIVER_ADMIN_TOKEN": "f" * 40,
            "HTTP_X_DRIVER_ADMIN_ACTOR_ID": "7",
            "HTTP_X_DRIVER_ADMIN_ACTOR_EMAIL": "reviewer@example.test",
        }

    def test_anomaly_queue_is_private_filtered_and_bounded(self):
        denied = self.client.get("/internal/v1/admin/drivers/location-anomalies/")
        listed = self.client.get(
            "/internal/v1/admin/drivers/location-anomalies/",
            {"severity": "CRITICAL", "driver_id": "41", "page_size": "500"},
            **self.headers,
        )

        self.assertEqual(denied.status_code, 404)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["total"], 1)
        self.assertEqual(listed.data["page_size"], 100)
        self.assertEqual(listed.data["results"][0]["type"], "IMPOSSIBLE_TRAVEL")
        self.assertEqual(listed.data["results"][0]["reported_location"]["latitude"], -17.7)

    def test_anomaly_review_records_actor_and_prevents_rewrite(self):
        reviewed = self.client.post(
            f"/internal/v1/admin/drivers/location-anomalies/{self.anomaly.pk}/review/",
            {"review_note": "Confirmed GPS jump; escalated for investigation."},
            format="json",
            **self.headers,
        )
        repeated = self.client.post(
            f"/internal/v1/admin/drivers/location-anomalies/{self.anomaly.pk}/review/",
            {"review_note": "Attempt to overwrite review"},
            format="json",
            **self.headers,
        )

        self.assertEqual(reviewed.status_code, 200)
        self.assertTrue(reviewed.data["is_reviewed"])
        self.assertEqual(reviewed.data["reviewed_by"], "reviewer@example.test")
        self.assertEqual(repeated.status_code, 409)

    def test_anomaly_review_rejects_non_object_payload_without_mutating_record(self):
        response = self.client.post(
            f"/internal/v1/admin/drivers/location-anomalies/{self.anomaly.pk}/review/",
            ["not", "an", "object"],
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "A JSON object is required.")
        self.anomaly.refresh_from_db()
        self.assertFalse(self.anomaly.is_reviewed)
