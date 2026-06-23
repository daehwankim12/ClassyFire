"""Typed environment-variable helpers for Django settings.

Each helper reads from ``os.environ`` (python-dotenv may pre-populate that
dict in dev).  All helpers are pure functions with no side-effects, which
makes them straightforward to unit-test via monkeypatching.

Usage example::

    from apps.core.settings import env_str, env_int, env_bool, env_required

    SECRET_KEY = env_required("DJANGO_SECRET_KEY")
    DEBUG = env_bool("DJANGO_DEBUG", default=False)
    PORT = env_int("APP_PORT", default=8000)
"""

from __future__ import annotations

import os

from django.core.exceptions import ImproperlyConfigured

__all__ = [
    "env_bool",
    "env_float",
    "env_int",
    "env_required",
    "env_str",
]


def env_str(name: str, default: str) -> str:
    """Return the string value of *name* from the environment, or *default*."""
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    """Return the int value of *name* from the environment, or *default*."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"Environment variable {name!r} must be an integer; got {raw!r}."
        ) from exc


def env_bool(name: str, default: bool) -> bool:
    """Return the bool value of *name* from the environment, or *default*.

    Truthy strings: ``"true"``, ``"1"``, ``"yes"`` (case-insensitive).
    Falsy strings: ``"false"``, ``"0"``, ``"no"`` (case-insensitive).
    Any other value raises ``ImproperlyConfigured``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalised = raw.strip().lower()
    if normalised in {"true", "1", "yes"}:
        return True
    if normalised in {"false", "0", "no"}:
        return False
    raise ImproperlyConfigured(
        f"Environment variable {name!r} must be a boolean ('true'/'false'); got {raw!r}."
    )


def env_float(name: str, default: float) -> float:
    """Return the float value of *name* from the environment, or *default*."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"Environment variable {name!r} must be a float; got {raw!r}."
        ) from exc


def env_required(name: str) -> str:
    """Return the string value of *name* from the environment.

    Raises :exc:`django.core.exceptions.ImproperlyConfigured` when the
    variable is absent *or* set to an empty string.  Use this helper for
    secrets and other values that must be supplied explicitly in production.
    """
    value = os.environ.get(name, "")
    if not value.strip():
        raise ImproperlyConfigured(
            f"Required environment variable {name!r} is missing or empty. "
            "Set it in .env or as a real environment variable."
        )
    return value
