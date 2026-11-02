import csv
import hashlib
import hmac
import json
import os
from datetime import date, datetime, time
from datetime import timezone as datetime_timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from bikadeli_common.settings import database_config_from_url
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connections, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from driver_profiles.models import DriverProfile

FIELDS = (
    "id",
    "owner_user_id",
    "source_user_id",
    "vehicle_type",
    "vehicle_registration",
    "license_number",
    "verification_status",
    "is_available",
    "service_radius_km",
    "current_order_id",
    "source_current_order_id",
    "created_at",
    "updated_at",
)
SOURCE_TABLE = "drivers_driverprofile"


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


def _canonical(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, float):
        return format(value, ".15g")
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _checksum(rows):
    digest = hmac.new(
        settings.DRIVER_FIELDS_ENCRYPTION_KEY.encode(),
        digestmod=hashlib.sha256,
    )
    for row in sorted(rows, key=lambda item: item["id"]):
        serialized = {field: _canonical(row.get(field)) for field in FIELDS}
        digest.update(
            json.dumps(serialized, sort_keys=True, separators=(",", ":")).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


class Command(BaseCommand):
    help = "Dry-run or apply an insert-only copy of driver profiles while preserving profile IDs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Insert missing rows into this service database. Without this flag, no rows are written.",
        )

    def handle(self, *args, **options):
        source_url = os.getenv("SOURCE_DATABASE_URL", "").strip()
        destination_url = os.getenv("DATABASE_URL", "").strip()
        if not source_url or not destination_url:
            raise CommandError(
                "Set both SOURCE_DATABASE_URL and DATABASE_URL explicitly."
            )
        try:
            source_identity = _database_identity(source_url)
            destination_identity = _database_identity(destination_url)
            source_config = database_config_from_url(source_url)
            database_config_from_url(destination_url)
        except (ValueError, TypeError) as exc:
            raise CommandError(
                f"SOURCE_DATABASE_URL and DATABASE_URL must be valid database URLs ({exc})."
            ) from exc
        if source_identity == destination_identity:
            raise CommandError("Source and destination must be different databases.")
        if source_config["ENGINE"] not in {
            "django.db.backends.sqlite3",
            "django.db.backends.postgresql",
        }:
            raise CommandError(
                "Driver profile backfill supports PostgreSQL or SQLite sources."
            )

        source_config = connections.databases["default"].copy() | source_config
        if source_config["ENGINE"] == "django.db.backends.sqlite3":
            source_config["OPTIONS"] = {}
        connections.databases["source"] = source_config
        try:
            expected = self._read_source()
            identity_mapping = self._read_identity_mapping()
            self._apply_identity_mapping(expected, identity_mapping)
            self._validate_source(expected)
            plans = self._preflight(expected)

            if options["apply"]:
                with transaction.atomic(using="default"):
                    plans = self._preflight(expected)
                    self._insert_missing(plans)
                    self._verify_destination(expected)
            actual = self._read_destination()
            self._report(expected, actual, plans, options["apply"])
        finally:
            connections["source"].close()
            del connections["source"]
            connections.databases.pop("source", None)

    def _read_identity_mapping(self):
        mapping_path = os.getenv("IDENTITY_MAPPING_CSV", "").strip()
        if not mapping_path:
            raise CommandError(
                "Set IDENTITY_MAPPING_CSV to a reviewed identity crosswalk."
            )
        try:
            with Path(mapping_path).open(
                encoding="utf-8-sig", newline=""
            ) as mapping_file:
                reader = csv.DictReader(mapping_file)
                if reader.fieldnames != ["source_user_id", "destination_user_id"]:
                    raise CommandError(
                        "Identity mapping CSV headers must be source_user_id,destination_user_id."
                    )
                mapping = {}
                destinations = set()
                for line_number, row in enumerate(reader, start=2):
                    try:
                        source_user_id = int(row["source_user_id"])
                        destination_user_id = int(row["destination_user_id"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise CommandError(
                            f"Identity mapping CSV has invalid IDs on line {line_number}."
                        ) from exc
                    if (
                        source_user_id < 1
                        or destination_user_id < 1
                        or source_user_id in mapping
                        or destination_user_id in destinations
                    ):
                        raise CommandError(
                            f"Identity mapping CSV has invalid or duplicate IDs on line {line_number}."
                        )
                    mapping[source_user_id] = destination_user_id
                    destinations.add(destination_user_id)
        except (OSError, UnicodeError) as exc:
            raise CommandError("Could not read IDENTITY_MAPPING_CSV.") from exc
        return mapping

    def _apply_identity_mapping(self, rows, mapping):
        source_ids = {row["owner_user_id"] for row in rows}
        if source_ids != set(mapping):
            raise CommandError(
                "Identity mapping CSV must map every source driver exactly once and contain no extra IDs."
            )
        destination_ids = set(mapping.values())
        service_url = os.getenv("IDENTITY_SERVICE_URL", "").strip().rstrip("/")
        lookup_token = os.getenv("DEMAND_SYNC_TOKEN", "").strip()
        if not service_url or not lookup_token:
            raise CommandError(
                "Set IDENTITY_SERVICE_URL and DEMAND_SYNC_TOKEN to verify mapped active drivers."
            )
        active_ids = set()
        requested_ids = sorted(destination_ids)
        try:
            for offset in range(0, len(requested_ids), 1000):
                requested_batch = requested_ids[offset : offset + 1000]
                response = httpx.post(
                    f"{service_url}/internal/v1/identity/active-users/",
                    headers={"X-Demand-Sync-Token": lookup_token},
                    json={"user_ids": requested_batch},
                    timeout=10,
                )
                response.raise_for_status()
                payload = response.json()
                batch_ids = (
                    payload.get("active_user_ids")
                    if isinstance(payload, dict)
                    else None
                )
                if not isinstance(batch_ids, list) or any(
                    type(user_id) is not int or user_id < 1 for user_id in batch_ids
                ):
                    raise CommandError(
                        "Identity service returned an invalid active-user response."
                    )
                active_ids.update(batch_ids)
        except httpx.HTTPError as exc:
            raise CommandError(
                f"Could not verify destination identities with identity-service ({type(exc).__name__})."
            ) from exc
        except ValueError as exc:
            raise CommandError("Identity service returned invalid JSON.") from exc
        if active_ids != destination_ids:
            raise CommandError(
                f"{len(destination_ids - active_ids)} mapped destination identities are not active drivers."
            )
        for row in rows:
            row["source_user_id"] = row["owner_user_id"]
            row["owner_user_id"] = mapping[row["source_user_id"]]
            row["source_current_order_id"] = row["current_order_id"]
            row["current_order_id"] = None

    def _read_source(self):
        source_connection = connections["source"]
        try:
            with (
                transaction.atomic(using="source"),
                source_connection.cursor() as cursor,
            ):
                if source_connection.vendor == "postgresql":
                    cursor.execute("SET TRANSACTION READ ONLY")
                elif source_connection.vendor == "sqlite":
                    cursor.execute("PRAGMA query_only = ON")
                else:
                    raise CommandError(
                        "Driver profile backfill supports PostgreSQL or SQLite sources."
                    )

                table = source_connection.ops.quote_name(SOURCE_TABLE)
                selected = ", ".join(
                    f"{source_connection.ops.quote_name(column)} AS "
                    f"{source_connection.ops.quote_name(alias)}"
                    for alias, column in (
                        ("id", "id"),
                        ("owner_user_id", "user_id"),
                        ("source_user_id", "user_id"),
                        ("vehicle_type", "vehicle_type"),
                        ("vehicle_registration", "vehicle_registration"),
                        ("license_number", "license_number"),
                        ("verification_status", "verification_status"),
                        ("is_available", "is_available"),
                        ("service_radius_km", "service_radius_km"),
                        ("current_order_id", "current_order_id"),
                        ("source_current_order_id", "current_order_id"),
                        ("created_at", "created_at"),
                        ("updated_at", "updated_at"),
                    )
                )
                cursor.execute(f"SELECT {selected} FROM {table}")
                names = [column[0] for column in cursor.description]
                rows = [dict(zip(names, row)) for row in cursor.fetchall()]
            return rows
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(
                f"Could not read {SOURCE_TABLE}; verify the source schema and database access "
                f"({type(exc).__name__}: {exc})."
            ) from exc

    def _validate_source(self, expected):
        profile_ids = set()
        owners = set()
        for row in expected:
            if not row["id"] or not row["owner_user_id"]:
                raise CommandError(
                    "Driver profiles must have valid nonzero profile and owner IDs."
                )
            if row["id"] in profile_ids:
                raise CommandError(f"Duplicate source driver profile ID {row['id']}.")
            if row["owner_user_id"] in owners:
                raise CommandError(
                    f"Duplicate source driver profile for user ID {row['owner_user_id']}."
                )
            profile_ids.add(row["id"])
            owners.add(row["owner_user_id"])
            if row["vehicle_type"] not in dict(DriverProfile.VEHICLE_TYPES):
                raise CommandError(
                    f"Driver profile {row['id']} has unsupported vehicle type."
                )
            if row["verification_status"] not in dict(
                DriverProfile.VERIFICATION_STATUSES
            ):
                raise CommandError(
                    f"Driver profile {row['id']} has unsupported verification status."
                )
            row["is_available"] = bool(row["is_available"])
            try:
                row["service_radius_km"] = int(row["service_radius_km"])
            except (TypeError, ValueError) as exc:
                raise CommandError(
                    f"Driver profile {row['id']} has invalid service radius."
                ) from exc
            if row["service_radius_km"] < 1:
                raise CommandError(
                    f"Driver profile {row['id']} has invalid service radius."
                )
            if row["current_order_id"] is not None:
                row["current_order_id"] = int(row["current_order_id"])
            if row["source_current_order_id"] is not None:
                row["source_current_order_id"] = int(row["source_current_order_id"])
            for field in ("created_at", "updated_at"):
                if isinstance(row[field], str):
                    row[field] = parse_datetime(row[field])
                    if row[field] is None:
                        raise CommandError(
                            f"Driver profile {row['id']} has invalid {field}."
                        )
                if row[field] is None:
                    raise CommandError(
                        f"Driver profile {row['id']} is missing required {field}."
                    )
                if row[field] is not None and timezone.is_naive(row[field]):
                    row[field] = timezone.make_aware(row[field], datetime_timezone.utc)

    def _read_destination(self):
        return [
            {field: getattr(row, field) for field in FIELDS}
            for row in DriverProfile.objects.order_by("id")
        ]

    def _preflight(self, expected):
        actual = self._read_destination()
        actual_by_id = {row["id"]: row for row in actual}
        owners = {row["owner_user_id"]: row["id"] for row in actual}
        missing = []
        for source_row in expected:
            destination_row = actual_by_id.get(source_row["id"])
            if destination_row is None:
                existing_id = owners.get(source_row["owner_user_id"])
                if existing_id is not None:
                    raise CommandError(
                        f"Identity user ID {source_row['owner_user_id']} already belongs to destination "
                        f"driver profile {existing_id}, not source profile {source_row['id']}."
                    )
                missing.append(source_row)
                continue
            conflicts = [
                field
                for field in FIELDS
                if _canonical(destination_row.get(field))
                != _canonical(source_row.get(field))
            ]
            if conflicts:
                raise CommandError(
                    f"Driver profile ID {source_row['id']} conflicts on: {', '.join(conflicts)}."
                )
        return missing

    def _insert_missing(self, rows):
        for row in rows:
            DriverProfile.objects.create(**row)
        for statement in connections["default"].ops.sequence_reset_sql(
            no_style(), [DriverProfile]
        ):
            with connections["default"].cursor() as cursor:
                cursor.execute(statement)

    def _verify_destination(self, expected):
        actual_by_id = {row["id"]: row for row in self._read_destination()}
        for source_row in expected:
            destination_row = actual_by_id.get(source_row["id"])
            if destination_row is None or any(
                _canonical(destination_row.get(field))
                != _canonical(source_row.get(field))
                for field in FIELDS
            ):
                raise CommandError(
                    f"Post-write reconciliation failed for driver profile ID {source_row['id']}."
                )

    def _report(self, expected, actual, plans, applied):
        actual_by_id = {row["id"]: row for row in actual}
        matching = [
            actual_by_id[row["id"]] for row in expected if row["id"] in actual_by_id
        ]
        self.stdout.write(
            "Driver profile backfill: {}".format("APPLIED" if applied else "DRY RUN")
        )
        self.stdout.write(
            f"profiles: source={len(expected)} already_matching={len(expected) - len(plans)} insertable={len(plans)} source_checksum={_checksum(expected)} "
            f"destination_matching_checksum={_checksum(matching)}"
        )
        if not applied:
            self.stdout.write(
                "No destination rows were written. Re-run with --apply after reviewing this report."
            )
