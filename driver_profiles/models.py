from uuid import uuid4

from django.db import models
from django.utils import timezone

from .fields import EncryptedCharField
from .kyc_storage import driver_kyc_storage


def driver_kyc_upload_path(instance, filename):
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return (
        f"drivers/kyc/{timezone.now():%Y/%m}/"
        f"{instance.document_type.lower()}/{uuid4().hex}.{extension}"
    )


def driver_support_attachment_upload_path(instance, filename):
    extension = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "application/pdf": "pdf",
    }.get(instance.mime_type, "bin")
    return (
        f"drivers/support/{timezone.now():%Y/%m}/"
        f"{instance.ticket_id}/{uuid4().hex}.{extension}"
    )


class DriverProfile(models.Model):
    VEHICLE_TYPES = [
        ("MOTORCYCLE", "Motorcycle"),
        ("BICYCLE", "Bicycle"),
        ("E-SCOOTER", "E-Scooter"),
        ("SCOOTER", "Scooter"),
    ]
    VERIFICATION_STATUSES = [
        ("PENDING", "Pending"),
        ("VERIFIED", "Verified"),
        ("REJECTED", "Rejected"),
    ]

    owner_user_id = models.PositiveBigIntegerField(unique=True)
    source_user_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    vehicle_type = models.CharField(
        max_length=20, choices=VEHICLE_TYPES, default="MOTORCYCLE"
    )
    vehicle_registration = models.CharField(max_length=50, blank=True)
    license_number = EncryptedCharField(max_length=50, blank=True, default="")
    verification_status = models.CharField(
        max_length=20,
        choices=VERIFICATION_STATUSES,
        default="PENDING",
    )
    is_available = models.BooleanField(default=False)
    service_radius_km = models.PositiveIntegerField(default=10)
    current_order_id = models.PositiveBigIntegerField(null=True, blank=True)
    source_current_order_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["verification_status", "is_available"]),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding and "updated_at" not in kwargs.get(
            "update_fields", ()
        ):
            self.updated_at = timezone.now()
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = tuple(kwargs["update_fields"]) + (
                    "updated_at",
                )
        super().save(*args, **kwargs)


class DriverLocation(models.Model):
    owner_user_id = models.PositiveBigIntegerField(unique=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90, latitude__lte=90),
                name="driver_location_latitude_range",
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__gte=-180, longitude__lte=180),
                name="driver_location_longitude_range",
            ),
        ]


class DriverLocationHistory(models.Model):
    source_log_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    owner_user_id = models.PositiveBigIntegerField(db_index=True)
    active_order_id = models.PositiveBigIntegerField(
        null=True, blank=True, db_index=True
    )
    latitude = models.FloatField()
    longitude = models.FloatField()
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-recorded_at", "-id"]
        indexes = [
            models.Index(
                fields=["owner_user_id", "-recorded_at"],
                name="driver_loc_hist_owner_time",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90, latitude__lte=90),
                name="driver_hist_latitude_range",
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__gte=-180, longitude__lte=180),
                name="driver_hist_longitude_range",
            ),
        ]


