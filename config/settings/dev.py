"""Development settings."""

from .base import *  # noqa: F403

DEBUG = True

ALLOWED_HOSTS = ["*"]

INTERNAL_IPS = ["127.0.0.1"]

# H-05: base.py defaults to 100 (prod-safe), but dev/test must stay unlimited
# so that the test suite can enqueue tasks freely without hitting the cap.
# pytest uses config.settings.dev (pyproject.toml DJANGO_SETTINGS_MODULE), so
# this single line protects the whole test suite from the new base default.
REMOTE_DAILY_BUDGET_PER_PRINCIPAL = -1
