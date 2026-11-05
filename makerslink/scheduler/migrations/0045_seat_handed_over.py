from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0044_booking_change_handed_over_label"),
    ]

    operations = [
        migrations.AddField(
            model_name="entryhost",
            name="handed_over_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Set when the host cannot come and asks someone to take over",
                null=True,
            ),
        ),
    ]