class DriverLocationAnomaly(models.Model):
    ANOMALY_TYPES = (
        ("SPEED_EXCEEDED", "Speed exceeded"),
        ("IMPOSSIBLE_TRAVEL", "Impossible travel"),
        ("GEOBOUNDARY_EXIT", "Outside service boundary"),
        ("STATIONARY_SPOOFING", "Stationary while reporting movement"),
        ("MIN_INTERVAL", "Update interval too short"),
        ("STATIC_LOCATION", "Repeated static location"),
    )
    SEVERITIES = (
        ("LOW", "Low"),
        ("MEDIUM", "Medium"),
        ("HIGH", "High"),
        ("CRITICAL", "Critical"),
    )

    owner_user_id = models.PositiveBigIntegerField(db_index=True)
    source_owner_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    active_order_id = models.PositiveBigIntegerField(
        null=True, blank=True, db_index=True
    )
    source_anomaly_id = models.PositiveBigIntegerField(
        null=True, blank=True, unique=True
    )
    source_order_id = models.PositiveBigIntegerField(null=True, blank=True)
    source_dispute_id = models.PositiveBigIntegerField(null=True, blank=True)
    source_reviewer_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    anomaly_type = models.CharField(max_length=30, choices=ANOMALY_TYPES)
    severity = models.CharField(max_length=10, choices=SEVERITIES, default="MEDIUM")
    details = models.JSONField(default=dict, blank=True)
    reported_latitude = models.FloatField(null=True, blank=True)
    reported_longitude = models.FloatField(null=True, blank=True)
    previous_latitude = models.FloatField(null=True, blank=True)
    previous_longitude = models.FloatField(null=True, blank=True)
    reported_speed_kmh = models.FloatField(null=True, blank=True)
    max_allowed_speed_kmh = models.FloatField(null=True, blank=True)
    distance_km = models.FloatField(null=True, blank=True)
    time_delta_seconds = models.FloatField(null=True, blank=True)
    is_reviewed = models.BooleanField(default=False)
    reviewed_by_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    reviewed_by_email = models.EmailField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["owner_user_id", "anomaly_type", "-created_at"],
                name="driver_loc_anomaly_owner",
            ),
            models.Index(
                fields=["severity", "-created_at"],
                name="driver_loc_anomaly_severity",
            ),
        ]


class DriverBehaviorScore(models.Model):
    owner_user_id = models.PositiveBigIntegerField(unique=True)
    source_score_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    on_time_score = models.DecimalField(max_digits=5, decimal_places=2, default=50)
    cancellation_score = models.DecimalField(max_digits=5, decimal_places=2, default=50)
    complaint_score = models.DecimalField(max_digits=5, decimal_places=2, default=50)
    location_accuracy_score = models.DecimalField(
        max_digits=5, decimal_places=2, default=50
    )
    rating_score = models.DecimalField(max_digits=5, decimal_places=2, default=50)
    overall_score = models.DecimalField(max_digits=5, decimal_places=2, default=50)
    total_deliveries_30d = models.IntegerField(default=0)
    on_time_deliveries_30d = models.IntegerField(default=0)
    cancellations_30d = models.IntegerField(default=0)
    complaints_30d = models.IntegerField(default=0)
    avg_rating_30d = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    gps_anomalies_30d = models.IntegerField(default=0)
    rating_spikes_30d = models.IntegerField(default=0)
    is_flagged = models.BooleanField(default=False)
    flag_reasons = models.JSONField(default=list, blank=True)
    computed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["overall_score", "id"]
        indexes = [
            models.Index(
                fields=["is_flagged", "overall_score"], name="driver_behavior_flag"
            ),
        ]

    @property
    def risk_level(self):
        if self.overall_score >= 80:
            return "LOW"
        if self.overall_score >= 60:
            return "MEDIUM"
        if self.overall_score >= 40:
            return "HIGH"
        return "CRITICAL"


class DriverRatingAnomaly(models.Model):
    ANOMALY_TYPES = (
        ("SPIKE", "Sudden Rating Spike"),
        ("CLUSTER", "Suspicious 5-Star Cluster"),
        ("RECIPROCAL", "Reciprocal Rating Pattern"),
        ("NEW_DRIVER_RUSH", "New Driver Rating Rush"),
    )

    owner_user_id = models.PositiveBigIntegerField(db_index=True)
    source_anomaly_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    anomaly_type = models.CharField(max_length=30, choices=ANOMALY_TYPES)
    details = models.JSONField(default=dict, blank=True)
    rating_count = models.IntegerField(default=0)
    avg_rating_in_window = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    baseline_rating = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    is_reviewed = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]


class DriverDevice(models.Model):
    owner_user_id = models.PositiveBigIntegerField(db_index=True)
    source_device_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    device_id = models.CharField(max_length=255)
    device_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)
    device_type = models.CharField(max_length=20, blank=True)
    device_model = models.CharField(max_length=100, blank=True)
    os_version = models.CharField(max_length=30, blank=True)
    app_version = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-last_seen_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner_user_id", "device_id"], name="uq_driver_device_owner_device"
            ),
        ]


