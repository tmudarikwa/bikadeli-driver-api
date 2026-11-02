from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase, override_settings

from driver_profiles.fraud_forwarding import forward_anomaly_alert

ANOMALY = SimpleNamespace(
    pk=9,
    anomaly_type="IMPOSSIBLE_TRAVEL",
    severity="CRITICAL",
    details="jump",
    owner_user_id=3,
    active_order_id=5,
    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
)


@override_settings(
    FRAUD_ALERT_FORWARDING_ENABLED=True,
    ADMIN_SERVICE_URL="http://admin",
    FRAUD_INGEST_TOKEN="t" * 40,
)
class ForwardingTests(SimpleTestCase):
    @patch("driver_profiles.fraud_forwarding.httpx.post")
    def test_posts_idempotent_alert(self, post):
        self.assertTrue(forward_anomaly_alert(ANOMALY))
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"]["source_alert_id"], "driver-anomaly-9")
        self.assertEqual(kwargs["headers"]["X-Fraud-Ingest-Token"], "t" * 40)

    @patch("driver_profiles.fraud_forwarding.httpx.post", side_effect=httpx.ConnectError("x"))
    def test_failure_does_not_raise(self, _post):
        self.assertFalse(forward_anomaly_alert(ANOMALY))

    @patch("driver_profiles.fraud_forwarding.httpx.post")
    def test_low_severity_not_sent(self, post):
        low = SimpleNamespace(**{**ANOMALY.__dict__, "severity": "LOW"})
        self.assertFalse(forward_anomaly_alert(low))
        post.assert_not_called()

    @override_settings(FRAUD_ALERT_FORWARDING_ENABLED=False)
    @patch("driver_profiles.fraud_forwarding.httpx.post")
    def test_disabled_by_default(self, post):
        self.assertFalse(forward_anomaly_alert(ANOMALY))
        post.assert_not_called()
