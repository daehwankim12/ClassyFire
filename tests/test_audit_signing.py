from __future__ import annotations

import pytest
from django.test import override_settings


@pytest.mark.django_db(transaction=True)
@override_settings(AUDIT_SIGNING_KEY="audit-test-secret")
def test_signed_system_event_verifies_and_detects_tampering():
    from apps.core.enums import EventCategory
    from apps.operations.audit import sign_system_event, verify_system_event_signature
    from apps.operations.models import SystemEvent

    event = SystemEvent.objects.create(
        category=EventCategory.SECURITY,
        event_type="remote_pause",
        payload={"actor": "alice"},
    )

    signature = sign_system_event(event, key_id="test-key")

    assert signature.signature
    assert verify_system_event_signature(event) is True

    event.payload = {"actor": "mallory"}
    event.save(update_fields=["payload"])

    assert verify_system_event_signature(event) is False


@pytest.mark.django_db(transaction=True)
@override_settings(AUDIT_SIGNING_KEY="audit-test-secret")
def test_system_event_create_is_signed_automatically():
    from apps.core.enums import EventCategory
    from apps.operations.audit import verify_system_event_signature
    from apps.operations.models import SystemEvent

    event = SystemEvent.objects.create(
        category=EventCategory.WORKER,
        event_type="retry_deferred_tasks",
        payload={"actor": "admin", "count": 3},
    )

    assert hasattr(event, "audit_signature")
    assert verify_system_event_signature(event) is True
