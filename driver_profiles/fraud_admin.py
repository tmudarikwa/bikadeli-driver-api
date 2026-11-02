import csv
import hmac
from datetime import timedelta
from typing import ClassVar

from django.conf import settings
from django.db.models import Count
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    DriverBehaviorScore,
    DriverDevice,
    DriverLocationAnomaly,
    DriverRatingAnomaly,
)

PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}


def _csv_cell(value):
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in {"=", "+", "-", "@", "\t", "\r"} else text


class InternalDriverRiskBreakdownView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    WEIGHTS: ClassVar[dict] = {
        "on_time": 0.25,
        "cancellation": 0.15,
        "complaint": 0.20,
        "location": 0.20,
        "rating": 0.20,
    }

    def get(self, request, driver_id):
        if not _authorized(request):
            return Response({"detail": "Not found."}, status=404)
        score = DriverBehaviorScore.objects.filter(owner_user_id=driver_id).first()
        if score is None:
            return Response({"error": "No behavior score for driver"}, status=404)
        parts = [
            ("on_time", "On-time delivery", score.on_time_score,
             f"{score.on_time_deliveries_30d}/{score.total_deliveries_30d} on time"),
            ("cancellation", "Cancellation rate", score.cancellation_score,
             f"{score.cancellations_30d} cancellations in 30d"),
            ("complaint", "Customer complaints", score.complaint_score,
             f"{score.complaints_30d} complaints in 30d"),
            ("location", "Location accuracy", score.location_accuracy_score,
             f"{score.gps_anomalies_30d} GPS anomalies in 30d"),
            ("rating", "Rating pattern", score.rating_score, f"avg {score.avg_rating_30d} over 30d"),
        ]
        components = []
        for factor, label, value, details in parts:
            value_f = float(value)
            weight = self.WEIGHTS[factor]
            components.append(
                {
                    "factor": factor,
                    "label": f"{label} ({details})",
                    "score": value_f,
                    "weight": weight,
                    "contribution": round((100 - value_f) * weight, 1),
                    "details": details,
                }
            )
        return Response(
            {
                "driver_id": driver_id,
                "driver_email": None,
                "driver_name": "",
                "overall_score": float(score.overall_score),
                "components": components,
                "total_deliveries_30d": score.total_deliveries_30d,
                "last_calculated": score.computed_at.isoformat(),
            }
        )


class InternalFlaggedDriversView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    def get(self, request):
        if not _authorized(request):
            return Response({"detail": "Not found."}, status=404)
        period = request.query_params.get("period", "30d")
        cutoff = timezone.now() - timedelta(days=PERIOD_DAYS.get(period, 30))
        scores = list(
            DriverBehaviorScore.objects.filter(is_flagged=True, computed_at__gte=cutoff).order_by(
                "overall_score", "id"
            )[:200]
        )
        ids = [s.owner_user_id for s in scores]
        ratings = dict(
            DriverRatingAnomaly.objects.filter(owner_user_id__in=ids, created_at__gte=cutoff)
            .values("owner_user_id")
            .annotate(c=Count("id"))
            .values_list("owner_user_id", "c")
        )
        devices = dict(
            DriverDevice.objects.filter(owner_user_id__in=ids, is_active=True)
            .values("owner_user_id")
            .annotate(c=Count("id"))
            .values_list("owner_user_id", "c")
        )
        results = [
            {
                "driver_id": s.owner_user_id,
                "driver_email": None,
                "driver_name": "",
                "vehicle_type": "UNKNOWN",
                "overall_score": float(s.overall_score),
                "risk_level": s.risk_level,
                "flag_reasons": s.flag_reasons,
                "on_time_score": float(s.on_time_score),
                "cancellation_score": float(s.cancellation_score),
                "complaint_score": float(s.complaint_score),
                "location_accuracy_score": float(s.location_accuracy_score),
                "rating_score": float(s.rating_score),
                "total_deliveries_30d": s.total_deliveries_30d,
                "gps_anomalies_30d": s.gps_anomalies_30d,
                "rating_anomalies_30d": ratings.get(s.owner_user_id, 0),
                "active_devices": devices.get(s.owner_user_id, 0),
                "avg_rating_30d": float(s.avg_rating_30d),
            }
            for s in scores
        ]
        return Response({"count": len(results), "period": period, "results": results})


class InternalAnomalyMapView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    def get(self, request):
        if not _authorized(request):
            return Response({"detail": "Not found."}, status=404)
        items = DriverLocationAnomaly.objects.filter(
            is_reviewed=False,
            reported_latitude__isnull=False,
            reported_longitude__isnull=False,
            created_at__gte=timezone.now() - timedelta(days=7),
        ).order_by("-created_at", "-id")[:100]
        results = [
            {
                "id": a.pk,
                "driver_id": a.owner_user_id,
                "driver_email": "",
                "order_number": None,
                "order_id": a.active_order_id,
                "anomaly_type": a.anomaly_type,
                "anomaly_display": a.get_anomaly_type_display(),
                "severity": a.severity,
                "lat": a.reported_latitude,
                "lng": a.reported_longitude,
                "created_at": a.created_at.isoformat(),
                "is_reviewed": a.is_reviewed,
            }
            for a in items
        ]
        return Response({"count": len(results), "results": results})


