# Generated 2026-06-25: add SourceArtifact storage fields, full-InChIKey regex
# CHECK constraints on ClassificationResult, ClassyfireLive, ClassificationHistory.

from django.db import migrations, models

INCHIKEY_REGEX = r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$"


def fail_on_invalid_classification_inchikeys(apps, schema_editor):
    """Preflight: abort if any existing rows would violate the new regex constraints."""
    ClassificationResult = apps.get_model("classifications", "ClassificationResult")
    invalid = (
        ClassificationResult.objects.exclude(
            requested_inchikey__regex=INCHIKEY_REGEX
        )
        .values("id", "requested_inchikey")
        .order_by("id")
        .first()
    )
    if invalid is not None:
        raise RuntimeError(
            "Cannot add CHECK classification_results.requested_inchikey format "
            f"constraint: sample invalid row id={invalid['id']} "
            f"value={invalid['requested_inchikey']!r}. Fix invalid InChIKey values "
            "before applying this migration."
        )

    invalid_returned = (
        ClassificationResult.objects.exclude(returned_inchikey__isnull=True)
        .exclude(returned_inchikey__regex=INCHIKEY_REGEX)
        .values("id", "returned_inchikey")
        .order_by("id")
        .first()
    )
    if invalid_returned is not None:
        raise RuntimeError(
            "Cannot add CHECK classification_results.returned_inchikey format "
            f"constraint: sample invalid row id={invalid_returned['id']} "
            f"value={invalid_returned['returned_inchikey']!r}. Fix invalid non-null "
            "InChIKey values before applying this migration."
        )

    ClassyfireLive = apps.get_model("classifications", "ClassyfireLive")
    invalid_live = (
        ClassyfireLive.objects.exclude(inchikey__regex=INCHIKEY_REGEX)
        .values("id", "inchikey")
        .order_by("id")
        .first()
    )
    if invalid_live is not None:
        raise RuntimeError(
            "Cannot add CHECK classyfire_live.inchikey format constraint: "
            f"sample invalid row id={invalid_live['id']} "
            f"value={invalid_live['inchikey']!r}. Fix invalid InChIKey values "
            "before applying this migration."
        )

    ClassificationHistory = apps.get_model("classifications", "ClassificationHistory")
    invalid_history = (
        ClassificationHistory.objects.exclude(inchikey__regex=INCHIKEY_REGEX)
        .values("id", "inchikey")
        .order_by("id")
        .first()
    )
    if invalid_history is not None:
        raise RuntimeError(
            "Cannot add CHECK classification_history.inchikey format constraint: "
            f"sample invalid row id={invalid_history['id']} "
            f"value={invalid_history['inchikey']!r}. Fix invalid InChIKey values "
            "before applying this migration."
        )


class Migration(migrations.Migration):

    dependencies = [
        ('classifications', '0003_classificationresult_classyfirelive_result_and_more'),
    ]

    operations = [
        # Add new SourceArtifact fields for raw byte storage.
        migrations.AddField(
            model_name='sourceartifact',
            name='storage_key',
            field=models.TextField(
                blank=True,
                null=True,
                help_text='Storage key (relative path) where the raw artifact bytes are persisted.',
            ),
        ),
        migrations.AddField(
            model_name='sourceartifact',
            name='content_sha256',
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                help_text='SHA-256 hex digest of the stored content bytes (mirrors checksum).',
            ),
        ),
        # Preflight: verify all existing rows satisfy the new regex before adding constraints.
        migrations.RunPython(
            fail_on_invalid_classification_inchikeys,
            reverse_code=migrations.RunPython.noop,
        ),
        # Add regex CHECK constraints (in addition to existing length-27 checks).
        migrations.AddConstraint(
            model_name='classificationresult',
            constraint=models.CheckConstraint(
                condition=models.Q(requested_inchikey__regex=INCHIKEY_REGEX),
                name='classification_result_requested_inchikey_format',
            ),
        ),
        migrations.AddConstraint(
            model_name='classificationresult',
            constraint=models.CheckConstraint(
                condition=models.Q(returned_inchikey__isnull=True)
                | models.Q(returned_inchikey__regex=INCHIKEY_REGEX),
                name='classification_result_returned_inchikey_format',
            ),
        ),
        migrations.AddConstraint(
            model_name='classyfirelive',
            constraint=models.CheckConstraint(
                condition=models.Q(inchikey__regex=INCHIKEY_REGEX),
                name='classyfire_live_inchikey_format',
            ),
        ),
        migrations.AddConstraint(
            model_name='classificationhistory',
            constraint=models.CheckConstraint(
                condition=models.Q(inchikey__regex=INCHIKEY_REGEX),
                name='classification_history_inchikey_format',
            ),
        ),
    ]
