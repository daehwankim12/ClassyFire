from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from django.conf import settings

from apps.operations.models import AuditSignature, SystemEvent


def _signing_key() -> bytes:
    configured = getattr(settings, "AUDIT_SIGNING_KEY", "") or settings.SECRET_KEY
    return str(configured).encode("utf-8")


def _canonical_event_payload(event: SystemEvent) -> bytes:
    payload: dict[str, Any] = {
        "event_id": event.pk,
        "category": event.category,
        "event_type": event.event_type,
        "payload": event.payload,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _payload_digest(event: SystemEvent) -> str:
    return hashlib.sha256(_canonical_event_payload(event)).hexdigest()


def _signature(event: SystemEvent) -> str:
    return hmac.new(
        _signing_key(),
        _canonical_event_payload(event),
        hashlib.sha256,
    ).hexdigest()


def sign_system_event(
    event: SystemEvent,
    *,
    key_id: str = "default",
) -> AuditSignature:
    """Create or replace the HMAC signature for a SystemEvent."""
    digest = _payload_digest(event)
    signature = _signature(event)
    audit_signature, _created = AuditSignature.objects.update_or_create(
        event=event,
        defaults={
            "key_id": key_id,
            "payload_sha256": digest,
            "signature": signature,
        },
    )
    return audit_signature


def verify_system_event_signature(event: SystemEvent) -> bool:
    """Return True when an event still matches its stored signature."""
    try:
        audit_signature = event.audit_signature
    except AuditSignature.DoesNotExist:
        return False

    digest_matches = hmac.compare_digest(
        audit_signature.payload_sha256,
        _payload_digest(event),
    )
    signature_matches = hmac.compare_digest(
        audit_signature.signature,
        _signature(event),
    )
    return digest_matches and signature_matches
