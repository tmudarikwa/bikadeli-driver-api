from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from driver_profiles.models import DriverQuest, DriverQuestMilestone, QuestTemplate
from rest_framework.test import APIClient

TOKEN = "q" * 40
AUTH = {"HTTP_X_DRIVER_ADMIN_TOKEN": TOKEN}
BASE = "/internal/v1/admin/drivers/quests/"


@override_settings(DRIVER_QUESTS_ADMIN_ENABLED=True, DRIVER_ADMIN_TOKEN=TOKEN)
class QuestTemplateTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def create(self, **extra):
        body = {"name": "Weekend", "quest_type": "DELIVERIES", "target_value": 10, "reward_amount": "5.00", **extra}
        return self.client.post(BASE + "templates/", body, format="json", **AUTH)

    def test_gate_and_token(self):
        self.assertEqual(self.client.get(BASE + "templates/").status_code, 404)
        with override_settings(DRIVER_QUESTS_ADMIN_ENABLED=False):
            self.assertEqual(self.client.get(BASE + "templates/", **AUTH).status_code, 404)

    def test_create_list_update_delete(self):
        response = self.create(milestone_schedule=[
            {"trigger_value": 5, "reward_amount": "2.00"},
            {"trigger_value": 10, "reward_amount": "3.00"},
        ])
        self.assertEqual(response.status_code, 201)
        template_id = response.json()["id"]
        self.assertEqual(response.json()["title"], "Weekend")
        self.assertEqual(len(self.client.get(BASE + "templates/", **AUTH).json()), 1)

        update = self.client.post(
            f"{BASE}templates/{template_id}/update/",
            {"is_active": False, "milestone_schedule": []},
            format="json",
            **AUTH,
        )
        self.assertEqual(update.status_code, 200)
        self.assertEqual(self.client.get(BASE + "templates/", **AUTH).json(), [])
        self.assertEqual(len(self.client.get(BASE + "templates/?all=true", **AUTH).json()), 1)

        self.assertEqual(self.client.post(f"{BASE}templates/{template_id}/delete/", **AUTH).status_code, 200)
        self.assertEqual(self.client.post(f"{BASE}templates/{template_id}/delete/", **AUTH).status_code, 404)

    def test_validation(self):
        self.assertEqual(self.create(name="").status_code, 400)
        self.assertEqual(self.create(quest_type="BAD").status_code, 400)
        self.assertEqual(self.create(target_value=0).status_code, 400)
        bad_sum = [{"trigger_value": 10, "reward_amount": "1.00"}]
        self.assertEqual(self.create(milestone_schedule=bad_sum).status_code, 400)
        not_final = [{"trigger_value": 5, "reward_amount": "5.00"}]
        self.assertEqual(self.create(milestone_schedule=not_final).status_code, 400)
        self.assertEqual(self.create().status_code, 201)
        self.assertEqual(self.create().status_code, 400)
        self.assertEqual(QuestTemplate.objects.count(), 1)

    def test_update_rename_conflict_and_missing(self):
        self.create()
        other = self.create(name="Other").json()["id"]
        conflict = self.client.post(f"{BASE}templates/{other}/update/", {"name": "Weekend"}, format="json", **AUTH)
        self.assertEqual(conflict.status_code, 400)
        missing = self.client.post(f"{BASE}templates/999/update/", {}, format="json", **AUTH)
        self.assertEqual(missing.status_code, 404)


@override_settings(DRIVER_QUESTS_ADMIN_ENABLED=True, DRIVER_ADMIN_TOKEN=TOKEN)
class QuestListTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        now = timezone.now()
        quest = DriverQuest.objects.create(
            driver_user_id=7, quest_type="DELIVERIES", title="Ten", target_value=10, current_value=5,
            reward_amount="5.00", starts_at=now, expires_at=now + timedelta(days=7),
        )
        DriverQuestMilestone.objects.create(quest=quest, level=1, trigger_value=5, reward_amount="2.00", status="RELEASED")
        DriverQuestMilestone.objects.create(quest=quest, level=2, trigger_value=10, reward_amount="3.00")
        DriverQuest.objects.create(
            driver_user_id=8, quest_type="STREAK", title="Streak", target_value=3, is_completed=True,
            starts_at=now, expires_at=now + timedelta(days=7),
        )

    def test_list_filters_and_amounts(self):
        body = self.client.get(BASE, {"driver_id": 7}, **AUTH).json()
        self.assertEqual(body["total"], 1)
        row = body["results"][0]
        self.assertEqual(row["progress_pct"], 50)
        self.assertEqual(row["released_amount"], 2.0)
        self.assertEqual(row["remaining_amount"], 3.0)
        self.assertEqual(self.client.get(BASE, {"is_completed": "true"}, **AUTH).json()["total"], 1)
        self.assertEqual(self.client.get(BASE, {"quest_type": "DELIVERIES"}, **AUTH).json()["total"], 1)

    def test_invalid_params(self):
        self.assertEqual(self.client.get(BASE, {"page": "0"}, **AUTH).status_code, 400)
        self.assertEqual(self.client.get(BASE, {"driver_id": "x"}, **AUTH).status_code, 400)
