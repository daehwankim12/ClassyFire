from __future__ import annotations

import pytest


def test_production_requires_positive_remote_budget(settings):
    settings.REMOTE_DAILY_BUDGET_PER_PRINCIPAL = 0
    settings.DEBUG = False
    from apps.remote.budget import validate_remote_budget_settings

    with pytest.raises(RuntimeError, match="positive remote budget"):
        validate_remote_budget_settings()


def test_development_may_disable_remote_budget(settings):
    settings.REMOTE_DAILY_BUDGET_PER_PRINCIPAL = -1
    settings.DEBUG = True
    from apps.remote.budget import validate_remote_budget_settings

    validate_remote_budget_settings()
