import django.db.models.deletion
from django.db import migrations, models
from django.db.models import OuterRef, Subquery


ACTIVE_TASK_STATES = (
    "READY",
    "LOCKED",
    "RUNNING",
    "ENTITY_LOOKUP",
    "SUBMIT_WAIT",
    "SUBMITTED",
    "POLL_WAIT",
    "RETRY_WAIT",
    "DEFERRED",
)

WAITING_ITEM_STATUSES = (
    "PENDING_CLASSIFICATION",
    "REMOTE_DELAYED",
)


def backfill_remote_task_membership(apps, schema_editor):
    BatchItem = apps.get_model("batches", "BatchItem")
    RemoteTask = apps.get_model("remote", "RemoteTask")
    active_task = (
        RemoteTask.objects.filter(
            inchikey=OuterRef("inchikey"),
            state__in=ACTIVE_TASK_STATES,
        )
        .order_by("pk")
        .values("pk")[:1]
    )
    BatchItem.objects.filter(
        remote_task__isnull=True,
        status__in=WAITING_ITEM_STATUSES,
        inchikey__isnull=False,
    ).update(remote_task_id=Subquery(active_task))


class Migration(migrations.Migration):

    dependencies = [
        ("batches", "0003_batchjob_retry_not_before"),
        ("remote", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="batchitem",
            name="remote_task",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Remote task generation this item joined; fan-out is scoped "
                    "to this identity rather than only the InChIKey."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="batch_items",
                to="remote.remotetask",
            ),
        ),
        migrations.RunPython(
            backfill_remote_task_membership,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
