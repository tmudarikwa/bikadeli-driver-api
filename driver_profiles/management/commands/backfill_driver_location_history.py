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

from driver_profiles.models import DriverLocationHistory

SOURCE_LOG_TABLE = "orders_driverlocationlog"
SOURCE_ORDER_TABLE = "orders_order"
LOCATION_FIELDS = (
    "source_log_id",
    "owner_user_id",
    "active_order_id",
    "latitude",
    "longitude",
    "recorded_at",
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
    return (scheme, (parsed.hostname or "").lower(), parsed.port or 5432, unquote(parsed.path))


def _as_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = parse_datetime(value)
    else:
        parsed = None
    if parsed is None:
        raise ValueError("invalid timestamp")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


class Command(BaseCommand):
    help = "Dry-run or apply an idempotent backfill of order-linked driver locations."

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
            raise CommandError("Set both SOURCE_DATABASE_URL and DATABASE_URL explicitly.")
        try:
            if _database_identity(source_url) == _database_identity(destination_url):
                raise CommandError("Source and destination must be different databases.")
            source_config = database_config_from_url(source_url)
            database_config_from_url(destination_url)
        except (ValueError, TypeError) as exc:
            raise CommandError(
                f"SOURCE_DATABASE_URL and DATABASE_URL must be valid database URLs ({exc})."
            ) from exc
        if source_config["ENGINE"] not in {
            "django.db.backends.postgresql",
            "django.db.backends.sqlite3",
        }:
            raise CommandError("Driver location backfill supports PostgreSQL or SQLite sources.")

        source_config = connections.databases["default"].copy() | source_config
        if source_config["ENGINE"] == "django.db.backends.sqlite3":
            source_config["OPTIONS"] = {}
        connections.databases["source"] = source_config
        try:
            source_rows, unattributed_ids = self._read_source()
            self._validate_source(source_rows)
            if options["apply"] and unattributed_ids:
                raise CommandError(
                    f"{len(unattributed_ids)} legacy location logs have no driver owner; "
                    "reconcile them before applying the backfill."
                )
            existing, insertable = self._preflight(source_rows)
            if options["apply"]:
                with transaction.atomic(using="default"):
                    existing, insertable = self._preflight(source_rows)
                    for start in range(0, len(insertable), 500):
                        DriverLocationHistory.objects.bulk_create(
                            [
                                DriverLocationHistory(**row)
                                for row in insertable[start : start + 500]
                            ],
                            batch_size=500,
                        )
                    self._verify_destination(source_rows)
            self._report(
                source_rows,
                unattributed_ids,
                existing,
                insertable,
                options["apply"],
            )
        finally:
            connections["source"].close()
            del connections["source"]
            connections.databases.pop("source", None)

    def _read_source(self):
        source = connections["source"]
        quote = source.ops.quote_name
        if source.vendor == "postgresql":
            location_select = (
                f"ST_Y(logs.{quote('location')}) AS latitude, "
                f"ST_X(logs.{quote('location')}) AS longitude"
            )
        elif source.vendor == "sqlite":
            location_select = (
                f"logs.{quote('location_lat')} AS latitude, "
                f"logs.{quote('location_lng')} AS longitude"
            )
        else:
            raise CommandError("Driver location backfill supports PostgreSQL or SQLite sources.")

        try:
            with transaction.atomic(using="source"), source.cursor() as cursor:
                if source.vendor == "postgresql":
                    cursor.execute("SET TRANSACTION READ ONLY")
                else:
                    cursor.execute("PRAGMA query_only = ON")
                cursor.execute(
                    f"""
                    SELECT logs.{quote('id')} AS source_log_id,
                           orders.{quote('driver_id')} AS owner_user_id,
                           logs.{quote('order_id')} AS active_order_id,
                           {location_select},
                           logs.{quote('timestamp')} AS recorded_at
                    FROM {quote(SOURCE_LOG_TABLE)} AS logs
                    LEFT JOIN {quote(SOURCE_ORDER_TABLE)} AS orders
                        ON orders.{quote('id')} = logs.{quote('order_id')}
                    ORDER BY logs.{quote('id')}
                    """
                )
                names = [column[0] for column in cursor.description]
                rows = [dict(zip(names, row)) for row in cursor.fetchall()]
                unattributed_ids = [
                    row["source_log_id"]
                    for row in rows
                    if row["owner_user_id"] is None
                ]
                return (
                    [row for row in rows if row["owner_user_id"] is not None],
                    unattributed_ids,
                )
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(
                "Could not read the legacy driver location logs; verify the source schema "
                f"and read-only database access ({type(exc).__name__}: {exc})."
            ) from exc

    def _validate_source(self, rows):
        source_ids = set()
        for row in rows:
            try:
                row["source_log_id"] = int(row["source_log_id"])
                row["owner_user_id"] = int(row["owner_user_id"])
                row["active_order_id"] = int(row["active_order_id"])
                row["latitude"] = float(row["latitude"])
                row["longitude"] = float(row["longitude"])
                row["recorded_at"] = _as_datetime(row["recorded_at"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise CommandError(
                    "Legacy driver location rows contain invalid fields."
                ) from exc
            if (
                row["source_log_id"] < 1
                or row["owner_user_id"] < 1
                or row["active_order_id"] < 1
                or not math.isfinite(row["latitude"])
                or not math.isfinite(row["longitude"])
                or not -90 <= row["latitude"] <= 90
                or not -180 <= row["longitude"] <= 180
            ):
                raise CommandError(
                    f"Legacy driver location row {row['source_log_id']} has invalid identifiers or coordinates."
                )
            if row["source_log_id"] in source_ids:
                raise CommandError(
                    f"Duplicate source driver location log ID {row['source_log_id']}."
                )
            source_ids.add(row["source_log_id"])

    def _preflight(self, source_rows):
        ids = [row["source_log_id"] for row in source_rows]
        existing_by_source = {
            row.source_log_id: row
            for row in DriverLocationHistory.objects.filter(source_log_id__in=ids)
        }
        existing = []
        insertable = []
        for row in source_rows:
            current = existing_by_source.get(row["source_log_id"])
            if current is None:
                insertable.append(row)
                continue
            fields = (
                "owner_user_id",
                "active_order_id",
                "latitude",
                "longitude",
                "recorded_at",
            )
            conflicts = [
                field
                for field in fields
                if getattr(current, field) != row[field]
            ]
            if conflicts:
                raise CommandError(
                    f"Source location log {row['source_log_id']} conflicts on: "
                    f"{', '.join(conflicts)}."
                )
            existing.append(row)
        return existing, insertable

    def _verify_destination(self, source_rows):
        actual = {
            row.source_log_id: row
            for row in DriverLocationHistory.objects.filter(
                source_log_id__in=[item["source_log_id"] for item in source_rows]
            )
        }
        if len(actual) != len(source_rows):
            raise CommandError("Destination verification failed: location log counts differ.")
        for expected in source_rows:
            current = actual.get(expected["source_log_id"])
            if current is None or any(
                getattr(current, field) != expected[field]
                for field in LOCATION_FIELDS[1:]
            ):
                raise CommandError(
                    f"Destination verification failed for source location log "
                    f"{expected['source_log_id']}."
                )

    def _checksum(self, rows):
        digest = hmac.new(
            settings.SECRET_KEY.encode(),
            digestmod=hashlib.sha256,
        )
        for row in rows:
            normalized = {
                field: (
                    row[field].isoformat()
                    if isinstance(row[field], datetime)
                    else row[field]
                )
                for field in LOCATION_FIELDS
            }
            digest.update(
                json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
            )
            digest.update(b"\n")
        return digest.hexdigest()

    def _report(self, source_rows, unattributed_ids, existing, insertable, applied):
        mode = "APPLIED" if applied else "DRY RUN"
        self.stdout.write(f"Driver location history backfill: {mode}")
        self.stdout.write(
            f"location_logs: source_mapped={len(source_rows)} "
            f"source_total={len(source_rows) + len(unattributed_ids)} "
            f"already_matching={len(existing)} "
            f"insertable={len(insertable)} unattributed={len(unattributed_ids)} "
            f"source_checksum={self._checksum(source_rows)}"
        )
        if unattributed_ids:
            self.stdout.write(
                f"unattributed_log_ids_checksum={self._checksum_ids(unattributed_ids)}"
            )

    def _checksum_ids(self, source_ids):
        digest = hmac.new(
            settings.SECRET_KEY.encode(),
            digestmod=hashlib.sha256,
        )
        for source_id in sorted(source_ids):
            digest.update(f"{source_id}\n".encode())
        return digest.hexdigest()
