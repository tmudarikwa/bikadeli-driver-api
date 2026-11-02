import math

from PIL import Image, UnidentifiedImageError
from rest_framework import serializers

from .models import (
    DriverKYCDocument,
    DriverProfile,
    DriverSupportTicket,
    DriverSupportTicketMessage,
)


class DriverProfileSerializer(serializers.ModelSerializer):
    selfie_holding_id_status = serializers.SerializerMethodField()
    vehicle_photo_status = serializers.SerializerMethodField()
    kyc_document_statuses = serializers.SerializerMethodField()

    class Meta:
        model = DriverProfile
        fields = [
            "id",
            "owner_user_id",
            "vehicle_type",
            "vehicle_registration",
            "verification_status",
            "is_available",
            "service_radius_km",
            "current_order_id",
            "selfie_holding_id_status",
            "vehicle_photo_status",
            "kyc_document_statuses",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "owner_user_id",
            "verification_status",
            "is_available",
            "service_radius_km",
            "current_order_id",
            "selfie_holding_id_status",
            "vehicle_photo_status",
            "kyc_document_statuses",
            "created_at",
            "updated_at",
        ]

    @staticmethod
    def _document_status(profile, document_type):
        return (
            profile.kyc_documents.filter(
                document_type=document_type,
                is_current=True,
            )
            .values_list("verification_status", flat=True)
            .first()
            or "PENDING"
        )

    def get_selfie_holding_id_status(self, profile):
        return self._document_status(profile, "SELFIE_HOLDING_ID")

    def get_vehicle_photo_status(self, profile):
        return self._document_status(profile, "VEHICLE_PHOTO")

    def get_kyc_document_statuses(self, profile):
        statuses = {
            document.document_type: document.verification_status
            for document in profile.kyc_documents.filter(is_current=True)
        }
        return {
            document_type: statuses.get(document_type, "PENDING")
            for document_type, _ in DriverKYCDocument.REQUIRED_DOCUMENT_TYPES
        }


class CreateDriverProfileSerializer(serializers.ModelSerializer):
    license_number = serializers.CharField(
        max_length=50, write_only=True, required=False, allow_blank=True
    )

    class Meta:
        model = DriverProfile
        fields = ["vehicle_type", "vehicle_registration", "license_number"]
        extra_kwargs = {
            "vehicle_type": {"required": False},
            "vehicle_registration": {"required": False, "allow_blank": True},
        }


class DriverRegistrationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    phone_number = serializers.CharField(max_length=100)
    password = serializers.CharField(write_only=True, min_length=12)
    confirm_password = serializers.CharField(write_only=True, min_length=12)
    vehicle_type = serializers.ChoiceField(choices=DriverProfile.VEHICLE_TYPES)
    license_number = serializers.CharField(max_length=50, allow_blank=False)
    vehicle_registration = serializers.CharField(
        max_length=50, required=False, allow_blank=True
    )

    def validate(self, attrs):
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError(
                {"confirm_password": "Passwords do not match."}
            )
        return attrs


