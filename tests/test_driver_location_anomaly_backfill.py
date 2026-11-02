import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
COMMON_PYTHON = SERVICE_ROOT.parent / "common-services" / "python"


class DriverLocationAnomalyBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source_database = self.root / "legacy.sqlite3"
        self.destination_database = self.root / "driver.sqlite3"
        self._create_source_database()
        migration = self._run_manage("migrate", "--noinput", "--verbosity", "0")
        self.assertEqual(migration.returncode, 0, migration.stdout + migration.stderr)
        profile = self._run_manage(
            "shell",
            "-c",
            "from driver_profiles.models import DriverProfile; "
            "DriverProfile.objects.create(pk=7, owner_user_id=7101, source_user_id=71)",
        )
        self.assertEqual(profile.returncode, 0, profile.stdout + profile.stderr)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _create_source_database(self):
        with closing(sqlite3.connect(self.source_database)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE fraud_driverlocationanomaly (
                    id INTEGER PRIMARY KEY,
                    driver_id INTEGER NOT NULL,
                    order_id INTEGER,
                    dispute_id INTEGER,
                    reviewed_by_id INTEGER,
                    anomaly_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    details TEXT NOT NULL,
                    reported_latitude REAL,
                    reported_longitude REAL,
                    previous_latitude REAL,
                    previous_longitude REAL,
                    reported_speed_kmh REAL,
                    max_allowed_speed_kmh REAL,
                    distance_km REAL,
                    time_delta_seconds REAL,
                    is_reviewed INTEGER NOT NULL,
                    review_note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO fraud_driverlocationanomaly (
                    id, driver_id, order_id, dispute_id, reviewed_by_id,
                    anomaly_type, severity, details, reported_latitude,
                    reported_longitude, previous_latitude, previous_longitude,
                    reported_speed_kmh, max_allowed_speed_kmh, distance_km,
                    time_delta_seconds, is_reviewed, review_note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        11,
                        71,
                        901,
                        801,
                        700,
                        "STATIONARY_SPOOFING",
                        "HIGH",
                        '{"source": "legacy"}',
                        -17.8,
                        31.0,
                        -17.9,
                        30.9,
                        100.0,
                        60.0,
                        8.2,
                        60.0,
                        1,
                        "Reviewed in legacy admin",
                        "2026-10-30T12:00:00+00:00",
                    ),
                    (
                        12,
                        71,
                        None,
                        None,
                        None,
                        "GEOBOUNDARY_EXIT",
                        "MEDIUM",
                        "{}",
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        0,
                        "",
                        "2026-10-31T12:00:00+00:00",
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
                "DJANGO_DEBUG": "true",
                "DJANGO_SECRET_KEY": "driver-anomaly-backfill-test-secret",
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

    def _count(self):
        with closing(sqlite3.connect(self.destination_database)) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM driver_profiles_driverlocationanomaly"
            ).fetchone()[0]

    def test_dry_run_apply_and_rerun_preserve_anomaly_lineage(self):
        dry_run = self._run_manage("backfill_driver_location_anomalies")
        self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
        self.assertIn("DRY RUN", dry_run.stdout)
        self.assertEqual(self._count(), 0)

        apply = self._run_manage("backfill_driver_location_anomalies", "--apply")
        self.assertEqual(apply.returncode, 0, apply.stdout + apply.stderr)
        self.assertIn("APPLIED", apply.stdout)
        self.assertEqual(self._count(), 2)

        rerun = self._run_manage("backfill_driver_location_anomalies", "--apply")
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertIn("already_matching=2", rerun.stdout)
        self.assertEqual(self._count(), 2)

        with closing(sqlite3.connect(self.destination_database)) as connection:
            rows = connection.execute(
                """
                SELECT source_anomaly_id, owner_user_id, active_order_id,
                    source_owner_user_id,
                    source_order_id, source_dispute_id, source_reviewer_user_id,
                    anomaly_type, severity, details, reported_latitude,
                    reported_longitude, previous_latitude, previous_longitude,
                    is_reviewed, review_note
                FROM driver_profiles_driverlocationanomaly
                ORDER BY source_anomaly_id
                """
            ).fetchall()
        self.assertEqual(
            rows[0][:9],
            (
                11,
                7101,
                None,
                71,
                901,
                801,
                700,
                "STATIONARY_SPOOFING",
                "HIGH",
            ),
        )
        self.assertEqual(
            rows[0][9:],
            (
                json.dumps({"source": "legacy"}),
                -17.8,
                31.0,
                -17.9,
                30.9,
                1,
                "Reviewed in legacy admin",
            ),
        )
        self.assertEqual(
            rows[1][0:9],
            (
                12,
                7101,
                None,
                71,
                None,
                None,
                None,
                "GEOBOUNDARY_EXIT",
                "MEDIUM",
            ),
        )
        self.assertEqual(rows[1][10:14], (None, None, None, None))


if __name__ == "__main__":
    unittest.main()
