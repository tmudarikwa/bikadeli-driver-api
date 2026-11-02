from django.db import migrations

import driver_profiles.fields


class Migration(migrations.Migration):

    dependencies = [
        ("driver_profiles", "0002_driverlocation"),
    ]

    operations = [
        migrations.AddField(
            model_name="driverprofile",
            name="license_number",
            field=driver_profiles.fields.EncryptedCharField(
                blank=True, default="", max_length=50
            ),
        ),
    ]
