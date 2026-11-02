import hmac
import math
from datetime import timedelta
from typing import ClassVar

import httpx
from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from .admin_audit_models import DriverAdminAuditLog
from .models import DriverLocation, DriverLocationHistory, DriverProfile

PAGE_SIZE = 25
BOOLEAN = {"true": True, "false": False}


class _DriverAdmin(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    def initial(self, request, *args, **kwargs):
        supplied = request.headers.get("X-Driver-Admin-Token", "")
        if not (
            settings.DRIVER_ADMIN_OPS_ENABLED
            and settings.DRIVER_ADMIN_TOKEN
            and hmac.compare_digest(supplied, settings.DRIVER_ADMIN_TOKEN)
        ):
            raise NotFound
        super().initial(request, *args, **kwargs)

    def actor(self, request):
        actor_id = request.data.get("actor_id")
        email = request.data.get("actor_email")
        if type(actor_id) is not int or actor_id < 1 or not isinstance(email, str) or not email:
            return None
        return actor_id, email

    def audit(self, profile, actor, action, **details):
        DriverAdminAuditLog.objects.create(
            profile=profile, actor_user_id=actor[0], actor_email=actor[1], action=action, details=details
        )


def _serialize(d):
    return {
        "id": d.id,
        "user_id": d.owner_user_id,
        "vehicle_type": d.vehicle_type,
        "vehicle_plate": d.vehicle_registration,
        "verification_status": d.verification_status,
        "is_available": d.is_available,
        "service_radius_km": d.service_radius_km,
        "current_order_id": d.current_order_id,
        "created_at": d.created_at.isoformat(),
        "updated_at": d.updated_at.isoformat(),
        "user_name": None,
        "user_email": None,
        "rating": None,
        "total_deliveries": None,
    }


class AdminDriverListView(_DriverAdmin):
    def get(self, request):
        try:
            page = int(request.query_params.get("page", 1))
        except ValueError:
            page = 0
        if page < 1:
            return Response({"page": ["Must be a positive integer."]}, status=400)
        qs = DriverProfile.objects.order_by("-created_at", "-id")
        status = request.query_params.get("verification_status")
        if status:
            qs = qs.filter(verification_status=status)
        available = BOOLEAN.get(request.query_params.get("is_available", ""))
        if available is not None:
            qs = qs.filter(is_available=available)
        start = (page - 1) * PAGE_SIZE
        return Response(
            {
                "results": [_serialize(d) for d in qs[start : start + PAGE_SIZE]],
                "total": qs.count(),
                "page": page,
                "page_size": PAGE_SIZE,
            }
        )


class AdminDriverToggleAvailableView(_DriverAdmin):
    def post(self, request, driver_id):
        actor = self.actor(request)
        if actor is None:
            return Response({"detail": "actor_id and actor_email are required."}, status=400)
        with transaction.atomic():
            driver = get_object_or_404(DriverProfile.objects.select_for_update(), id=driver_id)
            driver.is_available = not driver.is_available
            driver.save(update_fields=["is_available"])
            self.audit(driver, actor, "TOGGLE_AVAILABLE", is_available=driver.is_available)
        return Response({"status": "ok", "is_available": driver.is_available})


class AdminDriverDeliverySettingsView(_DriverAdmin):
    def post(self, request, driver_id):
        actor = self.actor(request)
        if actor is None:
            return Response({"detail": "actor_id and actor_email are required."}, status=400)
        raw = request.data.get("service_radius_km")
        try:
            radius = float(raw)
        except (TypeError, ValueError):
            return Response({"error": "service_radius_km must be a number"}, status=400)
        if isinstance(raw, bool) or not math.isfinite(radius) or not 0 < radius <= 1000:
            return Response({"error": "service_radius_km must be greater than 0 and at most 1000"}, status=400)
        with transaction.atomic():
            driver = get_object_or_404(DriverProfile.objects.select_for_update(), id=driver_id)
            previous = driver.service_radius_km
            driver.service_radius_km = int(radius)
            driver.save(update_fields=["service_radius_km"])
            self.audit(driver, actor, "DELIVERY_SETTINGS", previous=previous, current=driver.service_radius_km)
        return Response({"status": "ok", "service_radius_km": driver.service_radius_km})


class AdminDriverVerifyView(_DriverAdmin):
    def post(self, request, driver_id):
        actor = self.actor(request)
        if actor is None:
            return Response({"detail": "actor_id and actor_email are required."}, status=400)
        new_status = request.data.get("verification_status")
        if new_status not in {value for value, _ in DriverProfile.VERIFICATION_STATUSES}:
            return Response({"verification_status": ["Invalid verification status."]}, status=400)
        with transaction.atomic():
            driver = get_object_or_404(DriverProfile.objects.select_for_update(), id=driver_id)
            previous = driver.verification_status
            driver.verification_status = new_status
            driver.save(update_fields=["verification_status"])
            self.audit(driver, actor, "VERIFY", previous=previous, current=new_status)
        return Response({"status": "ok", "verification_status": new_status})


class AdminDriverLocationPrivacyView(_DriverAdmin):
    def get(self, request, driver_id):
        driver = get_object_or_404(DriverProfile, pk=driver_id)
        now = timezone.now()
        location = DriverLocation.objects.filter(owner_user_id=driver.owner_user_id).first()
        visible = driver.is_available or (
            location is not None and (now - location.updated_at).total_seconds() < 7200
        )
        location_data = None
        if visible:
            location_data = {
                "lat": location.latitude if location else None,
                "lng": location.longitude if location else None,
                "updated_at": location.updated_at.isoformat() if location else None,
                "freshness_seconds": (now - location.updated_at).total_seconds() if location else None,
                "is_stale": (now - location.updated_at).total_seconds() > 300 if location else True,
            }
        retention_days = 30
        return Response(
            {
                "driver_id": driver_id,
                "driver_name": f"Driver #{driver_id}",
                "is_available": driver.is_available,
                "location_visible": visible,
                "location": location_data,
                "retention_policy_days": retention_days,
                "retention_cutoff": (now - timedelta(days=retention_days)).isoformat(),
                "privacy_note": (
                    "Location only visible while active or within 2 hours of last shift. "
                    "Logs older than 30 days are purged."
                ),
            }
        )


class AdminDriverPurgeLocationHistoryView(_DriverAdmin):
    def post(self, request, driver_id):
        actor = self.actor(request)
        if actor is None:
            return Response({"detail": "actor_id and actor_email are required."}, status=400)
        raw_days = request.data.get("retention_days", 30)
        try:
            retention_days = int(raw_days)
        except (TypeError, ValueError):
            return Response({"retention_days": ["Must be an integer from 1 to 365."]}, status=400)
        if isinstance(raw_days, bool) or not 1 <= retention_days <= 365:
            return Response({"retention_days": ["Must be an integer from 1 to 365."]}, status=400)
        driver = get_object_or_404(DriverProfile, pk=driver_id)
        cutoff = timezone.now() - timedelta(days=retention_days)
        deleted, _ = DriverLocationHistory.objects.filter(
            owner_user_id=driver.owner_user_id,
            recorded_at__lt=cutoff,
        ).delete()
        self.audit(
            driver,
            actor,
            "PURGE_LOCATION_HISTORY",
            retention_days=retention_days,
            deleted_location_logs=deleted,
            cutoff=cutoff.isoformat(),
        )
        return Response(
            {
                "status": "ok",
                "driver_id": driver_id,
                "deleted_location_logs": deleted,
                "cutoff": cutoff.isoformat(),
            }
        )


class AdminDriverEligibilityView(_DriverAdmin):
    def get(self, request, driver_id):
        driver = get_object_or_404(DriverProfile, pk=driver_id)
        if not driver.is_available or driver.verification_status != "VERIFIED":
            return Response({"detail": "Driver is not available and verified."}, status=409)
        if not settings.IDENTITY_SERVICE_URL or len(settings.DEMAND_SYNC_TOKEN) < 32:
            return Response({"detail": "Driver eligibility verification is not configured."}, status=503)
        try:
            response = httpx.post(
                f"{settings.IDENTITY_SERVICE_URL.rstrip('/')}/internal/v1/identity/active-users/",
                json={"user_ids": [driver.owner_user_id]},
                headers={"X-Demand-Sync-Token": settings.DEMAND_SYNC_TOKEN},
                timeout=5.0,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.RequestError:
            return Response({"detail": "Identity service unavailable."}, status=503)
        except (httpx.HTTPStatusError, ValueError):
            return Response({"detail": "Identity service returned an invalid response."}, status=502)
        active_user_ids = payload.get("active_user_ids") if isinstance(payload, dict) else None
        if (
            not isinstance(active_user_ids, list)
            or any(type(user_id) is not int for user_id in active_user_ids)
            or not set(active_user_ids).issubset({driver.owner_user_id})
        ):
            return Response({"detail": "Identity service returned an invalid response."}, status=502)
        if driver.owner_user_id not in active_user_ids:
            return Response({"detail": "Driver identity is inactive."}, status=409)
        return Response(
            {
                "driver_id": driver.pk,
                "owner_user_id": driver.owner_user_id,
                "is_available": driver.is_available,
                "verification_status": driver.verification_status,
                "current_order_id": driver.current_order_id,
            }
        )


class AdminDriverEligibleListView(_DriverAdmin):
    def get(self, request):
        try:
            limit = int(request.query_params.get("limit", 100))
            excluded_driver_id = int(request.query_params.get("exclude_driver_id", 0))
        except ValueError:
            return Response({"detail": "limit and exclude_driver_id must be integers."}, status=400)
        if not 1 <= limit <= 200 or excluded_driver_id < 0:
            return Response({"detail": "limit must be 1-200 and exclude_driver_id cannot be negative."}, status=400)
        profiles = list(
            DriverProfile.objects.filter(
                is_available=True,
                verification_status="VERIFIED",
                current_order_id__isnull=True,
            )
            .exclude(pk=excluded_driver_id)
            .order_by("id")[:500]
        )
        if not profiles:
            return Response({"results": []})
        if not settings.IDENTITY_SERVICE_URL or len(settings.DEMAND_SYNC_TOKEN) < 32:
            return Response({"detail": "Driver eligibility verification is not configured."}, status=503)
        user_ids = [profile.owner_user_id for profile in profiles]
        active_user_ids = set()
        try:
            with httpx.Client(timeout=5.0, follow_redirects=False) as client:
                for offset in range(0, len(user_ids), 500):
                    batch = user_ids[offset : offset + 500]
                    response = client.post(
                        f"{settings.IDENTITY_SERVICE_URL.rstrip('/')}/internal/v1/identity/active-users/",
                        json={"user_ids": batch},
                        headers={"X-Demand-Sync-Token": settings.DEMAND_SYNC_TOKEN},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get("active_user_ids"), list):
                        raise TypeError("Invalid active-user response.")
                    active = payload["active_user_ids"]
                    if any(type(user_id) is not int for user_id in active) or not set(active).issubset(batch):
                        raise TypeError("Invalid active-user response.")
                    active_user_ids.update(active)
        except httpx.RequestError:
            return Response({"detail": "Identity service unavailable."}, status=503)
        except (httpx.HTTPStatusError, TypeError):
            return Response({"detail": "Identity service returned an invalid response."}, status=502)
        locations = {
            location.owner_user_id: location
            for location in DriverLocation.objects.filter(
                owner_user_id__in=active_user_ids
            )
        }
        rows = []
        now = timezone.now()
        for profile in profiles:
            if profile.owner_user_id not in active_user_ids:
                continue
            location = locations.get(profile.owner_user_id)
            if location is None:
                continue
            rows.append(
                {
                    "driver_id": profile.pk,
                    "owner_user_id": profile.owner_user_id,
                    "vehicle_type": profile.vehicle_type,
                    "service_radius_km": profile.service_radius_km,
                    "current_order_id": profile.current_order_id,
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "location_updated_at": location.updated_at.isoformat(),
                    "location_freshness_seconds": (now - location.updated_at).total_seconds(),
                }
            )
            if len(rows) >= limit:
                break
        return Response({"results": rows})
