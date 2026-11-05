"""
Booking reminders: the email a host gets when they are behind, and sending it.
"""
import datetime

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .booking_rules import DependencyChecker
from .models import HostReminder
from .views_week import DAY_SHORT, MONTH_NAMES, _slots_by_day

# Hosts in these states get reminders.
NEEDS_REMINDER = ('none', 'behind')
SLOTS_LISTED = 8
LOOKAHEAD_DAYS = 14


def site_url(request=None):
    if getattr(settings, 'SITE_URL', None):
        return settings.SITE_URL.rstrip('/')
    if request is not None:
        return request.build_absolute_uri('/').rstrip('/')
    return 'http://localhost:8000'


def bookable_soon(now=None):
    """Slots in the coming weeks that a host could book right now."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    last = today + datetime.timedelta(days=LOOKAHEAD_DAYS - 1)
    checker = DependencyChecker(today, last)
    slots = []
    for day, day_slots in sorted(_slots_by_day(today, last).items()):
        for slot in day_slots:
            if (slot.start > now and slot.status in (0, -1)
                    and not checker.closing(slot) and not checker.replacing(slot)
                    and not checker.unmet(slot)):
                slots.append(slot)
    return slots


def special_passes_soon(now=None):
    """Special passes in the coming weeks that still have free host seats."""
    from .models import CalendarEntry
    now = now or timezone.now()
    found = []
    for entry in (CalendarEntry.objects.filter(kind='special', start__gt=now,
                                               start__lt=now + datetime.timedelta(days=LOOKAHEAD_DAYS))
                  .select_related('special_type').prefetch_related('seats').order_by('start')):
        entry.free_seats = entry.hosts_needed - len(entry.seats.all())
        if entry.free_seats > 0:
            found.append(entry)
    return found


def _special_line(entry):
    start, end = timezone.localtime(entry.start), timezone.localtime(entry.end)
    return '  {} {} {} {:%H:%M}–{:%H:%M}  {} ({} {} kvar{})'.format(
        DAY_SHORT[start.weekday()], start.day, MONTH_NAMES[start.month - 1][:3], start, end, entry.title,
        entry.free_seats, 'plats' if entry.free_seats == 1 else 'platser',
        ', räknas som värdpass' if entry.counts_as_session else '')


def _slot_line(slot):
    start, end = timezone.localtime(slot.start), timezone.localtime(slot.end)
    return '  {} {} {} {:%H:%M}–{:%H:%M}  {}'.format(
        DAY_SHORT[start.weekday()], start.day, MONTH_NAMES[start.month - 1][:3],
        start, end, slot.event.name if slot.event else slot.title)


def compose(progress, base_url, slots, specials=()):
    """(subject, body) for one host's reminder, in Swedish."""
    period = progress.period
    days_left = max(0, (period.end - timezone.localdate()).days)
    subject = 'Påminnelse: boka dina värdpass för {}'.format(period.name)

    lines = ['Hej {}!'.format(progress.user.slackId), '']
    lines.append('Det är {} dagar kvar av perioden {}.'.format(days_left, period.name))
    if progress.host_committed == 0 and not progress.participant_keys:
        lines.append('Du har inte bokat några pass än.')
    else:
        lines.append('Du har bokat {} av {} värdpass.'.format(
            progress.host_committed, progress.host_required))
    todo = []
    if progress.host_missing:
        todo.append('{} värdpass'.format(progress.host_missing))
    if progress.missing_keys:
        todo.append('deltagarpass {}'.format(', '.join(progress.missing_keys)))
    if todo:
        lines.append('Kvar att boka: {}.'.format(' och '.join(todo)))
    lines.append('')
    if slots:
        lines.append('Lediga pass de närmaste två veckorna:')
        lines.extend(_slot_line(slot) for slot in slots[:SLOTS_LISTED])
        if len(slots) > SLOTS_LISTED:
            lines.append('  ...och {} till.'.format(len(slots) - SLOTS_LISTED))
        lines.append('')
    if specials:
        lines.append('Specialpass som behöver fler värdar:')
        lines.extend(_special_line(entry) for entry in specials[:SLOTS_LISTED])
        lines.append('')
    lines.append('Boka i veckovyn: {}/scheduler/week/'.format(base_url))
    lines.append('')
    lines.append('Kan du inte vara värd den här perioden? Svara på det här mejlet, '
                 'så planerar vi efter det.')
    lines.append('')
    lines.append('Hälsningar,')
    lines.append('MakersLink bokningssystem')
    return subject, '\n'.join(lines)


def send_reminder(progress, base_url, slots, sent_by=None, specials=()):
    """Email one host and log it. Returns the HostReminder."""
    subject, body = compose(progress, base_url, slots, specials)
    send_mail(subject, body, None, [progress.user.email])
    return HostReminder.objects.create(user=progress.user, period=progress.period, sent_by=sent_by)
