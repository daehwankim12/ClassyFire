"""runworker should not let stale-task cleanup starve live remote work."""
from __future__ import annotations

from apps.remote.management.commands import runworker


class _FakeWorker:
    def __init__(self, events: list[str], result: bool) -> None:
        self.events = events
        self.result = result

    def tick(self) -> bool:
        self.events.append("tick")
        return self.result


def test_runworker_skips_inline_reaper_when_live_work_is_processed(
    monkeypatch, settings
):
    events: list[str] = []
    settings.REMOTE_MAX_CONCURRENCY = 1

    monkeypatch.setattr(
        runworker,
        "RemoteWorker",
        lambda _provider: _FakeWorker(events, True),
    )
    monkeypatch.setattr(
        runworker.Command,
        "_reap_stale_tasks",
        lambda _self: events.append("reap"),
    )

    runworker.Command().handle(once=True, mock=True, idle_sleep=0)

    assert events == ["tick"]


def test_runworker_reaps_only_after_an_idle_tick(monkeypatch, settings):
    events: list[str] = []
    settings.REMOTE_MAX_CONCURRENCY = 1

    monkeypatch.setattr(
        runworker,
        "RemoteWorker",
        lambda _provider: _FakeWorker(events, False),
    )
    monkeypatch.setattr(
        runworker.Command,
        "_reap_stale_tasks",
        lambda _self: events.append("reap"),
    )

    runworker.Command().handle(once=True, mock=True, idle_sleep=0)

    assert events == ["tick", "reap"]
