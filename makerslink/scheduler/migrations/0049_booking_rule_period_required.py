import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0048_booking_rule_period"),
    ]

    operations = [
        migrations.AlterField(
            model_name="bookingdependency",
            name="period",
            field=models.ForeignKey(
                help_text="The hosting period the rule applies in",
                on_delete=django.db.models.deletion.CASCADE, related_name="booking_rules",
                to="scheduler.schedulingperiod"),
        ),
    ]
