import base64
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet
from PIL import Image

SERVICE_ROOT = Path(__file__).resolve().parents[1]
COMMON_PYTHON = SERVICE_ROOT.parent / "common-services" / "python"
DJANGO_SECRET = "driver-profile-kyc-backfill-test-secret"


class DriverProfileKycBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source_database = self.root / "legacy.sqlite3"
        self.destination_database = self.root / "finance.sqlite3"
        self.source_media_root = self.root / "legacy-media"
        self.destination_media_root = self.root / "private-kyc"
        self.source_media_root.mkdir()
        self.destination_media_root.mkdir()
        self.image_path = self.source_media_root / "license.png"
        self.image_bytes = self._make_image(self.image_path)
        self.history_image_path = self.source_media_root / "history-v1.png"
        self.history_image_bytes = self._make_image(self.history_image_path)
        self._create_source_database()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _make_image(self, path):
        image = Image.new("RGB", (4, 4), color="blue")
        image.save(path, format="PNG")
        return path.read_bytes()

    def _create_source_database(self):
        with closing(sqlite3.connect(self.source_database)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE drivers_driverprofile (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    license_photo TEXT,
                    id_document TEXT,
                    vehicle_photo TEXT,
                    vehicle_photo_status TEXT,
                    registration_book TEXT,
                    selfie TEXT,
                    selfie_holding_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    selfie_holding_id_status TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO drivers_driverprofile (
                    id, user_id, license_photo, vehicle_photo,
                    vehicle_photo_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    7,
                    71,
                    "license.png",
                    "license.png",
                    "APPROVED",
                    "2026-10-30T12:00:00+00:00",
                    "2026-10-31T12:00:00+00:00",
                ),
            )
            connection.execute(
                """
                CREATE TABLE drivers_driverdocument (
                    id INTEGER PRIMARY KEY,
                    driver_id INTEGER NOT NULL,
                    doc_type TEXT NOT NULL,
                    file TEXT NOT NULL DEFAULT '',
                    file_hash TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    expires_at TEXT,
                    rejection_reason TEXT NOT NULL DEFAULT '',
                    reviewed_by_id INTEGER,
                    reviewed_at TEXT,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO drivers_driverdocument (
                    id, driver_id, doc_type, file, version, status,
                    expires_at, rejection_reason, reviewed_by_id, reviewed_at,
                    is_current, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        17,
                        7,
                        "LICENSE",
                        "history-v1.png",
                        1,
                        "REJECTED",
                        None,
                        "Image is not clear",
                        701,
                        "2026-10-29T12:00:00+00:00",
                        0,
                        "2026-10-28T12:00:00+00:00",
                        "2026-10-29T12:00:00+00:00",
                    ),
                    (
                        18,
                        7,
                        "LICENSE",
                        "history-v1.png",
                        2,
                        "APPROVED",
                        "2027-10-29",
                        "",
                        702,
                        "2026-10-30T12:00:00+00:00",
                        1,
                        "2026-10-29T12:00:00+00:00",
                        "2026-10-30T12:00:00+00:00",
                    ),
                    (
                        19,
                        7,
                        "INSURANCE",
                        "",
                        1,
                        "PENDING",
                        None,
                        "",
                        None,
                        None,
                        1,
                        "2026-10-29T12:00:00+00:00",
                        "2026-10-30T12:00:00+00:00",
                    ),
                ],
            )

    def _environment(self):
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(COMMON_PYTHON),
                "DATABASE_URL": self._database_url(self.destination_database),
                "SOURCE_DATABASE_URL": self._database_url(self.source_database),
                "SOURCE_MEDIA_ROOT": str(self.source_media_root),
                "KYC_MEDIA_ROOT": str(self.destination_media_root),
                "DJANGO_DEBUG": "true",
                "DJANGO_SECRET_KEY": DJANGO_SECRET,
                "DRIVER_KYC_SCANNER_HOST": "",
            }
        )
        return environment

    @staticmethod
    def _database_url(path):
        return f"sqlite:///{path.as_posix()}"

    def _run_manage(self, *arguments):
        return subprocess.run(
            [sys.executable, "manage.py", *arguments],
            cwd=SERVICE_ROOT,
            env=self._environment(),
            capture_output=True,
            check=False,
            text=True,
        )

    def _prepare_destination(self):
        migration = self._run_manage("migrate", "--noinput", "--verbosity", "0")
        self.assertEqual(migration.returncode, 0, migration.stdout + migration.stderr)
        profile = self._run_manage(
            "shell",
            "-c",
            "from driver_profiles.models import DriverProfile; "
            "DriverProfile.objects.create(pk=7, owner_user_id=7101, source_user_id=71)",
        )
        self.assertEqual(profile.returncode, 0, profile.stdout + profile.stderr)

    def test_dry_run_apply_and_rerun_reconcile_encrypted_files(self):
        self._prepare_destination()

        dry_run = self._run_manage("backfill_driver_profile_kyc")
        self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
        self.assertIn("DRY RUN", dry_run.stdout)
        self.assertEqual(self._document_count(), 0)
        self.assertEqual(list(self.destination_media_root.rglob("*")), [])

        apply = self._run_manage("backfill_driver_profile_kyc", "--apply")
        self.assertEqual(apply.returncode, 0, apply.stdout + apply.stderr)
        self.assertIn("APPLIED", apply.stdout)
        self.assertEqual(self._document_count(), 5)

        rerun = self._run_manage("backfill_driver_profile_kyc", "--apply")
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertIn("already_matching=5", rerun.stdout)
        self.assertEqual(self._document_count(), 5)

        with closing(sqlite3.connect(self.destination_database)) as connection:
            rows = connection.execute(
                """
                SELECT document_type, file, file_size, verification_status,
                    source_document_id, version, is_current, expires_at,
                    rejection_reason, reviewed_by_user_id,
                    source_reviewer_user_id
                FROM driver_profiles_driverkycdocument
                ORDER BY source_document_id, document_type
                """
            ).fetchall()
        self.assertEqual(
            [(row[0], row[2], row[3]) for row in rows],
            [
                ("LICENSE_PHOTO", len(self.image_bytes), "PENDING"),
                ("VEHICLE_PHOTO", len(self.image_bytes), "APPROVED"),
                ("LEGACY_LICENSE", len(self.history_image_bytes), "REJECTED"),
                ("LEGACY_LICENSE", len(self.history_image_bytes), "APPROVED"),
                ("LEGACY_INSURANCE", 0, "PENDING"),
            ],
        )
        history = [row for row in rows if row[4] is not None]
        self.assertEqual(
            [(row[4], row[5], row[6]) for row in history],
            [
                (17, 1, 0),
                (18, 2, 1),
                (19, 1, 1),
            ],
        )
        self.assertEqual(history[0][8:], ("Image is not clear", None, 701))
        self.assertEqual(history[1][7], "2027-10-29")
        encryption_key = base64.urlsafe_b64encode(
            hashlib.sha256(DJANGO_SECRET.encode()).digest()
        )
        for row in rows:
            name = row[1]
            if not name:
                continue
            stored_bytes = (self.destination_media_root / name).read_bytes()
            plaintext = Fernet(encryption_key).decrypt(stored_bytes)
            self.assertNotEqual(stored_bytes, plaintext)
            self.assertIn(plaintext, {self.image_bytes, self.history_image_bytes})

    def test_unsafe_source_media_path_is_rejected_without_writes(self):
        self._prepare_destination()
        with closing(sqlite3.connect(self.source_database)) as connection, connection:
            connection.execute(
                "UPDATE drivers_driverprofile SET license_photo = ? WHERE id = ?",
                ("../outside.png", 7),
            )

        result = self._run_manage("backfill_driver_profile_kyc", "--apply")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe license_photo media path", result.stderr)
        self.assertEqual(self._document_count(), 0)
        self.assertEqual(list(self.destination_media_root.rglob("*")), [])

    def _document_count(self):
        with closing(sqlite3.connect(self.destination_database)) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM driver_profiles_driverkycdocument"
            ).fetchone()[0]


if __name__ == "__main__":
    unittest.main()
