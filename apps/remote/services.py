"""Operator/admin service actions on remote-domain models.

Shared by management commands AND the Django admin so both operator entry
points run the SAME verified state transitions and emit the SAME append-only
:class:`~apps.operations.models.SystemEvent` audit rows. Keeping the logic here
(rather than inside a command's handler) is what lets the admin reuse it without
duplicating — and possibly drifting from — the canonical behaviour.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.core.enums import EventCategory, ProviderControlState, SubmissionState
from apps.operations.models import SystemEvent
from apps.remote.models import (
    RemoteProviderControl,
    RemoteSubmission,
    RemoteSubmissionMember,
)


def reset_provider_auth(provider: str = "wishart", actor: str = "admin") -> SystemEvent:
    """Re-enable an ``AUTH_BLOCKED`` remote provider after credentials are fixed.

    Flips the durable :class:`RemoteProviderControl` row back to ``ENABLED`` and
    clears the block reason so every worker resumes calling the provider. Always
    records a ``provider_auth_reset`` SystemEvent (audit), which is why both
    ``manage.py reset_provider_auth`` and the admin action call this.
    """
    RemoteProviderControl.objects.update_or_create(
        provider=provider,
        defaults={
            "state": ProviderControlState.ENABLED,
            "blocked_reason": "",
        },
    )
    return SystemEvent.objects.create(
        category=EventCategory.REMOTE,
        event_type="provider_auth_reset",
        payload={"provider": provider, "actor": actor},
    )


def fail_unknown_submission(
    submission: RemoteSubmission, actor: str = "admin"
) -> None:
    """Move an in-doubt ``UNKNOWN`` submission to ``FAILED``, freeing its key.

    The submission stops holding the idempotency slot, so its active per-member
    reservations (R-01) are released in the SAME transaction — a genuine re-POST
    of those members may now occur. Refuses any submission not in ``UNKNOWN``.
    Records a ``submission_marked_failed`` SystemEvent (audit). Shared by
    ``manage.py resolve_submission --mark-failed`` and the admin action.
    """
    if submission.state != SubmissionState.UNKNOWN:
        raise ValueError(
            f"Submission {submission.pk} is {submission.state}, not UNKNOWN; "
            "only in-doubt UNKNOWN intents may be marked failed."
        )
    with transaction.atomic():
        submission.state = SubmissionState.FAILED
        submission.detail = (
            "operator verified provider did NOT accept "
            "(idempotency key freed)"
        )
        submission.save(update_fields=["state", "detail", "updated_at"])
        RemoteSubmissionMember.objects.filter(
            submission=submission, active=True
        ).update(
            active=False,
            released_at=timezone.now(),
            release_reason="operator_mark_failed",
        )
        SystemEvent.objects.create(
            category=EventCategory.REMOTE,
            event_type="submission_marked_failed",
            payload={"submission_id": submission.pk, "actor": actor},
        )
