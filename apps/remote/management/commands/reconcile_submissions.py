"""``manage.py reconcile_submissions`` — operator tool for in-doubt intents (F-06).

A ``RemoteSubmission`` left in ``PENDING`` after a crash between the provider
POST and its local commit is "in-doubt": the POST may or may not have reached the
provider. The worker ages such intents into ``UNKNOWN`` automatically (cap / TTL),
but this command lets an operator inspect them and explicitly PARK a stuck
``PENDING`` intent as ``UNKNOWN``.

UNKNOWN is a LIVE state — it HOLDS the ``(provider, payload_hash)`` idempotency
key — so parking NEVER frees the key and the worker NEVER auto-resubmits the same
payload. The ONLY command path that frees a key is ``resolve_submission
--mark-failed``, which requires the submission to already be ``UNKNOWN`` and
records the operator's verified non-acceptance decision.

Usage::

    # List in-doubt PENDING intents (default).
    manage.py reconcile_submissions

    # Move a specific in-doubt PENDING intent to UNKNOWN (key stays HELD).
    manage.py reconcile_submissions --park-unknown <submission_id>
"""
from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.enums import SubmissionState
from apps.remote.models import RemoteSubmission


class Command(BaseCommand):
    help = "List or park in-doubt PENDING remote submission intents as UNKNOWN."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--park-unknown",
            type=int,
            default=None,
            metavar="ID",
            help="Move a PENDING in-doubt submission to UNKNOWN without freeing its key.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        pending = RemoteSubmission.objects.filter(state=SubmissionState.PENDING)

        if options["park_unknown"] is not None:
            self._park_unknown(options["park_unknown"])
            return
        self._list(pending)

    def _list(self, pending: Any) -> None:
        rows = list(pending.order_by("created_at"))
        if not rows:
            self.stdout.write(self.style.SUCCESS("No in-doubt PENDING submissions."))
            return
        self.stdout.write(f"In-doubt PENDING submissions ({len(rows)}):")
        for sub in rows:
            age = (timezone.now() - sub.created_at).total_seconds()
            self.stdout.write(
                f"  id={sub.pk} provider={sub.provider} "
                f"hash={sub.payload_hash[:12]} attempts={sub.reconcile_attempts} "
                f"age={age:.0f}s"
            )

    def _park_unknown(self, submission_id: int) -> None:
        try:
            sub = RemoteSubmission.objects.get(
                pk=submission_id, state=SubmissionState.PENDING
            )
        except RemoteSubmission.DoesNotExist as exc:
            raise CommandError(
                f"No PENDING submission with id={submission_id}."
            ) from exc
        sub.state = SubmissionState.UNKNOWN
        sub.detail = "operator parked PENDING intent as UNKNOWN via reconcile_submissions"
        sub.save(update_fields=["state", "detail", "updated_at"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Submission {submission_id} -> UNKNOWN (idempotency key held)."
            )
        )
