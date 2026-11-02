import json
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import TestCase

SERVICE_ROOT = Path(__file__).resolve().parents[1]
MANAGE_PY = SERVICE_ROOT / "manage.py"


def _sqlite_url(path):
    return f"sqlite:///{Path(path).as_posix()}"


class IdentityLookupHandler(BaseHTTPRequestHandler):
    active_user_ids: ClassVar[set[int]] = {9101}

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        requested = json.loads(body)["user_ids"]
        active = sorted(set(requested) & self.active_user_ids)
        response = json.dumps({"active_user_ids": active}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        return


class DriverProfileBackfillTests(TestCase):
    def setUp(self):
        suffix = uuid.uuid4().hex
        self.source_path = SERVICE_ROOT / "tests" / f"backfill-source-{suffix}.sqlite3"
        self.destination_path = (
            SERVICE_ROOT / "tests" / f"backfill-destination-{suffix}.sqlite3"
        )
        self.mapping_path = SERVICE_ROOT / "tests" / f"identity-map-{suffix}.csv"
        self.mapping_path.write_text(
            "source_user_id,destination_user_id\n4101,9101\n",
            encoding="utf-8",
        )
        self.addCleanup(self.mapping_path.unlink, missing_ok=True)
        self.identity_server = ThreadingHTTPServer(
            ("127.0.0.1", 0), IdentityLookupHandler
        )
        self.identity_thread = threading.Thread(
            target=self.identity_server.serve_forever,
            daemon=True,
        )
        self.identity_thread.start()
        self.addCleanup(self._stop_identity_server)
        self.addCleanup(self._remove_databases)
        self._create_source_database()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "DJANGO_DEBUG": "1",
                "DATABASE_URL": _sqlite_url(self.destination_path),
                "SOURCE_DATABASE_URL": _sqlite_url(self.source_path),
                "IDENTITY_MAPPING_CSV": str(self.mapping_path),
                "IDENTITY_SERVICE_URL": f"http://127.0.0.1:{self.identity_server.server_port}",
                "DEMAND_SYNC_TOKEN": "driver-profile-backfill-test-token",
            }
        )
        self._run_manage("migrate", "--noinput")

    def _stop_identity_server(self):
        self.identity_server.shutdown()
        self.identity_server.server_close()
        self.identity_thread.join(timeout=2)

    def _remove_databases(self):
        for path in (self.source_path, self.destination_path):
            if path.exists():
                path.unlink()

    def _create_source_database(self):
        connection = sqlite3.connect(self.source_path)
        try:
            connection.execute(
                """
                CREATE TABLE drivers_driverprofile (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    vehicle_type VARCHAR(20) NOT NULL,
                    vehicle_registration VARCHAR(50) NOT NULL,
                    license_number VARCHAR(50) NOT NULL DEFAULT '',
                    verification_status VARCHAR(20) NOT NULL,
                    is_available INTEGER NOT NULL,
                    service_radius_km INTEGER NOT NULL,
                    current_order_id INTEGER NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO drivers_driverprofile
                    (id, user_id, vehicle_type, vehicle_registration, license_number, verification_status,
                     is_available, service_radius_km, current_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    81,
                    4101,
                    "MOTORCYCLE",
                    "ZX-42",
                    "DL-98765",
                    "VERIFIED",
                    1,
                    18,
                    992,
                    "2025-03-01T08:00:00+00:00",
                    "2025-03-02T08:00:00+00:00",
                ),
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
            self.fail(f"Command failed:\n{result.stdout}\n{result.stderr}")
        if not success and result.returncode == 0:
            self.fail(f"Command unexpectedly succeeded:\n{result.stdout}")
        return result

    def _count_profiles(self):
        connection = sqlite3.connect(self.destination_path)
        try:
            return connection.execute(
                "SELECT COUNT(*) FROM driver_profiles_driverprofile"
            ).fetchone()[0]
        finally:
            connection.close()

    def test_dry_run_and_apply_preserve_profile_id_and_report_checksums(self):
        dry_run = self._run_manage("backfill_driver_profiles")
        self.assertIn("Driver profile backfill: DRY RUN", dry_run.stdout)
        self.assertIn(
            "profiles: source=1 already_matching=0 insertable=1", dry_run.stdout
        )
        self.assertIn("source_checksum=", dry_run.stdout)
        self.assertIn("destination_matching_checksum=", dry_run.stdout)
        self.assertEqual(self._count_profiles(), 0)

        applied = self._run_manage("backfill_driver_profiles", "--apply")
        self.assertIn("Driver profile backfill: APPLIED", applied.stdout)
        self.assertEqual(self._count_profiles(), 1)

        repeated = self._run_manage("backfill_driver_profiles", "--apply")
        self.assertIn(
            "profiles: source=1 already_matching=1 insertable=0", repeated.stdout
        )
        report = next(
            line
            for line in repeated.stdout.splitlines()
            if line.startswith("profiles:")
        )
        self.assertEqual(
            report.split("source_checksum=")[1].split()[0],
            report.split("destination_matching_checksum=")[1],
        )
        connection = sqlite3.connect(self.destination_path)
        try:
            profile = connection.execute(
                """
                SELECT id, owner_user_id, source_user_id, vehicle_type, vehicle_registration,
                       license_number, verification_status, is_available, service_radius_km,
                       current_order_id, source_current_order_id
                FROM driver_profiles_driverprofile
                """
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(
            profile[:5] + profile[6:],
            (81, 9101, 4101, "MOTORCYCLE", "ZX-42", "VERIFIED", 1, 18, None, 992),
        )
        self.assertTrue(profile[5].startswith("gAAAAA"))

    def test_profile_id_conflict_aborts_without_changes(self):
        connection = sqlite3.connect(self.destination_path)
        try:
            connection.execute(
                """
                INSERT INTO driver_profiles_driverprofile
                    (id, owner_user_id, vehicle_type, vehicle_registration, license_number,
                     verification_status, is_available, service_radius_km, current_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    81,
                    4101,
                    "BICYCLE",
                    "",
                    "",
                    "PENDING",
                    0,
                    10,
                    None,
                    "2025-03-01 08:00:00",
                    "2025-03-01 08:00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        result = self._run_manage("backfill_driver_profiles", "--apply", success=False)
        self.assertIn("Driver profile ID 81 conflicts on:", result.stderr)
        self.assertIn("vehicle_type", result.stderr)
        self.assertEqual(self._count_profiles(), 1)

    def test_owner_conflict_aborts_without_changes(self):
        connection = sqlite3.connect(self.destination_path)
        try:
            connection.execute(
                """
                INSERT INTO driver_profiles_driverprofile
                    (id, owner_user_id, vehicle_type, vehicle_registration, license_number,
                     verification_status, is_available, service_radius_km, current_order_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    99,
                    9101,
                    "MOTORCYCLE",
                    "ZX-42",
                    "DL-98765",
                    "VERIFIED",
                    1,
                    18,
                    992,
                    "2025-03-01 08:00:00",
                    "2025-03-02 08:00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        result = self._run_manage("backfill_driver_profiles", "--apply", success=False)
        self.assertIn("already belongs to destination driver profile 99", result.stderr)
        self.assertEqual(self._count_profiles(), 1)

    def test_inactive_destination_identity_fails_closed(self):
        self.mapping_path.write_text(
            "source_user_id,destination_user_id\n4101,9999\n",
            encoding="utf-8",
        )
        result = self._run_manage("backfill_driver_profiles", success=False)
        self.assertIn("not active drivers", result.stderr)
        self.assertEqual(self._count_profiles(), 0)

    def test_refuses_same_source_and_destination_database(self):
        self.environment["SOURCE_DATABASE_URL"] = _sqlite_url(self.destination_path)
        result = self._run_manage("backfill_driver_profiles", success=False)
        self.assertIn(
            "Source and destination must be different databases", result.stderr
        )

    def test_requires_both_explicit_database_urls(self):
        self.environment.pop("SOURCE_DATABASE_URL")
        result = self._run_manage("backfill_driver_profiles", success=False)
        self.assertIn(
            "Set both SOURCE_DATABASE_URL and DATABASE_URL explicitly", result.stderr
        )
