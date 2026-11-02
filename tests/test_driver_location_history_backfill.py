import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from unittest import TestCase

SERVICE_ROOT = Path(__file__).resolve().parents[1]
MANAGE_PY = SERVICE_ROOT / 'manage.py'


def _sqlite_url(path):
    return f'sqlite:///{Path(path).as_posix()}'


class DriverLocationHistoryBackfillTests(TestCase):
    def setUp(self):
        suffix = uuid.uuid4().hex
        self.source_path = SERVICE_ROOT / 'tests' / f'location-history-source-{suffix}.sqlite3'
        self.destination_path = SERVICE_ROOT / 'tests' / f'location-history-destination-{suffix}.sqlite3'
        self.addCleanup(self._remove_databases)
        self._create_source_database()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                'DJANGO_DEBUG': '1',
                'DATABASE_URL': _sqlite_url(self.destination_path),
                'SOURCE_DATABASE_URL': _sqlite_url(self.source_path),
            }
        )
        self._run_manage('migrate', '--noinput')

    def _remove_databases(self):
        for path in (self.source_path, self.destination_path):
            if path.exists():
                path.unlink()

    def _create_source_database(self):
        connection = sqlite3.connect(self.source_path)
        try:
            connection.execute(
                '''
                CREATE TABLE orders_order (
                    id INTEGER PRIMARY KEY,
                    driver_id INTEGER NULL
                )
                '''
            )
            connection.execute(
                '''
                CREATE TABLE orders_driverlocationlog (
                    id INTEGER PRIMARY KEY,
                    order_id INTEGER NULL,
                    location_lat REAL NULL,
                    location_lng REAL NULL,
                    timestamp TEXT NOT NULL
                )
                '''
            )
            connection.executemany(
                'INSERT INTO orders_order (id, driver_id) VALUES (?, ?)',
                [(992, 4101), (993, None)],
            )
            connection.executemany(
                '''
                INSERT INTO orders_driverlocationlog
                    (id, order_id, location_lat, location_lng, timestamp)
                VALUES (?, ?, ?, ?, ?)
                ''',
                [
                    (301, 992, -17.8292, 31.0522, '2025-03-01T08:00:00+00:00'),
                    (302, 993, -17.83, 31.05, '2025-03-01T08:01:00+00:00'),
                    (303, None, None, None, '2025-03-01T08:02:00+00:00'),
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def _run_manage(self, *arguments, success=True):
        result = subprocess.run(
            [sys.executable, str(MANAGE_PY), *arguments],
            cwd=SERVICE_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if success and result.returncode:
            self.fail(f'Command failed:\n{result.stdout}\n{result.stderr}')
        if not success and result.returncode == 0:
            self.fail(f'Command unexpectedly succeeded:\n{result.stdout}')
        return result

    def _count_history(self):
        connection = sqlite3.connect(self.destination_path)
        try:
            return connection.execute(
                'SELECT COUNT(*) FROM driver_profiles_driverlocationhistory'
            ).fetchone()[0]
        finally:
            connection.close()

    def test_dry_run_and_replayable_apply_migrate_only_attributed_logs(self):
        dry_run = self._run_manage('backfill_driver_location_history')
        self.assertIn('Driver location history backfill: DRY RUN', dry_run.stdout)
        self.assertIn(
            'location_logs: source_mapped=1 source_total=3 '
            'already_matching=0 insertable=1 unattributed=2',
            dry_run.stdout,
        )
        self.assertIn('unattributed_log_ids_checksum=', dry_run.stdout)
        self.assertEqual(self._count_history(), 0)

        blocked_apply = self._run_manage(
            'backfill_driver_location_history',
            '--apply',
            success=False,
        )
        self.assertIn('2 legacy location logs have no driver owner', blocked_apply.stderr)
        self.assertEqual(self._count_history(), 0)

        connection = sqlite3.connect(self.source_path)
        try:
            connection.execute(
                'DELETE FROM orders_driverlocationlog WHERE id IN (302, 303)'
            )
            connection.commit()
        finally:
            connection.close()

        applied = self._run_manage('backfill_driver_location_history', '--apply')
        self.assertIn('Driver location history backfill: APPLIED', applied.stdout)
        self.assertEqual(self._count_history(), 1)

        repeated = self._run_manage('backfill_driver_location_history', '--apply')
        self.assertIn(
            'location_logs: source_mapped=1 source_total=1 '
            'already_matching=1 insertable=0 unattributed=0',
            repeated.stdout,
        )
        connection = sqlite3.connect(self.destination_path)
        try:
            record = connection.execute(
                '''
                SELECT source_log_id, owner_user_id, active_order_id, latitude, longitude
                FROM driver_profiles_driverlocationhistory
                '''
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(record, (301, 4101, 992, -17.8292, 31.0522))

    def test_conflicting_existing_source_id_aborts_without_changes(self):
        connection = sqlite3.connect(self.source_path)
        try:
            connection.execute(
                'DELETE FROM orders_driverlocationlog WHERE id IN (302, 303)'
            )
            connection.commit()
        finally:
            connection.close()

        connection = sqlite3.connect(self.destination_path)
        try:
            connection.execute(
                '''
                INSERT INTO driver_profiles_driverlocationhistory
                    (source_log_id, owner_user_id, active_order_id, latitude, longitude, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (301, 4101, 992, -17.0, 31.0522, '2025-03-01 08:00:00'),
            )
            connection.commit()
        finally:
            connection.close()

        result = self._run_manage(
            'backfill_driver_location_history',
            '--apply',
            success=False,
        )
        self.assertIn('Source location log 301 conflicts on: latitude', result.stderr)
        self.assertEqual(self._count_history(), 1)
