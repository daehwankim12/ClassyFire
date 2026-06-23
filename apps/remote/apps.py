"""App configuration for remote."""
from django.apps import AppConfig


class RemoteConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.remote"
    label = "remote"
