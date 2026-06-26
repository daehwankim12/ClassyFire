"""``manage.py resolve_submission`` — operator resolution of UNKNOWN intents (G5-T1).

A ``RemoteSubmission`` parked in ``UNKNOWN`` is in-doubt: the worker POSTed the
batch, but it could not confirm whether the provider accepted it (the socket
dropped). To protect exactly-once submission the worker NEVER auto-re-POSTs an
UNKNOWN intent — it HOLDS the idempotency key forever until an operator, having
checked the provider out-of-band, resolves it with one of two modes:

Usage::

    # The operator CONFIRMED the provider DID accept the POST. Create/link the
    # RemoteQuery for the (now-known) upstream id and move the intent to
    # SUBMITTED so polling resumes against the real query.
    manage.py resolve_submission --submission-id 123 --remote-query-id QUERY123

    # The operator VERIFIED the provider did NOT accept the POST. Move the intent
    # to FAILED, freeing the idempotency key so a genuine re-POST can occur.
    manage.py resolve_submission --submission-id 123 --mark-failed

Exactly one of ``--remote-query-id`` / ``--mark-failed`` is required, and the
target submission MUST be in UNKNOWN (the command refuses any other state).
"""
from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.enums import QueryState, SubmissionState
from apps.remote.models import RemoteQuery, RemoteSubmission, RemoteSubmissionMember


class Command(BaseCommand):
    help = "Resolve an in-doubt UNKNOWN remote submission (operator-confirmed)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--submission-id",
            type=int,
            required=True,
            metavar="ID",
            help="Primary key of the UNKNOWN RemoteSubmission to resolve.",
        )
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument(
            "--remote-query-id",
            type=str,
            default=None,
            metavar="QID",
            help=(
                "Operator confirmed acceptance: create/link this upstream query "
                "id and move the intent to SUBMITTED."
            ),
        )
        mode.add_argument(
            "--mark-failed",
            action="store_true",
            help=(
                "Operator verified non-acceptance: move the intent to FAILED "
                "(frees the idempotency key for a genuine re-POST)."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        submission_id: int = options["submission_id"]
        try:
            submission = RemoteSubmission.objects.get(pk=submission_id)
        except RemoteSubmission.DoesNotExist as exc:
            raise CommandError(
                f"No RemoteSubmission with id={submission_id}."
            ) from exc

        if submission.state != SubmissionState.UNKNOWN:
            raise CommandError(
                f"Submission {submission_id} is {submission.state}, not UNKNOWN; "
                "resolve_submission only acts on in-doubt UNKNOWN intents."
            )

        if options["mark_failed"]:
            self._mark_failed(submission)
        else:
            self._confirm_accepted(submission, options["remote_query_id"])

    def _confirm_accepted(
        self, submission: RemoteSubmission, remote_query_id: str
    ) -> None:
        """Operator confirmed acceptance: link the query and move to SUBMITTED.

        Mirrors the worker's submit-success path (create the RemoteQuery, flip
        the intent to SUBMITTED, link it) so that when the parked member tasks are
        re-claimed they rebuild the SAME payload, hit the SUBMITTED reconcile
        branch, get their durable ``RemoteQueryItem`` links, and resume polling —
        exactly as if the dropped response had committed normally. No re-POST.
        """
        with transaction.atomic():
            query, _ = RemoteQuery.objects.get_or_create(
                provider=submission.provider,
                remote_query_id=remote_query_id,
                defaults={
                    "payload_hash": submission.payload_hash,
                    "state": QueryState.SUBMITTED,
                    "next_poll_at": timezone.now(),
                },
            )
            submission.state = SubmissionState.SUBMITTED
            submission.query = query
            submission.remote_query_id = remote_query_id
            submission.detail = (
                "operator confirmed provider acceptance via resolve_submission "
                f"(remote_query_id={remote_query_id})"
            )
            submission.save(
                update_fields=[
                    "state",
                    "query",
                    "remote_query_id",
                    "detail",
                    "updated_at",
                ]
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Submission {submission.pk} -> SUBMITTED "
                f"(linked RemoteQuery {query.pk}, remote_query_id={remote_query_id})."
            )
        )

    def _mark_failed(self, submission: RemoteSubmission) -> None:
        """Operator verified non-acceptance: move to FAILED, freeing the key.

        The submission no longer holds the idempotency slot, so its per-member
        reservations (R-01) are released in the SAME transaction — a genuine
        re-POST of those members may now occur.
        """
        with transaction.atomic():
            submission.state = SubmissionState.FAILED
            submission.detail = (
                "operator verified provider did NOT accept via resolve_submission "
                "--mark-failed (idempotency key freed)"
            )
            submission.save(update_fields=["state", "detail", "updated_at"])
            RemoteSubmissionMember.objects.filter(
                submission=submission, active=True
            ).update(
                active=False,
                released_at=timezone.now(),
                release_reason="operator_mark_failed",
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Submission {submission.pk} -> FAILED (idempotency key freed)."
            )
        )
