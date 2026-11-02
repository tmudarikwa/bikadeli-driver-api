import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("driver_profiles", "0005_driverkycauditlog"),
    ]

    operations = [
        migrations.CreateModel(
            name="DriverLocationHistory",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "source_log_id",
                    models.PositiveBigIntegerField(blank=True, null=True, unique=True),
                ),
                ("owner_user_id", models.PositiveBigIntegerField(db_index=True)),
                (
                    "active_order_id",
                    models.PositiveBigIntegerField(blank=True, db_index=True, null=True),
                ),
                ("latitude", models.FloatField()),
                ("longitude", models.FloatField()),
                (
                    "recorded_at",
                    models.DateTimeField(db_index=True, default=django.utils.timezone.now),
                ),
            ],
            options={
                "ordering": ["-recorded_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["owner_user_id", "-recorded_at"],
                        name="driver_loc_hist_owner_time",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("latitude__gte", -90), ("latitude__lte", 90)
                        ),
                        name="driver_hist_latitude_range",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("longitude__gte", -180), ("longitude__lte", 180)
                        ),
                        name="driver_hist_longitude_range",
                    ),
                ],
            },
        ),
    ]
