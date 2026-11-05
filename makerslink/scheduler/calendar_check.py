"""
Compare a Google calendar with what the schedule says it should contain,
list the differences for staff, and fix them from the schedule's side.

Our truth, per calendar, within a time window:
- Booked slots (status 1) of templates that synchronize: an event with the
  template's title, the slot's times and "Värd: <host>" in the description.
- Cancelled slots (status 2): an event titled "Inställt: ...".
- Slots that need rebooking (-1) or are free (0) but still have a Google
  event: shown in Google as an ordinary open slot, which they are not.
- Calendar entries in this calendar: an event with the entry's title and times.
Anything else in Google during the window is reported as Google-only.
"""
import datetime
import re

from django.conf import settings
from django.utils import timezone

from .entry_sync import PRIVATE_KEY
from .google_calendar import client_for, event_bounds
from .models import CalendarCheck, CalendarEntry, EventInstance
from .views_week import DAY_SHORT, MONTH_NAMES

# kind: (heading, what it means, what to do)
KINDS = {
    'missing': ('Saknas i Google',
                'Finns här men inte i Google-kalendern (borttagen där, eller flyttad utanför perioden).',
                'Tryck "Rätta i Google" så läggs det in igen.'),
    'not_synced': ('Aldrig synkad',
                   'Ska finnas i Google-kalendern men har aldrig kommit dit.',
                   'Tryck "Rätta i Google" (samma som "Synka igen" på händelsen).'),
    'cancelled_shown': ('Avbokat pass visas som öppet',
                        'Passet saknar värd här, men ligger kvar i Google som ett vanligt öppet pass. '
                        'Det händer för avbokningar från innan de började tas bort ur Google.',
                        'Tryck "Ta bort ur Google".'),
    'wrong_time': ('Fel tid i Google', 'Tiden i Google stämmer inte med bokningssystemet.',
                   'Tryck "Rätta i Google" så skrivs tiden härifrån dit, eller ändra här om Google har rätt.'),
    'wrong_title': ('Fel rubrik i Google', 'Rubriken i Google stämmer inte med bokningssystemet.',
                    'Tryck "Rätta i Google" så skrivs rubriken härifrån dit.'),
    'wrong_host': ('Fel värd i Google', 'Google visar en annan värd än den som har bokat passet.',
                   'Tryck "Rätta i Google" så skrivs värden härifrån dit.'),
    'only_google': ('Finns bara i Google',
                    'Händelsen finns i Google-kalendern men inte i bokningssystemet.',
                    'Ta bort den i Google, eller lägg till den som händelse här.'),
}
ORDER = list(KINDS)
DEFAULT_DAYS = 90
HOST_LINE = re.compile(r'Värd: (\S+)')


def _when(start, end, all_day):
    if all_day:
        last = end - datetime.timedelta(days=1)
        text = '{} {} {}'.format(DAY_SHORT[start.weekday()], start.day, MONTH_NAMES[start.month - 1][:3])
        if last != start:
            text += ' – {} {} {}'.format(DAY_SHORT[last.weekday()], last.day, MONTH_NAMES[last.month - 1][:3])
        return text + ' (heldag)'
    start, end = timezone.localtime(start), timezone.localtime(end)
    return '{} {} {} {:%H:%M}–{:%H:%M}'.format(
        DAY_SHORT[start.weekday()], start.day, MONTH_NAMES[start.month - 1][:3], start, end)


def _same_time(ours, google):
    (a_start, a_end, a_all), (b_start, b_end, b_all) = ours, google
    if a_all != b_all:
        return False
    if a_all:
        return (a_start, a_end) == (b_start, b_end)
    return abs((a_start - b_start).total_seconds()) < 60 and abs((a_end - b_end).total_seconds()) < 60


def _entry_bounds(entry):
    if entry.all_day:
        days = entry.local_days()
        return days[0], days[-1] + datetime.timedelta(days=1), True
    return entry.start, entry.end, False


def _diff(kind, key, bounds, title, ours='', google='', link=None, google_link=None):
    start = bounds[0]
    return {
        'kind': kind, 'key': key, 'title': title,
        'start': start.isoformat(), 'when': _when(*bounds),
        'ours': ours, 'google': google, 'link': link, 'google_link': google_link,
    }


