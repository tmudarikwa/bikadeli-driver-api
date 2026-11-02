from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import jwt
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from driver_profiles.kyc_scanning import MalwareDetected, ScannerUnavailable
from driver_profiles.kyc_storage import driver_kyc_storage
from driver_profiles.models import (
    DriverKYCAuditLog,
    DriverKYCDocument,
    DriverProfile,
)


def image_upload(name, color=(20, 120, 80)):
    stream = BytesIO()
    Image.new("RGB", (8, 8), color=color).save(stream, format="PNG")
    return SimpleUploadedFile(name, stream.getvalue(), content_type="image/png")


class DriverKycApiTests(TestCase):
    def setUp(self):
        self.profile = DriverProfile.objects.create(owner_user_id=415)
        self.client = APIClient()
        token = jwt.encode(
            {
                "token_type": "access",
                "user_id": 415,
                "iat": timezone.now(),
                "exp": timezone.now() + timedelta(minutes=5),
            },
            settings.JWT_SIGNING_KEY,
            algorithm="HS256",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.override = self.settings(
            DRIVER_KYC_SERVICE_ENABLED=True,
            DRIVER_ADMIN_TOKEN="d" * 40,
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.scanner_patcher = patch("driver_profiles.views.scan_kyc_upload")
        self.scanner_mock = self.scanner_patcher.start()
        self.addCleanup(self.scanner_patcher.stop)
        self.addCleanup(self._delete_profile_files)

    def _delete_profile_files(self):
        for document in DriverKYCDocument.objects.filter(profile=self.profile):
            if document.file:
                driver_kyc_storage.delete(document.file.name)

    def test_driver_can_upload_private_encrypted_documents_and_read_statuses(self):
        response = self.client.post(
            "/api/v1/drivers/profile/submit_verification/",
            {"license_photo": image_upload("license.png")},
            format="multipart",
            HTTP_X_REQUEST_ID="driver-upload-1",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["verification_status"], "PENDING")
        self.assertTrue(response.data["documents"]["LICENSE_PHOTO"])
        document = DriverKYCDocument.objects.get(
            profile=self.profile, document_type="LICENSE_PHOTO"
        )
        self.assertEqual(document.verification_status, "PENDING")
        self.assertEqual(document.content_type, "image/png")
        self.assertEqual(document.file.open("rb").read()[:8], b"\x89PNG\r\n\x1a\n")
        encrypted = Path(driver_kyc_storage.path(document.file.name)).read_bytes()
        self.assertNotEqual(encrypted[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(DriverKYCAuditLog.objects.filter(action="UPLOAD").count(), 1)

        status_response = self.client.get("/api/v1/drivers/profile/kyc/")
        self.assertEqual(status_response.status_code, 200)
        self.assertTrue(status_response.data["documents_submitted"]["LICENSE_PHOTO"])
        self.assertFalse(status_response.data["all_complete"])
        self.assertNotIn("file", status_response.data["documents"]["LICENSE_PHOTO"])
        profile_response = self.client.get("/api/v1/drivers/profile/")
        self.assertEqual(
            profile_response.data["kyc_document_statuses"]["LICENSE_PHOTO"],
            "PENDING",
        )
        self.assertEqual(
            profile_response.data["kyc_document_statuses"]["SELFIE"],
            "PENDING",
        )

    def test_reupload_keeps_previous_version_and_exposes_only_current_status(self):
        first_upload = image_upload("license-v1.png", color=(10, 20, 30))
        first_bytes = first_upload.read()
        first_upload.seek(0)
        first_response = self.client.post(
            "/api/v1/drivers/profile/submit_verification/",
            {"license_photo": first_upload},
            format="multipart",
        )
        self.assertEqual(first_response.status_code, 200)
        first_document = DriverKYCDocument.objects.get(
            profile=self.profile,
            document_type="LICENSE_PHOTO",
            version=1,
        )

        second_upload = image_upload("license-v2.png", color=(40, 50, 60))
        second_bytes = second_upload.read()
        second_upload.seek(0)
        second_response = self.client.post(
            "/api/v1/drivers/profile/submit_verification/",
            {"license_photo": second_upload},
            format="multipart",
        )

        self.assertEqual(second_response.status_code, 200)
        first_document.refresh_from_db()
        second_document = DriverKYCDocument.objects.get(
            profile=self.profile,
            document_type="LICENSE_PHOTO",
            version=2,
        )
        self.assertFalse(first_document.is_current)
        self.assertTrue(second_document.is_current)
        with first_document.file.open("rb") as stored_first:
            self.assertEqual(stored_first.read(), first_bytes)
        with second_document.file.open("rb") as stored_second:
            self.assertEqual(stored_second.read(), second_bytes)
        self.assertEqual(
            second_response.data["documents"]["LICENSE_PHOTO"],
            True,
        )
        status_response = self.client.get("/api/v1/drivers/profile/kyc/")
        self.assertEqual(
            status_response.data["documents"]["LICENSE_PHOTO"]["version"],
            2,
        )
        self.assertEqual(
            DriverProfile.objects.get(pk=self.profile.pk)
            .kyc_documents.filter(is_current=True)
            .count(),
            1,
        )
        old_review = self.client.post(
            f"/internal/v1/admin/drivers/kyc/documents/{first_document.pk}/review/",
            {
                "verification_status": "APPROVED",
                "actor_id": 7,
                "actor_email": "reviewer@example.test",
            },
            format="json",
            HTTP_X_DRIVER_ADMIN_TOKEN="d" * 40,
        )
        self.assertEqual(old_review.status_code, 409)

    def test_upload_rejects_spoofed_content_and_oversized_images(self):
        spoofed = SimpleUploadedFile(
            "license.jpg", b"not an image", content_type="image/jpeg"
        )
        response = self.client.post(
            "/api/v1/drivers/profile/submit_verification/",
            {"license_photo": spoofed},
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(DriverKYCDocument.objects.exists())

    def test_upload_is_rejected_when_malware_is_detected(self):
        self.scanner_mock.side_effect = MalwareDetected

        response = self.client.post(
            "/api/v1/drivers/profile/submit_verification/",
            {"license_photo": image_upload("license.png")},
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(DriverKYCDocument.objects.exists())
        self.assertFalse(DriverKYCAuditLog.objects.filter(action="UPLOAD").exists())

    def test_upload_fails_closed_when_scanner_is_unavailable(self):
        self.scanner_mock.side_effect = ScannerUnavailable

        response = self.client.post(
            "/api/v1/drivers/profile/submit_verification/",
            {"license_photo": image_upload("license.png")},
            format="multipart",
        )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(DriverKYCDocument.objects.exists())
        self.assertFalse(DriverKYCAuditLog.objects.filter(action="UPLOAD").exists())

    def test_upload_requires_existing_driver_profile(self):
        other_client = APIClient()
        token = jwt.encode(
            {
                "token_type": "access",
                "user_id": 9999,
                "iat": timezone.now(),
                "exp": timezone.now() + timedelta(minutes=5),
            },
            settings.JWT_SIGNING_KEY,
            algorithm="HS256",
        )
        other_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        response = other_client.post(
            "/api/v1/drivers/profile/submit_verification/",
            {"license_photo": image_upload("license.png")},
            format="multipart",
        )

        self.assertEqual(response.status_code, 404)

    def test_internal_review_requires_secret_and_records_actor_audit(self):
        document = DriverKYCDocument.objects.create(
            profile=self.profile,
            document_type="LICENSE_PHOTO",
            original_filename="license.png",
            content_type="image/png",
            file_size=10,
        )
        payload = {
            "verification_status": "APPROVED",
            "actor_id": 7,
            "actor_email": "reviewer@example.test",
            "request_id": "review-1",
        }
        denied = self.client.post(
            f"/internal/v1/admin/drivers/kyc/documents/{document.pk}/review/",
            payload,
            format="json",
        )
        with self.settings(DRIVER_ADMIN_TOKEN="d" * 40):
            approved = self.client.post(
                f"/internal/v1/admin/drivers/kyc/documents/{document.pk}/review/",
                payload,
                format="json",
                HTTP_X_DRIVER_ADMIN_TOKEN="d" * 40,
            )

        self.assertEqual(denied.status_code, 404)
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.data["verification_status"], "APPROVED")
        self.assertEqual(
            DriverKYCAuditLog.objects.get(action="APPROVE").actor_user_id, 7
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.verification_status, "PENDING")
        self.assertFalse(self.profile.is_available)

    def test_rejection_requires_reason_and_review_retry_is_idempotent(self):
        document = DriverKYCDocument.objects.create(
            profile=self.profile,
            document_type="LICENSE_PHOTO",
            original_filename="license.png",
            content_type="image/png",
            file_size=10,
        )
        url = f"/internal/v1/admin/drivers/kyc/documents/{document.pk}/review/"
        headers = {"HTTP_X_DRIVER_ADMIN_TOKEN": "d" * 40}
        missing_reason = self.client.post(
            url,
            {
                "verification_status": "REJECTED",
                "actor_id": 7,
                "actor_email": "reviewer@example.test",
            },
            format="json",
            **headers,
        )
        rejection_payload = {
            "verification_status": "REJECTED",
            "reason": "Unreadable image",
            "actor_id": 7,
            "actor_email": "reviewer@example.test",
            "request_id": "reject-1",
        }
        rejected = self.client.post(url, rejection_payload, format="json", **headers)
        repeated = self.client.post(url, rejection_payload, format="json", **headers)
        conflict = self.client.post(
            url,
            {
                **rejection_payload,
                "reason": "Different decision",
            },
            format="json",
            **headers,
        )

        self.assertEqual(missing_reason.status_code, 400)
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.verification_status, "REJECTED")

    def test_profile_becomes_verified_only_after_all_required_documents_are_approved(
        self,
    ):
        documents = [
            DriverKYCDocument.objects.create(
                profile=self.profile,
                document_type=document_type,
                original_filename=f"{document_type.lower()}.png",
                content_type="image/png",
                file_size=10,
            )
            for document_type, _ in DriverKYCDocument.DOCUMENT_TYPES
        ]
        with self.settings(DRIVER_ADMIN_TOKEN="d" * 40):
            for index, document in enumerate(documents):
                response = self.client.post(
                    f"/internal/v1/admin/drivers/kyc/documents/{document.pk}/review/",
                    {
                        "verification_status": "APPROVED",
                        "actor_id": 7,
                        "actor_email": "reviewer@example.test",
                        "request_id": f"approve-{index}",
                    },
                    format="json",
                    HTTP_X_DRIVER_ADMIN_TOKEN="d" * 40,
                )
                self.assertEqual(response.status_code, 200)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.verification_status, "VERIFIED")
        self.assertFalse(self.profile.is_available)
        self.assertEqual(
            self.client.get("/api/v1/drivers/profile/").data[
                "selfie_holding_id_status"
            ],
            "APPROVED",
        )

    def test_document_content_is_private_and_access_is_audited(self):
        document = DriverKYCDocument.objects.create(
            profile=self.profile,
            document_type="LICENSE_PHOTO",
            original_filename="license.png",
            content_type="image/png",
            file_size=10,
        )
        document.file.save("license.png", image_upload("license.png"), save=True)
        url = f"/internal/v1/admin/drivers/kyc/documents/{document.pk}/content/"
        with self.settings(DRIVER_ADMIN_TOKEN="d" * 40):
            response = self.client.get(
                url,
                HTTP_X_DRIVER_ADMIN_TOKEN="d" * 40,
                HTTP_X_DRIVER_ADMIN_ACTOR_ID="7",
                HTTP_X_DRIVER_ADMIN_ACTOR_EMAIL="reviewer@example.test",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(DriverKYCAuditLog.objects.get(action="VIEW").actor_user_id, 7)
