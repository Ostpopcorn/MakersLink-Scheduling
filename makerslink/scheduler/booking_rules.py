"""
What keeps a slot from being booked right now:
- closures: a "Stängt" calendar entry overlapping the slot;
- special passes whose type blocks regular slots: the slot is replaced by
  the special pass (e.g. an introductory evening) for that time;
- booking dependencies: some slots only open once another slot the same
  day is booked (models.BookingDependency). Booked means status 1: a slot
  whose host asked for someone to take over ("Ombokning önskas") does not
  open anything until someone has.
"""
import datetime

from django.utils import timezone

from .models import BookingDependency, CalendarEntry, EventInstance


def _day_bounds(first_day, last_day):
    tz = timezone.get_current_timezone()
    return (datetime.datetime.combine(first_day, datetime.time(), tz),
            datetime.datetime.combine(last_day + datetime.timedelta(days=1), datetime.time(), tz))


class Closures:
    """
    Calendar entries that block regular slots in a date range, in one
    query: closures ("Stängt") and special passes whose type blocks slots.
    """

    def __init__(self, first_day, last_day):
        from django.db.models import Q
        start, end = _day_bounds(first_day, last_day)
        self.entries = list(CalendarEntry.objects.filter(
            Q(kind='closed') | Q(kind='special', special_type__blocks_slots=True),
            start__lt=end, end__gt=start).select_related('special_type'))

    def _overlapping(self, instance, kind):
        return [entry for entry in self.entries
                if entry.kind == kind and entry.pk != getattr(instance, 'pk', None)
                and entry.start < instance.end and entry.end > instance.start]

    def closing(self, instance):
        """The closures that overlap `instance` at all."""
        return self._overlapping(instance, 'closed')

    def replacing(self, instance):
        """The special passes that take the place of `instance`."""
        return self._overlapping(instance, 'special')

    @staticmethod
    def describe(entries):
        return ", ".join(entry.title for entry in entries)


class DependencyChecker:
    """
    Answers "can this slot be booked right now?" for every slot in a date
    range with three queries: closures, the active dependency rules, and
    which Events have a host on which days.
    """

    def __init__(self, first_day, last_day):
        self.closures = Closures(first_day, last_day)
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
        start, end = _day_bounds(first_day, last_day)
        self.hosted = {
            (event_id, timezone.localtime(start).date())
            for event_id, start in EventInstance.objects.filter(
                status=1, host__isnull=False, start__gte=start, start__lt=end)
                .values_list('event_id', 'start')}

    def closing(self, instance):
        return self.closures.closing(instance)

    def replacing(self, instance):
        return self.closures.replacing(instance)

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