def compare(calendar, window_start, window_end):
    """The differences between `calendar` in Google and the schedule, in a window."""
    google = {event['id']: event for event in client_for(calendar).list(window_start, window_end)}
    differences = []

    instances = (EventInstance.objects
                 .filter(event__template__calendar=calendar, event__template__synchronize=True,
                         start__lt=window_end, end__gt=window_start)
                 .select_related('event__template', 'host'))
    for instance in instances:
        template = instance.template
        bounds = (instance.start, instance.end, False)
        key = 'slot:{}'.format(instance.pk)
        link = '/scheduler/eventinstance-admin/{}'.format(instance.pk)
        event = google.get(instance.google_calendar_booking_id) if instance.google_calendar_booking_id else None
        if instance.is_published:
            if event is None:
                differences.append(_diff('missing', key, bounds, template.title,
                                         ours='Bokat, värd: {}'.format(instance.host.slackId)
                                         if instance.status in EventInstance.HOSTED_STATUSES else instance.statusText,
                                         link=link))
                continue
            expected = template._createUpdatedEventData(instance.host, instance.start, instance.end, instance.status)
            if not _same_time(bounds, event_bounds(event)):
                differences.append(_diff('wrong_time', key, bounds, template.title,
                                         ours=_when(*bounds), google=_when(*event_bounds(event)),
                                         link=link, google_link=event.get('htmlLink')))
            if event.get('summary', '') != expected['summary']:
                differences.append(_diff('wrong_title', key, bounds, template.title,
                                         ours=expected['summary'], google=event.get('summary', ''),
                                         link=link, google_link=event.get('htmlLink')))
            if instance.status in EventInstance.HOSTED_STATUSES:
                found = HOST_LINE.search(event.get('description') or '')
                if not found or found.group(1) != instance.host.slackId:
                    differences.append(_diff('wrong_host', key, bounds, template.title,
                                             ours='Värd: {}'.format(instance.host.slackId),
                                             google='Värd: {}'.format(found.group(1)) if found else 'ingen värd',
                                             link=link, google_link=event.get('htmlLink')))
        elif event is not None and not event.get('summary', '').startswith(settings.CANCELLED_TITLE):
            found = HOST_LINE.search(event.get('description') or '')
            differences.append(_diff('cancelled_shown', key, bounds, template.title,
                                     ours=instance.statusText + (', ingen värd' if instance.host is None else ''),
                                     google='{}{}'.format(event.get('summary', ''),
                                                          ', värd: {}'.format(found.group(1)) if found else ''),
                                     link=link, google_link=event.get('htmlLink')))

    for entry in CalendarEntry.objects.filter(calendar=calendar, start__lt=window_end, end__gt=window_start):
        bounds = _entry_bounds(entry)
        key = 'entry:{}'.format(entry.pk)
        link = entry.get_absolute_url()
        if not entry.google_event_id:
            differences.append(_diff('not_synced', key, bounds, entry.title,
                                     ours=entry.sync_error or 'Inte skickad än', link=link))
            continue
        event = google.get(entry.google_event_id)
        if event is None:
            differences.append(_diff('missing', key, bounds, entry.title, ours=entry.get_kind_display(), link=link))
            continue
        if not _same_time(bounds, event_bounds(event)):
            differences.append(_diff('wrong_time', key, bounds, entry.title, ours=_when(*bounds),
                                     google=_when(*event_bounds(event)), link=link,
                                     google_link=event.get('htmlLink')))
        if event.get('summary', '') != entry.title:
            differences.append(_diff('wrong_title', key, bounds, entry.title, ours=entry.title,
                                     google=event.get('summary', ''), link=link,
                                     google_link=event.get('htmlLink')))
        if entry.is_special:
            from .entry_sync import hosts_line
            expected = hosts_line(entry)
            first = (event.get('description') or '').split('\n')[0]
            shown = first if first.startswith('Värdar:') else ''
            if shown != expected:
                differences.append(_diff('wrong_host', key, bounds, entry.title, ours=expected,
                                         google=shown or 'ingen värdlista', link=link,
                                         google_link=event.get('htmlLink')))

    ours = set(EventInstance.objects.filter(event__template__calendar=calendar)
               .exclude(google_calendar_booking_id=None).values_list('google_calendar_booking_id', flat=True))
    ours |= set(CalendarEntry.objects.filter(calendar=calendar)
                .exclude(google_event_id=None).values_list('google_event_id', flat=True))
    for event_id, event in google.items():
        if event_id in ours:
            continue
        marker = (event.get('extendedProperties') or {}).get('private', {}).get(PRIVATE_KEY)
        differences.append(_diff('only_google', 'google:{}'.format(event_id), event_bounds(event),
                                 event.get('summary', '(utan rubrik)'),
                                 ours='Borttagen händelse #{}'.format(marker) if marker else 'Finns inte här',
                                 google=(event.get('description') or '').split('\n')[0][:120],
                                 google_link=event.get('htmlLink')))

    differences.sort(key=lambda d: (ORDER.index(d['kind']), d['start']))
    return differences


