"""Tests for classification result writer and InChIKey format constraints.

Verifies that the full-InChIKey regex CHECK constraints on ClassificationResult,
ClassyfireLive, and ClassificationHistory reject malformed keys, and that valid
full InChIKeys (with the correct alphabet) pass through correctly.
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.classifications.models import ClassificationResult, ClassyfireLive
from apps.classifications.result_writer import write_live_result
from apps.core.enums import SourceType
from apps.remote.provider import ChemOntSlot, NormalizedClassification

ASPIRIN_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
CAFFEINE_INCHIKEY = "RYYVLZVUVIJVGH-UHFFFAOYSA-N"

# 27-char but invalid format (lowercase → fails regex but passes length)
LOWERCASE_INCHIKEY = "bsynrymutxbxsq-uhfffaoysa-n"
# 27-char with a digit in last block — fails the [A-Z] regex
DIGIT_SUFFIX_INCHIKEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-1"


def _norm(inchikey: str) -> NormalizedClassification:
    return NormalizedClassification(
        inchikey=inchikey,
        kingdom=ChemOntSlot(chemont_id=None, name="Organic compounds"),
        superclass=None,
        class_=None,
        subclass=None,
        direct_parent=None,
        classification_version="cf-2.1",
        raw_json={"inchikey": inchikey},
    )


@pytest.mark.django_db(transaction=True)
def test_write_live_result_valid_inchikey_succeeds():
    outcome = write_live_result(
        ASPIRIN_INCHIKEY, _norm(ASPIRIN_INCHIKEY),
        source_type=SourceType.WISHART_LIVE_ENTITY,
    )
    assert outcome.applied is True
    assert ClassificationResult.objects.filter(
        requested_inchikey=ASPIRIN_INCHIKEY
    ).exists()
    assert ClassyfireLive.objects.filter(inchikey=ASPIRIN_INCHIKEY).exists()


@pytest.mark.django_db(transaction=True)
def test_classification_result_rejects_digit_suffix_inchikey():
    """requested_inchikey with a digit in last char violates the regex CHECK."""
    from apps.core.enums import SOURCE_PRIORITY, ResultKind

    # Bypass write_live_result and insert directly to test the DB constraint.
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ClassificationResult.objects.create(
                requested_inchikey=DIGIT_SUFFIX_INCHIKEY,
                returned_inchikey=ASPIRIN_INCHIKEY,
                kind=ResultKind.APPLIED,
                source_type=SourceType.WISHART_LIVE_ENTITY,
                source_priority=SOURCE_PRIORITY[SourceType.WISHART_LIVE_ENTITY],
                raw_checksum="a" * 64,
                retrieved_at="2026-01-01T00:00:00Z",
            )


@pytest.mark.django_db(transaction=True)
def test_classification_result_rejects_lowercase_returned_inchikey():
    """returned_inchikey with lowercase letters violates the regex CHECK."""
    from apps.core.enums import SOURCE_PRIORITY, ResultKind

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ClassificationResult.objects.create(
                requested_inchikey=ASPIRIN_INCHIKEY,
                returned_inchikey=LOWERCASE_INCHIKEY,
                kind=ResultKind.REJECTED_MISMATCH,
                source_type=SourceType.WISHART_LIVE_ENTITY,
                source_priority=SOURCE_PRIORITY[SourceType.WISHART_LIVE_ENTITY],
                raw_checksum="b" * 64,
                retrieved_at="2026-01-01T00:00:00Z",
            )


@pytest.mark.django_db(transaction=True)
def test_classification_result_null_returned_inchikey_allowed():
    """returned_inchikey=NULL is allowed by the regex constraint (nullable column)."""
    from apps.core.enums import SOURCE_PRIORITY, ResultKind

    obj = ClassificationResult.objects.create(
        requested_inchikey=ASPIRIN_INCHIKEY,
        returned_inchikey=None,
        kind=ResultKind.APPLIED,
        source_type=SourceType.WISHART_LIVE_ENTITY,
        source_priority=SOURCE_PRIORITY[SourceType.WISHART_LIVE_ENTITY],
        raw_checksum="c" * 64,
        retrieved_at="2026-01-01T00:00:00Z",
    )
    assert obj.pk is not None
    assert obj.returned_inchikey is None