class InternalAnomalyPointsView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]
    MAX_POINTS: ClassVar[int] = 5000

    def get(self, request):
        if not _authorized(request):
            return Response({"detail": "Not found."}, status=404)
        days = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}.get(request.query_params.get("range", "30d"), 30)
        rows = (
            DriverLocationAnomaly.objects.filter(
                reported_latitude__isnull=False,
                reported_longitude__isnull=False,
                created_at__gte=timezone.now() - timedelta(days=days),
            )
            .order_by("-created_at", "-id")
            .values_list("owner_user_id", "active_order_id", "reported_latitude", "reported_longitude")[
                : self.MAX_POINTS
            ]
        )
        results = [{"driver_id": d, "order_id": o, "lat": lat, "lng": lng} for d, o, lat, lng in rows]
        return Response({"count": len(results), "results": results})


class InternalAnomalyCsvView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    def get(self, request):
        if not _authorized(request):
            return Response({"detail": "Not found."}, status=404)
        days = PERIOD_DAYS.get(request.query_params.get("period", "30d"), 30)
        items = DriverLocationAnomaly.objects.filter(
            created_at__gte=timezone.now() - timedelta(days=days)
        ).order_by("-created_at", "-id")[:10000]
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="gps-anomalies.csv"'
        writer = csv.writer(response)
        writer.writerow(
            ["ID", "Driver ID", "Order", "Type", "Severity", "Speed (km/h)", "Max Speed",
             "Distance (km)", "Time (s)", "Reviewed", "Created"]
        )
        for a in items:
            values = (
                a.pk, a.owner_user_id, a.active_order_id, a.anomaly_type, a.severity,
                a.reported_speed_kmh, a.max_allowed_speed_kmh, a.distance_km,
                a.time_delta_seconds, a.is_reviewed, a.created_at.isoformat(),
            )
            writer.writerow([_csv_cell(v) for v in values])
        return response


class InternalDriverRatingAnomalyView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    def get(self, request):
        if not _authorized(request):
            return Response({"detail": "Not found."}, status=404)
        days = PERIOD_DAYS.get(request.query_params.get("period", "30d"), 30)
        items = DriverRatingAnomaly.objects.filter(
            created_at__gte=timezone.now() - timedelta(days=days)
        )[:100]
        results = [
            {
                "id": a.pk,
                "driver_id": a.owner_user_id,
                "driver_email": None,
                "driver_name": "",
                "anomaly_type": a.anomaly_type,
                "rating_count": a.rating_count,
                "avg_rating_in_window": float(a.avg_rating_in_window),
                "baseline_rating": float(a.baseline_rating),
                "details": a.details,
                "is_reviewed": a.is_reviewed,
                "created_at": a.created_at.isoformat(),
            }
            for a in items
        ]
        return Response({"count": len(results), "results": results})


class InternalDriverDeviceView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    def get(self, request):
        if not _authorized(request):
            return Response({"detail": "Not found."}, status=404)
        try:
            driver_id = int(request.query_params.get("driver_id", ""))
        except ValueError:
            return Response({"error": "driver_id must be an integer"}, status=400)
        devices = list(DriverDevice.objects.filter(owner_user_id=driver_id))
        fingerprints = [d.device_fingerprint for d in devices if d.device_fingerprint]
        counts = {}
        shared_with = {}
        if fingerprints:
            others = DriverDevice.objects.filter(
                is_active=True, device_fingerprint__in=fingerprints
            ).exclude(owner_user_id=driver_id)
            for row in others.values("device_fingerprint").annotate(
                c=Count("owner_user_id", distinct=True)
            ):
                counts[row["device_fingerprint"]] = row["c"]
            for other in others[:50]:
                shared_with.setdefault(other.device_fingerprint, []).append(
                    {"user_id": other.owner_user_id, "email": None}
                )
        results = [
            {
                "id": d.pk,
                "device_id": d.device_id,
                "device_fingerprint": d.device_fingerprint[:16] + "...",
                "device_type": d.device_type,
                "device_model": d.device_model,
                "os_version": d.os_version,
                "app_version": d.app_version,
                "is_active": d.is_active,
                "last_seen": d.last_seen_at.isoformat(),
                "registered": d.created_at.isoformat(),
                "shared_driver_count": counts.get(d.device_fingerprint, 0),
                "shared_with_drivers": shared_with.get(d.device_fingerprint, []),
            }
            for d in devices
        ]
        return Response(
            {
                "count": len(results),
                "results": results,
                "driver_profile_id": None,
                "user_id": driver_id,
            }
        )


def _authorized(request):
    supplied = request.headers.get("X-Driver-Admin-Token", "")
    return bool(
        settings.DRIVER_FRAUD_REVIEW_SERVICE_ENABLED
        and settings.DRIVER_ADMIN_TOKEN
        and hmac.compare_digest(supplied, settings.DRIVER_ADMIN_TOKEN)
    )