class DriverSupportTicket(models.Model):
    CATEGORY_CHOICES = (
        ("LONG_WAIT", "Long wait at pickup"),
        ("CUSTOMER_UNAVAILABLE", "Customer unavailable"),
        ("WRONG_ADDRESS", "Wrong address"),
        ("RESTAURANT_CLOSED", "Restaurant closed"),
        ("ALREADY_PICKED_UP", "Already picked up"),
        ("ORDER_CANCELED", "Order canceled"),
        ("RESTAURANT_REFUSES", "Restaurant refuses order"),
        ("VEHICLE_BREAKDOWN", "Vehicle breakdown"),
        ("CANT_ACCESS", "Can't access location"),
        ("PAYMENT_ISSUE", "Payment issue"),
        ("GPS_APP_ISSUE", "GPS / App issue"),
        ("SAFETY_CONCERN", "Safety concern"),
        ("MISSING_EQUIPMENT", "Missing equipment"),
        ("OVERLAPPING_ROUTES", "Overlapping routes"),
        ("ADDRESS_CHANGED", "Address changed"),
        ("OTHER", "Other issue"),
    )
    PRIORITY_CHOICES = (
        ("LOW", "Low"),
        ("MEDIUM", "Medium"),
        ("HIGH", "High"),
        ("URGENT", "Urgent"),
    )
    STATUS_CHOICES = (
        ("OPEN", "Open"),
        ("IN_PROGRESS", "In Progress"),
        ("WAITING_DRIVER", "Waiting for Driver"),
        ("RESOLVED", "Resolved"),
        ("CLOSED", "Closed"),
    )

    owner_user_id = models.PositiveBigIntegerField(db_index=True)
    order_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    category = models.CharField(
        max_length=30, choices=CATEGORY_CHOICES, default="OTHER"
    )
    subject = models.CharField(max_length=200)
    description = models.TextField()
    priority = models.CharField(
        max_length=10, choices=PRIORITY_CHOICES, default="MEDIUM"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="OPEN")
    resolution_notes = models.TextField(blank=True)
    assigned_to_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    assigned_to_email = models.EmailField(blank=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    escalated = models.BooleanField(default=False)
    escalation_reason = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["owner_user_id", "status"],
                name="driver_ticket_owner_status",
            ),
        ]


class DriverSupportTicketMessage(models.Model):
    ticket = models.ForeignKey(
        DriverSupportTicket,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    sender_is_driver = models.BooleanField(default=True)
    message = models.TextField()
    channel = models.CharField(max_length=10, default="IN_APP")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["ticket", "created_at"],
                name="driver_ticket_msg_time",
            ),
        ]


class DriverSupportTicketInternalNote(models.Model):
    ticket = models.ForeignKey(
        DriverSupportTicket,
        on_delete=models.CASCADE,
        related_name="internal_notes",
    )
    actor_user_id = models.PositiveBigIntegerField()
    actor_email = models.EmailField()
    message = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]


class DriverSupportTicketAudit(models.Model):
    ticket = models.ForeignKey(
        DriverSupportTicket,
        on_delete=models.CASCADE,
        related_name="audit_events",
    )
    actor_user_id = models.PositiveBigIntegerField()
    actor_email = models.EmailField()
    action = models.CharField(max_length=30)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]


