import hashlib
import hmac
import json
import os
import re
from datetime import date, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from bikadeli_common.settings import database_config_from_url
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from driver_profiles.kyc_scanning import (
    MalwareDetected,
    ScannerUnavailable,
    scan_kyc_upload,
)
from driver_profiles.kyc_storage import driver_kyc_storage
from driver_profiles.models import DriverKYCDocument, DriverProfile
from driver_profiles.source_media import SourceMediaReader

SOURCE_TABLE = "drivers_driverprofile"
SOURCE_DOCUMENT_TABLE = "drivers_driverdocument"
MAX_DOCUMENT_SIZE = 100 * 1024 * 1024
DOCUMENT_FIELDS = {
    "license_photo": ("LICENSE_PHOTO", None),
    "id_document": ("ID_DOCUMENT", None),
    "vehicle_photo": ("VEHICLE_PHOTO", "vehicle_photo_status"),
    "registration_book": ("REGISTRATION_BOOK", None),
    "selfie": ("SELFIE", None),
    "selfie_holding_id": ("SELFIE_HOLDING_ID", "selfie_holding_id_status"),
}
LEGACY_DOCUMENT_TYPE_MAP = {
    "ID_DOCUMENT": "LEGACY_ID_DOCUMENT",
    "LICENSE": "LEGACY_LICENSE",
    "VEHICLE_REGISTRATION": "LEGACY_VEHICLE_REGISTRATION",
    "INSURANCE": "LEGACY_INSURANCE",
    "SELFIE": "LEGACY_SELFIE",
    "SELFIE_HOLDING_ID": "LEGACY_SELFIE_HOLDING_ID",
    "POLICE_CLEARANCE": "LEGACY_POLICE_CLEARANCE",
    "MEDICAL_CERT": "LEGACY_MEDICAL_CERT",
    "OTHER": "LEGACY_OTHER",
}
DOCUMENT_METADATA_FIELDS = (
    "profile_id",
    "owner_user_id",
    "document_type",
    "source_document_id",
    "version",
    "is_current",
    "original_filename",
    "content_type",
    "file_size",
    "verification_status",
    "expires_at",
    "rejection_reason",
    "reviewed_at",
    "reviewed_by_user_id",
    "source_reviewer_user_id",
    "file_hash",
    "uploaded_at",
    "updated_at",
    "checksum",
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


def _content_checksum(content):
    return hmac.new(
        settings.DRIVER_FIELDS_ENCRYPTION_KEY.encode(),
        content,
        hashlib.sha256,
    ).hexdigest()


def _rows_checksum(rows):
    digest = hmac.new(
        settings.DRIVER_FIELDS_ENCRYPTION_KEY.encode(),
        digestmod=hashlib.sha256,
    )
    for row in sorted(
        rows,
        key=lambda item: (
            item["profile_id"],
            item["document_type"],
            item["version"],
            item["source_document_id"] or 0,
        ),
    ):
        serialized = {field: row[field] for field in DOCUMENT_METADATA_FIELDS}
        digest.update(
            json.dumps(
                serialized,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


class Command(BaseCommand):
    help = (
        "Dry-run or apply an insert-only copy of legacy driver KYC photos and "
        "versioned documents."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Insert missing KYC documents. Without this flag, no rows or files are written.",
        )

    def handle(self, *args, **options):
        source_url = os.getenv("SOURCE_DATABASE_URL", "").strip()
        destination_url = os.getenv("DATABASE_URL", "").strip()
        media_backend = os.getenv("SOURCE_MEDIA_BACKEND", "filesystem").strip().lower()
        source_media_root = os.getenv("SOURCE_MEDIA_ROOT", "").strip()
        source_id_media_root = os.getenv("SOURCE_ID_MEDIA_ROOT", "").strip()
        if not source_url or not destination_url:
            raise CommandError("Set SOURCE_DATABASE_URL and DATABASE_URL explicitly.")
        if media_backend not in {"filesystem", "s3"}:
            raise CommandError("SOURCE_MEDIA_BACKEND must be filesystem or s3.")
        if media_backend == "filesystem" and not source_media_root:
            raise CommandError(
                "Set SOURCE_MEDIA_ROOT when SOURCE_MEDIA_BACKEND=filesystem."
            )
        source_s3_bucket = os.getenv("SOURCE_S3_BUCKET", "").strip()
        source_id_s3_bucket = (
            os.getenv("SOURCE_ID_S3_BUCKET", "").strip() or source_s3_bucket
        )
        if media_backend == "s3" and not source_s3_bucket:
            raise CommandError("Set SOURCE_S3_BUCKET when SOURCE_MEDIA_BACKEND=s3.")
        try:
            if _database_identity(source_url) == _database_identity(destination_url):
                raise CommandError(
                    "Source and destination databases must be different."
                )
            source_config = database_config_from_url(source_url)
            database_config_from_url(destination_url)
            media_root = (
                Path(source_media_root).resolve(strict=True)
                if source_media_root
                else None
            )
            id_media_root = (
                Path(source_id_media_root).resolve(strict=True)
                if source_id_media_root
                else media_root
            )
            target_media_root = Path(driver_kyc_storage.location).resolve()
        except (OSError, ValueError, TypeError) as exc:
            raise CommandError(
                "Source database and media settings must be valid and accessible."
            ) from exc
        filesystem_roots = {}
        if media_backend == "filesystem":
            if not media_root.is_dir() or not id_media_root.is_dir():
                raise CommandError("Source KYC media roots must be directories.")
            filesystem_roots = {
                "default": media_root,
                "id_document": id_media_root,
            }
            for root in {media_root, id_media_root}:
                if (
                    root == target_media_root
                    or root.is_relative_to(target_media_root)
                    or target_media_root.is_relative_to(root)
                ):
                    raise CommandError(
                        "Source and destination KYC media roots must not overlap."
                    )
        if source_config["ENGINE"] not in {
            "django.db.backends.postgresql",
            "django.db.backends.sqlite3",
        }:
            raise CommandError(
                "Driver KYC backfill supports PostgreSQL or SQLite sources."
            )
        source_config = connections.databases["default"].copy() | source_config
        if source_config["ENGINE"] == "django.db.backends.sqlite3":
            source_config["OPTIONS"] = {}
        connections.databases["source"] = source_config
        self.source_media_reader = SourceMediaReader(
            media_backend,
            MAX_DOCUMENT_SIZE,
            filesystem_roots=filesystem_roots,
            s3_buckets={
                "default": source_s3_bucket,
                "id_document": source_id_s3_bucket,
            },
            region_name=os.getenv("SOURCE_S3_REGION", "").strip(),
            endpoint_url=os.getenv("SOURCE_S3_ENDPOINT_URL", "").strip(),
        )
        try:
            profiles = self._read_source_profiles()
            source_documents = self._read_source_documents(
                {profile["id"] for profile in profiles}
            )
            expected = self._build_source_documents(profiles)
            expected.extend(self._build_legacy_documents(source_documents, profiles))
            plans = self._preflight(expected)
            if options["apply"]:
                created_files = []
                try:
                    with transaction.atomic(using="default"):
                        plans = self._preflight(expected)
                        self._insert_missing(plans, created_files)
                        self._verify_destination(expected)
                except Exception:
                    for name in created_files:
                        driver_kyc_storage.delete(name)
                    raise
            self._report(expected, profiles, plans, options["apply"])
        finally:
            connections["source"].close()
            del connections["source"]
            connections.databases.pop("source", None)

    def _read_source_profiles(self):
        source = connections["source"]
        quote = source.ops.quote_name
        columns = ["id", "user_id", "created_at", "updated_at", *DOCUMENT_FIELDS]
        columns.extend(
            field_name
            for _, status_field in DOCUMENT_FIELDS.values()
            if status_field
            for field_name in (status_field,)
        )
        selected = ", ".join(
            f"{quote(column)} AS {quote(column)}" for column in columns
        )
        try:
            with transaction.atomic(using="source"), source.cursor() as cursor:
                if source.vendor == "postgresql":
                    cursor.execute("SET TRANSACTION READ ONLY")
                else:
                    cursor.execute("PRAGMA query_only = ON")
                cursor.execute(
                    f"SELECT {selected} FROM {quote(SOURCE_TABLE)} ORDER BY {quote('id')}"
                )
                names = [column[0] for column in cursor.description]
                profiles = [dict(zip(names, row)) for row in cursor.fetchall()]
        except Exception as exc:
            raise CommandError(
                "Could not read legacy driver profile KYC fields; verify the source "
                f"schema and read-only access ({type(exc).__name__})."
            ) from exc

        profile_ids = set()
        owners = set()
        for profile in profiles:
            try:
                profile["id"] = int(profile["id"])
                profile["user_id"] = int(profile["user_id"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise CommandError(
                    "Legacy driver profiles contain invalid IDs."
                ) from exc
            if profile["id"] <= 0 or profile["user_id"] <= 0:
                raise CommandError("Legacy driver profiles contain invalid IDs.")
            if profile["id"] in profile_ids or profile["user_id"] in owners:
                raise CommandError(
                    "Legacy driver profile IDs or owners are duplicated."
                )
            profile["created_at"] = self._parse_source_datetime(
                profile["created_at"], profile["id"], "profile creation timestamp"
            )
            profile["updated_at"] = self._parse_source_datetime(
                profile["updated_at"], profile["id"], "profile update timestamp"
            )
            profile_ids.add(profile["id"])
            owners.add(profile["user_id"])
            destination = (
                DriverProfile.objects.filter(pk=profile["id"])
                .values("owner_user_id", "source_user_id")
                .first()
            )
            if not destination or destination["source_user_id"] != profile["user_id"]:
                raise CommandError(
                    f"Driver profile {profile['id']} must be backfilled with a verified identity mapping first."
                )
            profile["destination_user_id"] = destination["owner_user_id"]
        return profiles

    def _read_source_documents(self, profile_ids):
        source = connections["source"]
        quote = source.ops.quote_name
        columns = [
            "id",
            "driver_id",
            "doc_type",
            "file",
            "file_hash",
            "version",
            "status",
            "expires_at",
            "rejection_reason",
            "reviewed_by_id",
            "reviewed_at",
            "is_current",
            "created_at",
            "updated_at",
        ]
        selected = ", ".join(
            f"{quote(column)} AS {quote(column)}" for column in columns
        )
        try:
            with transaction.atomic(using="source"), source.cursor() as cursor:
                if source.vendor == "postgresql":
                    cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    f"SELECT {selected} FROM {quote(SOURCE_DOCUMENT_TABLE)} "
                    f"ORDER BY {quote('id')}"
                )
                names = [column[0] for column in cursor.description]
                rows = [dict(zip(names, row)) for row in cursor.fetchall()]
        except Exception as exc:
            raise CommandError(
                "Could not read legacy DriverDocument history; verify the source "
                f"schema and read-only access ({type(exc).__name__})."
            ) from exc

        seen_ids = set()
        current_documents = set()
        for row in rows:
            try:
                row["id"] = int(row["id"])
                row["driver_id"] = int(row["driver_id"])
                row["version"] = int(row["version"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise CommandError(
                    "Legacy DriverDocument rows contain invalid IDs or versions."
                ) from exc
            if (
                row["id"] <= 0
                or row["driver_id"] not in profile_ids
                or row["version"] <= 0
                or row["id"] in seen_ids
            ):
                raise CommandError(
                    f"Legacy DriverDocument {row.get('id')} has invalid identity data."
                )
            if row["doc_type"] not in LEGACY_DOCUMENT_TYPE_MAP:
                raise CommandError(
                    f"Legacy DriverDocument {row['id']} has an unsupported type."
                )
            if row["status"] not in {
                "PENDING",
                "APPROVED",
                "REJECTED",
                "EXPIRED",
                "UNDER_REVIEW",
            }:
                raise CommandError(
                    f"Legacy DriverDocument {row['id']} has an unsupported status."
                )
            if not isinstance(row["is_current"], (bool, int)) or row[
                "is_current"
            ] not in (0, 1):
                raise CommandError(
                    f"Legacy DriverDocument {row['id']} has an invalid current flag."
                )
            row["is_current"] = bool(row["is_current"])
            current_key = (row["driver_id"], row["doc_type"])
            if row["is_current"] and current_key in current_documents:
                raise CommandError(
                    f"Legacy driver {row['driver_id']} has multiple current "
                    f"{row['doc_type']} documents."
                )
            if row["is_current"]:
                current_documents.add(current_key)
            row["expires_at"] = self._parse_source_date(
                row["expires_at"], row["id"], "expiry"
            )
            row["reviewed_at"] = self._parse_source_datetime(
                row["reviewed_at"], row["id"], "review timestamp"
            )
            row["created_at"] = self._parse_source_datetime(
                row["created_at"], row["id"], "creation timestamp"
            )
            row["updated_at"] = self._parse_source_datetime(
                row["updated_at"], row["id"], "update timestamp"
            )
            try:
                row["reviewed_by_id"] = (
                    int(row["reviewed_by_id"])
                    if row["reviewed_by_id"] is not None
                    else None
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise CommandError(
                    f"Legacy DriverDocument {row['id']} has an invalid reviewer ID."
                ) from exc
            if row["reviewed_by_id"] is not None and row["reviewed_by_id"] <= 0:
                raise CommandError(
                    f"Legacy DriverDocument {row['id']} has an invalid reviewer ID."
                )
            file_hash = (row["file_hash"] or "").lower()
            if file_hash and not re.fullmatch(r"[0-9a-f]{64}", file_hash):
                raise CommandError(
                    f"Legacy DriverDocument {row['id']} has an invalid file hash."
                )
            row["file_hash"] = file_hash
            seen_ids.add(row["id"])
        return rows

    @staticmethod
    def _parse_source_date(value, document_id, label):
        if value is None or (
            isinstance(value, date) and not isinstance(value, datetime)
        ):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise CommandError(
                f"Legacy DriverDocument {document_id} has an invalid {label}."
            ) from exc

    @staticmethod
    def _parse_source_datetime(value, document_id, label):
        if value is None:
            return value
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError as exc:
                raise CommandError(
                    f"Legacy DriverDocument {document_id} has an invalid {label}."
                ) from exc
        if timezone.is_naive(parsed) and timezone.is_aware(timezone.now()):
            parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
        return parsed

    def _build_source_documents(self, profiles):
        documents = []
        for profile in profiles:
            for field, (document_type, status_field) in DOCUMENT_FIELDS.items():
                filename = profile.get(field)
                if not filename:
                    continue
                try:
                    content, _ = self.source_media_reader.read(
                        filename,
                        field,
                        profile["id"],
                    )
                except CommandError:
                    raise
                except OSError as exc:
                    raise CommandError(
                        f"Driver profile {profile['id']} has an unreadable {field} file."
                    ) from exc
                if not content or len(content) > MAX_DOCUMENT_SIZE:
                    raise CommandError(
                        f"Driver profile {profile['id']} has an invalid-size {field} file."
                    )
                try:
                    image = Image.open(BytesIO(content))
                    image_format = image.format
                    if image.width * image.height > 20_000_000:
                        raise CommandError(
                            f"Driver profile {profile['id']} has an oversized {field} image."
                        )
                    image.verify()
                except CommandError:
                    raise
                except (
                    Image.DecompressionBombError,
                    UnidentifiedImageError,
                    OSError,
                    ValueError,
                ) as exc:
                    raise CommandError(
                        f"Driver profile {profile['id']} has an invalid {field} image."
                    ) from exc
                if image_format not in {"JPEG", "PNG", "WEBP"}:
                    raise CommandError(
                        f"Driver profile {profile['id']} has an unsupported {field} image format."
                    )
                try:
                    scan_kyc_upload(BytesIO(content))
                except MalwareDetected as exc:
                    raise CommandError(
                        f"Driver profile {profile['id']} has a {field} flagged by malware scanning."
                    ) from exc
                except ScannerUnavailable as exc:
                    raise CommandError(
                        "Driver KYC malware scanner is unavailable; backfill stopped."
                    ) from exc

                verification_status = (
                    profile[status_field] if status_field else "PENDING"
                )
                if verification_status not in {"PENDING", "APPROVED", "REJECTED"}:
                    raise CommandError(
                        f"Driver profile {profile['id']} has an unsupported {field} review status."
                    )
                original_filename = PurePosixPath(
                    str(filename).replace("\\", "/")
                ).name[:255]
                content_type = {
                    "JPEG": "image/jpeg",
                    "PNG": "image/png",
                    "WEBP": "image/webp",
                }[image_format]
                documents.append(
                    {
                        "profile_id": profile["id"],
                        "owner_user_id": profile["destination_user_id"],
                        "document_type": document_type,
                        "source_document_id": None,
                        "version": 1,
                        "is_current": True,
                        "original_filename": original_filename,
                        "content_type": content_type,
                        "file_size": len(content),
                        "verification_status": verification_status,
                        "expires_at": None,
                        "rejection_reason": "",
                        "reviewed_at": None,
                        "reviewed_by_user_id": None,
                        "source_reviewer_user_id": None,
                        "file_hash": hashlib.sha256(content).hexdigest(),
                        "checksum": _content_checksum(content),
                        "uploaded_at": profile["created_at"],
                        "updated_at": profile["updated_at"],
                        "source_field": field,
                        "source_filename": str(filename),
                    }
                )
        return documents

    def _build_legacy_documents(self, rows, profiles):
        profile_owners = {
            profile["id"]: profile["destination_user_id"] for profile in profiles
        }
        documents = []
        for row in rows:
            filename = row["file"] or ""
            try:
                content = (
                    self.source_media_reader.read(
                        filename,
                        "default",
                        row["driver_id"],
                    )[0]
                    if filename
                    else b""
                )
            except CommandError as exc:
                raise CommandError(
                    f"Legacy DriverDocument {row['id']} has an unreadable file."
                ) from exc
            if filename and (not content or len(content) > MAX_DOCUMENT_SIZE):
                raise CommandError(
                    f"Legacy DriverDocument {row['id']} has an invalid-size file."
                )
            source_hash = row["file_hash"]
            content_hash = hashlib.sha256(content).hexdigest() if content else ""
            if (
                source_hash
                and content
                and not hmac.compare_digest(
                    source_hash,
                    content_hash,
                )
            ):
                raise CommandError(
                    f"Legacy DriverDocument {row['id']} does not match its stored file hash."
                )
            if content:
                try:
                    scan_kyc_upload(BytesIO(content))
                except MalwareDetected as exc:
                    raise CommandError(
                        f"Legacy DriverDocument {row['id']} was flagged by malware scanning."
                    ) from exc
                except ScannerUnavailable as exc:
                    raise CommandError(
                        "Driver KYC malware scanner is unavailable; backfill stopped."
                    ) from exc
            documents.append(
                {
                    "profile_id": row["driver_id"],
                    "owner_user_id": profile_owners[row["driver_id"]],
                    "document_type": LEGACY_DOCUMENT_TYPE_MAP[row["doc_type"]],
                    "source_document_id": row["id"],
                    "version": row["version"],
                    "is_current": row["is_current"],
                    "original_filename": (
                        PurePosixPath(str(filename).replace("\\", "/")).name[:255]
                        if filename
                        else ""
                    ),
                    "content_type": (
                        "application/octet-stream"
                        if not content
                        else self._legacy_content_type(content)
                    ),
                    "file_size": len(content),
                    "verification_status": row["status"],
                    "expires_at": row["expires_at"],
                    "rejection_reason": row["rejection_reason"] or "",
                    "reviewed_at": row["reviewed_at"],
                    "reviewed_by_user_id": None,
                    "source_reviewer_user_id": row["reviewed_by_id"],
                    "file_hash": source_hash or content_hash,
                    "checksum": _content_checksum(content),
                    "source_filename": str(filename),
                    "source_field": "default",
                    "uploaded_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return documents

    @staticmethod
    def _legacy_content_type(content):
        if content.startswith(b"%PDF-"):
            return "application/pdf"
        try:
            image = Image.open(BytesIO(content))
            image_format = image.format
            image.verify()
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ):
            return "application/octet-stream"
        return {
            "JPEG": "image/jpeg",
            "PNG": "image/png",
            "WEBP": "image/webp",
        }.get(image_format, "application/octet-stream")

    def _destination_metadata(self, document):
        content = b""
        if document.file:
            try:
                with document.file.open("rb") as stored_file:
                    content = stored_file.read(MAX_DOCUMENT_SIZE + 1)
            except (OSError, ValueError) as exc:
                raise CommandError(
                    f"Destination KYC document {document.pk} could not be read."
                ) from exc
        if len(content) > MAX_DOCUMENT_SIZE:
            raise CommandError(
                f"Destination KYC document {document.pk} exceeds the supported size."
            )
        return {
            "profile_id": document.profile_id,
            "owner_user_id": document.profile.owner_user_id,
            "document_type": document.document_type,
            "source_document_id": document.source_document_id,
            "version": document.version,
            "is_current": document.is_current,
            "original_filename": document.original_filename,
            "content_type": document.content_type,
            "file_size": document.file_size,
            "verification_status": document.verification_status,
            "expires_at": document.expires_at,
            "rejection_reason": document.rejection_reason,
            "reviewed_at": document.reviewed_at,
            "reviewed_by_user_id": document.reviewed_by_user_id,
            "source_reviewer_user_id": document.source_reviewer_user_id,
            "file_hash": document.file_hash,
            "checksum": _content_checksum(content),
            "uploaded_at": document.uploaded_at,
            "updated_at": document.updated_at,
        }

    @staticmethod
    def _find_destination(source):
        query = DriverKYCDocument.objects.select_related("profile")
        if source["source_document_id"] is not None:
            return query.filter(source_document_id=source["source_document_id"]).first()
        return query.filter(
            profile_id=source["profile_id"],
            document_type=source["document_type"],
            version=source["version"],
        ).first()

    def _preflight(self, expected):
        plans = []
        for source in expected:
            document = self._find_destination(source)
            if document is None:
                plans.append(source)
                continue
            destination = self._destination_metadata(document)
            if any(
                destination[field] != source[field]
                for field in DOCUMENT_METADATA_FIELDS
            ):
                raise CommandError(
                    f"Driver profile {source['profile_id']} {source['document_type']} "
                    "conflicts with an existing destination document."
                )
        return plans

    def _insert_missing(self, plans, created_files):
        for source in plans:
            content = b""
            if source["source_filename"]:
                content, _ = self.source_media_reader.read(
                    source["source_filename"],
                    source["source_field"],
                    source["profile_id"],
                )
            if len(content) != source["file_size"] or not hmac.compare_digest(
                _content_checksum(content),
                source["checksum"],
            ):
                raise CommandError(
                    f"Driver profile {source['profile_id']} {source['document_type']} "
                    "changed after preflight."
                )
            if content:
                try:
                    scan_kyc_upload(BytesIO(content))
                except MalwareDetected as exc:
                    raise CommandError(
                        f"Driver profile {source['profile_id']} {source['document_type']} "
                        "was flagged during apply."
                    ) from exc
                except ScannerUnavailable as exc:
                    raise CommandError(
                        "Driver KYC malware scanner is unavailable; backfill stopped."
                    ) from exc

            document = DriverKYCDocument(
                profile_id=source["profile_id"],
                document_type=source["document_type"],
                source_document_id=source["source_document_id"],
                version=source["version"],
                is_current=source["is_current"],
                original_filename=source["original_filename"],
                content_type=source["content_type"],
                file_size=source["file_size"],
                verification_status=source["verification_status"],
                expires_at=source["expires_at"],
                rejection_reason=source["rejection_reason"],
                reviewed_at=source["reviewed_at"],
                reviewed_by_user_id=source["reviewed_by_user_id"],
                source_reviewer_user_id=source["source_reviewer_user_id"],
                file_hash=source["file_hash"],
                uploaded_at=source["uploaded_at"],
                updated_at=source["updated_at"],
            )
            if content:
                document.file.save(
                    source["original_filename"],
                    ContentFile(content),
                    save=False,
                )
                created_files.append(document.file.name)
            document.save()

    def _verify_destination(self, expected):
        for source in expected:
            document = self._find_destination(source)
            if document is None:
                raise CommandError(
                    f"Post-write KYC reconciliation failed for profile "
                    f"{source['profile_id']} {source['document_type']}."
                )
            destination = self._destination_metadata(document)
            if any(
                destination[field] != source[field]
                for field in DOCUMENT_METADATA_FIELDS
            ):
                raise CommandError(
                    f"Post-write KYC reconciliation failed for profile {source['profile_id']} "
                    f"{source['document_type']}."
                )

    def _report(self, expected, profiles, plans, applied):
        source_profiles = len(profiles)
        matching = len(expected) - len(plans)
        self.stdout.write(
            f"Driver profile KYC backfill: {'APPLIED' if applied else 'DRY RUN'}"
        )
        self.stdout.write(
            f"profiles={source_profiles} documents={len(expected)} "
            f"already_matching={matching} insertable={len(plans)} "
            f"source_checksum={_rows_checksum(expected)}"
        )
        if not applied:
            self.stdout.write(
                "No destination rows or files were written. Re-run with --apply "
                "after reviewing this report."
            )
