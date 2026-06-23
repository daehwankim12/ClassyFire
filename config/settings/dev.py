"""Development settings."""
from pathlib import Path

from .base import *  # noqa: F403

DEBUG = True

ALLOWED_HOSTS = ["*"]

INTERNAL_IPS = ["127.0.0.1"]

# Serve static files from the project's static/ directory during development
STATICFILES_DIRS = [Path(__file__).resolve().parent.parent.parent / "static"]
