from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote", "0009_remote_query_item_outcome"),
    ]

    operations = [
        migrations.CreateModel(
            name="RemoteProviderControl",
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
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("ENABLED", "Enabled"),
                            ("AUTH_BLOCKED", "Auth blocked"),
                        ],
                        default="ENABLED",
                        max_length=24,
                    ),
                ),
                ("blocked_reason", models.TextField(blank=True, null=True)),
                (
                    "credential_version",
                    models.CharField(blank=True, max_length=128, null=True),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "remote_provider_controls",
            },
        ),
    ]
