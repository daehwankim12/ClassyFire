from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote", "0010_provider_auth_blocked"),
    ]

    operations = [
        migrations.CreateModel(
            name="RemoteQueueControl",
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
                    "provider",
                    models.CharField(default="wishart", max_length=32, unique=True),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "remote_queue_controls",
            },
        ),
    ]