def run(calendar, days=DEFAULT_DAYS, ran_by=None):
    """Compare from today's midnight and `days` ahead, and store the result."""
    window_start = timezone.make_aware(datetime.datetime.combine(timezone.localdate(), datetime.time()))
    window_end = window_start + datetime.timedelta(days=days)
    check = CalendarCheck(calendar=calendar, ran_by=ran_by, window_start=window_start, window_end=window_end)
    try:
        check.differences = compare(calendar, window_start, window_end)
    except Exception as error:
        check.error = '{}: {}'.format(type(error).__name__, error)[:2000]
    check.save()
    return check


def latest_checks():
    """The most recent check of every calendar that has been checked."""
    from .models import SchedulingCalendar
    checks = []
    for calendar in SchedulingCalendar.objects.order_by('name'):
        check = calendar.checks.first()
        if check:
            checks.append(check)
    return checks


def grouped(check):
    """The check's differences as [(kind, heading, meaning, action, [diffs])], in ORDER."""
    groups = []
    for kind in ORDER:
        items = [d for d in check.differences if d['kind'] == kind]
        if items:
            heading, meaning, action = KINDS[kind]
            groups.append({'kind': kind, 'heading': heading, 'meaning': meaning,
                           'action': action, 'items': items, 'fix_label': FIX_LABELS.get(kind)})
    return groups


def calendars_to_check():
    """Calendars something here writes to: a synchronizing template, or an entry."""
    from django.db.models import Q
    from .models import SchedulingCalendar
    return (SchedulingCalendar.objects
            .filter(Q(eventtemplate__synchronize=True) | Q(entries__isnull=False))
            .distinct().order_by('name'))


def attention_count():
    """Differences (and failed runs) in the latest check of each calendar, for the menu badge."""
    return sum(len(check.differences) + (1 if check.error else 0) for check in latest_checks())


# Kinds that can be fixed by writing the schedule's version to Google. A
# Google-only event is left to a person: it may have been added on purpose.
FIX_LABELS = {kind: 'Rätta i Google' for kind in KINDS if kind != 'only_google'}
FIX_LABELS['cancelled_shown'] = 'Ta bort ur Google'


def fix(difference):
    """Make Google match the schedule for one difference. Returns an error text, or ''."""
    from . import entry_sync
    what, _, ident = difference['key'].partition(':')
    if difference['kind'] not in FIX_LABELS:
        return 'Kan inte rättas automatiskt'
    try:
        if what == 'slot':
            instance = EventInstance.objects.get(pk=ident)
            if difference['kind'] == 'missing':
                instance.google_calendar_booking_id = None  # gone in Google: create anew
            # save() writes Google from the slot: removes the event of a slot
            # without a host, updates or creates one for a booked or cancelled slot.
            instance.save()
        elif what == 'entry':
            entry = CalendarEntry.objects.get(pk=ident)
            if not entry_sync.push(entry):
                return entry.sync_error
        else:
            return 'Kan inte rättas automatiskt'
    except (EventInstance.DoesNotExist, CalendarEntry.DoesNotExist):
        return 'Finns inte längre här'
    except Exception as error:
        return '{}: {}'.format(type(error).__name__, error)
    return ''


def fix_in_check(check, keys):
    """Fix the differences with these keys and drop the fixed ones from the stored check."""
    fixed, failed = [], []
    for difference in [d for d in check.differences if d['key'] in keys]:
        error = fix(difference)
        (failed if error else fixed).append((difference, error))
    done = {d['key'] for d, _ in fixed}
    check.differences = [d for d in check.differences if d['key'] not in done]
    check.save(update_fields=['differences'])
    return fixed, failed
