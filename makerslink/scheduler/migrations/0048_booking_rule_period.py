"""
Booking rules belong to a hosting period. Rules made before that apply to
every period that has not ended yet (or the latest one): each gets a copy.
"""
import datetime

import django.db.models.deletion
from django.db import migrations, models


def tie_rules_to_periods(apps, schema_editor):
    Rule = apps.get_model("scheduler", "BookingDependency")
    Period = apps.get_model("scheduler", "SchedulingPeriod")
    today = datetime.date.today()
    periods = list(Period.objects.filter(end__gte=today).order_by("start")) or \
        list(Period.objects.order_by("-end")[:1])
    for rule in Rule.objects.filter(period__isnull=True):
        if not periods:
            rule.delete()  # no period to belong to
            continue
        dependent, required = list(rule.dependent_events.all()), list(rule.required_events.all())
        rule.period = periods[0]
        rule.save()
        for period in periods[1:]:
            copy = Rule.objects.create(
                period=period, name=rule.name, description=rule.description, is_active=rule.is_active,
                weekdays=rule.weekdays, require_all=rule.require_all, day_offset=rule.day_offset,
                same_host=rule.same_host)
            copy.dependent_events.set(dependent)
            copy.required_events.set(required)


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0047_booking_rule_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="bookingdependency",
            name="period",
            field=models.ForeignKey(
                help_text="The hosting period the rule applies in", null=True,
                on_delete=django.db.models.deletion.CASCADE, related_name="booking_rules",
                to="scheduler.schedulingperiod"),
        ),
        migrations.RunPython(tie_rules_to_periods, migrations.RunPython.noop),
        migrations.RemoveField(model_name="bookingdependency", name="valid_from"),
        migrations.RemoveField(model_name="bookingdependency", name="valid_until"),
    ]
