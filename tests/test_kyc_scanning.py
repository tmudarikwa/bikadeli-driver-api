from io import BytesIO
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from driver_profiles.kyc_scanning import (
    MalwareDetected,
    ScannerUnavailable,
    scan_kyc_upload,
)


class FakeScanner:
    def __init__(self, response):
        self.response = response
        self.sent = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.sent.extend(data)

    def recv(self, size):
        response = self.response
        self.response = b""
        return response


@override_settings(
    DRIVER_KYC_SCANNER_HOST="clamav",
    DRIVER_KYC_SCANNER_PORT=3310,
    DRIVER_KYC_SCANNER_TIMEOUT_SECONDS=5,
)
class DriverKycScanningTests(SimpleTestCase):
    @patch("driver_profiles.kyc_scanning.socket.create_connection")
    def test_clean_upload_uses_instream_protocol_and_rewinds(self, create_connection):
        scanner = FakeScanner(b"stream: OK\x00")
        create_connection.return_value = scanner
        upload = BytesIO(b"private-image")

        scan_kyc_upload(upload)

        self.assertTrue(scanner.sent.startswith(b"zINSTREAM\x00"))
        self.assertTrue(scanner.sent.endswith(b"\x00\x00\x00\x00"))
        self.assertIn(b"private-image", scanner.sent)
        self.assertEqual(upload.tell(), 0)
        create_connection.assert_called_once_with(("clamav", 3310), timeout=5)

    @patch("driver_profiles.kyc_scanning.socket.create_connection")
    def test_infected_upload_is_rejected(self, create_connection):
        create_connection.return_value = FakeScanner(
            b"stream: Eicar-Test-Signature FOUND\x00"
        )

        with self.assertRaises(MalwareDetected):
            scan_kyc_upload(BytesIO(b"malware"))

    @patch("driver_profiles.kyc_scanning.socket.create_connection")
    def test_scanner_errors_fail_closed(self, create_connection):
        create_connection.side_effect = OSError("connection refused")

        with self.assertRaises(ScannerUnavailable):
            scan_kyc_upload(BytesIO(b"private-image"))

    def test_missing_scanner_is_only_bypassed_in_debug(self):
        upload = BytesIO(b"private-image")
        with override_settings(DRIVER_KYC_SCANNER_HOST="", DEBUG=True):
            scan_kyc_upload(upload)
        self.assertEqual(upload.tell(), 0)

        with override_settings(
            DRIVER_KYC_SCANNER_HOST="", DEBUG=False
        ), self.assertRaises(ScannerUnavailable):
            scan_kyc_upload(upload)
