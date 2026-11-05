"""
Prototype: book host slots one week at a time.

Weeks are ISO 8601 weeks, so they always start on Monday and the week
numbers match the ones printed in Swedish calendars.
"""
import datetime

import pytz
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from .booking_rules import Closures, DependencyChecker
from .host_progress import progress_for
from .models import BookingChange, CalendarEntry, Event, EventInstance, SchedulingPeriod, SchedulingRuleExclusion

DAY_NAMES = ["måndag", "tisdag", "onsdag", "torsdag",
             "fredag", "lördag", "söndag"]
DAY_SHORT = ["må", "ti", "on", "to", "fr", "lö", "sö"]
MONTH_NAMES = ["januari", "februari", "mars", "april", "maj", "juni", "juli",
               "augusti", "september", "oktober", "november", "december"]

ONE_DAY = datetime.timedelta(days=1)


def _local_midnight(day):
    tz = pytz.timezone(settings.TIME_ZONE)
    return tz.localize(datetime.datetime.combine(day, datetime.time()))


def _week_url(day):
    iso = day.isocalendar()
    return reverse('week-view', args=[iso.year, iso.week])


def _slots_by_day(first_day, last_day):
    """
    Every occurrence, generated or already in the database, whose local start
    date is within [first_day, last_day], grouped by that date.
    """
    # One extra day on each side: generation works on UTC dates, which differ
    # from local dates around midnight. The filter below trims it back.
    start = _local_midnight(first_day - ONE_DAY).astimezone(pytz.utc)
    end = _local_midnight(last_day + 2 * ONE_DAY).astimezone(pytz.utc)
    by_day = {}
    for event in Event.objects.select_related('template', 'rule'):
        for instance in event.get_events(start, end):
            day = timezone.localtime(instance.start).date()
            if first_day <= day <= last_day:
                by_day.setdefault(day, []).append(instance)
    for slots in by_day.values():
        slots.sort(key=lambda instance: instance.start)
    return by_day


def _entries_by_day(first_day, last_day, user):
    """Calendar entries touching [first_day, last_day], listed on every day they touch."""
    start = _local_midnight(first_day)
    end = _local_midnight(last_day + ONE_DAY)
    by_day = {}
    for entry in CalendarEntry.objects.filter(start__lt=end, end__gt=start):
        entry.editable = entry.can_edit(user)
        for day in entry.local_days():
            if first_day <= day <= last_day:
                by_day.setdefault(day, []).append(entry)
    return by_day


def _day_items(day, slots, entries):
    """
    Timed entries and slots in one list by start time, so a 19:00 meeting
    sits between the 15-18 and 18-21 slots. Entries running past midnight
    sort from the start of the days they continue on.
    """
    midnight = _local_midnight(day)
    items = [{'slot': slot, 'at': slot.start} for slot in slots]
    items += [{'entry': entry, 'at': max(entry.start, midnight)}
              for entry in entries if not entry.all_day]
    return sorted(items, key=lambda item: item['at'])


def _describe(instance, user, now, checker):
    """Decorate an instance with what the week view shows and allows."""
    instance.is_past = instance.start <= now
    instance.start_iso = instance.start.isoformat()
    instance.action = None
    instance.hint = None
    instance.warning = None
    if instance.status == 2:
        instance.state, instance.label = 'cancelled', 'Inställt'
    elif instance.status == 1 and instance.host_id == user.id:
        instance.state, instance.label = 'mine', 'Du är värd'
        instance.action = 'Boka av'
    elif instance.status == 1:
        instance.state = 'taken'
        instance.label = 'Värd: ' + instance.host.slackId if instance.host else 'Bokat'
    elif instance.status == -1:
        instance.state, instance.label = 'rebook', 'Ombokning krävs'
        instance.action = 'Boka'
    else:
        instance.state, instance.label = 'free', 'Ledigt'
        instance.action = 'Boka'
    if not instance.is_past and instance.state in ('free', 'rebook', 'mine', 'taken'):
        closing = checker.closing(instance)
        unmet = checker.unmet(instance)
        if closing and instance.state in ('free', 'rebook'):
            # A "Stängt" entry overlaps the slot: nobody can book it.
            instance.state, instance.label = 'closed', 'Stängt'
            instance.hint = Closures.describe(closing)
            instance.action = None
        elif closing:
            # Booked before the closure was added: keep it, but say so.
            instance.warning = 'Lokalen är stängd: {}'.format(Closures.describe(closing))
        elif unmet and instance.state in ('free', 'rebook'):
            # Not bookable yet: say what would open it.
            instance.state, instance.label = 'locked', 'Låst'
            instance.hint = 'Öppnas när {} har en värd'.format(checker.describe(unmet))
            instance.action = None
        elif unmet:
            # Booked, but what it depends on lost its host (a cancellation).
            instance.warning = '{} saknar värd'.format(checker.describe(unmet))
    if instance.is_past:
        instance.action = None
    return instance


