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
    who is booked (status 1) on which Events on which days.
    """

    def __init__(self, first_day, last_day):
        self.closures = Closures(first_day, last_day)
        self.rules = []
        # Only the rules of the periods these days are in.
        for rule in BookingDependency.objects.filter(
                is_active=True, period__start__lte=last_day, period__end__gte=first_day).select_related(
                'period').prefetch_related('dependent_events', 'required_events', 'exemptions'):
            required = list(rule.required_events.all())
            self.rules.append({
                'rule': rule,
                'dependent': {event.id for event in rule.dependent_events.all()},
                'required': [event.id for event in required],
                'needs': rule.condition_text(required),
                'needs_you': rule.condition_text(required, you=True),
            })
        # One day more on each side, for rules on the day before or after.
        start, end = _day_bounds(first_day - datetime.timedelta(days=1), last_day + datetime.timedelta(days=1))
        self.hosts = {}
        for event_id, start, host_id in EventInstance.objects.filter(
                status=1, host__isnull=False, start__gte=start, start__lt=end).values_list(
                'event_id', 'start', 'host_id'):
            self.hosts.setdefault((event_id, timezone.localtime(start).date()), set()).add(host_id)

    def closing(self, instance):
        return self.closures.closing(instance)

    def replacing(self, instance):
        return self.closures.replacing(instance)

    def unmet(self, instance, user=None):
        """
        The rules that keep `instance` from being booked right now. For a
        rule that needs the same host, that is `user` (who is about to book
        it), else the slot's own host; with neither, any host will do.
        """
        day = timezone.localtime(instance.start).date()
        who = user.id if user is not None else instance.host_id
        unmet = []
        for r in self.rules:
            rule = r['rule']
            if instance.event_id not in r['dependent'] or not rule.applies_on(day):
                continue
            target = day + datetime.timedelta(days=rule.day_offset)

            def booked(event_id):
                hosts = self.hosts.get((event_id, target), set())
                return who in hosts if rule.same_host and who is not None else bool(hosts)
            met = (all if rule.require_all else any)(booked(event_id) for event_id in r['required'])
            if not met:
                unmet.append(r)
        return unmet

    @staticmethod
    def describe(unmet, you=False):
        """What has to be true first, in Swedish: "Vardag 18-21 är bokat"."""
        return " och ".join(r['needs_you' if you else 'needs'] for r in unmet)
