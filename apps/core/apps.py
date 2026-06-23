"""App configuration for core."""
from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"

    def ready(self) -> None:
        # Register the ``__length`` lookup on text fields so CHAR(N) check
        # constraints can express ``char_length(col) = N`` portably. This is
        # what enforces the exact 27/14-char InChIKey invariants at the DB level.
        from django.db.models import CharField, TextField
        from django.db.models.functions import Length

        CharField.register_lookup(Length)
        TextField.register_lookup(Length)
