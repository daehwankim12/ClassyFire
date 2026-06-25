from __future__ import annotations

import multiprocessing as mp

import pytest
from django.db import close_old_connections, connections

from apps.core.security import record_failed_login_attempt


def _fail_login(ip: str, output: mp.Queue) -> None:
    close_old_connections()
    blocked = record_failed_login_attempt(ip)
    output.put(blocked)


@pytest.mark.django_db(transaction=True)
def test_login_throttle_shared_across_processes(settings):
    settings.SHARED_ACCESS_LOGIN_MAX_FAILURES = 3
    settings.SHARED_ACCESS_LOGIN_WINDOW_SECONDS = 300
    output: mp.Queue = mp.Queue()
    processes = [mp.Process(target=_fail_login, args=("203.0.113.10", output)) for _ in range(4)]
    # Close the parent's DB connection BEFORE forking so each child opens its own
    # fresh socket to the (inherited) test DB. Without this the children share the
    # parent's live socket fd and tearing one down kills the parent's connection
    # (matches tests/test_remote_queue_backpressure_race.py).
    connections.close_all()
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    results = [output.get(timeout=1) for _ in processes]
    assert results.count(True) == 1
