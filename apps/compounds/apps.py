"""App configuration for compounds."""
from django.apps import AppConfig


class CompoundsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.compounds"
    label = "compounds"
