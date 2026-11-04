"""
Booking dependencies: some slots only open once another slot the same day
has a host. See models.BookingDependency.
"""
import datetime

from django.utils import timezone

from .models import BookingDependency, EventInstance


class DependencyChecker:
    """
    Answers "is this slot locked?" for every slot in a date range with two
    queries: the active rules, and which Events have a host on which days.
    """

    def __init__(self, first_day, last_day):
        self.rules = []
        for rule in BookingDependency.objects.filter(is_active=True).prefetch_related(
                'dependent_events', 'required_events'):
            required = list(rule.required_events.all())
            self.rules.append({
                'rule': rule,
                'dependent': {event.id for event in rule.dependent_events.all()},
                'required': {event.id for event in required},
                'required_names': " eller ".join(event.name for event in required),
                'weekdays': rule.weekday_set(),
            })
        tz = timezone.get_current_timezone()
        start = datetime.datetime.combine(first_day, datetime.time(), tz)
        end = datetime.datetime.combine(last_day + datetime.timedelta(days=1), datetime.time(), tz)
        self.hosted = {
            (event_id, timezone.localtime(start).date())
            for event_id, start in EventInstance.objects.filter(
                status=1, start__gte=start, start__lt=end).values_list('event_id', 'start')}

    def unmet(self, instance):
        """The rules that keep `instance` from being booked right now."""
        day = timezone.localtime(instance.start).date()
        return [r for r in self.rules
                if instance.event_id in r['dependent']
                and day.weekday() in r['weekdays']
                and not any((event_id, day) in self.hosted for event_id in r['required'])]

    @staticmethod
    def describe(unmet):
        """Swedish text naming what has to be booked first."""
        return " och ".join(r['required_names'] for r in unmet)
