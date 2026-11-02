from django.db import models
from django.utils import timezone


class DriverAdminAuditLog(models.Model):
    profile = models.ForeignKey(
        "driver_profiles.DriverProfile", on_delete=models.PROTECT, related_name="admin_audit_entries"
    )
    actor_user_id = models.PositiveBigIntegerField()
    actor_email = models.EmailField(blank=True)
    action = models.CharField(max_length=40)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
