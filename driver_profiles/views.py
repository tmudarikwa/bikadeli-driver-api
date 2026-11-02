import hmac
import logging
from collections.abc import Mapping
from datetime import timedelta
from http.cookies import SimpleCookie
from typing import ClassVar

import httpx
from bikadeli_common.authentication import AccessTokenAuthentication
from cryptography.fernet import InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import DatabaseError, transaction
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import get_valid_filename
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .fraud_forwarding import forward_anomaly_alert
from .kyc_scanning import MalwareDetected, ScannerUnavailable, scan_kyc_upload
from .kyc_storage import driver_kyc_storage
from .location_events import publish_driver_location_events
from .location_fraud import detect_location_anomalies, is_critical_location_anomaly
from .models import (
    DriverKYCAuditLog,
    DriverKYCDocument,
    DriverLocation,
    DriverLocationAnomaly,
    DriverLocationHistory,
    DriverProfile,
    DriverSupportTicket,
    DriverSupportTicketAttachment,
    DriverSupportTicketAudit,
    DriverSupportTicketInternalNote,
    DriverSupportTicketMessage,
)
from .serializers import (
    CreateDriverProfileSerializer,
    CreateDriverSupportTicketMessageSerializer,
    CreateDriverSupportTicketSerializer,
    DriverKYCDocumentReviewSerializer,
    DriverKYCDocumentSerializer,
    DriverKYCDocumentUploadSerializer,
    DriverLocationUpdateSerializer,
    DriverProfileSerializer,
    DriverRegistrationSerializer,
    DriverSupportTicketMessageSerializer,
    DriverSupportTicketSerializer,
)

logger = logging.getLogger(__name__)


