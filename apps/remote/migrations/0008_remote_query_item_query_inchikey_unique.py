from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("remote", "0007_remote_attempt_provenance"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="remotequeryitem",
            constraint=models.UniqueConstraint(
                fields=["query", "inchikey"],
                condition=Q(inchikey__isnull=False),
                name="remote_query_item_query_inchikey_unique",
            ),
        ),
    ]
