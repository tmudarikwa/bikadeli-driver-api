import logging
import socket
import struct

from django.conf import settings

logger = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
MAX_RESPONSE_SIZE = 4096


class MalwareDetected(Exception):
    pass


class ScannerUnavailable(Exception):
    pass


def scan_kyc_upload(upload):
    if not settings.DRIVER_KYC_SCANNER_HOST:
        if settings.DEBUG:
            logger.warning("Driver KYC malware scan skipped in DEBUG without a scanner.")
            upload.seek(0)
            return
        raise ScannerUnavailable("Driver KYC scanner is not configured.")

    try:
        upload.seek(0)
        with socket.create_connection(
            (
                settings.DRIVER_KYC_SCANNER_HOST,
                settings.DRIVER_KYC_SCANNER_PORT,
            ),
            timeout=settings.DRIVER_KYC_SCANNER_TIMEOUT_SECONDS,
        ) as scanner:
            scanner.settimeout(settings.DRIVER_KYC_SCANNER_TIMEOUT_SECONDS)
            scanner.sendall(b"zINSTREAM\x00")
            while chunk := upload.read(CHUNK_SIZE):
                scanner.sendall(struct.pack("!I", len(chunk)))
                scanner.sendall(chunk)
            scanner.sendall(struct.pack("!I", 0))

            response = bytearray()
            while b"\x00" not in response:
                part = scanner.recv(1024)
                if not part:
                    raise ScannerUnavailable("Driver KYC scanner closed without a result.")
                response.extend(part)
                if len(response) > MAX_RESPONSE_SIZE:
                    raise ScannerUnavailable("Driver KYC scanner returned an oversized result.")
    except OSError as exc:
        raise ScannerUnavailable("Driver KYC scanner could not be reached.") from exc
    finally:
        upload.seek(0)

    result = bytes(response).split(b"\x00", 1)[0].decode("ascii", errors="replace")
    if result == "stream: OK":
        return
    if result.startswith("stream: ") and result.endswith(" FOUND"):
        raise MalwareDetected("Driver KYC upload was flagged by malware scanning.")
    raise ScannerUnavailable("Driver KYC scanner returned an invalid result.")
