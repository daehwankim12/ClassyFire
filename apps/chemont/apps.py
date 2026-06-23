"""App configuration for chemont."""
from django.apps import AppConfig


class ChemontConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.chemont"
    label = "chemont"
