import hashlib
import hmac
import json
import math
import os
from datetime import datetime
from datetime import timezone as datetime_timezone
from urllib.parse import unquote, urlsplit

from bikadeli_common.settings import database_config_from_url
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from driver_profiles.models import DriverLocationAnomaly, DriverProfile

SOURCE_TABLE = "fraud_driverlocationanomaly"
ANOMALY_TYPES = {
    "SPEED_EXCEEDED",
    "IMPOSSIBLE_TRAVEL",
    "GEOBOUNDARY_EXIT",
    "STATIONARY_SPOOFING",
    "MIN_INTERVAL",
    "STATIC_LOCATION",
}
SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
SOURCE_FIELDS = (
    "source_anomaly_id",
    "owner_user_id",
    "source_owner_user_id",
    "source_order_id",
    "source_dispute_id",
    "source_reviewer_user_id",
    "anomaly_type",
    "severity",
    "details",
    "reported_latitude",
    "reported_longitude",
    "previous_latitude",
    "previous_longitude",
    "reported_speed_kmh",
    "max_allowed_speed_kmh",
    "distance_km",
    "time_delta_seconds",
    "is_reviewed",
    "review_note",
    "created_at",
)


def _database_identity(database_url):
    parsed = urlsplit(database_url.strip())
    if parsed.scheme == "sqlite":
        path = unquote(parsed.path)
        if path in {"", "/:memory:"}:
            path = ":memory:"
        if os.name == "nt" and len(path) > 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return (
            "sqlite",
            os.path.normcase(os.path.realpath(path)) if path != ":memory:" else path,
        )
    scheme = parsed.scheme.replace("+psycopg", "")
    if scheme in {"postgres", "postgresql"}:
        scheme = "postgresql"
    return (
        scheme,
        (parsed.hostname or "").lower(),
        parsed.port or 5432,
        unquote(parsed.path),
    )