class DriverKYCDocumentUploadSerializer(serializers.Serializer):
    license_photo = serializers.FileField(required=False)
    id_document = serializers.FileField(required=False)
    vehicle_photo = serializers.FileField(required=False)
    registration_book = serializers.FileField(required=False)
    selfie = serializers.FileField(required=False)
    selfie_holding_id = serializers.FileField(required=False)

    DOCUMENT_FIELDS = {
        "license_photo": "LICENSE_PHOTO",
        "id_document": "ID_DOCUMENT",
        "vehicle_photo": "VEHICLE_PHOTO",
        "registration_book": "REGISTRATION_BOOK",
        "selfie": "SELFIE",
        "selfie_holding_id": "SELFIE_HOLDING_ID",
    }

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                {"detail": "At least one KYC image is required."}
            )
        for field, upload in attrs.items():
            if upload.size <= 0 or upload.size > 10 * 1024 * 1024:
                raise serializers.ValidationError(
                    {field: "Image must be between 1 byte and 10 MB."}
                )
            filename = (upload.name or "").lower()
            extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
            if extension not in {"jpg", "jpeg", "png", "webp"}:
                raise serializers.ValidationError(
                    {field: "Only JPEG, PNG, and WebP images are accepted."}
                )
            try:
                upload.seek(0)
                image = Image.open(upload)
                if image.width * image.height > 20_000_000:
                    raise serializers.ValidationError(
                        {field: "Image dimensions must not exceed 20 megapixels."}
                    )
                image.verify()
                image_format = image.format
                upload.seek(0)
                if image_format not in {"JPEG", "PNG", "WEBP"}:
                    raise serializers.ValidationError(
                        {field: "Only JPEG, PNG, and WebP images are accepted."}
                    )
                upload.verified_image_format = image_format
            except (
                Image.DecompressionBombError,
                UnidentifiedImageError,
                OSError,
                ValueError,
            ) as exc:
                raise serializers.ValidationError(
                    {field: "The uploaded file is not a valid image."}
                ) from exc
        return attrs


class DriverKYCDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverKYCDocument
        fields = [
            "id",
            "document_type",
            "original_filename",
            "content_type",
            "file_size",
            "verification_status",
            "rejection_reason",
            "version",
            "is_current",
            "expires_at",
            "uploaded_at",
            "updated_at",
            "reviewed_at",
        ]
        read_only_fields = fields


class DriverKYCDocumentReviewSerializer(serializers.Serializer):
    verification_status = serializers.ChoiceField(choices=["APPROVED", "REJECTED"])
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)
    actor_id = serializers.IntegerField(min_value=1)
    actor_email = serializers.EmailField()
    request_id = serializers.CharField(max_length=100, required=False, allow_blank=True)

    def validate(self, attrs):
        if (
            attrs["verification_status"] == "REJECTED"
            and not attrs.get("reason", "").strip()
        ):
            raise serializers.ValidationError(
                {"reason": "A rejection reason is required."}
            )
        return attrs


class DriverLocationUpdateSerializer(serializers.Serializer):
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()

    def validate(self, attrs):
        for field in ("latitude", "longitude"):
            if not math.isfinite(attrs[field]):
                raise serializers.ValidationError(
                    {field: "A finite number is required."}
                )
        return attrs


class DriverSupportTicketSerializer(serializers.ModelSerializer):
    message_count = serializers.IntegerField(read_only=True)
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = DriverSupportTicket
        fields = [
            "id",
            "category",
            "subject",
            "description",
            "priority",
            "status",
            "resolution_notes",
            "created_at",
            "updated_at",
            "resolved_at",
            "message_count",
            "last_message",
        ]
        read_only_fields = fields

    def get_last_message(self, ticket):
        latest_messages = getattr(ticket, "latest_messages", None)
        if latest_messages is None:
            message = ticket.messages.order_by("-created_at", "-id").first()
        else:
            message = latest_messages[0] if latest_messages else None
        if message is None:
            return None
        return {
            "message": message.message[:120],
            "sender_is_driver": message.sender_is_driver,
            "created_at": message.created_at.isoformat(),
        }


class CreateDriverSupportTicketSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=DriverSupportTicket.CATEGORY_CHOICES)
    subject = serializers.CharField(max_length=200)
    description = serializers.CharField(max_length=10000)
    priority = serializers.ChoiceField(
        choices=DriverSupportTicket.PRIORITY_CHOICES,
        default="MEDIUM",
    )
    order_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
    )


class DriverSupportTicketMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverSupportTicketMessage
        fields = ["id", "sender_is_driver", "message", "created_at"]
        read_only_fields = fields


class CreateDriverSupportTicketMessageSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=10000, allow_blank=False)

    def validate_message(self, value):
        if not value.strip():
            raise serializers.ValidationError("Message cannot be blank.")
        return value.strip()