def _period_for(day):
    """The period covering `day`, else the next one to start, else the latest."""
    return (SchedulingPeriod.get_period_for_date(day)
            or SchedulingPeriod.objects.filter(start__gt=day).order_by('start').first()
            or SchedulingPeriod.objects.order_by('-end').first())


def _segment(slot):
    """One small bar per slot in the picker: what state is that slot in?"""
    if slot.is_past:
        return 'done' if slot.status == 1 else 'past'
    return slot.state


def _period_picker(period, selected_monday, by_day, excluded, today, entries=None):
    """
    Every ISO week of `period`, Monday first, grouped under the month that
    holds the week's Thursday (so a week never appears twice). Each row is
    one clickable week; each day shows one bar per slot.
    """
    months = []
    monday = period.start - period.start.weekday() * ONE_DAY
    while monday <= period.end:
        week = [monday + offset * ONE_DAY for offset in range(7)]
        thursday = week[3]
        if not months or months[-1]['month'] != (thursday.year, thursday.month):
            months.append({'month': (thursday.year, thursday.month),
                           'name': MONTH_NAMES[thursday.month - 1], 'weeks': []})
        days = []
        for day in week:
            in_period = period.start <= day <= period.end
            days.append({
                'date': day,
                'outside': not in_period,
                'first_of_month': day.day == 1,
                'today': day == today,
                'excluded': excluded.get(day),
                'segments': [_segment(s) for s in by_day.get(day, [])] if in_period else [],
                'free': sum(1 for s in by_day.get(day, [])
                            if in_period and not s.is_past and s.state in ('free', 'rebook')),
                'entries': (entries or {}).get(day, [])[:3] if in_period else [],
            })
        months[-1]['weeks'].append({
            'number': monday.isocalendar().week,
            'url': _week_url(monday),
            'selected': monday == selected_monday,
            'days': days,
        })
        monday += 7 * ONE_DAY

    previous = SchedulingPeriod.objects.filter(start__lt=period.start).order_by('-start').first()
    following = SchedulingPeriod.objects.filter(start__gt=period.start).order_by('start').first()
    return {
        'period': period,
        'covers_week': period.start <= selected_monday + 6 * ONE_DAY and selected_monday <= period.end,
        'prev_url': _week_url(previous.start) if previous else None,
        'prev_name': previous.name if previous else None,
        'next_url': _week_url(following.start) if following else None,
        'next_name': following.name if following else None,
        'day_names': DAY_SHORT,
        'months': months,
    }


