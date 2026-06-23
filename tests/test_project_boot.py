def test_django_settings_load(settings):
    assert settings.ROOT_URLCONF == "config.urls"
    assert "apps.batches" in settings.INSTALLED_APPS
    assert "apps.remote" in settings.INSTALLED_APPS