class DriverSupportTicketView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def _require_driver(self, request):
        if not request.auth or request.auth.get("user_type") != "DRIVER":
            return Response(
                {"detail": "Driver access is required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def get(self, request):
        error = self._require_driver(request)
        if error:
            return error

        status_filter = request.query_params.get("status")
        tickets = DriverSupportTicket.objects.filter(owner_user_id=request.user.id)
        if status_filter:
            status_filter = status_filter.upper()
            if status_filter not in dict(DriverSupportTicket.STATUS_CHOICES):
                return Response(
                    {"status": "Invalid support ticket status."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            tickets = tickets.filter(status=status_filter)
        tickets = tickets.annotate(message_count=Count("messages")).prefetch_related(
            Prefetch(
                "messages",
                queryset=DriverSupportTicketMessage.objects.order_by(
                    "-created_at", "-id"
                )[:1],
                to_attr="latest_messages",
            )
        )[:50]
        return Response(DriverSupportTicketSerializer(tickets, many=True).data)

    def post(self, request):
        error = self._require_driver(request)
        if error:
            return error
        if not DriverProfile.objects.filter(owner_user_id=request.user.id).exists():
            return Response(
                {"error": "Driver profile not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = CreateDriverSupportTicketSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        order_id = values.get("order_id")
        if order_id is not None:
            if not settings.ORDERS_SERVICE_URL:
                return Response(
                    {"detail": "Order ownership could not be verified."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            authorization = request.headers.get("Authorization")
            access_token = request.COOKIES.get("access_token")
            if not authorization and access_token:
                authorization = "Bearer " + access_token
            try:
                with httpx.Client(timeout=5.0, follow_redirects=False) as client:
                    ownership_response = client.get(
                        f"{settings.ORDERS_SERVICE_URL}/internal/v1/drivers/orders/{order_id}/ownership/",
                        headers={"Authorization": authorization or ""},
                    )
            except httpx.RequestError as exc:
                logger.error(
                    "Driver support order validation failed: %s", type(exc).__name__
                )
                return Response(
                    {"detail": "Order ownership could not be verified."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            if ownership_response.status_code == status.HTTP_404_NOT_FOUND:
                return Response(
                    {"order_id": "Order is not assigned to this driver."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if ownership_response.status_code != status.HTTP_200_OK:
                logger.error(
                    "Driver support order validation returned HTTP %s",
                    ownership_response.status_code,
                )
                return Response(
                    {"detail": "Order ownership could not be verified."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            try:
                ownership = ownership_response.json()
            except ValueError:
                logger.error("Driver support order validation returned invalid JSON")
                return Response(
                    {"detail": "Order ownership could not be verified."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            if ownership != {"order_id": order_id, "owned": True}:
                logger.error("Driver support order validation response was invalid")
                return Response(
                    {"detail": "Order ownership could not be verified."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
        try:
            with transaction.atomic():
                ticket = DriverSupportTicket.objects.create(
                    owner_user_id=request.user.id,
                    order_id=order_id,
                    category=values["category"],
                    subject=values["subject"],
                    description=values["description"],
                    priority=values["priority"],
                )
                DriverSupportTicketMessage.objects.create(
                    ticket=ticket,
                    sender_is_driver=True,
                    message=values["description"],
                )
        except DatabaseError as exc:
            logger.error(
                "Driver support ticket creation failed: %s", type(exc).__name__
            )
            return Response(
                {"detail": "Support ticket could not be created. Please retry."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        ticket.message_count = 1
        ticket.latest_messages = [
            DriverSupportTicketMessage.objects.filter(ticket=ticket)
            .order_by("-created_at", "-id")
            .first()
        ]
        return Response(
            DriverSupportTicketSerializer(ticket).data,
            status=status.HTTP_201_CREATED,
        )


class DriverSupportTicketMessagesView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def _ticket(self, request, ticket_id):
        if not request.auth or request.auth.get("user_type") != "DRIVER":
            return None, Response(
                {"detail": "Driver access is required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        ticket = DriverSupportTicket.objects.filter(
            pk=ticket_id,
            owner_user_id=request.user.id,
        ).first()
        if ticket is None:
            return None, Response({"error": "Ticket not found"}, status=404)
        return ticket, None

    def get(self, request, ticket_id):
        ticket, error = self._ticket(request, ticket_id)
        if error:
            return error
        messages = ticket.messages.all()
        return Response(DriverSupportTicketMessageSerializer(messages, many=True).data)

    def post(self, request, ticket_id):
        ticket, error = self._ticket(request, ticket_id)
        if error:
            return error
        serializer = CreateDriverSupportTicketMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                ticket = DriverSupportTicket.objects.select_for_update().get(
                    pk=ticket.pk,
                    owner_user_id=request.user.id,
                )
                if ticket.status in {"RESOLVED", "CLOSED"}:
                    ticket.status = "IN_PROGRESS"
                    ticket.resolved_at = None
                ticket.updated_at = timezone.now()
                ticket.save(update_fields=["status", "resolved_at", "updated_at"])
                message = DriverSupportTicketMessage.objects.create(
                    ticket=ticket,
                    sender_is_driver=True,
                    message=serializer.validated_data["message"],
                )
        except DatabaseError as exc:
            logger.error("Driver support reply failed: %s", type(exc).__name__)
            return Response(
                {"detail": "Support reply could not be saved. Please retry."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            DriverSupportTicketMessageSerializer(message).data,
            status=status.HTTP_201_CREATED,
        )


class InternalDriverSupportTicketView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    @staticmethod
    def _authorized(request):
        supplied = request.headers.get("X-Driver-Admin-Token", "")
        return bool(
            settings.DRIVER_SUPPORT_SERVICE_ENABLED
            and settings.DRIVER_ADMIN_TOKEN
            and hmac.compare_digest(supplied, settings.DRIVER_ADMIN_TOKEN)
        )

    @staticmethod
    def _actor(request):
        try:
            actor_id = int(request.headers.get("X-Driver-Admin-Actor-ID", ""))
        except ValueError:
            return None
        email = request.headers.get("X-Driver-Admin-Actor-Email", "").strip()
        if actor_id <= 0 or not email or len(email) > 254:
            return None
        return actor_id, email

    @staticmethod
    def _serialize_ticket(ticket):
        latest_messages = getattr(ticket, "latest_messages", None)
        if latest_messages is None:
            latest = ticket.messages.order_by("-created_at", "-id").first()
        else:
            latest = latest_messages[0] if latest_messages else None
        message_count = getattr(ticket, "message_count", None)
        if message_count is None:
            message_count = ticket.messages.count()
        internal_note_count = getattr(ticket, "internal_note_count", None)
        if internal_note_count is None:
            internal_note_count = ticket.internal_notes.count()
        attachment_count = getattr(ticket, "attachment_count", None)
        if attachment_count is None:
            attachment_count = ticket.attachments.count()
        return {
            "id": ticket.pk,
            "ticket_type": "driver",
            "subject": ticket.subject,
            "description": ticket.description,
            "category": ticket.category,
            "priority": ticket.priority,
            "status": ticket.status,
            "order_id": ticket.order_id,
            "created_at": ticket.created_at.isoformat(),
            "updated_at": ticket.updated_at.isoformat(),
            "resolved_at": (
                ticket.resolved_at.isoformat() if ticket.resolved_at else None
            ),
            "last_message": (
                {
                    "message": latest.message[:120],
                    "sender_is_driver": latest.sender_is_driver,
                    "created_at": latest.created_at.isoformat(),
                }
                if latest
                else None
            ),
            "assigned_to": ticket.assigned_to_email or None,
            "assigned_to_email": ticket.assigned_to_email or None,
            "assigned_at": (
                ticket.assigned_at.isoformat() if ticket.assigned_at else None
            ),
            "escalated": ticket.escalated,
            "escalation_reason": ticket.escalation_reason or None,
            "driver_id": ticket.owner_user_id,
            "driver_email": None,
            "driver_name": None,
            "resolution_notes": ticket.resolution_notes,
            "message_count": message_count,
            "has_internal_notes": internal_note_count > 0,
            "has_attachments": attachment_count > 0,
        }

    def get(self, request, ticket_id=None):
        if not self._authorized(request):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if ticket_id is None:
            queryset = DriverSupportTicket.objects.annotate(
                message_count=Count("messages", distinct=True),
                internal_note_count=Count("internal_notes", distinct=True),
                attachment_count=Count("attachments", distinct=True),
            ).prefetch_related(
                Prefetch(
                    "messages",
                    queryset=DriverSupportTicketMessage.objects.order_by(
                        "-created_at", "-id"
                    )[:1],
                    to_attr="latest_messages",
                )
            )
            status_filter = request.query_params.get("status", "").upper()
            priority = request.query_params.get("priority", "").upper()
            category = request.query_params.get("category", "").upper()
            search = request.query_params.get("search", "").strip()
            assigned_to = request.query_params.get("assigned_to", "").strip()
            unassigned = request.query_params.get("unassigned") == "true"
            if request.query_params.get("overdue") == "true":
                return Response(
                    {
                        "overdue": (
                            "Driver support SLA deadlines are not configured in this service."
                        )
                    },
                    status=400,
                )
            if status_filter:
                if status_filter not in dict(DriverSupportTicket.STATUS_CHOICES):
                    return Response(
                        {"status": "Invalid support ticket status."}, status=400
                    )
                queryset = queryset.filter(status=status_filter)
            if priority:
                if priority not in dict(DriverSupportTicket.PRIORITY_CHOICES):
                    return Response(
                        {"priority": "Invalid support ticket priority."}, status=400
                    )
                queryset = queryset.filter(priority=priority)
            if category:
                if category not in dict(DriverSupportTicket.CATEGORY_CHOICES):
                    return Response(
                        {"category": "Invalid support ticket category."}, status=400
                    )
                queryset = queryset.filter(category=category)
            if search:
                queryset = queryset.filter(
                    Q(subject__icontains=search) | Q(description__icontains=search)
                )
            if assigned_to:
                queryset = queryset.filter(assigned_to_email__iexact=assigned_to)
            elif unassigned:
                queryset = queryset.filter(assigned_to_email="")
            try:
                page = max(1, int(request.query_params.get("page", "1")))
                page_size = min(
                    100, max(1, int(request.query_params.get("page_size", "20")))
                )
            except ValueError:
                return Response({"detail": "Invalid pagination values."}, status=400)
            total = queryset.count()
            start = (page - 1) * page_size
            results = [
                self._serialize_ticket(ticket)
                for ticket in queryset.order_by("-created_at", "-id")[
                    start : start + page_size
                ]
            ]
            return Response(
                {
                    "results": results,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                }
            )

        ticket = get_object_or_404(
            DriverSupportTicket.objects.prefetch_related(
                "messages", "internal_notes", "audit_events", "attachments"
            ),
            pk=ticket_id,
        )
        payload = self._serialize_ticket(ticket)
        payload["messages"] = [
            {
                "id": message.pk,
                "sender": "driver" if message.sender_is_driver else "support",
                "sender_is_driver": message.sender_is_driver,
                "message": message.message,
                "channel": message.channel,
                "created_at": message.created_at.isoformat(),
            }
            for message in ticket.messages.all()
        ]
        payload["internal_notes"] = [
            {
                "id": note.pk,
                "author": note.actor_email,
                "author_email": note.actor_email,
                "message": note.message,
                "created_at": note.created_at.isoformat(),
            }
            for note in ticket.internal_notes.all()
        ]
        payload["status_history"] = [
            {
                "status": event.details.get("status", ""),
                "changed_by": event.actor_email,
                "created_at": event.created_at.isoformat(),
            }
            for event in ticket.audit_events.filter(action="STATUS")
        ]
        payload["attachments"] = [
            {
                "id": attachment.pk,
                "file_name": attachment.file_name,
                "file_url": (
                    "/api/v1/admin/support/tickets/driver/"
                    f"{ticket.pk}/attachments/{attachment.pk}/content/"
                ),
                "file_size": attachment.file_size,
                "mime_type": attachment.mime_type,
                "uploaded_by": attachment.uploaded_by_email,
                "uploaded_at": attachment.uploaded_at.isoformat(),
                "access_logged": False,
            }
            for attachment in ticket.attachments.all()
        ]
        return Response(payload)

    def post(self, request, ticket_id=None, action=None):
        if not self._authorized(request):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        actor = self._actor(request)
        if actor is None:
            return Response({"detail": "Admin actor metadata is invalid."}, status=400)
        if action == "create":
            driver_email = request.data.get("driver_email", "")
            subject = request.data.get("subject", "")
            description = request.data.get("description", "")
            category = request.data.get("category", "OTHER")
            priority = request.data.get("priority", "MEDIUM")
            order_id = request.data.get("order_id")
            if not isinstance(driver_email, str) or not driver_email.strip():
                return Response(
                    {"driver_email": "A driver email is required."}, status=400
                )
            try:
                validate_email(driver_email.strip())
            except DjangoValidationError:
                return Response(
                    {"driver_email": "A valid email is required."}, status=400
                )
            if (
                not isinstance(subject, str)
                or not subject.strip()
                or len(subject) > 200
            ):
                return Response({"subject": "A subject is required."}, status=400)
            if (
                not isinstance(description, str)
                or not description.strip()
                or len(description) > 10000
            ):
                return Response(
                    {"description": "A description is required."}, status=400
                )
            if not isinstance(category, str) or category not in dict(
                DriverSupportTicket.CATEGORY_CHOICES
            ):
                return Response(
                    {"category": "Invalid driver ticket category."}, status=400
                )
            if not isinstance(priority, str) or priority not in dict(
                DriverSupportTicket.PRIORITY_CHOICES
            ):
                return Response(
                    {"priority": "Invalid driver ticket priority."}, status=400
                )
            if order_id is not None and (type(order_id) is not int or order_id < 1):
                return Response({"order_id": "Invalid order ID."}, status=400)
            if order_id is not None:
                return Response(
                    {
                        "order_id": (
                            "Driver ticket order links are unavailable until the "
                            "orders service provides a unified order reference."
                        )
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            if (
                not settings.IDENTITY_SERVICE_URL
                or not settings.IDENTITY_USER_LOOKUP_TOKEN
            ):
                logger.error(
                    "Identity user lookup is not configured for driver support."
                )
                return Response(
                    {"detail": "Driver identity lookup is not configured."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            try:
                with httpx.Client(timeout=5.0, follow_redirects=False) as client:
                    identity_response = client.post(
                        f"{settings.IDENTITY_SERVICE_URL.rstrip('/')}/internal/v1/identity/drivers/validate/",
                        json={"email": driver_email.strip()},
                        headers={
                            "X-Identity-User-Lookup-Token": settings.IDENTITY_USER_LOOKUP_TOKEN
                        },
                    )
            except httpx.RequestError as exc:
                logger.error("Driver identity lookup failed: %s", type(exc).__name__)
                return Response(
                    {"detail": "Driver identity could not be verified."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            if identity_response.status_code != status.HTTP_200_OK:
                logger.error(
                    "Driver identity lookup returned HTTP %s",
                    identity_response.status_code,
                )
                return Response(
                    {"detail": "Driver identity could not be verified."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            try:
                identity_payload = identity_response.json()
                if (
                    identity_payload.get("is_active_driver") is not True
                    or identity_payload.get("email", "").lower()
                    != driver_email.strip().lower()
                    or type(identity_payload.get("user_id")) is not int
                ):
                    raise ValueError("Identity response did not identify one driver.")
                owner_user_id = identity_payload["user_id"]
            except (KeyError, TypeError, ValueError, AttributeError):
                logger.error("Driver identity lookup returned an invalid response.")
                return Response(
                    {"detail": "Driver identity could not be verified."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            if not DriverProfile.objects.filter(owner_user_id=owner_user_id).exists():
                return Response({"detail": "Driver profile not found."}, status=404)
            with transaction.atomic():
                ticket = DriverSupportTicket.objects.create(
                    owner_user_id=owner_user_id,
                    order_id=order_id,
                    category=category,
                    subject=subject.strip(),
                    description=description.strip(),
                    priority=priority,
                    assigned_to_user_id=actor[0],
                    assigned_to_email=actor[1],
                    assigned_at=timezone.now(),
                )
                DriverSupportTicketAudit.objects.create(
                    ticket=ticket,
                    actor_user_id=actor[0],
                    actor_email=actor[1],
                    action="CREATE",
                    details={
                        "category": category,
                        "priority": priority,
                        "order_id": order_id,
                    },
                )
            return Response(self._serialize_ticket(ticket), status=201)

        if action == "bulk":
            ticket_ids = request.data.get("ids")
            operation = request.data.get("action")
            if (
                not isinstance(ticket_ids, list)
                or not ticket_ids
                or len(ticket_ids) > 100
                or any(
                    type(ticket_id) is not int or ticket_id < 1
                    for ticket_id in ticket_ids
                )
                or len(set(ticket_ids)) != len(ticket_ids)
            ):
                return Response(
                    {"ids": "Provide 1 to 100 unique positive ticket IDs."},
                    status=400,
                )
            if not isinstance(operation, str) or operation not in {"assign", "export"}:
                return Response(
                    {"action": "Only assign and export are supported."}, status=400
                )

            assigned_to = ""
            if operation == "assign":
                assigned_to = request.data.get("assigned_to", "")
                if not isinstance(assigned_to, str) or len(assigned_to) > 254:
                    return Response({"assigned_to": "Invalid assignee."}, status=400)
                assigned_to = assigned_to.strip()
                if assigned_to:
                    try:
                        validate_email(assigned_to)
                    except DjangoValidationError:
                        return Response(
                            {"assigned_to": "A valid email is required."}, status=400
                        )

            with transaction.atomic():
                tickets = list(
                    DriverSupportTicket.objects.select_for_update()
                    .filter(pk__in=ticket_ids)
                    .order_by("pk")
                )
                if len(tickets) != len(ticket_ids):
                    return Response(
                        {"detail": "A driver ticket was not found."}, status=404
                    )
                now = timezone.now()
                for ticket in tickets:
                    if operation == "assign":
                        ticket.assigned_to_email = assigned_to
                        ticket.assigned_to_user_id = (
                            actor[0]
                            if assigned_to.lower() == actor[1].lower()
                            else None
                        )
                        ticket.assigned_at = now if assigned_to else None
                        ticket.updated_at = now
                        ticket.save(
                            update_fields=[
                                "assigned_to_email",
                                "assigned_to_user_id",
                                "assigned_at",
                                "updated_at",
                            ]
                        )
                    DriverSupportTicketAudit.objects.create(
                        ticket=ticket,
                        actor_user_id=actor[0],
                        actor_email=actor[1],
                        action="ASSIGN" if operation == "assign" else "EXPORT",
                        details=(
                            {"assigned_to": assigned_to}
                            if operation == "assign"
                            else {"export_format": "csv"}
                        ),
                    )
            return Response({"action": operation, "updated": len(tickets)})

        if ticket_id is None:
            return Response({"detail": "Not found."}, status=404)
        ticket = get_object_or_404(DriverSupportTicket, pk=ticket_id)
        if action == "reply":
            message_text = request.data.get("message", "")
            channel = request.data.get("channel", "IN_APP")
            if not isinstance(message_text, str) or not message_text.strip():
                return Response({"message": "A message is required."}, status=400)
            if len(message_text) > 10000:
                return Response({"message": "Message is too long."}, status=400)
            if channel != "IN_APP":
                return Response(
                    {
                        "channel": (
                            "Driver support replies are currently saved to the "
                            "in-app ticket thread only."
                        )
                    },
                    status=400,
                )
            with transaction.atomic():
                ticket = DriverSupportTicket.objects.select_for_update().get(
                    pk=ticket_id
                )
                old_status = ticket.status
                if ticket.status in {"RESOLVED", "CLOSED"}:
                    ticket.status = "IN_PROGRESS"
                    ticket.resolved_at = None
                ticket.updated_at = timezone.now()
                ticket.save(update_fields=["status", "resolved_at", "updated_at"])
                message = DriverSupportTicketMessage.objects.create(
                    ticket=ticket,
                    sender_is_driver=False,
                    message=message_text.strip(),
                    channel=channel,
                )
                DriverSupportTicketAudit.objects.create(
                    ticket=ticket,
                    actor_user_id=actor[0],
                    actor_email=actor[1],
                    action="REPLY",
                    details={"previous_status": old_status, "status": ticket.status},
                )
            return Response(
                {
                    "id": message.pk,
                    "sender": "support",
                    "sender_is_driver": False,
                    "message": message.message,
                    "channel": message.channel,
                    "created_at": message.created_at.isoformat(),
                },
                status=status.HTTP_201_CREATED,
            )
        if action == "assign":
            assigned_to = request.data.get("assigned_to", "")
            if not isinstance(assigned_to, str) or len(assigned_to) > 254:
                return Response({"assigned_to": "Invalid assignee."}, status=400)
            assigned_to = assigned_to.strip()
            if assigned_to:
                try:
                    validate_email(assigned_to)
                except DjangoValidationError:
                    return Response(
                        {"assigned_to": "A valid email is required."}, status=400
                    )
            with transaction.atomic():
                ticket = DriverSupportTicket.objects.select_for_update().get(
                    pk=ticket_id
                )
                ticket.assigned_to_email = assigned_to
                ticket.assigned_to_user_id = (
                    actor[0] if assigned_to.lower() == actor[1].lower() else None
                )
                ticket.assigned_at = timezone.now() if assigned_to else None
                ticket.updated_at = timezone.now()
                ticket.save(
                    update_fields=[
                        "assigned_to_email",
                        "assigned_to_user_id",
                        "assigned_at",
                        "updated_at",
                    ]
                )
                DriverSupportTicketAudit.objects.create(
                    ticket=ticket,
                    actor_user_id=actor[0],
                    actor_email=actor[1],
                    action="ASSIGN",
                    details={"assigned_to": assigned_to},
                )
            return Response(self._serialize_ticket(ticket))
        elif action == "internal-notes":
            message = request.data.get("message", "")
            if not isinstance(message, str) or not message.strip():
                return Response({"message": "A note is required."}, status=400)
            with transaction.atomic():
                ticket = DriverSupportTicket.objects.select_for_update().get(
                    pk=ticket_id
                )
                note = DriverSupportTicketInternalNote.objects.create(
                    ticket=ticket,
                    actor_user_id=actor[0],
                    actor_email=actor[1],
                    message=message.strip(),
                )
                ticket.updated_at = timezone.now()
                ticket.save(update_fields=["updated_at"])
                DriverSupportTicketAudit.objects.create(
                    ticket=ticket,
                    actor_user_id=actor[0],
                    actor_email=actor[1],
                    action="NOTE",
                    details={"note_id": note.pk},
                )
            return Response(self._serialize_ticket(ticket))
        elif action == "escalate":
            reason = request.data.get("reason", "")
            team = request.data.get("team", "")
            if (
                not isinstance(reason, str)
                or not reason.strip()
                or not isinstance(team, str)
                or not team.strip()
            ):
                return Response({"detail": "Team and reason are required."}, status=400)
            with transaction.atomic():
                ticket = DriverSupportTicket.objects.select_for_update().get(
                    pk=ticket_id
                )
                ticket.escalated = True
                ticket.escalation_reason = f"{team.strip()}: {reason.strip()}"[:1000]
                ticket.updated_at = timezone.now()
                ticket.save(
                    update_fields=["escalated", "escalation_reason", "updated_at"]
                )
                DriverSupportTicketAudit.objects.create(
                    ticket=ticket,
                    actor_user_id=actor[0],
                    actor_email=actor[1],
                    action="ESCALATE",
                    details={"team": team.strip(), "reason": reason.strip()[:900]},
                )
            return Response(self._serialize_ticket(ticket))
        else:
            return Response({"detail": "Not found."}, status=404)

    def patch(self, request, ticket_id):
        if not self._authorized(request):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        actor = self._actor(request)
        if actor is None:
            return Response({"detail": "Admin actor metadata is invalid."}, status=400)
        requested_status = request.data.get("status")
        if requested_status is not None and (
            not isinstance(requested_status, str)
            or requested_status not in dict(DriverSupportTicket.STATUS_CHOICES)
        ):
            return Response({"status": "Invalid support ticket status."}, status=400)
        notes = request.data.get("resolution_notes")
        if notes is not None and (not isinstance(notes, str) or len(notes) > 10000):
            return Response(
                {"resolution_notes": "Invalid resolution notes."}, status=400
            )
        category = request.data.get("category")
        if category is not None and (
            not isinstance(category, str)
            or category not in dict(DriverSupportTicket.CATEGORY_CHOICES)
        ):
            return Response(
                {"category": "Invalid support ticket category."}, status=400
            )
        with transaction.atomic():
            ticket = get_object_or_404(
                DriverSupportTicket.objects.select_for_update(), pk=ticket_id
            )
            updated_fields = []
            details = {}
            if requested_status is not None and requested_status != ticket.status:
                details["previous_status"] = ticket.status
                details["status"] = requested_status
                ticket.status = requested_status
                ticket.resolved_at = (
                    timezone.now() if requested_status == "RESOLVED" else None
                )
                updated_fields.extend(["status", "resolved_at"])
            if notes is not None and notes != ticket.resolution_notes:
                ticket.resolution_notes = notes
                updated_fields.append("resolution_notes")
                details["resolution_notes_updated"] = True
            if category is not None and category != ticket.category:
                ticket.category = category
                updated_fields.append("category")
                details["category"] = category
            if not updated_fields:
                return Response(
                    {"detail": "No supported changes were provided."}, status=400
                )
            ticket.updated_at = timezone.now()
            updated_fields.append("updated_at")
            ticket.save(update_fields=updated_fields)
            if details:
                DriverSupportTicketAudit.objects.create(
                    ticket=ticket,
                    actor_user_id=actor[0],
                    actor_email=actor[1],
                    action=(
                        "STATUS"
                        if "status" in details
                        else "CATEGORY"
                        if "category" in details
                        else "RESOLUTION"
                    ),
                    details=details,
                )
        return Response(self._serialize_ticket(ticket))


class InternalDriverSupportTicketAttachmentView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    @staticmethod
    def _authorized(request):
        supplied = request.headers.get("X-Driver-Admin-Token", "")
        return bool(
            settings.DRIVER_SUPPORT_SERVICE_ENABLED
            and settings.DRIVER_ADMIN_TOKEN
            and hmac.compare_digest(supplied, settings.DRIVER_ADMIN_TOKEN)
        )

    @staticmethod
    def _actor(request):
        try:
            actor_id = int(request.headers.get("X-Driver-Admin-Actor-ID", ""))
        except ValueError:
            return None
        email = request.headers.get("X-Driver-Admin-Actor-Email", "").strip()
        if actor_id <= 0 or not email or len(email) > 254:
            return None
        return actor_id, email

    def post(self, request, ticket_id):
        if not self._authorized(request):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        actor = self._actor(request)
        if actor is None:
            return Response({"detail": "Admin actor metadata is invalid."}, status=400)
        ticket = get_object_or_404(DriverSupportTicket, pk=ticket_id)
        files = request.FILES.getlist("file")
        if not files:
            return Response({"file": "At least one file is required."}, status=400)
        if len(files) > 5:
            return Response({"file": "At most five files may be uploaded."}, status=400)

        allowed_types = {
            "image/jpeg": (".jpg", ".jpeg"),
            "image/png": (".png",),
            "image/webp": (".webp",),
            "application/pdf": (".pdf",),
        }
        signatures = {
            "image/jpeg": lambda head: head.startswith(b"\xff\xd8\xff"),
            "image/png": lambda head: head.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": lambda head: (
                len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
            ),
            "application/pdf": lambda head: head.startswith(b"%PDF-"),
        }
        validated = []
        errors = []
        for uploaded in files:
            mime_type = uploaded.content_type
            suffix = uploaded.name.rsplit(".", 1)[-1].lower()
            extension = f".{suffix}" if "." in uploaded.name else ""
            if mime_type not in allowed_types or (
                extension and extension not in allowed_types[mime_type]
            ):
                errors.append(f"{uploaded.name}: unsupported file type or extension.")
                continue
            if uploaded.size <= 0 or uploaded.size > 5 * 1024 * 1024:
                errors.append(f"{uploaded.name}: file must be between 1 byte and 5 MB.")
                continue
            head = uploaded.read(16)
            uploaded.seek(0)
            if not signatures[mime_type](head):
                errors.append(f"{uploaded.name}: file contents do not match its type.")
                continue
            safe_name = get_valid_filename(uploaded.name)[:255]
            if not safe_name:
                errors.append("An uploaded file has an invalid filename.")
                continue
            validated.append((uploaded, safe_name, mime_type))
        if errors:
            return Response(
                {"detail": "Invalid attachments.", "errors": errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        saved = []
        with transaction.atomic():
            ticket = get_object_or_404(
                DriverSupportTicket.objects.select_for_update(), pk=ticket_id
            )
            for uploaded, safe_name, mime_type in validated:
                attachment = DriverSupportTicketAttachment.objects.create(
                    ticket=ticket,
                    file=uploaded,
                    file_name=safe_name,
                    file_size=uploaded.size,
                    mime_type=mime_type,
                    uploaded_by_user_id=actor[0],
                    uploaded_by_email=actor[1],
                    request_id=request.headers.get("X-Request-ID", "")[:100],
                )
                saved.append(
                    {
                        "id": attachment.pk,
                        "file_name": safe_name,
                        "file_size": attachment.file_size,
                        "mime_type": mime_type,
                        "uploaded_by": actor[1],
                        "uploaded_at": attachment.uploaded_at.isoformat(),
                    }
                )
            DriverSupportTicketAudit.objects.create(
                ticket=ticket,
                actor_user_id=actor[0],
                actor_email=actor[1],
                action="ATTACHMENT_UPLOAD",
                details={"attachment_ids": [item["id"] for item in saved]},
            )
        return Response({"status": "ok", "attachments": saved}, status=201)

    def get(self, request, ticket_id, attachment_id):
        if not self._authorized(request):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        actor = self._actor(request)
        if actor is None:
            return Response({"detail": "Admin actor metadata is invalid."}, status=400)
        attachment = get_object_or_404(
            DriverSupportTicketAttachment,
            ticket_id=ticket_id,
            pk=attachment_id,
        )
        content = attachment.file.open("rb")
        DriverSupportTicketAudit.objects.create(
            ticket_id=ticket_id,
            actor_user_id=actor[0],
            actor_email=actor[1],
            action="ATTACHMENT_VIEW",
            details={"attachment_id": attachment.pk},
        )
        response = FileResponse(
            content,
            as_attachment=True,
            filename=attachment.file_name,
            content_type="application/octet-stream",
        )
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class InternalDriverLocationAnomalyView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    @staticmethod
    def _authorized(request):
        supplied = request.headers.get("X-Driver-Admin-Token", "")
        return bool(
            settings.DRIVER_FRAUD_REVIEW_SERVICE_ENABLED
            and settings.DRIVER_ADMIN_TOKEN
            and hmac.compare_digest(supplied, settings.DRIVER_ADMIN_TOKEN)
        )

    @staticmethod
    def _actor(request):
        try:
            actor_id = int(request.headers.get("X-Driver-Admin-Actor-ID", ""))
        except ValueError:
            return None
        email = request.headers.get("X-Driver-Admin-Actor-Email", "").strip()
        if actor_id <= 0 or not email or len(email) > 254:
            return None
        return actor_id, email

    @staticmethod
    def _serialize(anomaly):
        return {
            "id": anomaly.pk,
            "driver_id": anomaly.owner_user_id,
            "order_id": anomaly.active_order_id,
            "type": anomaly.anomaly_type,
            "severity": anomaly.severity,
            "details": anomaly.details,
            "reported_location": {
                "latitude": anomaly.reported_latitude,
                "longitude": anomaly.reported_longitude,
            },
            "previous_location": (
                {
                    "latitude": anomaly.previous_latitude,
                    "longitude": anomaly.previous_longitude,
                }
                if anomaly.previous_latitude is not None
                and anomaly.previous_longitude is not None
                else None
            ),
            "reported_speed_kmh": anomaly.reported_speed_kmh,
            "max_allowed_speed_kmh": anomaly.max_allowed_speed_kmh,
            "distance_km": anomaly.distance_km,
            "time_delta_seconds": anomaly.time_delta_seconds,
            "is_reviewed": anomaly.is_reviewed,
            "reviewed_by": anomaly.reviewed_by_email or None,
            "reviewed_at": (
                anomaly.reviewed_at.isoformat() if anomaly.reviewed_at else None
            ),
            "review_note": anomaly.review_note,
            "created_at": anomaly.created_at.isoformat(),
        }

    def get(self, request):
        if not self._authorized(request):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        queryset = DriverLocationAnomaly.objects.all()
        for field in ("owner_user_id",):
            value = request.query_params.get("driver_id")
            if value:
                try:
                    queryset = queryset.filter(**{field: int(value)})
                except ValueError:
                    return Response({"driver_id": "Must be an integer."}, status=400)
        anomaly_type = request.query_params.get("type")
        if anomaly_type:
            if anomaly_type not in dict(DriverLocationAnomaly.ANOMALY_TYPES):
                return Response({"type": "Unknown anomaly type."}, status=400)
            queryset = queryset.filter(anomaly_type=anomaly_type)
        severity = request.query_params.get("severity")
        if severity:
            if severity not in dict(DriverLocationAnomaly.SEVERITIES):
                return Response({"severity": "Unknown severity."}, status=400)
            queryset = queryset.filter(severity=severity)
        reviewed = request.query_params.get("is_reviewed")
        if reviewed is not None:
            if reviewed not in {"true", "false"}:
                return Response({"is_reviewed": "Must be true or false."}, status=400)
            queryset = queryset.filter(is_reviewed=reviewed == "true")
        try:
            page = max(1, int(request.query_params.get("page", "1")))
            page_size = min(
                100, max(1, int(request.query_params.get("page_size", "20")))
            )
        except ValueError:
            return Response({"detail": "Invalid pagination values."}, status=400)
        total = queryset.count()
        items = queryset.order_by("-created_at", "-id")[
            (page - 1) * page_size : page * page_size
        ]
        return Response(
            {
                "results": [self._serialize(item) for item in items],
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        )

    def post(self, request, anomaly_id):
        if not self._authorized(request):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        actor = self._actor(request)
        if actor is None:
            return Response({"detail": "Admin actor metadata is invalid."}, status=400)
        if not isinstance(request.data, Mapping):
            return Response({"detail": "A JSON object is required."}, status=400)
        if set(request.data) - {"review_note"}:
            return Response({"detail": "Only review_note may be updated."}, status=400)
        note = request.data.get("review_note", "")
        if not isinstance(note, str) or len(note) > 10000:
            return Response(
                {"review_note": "Must be a string up to 10000 characters."}, status=400
            )
        with transaction.atomic():
            anomaly = get_object_or_404(
                DriverLocationAnomaly.objects.select_for_update(), pk=anomaly_id
            )
            if anomaly.is_reviewed:
                return Response(
                    {"detail": "Location anomaly has already been reviewed."},
                    status=status.HTTP_409_CONFLICT,
                )
            anomaly.is_reviewed = True
            anomaly.reviewed_by_user_id = actor[0]
            anomaly.reviewed_by_email = actor[1]
            anomaly.reviewed_at = timezone.now()
            anomaly.review_note = note.strip()
            anomaly.save(
                update_fields=[
                    "is_reviewed",
                    "reviewed_by_user_id",
                    "reviewed_by_email",
                    "reviewed_at",
                    "review_note",
                ]
            )
        return Response(self._serialize(anomaly))


class DriverKYCFetchMixin:
    def dispatch(self, request, *args, **kwargs):
        if not settings.DRIVER_KYC_SERVICE_ENABLED:
            from bikadeli_common.gateway import proxy_to_monolith

            return proxy_to_monolith(request)
        return super().dispatch(request, *args, **kwargs)


class DriverKYCDocumentView(DriverKYCFetchMixin, APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        profile = get_object_or_404(
            DriverProfile.objects.prefetch_related("kyc_documents"),
            owner_user_id=request.user.id,
        )
        documents = {
            document.document_type: DriverKYCDocumentSerializer(document).data
            for document in profile.kyc_documents.filter(is_current=True)
        }
        required_types = [
            document_type
            for document_type, _ in DriverKYCDocument.REQUIRED_DOCUMENT_TYPES
        ]
        submitted = {
            document_type: document_type in documents
            for document_type in required_types
        }
        return Response(
            {
                "verification_status": profile.verification_status,
                "documents": documents,
                "documents_submitted": submitted,
                "all_complete": all(submitted.values()),
            }
        )

    def post(self, request):
        serializer = DriverKYCDocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            for upload in serializer.validated_data.values():
                scan_kyc_upload(upload)
        except MalwareDetected:
            return Response(
                {"detail": "KYC document was rejected by malware scanning."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ScannerUnavailable as exc:
            logger.error("Driver KYC scanner unavailable: %s", type(exc).__name__)
            return Response(
                {"detail": "KYC document could not be scanned. Please retry."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        saved_files = []
        try:
            with transaction.atomic():
                profile = get_object_or_404(
                    DriverProfile.objects.select_for_update(),
                    owner_user_id=request.user.id,
                )
                for field, upload in serializer.validated_data.items():
                    document_type = serializer.DOCUMENT_FIELDS[field]
                    document = (
                        DriverKYCDocument.objects.select_for_update()
                        .filter(profile=profile, document_type=document_type)
                        .order_by("-version")
                        .first()
                    )
                    version = 1 if document is None else document.version + 1
                    if document is not None and document.is_current:
                        document.is_current = False
                        document.updated_at = timezone.now()
                        document.save(update_fields=["is_current", "updated_at"])
                    document = DriverKYCDocument(
                        profile=profile,
                        document_type=document_type,
                        version=version,
                    )
                    document.original_filename = upload.name[:255]
                    document.content_type = {
                        "JPEG": "image/jpeg",
                        "PNG": "image/png",
                        "WEBP": "image/webp",
                    }[upload.verified_image_format]
                    document.file_size = upload.size
                    document.verification_status = "PENDING"
                    document.rejection_reason = ""
                    document.reviewed_at = None
                    document.reviewed_by_user_id = None
                    document.reviewed_by_email = ""
                    document.request_id = request.headers.get("X-Request-ID", "")[:100]
                    document.file.save(upload.name, upload, save=False)
                    saved_files.append(document.file.name)
                    document.save()
                    DriverKYCAuditLog.objects.create(
                        profile=profile,
                        document=document,
                        actor_user_id=request.user.id,
                        actor_email=getattr(request.user, "email", "") or "",
                        action="UPLOAD",
                        request_id=request.headers.get("X-Request-ID", "")[:100],
                    )

                profile.verification_status = "PENDING"
                profile.is_available = False
                profile.save(
                    update_fields=[
                        "verification_status",
                        "is_available",
                        "updated_at",
                    ]
                )
        except DatabaseError as exc:
            for name in saved_files:
                driver_kyc_storage.delete(name)
            logger.error("Driver KYC persistence failed: %s", type(exc).__name__)
            return Response(
                {"detail": "KYC documents could not be saved. Please retry."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        current_documents = set(
            profile.kyc_documents.filter(is_current=True).values_list(
                "document_type", flat=True
            )
        )
        return Response(
            {
                "message": "Verification documents submitted successfully.",
                "verification_status": profile.verification_status,
                "documents": {
                    document_type: document_type in current_documents
                    for document_type, _ in DriverKYCDocument.REQUIRED_DOCUMENT_TYPES
                },
            },
            status=status.HTTP_200_OK,
        )


class InternalDriverKYCReviewQueueView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    @staticmethod
    def _authorized(request):
        supplied = request.headers.get("X-Driver-Admin-Token", "")
        return bool(
            settings.DRIVER_KYC_SERVICE_ENABLED
            and settings.DRIVER_ADMIN_TOKEN
            and hmac.compare_digest(supplied, settings.DRIVER_ADMIN_TOKEN)
        )

    def get(self, request):
        if not self._authorized(request):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        documents = (
            DriverKYCDocument.objects.filter(verification_status="PENDING")
            .filter(is_current=True)
            .select_related("profile")
            .order_by("uploaded_at", "id")[:100]
        )
        return Response(
            {
                "results": [
                    {
                        **DriverKYCDocumentSerializer(document).data,
                        "profile_id": document.profile_id,
                        "owner_user_id": document.profile.owner_user_id,
                    }
                    for document in documents
                ]
            }
        )


class InternalDriverKYCDocumentView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    def _document(self, request, document_id):
        supplied = request.headers.get("X-Driver-Admin-Token", "")
        if (
            not settings.DRIVER_KYC_SERVICE_ENABLED
            or not settings.DRIVER_ADMIN_TOKEN
            or not hmac.compare_digest(supplied, settings.DRIVER_ADMIN_TOKEN)
        ):
            return None, Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )
        return get_object_or_404(DriverKYCDocument, pk=document_id), None

    def get(self, request, document_id):
        document, error = self._document(request, document_id)
        if error:
            return error
        actor_id = request.headers.get("X-Driver-Admin-Actor-ID", "")
        actor_email = request.headers.get("X-Driver-Admin-Actor-Email", "")
        try:
            actor_id = int(actor_id)
        except ValueError:
            return Response(
                {"detail": "Admin actor metadata is invalid."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if actor_id < 1:
            return Response(
                {"detail": "Admin actor metadata is invalid."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        DriverKYCAuditLog.objects.create(
            profile=document.profile,
            document=document,
            actor_user_id=actor_id,
            actor_email=actor_email[:254],
            action="VIEW",
            request_id=request.headers.get("X-Request-ID", "")[:100],
        )
        return Response(
            {
                **DriverKYCDocumentSerializer(document).data,
                "profile_id": document.profile_id,
                "owner_user_id": document.profile.owner_user_id,
            }
        )


class InternalDriverKYCDocumentContentView(InternalDriverKYCDocumentView):
    def get(self, request, document_id):
        document, error = self._document(request, document_id)
        if error:
            return error
        actor_id = request.headers.get("X-Driver-Admin-Actor-ID", "")
        actor_email = request.headers.get("X-Driver-Admin-Actor-Email", "")
        try:
            actor_id = int(actor_id)
        except ValueError:
            return Response(
                {"detail": "Admin actor metadata is invalid."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if actor_id < 1:
            return Response(
                {"detail": "Admin actor metadata is invalid."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        DriverKYCAuditLog.objects.create(
            profile=document.profile,
            document=document,
            actor_user_id=actor_id,
            actor_email=actor_email[:254],
            action="VIEW",
            request_id=request.headers.get("X-Request-ID", "")[:100],
        )
        try:
            content = document.file.open("rb").read()
        except (InvalidToken, OSError, ValueError) as exc:
            logger.error("Driver KYC document retrieval failed: %s", type(exc).__name__)
            return Response(
                {"detail": "KYC document is unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        response = HttpResponse(content, content_type="application/octet-stream")
        response["Content-Disposition"] = 'attachment; filename="kyc-document"'
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "no-store"
        return response


class InternalDriverKYCDocumentReviewView(InternalDriverKYCDocumentView):
    def post(self, request, document_id):
        document, error = self._document(request, document_id)
        if error:
            return error
        serializer = DriverKYCDocumentReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decision = serializer.validated_data
        with transaction.atomic():
            document = DriverKYCDocument.objects.select_for_update().get(pk=document_id)
            if not document.is_current:
                return Response(
                    {"detail": "Historical documents cannot be reviewed."},
                    status=status.HTTP_409_CONFLICT,
                )
            reason = decision.get("reason", "").strip()
            if document.verification_status != "PENDING":
                same_decision = (
                    document.verification_status == decision["verification_status"]
                    and document.reviewed_by_user_id == decision["actor_id"]
                    and document.reviewed_by_email == decision["actor_email"]
                    and document.request_id == decision.get("request_id", "")
                    and document.rejection_reason == reason
                )
                if same_decision:
                    return Response(DriverKYCDocumentSerializer(document).data)
                return Response(
                    {"detail": "This document has already been reviewed."},
                    status=status.HTTP_409_CONFLICT,
                )

            document.verification_status = decision["verification_status"]
            document.rejection_reason = reason
            document.reviewed_by_user_id = decision["actor_id"]
            document.reviewed_by_email = decision["actor_email"]
            document.request_id = decision.get("request_id", "")[:100]
            document.reviewed_at = timezone.now()
            document.updated_at = timezone.now()
            document.save(
                update_fields=[
                    "verification_status",
                    "rejection_reason",
                    "reviewed_by_user_id",
                    "reviewed_by_email",
                    "request_id",
                    "reviewed_at",
                    "updated_at",
                ]
            )
            DriverKYCAuditLog.objects.create(
                profile=document.profile,
                document=document,
                actor_user_id=decision["actor_id"],
                actor_email=decision["actor_email"],
                action={
                    "APPROVED": "APPROVE",
                    "REJECTED": "REJECT",
                }[decision["verification_status"]],
                request_id=decision.get("request_id", "")[:100],
            )
            profile = DriverProfile.objects.select_for_update().get(
                pk=document.profile_id
            )
            required_types = [
                document_type
                for document_type, _ in DriverKYCDocument.REQUIRED_DOCUMENT_TYPES
            ]
            statuses = list(
                profile.kyc_documents.filter(
                    is_current=True,
                    document_type__in=required_types,
                ).values_list("verification_status", flat=True)
            )
            if len(statuses) == len(required_types) and all(
                value == "APPROVED" for value in statuses
            ):
                profile.verification_status = "VERIFIED"
            elif any(value == "REJECTED" for value in statuses):
                profile.verification_status = "REJECTED"
            else:
                profile.verification_status = "PENDING"
            profile.is_available = False
            profile.save(
                update_fields=[
                    "verification_status",
                    "is_available",
                    "updated_at",
                ]
            )
        return Response(DriverKYCDocumentSerializer(document).data)


class DriverRegistrationView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    def post(self, request):
        serializer = DriverRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not settings.IDENTITY_SERVICE_URL:
            logger.error("Identity service is not configured for driver registration.")
            return Response(
                {"detail": "Identity service is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        identity_url = settings.IDENTITY_SERVICE_URL.rstrip("/")
        identity_payload = {
            field: data[field]
            for field in (
                "email",
                "first_name",
                "last_name",
                "phone_number",
                "password",
                "confirm_password",
            )
        }
        identity_payload["user_type"] = "DRIVER"

        try:
            with httpx.Client(timeout=5.0, follow_redirects=False) as client:
                identity_response = client.post(
                    f"{identity_url}/api/v1/auth/register/",
                    json=identity_payload,
                    headers={
                        "X-Forwarded-For": request.META.get("REMOTE_ADDR", ""),
                        "X-Forwarded-Proto": (
                            "https" if request.is_secure() else "http"
                        ),
                    },
                )
        except httpx.RequestError as exc:
            logger.warning(
                "Identity registration request failed: %s", type(exc).__name__
            )
            return Response(
                {"detail": "Identity service is unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            identity_result = identity_response.json()
        except ValueError:
            logger.error(
                "Identity registration returned invalid JSON with status %s.",
                identity_response.status_code,
            )
            return Response(
                {"detail": "Identity service returned an invalid response."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if identity_response.status_code != status.HTTP_201_CREATED:
            if (
                identity_response.status_code == status.HTTP_400_BAD_REQUEST
                and isinstance(identity_result, dict)
                and identity_result.get("detail")
                == "An account with this email or phone number already exists."
            ):
                return Response(
                    {
                        "message": (
                            "If the email is registered, check your inbox for "
                            "next steps"
                        )
                    },
                    status=status.HTTP_200_OK,
                )
            if not isinstance(identity_result, dict):
                identity_result = {"detail": "Identity registration failed."}
            return Response(
                identity_result,
                status=identity_response.status_code,
            )

        identity_user = (
            identity_result.get("user") if isinstance(identity_result, dict) else None
        )
        access = (
            identity_result.get("access") if isinstance(identity_result, dict) else None
        )
        refresh = (
            identity_result.get("refresh")
            if isinstance(identity_result, dict)
            else None
        )
        if (
            not isinstance(identity_user, dict)
            or type(identity_user.get("id")) is not int
            or identity_user.get("user_type") != "DRIVER"
            or not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not refresh
        ):
            logger.error("Identity registration response did not match its contract.")
            return Response(
                {"detail": "Identity service returned an invalid response."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        profile_defaults = {
            "vehicle_type": data["vehicle_type"],
            "vehicle_registration": data.get("vehicle_registration", ""),
            "license_number": data["license_number"],
        }
        try:
            with transaction.atomic():
                profile, created = DriverProfile.objects.get_or_create(
                    owner_user_id=identity_user["id"],
                    defaults=profile_defaults,
                )
        except DatabaseError as exc:
            logger.error(
                "Driver profile setup failed after identity registration: %s",
                type(exc).__name__,
            )
            return Response(
                {
                    "detail": (
                        "Identity was created, but driver profile setup is "
                        "unavailable. Log in and retry profile setup."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if not created and any(
            getattr(profile, field) != value
            for field, value in profile_defaults.items()
        ):
            return Response(
                {"detail": "A driver profile already exists with different details."},
                status=status.HTTP_409_CONFLICT,
            )

        profile_data = DriverProfileSerializer(profile).data
        driver_user = {
            field: identity_user.get(field)
            for field in (
                "id",
                "email",
                "first_name",
                "last_name",
                "phone_number",
                "user_type",
            )
        }
        response = Response(
            {
                "user": driver_user,
                "profile": profile_data,
                "access": access,
                "refresh": refresh,
                "message": (
                    "Registration successful. Your account is pending admin "
                    "approval. Please upload your verification documents and wait "
                    "for admin approval before accepting deliveries."
                ),
                "onboarding_status": {
                    "verification_status": profile.verification_status,
                    "documents_submitted": False,
                    "admin_approved": False,
                    "next_steps": [
                        "Upload your verification documents.",
                        "Wait for admin review and approval.",
                        "Go online after approval.",
                    ],
                },
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
        cookies = SimpleCookie()
        for value in identity_response.headers.get_list("set-cookie"):
            cookies.load(value)
        response.cookies.update(cookies)
        return response


class DriverProfileView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        profile = get_object_or_404(DriverProfile, owner_user_id=request.user.id)
        return Response(DriverProfileSerializer(profile).data)

    def post(self, request):
        unsupported = set(request.data) - {
            "vehicle_type",
            "vehicle_registration",
            "license_number",
        }
        if unsupported:
            raise ValidationError(
                {
                    "detail": (
                        "Fields cannot be set during creation: "
                        f"{', '.join(sorted(unsupported))}."
                    )
                }
            )
        serializer = CreateDriverProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile_values = dict(serializer.validated_data)
        profile_defaults = {
            "vehicle_type": profile_values.get("vehicle_type", "MOTORCYCLE"),
            "vehicle_registration": profile_values.get("vehicle_registration", ""),
            "license_number": profile_values.get("license_number", ""),
        }
        with transaction.atomic():
            profile, created = DriverProfile.objects.get_or_create(
                owner_user_id=request.user.id,
                defaults=profile_defaults,
            )
        if not created and any(
            getattr(profile, field) != value
            for field, value in profile_defaults.items()
        ):
            return Response(
                {"detail": "A driver profile already exists with different details."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            DriverProfileSerializer(profile).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @transaction.atomic
    def patch(self, request):
        unsupported = set(request.data) - {"vehicle_registration"}
        if unsupported:
            raise ValidationError(
                {
                    "detail": (
                        f"Fields are not editable: {', '.join(sorted(unsupported))}."
                    )
                }
            )
        profile = get_object_or_404(
            DriverProfile.objects.select_for_update(),
            owner_user_id=request.user.id,
        )
        serializer = DriverProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def put(self, request):
        return self.patch(request)


class DriverAvailabilityView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        is_online = request.data.get("is_online")
        if not isinstance(is_online, bool):
            raise ValidationError({"is_online": "A boolean value is required."})
        profile = get_object_or_404(
            DriverProfile.objects.select_for_update(),
            owner_user_id=request.user.id,
        )
        if profile.verification_status != "VERIFIED":
            return Response(
                {
                    "error": (
                        "Account pending admin approval. You cannot go online until "
                        "your account is verified."
                    ),
                    "verification_status": profile.verification_status,
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        profile.is_available = is_online
        profile.save(update_fields=["is_available"])
        return Response({"is_online": is_online, "is_available": profile.is_available})


def _orders_active_assignment_id(request):
    authorization = request.headers.get("Authorization", "")
    access_token = request.COOKIES.get("access_token")
    if not authorization and access_token:
        authorization = "Bearer " + access_token

    try:
        with httpx.Client(timeout=5.0, follow_redirects=False) as client:
            response = client.get(
                f"{settings.ORDERS_SERVICE_URL}/internal/v1/drivers/active-assignment/",
                headers={"Authorization": authorization},
            )
    except httpx.RequestError as exc:
        logger.error("Driver assignment lookup failed: %s", type(exc).__name__)
        return None, Response(
            {"detail": "Active driver assignment could not be verified."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if response.status_code != status.HTTP_200_OK:
        logger.error("Driver assignment lookup returned HTTP %s", response.status_code)
        return None, Response(
            {"detail": "Active driver assignment could not be verified."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    try:
        payload = response.json()
    except ValueError:
        logger.error("Driver assignment lookup returned invalid JSON")
        return None, Response(
            {"detail": "Active driver assignment could not be verified."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if not isinstance(payload, dict) or set(payload) != {"assignment"}:
        logger.error("Driver assignment lookup returned an invalid response")
        return None, Response(
            {"detail": "Active driver assignment could not be verified."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    assignment = payload["assignment"]
    if assignment is None:
        return None, None
    if (
        not isinstance(assignment, dict)
        or set(assignment) != {"work_type", "work_id"}
        or assignment["work_type"] not in {"FOOD_ORDER", "PARCEL"}
        or isinstance(assignment["work_id"], bool)
        or not isinstance(assignment["work_id"], int)
        or assignment["work_id"] <= 0
    ):
        logger.error("Driver assignment lookup returned an invalid assignment")
        return None, Response(
            {"detail": "Active driver assignment could not be verified."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return assignment["work_id"], None


class DriverLocationUpdateView(APIView):
    authentication_classes = [AccessTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = DriverLocationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        latitude = serializer.validated_data["latitude"]
        longitude = serializer.validated_data["longitude"]
        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            return Response(
                {"error": "Invalid coordinates range"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        active_order_id = None
        if settings.ORDERS_SERVICE_URL:
            active_order_id, error = _orders_active_assignment_id(request)
            if error:
                return error
        elif not settings.DEBUG:
            return Response(
                {"detail": "Active driver assignment could not be verified."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        recorded_at = timezone.now()
        with transaction.atomic():
            profile = (
                DriverProfile.objects.select_for_update()
                .filter(owner_user_id=request.user.id)
                .first()
            )
            if settings.ORDERS_SERVICE_URL:
                order_id = active_order_id
            else:
                order_id = profile.current_order_id if profile else None
            previous = (
                DriverLocationHistory.objects.select_for_update()
                .filter(owner_user_id=request.user.id)
                .first()
            )
            vehicle_type = profile.vehicle_type if profile else "MOTORCYCLE"
            static_latitude = round(latitude, 4)
            static_longitude = round(longitude, 4)
            static_window_start = recorded_at - timedelta(seconds=300)
            prior_static_count = DriverLocationHistory.objects.filter(
                owner_user_id=request.user.id,
                recorded_at__gte=static_window_start,
                recorded_at__lte=recorded_at,
                latitude__gte=static_latitude - 0.00005,
                latitude__lte=static_latitude + 0.00005,
                longitude__gte=static_longitude - 0.00005,
                longitude__lte=static_longitude + 0.00005,
            ).count()
            anomalies = detect_location_anomalies(
                latitude=latitude,
                longitude=longitude,
                vehicle_type=vehicle_type,
                recorded_at=recorded_at,
                previous=previous,
                prior_static_count=prior_static_count,
            )
            DriverLocation.objects.update_or_create(
                owner_user_id=request.user.id,
                defaults={"latitude": latitude, "longitude": longitude},
            )
            DriverLocationHistory.objects.create(
                owner_user_id=request.user.id,
                active_order_id=order_id,
                latitude=latitude,
                longitude=longitude,
                recorded_at=recorded_at,
            )
            for anomaly in anomalies:
                created_anomaly = DriverLocationAnomaly.objects.create(
                    owner_user_id=request.user.id,
                    active_order_id=order_id,
                    anomaly_type=anomaly["type"],
                    severity=anomaly["severity"],
                    details=anomaly["details"],
                    reported_latitude=latitude,
                    reported_longitude=longitude,
                    previous_latitude=anomaly.get("previous_latitude"),
                    previous_longitude=anomaly.get("previous_longitude"),
                    reported_speed_kmh=anomaly.get("reported_speed_kmh"),
                    max_allowed_speed_kmh=anomaly.get("max_allowed_speed_kmh"),
                    distance_km=anomaly.get("distance_km"),
                    time_delta_seconds=anomaly.get("time_delta_seconds"),
                    created_at=recorded_at,
                )
                transaction.on_commit(
                    lambda created_anomaly=created_anomaly: forward_anomaly_alert(
                        created_anomaly
                    )
                )
        if not publish_driver_location_events(
            request.user.id,
            order_id,
            latitude,
            longitude,
        ):
            return Response(
                {"error": "Location event could not be published", "success": False},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if is_critical_location_anomaly(anomalies):
            return Response(
                {
                    "error": "Location rejected: impossible travel detected",
                    "success": False,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        response_data = {"success": True, "buffered": True}
        if anomalies:
            response_data["anomalies_detected"] = len(anomalies)
            response_data["anomaly_types"] = [anomaly["type"] for anomaly in anomalies]
        return Response(response_data)


class DriverLocationHistoryPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100


class DriverLocationHistoryView(APIView):
    authentication_classes: ClassVar[list] = [AccessTokenAuthentication]
    permission_classes: ClassVar[list] = [permissions.IsAuthenticated]
    pagination_class = DriverLocationHistoryPagination

    def get(self, request):
        history = DriverLocationHistory.objects.filter(
            owner_user_id=request.user.id
        ).order_by("-recorded_at", "-id")
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(history, request, view=self)
        results = [
            {
                "id": item.id,
                "active_order_id": item.active_order_id,
                "latitude": item.latitude,
                "longitude": item.longitude,
                "recorded_at": item.recorded_at.isoformat(),
            }
            for item in page
        ]
        return paginator.get_paginated_response(results)


class AvailableDriverDemandView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    def get(self, request):
        supplied_token = request.headers.get("X-Demand-Sync-Token", "")
        if not settings.DEMAND_SYNC_TOKEN or not hmac.compare_digest(
            supplied_token,
            settings.DEMAND_SYNC_TOKEN,
        ):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        available_user_ids = list(
            DriverProfile.objects.filter(
                verification_status="VERIFIED",
                is_available=True,
            ).values_list("owner_user_id", flat=True)
        )
        active_driver_count = 0
        identity_url = settings.IDENTITY_SERVICE_URL.rstrip("/")
        try:
            with httpx.Client(timeout=5.0) as client:
                for offset in range(0, len(available_user_ids), 1000):
                    batch = available_user_ids[offset : offset + 1000]
                    response = client.post(
                        f"{identity_url}/internal/v1/identity/active-users/",
                        json={"user_ids": batch},
                        headers={"X-Demand-Sync-Token": settings.DEMAND_SYNC_TOKEN},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("active_user_ids"), list
                    ):
                        raise TypeError("Invalid active-user response.")
                    active_user_ids = payload["active_user_ids"]
                    if any(type(user_id) is not int for user_id in active_user_ids):
                        raise ValueError("Invalid active-user response.")
                    if not set(active_user_ids).issubset(batch):
                        raise ValueError("Unexpected active-user ID.")
                    active_driver_count += len(set(active_user_ids))
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return Response(
                {"detail": "Identity service unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"available_drivers": active_driver_count})