def _as_datetime(value):
    parsed = value if isinstance(value, datetime) else parse_datetime(str(value))
    if parsed is None:
        raise ValueError("invalid timestamp")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def _checksum(row):
    serialized = json.dumps(
        {field: row[field] for field in SOURCE_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hmac.new(
        settings.DRIVER_FIELDS_ENCRYPTION_KEY.encode(),
        serialized,
        hashlib.sha256,
    ).hexdigest()


class Command(BaseCommand):
    help = (
        "Dry-run or apply an idempotent backfill of legacy driver location anomalies."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Insert missing rows. Without this flag, no rows are written.",
        )

    def handle(self, *args, **options):
        source_url = os.getenv("SOURCE_DATABASE_URL", "").strip()
        destination_url = os.getenv("DATABASE_URL", "").strip()
        if not source_url or not destination_url:
            raise CommandError("Set SOURCE_DATABASE_URL and DATABASE_URL explicitly.")
        try:
            if _database_identity(source_url) == _database_identity(destination_url):
                raise CommandError(
                    "Source and destination must be different databases."
                )
            source_config = database_config_from_url(source_url)
            database_config_from_url(destination_url)
        except (ValueError, TypeError) as exc:
            raise CommandError(
                f"Source and destination database URLs must be valid ({exc})."
            ) from exc
        if source_config["ENGINE"] not in {
            "django.db.backends.postgresql",
            "django.db.backends.sqlite3",
        }:
            raise CommandError(
                "Driver anomaly backfill supports PostgreSQL or SQLite sources."
            )

        source_config = connections.databases["default"].copy() | source_config
        if source_config["ENGINE"] == "django.db.backends.sqlite3":
            source_config["OPTIONS"] = {}
        connections.databases["source"] = source_config
        try:
            rows = self._read_source()
            self._validate_rows(rows)
            self._validate_owners(rows)
            insertable = self._preflight(rows)
            if options["apply"]:
                with transaction.atomic(using="default"):
                    insertable = self._preflight(rows)
                    for start in range(0, len(insertable), 500):
                        DriverLocationAnomaly.objects.bulk_create(
                            [
                                DriverLocationAnomaly(
                                    **{field: row[field] for field in SOURCE_FIELDS},
                                    active_order_id=None,
                                    reviewed_by_user_id=None,
                                    reviewed_by_email="",
                                    reviewed_at=None,
                                )
                                for row in insertable[start : start + 500]
                            ],
                            batch_size=500,
                        )
                    self._verify_destination(rows)
            self._report(rows, insertable, options["apply"])
        finally:
            connections["source"].close()
            del connections["source"]
            connections.databases.pop("source", None)

    def _read_source(self):
        source = connections["source"]
        quote = source.ops.quote_name
        if source.vendor == "postgresql":
            reported = (
                f"ST_Y({quote('reported_location')}) AS reported_latitude, "
                f"ST_X({quote('reported_location')}) AS reported_longitude"
            )
            previous = (
                f"ST_Y({quote('previous_location')}) AS previous_latitude, "
                f"ST_X({quote('previous_location')}) AS previous_longitude"
            )
        else:
            reported = f"{quote('reported_latitude')}, {quote('reported_longitude')}"
            previous = f"{quote('previous_latitude')}, {quote('previous_longitude')}"
        columns = [
            f"{quote('id')} AS source_anomaly_id",
            f"{quote('driver_id')} AS source_owner_user_id",
            f"{quote('order_id')} AS source_order_id",
            f"{quote('dispute_id')} AS source_dispute_id",
            f"{quote('reviewed_by_id')} AS source_reviewer_user_id",
            quote("anomaly_type"),
            quote("severity"),
            quote("details"),
            reported,
            previous,
            quote("reported_speed_kmh"),
            quote("max_allowed_speed_kmh"),
            quote("distance_km"),
            quote("time_delta_seconds"),
            quote("is_reviewed"),
            quote("review_note"),
            quote("created_at"),
        ]
        try:
            with transaction.atomic(using="source"), source.cursor() as cursor:
                if source.vendor == "postgresql":
                    cursor.execute("SET TRANSACTION READ ONLY")
                else:
                    cursor.execute("PRAGMA query_only = ON")
                cursor.execute(
                    f"SELECT {', '.join(columns)} FROM {quote(SOURCE_TABLE)} "
                    f"ORDER BY {quote('id')}"
                )
                names = [column[0] for column in cursor.description]
                return [dict(zip(names, row)) for row in cursor.fetchall()]
        except Exception as exc:
            raise CommandError(
                "Could not read legacy driver location anomalies; verify the source "
                f"schema and read-only access ({type(exc).__name__})."
            ) from exc

    def _validate_rows(self, rows):
        seen_ids = set()
        for row in rows:
            try:
                row["source_anomaly_id"] = int(row["source_anomaly_id"])
                row["source_owner_user_id"] = int(row["source_owner_user_id"])
                row["owner_user_id"] = row["source_owner_user_id"]
                for field in (
                    "source_order_id",
                    "source_dispute_id",
                    "source_reviewer_user_id",
                ):
                    row[field] = int(row[field]) if row[field] is not None else None
                for field in (
                    "reported_latitude",
                    "reported_longitude",
                    "previous_latitude",
                    "previous_longitude",
                    "reported_speed_kmh",
                    "max_allowed_speed_kmh",
                    "distance_km",
                    "time_delta_seconds",
                ):
                    row[field] = float(row[field]) if row[field] is not None else None
                row["created_at"] = _as_datetime(row["created_at"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise CommandError(
                    "Legacy driver anomaly rows contain invalid values."
                ) from exc
            if (
                row["source_anomaly_id"] <= 0
                or row["source_owner_user_id"] <= 0
                or row["source_anomaly_id"] in seen_ids
                or any(
                    row[field] is not None and row[field] <= 0
                    for field in (
                        "source_order_id",
                        "source_dispute_id",
                        "source_reviewer_user_id",
                    )
                )
            ):
                raise CommandError(
                    f"Legacy driver anomaly {row['source_anomaly_id']} has invalid IDs."
                )
            if row["anomaly_type"] not in ANOMALY_TYPES:
                raise CommandError(
                    f"Legacy driver anomaly {row['source_anomaly_id']} has an unsupported type."
                )
            if row["severity"] not in SEVERITIES:
                raise CommandError(
                    f"Legacy driver anomaly {row['source_anomaly_id']} has an unsupported severity."
                )
            if not isinstance(row["is_reviewed"], (bool, int)) or row[
                "is_reviewed"
            ] not in (0, 1):
                raise CommandError(
                    f"Legacy driver anomaly {row['source_anomaly_id']} has an invalid review flag."
                )
            for latitude, longitude in (
                ("reported_latitude", "reported_longitude"),
                ("previous_latitude", "previous_longitude"),
            ):
                lat, lon = row[latitude], row[longitude]
                if (lat is None) != (lon is None):
                    raise CommandError(
                        f"Legacy driver anomaly {row['source_anomaly_id']} has incomplete coordinates."
                    )
                if lat is not None and (not -90 <= lat <= 90 or not -180 <= lon <= 180):
                    raise CommandError(
                        f"Legacy driver anomaly {row['source_anomaly_id']} has invalid coordinates."
                    )
            for field in (
                "reported_latitude",
                "reported_longitude",
                "previous_latitude",
                "previous_longitude",
                "reported_speed_kmh",
                "max_allowed_speed_kmh",
                "distance_km",
                "time_delta_seconds",
            ):
                if row[field] is not None and not math.isfinite(row[field]):
                    raise CommandError(
                        f"Legacy driver anomaly {row['source_anomaly_id']} has non-finite measurements."
                    )
            if isinstance(row["details"], str):
                try:
                    row["details"] = json.loads(row["details"])
                except json.JSONDecodeError as exc:
                    raise CommandError(
                        f"Legacy driver anomaly {row['source_anomaly_id']} has invalid details."
                    ) from exc
            row["is_reviewed"] = bool(row["is_reviewed"])
            row["review_note"] = row["review_note"] or ""
            row["reviewed_by_user_id"] = None
            row["active_order_id"] = None
            row["reviewed_at"] = None
            row["reviewed_by_email"] = ""
            row["checksum"] = _checksum(row)
            seen_ids.add(row["source_anomaly_id"])

    def _validate_owners(self, rows):
        owner_ids = {row["source_owner_user_id"] for row in rows}
        identity_mapping = dict(
            DriverProfile.objects.filter(source_user_id__in=owner_ids).values_list(
                "source_user_id", "owner_user_id"
            )
        )
        missing = owner_ids - identity_mapping.keys()
        if missing:
            raise CommandError(
                f"{len(missing)} anomaly owners have no verified destination identity mapping; "
                "reconcile driver profiles before applying."
            )
        for row in rows:
            row["owner_user_id"] = identity_mapping[row["source_owner_user_id"]]
            row["checksum"] = _checksum(row)

    def _destination_row(self, source_id):
        anomaly = DriverLocationAnomaly.objects.filter(
            source_anomaly_id=source_id
        ).first()
        if anomaly is None:
            return None
        row = {
            field: getattr(anomaly, field)
            for field in SOURCE_FIELDS
            if field != "source_anomaly_id"
        }
        row["source_anomaly_id"] = anomaly.source_anomaly_id
        row["owner_user_id"] = anomaly.owner_user_id
        row["checksum"] = _checksum(row)
        return row

    def _preflight(self, rows):
        insertable = []
        for row in rows:
            existing = self._destination_row(row["source_anomaly_id"])
            if existing is None:
                insertable.append(row)
                continue
            if any(existing[field] != row[field] for field in SOURCE_FIELDS) or (
                existing["checksum"] != row["checksum"]
            ):
                raise CommandError(
                    f"Driver anomaly {row['source_anomaly_id']} conflicts with "
                    "an existing destination record."
                )
        return insertable

    def _verify_destination(self, rows):
        if self._preflight(rows):
            raise CommandError("Post-write driver anomaly reconciliation failed.")

    def _report(self, rows, insertable, applied):
        self.stdout.write(
            f"Driver location anomaly backfill: {'APPLIED' if applied else 'DRY RUN'}"
        )
        self.stdout.write(
            f"source={len(rows)} already_matching={len(rows) - len(insertable)} "
            f"insertable={len(insertable)} "
            f"source_checksum={self._rows_checksum(rows)}"
        )
        if not applied:
            self.stdout.write(
                "No destination rows were written. Re-run with --apply after review."
            )

    @staticmethod
    def _rows_checksum(rows):
        digest = hmac.new(
            settings.DRIVER_FIELDS_ENCRYPTION_KEY.encode(),
            digestmod=hashlib.sha256,
        )
        for row in rows:
            digest.update(row["checksum"].encode())
            digest.update(b"\n")
        return digest.hexdigest()
