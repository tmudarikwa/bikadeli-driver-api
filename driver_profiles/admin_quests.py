import hmac
from decimal import Decimal, InvalidOperation
from typing import ClassVar

from django.conf import settings
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .quest_models import QUEST_TYPE_CHOICES, DriverQuest, QuestTemplate

PAGE_SIZE = 20
VALID_TYPES = [choice[0] for choice in QUEST_TYPE_CHOICES]


class QuestValidationError(ValueError):
    pass


def normalize_milestone_schedule(raw_schedule, target_value, reward_amount, reward_currency):
    """Validate a milestone schedule; an empty schedule means a single-reward quest."""
    if raw_schedule is None or raw_schedule == []:
        return []
    if not isinstance(raw_schedule, list):
        raise QuestValidationError("milestones must be a list")
    try:
        target = int(target_value)
    except (TypeError, ValueError) as exc:
        raise QuestValidationError("target_value must be an integer") from exc
    if target <= 0:
        raise QuestValidationError("target_value must be at least 1")
    try:
        parent_total = Decimal(str(reward_amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise QuestValidationError("reward_amount must be a number") from exc
    if parent_total < 0:
        raise QuestValidationError("reward_amount must be at least 0")
    parent_currency = str(reward_currency or "USD").strip().upper()
    if len(parent_currency) != 3 or not parent_currency.isalpha():
        raise QuestValidationError("reward_currency must be a 3-letter code")

    cleaned = []
    total = Decimal("0.00")
    previous_trigger = 0
    for index, item in enumerate(raw_schedule, start=1):
        if not isinstance(item, dict):
            raise QuestValidationError(f"milestone {index} must be an object")
        unknown = set(item) - {"trigger_value", "reward_amount", "reward_currency"}
        if unknown:
            raise QuestValidationError(f"milestone {index} has unknown field(s): {sorted(unknown)}")
        raw_trigger = item.get("trigger_value")
        if isinstance(raw_trigger, bool):
            raise QuestValidationError(f"milestone {index}: trigger_value must be an integer")
        try:
            trigger = int(raw_trigger)
        except (TypeError, ValueError) as exc:
            raise QuestValidationError(f"milestone {index}: trigger_value must be an integer") from exc
        if trigger <= 0:
            raise QuestValidationError(f"milestone {index}: trigger_value must be at least 1")
        if trigger <= previous_trigger:
            raise QuestValidationError(f"milestone {index}: trigger_value must increase")
        try:
            amount = Decimal(str(item.get("reward_amount"))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise QuestValidationError(f"milestone {index}: reward_amount must be a number") from exc
        if amount < 0:
            raise QuestValidationError(f"milestone {index}: reward_amount must be at least 0")
        if amount > Decimal("999999.99"):
            raise QuestValidationError(f"milestone {index}: reward_amount is too large")
        currency = str(item.get("reward_currency") or parent_currency).strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise QuestValidationError(f"milestone {index}: reward_currency must be a 3-letter code")
        if currency != parent_currency:
            raise QuestValidationError(
                f"milestone {index}: reward_currency must match the quest currency {parent_currency}"
            )
        total += amount
        previous_trigger = trigger
        cleaned.append(
            {
                "level": index,
                "trigger_value": trigger,
                "reward_amount": amount,
                "reward_currency": currency,
            }
        )
    if cleaned[-1]["trigger_value"] != target:
        raise QuestValidationError("the final milestone trigger must equal target_value")
    if total != parent_total:
        raise QuestValidationError("milestone rewards must sum to reward_amount")
    return cleaned


def _stored_schedule(schedule):
    return [
        {
            "trigger_value": item["trigger_value"],
            "reward_amount": str(item["reward_amount"]),
            "reward_currency": item["reward_currency"],
        }
        for item in schedule
    ]


def _int(value, name):
    if isinstance(value, bool):
        raise QuestValidationError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise QuestValidationError(f"{name} must be an integer") from exc


def _serialize_template(t):
    return {
        "id": t.id,
        "name": t.name,
        "quest_type": t.quest_type,
        "title": t.title,
        "description": t.description,
        "target_value": t.target_value,
        "reward_amount": float(t.reward_amount),
        "reward_currency": t.reward_currency,
        "milestone_schedule": [
            {
                "trigger_value": int(item.get("trigger_value", 0)),
                "reward_amount": float(item.get("reward_amount", 0)),
                "reward_currency": item.get("reward_currency", t.reward_currency),
            }
            for item in (t.milestone_schedule or [])
        ],
        "default_rules": t.default_rules or {},
        "claim_window_days": t.claim_window_days,
        "on_time_window_minutes": t.on_time_window_minutes,
        "is_active": t.is_active,
        "created_at": t.created_at.isoformat(),
    }


class _QuestAdmin(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [permissions.AllowAny]

    def initial(self, request, *args, **kwargs):
        supplied = request.headers.get("X-Driver-Admin-Token", "")
        if not (
            settings.DRIVER_QUESTS_ADMIN_ENABLED
            and settings.DRIVER_ADMIN_TOKEN
            and hmac.compare_digest(supplied, settings.DRIVER_ADMIN_TOKEN)
        ):
            from rest_framework.exceptions import NotFound

            raise NotFound
        super().initial(request, *args, **kwargs)


class QuestTemplateListCreateView(_QuestAdmin):
    def get(self, request):
        qs = QuestTemplate.objects.order_by("name")
        if request.query_params.get("all") != "true":
            qs = qs.filter(is_active=True)
        return Response([_serialize_template(t) for t in qs])

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        name = str(data.get("name") or "").strip()
        if not name:
            return Response({"error": "Name is required"}, status=400)
        if QuestTemplate.objects.filter(name=name).exists():
            return Response({"error": "A template with this name already exists"}, status=400)
        quest_type = data.get("quest_type", "DELIVERIES")
        if quest_type not in VALID_TYPES:
            return Response(
                {"error": f"Invalid quest_type. Must be one of: {', '.join(VALID_TYPES)}"}, status=400
            )
        try:
            target = _int(data.get("target_value"), "target_value")
            if target < 1:
                raise QuestValidationError("target_value must be at least 1")
            reward_amount = data.get("reward_amount", 0)
            currency = data.get("reward_currency", "USD")
            schedule = normalize_milestone_schedule(
                data.get("milestone_schedule") or [], target, reward_amount, currency
            )
            claim_window = _int(data.get("claim_window_days", 7), "claim_window_days")
            on_time = _int(data.get("on_time_window_minutes", 45), "on_time_window_minutes")
            rules = data.get("default_rules") or {}
            if not isinstance(rules, dict):
                raise QuestValidationError("default_rules must be an object")
            template = QuestTemplate.objects.create(
                name=name,
                quest_type=quest_type,
                title=str(data.get("title") or "").strip() or name,
                description=data.get("description", ""),
                target_value=target,
                reward_amount=Decimal(str(reward_amount)).quantize(Decimal("0.01")),
                reward_currency=str(currency).strip().upper(),
                milestone_schedule=_stored_schedule(schedule),
                default_rules=rules,
                claim_window_days=claim_window,
                on_time_window_minutes=on_time,
                is_active=bool(data.get("is_active", True)),
            )
        except (QuestValidationError, InvalidOperation) as exc:
            return Response({"error": str(exc)}, status=400)
        return Response(_serialize_template(template), status=201)


class QuestTemplateUpdateView(_QuestAdmin):
    def post(self, request, template_id):
        template = QuestTemplate.objects.filter(pk=template_id).first()
        if template is None:
            return Response({"error": "Template not found"}, status=404)
        data = request.data if isinstance(request.data, dict) else {}
        name = str(data.get("name") or "").strip()
        if name and name != template.name:
            if QuestTemplate.objects.filter(name=name).exclude(pk=template_id).exists():
                return Response({"error": "A template with this name already exists"}, status=400)
            template.name = name
        if data.get("quest_type"):
            if data["quest_type"] not in VALID_TYPES:
                return Response(
                    {"error": f"Invalid quest_type. Must be one of: {', '.join(VALID_TYPES)}"},
                    status=400,
                )
            template.quest_type = data["quest_type"]
        try:
            for field in ("title", "description"):
                if field in data:
                    setattr(template, field, str(data[field]))
            if "target_value" in data:
                template.target_value = _int(data["target_value"], "target_value")
            if "reward_amount" in data:
                template.reward_amount = Decimal(str(data["reward_amount"])).quantize(Decimal("0.01"))
            if "reward_currency" in data:
                template.reward_currency = str(data["reward_currency"]).strip().upper()
            if "milestone_schedule" in data or any(
                key in data for key in ("target_value", "reward_amount", "reward_currency")
            ):
                schedule = normalize_milestone_schedule(
                    data.get("milestone_schedule", template.milestone_schedule or []),
                    template.target_value,
                    template.reward_amount,
                    template.reward_currency,
                )
                template.milestone_schedule = _stored_schedule(schedule)
            if "default_rules" in data:
                rules = data["default_rules"] or {}
                if not isinstance(rules, dict):
                    raise QuestValidationError("default_rules must be an object")
                template.default_rules = rules
            if "claim_window_days" in data:
                template.claim_window_days = _int(data["claim_window_days"], "claim_window_days")
            if "on_time_window_minutes" in data:
                template.on_time_window_minutes = _int(data["on_time_window_minutes"], "on_time_window_minutes")
            if "is_active" in data:
                template.is_active = bool(data["is_active"])
            template.save()
        except (QuestValidationError, InvalidOperation) as exc:
            return Response({"error": str(exc)}, status=400)
        return Response(_serialize_template(template))


class QuestTemplateDeleteView(_QuestAdmin):
    def post(self, request, template_id):
        deleted, _ = QuestTemplate.objects.filter(pk=template_id).delete()
        if not deleted:
            return Response({"error": "Template not found"}, status=404)
        return Response({"message": "Template deleted"})


class QuestListView(_QuestAdmin):
    def get(self, request):
        raw = request.query_params.get("page", "1")
        if not raw.isdigit() or int(raw) < 1:
            return Response({"error": "Invalid page number"}, status=400)
        page = int(raw)
        params = request.query_params
        qs = DriverQuest.objects.prefetch_related("milestones").order_by("-created_at", "-id")
        if params.get("quest_type"):
            qs = qs.filter(quest_type=params["quest_type"])
        if params.get("is_completed") in {"true", "false"}:
            qs = qs.filter(is_completed=params["is_completed"] == "true")
        if params.get("driver_id"):
            if not params["driver_id"].isdigit():
                return Response({"error": "Invalid driver_id"}, status=400)
            qs = qs.filter(driver_user_id=int(params["driver_id"]))
        total = qs.count()
        start = (page - 1) * PAGE_SIZE
        return Response(
            {
                "results": [self._row(q) for q in qs[start : start + PAGE_SIZE]],
                "total": total,
                "page": page,
                "pages": (total + PAGE_SIZE - 1) // PAGE_SIZE if total else 1,
                "page_size": PAGE_SIZE,
            }
        )

    @staticmethod
    def _row(q):
        milestones = list(q.milestones.all())
        released = sum((m.reward_amount for m in milestones if m.status == "RELEASED"), Decimal(0))
        pct = 0 if q.target_value == 0 else min(100, int(q.current_value / q.target_value * 100))
        return {
            "id": q.id,
            "driver_id": q.driver_user_id,
            "driver_name": f"Driver #{q.driver_user_id}",
            "driver_email": None,
            "quest_type": q.quest_type,
            "title": q.title,
            "description": q.description,
            "target_value": q.target_value,
            "current_value": q.current_value,
            "progress_pct": pct,
            "reward_amount": float(q.reward_amount),
            "reward_currency": q.reward_currency,
            "is_completed": q.is_completed,
            "is_claimed": q.is_claimed,
            "starts_at": q.starts_at.isoformat(),
            "expires_at": q.expires_at.isoformat(),
            "completed_at": q.completed_at.isoformat() if q.completed_at else None,
            "created_at": q.created_at.isoformat(),
            "eligibility_rules": q.eligibility_rules or {},
            # Live eligibility needs order and earnings metrics that this service does not own.
            "is_eligible": None,
            "ineligibility_reasons": [],
            "claim_window_days": q.claim_window_days,
            "on_time_window_minutes": q.on_time_window_minutes,
            "has_milestones": bool(milestones),
            "milestones": [
                {
                    "id": m.id,
                    "level": m.level,
                    "trigger_value": m.trigger_value,
                    "reward_amount": float(m.reward_amount),
                    "reward_currency": m.reward_currency,
                    "status": m.status,
                    "released_at": m.released_at.isoformat() if m.released_at else None,
                }
                for m in milestones
            ],
            "released_amount": float(released),
            "remaining_amount": float(q.reward_amount - released),
        }
