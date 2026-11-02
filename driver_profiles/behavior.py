import hmac
from typing import ClassVar

from django.conf import settings
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import DriverBehaviorScore

VALID_SORTS = {
    "overall_score",
    "on_time_score",
    "cancellation_score",
    "complaint_score",
    "location_accuracy_score",
    "rating_score",
}


def _serialize(score):
    return {
        "driver_id": score.owner_user_id,
        "driver_email": None,
        "driver_name": "",
        "overall_score": float(score.overall_score),
        "risk_level": score.risk_level,
        "on_time_score": float(score.on_time_score),
        "cancellation_score": float(score.cancellation_score),
        "complaint_score": float(score.complaint_score),
        "location_accuracy_score": float(score.location_accuracy_score),
        "rating_score": float(score.rating_score),
        "total_deliveries_30d": score.total_deliveries_30d,
        "is_flagged": score.is_flagged,
        "flag_reasons": score.flag_reasons,
        "computed_at": score.computed_at.isoformat(),
    }


class InternalDriverBehaviorScoreView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    def get(self, request):
        supplied = request.headers.get("X-Driver-Admin-Token", "")
        if not (
            settings.DRIVER_FRAUD_REVIEW_SERVICE_ENABLED
            and settings.DRIVER_ADMIN_TOKEN
            and hmac.compare_digest(supplied, settings.DRIVER_ADMIN_TOKEN)
        ):
            return Response({"detail": "Not found."}, status=404)
        sort = request.query_params.get("sort", "overall_score")
        if sort not in VALID_SORTS:
            sort = "overall_score"
        direction = request.query_params.get("direction", "asc")
        queryset = DriverBehaviorScore.objects.order_by(
            sort if direction == "asc" else f"-{sort}", "id"
        )
        if request.query_params.get("flagged") == "true":
            queryset = queryset.filter(is_flagged=True)
        results = [_serialize(item) for item in queryset[:100]]
        return Response({"count": len(results), "results": results})
