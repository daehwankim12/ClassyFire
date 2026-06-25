from __future__ import annotations

import multiprocessing as mp

import pytest
from django.db import close_old_connections, connections

from apps.core.enums import TaskState
from apps.remote.models import RemoteTask
from apps.remote.queue import RemoteQueueBackpressure, enqueue_task

KEYS = [
    "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
    "QTBSBXVTEAMEQO-UHFFFAOYSA-N",
    "XLYOFNOQVPJJNP-UHFFFAOYSA-N",
    "UHOVQNZJYSORNB-UHFFFAOYSA-N",
    "OKKJLVBELUTLKV-UHFFFAOYSA-N",
    "QUSNBJAOOMFDIB-UHFFFAOYSA-N",
]


def _enqueue_one(key: str, barrier: mp.Barrier, queue_out: mp.Queue) -> None:
    close_old_connections()
    barrier.wait()
    try:
        task = enqueue_task(key)
        queue_out.put(("created", task.inchikey if task else None))
    except RemoteQueueBackpressure:
        queue_out.put(("blocked", key))


@pytest.mark.django_db(transaction=True)
def test_global_queue_depth_is_race_safe(settings):
    settings.REMOTE_MAX_QUEUE_DEPTH = 3
    barrier = mp.Barrier(len(KEYS))
    queue_out: mp.Queue = mp.Queue()
    processes = [mp.Process(target=_enqueue_one, args=(key, barrier, queue_out)) for key in KEYS]
    # Close the parent's DB connection BEFORE forking so each child opens its own
    # fresh socket to the (inherited) test DB. Without this the children share the
    # parent's live socket fd and tearing one down kills the parent's connection.
    connections.close_all()
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    results = [queue_out.get(timeout=1) for _ in processes]
    assert sum(1 for status, _ in results if status == "created") == 3
    assert sum(1 for status, _ in results if status == "blocked") == 3
    assert RemoteTask.objects.filter(state__in=[TaskState.READY]).count() == 3
