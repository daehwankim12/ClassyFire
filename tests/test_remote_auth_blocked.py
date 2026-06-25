from __future__ import annotations

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.compounds.models import CompoundStructure
from apps.core.enums import ProviderControlState, TaskState
from apps.remote.models import RemoteProviderControl, RemoteTask
from apps.remote.provider import AuthProviderError
from apps.remote.worker import RemoteWorker


class AuthFailProvider:
    def __init__(self) -> None:
        self.calls = 0

    def lookup_entity(self, inchikey: str):
        self.calls += 1
        raise AuthProviderError("HTTP 401")

    def submit_structures(self, structures):
        self.calls += 1
        raise AuthProviderError("HTTP 401")

    def poll_query(self, remote_query_id: str):
        self.calls += 1
        raise AssertionError("poll not used")


class AllowLimiter:
    class Bucket:
        def allow(self) -> bool:
            return True

        def penalize(self, retry_after: float) -> None:
            self.last_retry_after = retry_after

    def __init__(self) -> None:
        self.entity = self.Bucket()
        self.submit = self.Bucket()


class ClosedCircuit:
    def allow(self) -> bool:
        return True

    def record_success(self) -> None:
        self.last = "success"

    def record_failure(self) -> None:
        self.last = "failure"


@pytest.mark.django_db(transaction=True)
def test_auth_failure_blocks_provider_until_operator_reset(settings):
    compound = CompoundStructure.objects.create(
        inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        canonical_smiles="CCO",
        original_input="CCO",
    )
    RemoteTask.objects.create(
        inchikey=compound.inchikey,
        compound=compound,
        state=TaskState.READY,
        next_run_at=timezone.now(),
    )
    provider = AuthFailProvider()
    worker = RemoteWorker(
        provider,
        worker_id="worker-a",
        rate_limiter=AllowLimiter(),
        circuit=ClosedCircuit(),
        lease_seconds=0.1,
    )

    worker.tick()
    assert provider.calls == 1
    control = RemoteProviderControl.objects.get(provider="wishart")
    assert control.state == ProviderControlState.AUTH_BLOCKED

    for _ in range(3):
        worker.tick()
    assert provider.calls == 1

    call_command("reset_provider_auth", "--provider", "wishart")
    control.refresh_from_db()
    assert control.state == ProviderControlState.ENABLED
