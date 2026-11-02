from datetime import timedelta
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from driver_profiles.models import DriverLocationHistory


class DriverLocationRetentionTests(TestCase):
    def test_purge_is_dry_run_by_default_and_apply_deletes_only_expired_rows(self):
        now = timezone.now()
        expired = DriverLocationHistory.objects.create(
            owner_user_id=71,
            latitude=-17.8,
            longitude=31.0,
            recorded_at=now - timedelta(days=91),
        )
        recent = DriverLocationHistory.objects.create(
            owner_user_id=72,
            latitude=-17.9,
            longitude=31.1,
            recorded_at=now - timedelta(days=89),
        )
        output = StringIO()

        call_command(
            "purge_driver_location_history",
            stdout=output,
        )

        self.assertIn("Dry run: 1", output.getvalue())
        self.assertTrue(
            DriverLocationHistory.objects.filter(pk=expired.pk).exists()
        )

        output = StringIO()
        call_command(
            "purge_driver_location_history",
            "--apply",
            stdout=output,
        )

        self.assertIn("Purged 1", output.getvalue())
        self.assertFalse(
            DriverLocationHistory.objects.filter(pk=expired.pk).exists()
        )
        self.assertTrue(DriverLocationHistory.objects.filter(pk=recent.pk).exists())

    def test_purge_rejects_invalid_retention_period(self):
        with self.assertRaisesMessage(CommandError, "--retention-days"):
            call_command(
                "purge_driver_location_history",
                retention_days=0,
                stdout=StringIO(),
            )
