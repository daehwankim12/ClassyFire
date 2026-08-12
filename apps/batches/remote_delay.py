"""Explain user-facing REMOTE_DELAYED rows for UI and API callers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db.models import OuterRef, Subquery

from apps.batches.models import BatchItem, BatchJob
from apps.core.enums import (
    BatchItemStatus,
    ProviderControlState,
    TaskState,
)
from apps.remote.models import (
    RemoteAttempt,
    RemoteProviderControl,
    RemoteTask,
)

REMOTE_DELAYED_DEFAULT_MESSAGE = (
    "Waiting on remote retry or operator action."
)

QUEUE_CAPACITY_MESSAGE = "Remote queue is at capacity; retrying later."
BUDGET_EXCEEDED_MESSAGE = "Remote budget exceeded for this principal."
REMOTE_DISABLED_MESSAGE = "Structure provided; remote classification disabled."
RETRYABLE_REMOTE_MESSAGE = "Wishart returned a retryable error; retrying later."
AUTH_BLOCKED_MESSAGE = (
    "Wishart authentication is blocked; operator action is required."
)
PROTOCOL_RETRY_MESSAGE = (
    "Wishart returned an unexpected response; retrying later."
)

_RETRYABLE_HTTP_STATUSES = {408, 425, 429}
_WISHART_RETRY_STATES = {
    TaskState.RETRY_WAIT,
    TaskState.POLL_WAIT,
    TaskState.SUBMIT_WAIT,
}
REMOTE_DELAYED_BREAKDOWN_ITEM_LIMIT = 100


@dataclass(frozen=True)
class RemoteDelayCause:
    code: str
    label: str
    description: str
    next_action: str
    severity: str
    priority: int


CAUSES: dict[str, RemoteDelayCause] = {
    "wishart_auth_blocked": RemoteDelayCause(
        code="wishart_auth_blocked",
        label="Wishart auth blocked",
        description="Wishart returned an auth failure. Operator reset is required.",
        next_action="fix_wishart_auth",
        severity="error",
        priority=10,
    ),
    "queue_capacity": RemoteDelayCause(
        code="queue_capacity",
        label="Queue capacity",
        description="The local remote queue is full. Workers will retry enqueueing later.",
        next_action="wait_for_remote_capacity",
        severity="warning",
        priority=30,
    ),
    "wishart_retry": RemoteDelayCause(
        code="wishart_retry",
        label="Wishart outage / retrying",
        description="Wishart is returning retryable errors. Workers will retry automatically.",
        next_action="wait_for_wishart_recovery",
        severity="warning",
        priority=40,
    ),
    "budget_exceeded": RemoteDelayCause(
        code="budget_exceeded",
        label="Budget exceeded",
        description=(
            "This principal has exhausted its daily remote budget. Workers "
            "retry automatically once the budget window resets."
        ),
        next_action="increase_remote_budget",
        severity="warning",
        priority=50,
    ),
    "remote_disabled": RemoteDelayCause(
        code="remote_disabled",
        label="Remote disabled",
        description="Remote classification is disabled for this job.",
        next_action="enable_remote_or_resubmit",
        severity="info",
        priority=60,
    ),
    "other_delayed": RemoteDelayCause(
        code="other_delayed",
        label="Other delayed",
        description="Remote work is still outstanding, but no specific cause was recorded.",
        next_action="wait_for_remote_recovery",
        severity="info",
        priority=70,
    ),
}


def build_remote_delayed_breakdown(job: BatchJob) -> list[dict[str, Any]]:
    delayed_qs = (
        BatchItem.objects.filter(job=job, status=BatchItemStatus.REMOTE_DELAYED)
        .only("inchikey", "message", "row_order")
        .order_by("row_order")
    )
    delayed_count = delayed_qs.count()
    if not delayed_count:
        return []

    delayed_items = list(
        delayed_qs[:REMOTE_DELAYED_BREAKDOWN_ITEM_LIMIT]
    )

    inchikeys = [item.inchikey for item in delayed_items if item.inchikey]
    active_tasks = _active_tasks_by_inchikey(inchikeys)
    latest_attempts = _latest_attempts_by_inchikey(inchikeys)
    wishart_auth_blocked = RemoteProviderControl.objects.filter(
        provider="wishart",
        state=ProviderControlState.AUTH_BLOCKED,
    ).exists()
    grouped: dict[str, dict[str, Any]] = {}
    for item in delayed_items:
        inchikey = item.inchikey
        code = _classify_item(
            item,
            active_task=active_tasks.get(inchikey) if inchikey else None,
            latest_attempt=latest_attempts.get(inchikey) if inchikey else None,
            wishart_auth_blocked=wishart_auth_blocked,
        )
        cause = CAUSES[code]
        entry = grouped.setdefault(
            code,
            {
                "code": cause.code,
                "label": cause.label,
                "description": cause.description,
                "next_action": cause.next_action,
                "severity": cause.severity,
                "priority": cause.priority,
                "count": 0,
                "inchikeys": [],
            },
        )
        entry["count"] += 1
        if item.inchikey and item.inchikey not in entry["inchikeys"]:
            entry["inchikeys"].append(item.inchikey)

    unclassified_count = delayed_count - len(delayed_items)
    if unclassified_count:
        cause = CAUSES["other_delayed"]
        entry = grouped.setdefault(
            cause.code,
            {
                "code": cause.code,
                "label": cause.label,
                "description": cause.description,
                "next_action": cause.next_action,
                "severity": cause.severity,
                "priority": cause.priority,
                "count": 0,
                "inchikeys": [],
            },
        )
        entry["count"] += unclassified_count

    return [
        _public_entry(entry)
        for entry in sorted(grouped.values(), key=lambda value: value["priority"])
    ]


def next_action_for_remote_delayed(
    breakdown: list[dict[str, Any]],
) -> str:
    if not breakdown:
        return CAUSES["other_delayed"].next_action
    return str(breakdown[0]["next_action"])


def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    public = dict(entry)
    public.pop("priority", None)
    public["inchikeys"] = public["inchikeys"][:5]
    return public


def _classify_item(
    item: BatchItem,
    *,
    active_task: RemoteTask | None,
    latest_attempt: RemoteAttempt | None,
    wishart_auth_blocked: bool,
) -> str:
    message = (item.message or "").lower()
    if QUEUE_CAPACITY_MESSAGE.lower() in message:
        return "queue_capacity"
    if BUDGET_EXCEEDED_MESSAGE.lower() in message:
        return "budget_exceeded"
    if "remote classification disabled" in message:
        return "remote_disabled"

    if wishart_auth_blocked or _attempt_is_auth_failure(latest_attempt):
        return "wishart_auth_blocked"
    if _attempt_is_retryable(latest_attempt) or (
        active_task is not None and active_task.state in _WISHART_RETRY_STATES
    ):
        return "wishart_retry"
    return "other_delayed"


def _active_tasks_by_inchikey(inchikeys: list[str]) -> dict[str, RemoteTask]:
    tasks: dict[str, RemoteTask] = {}
    for task in (
        RemoteTask.objects.filter(inchikey__in=inchikeys)
        .exclude(state=TaskState.DONE)
        .order_by("inchikey", "-updated_at", "-pk")
    ):
        tasks.setdefault(task.inchikey, task)
    return tasks


def _latest_attempts_by_inchikey(inchikeys: list[str]) -> dict[str, RemoteAttempt]:
    if not inchikeys:
        return {}

    latest_attempt_pk = (
        RemoteAttempt.objects.filter(task__inchikey=OuterRef("task__inchikey"))
        .order_by("-created_at", "-pk")
        .values("pk")[:1]
    )
    attempts: dict[str, RemoteAttempt] = {}
    for attempt in (
        RemoteAttempt.objects.filter(
            task__inchikey__in=inchikeys,
            pk=Subquery(latest_attempt_pk),
        )
        .select_related("task")
        .order_by("task__inchikey", "-created_at", "-pk")
    ):
        if attempt.task is not None:
            attempts.setdefault(attempt.task.inchikey, attempt)
    return attempts


def _attempt_is_auth_failure(attempt: RemoteAttempt | None) -> bool:
    return attempt is not None and attempt.classification == "AUTH_FAILURE"


def _attempt_is_retryable(attempt: RemoteAttempt | None) -> bool:
    if attempt is None:
        return False
    if attempt.classification == "RETRYABLE":
        return True
    if attempt.http_status in _RETRYABLE_HTTP_STATUSES:
        return True
    return attempt.http_status is not None and 500 <= attempt.http_status < 600
