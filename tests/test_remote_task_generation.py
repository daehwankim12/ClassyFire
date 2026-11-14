import pytest

from apps.batches.models import BatchItem, BatchJob
from apps.core.enums import BatchItemStatus, TaskState
from apps.remote import fanout
from apps.remote.models import RemoteTask

pytestmark = pytest.mark.django_db(transaction=True)

IK = "RDHQFKQIGNGIED-MRVPVSSYSA-N"


def _item(*, row: int, task: RemoteTask | None) -> BatchItem:
    job = BatchJob.objects.create(secret_hash=f"job-{row}", total_count=1)
    return BatchItem.objects.create(
        job=job,
        row_order=0,
        original_input=IK,
        inchikey=IK,
        remote_task=task,
        status=BatchItemStatus.PENDING_CLASSIFICATION,
    )


def test_terminal_fanout_stays_with_its_generation():
    old_task = RemoteTask.objects.create(inchikey=IK, state=TaskState.DONE)
    new_task = RemoteTask.objects.create(inchikey=IK, state=TaskState.READY)
    old_item = _item(row=1, task=old_task)
    new_item = _item(row=2, task=new_task)

    fanout.fan_out_result(old_task, BatchItemStatus.REMOTE_TIMED_OUT)

    old_item.refresh_from_db()
    new_item.refresh_from_db()
    assert old_item.status == BatchItemStatus.REMOTE_TIMED_OUT
    assert new_item.status == BatchItemStatus.PENDING_CLASSIFICATION
    assert new_item.remote_task_id == new_task.pk


def test_unassigned_waiter_is_adopted_by_active_generation():
    task = RemoteTask.objects.create(inchikey=IK, state=TaskState.READY)
    item = _item(row=1, task=None)

    fanout.mark_items_delayed(task)

    item.refresh_from_db()
    assert item.status == BatchItemStatus.REMOTE_DELAYED
    assert item.remote_task_id == task.pk


def _hold_task_lock(task_pk: int, *, no_key: bool, locked, release) -> None:
    from django.db import connection, transaction

    try:
        with transaction.atomic():
            list(
                RemoteTask.objects.select_for_update(no_key=no_key).filter(pk=task_pk)
            )
            locked.set()
            release.wait(10)
    finally:
        connection.close()


def _insert_attempt_with_lock_timeout(task: RemoteTask) -> bool:
    """Insert an FK row referencing task; False if it blocks past lock_timeout."""
    from django.db import OperationalError, connection, transaction

    from apps.remote.models import RemoteAttempt

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '1500ms'")
            RemoteAttempt.objects.create(
                provider="wishart",
                operation=RemoteAttempt.OPERATION_SUBMIT,
                task=task,
                request_inchikey=task.inchikey,
                classification="ACCEPTED",
            )
    except OperationalError:
        return False
    return True


@pytest.mark.parametrize(
    ("no_key", "expect_insert_ok"),
    [(True, True), (False, False)],
    ids=["enrollment-no-key-lock", "plain-for-update-control"],
)
def test_enrollment_task_lock_does_not_block_worker_fk_inserts(no_key, expect_insert_ok):
    """Real-Postgres check for the enrollment/worker deadlock.

    Workers insert RemoteAttempt/RemoteQueryItem rows referencing task rows
    (FOR KEY SHARE on each). Enrollment's FOR NO KEY UPDATE must not block
    that; a plain FOR UPDATE does (the control case), which is what let an
    ingest chunk and a worker deadlock.
    """
    import threading

    task = RemoteTask.objects.create(inchikey=IK, state=TaskState.LOCKED)
    locked = threading.Event()
    release = threading.Event()
    holder = threading.Thread(
        target=_hold_task_lock,
        args=(task.pk,),
        kwargs={"no_key": no_key, "locked": locked, "release": release},
    )
    holder.start()
    try:
        assert locked.wait(10)
        assert _insert_attempt_with_lock_timeout(task) is expect_insert_ok
    finally:
        release.set()
        holder.join(10)
