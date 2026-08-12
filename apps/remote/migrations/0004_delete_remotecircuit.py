from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("remote", "0003_delete_manualclassificationoverride"),
    ]

    operations = [
        migrations.DeleteModel(
            name="RemoteCircuit",
        ),
    ]
