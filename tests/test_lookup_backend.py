from __future__ import annotations

import datetime as dt
from typing import ClassVar

import pytest
from django.test import override_settings

IK_A = "RDHQFKQIGNGIED-MRVPVSSYSA-N"
IK_B = "WQZGKKKJIJFFOK-GASJEMHNSA-N"


class RecordingLookupBackend:
    one_calls: ClassVar[list[str]] = []
    many_calls: ClassVar[list[list[str]]] = []

    @classmethod
    def reset(cls) -> None:
        cls.one_calls = []
        cls.many_calls = []

    def lookup_one(self, inchikey: str):
        from apps.classifications.services import (
            ChemOntLabel,
            ClassificationLookupResult,
        )

        self.__class__.one_calls.append(inchikey)
        return ClassificationLookupResult(
            inchikey=inchikey,
            kingdom=ChemOntLabel(chemont_id="CHEMONT:0000001", name="Backend Kingdom"),
            superclass=None,
            class_=None,
            subclass=None,
            direct_parent=None,
            source_type="WISHART_LIVE_QUERY",
            source_priority=100,
            source_ref="backend-ref",
            classification_version="backend-v1",
            retrieved_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            query_id="backend-query",
            result_id=123,
        )

    def lookup_many(self, inchikeys: list[str]):
        self.__class__.many_calls.append(list(inchikeys))
        return {key: self.lookup_one(key) for key in inchikeys}


@pytest.mark.django_db(transaction=True)
@override_settings(
    CLASSIFICATION_LOOKUP_BACKEND="tests.test_lookup_backend.RecordingLookupBackend"
)
def test_single_lookup_delegates_to_configured_backend():
    from apps.classifications import services

    services.reset_lookup_backend_cache()
    RecordingLookupBackend.reset()

    result = services.lookup_current_classification(IK_A)

    assert RecordingLookupBackend.one_calls == [IK_A]
    assert result is not None
    assert result.classification_version == "backend-v1"
    assert result.kingdom is not None
    assert result.kingdom.name == "Backend Kingdom"


@pytest.mark.django_db(transaction=True)
@override_settings(
    CLASSIFICATION_LOOKUP_BACKEND="tests.test_lookup_backend.RecordingLookupBackend"
)
def test_bulk_lookup_delegates_to_configured_backend_once():
    from apps.classifications import services

    services.reset_lookup_backend_cache()
    RecordingLookupBackend.reset()

    results = services.lookup_current_classifications([IK_A, IK_B, IK_A])

    assert RecordingLookupBackend.many_calls == [[IK_A, IK_B]]
    assert list(results) == [IK_A, IK_B]
    assert results[IK_B].source_ref == "backend-ref"


@pytest.mark.django_db(transaction=True)
def test_default_lookup_backend_resolves_live_row():
    from django.utils import timezone

    from apps.classifications.models import ClassyfireLive
    from apps.classifications.services import lookup_current_classification
    from apps.core.enums import SOURCE_PRIORITY, SourceType

    ClassyfireLive.objects.create(
        inchikey=IK_A,
        source_type=SourceType.WISHART_LIVE_QUERY.value,
        source_priority=SOURCE_PRIORITY[SourceType.WISHART_LIVE_QUERY],
        retrieved_at=timezone.now(),
    )

    result = lookup_current_classification(IK_A)

    assert result is not None
    assert result.source_type == SourceType.WISHART_LIVE_QUERY.value