class DriverSupportTicketAttachment(models.Model):
    ticket = models.ForeignKey(
        DriverSupportTicket,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(
        upload_to=driver_support_attachment_upload_path,
        storage=driver_kyc_storage,
    )
    file_name = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField()
    mime_type = models.CharField(max_length=100)
    uploaded_by_user_id = models.PositiveBigIntegerField()
    uploaded_by_email = models.EmailField()
    uploaded_at = models.DateTimeField(default=timezone.now)
    request_id = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-uploaded_at", "-id"]
        indexes = [models.Index(fields=["ticket", "-uploaded_at"])]


class DriverKYCDocument(models.Model):
    REQUIRED_DOCUMENT_TYPES = [
        ("LICENSE_PHOTO", "Driver License"),
        ("ID_DOCUMENT", "National Identity Document"),
        ("VEHICLE_PHOTO", "Vehicle Photo"),
        ("REGISTRATION_BOOK", "Vehicle Registration Book"),
        ("SELFIE", "Driver Selfie"),
        ("SELFIE_HOLDING_ID", "Selfie Holding Identity Document"),
    ]
    LEGACY_DOCUMENT_TYPES = [
        ("LEGACY_ID_DOCUMENT", "Legacy ID Document"),
        ("LEGACY_LICENSE", "Legacy Driver License"),
        ("LEGACY_VEHICLE_REGISTRATION", "Legacy Vehicle Registration"),
        ("LEGACY_INSURANCE", "Legacy Insurance Certificate"),
        ("LEGACY_SELFIE", "Legacy Selfie"),
        ("LEGACY_SELFIE_HOLDING_ID", "Legacy Selfie Holding ID"),
        ("LEGACY_POLICE_CLEARANCE", "Legacy Police Clearance"),
        ("LEGACY_MEDICAL_CERT", "Legacy Medical Certificate"),
        ("LEGACY_OTHER", "Other Legacy Document"),
    ]
    DOCUMENT_TYPES = REQUIRED_DOCUMENT_TYPES + LEGACY_DOCUMENT_TYPES
    REVIEW_STATUSES = [
        ("PENDING", "Pending Review"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("EXPIRED", "Expired"),
        ("UNDER_REVIEW", "Under Review"),
    ]

    profile = models.ForeignKey(
        DriverProfile, on_delete=models.CASCADE, related_name="kyc_documents"
    )
    document_type = models.CharField(max_length=30, choices=DOCUMENT_TYPES)
    file = models.FileField(
        upload_to=driver_kyc_upload_path,
        storage=driver_kyc_storage,
        blank=True,
    )
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    file_size = models.PositiveIntegerField()
    file_hash = models.CharField(max_length=64, blank=True)
    version = models.PositiveIntegerField(default=1)
    is_current = models.BooleanField(default=True)
    expires_at = models.DateField(null=True, blank=True)
    verification_status = models.CharField(
        max_length=20, choices=REVIEW_STATUSES, default="PENDING"
    )
    rejection_reason = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    reviewed_by_email = models.EmailField(blank=True)
    source_reviewer_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    source_document_id = models.PositiveBigIntegerField(
        null=True, blank=True, unique=True
    )
    request_id = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["document_type", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "document_type", "version"],
                name="uq_driver_kyc_profile_document_version",
            ),
            models.UniqueConstraint(
                fields=["profile", "document_type"],
                condition=models.Q(is_current=True),
                name="uq_driver_kyc_current_document",
            ),
        ]
        indexes = [
            models.Index(fields=["verification_status", "uploaded_at"]),
        ]

    def __str__(self):
        return f"Driver KYC {self.profile_id}: {self.document_type}"


class DriverKYCAuditLog(models.Model):
    ACTIONS = [
        ("UPLOAD", "Upload"),
        ("APPROVE", "Approve"),
        ("REJECT", "Reject"),
        ("VIEW", "View"),
    ]

    profile = models.ForeignKey(
        DriverProfile, on_delete=models.PROTECT, related_name="kyc_audit_entries"
    )
    document = models.ForeignKey(
        DriverKYCDocument,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    actor_user_id = models.PositiveBigIntegerField()
    actor_email = models.EmailField(blank=True)
    action = models.CharField(max_length=10, choices=ACTIONS)
    request_id = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]


from .admin_audit_models import DriverAdminAuditLog  # noqa: E402, F401
from .quest_models import DriverQuest, DriverQuestMilestone, QuestTemplate  # noqa: E402, F401
