from django.db import models

QUEST_TYPE_CHOICES = [
    ("DELIVERIES", "Complete deliveries"),
    ("EARNINGS", "Earn amount"),
    ("RATING", "Maintain rating"),
    ("ON_TIME", "On-time deliveries"),
    ("STREAK", "Consecutive days"),
]


class QuestTemplate(models.Model):
    name = models.CharField(max_length=200, unique=True)
    quest_type = models.CharField(max_length=20, choices=QUEST_TYPE_CHOICES)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    target_value = models.IntegerField()
    reward_amount = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    reward_currency = models.CharField(max_length=3, default="USD")
    milestone_schedule = models.JSONField(default=list, blank=True)
    default_rules = models.JSONField(default=dict, blank=True)
    claim_window_days = models.IntegerField(default=7)
    on_time_window_minutes = models.IntegerField(default=45)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]


class DriverQuest(models.Model):
    source_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    driver_user_id = models.PositiveBigIntegerField(db_index=True)
    quest_type = models.CharField(max_length=20, choices=QUEST_TYPE_CHOICES)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    target_value = models.IntegerField()
    current_value = models.IntegerField(default=0)
    reward_amount = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    reward_currency = models.CharField(max_length=3, default="USD")
    is_completed = models.BooleanField(default=False)
    is_claimed = models.BooleanField(default=False)
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    eligibility_rules = models.JSONField(default=dict, blank=True)
    claim_window_days = models.IntegerField(default=7)
    on_time_window_minutes = models.IntegerField(default=45)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["driver_user_id", "is_completed"])]


class DriverQuestMilestone(models.Model):
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("RELEASED", "Released"),
        ("FORFEITED", "Forfeited"),
    ]

    quest = models.ForeignKey(DriverQuest, on_delete=models.CASCADE, related_name="milestones")
    level = models.PositiveIntegerField()
    trigger_value = models.IntegerField()
    reward_amount = models.DecimalField(max_digits=8, decimal_places=2)
    reward_currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PENDING")
    released_earning_id = models.PositiveBigIntegerField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["level"]
        constraints = [
            models.UniqueConstraint(fields=["quest", "level"], name="drv_quest_milestone_level_uniq"),
            models.UniqueConstraint(fields=["quest", "trigger_value"], name="drv_quest_milestone_trigger_uniq"),
        ]