@login_required
def week_view(request, year=None, week=None):
    today = timezone.localdate()
    if year is None:
        monday = today - today.weekday() * ONE_DAY
    else:
        try:
            monday = datetime.date.fromisocalendar(year, week, 1)
        except ValueError:
            raise Http404("No such week")
    sunday = monday + 6 * ONE_DAY

    period = _period_for(monday)
    first_day, last_day = monday, sunday
    if period:
        first_day = min(monday, period.start - period.start.weekday() * ONE_DAY)
        last_day = max(sunday, period.end + (6 - period.end.weekday()) * ONE_DAY)
    now = timezone.now()
    by_day = _slots_by_day(first_day, last_day)
    checker = DependencyChecker(first_day, last_day)
    for slots in by_day.values():
        for instance in slots:
            _describe(instance, request.user, now, checker)
    excluded = dict(SchedulingRuleExclusion.objects.filter(
        excluded_date__range=(first_day, last_day)).values_list('excluded_date', 'description'))
    entries = _entries_by_day(first_day, last_day, request.user)

    days = []
    for offset in range(7):
        day = monday + offset * ONE_DAY
        days.append({
            'date': day,
            'name': DAY_NAMES[offset],
            'today': day == today,
            'past': day < today,
            'excluded': excluded.get(day),
            'slots': by_day.get(day, []),
            'banners': [entry for entry in entries.get(day, []) if entry.all_day],
            'items': _day_items(day, by_day.get(day, []), entries.get(day, [])),
        })

    if monday.month == sunday.month:
        span = '{}–{} {} {}'.format(monday.day, sunday.day, MONTH_NAMES[monday.month - 1], monday.year)
    else:
        span = '{} {} – {} {} {}'.format(monday.day, MONTH_NAMES[monday.month - 1][:3],
                                        sunday.day, MONTH_NAMES[sunday.month - 1][:3], sunday.year)

    return render(request, 'week_view.html', {
        'week': {
            'number': monday.isocalendar().week,
            'span': span,
            'is_current': monday <= today <= sunday,
            'prev_url': _week_url(monday - 7 * ONE_DAY),
            'prev_number': (monday - 7 * ONE_DAY).isocalendar().week,
            'next_url': _week_url(monday + 7 * ONE_DAY),
            'next_number': (monday + 7 * ONE_DAY).isocalendar().week,
            'free_count': sum(1 for d in days for s in d['slots'] if s.state in ('free', 'rebook') and not s.is_past),
            'mine_count': sum(1 for d in days for s in d['slots'] if s.state == 'mine'),
        },
        'days': days,
        'picker': _period_picker(period, monday, by_day, excluded, today, entries) if period else None,
        'entry_kinds': CalendarEntry.KINDS,
        'progress': progress_for(period, [request.user], now)[0] if period else None,
    })


@login_required
@require_POST
def week_toggle(request):
    """Book or unbook one slot, identified by its Event and start time."""
    event = get_object_or_404(Event, pk=request.POST.get('event'))
    start = parse_datetime(request.POST.get('start', ''))
    if start is None:
        raise Http404("Bad start time")
    back = _week_url(timezone.localtime(start).date())

    # Rebuild the occurrence on the server instead of trusting posted fields,
    # so a slot can only be booked at a time its Event actually generates.
    matches = [i for i in event.get_events(start - ONE_DAY, start + ONE_DAY)
               if i.start == start]
    if not matches:
        messages.error(request, 'Passet finns inte längre.')
        return redirect(back)
    instance = matches[0]
    day = timezone.localtime(instance.start).date()
    when = timezone.localtime(instance.start).strftime('%Y-%m-%d %H:%M')
    checker = DependencyChecker(day, day)
    closing = checker.closing(instance)
    unmet = checker.unmet(instance)

    if instance.start <= timezone.now():
        messages.error(request, 'Passet {} har redan börjat.'.format(when))
    elif instance.status == 1 and instance.host_id == request.user.id:
        instance.status = -1
        instance.save()
        BookingChange.log(instance, 'cancelled', by=request.user)
        messages.success(request, 'Du har bokat av {}.'.format(when))
        # Cancelling is always allowed, but say what it leaves stranded.
        checker = DependencyChecker(day, day)
        stranded = [i for i in EventInstance.objects.filter(
                        status=1, start__date=day).select_related('event', 'host')
                    if checker.unmet(i)]
        for other in stranded:
            messages.warning(request, '{} {} ({}) bygger på passet du bokade av och saknar nu {}.'.format(
                other.event.name, timezone.localtime(other.start).strftime('%H:%M'),
                'ditt' if other.host_id == request.user.id else 'värd: ' + other.host.slackId,
                checker.describe(checker.unmet(other))))
    elif instance.status in (0, -1) and closing:
        messages.error(request, 'Passet {} kan inte bokas, lokalen är stängd: {}.'.format(
            when, Closures.describe(closing)))
    elif instance.status in (0, -1) and unmet:
        messages.error(request, 'Passet {} kan bokas först när {} har en värd samma dag.'.format(
            when, DependencyChecker.describe(unmet)))
    elif instance.status in (0, -1):
        instance.status = 1
        instance.host = request.user
        instance.save()
        BookingChange.log(instance, 'booked', by=request.user)
        messages.success(request, 'Du är värd för {}.'.format(when))
    else:
        messages.error(request, 'Passet {} är redan bokat av någon annan.'.format(when))
    return redirect(back)
