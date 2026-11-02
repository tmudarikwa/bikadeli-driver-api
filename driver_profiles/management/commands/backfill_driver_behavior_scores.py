import os
from datetime import timezone as datetime_timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction
from django.utils import timezone

from driver_profiles.models import DriverBehaviorScore

SOURCE_TABLE = "fraud_driverbehaviorscore"
COLUMNS = (
    "id",
    "driver_id",
    "on_time_score",
    "cancellation_score",
    "complaint_score",
    "location_accuracy_score",
    "rating_score",
    "overall_score",
    "total_deliveries_30d",
    "on_time_deliveries_30d",
    "cancellations_30d",
    "complaints_30d",
    "avg_rating_30d",
    "gps_anomalies_30d",
    "rating_spikes_30d",
    "is_flagged",
    "flag_reasons",
    "computed_at",
    "created_at",
)


def _aware(value):
    if timezone.is_naive(value):
        return timezone.make_aware(value, datetime_timezone.utc)
    return value


class Command(BaseCommand):
    help = "Dry-run or apply an idempotent backfill of legacy driver behavior scores."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        source_url = os.getenv("SOURCE_DATABASE_URL", "").strip()
        destination_url = os.getenv("DATABASE_URL", "").strip()
        if not source_url or not destination_url or source_url == destination_url:
            raise CommandError("Set distinct SOURCE_DATABASE_URL and DATABASE_URL.")
        from bikadeli_common.settings import database_config_from_url

        config = connections.databases["default"].copy() | database_config_from_url(
            source_url
        )
        connections.databases["source"] = config
        try:
            source = connections["source"]
            with transaction.atomic(using="source"), source.cursor() as cursor:
                if source.vendor == "postgresql":
                    cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    f"SELECT {', '.join(COLUMNS)} FROM {SOURCE_TABLE} ORDER BY id"
                )
                rows = [dict(zip(COLUMNS, row)) for row in cursor.fetchall()]
        except Exception as exc:
            raise CommandError(
                f"Could not read legacy behavior scores ({type(exc).__name__})."
            ) from exc
        finally:
            connections["source"].close()
            connections.databases.pop("source", None)
            connections.settings.pop("source", None) if hasattr(
                connections, "settings"
            ) else None

        existing = set(
            DriverBehaviorScore.objects.filter(
                source_score_id__in=[row["id"] for row in rows]
            ).values_list("source_score_id", flat=True)
        )
        missing = [row for row in rows if row["id"] not in existing]
        if options["apply"]:
            with transaction.atomic():
                DriverBehaviorScore.objects.bulk_create(
                    [
                        DriverBehaviorScore(
                            source_score_id=row["id"],
                            owner_user_id=row["driver_id"],
                            **{
                                field: row[field]
                                for field in COLUMNS
                                if field not in {"id", "driver_id", "computed_at", "created_at"}
                            },
                            computed_at=_aware(row["computed_at"]),
                            created_at=_aware(row["created_at"]),
                        )
                        for row in missing
                    ]
                )
        self.stdout.write(
            f"source={len(rows)} existing={len(existing)} "
            f"{'inserted' if options['apply'] else 'insertable'}={len(missing)}"
        )
