"""``manage.py reconcile_submissions`` — operator tool for in-doubt intents (F-06).

A ``RemoteSubmission`` left in ``PENDING`` after a crash between the provider
POST and its local commit is "in-doubt": the POST may or may not have reached the
provider. The worker ages such intents out automatically (cap / TTL), but this
command lets an operator inspect and force-resolve them.

Usage::

    # List in-doubt PENDING intents (default).
    manage.py reconcile_submissions

    # Force a specific in-doubt intent to FAILED (frees the idempotency key so a
    # single genuine re-POST can occur on the next worker tick).
    manage.py reconcile_submissions --fail <submission_id>

    # Force every in-doubt PENDING intent older than the TTL to FAILED.
    manage.py reconcile_submissions --fail-aged
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.enums import SubmissionState
from apps.remote.models import RemoteSubmission


class Command(BaseCommand):
    help = "List or force-resolve in-doubt PENDING remote submission intents."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--fail",
            type=int,
            default=None,
            metavar="ID",
            help="Force the given PENDING submission id to FAILED (frees its key).",
        )
        parser.add_argument(
            "--fail-aged",
            action="store_true",
            help="Force every PENDING intent older than the TTL to FAILED.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        pending = RemoteSubmission.objects.filter(state=SubmissionState.PENDING)

        if options["fail"] is not None:
            self._fail_one(options["fail"])
            return
        if options["fail_aged"]:
            self._fail_aged(pending)
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

    def _fail_one(self, submission_id: int) -> None:
        try:
            sub = RemoteSubmission.objects.get(
                pk=submission_id, state=SubmissionState.PENDING
            )
        except RemoteSubmission.DoesNotExist as exc:
            raise CommandError(
                f"No PENDING submission with id={submission_id}."
            ) from exc
        sub.state = SubmissionState.FAILED
        sub.detail = "operator forced FAILED via reconcile_submissions --fail"
        sub.save(update_fields=["state", "detail", "updated_at"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Submission {submission_id} -> FAILED (idempotency key freed)."
            )
        )

    def _fail_aged(self, pending: Any) -> None:
        cutoff = timezone.now() - timedelta(
            seconds=settings.REMOTE_SUBMISSION_TTL_SECONDS
        )
        aged = pending.filter(created_at__lte=cutoff)
        count = aged.update(
            state=SubmissionState.FAILED,
            detail="operator forced FAILED (aged) via reconcile_submissions",
        )
        self.stdout.write(
            self.style.SUCCESS(f"Aged out {count} PENDING submission(s) to FAILED.")
        )
