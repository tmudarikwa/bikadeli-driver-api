import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

ALERT_TYPE_MAP = {
    "SPEED_EXCEEDED": "SPEED_CHECK",
    "IMPOSSIBLE_TRAVEL": "IMPOSSIBLE_TRAVEL",
    "GEOBOUNDARY_EXIT": "GEOFENCE_BREACH",
    "STATIC_LOCATION": "STATIC_LOCATION",
}


def forward_anomaly_alert(anomaly):
    """Best-effort forward of a HIGH/CRITICAL anomaly to admin-api; never raises."""
    if not settings.FRAUD_ALERT_FORWARDING_ENABLED:
        return False
    if anomaly.severity not in {"HIGH", "CRITICAL"}:
        return False
    alert_type = ALERT_TYPE_MAP.get(anomaly.anomaly_type)
    if not alert_type:
        return False
    payload = {
        "source_alert_id": f"driver-anomaly-{anomaly.pk}",
        "alert_type": alert_type,
        "severity": anomaly.severity,
        "message": str(anomaly.details or "")[:500],
        "driver_id": anomaly.owner_user_id,
        "order_id": anomaly.active_order_id,
        "created_at": anomaly.created_at.isoformat(),
        "metadata": {"anomaly_id": anomaly.pk, "anomaly_type": anomaly.anomaly_type},
    }
    try:
        response = httpx.post(
            f"{settings.ADMIN_SERVICE_URL}/internal/v1/fraud/alerts/",
            json=payload,
            headers={"X-Fraud-Ingest-Token": settings.FRAUD_INGEST_TOKEN},
            timeout=3.0,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        logger.warning("Fraud alert forwarding failed for anomaly %s", anomaly.pk)
        return False
    return True
