from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from driver_profiles.models import DriverLocationHistory


class Command(BaseCommand):
    help = "Report or purge driver location history older than the retention period."

    def add_arguments(self, parser):
        parser.add_argument("--retention-days", type=int, default=90)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        retention_days = options["retention_days"]
        if not 1 <= retention_days <= 3650:
            raise CommandError("--retention-days must be between 1 and 3650.")

        cutoff = timezone.now() - timedelta(days=retention_days)
        expired = DriverLocationHistory.objects.filter(recorded_at__lt=cutoff)
        count = expired.count()
        if options["apply"]:
            deleted, _ = expired.delete()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Purged {deleted} driver location history rows older than "
                    f"{retention_days} days (cutoff {cutoff.isoformat()})."
                )
            )
            return

        self.stdout.write(
            f"Dry run: {count} driver location history rows are older than "
            f"{retention_days} days (cutoff {cutoff.isoformat()}). "
            "Pass --apply to delete them."
        )
