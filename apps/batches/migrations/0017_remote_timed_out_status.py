from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("batches", "0016_rename_batch_tables")]

    operations = [
        migrations.AlterField(
            model_name="batchitem",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING_NORMALIZATION", "Pending normalization"),
                    ("FOUND_LIVE_CACHE", "Found in live cache"),
                    ("FOUND_SEED_CACHE", "Found in seed cache"),
                    ("FOUND_WISHART_ENTITY", "Found Wishart entity"),
                    ("PENDING_CLASSIFICATION", "Pending classification"),
                    ("COMPLETE_AFTER_WISHART", "Complete after Wishart"),
                    ("NEED_STRUCTURE", "Needs structure"),
                    ("REMOTE_DELAYED", "Remote delayed"),
                    ("REMOTE_TIMED_OUT", "Remote timed out"),
                    ("INVALID_INPUT", "Invalid input"),
                    ("RESULT_KEY_MISMATCH", "Result key mismatch"),
                ],
                default="PENDING_NORMALIZATION",
                max_length=32,
            ),
        ),
    ]
