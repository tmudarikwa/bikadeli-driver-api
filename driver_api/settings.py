import math
import os
from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from bikadeli_common.settings import (
    configure_database,
    configure_gateway_settings,
    configure_token_verification,
    configure_upstream_url,
)
from cryptography.fernet import Fernet

configure_gateway_settings(globals(), "driver-api")
configure_database(globals(), "driver-api")
configure_token_verification(globals())
configure_upstream_url(globals(), "ORDERS_SERVICE_URL", "ORDERS_SERVICE_URL")
configure_upstream_url(globals(), "IDENTITY_SERVICE_URL", "IDENTITY_SERVICE_URL")
configure_upstream_url(globals(), "FINANCE_SERVICE_URL", "FINANCE_SERVICE_URL")
CHANNEL_REDIS_URL = os.getenv("CHANNEL_REDIS_URL", os.getenv("REDIS_URL", "")).strip()
DEMAND_SYNC_TOKEN = os.getenv("DEMAND_SYNC_TOKEN", "")
DRIVER_FIELDS_ENCRYPTION_KEY = os.getenv("DRIVER_FIELDS_ENCRYPTION_KEY", "").strip()
DRIVER_KYC_SERVICE_ENABLED = os.getenv(
    "DRIVER_KYC_SERVICE_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
DRIVER_SUPPORT_SERVICE_ENABLED = os.getenv(
    "DRIVER_SUPPORT_SERVICE_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
DRIVER_FRAUD_REVIEW_SERVICE_ENABLED = os.getenv(
    "DRIVER_FRAUD_REVIEW_SERVICE_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
DRIVER_ADMIN_OPS_ENABLED = os.getenv("DRIVER_ADMIN_OPS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
DRIVER_QUESTS_ADMIN_ENABLED = os.getenv("DRIVER_QUESTS_ADMIN_ENABLED", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FRAUD_ALERT_FORWARDING_ENABLED = os.getenv(
    "FRAUD_ALERT_FORWARDING_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
ADMIN_SERVICE_URL = os.getenv("ADMIN_SERVICE_URL", "").strip().rstrip("/")
FRAUD_INGEST_TOKEN = os.getenv("FRAUD_INGEST_TOKEN", "").strip()
if FRAUD_ALERT_FORWARDING_ENABLED and (
    not ADMIN_SERVICE_URL or len(FRAUD_INGEST_TOKEN) < 32
):
    raise ValueError(
        "ADMIN_SERVICE_URL and a 32+ character FRAUD_INGEST_TOKEN are required "
        "when fraud alert forwarding is enabled."
    )
DRIVER_ADMIN_TOKEN = os.getenv("DRIVER_ADMIN_TOKEN", "").strip()
IDENTITY_USER_LOOKUP_TOKEN = os.getenv("IDENTITY_USER_LOOKUP_TOKEN", "").strip()
KYC_MEDIA_ROOT = os.getenv(
    "KYC_MEDIA_ROOT", str(Path(BASE_DIR) / "private-kyc")
).strip()
DRIVER_KYC_SCANNER_HOST = os.getenv("DRIVER_KYC_SCANNER_HOST", "").strip()
try:
    DRIVER_KYC_SCANNER_PORT = int(os.getenv("DRIVER_KYC_SCANNER_PORT", "3310"))
    DRIVER_KYC_SCANNER_TIMEOUT_SECONDS = float(
        os.getenv("DRIVER_KYC_SCANNER_TIMEOUT_SECONDS", "5")
    )
except ValueError as exc:
    raise ValueError("Driver KYC scanner port and timeout must be valid numbers.") from exc
if not 1 <= DRIVER_KYC_SCANNER_PORT <= 65535:
    raise ValueError("DRIVER_KYC_SCANNER_PORT must be between 1 and 65535.")
if (
    not math.isfinite(DRIVER_KYC_SCANNER_TIMEOUT_SECONDS)
    or DRIVER_KYC_SCANNER_TIMEOUT_SECONDS <= 0
):
    raise ValueError("DRIVER_KYC_SCANNER_TIMEOUT_SECONDS must be positive and finite.")
if DRIVER_KYC_SERVICE_ENABLED and not DEBUG and not DRIVER_KYC_SCANNER_HOST:
    raise ValueError(
        "DRIVER_KYC_SCANNER_HOST must be configured when KYC is enabled outside DEBUG."
    )
if not DRIVER_FIELDS_ENCRYPTION_KEY and DEBUG:
    DRIVER_FIELDS_ENCRYPTION_KEY = urlsafe_b64encode(
        sha256(SECRET_KEY.encode()).digest()
    ).decode()
if not DEBUG and not ORDERS_SERVICE_URL:
    raise ValueError("ORDERS_SERVICE_URL must be configured.")
if not DEBUG and not IDENTITY_SERVICE_URL:
    raise ValueError("IDENTITY_SERVICE_URL must be configured.")
if not DEBUG and not FINANCE_SERVICE_URL:
    raise ValueError("FINANCE_SERVICE_URL must be configured.")
if not DEBUG and not CHANNEL_REDIS_URL:
    raise ValueError("CHANNEL_REDIS_URL or REDIS_URL must be configured.")
if not DEBUG and len(DEMAND_SYNC_TOKEN) < 32:
    raise ValueError("DEMAND_SYNC_TOKEN must contain at least 32 characters.")
if not DEBUG and not DRIVER_FIELDS_ENCRYPTION_KEY:
    raise ValueError("DRIVER_FIELDS_ENCRYPTION_KEY must be configured.")
if DRIVER_FIELDS_ENCRYPTION_KEY:
    try:
        Fernet(DRIVER_FIELDS_ENCRYPTION_KEY.encode())
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "DRIVER_FIELDS_ENCRYPTION_KEY must be a valid Fernet key."
        ) from exc
if DRIVER_KYC_SERVICE_ENABLED and len(DRIVER_ADMIN_TOKEN) < 32:
    raise ValueError(
        "DRIVER_ADMIN_TOKEN must contain at least 32 characters when driver KYC is enabled."
    )
if DRIVER_SUPPORT_SERVICE_ENABLED and len(DRIVER_ADMIN_TOKEN) < 32:
    raise ValueError(
        "DRIVER_ADMIN_TOKEN must contain at least 32 characters when driver support is enabled."
    )
if DRIVER_SUPPORT_SERVICE_ENABLED and len(IDENTITY_USER_LOOKUP_TOKEN) < 32:
    raise ValueError(
        "IDENTITY_USER_LOOKUP_TOKEN must contain at least 32 characters when driver support is enabled."
    )
if DRIVER_FRAUD_REVIEW_SERVICE_ENABLED and len(DRIVER_ADMIN_TOKEN) < 32:
    raise ValueError(
        "DRIVER_ADMIN_TOKEN must contain at least 32 characters when driver fraud review is enabled."
    )
if DRIVER_QUESTS_ADMIN_ENABLED and len(DRIVER_ADMIN_TOKEN) < 32:
    raise ValueError(
        "DRIVER_ADMIN_TOKEN must contain at least 32 characters when driver quests admin is enabled."
    )
if DRIVER_ADMIN_OPS_ENABLED and len(DRIVER_ADMIN_TOKEN) < 32:
    raise ValueError("DRIVER_ADMIN_TOKEN must contain at least 32 characters when driver admin ops are enabled.")
if CHANNEL_REDIS_URL:
    channel_redis_url = urlsplit(CHANNEL_REDIS_URL)
    if (
        channel_redis_url.scheme not in {"redis", "rediss"}
        or not channel_redis_url.hostname
    ):
        raise ValueError("CHANNEL_REDIS_URL must be a Redis URL.")

ROOT_URLCONF = "driver_api.urls"
WSGI_APPLICATION = "driver_api.wsgi.application"
ASGI_APPLICATION = "driver_api.asgi.application"
DATABASE_READINESS_CHECK = True
INSTALLED_APPS = [
    "rest_framework",
    "driver_profiles.apps.DriverProfilesConfig",
]
if DRIVER_QUESTS_ADMIN_ENABLED and len(DRIVER_ADMIN_TOKEN) < 32:
    raise ValueError(
        "DRIVER_ADMIN_TOKEN must contain at least 32 characters when driver quests admin is enabled."
    )
if CHANNEL_REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [CHANNEL_REDIS_URL]},
        },
    }
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        },
    }
REST_FRAMEWORK = {
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "bikadeli_common.authentication.AccessTokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}
